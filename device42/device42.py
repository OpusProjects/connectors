#!/usr/bin/env python3
"""Ansible dynamic inventory for Device42, through DOQL queries.

How a run works:
  1. Two DOQL queries in parallel: the devices joined with their addresses,
     and the custom fields.
  2. Each address's type tells whether it is shared (a VIP) or the host's own.
  3. Hosts whose addresses are all of unknown type are looked up in DNS.
  4. Hosts with several addresses get an SSH reachability probe, which picks
     the address ansible_host should use.
  5. The inventory is printed as JSON on stdout.

What the inventory holds is decided by a folder of filter files (the
config_dir setting), so one script serves several inventories: which domains
and devices to include, which fields become host variables and which become
groups. filters.example/ shows every file. Files are YAML (*.yml, *.yaml; needs
PyYAML) or JSON (*.json; no dependency).

Commands:
  --list                    the whole inventory
  --host NAME               one host's variables
  --host-with-groups A,B    the --list shape for the named hosts only, looked
                            up one by one (what a host-scoped sync calls)

Credentials: CREDENTIAL_BASE_URL, CREDENTIAL_USERNAME and CREDENTIAL_PASSWORD
as unified-api hands them over, else D42_BASE_URL, D42_USERNAME and
D42_PASSWORD. The credential_prefix setting picks another prefix.

Settings, from the source's config (SOURCE_CONFIG, JSON; strings accepted):
  config_dir               the filter folder (required)
  credential_prefix        read <prefix>_BASE_URL, _USERNAME and _PASSWORD
  insecure                 true to skip TLS verification
  connect_timeout_seconds  default 30
  read_timeout_seconds     default 120
  ssh_probe                default true
  ssh_port                 default 22
  ssh_timeout_seconds      default 0.3
  ssh_workers              default max(64, 32 per CPU)
  ssh_skip_when_dns        default true: no probe for a host DNS resolved
  dns_fallback             default true
  prefer_dns               default true
  dns_workers              default ssh_workers
  log_level                default WARNING
"""

import argparse
import base64
import csv
import io
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# The SDK sits at the repository root, one folder up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unified_connector import ConnectorError, as_bool, as_int, settings

log = logging.getLogger("device42")

# Network errors and timeouts are OSError (URLError and HTTPError among them).
FAILURES = (OSError, ValueError)

# Statuses retried with backoff. 500 is not: DOQL answers a bad query with it.
RETRY_STATUSES = (429, 502, 503, 504)

# Never exposed as host variables: structural fields and DOQL join columns.
INTERNAL_FIELDS = frozenset(
    {
        "name",
        "ip_addresses",
        "custom_fields",
        "device_pk",
        "ip_address",
        "ip_label",
        "ip_type",
    }
)

# REST field names (the ones the filter files use) whose view_device_v2 column
# is named differently. Only the names that differ are listed.
REST_TO_DOQL = {
    "customer": "customer_fk",
    "os": "os_name",
    "osver": "os_version",
    "osverno": "os_version_no",
    "osarch": "os_architecture",
    "hw_model": "hardware_fk",
    "rack": "rack_fk",
    "blade_host_name": "host_chassis_device_fk",
}

# Fields read off the device's customer, through one LEFT JOIN on
# view_customer_v1. customer_email is the customer's contact_info, where
# Device42 keeps the owner's address.
CUSTOMER_FIELDS = {"customer": "c.name", "customer_email": "c.contact_info"}

# Fields that are SQL expressions rather than plain columns.
REST_TO_DOQL_EXPR = {
    "device_sub_type": "COALESCE(NULLIF(d.virtualsubtype, ''), d.physicalsubtype)",
    "virtual_host_name": (
        "(SELECT vh.name FROM view_device_v2 AS vh WHERE vh.device_pk = d.virtual_host_device_fk)"
    ),
}


# ---------------------------------------------------------------------------
# Filter files
# ---------------------------------------------------------------------------


def _load_file(folder, stem, empty):
    """Load one filter file by name, trying .yml, .yaml and .json.

    Args:
        folder: The filter folder.
        stem: The file name without extension, such as d42_domains.
        empty: What an absent or empty file means: [] or {}.

    Returns:
        The file's content.

    Raises:
        ConnectorError: The file is YAML and PyYAML is not installed, or its
            top level is not the expected list or mapping.
    """
    for ext in (".yml", ".yaml", ".json"):
        path = os.path.join(folder, stem + ext)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if ext == ".json":
            data = json.loads(text) if text.strip() else None
        else:
            try:
                import yaml  # optional: only YAML filter files need it
            except ImportError:
                raise ConnectorError(
                    f"{path}: YAML filter files need PyYAML; install it or use .json"
                ) from None
            data = yaml.safe_load(text)
        data = empty if data is None else data
        if not isinstance(data, type(empty)):
            kind = "a mapping" if isinstance(empty, dict) else "a list"
            raise ConnectorError(f"{path} must contain {kind} at the top level")
        return data
    log.warning("filter file not found, using an empty default: %s", stem)
    return empty


def _patterns(filters, key):
    """Lowercase the patterns under avoid or required, per field."""
    return {
        field: [p.lower() for p in (patterns or []) if isinstance(p, str)]
        for field, patterns in (filters.get(key) or {}).items()
        if isinstance(patterns, list)
    }


class Config:
    """Everything a run reads: the filter folder, the settings, the credentials."""

    def __init__(self, source, environ):
        """Read the filter folder and the settings.

        Args:
            source: The source's settings.
            environ: The environment, for the credentials.

        Raises:
            ConnectorError: config_dir is missing or the credentials are incomplete.
        """
        folder = source.get("config_dir")
        if not folder:
            raise ConnectorError(
                "config_dir is not set: the folder holding the filter files"
            )
        filters = _load_file(folder, "d42_device_filters", {})
        self.domains = _load_file(folder, "d42_domains", [])
        self.excluded_hosts = set(_load_file(folder, "d42_excluded_hosts", []))
        self.group_fields = _load_file(folder, "d42_group_fields", [])
        self.group_custom_fields = _load_file(folder, "d42_group_custom_fields", [])
        self.host_fields = _load_file(folder, "d42_host_fields", [])
        self.host_custom_fields = _load_file(folder, "d42_host_custom_fields", [])
        aliases = _load_file(folder, "d42_field_aliases", {})
        self.inventory = _load_file(folder, "d42_settings", {})

        self.alias_reverse = {v: k for k, v in aliases.items()}
        self.avoid = _patterns(filters, "avoid")
        self.required = _patterns(filters, "required")
        self.all_host_fields = list(dict.fromkeys(self.host_fields + self.group_fields))
        self.all_custom_fields = list(
            dict.fromkeys(self.host_custom_fields + self.group_custom_fields)
        )
        self.include_no_ip = bool(self.inventory.get("include_hosts_without_ip", False))
        self.datacenter_networks = {
            str(dc): [ipaddress.ip_network(c, strict=False) for c in (cidrs or [])]
            for dc, cidrs in (self.inventory.get("datacenter_networks") or {}).items()
        }
        self.datacenter_domains = {
            str(suffix).lower(): str(dc)
            for suffix, dc in (self.inventory.get("datacenter_domains") or {}).items()
        }

        self.ssh_probe = as_bool(source.get("ssh_probe"), True)
        self.ssh_port = as_int(source.get("ssh_port"), 22)
        self.ssh_timeout = _as_float(source.get("ssh_timeout_seconds"), 0.3)
        self.ssh_workers = as_int(
            source.get("ssh_workers"), max(64, (os.cpu_count() or 1) * 32)
        )
        self.dns_fallback = as_bool(source.get("dns_fallback"), True)
        self.prefer_dns = as_bool(source.get("prefer_dns"), True)
        self.dns_skips_ssh = as_bool(source.get("ssh_skip_when_dns"), True)
        self.dns_workers = as_int(source.get("dns_workers"), self.ssh_workers)

        prefix = source.get("credential_prefix")
        prefixes = [prefix] if prefix else ["CREDENTIAL", "D42"]
        for p in prefixes:
            url, user, password = (
                environ.get(f"{p}_{k}", "").strip()
                for k in ("BASE_URL", "USERNAME", "PASSWORD")
            )
            if url and user and password:
                break
        else:
            names = " or ".join(
                f"{p}_BASE_URL, {p}_USERNAME, {p}_PASSWORD" for p in prefixes
            )
            raise ConnectorError(f"no Device42 credentials: set {names}")
        self.base_url = url.rstrip("/")
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.context = ssl.create_default_context()
        if as_bool(source.get("insecure")):
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE
        self.timeout = max(
            as_int(source.get("connect_timeout_seconds"), 30),
            as_int(source.get("read_timeout_seconds"), 120),
        )


def _as_float(value, default):
    """Read a decimal setting, given as a string or a number."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConnectorError(f"not a number: {value!r}") from None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class Device42:
    """The two calls the inventory makes: DOQL queries and REST device lookups."""

    def __init__(self, config):
        """Keep the address, authorization and TLS settings."""
        self.config = config

    def _request(self, path, data=None):
        """Send one request, retrying throttling and gateway errors.

        Returns:
            The response body.
        """
        url = self.config.base_url + path
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        for attempt in range(4):
            request = urllib.request.Request(
                url,
                data=body,
                headers={
                    "Authorization": self.config.auth,
                    "User-Agent": "unified-connectors",
                },
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.config.timeout, context=self.config.context
                ) as response:
                    return response.read()
            except urllib.error.HTTPError as e:
                if e.code not in RETRY_STATUSES or attempt == 3:
                    raise
                time.sleep(0.5 * 2**attempt)
        return b""

    def doql(self, sql):
        """Run a DOQL query.

        Returns:
            The rows as dicts.

        Raises:
            OSError: The query failed (a bad query answers 500 and is not retried).
        """
        text = self._request(
            "/services/data/v1.0/query/",
            {"query": sql, "output_type": "csv", "header": "yes"},
        ).decode("utf-8")
        return list(csv.DictReader(io.StringIO(text)))

    def device(self, name):
        """Look one device up by name; an empty dict when Device42 has none."""
        quoted = urllib.parse.quote(name, safe="")
        try:
            raw = self._request(f"/api/1.0/devices/name/{quoted}/")
        except FAILURES as e:
            log.error("lookup failed for %s: %s", name, e)
            return {}
        return json.loads(raw) if raw else {}


def _sql_str(value):
    """Escape a DOQL string literal."""
    return value.replace("'", "''")


def _sql_ident(value):
    """Escape a DOQL identifier."""
    return value.replace('"', '""')


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def build_device_ip_query(cfg):
    """Build the DOQL query: devices joined with their addresses and customer."""
    customer_fields = [f for f in CUSTOMER_FIELDS if f in cfg.all_host_fields]
    skip = INTERNAL_FIELDS | set(customer_fields)
    select_parts, seen = [], set()
    for rest_name in cfg.all_host_fields:
        source_name = cfg.alias_reverse.get(rest_name, rest_name)
        if source_name in skip:
            continue
        if source_name in REST_TO_DOQL_EXPR:
            select_parts.append(
                f'{REST_TO_DOQL_EXPR[source_name]} AS "{_sql_ident(rest_name)}"'
            )
            continue
        column = REST_TO_DOQL.get(source_name, source_name)
        if column in seen:
            continue
        seen.add(column)
        alias = f' AS "{_sql_ident(rest_name)}"' if column != rest_name else ""
        select_parts.append(f"d.{column}{alias}")

    select_device = (",\n    ".join(select_parts) + ",") if select_parts else ""
    customer_cols = " ".join(f"{CUSTOMER_FIELDS[f]} AS {f}," for f in customer_fields)
    customer_join = (
        "LEFT JOIN view_customer_v1 AS c ON c.customer_pk = d.customer_fk"
        if customer_fields
        else ""
    )
    if cfg.domains:
        clauses = " OR ".join(f"d.name ILIKE '%{_sql_str(d)}'" for d in cfg.domains)
        domain_filter = f"({clauses})"
    else:
        domain_filter = "TRUE"
    if cfg.excluded_hosts:
        listed = ", ".join(f"'{_sql_str(h)}'" for h in cfg.excluded_hosts)
        excluded_filter = f"d.name NOT IN ({listed})"
    else:
        excluded_filter = "TRUE"
    ip_join = "LEFT JOIN" if cfg.include_no_ip else "JOIN"
    ip_filter = "" if cfg.include_no_ip else "AND i.ip_address IS NOT NULL\n  "
    return f"""
SELECT
    d.device_pk,
    d.name,
    {select_device}
    {customer_cols}
    i.ip_address,
    i.label  AS ip_label,
    i.type   AS ip_type
FROM view_device_v2 AS d
{ip_join} view_ipaddress_v1 AS i ON i.device_fk = d.device_pk
{customer_join}
WHERE d.name IS NOT NULL
  {ip_filter}AND {domain_filter}
  AND {excluded_filter}
"""


def fetch_custom_fields(cfg, api):
    """Fetch the configured custom fields.

    Returns:
        Each device_pk mapped to its [{"key", "value"}] list.
    """
    if not cfg.all_custom_fields:
        return {}
    # One row tells which configured keys exist as columns of the flat view.
    probe = api.doql("SELECT * FROM view_device_custom_fields_flat_v2 AS cf LIMIT 1")
    if not probe:
        return {}
    existing = set(probe[0])
    valid = [k for k in cfg.all_custom_fields if k in existing]
    for key in cfg.all_custom_fields:
        if key not in existing:
            log.warning("custom field %r is not in Device42, skipping", key)
    if not valid:
        return {}
    columns = ", ".join(f'cf."{_sql_ident(k)}"' for k in valid)
    result = defaultdict(list)
    for row in api.doql(
        f"SELECT cf.device_fk, {columns} FROM view_device_custom_fields_flat_v2 AS cf"
    ):
        pk = (row.get("device_fk") or "").strip()
        if not pk:
            continue
        for key in valid:
            value = (row.get(key) or "").strip()
            if value:
                result[pk].append({"key": key, "value": value})
    return dict(result)


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------


def _shared(ip_type):
    """Read an address type: 1 is shared, empty is unknown, anything else is the host's."""
    raw = (ip_type or "").strip()
    if raw == "1":
        return True
    if not raw:
        return None
    return False


def collect_ip_entries(raw):
    """Deduplicate and normalise address rows, dropping IPv6."""
    seen, out = set(), []
    for e in raw:
        ip = (e.get("ip") or e.get("ip_address") or "").strip()
        if not ip or ip in seen or ":" in ip:
            continue
        seen.add(ip)
        shared = _shared(e.get("ip_type", "")) if "ip_type" in e else e.get("shared")
        out.append(
            {
                "ip": ip,
                "label": (e.get("label") or e.get("ip_label") or "").strip(),
                "type": e.get("type") or e.get("ip_type"),
                "shared": shared,
            }
        )
    return out


def primary_ip(entries):
    """Pick the address to use: ens*, then eth*, then any of the host's, unknown, shared."""
    for prefix in ("ens", "eth"):
        for e in entries:
            if e["shared"] is False and e["label"].startswith(prefix):
                return e["ip"]
    for e in entries:
        if e["shared"] is False:
            return e["ip"]
    for e in entries:
        if e["shared"] is None:
            return e["ip"]
    return entries[0]["ip"] if entries else None


def _enrich_ips(device, raw):
    """Attach the parsed addresses and the chosen one to a device."""
    entries = collect_ip_entries(raw)
    device["_ip_entries"] = entries
    device["_all_ips"] = {
        e["ip"]: {"label": e["label"], "type": e["type"], "shared": e["shared"]}
        for e in entries
        if e.get("ip")
    }
    device["_primary_ip"] = primary_ip(entries)


def matches_filters(cfg, device):
    """Tell whether a device passes the avoid and required patterns."""
    for field, patterns in cfg.avoid.items():
        value = device.get(field)
        if isinstance(value, str) and any(p in value.lower() for p in patterns):
            return False
    for field, patterns in cfg.required.items():
        if not patterns:
            continue
        value = device.get(field)
        if not isinstance(value, str) or not any(p in value.lower() for p in patterns):
            return False
    return True


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


def group_rows(cfg, rows, custom_fields):
    """Collapse the one-row-per-address join into one device per device_pk."""
    grouped, ip_rows = {}, defaultdict(list)
    for row in rows:
        pk = (row.get("device_pk") or "").strip()
        if not pk:
            continue
        if pk not in grouped:
            grouped[pk] = {
                k: v
                for k, v in row.items()
                if k not in ("ip_address", "ip_label", "ip_type")
            }
            grouped[pk]["custom_fields"] = custom_fields.get(pk, [])
        ip_rows[pk].append(row)
    devices = []
    for pk, device in grouped.items():
        _enrich_ips(device, ip_rows[pk])
        if matches_filters(cfg, device) and (
            device["_primary_ip"] or cfg.include_no_ip
        ):
            devices.append(device)
    return devices


def fetch_all_devices(cfg, api):
    """Fetch every device the filters keep, with two DOQL queries in parallel."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = pool.submit(api.doql, build_device_ip_query(cfg))
        custom = pool.submit(fetch_custom_fields, cfg, api)
        rows, custom = rows.result(), custom.result()
    log.info("device query returned %d rows", len(rows))
    return group_rows(cfg, rows, custom) if rows else []


class CustomerEmails:
    """Each customer's contact address, fetched once however many hosts ask."""

    def __init__(self, api):
        """Remember the API; nothing is fetched until the first lookup."""
        self.api = api
        self.lock = threading.Lock()
        self.cache = None

    def get(self, customer_id):
        """Return one customer's contact address, or an empty string."""
        with self.lock:
            if self.cache is None:
                rows = self.api.doql(
                    "SELECT c.customer_pk, c.contact_info FROM view_customer_v1 AS c"
                )
                self.cache = {
                    (r.get("customer_pk") or "").strip(): (
                        r.get("contact_info") or ""
                    ).strip()
                    for r in rows
                }
            return self.cache.get(str(customer_id), "")


def fetch_device(cfg, api, emails, name):
    """Fetch one device through REST, in the shape the DOQL path produces."""
    data = api.device(name)
    if not data:
        return None
    raw = [
        {
            "ip": (a.get("ip") or "").strip(),
            "label": (a.get("label") or "").strip(),
            "shared": _shared(str(a.get("type", ""))),
        }
        for a in (data.get("ip_addresses") or [])
    ]
    _enrich_ips(data, raw)
    data["custom_fields"] = data.get("custom_fields") or []
    if isinstance(data.get("customer"), dict):
        data["customer"] = data["customer"].get("name", "")
    # REST carries the customer's id but not its contact; a host-scoped sync
    # replaces the host's variables, so the address must come from somewhere.
    if "customer_email" in cfg.all_host_fields and data.get("customer_id"):
        data["customer_email"] = emails.get(data["customer_id"])
    # DOQL computes device_sub_type; REST only has the two parts. Without this a
    # host fetched by name would leave its device_sub_type group on every
    # host-scoped sync.
    if not data.get("device_sub_type"):
        sub = (
            data.get("virtual_subtype") or data.get("physical_subtype") or ""
        ).strip()
        if sub:
            data["device_sub_type"] = sub
    return data


def fetch_devices_by_name(cfg, api, names):
    """Fetch the named devices one lookup each, applying the --list filters too."""
    emails = CustomerEmails(api)
    devices = []
    with ThreadPoolExecutor(max_workers=min(len(names), cfg.ssh_workers) or 1) as pool:
        futures = {pool.submit(fetch_device, cfg, api, emails, n): n for n in names}
        for future in as_completed(futures):
            name = futures[future]
            try:
                device = future.result()
            except FAILURES as e:
                log.warning("lookup failed for %s: %s", name, e)
                continue
            if not device:
                log.warning("no such device: %s", name)
            elif not matches_filters(cfg, device):
                log.warning("excluded by the filters: %s", name)
            elif not (device.get("_primary_ip") or cfg.include_no_ip):
                log.warning(
                    "no usable address and include_hosts_without_ip is off: %s", name
                )
            else:
                devices.append(device)
    return devices


# ---------------------------------------------------------------------------
# DNS and SSH
# ---------------------------------------------------------------------------


def dns_ipv4(hostname):
    """Resolve a name to its IPv4 addresses, deduplicated; empty when it does not resolve."""
    try:
        infos = socket.getaddrinfo(
            hostname, None, family=socket.AF_INET, type=socket.SOCK_STREAM
        )
    except OSError:
        return []
    return list(dict.fromkeys(info[4][0] for info in infos))


def ssh_reachable(ip, port, timeout):
    """Tell whether a TCP connection to the SSH port opens in time."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ip_priority(ip, ipmap):
    """Sort key: ens* first, then eth*, then other labels, shared last."""
    meta = ipmap.get(ip, {})
    label = meta.get("label", "")
    if meta.get("shared") is True:
        return 3
    if label.startswith("ens"):
        return 0
    if label.startswith("eth"):
        return 1
    return 2


def probe_addresses(cfg, inventory):
    """Pick ansible_host for hosts with several addresses: DNS first, then an SSH probe."""
    if not cfg.ssh_probe:
        return
    hostvars = inventory.get("_meta", {}).get("hostvars", {})
    targets = [
        (h, hv["host_all_ips"])
        for h, hv in hostvars.items()
        if isinstance(hv.get("host_all_ips"), dict)
        and len(hv["host_all_ips"]) > 1
        and not str(hv.get("config_management_ip", "")).strip()
    ]
    if not targets:
        return
    if cfg.dns_fallback:
        unknown = [
            h
            for h, ipmap in targets
            if all(m.get("shared") is None for m in ipmap.values())
        ]
        if unknown:
            with ThreadPoolExecutor(max_workers=cfg.dns_workers) as pool:
                futures = {pool.submit(dns_ipv4, h): h for h in unknown}
                for future in as_completed(futures):
                    ips = future.result()
                    hv = hostvars.get(futures[future])
                    if ips and hv:
                        hv["nslookup_discovered_ip"] = ips[0]
                        hv["ansible_host"] = ips[0]
    probe = [
        (h, ipmap)
        for h, ipmap in targets
        if not (
            cfg.prefer_dns
            and cfg.dns_skips_ssh
            and hostvars.get(h, {}).get("nslookup_discovered_ip")
        )
    ]
    if not probe:
        return
    results = defaultdict(dict)
    with ThreadPoolExecutor(max_workers=cfg.ssh_workers) as pool:
        futures = {
            pool.submit(ssh_reachable, ip, cfg.ssh_port, cfg.ssh_timeout): (h, ip)
            for h, ipmap in probe
            for ip in ipmap
        }
        for future in as_completed(futures):
            h, ip = futures[future]
            results[h][ip] = future.result()
    for h, ipmap in probe:
        outcome = results.get(h, {})
        reachable = [ip for ip, ok in outcome.items() if ok]
        hv = hostvars.get(h)
        if not hv:
            continue
        hv["host_all_ips_connectivity"] = {
            "reachable": reachable,
            "unreachable": [ip for ip, ok in outcome.items() if not ok],
        }
        if reachable and not (cfg.prefer_dns and hv.get("nslookup_discovered_ip")):
            hv["ansible_host"] = min(reachable, key=lambda ip: _ip_priority(ip, ipmap))


def resolve_hosts_without_ip(cfg, inventory):
    """Look hosts Device42 has no address for up in DNS."""
    if not cfg.dns_fallback:
        return
    hostvars = inventory.get("_meta", {}).get("hostvars", {})
    names = [
        h
        for h, hv in hostvars.items()
        if not hv.get("host_all_ips")
        and not hv.get("config_management_ip")
        and hv.get("ansible_host") == h
    ]
    if not names:
        return
    by_hostname = cfg.inventory.get("hostname_as_fallback", False)
    with ThreadPoolExecutor(max_workers=cfg.dns_workers) as pool:
        futures = {pool.submit(dns_ipv4, h): h for h in names}
        for future in as_completed(futures):
            ips, hv = future.result(), hostvars[futures[future]]
            if ips:
                hv["ansible_host"] = ips[0]
                hv["nslookup_discovered_ip"] = ips[0]
                hv["ansible_host_source"] = "dns"
            else:
                hv["ansible_host_source"] = "hostname" if by_hostname else "unresolved"


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def group_name(value):
    """Turn any value into an Ansible group name: lowercase, underscores.

    CamelCase is split only for single words; values with spaces are already split.
    """
    value = str(value)
    if " " not in value:
        value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", value)
    value = re.sub(r"[^a-z0-9_]+", "_", value.casefold())
    return re.sub(r"_+", "_", value).strip("_")


def _present(value):
    return value is not None and value != ""


def host_vars(cfg, device):
    """Build one host's variables."""
    hv = {
        "ansible_host": device["_primary_ip"] or device["name"].lower(),
        "sourcedfromdevice42": True,
        **{
            f: device[f]
            for f in cfg.host_fields
            if f in device and f not in INTERNAL_FIELDS and _present(device[f])
        },
        **{
            group_name(cf["key"]): cf["value"]
            for cf in device.get("custom_fields", [])
            if cf.get("key") in cfg.host_custom_fields and cf.get("value")
        },
    }
    if len(device.get("_all_ips") or {}) > 1:
        hv["host_all_ips"] = device["_all_ips"]
    management = str(hv.get("config_management_ip", "")).strip()
    if management:
        hv["ansible_host"] = management
        hv["ansible_host_source"] = "config_management_ip"
    return hv


def _group_vars(cfg, device):
    return {
        **{
            f: device[f]
            for f in cfg.group_fields
            if f in device and _present(device[f])
        },
        **{
            group_name(cf["key"]): cf["value"]
            for cf in device.get("custom_fields", [])
            if cf.get("key") in cfg.group_custom_fields and cf.get("value")
        },
    }


def _datacenters(cfg, device):
    """Return the datacenters whose networks hold any of the device's addresses."""
    addresses = []
    for ip in device.get("_all_ips") or {}:
        try:
            addresses.append(ipaddress.ip_address(ip))
        except ValueError:
            continue
    found = {
        dc
        for dc, networks in cfg.datacenter_networks.items()
        if any(a in n for a in addresses for n in networks)
    }
    name = device["name"].lower()
    suffixes = [s for s in cfg.datacenter_domains if name.endswith(s)]
    if suffixes:
        found.add(cfg.datacenter_domains[max(suffixes, key=len)])
    return sorted(found)


def build_inventory(cfg, devices):
    """Build the --list inventory."""
    devices.sort(key=lambda d: d["name"].lower())
    inventory = {"_meta": {"hostvars": {}}, "all": {"hosts": [], "children": []}}
    groups = {}

    def join(gname, name, gvars):
        groups.setdefault(gname, {"hosts": [], "vars": gvars})["hosts"].append(name)

    for device in devices:
        name = device["name"].lower()
        inventory["_meta"]["hostvars"][name] = host_vars(cfg, device)
        inventory["all"]["hosts"].append(name)
        for key, value in _group_vars(cfg, device).items():
            join(f"{group_name(key)}_{group_name(value)}", name, {key: value})
        for dc in _datacenters(cfg, device):
            join(f"datacenter_{group_name(dc)}", name, {"d42_datacenter": dc})
    source = str(cfg.inventory.get("inventory_source", "")).strip()
    if source:
        gname = f"d42_inventory_source_{group_name(source)}"
        groups[gname] = {
            "hosts": list(inventory["all"]["hosts"]),
            "vars": {"d42_inventory_source_name": gname},
        }
    for gname in sorted(groups):
        data = groups[gname]
        inventory[gname] = {"hosts": sorted(data["hosts"]), "vars": data["vars"]}
        inventory["all"]["children"].append(gname)
    resolve_hosts_without_ip(cfg, inventory)
    probe_addresses(cfg, inventory)
    return inventory


def build_host(cfg, device):
    """Build one host's variables for --host, probing its addresses if it has several."""
    if not device:
        return {}
    hv = host_vars(cfg, device)
    if isinstance(hv.get("host_all_ips"), dict) and len(hv["host_all_ips"]) > 1:
        probe_addresses(cfg, {"_meta": {"hostvars": {device["name"].lower(): hv}}})
    return hv


def main(argv=None, environ=None, client=Device42):
    """Print the inventory, one host's variables, or the named hosts, as JSON."""
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="the whole inventory")
    mode.add_argument("--host", help="one host's variables, no groups")
    mode.add_argument(
        "--host-with-groups",
        metavar="HOSTS",
        help="the --list shape for the named hosts only (comma-separated)",
    )
    args = parser.parse_args(argv)
    try:
        source = settings(environ)
        logging.basicConfig(
            stream=sys.stderr,
            level=str(source.get("log_level") or "WARNING").upper(),
            format="%(levelname)s %(name)s: %(message)s",
        )
        cfg = Config(source, environ)
        api = client(cfg)
        if args.host_with_groups:
            names = [n.strip() for n in args.host_with_groups.split(",") if n.strip()]
            if not names:
                raise ConnectorError("--host-with-groups was given no host name")
            devices = fetch_devices_by_name(cfg, api, names)
            if not devices:
                raise ConnectorError(
                    f"none of {names} is a device this inventory serves"
                )
            output = build_inventory(cfg, devices)
        elif args.host:
            output = build_host(
                cfg, fetch_device(cfg, api, CustomerEmails(api), args.host)
            )
        else:
            output = build_inventory(cfg, fetch_all_devices(cfg, api))
    except (ConnectorError, *FAILURES) as e:
        print(e, file=sys.stderr)
        return 1
    print(json.dumps(output, indent=2))
    return 0 if output else 1


if __name__ == "__main__":
    sys.exit(main())
