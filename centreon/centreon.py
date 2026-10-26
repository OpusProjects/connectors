#!/usr/bin/env python3
"""Ansible dynamic inventory for Centreon, through its REST API v2.

Publishes the hosts of one estate as Centreon sees them, so a form or a play can
answer which service groups exist, which hosts are in one, and which services a
host has, without anybody typing a name from memory.

Centreon often stores a host under a short name (web01.prod.east) rather than
its FQDN, and its fqdn field is the monitoring address. So each configured
domain suffix is cut into its dot-delimited heads, longest first; a host
matches on the longest head its name ends in, and what the head leaves over is
appended back to give the FQDN:

  Centreon  web01.prod.east
  head      .prod.east
  emitted   web01.prod.east.example.com

Hostvars, under one key:

  web01.prod.east.example.com:
    centreon:
      host_id, name (as Centreon knows it), alias, address, datacenter,
      monitoring_server, status, is_in_downtime, is_acknowledged,
      hostgroups, servicegroups,
      services:           what the pollers run (with include_services)
      disabled_services:  configured but deactivated (with include_services)

Groups: centreon (every host), centreon_dc_<dc> (with datacenter_pattern),
centreon_hostgroup_<name>, centreon_servicegroup_<name> (hosts owning a service
in it) and centreon_disabled_services.

Every request is narrowed server side as far as the API allows, because a
platform can be far larger than the estate. What each endpoint accepts had to
be found by probing:

  /monitoring/resources      search on h.name with $lk
  /monitoring/hostgroups     search on host.name with $lk (h.name is refused)
  /monitoring/servicegroups  neither (host.name crashes it), so probed whole
  /configuration/services    search on host.name; not on is_activated

No endpoint selects fields, so a service row costs about 2 KB whatever it
holds. That is why include_services is off by default: the service group probes
already return the services that are in a group. With it on, each host also
gets every service the pollers run, and the deactivated ones, which only the
configuration API still has.

--host and --host-with-groups run the same build narrowed to the named hosts:
their candidate Centreon names replace the domain patterns in every request.

Credentials: CREDENTIAL_BASE_URL, CREDENTIAL_USERNAME and CREDENTIAL_PASSWORD
as unified-api hands them over, else CENTREON_BASE_URL, CENTREON_USERNAME and
CENTREON_PASSWORD.

Settings, from the source's config (SOURCE_CONFIG, JSON; strings accepted):
  domains             comma-separated FQDN suffixes of the estate (required)
  datacenter_pattern  a regular expression whose first group, found in a
                      domain suffix, names its datacenter; none by default
  include_services    attach each host's services, default false
  workers             parallel requests, default 8
  api_path            default /centreon/api/latest
  insecure            true to skip TLS verification
"""

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# --- begin shared helpers: copied from unified_connectors/inventory.py, keep identical ---


class ConnectorError(Exception):
    """A failure reported as one clean line on stderr, without a traceback."""


def settings(environ=None):
    """Read the settings unified-api hands a connector.

    Args:
        environ: The environment to read; os.environ by default.

    Returns:
        The SOURCE_CONFIG (or APPLICATION_CONFIG) JSON object, or an empty dict.

    Raises:
        ConnectorError: The variable is set but is not a JSON object.
    """
    environ = os.environ if environ is None else environ
    raw = environ.get("SOURCE_CONFIG") or environ.get("APPLICATION_CONFIG") or "{}"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ConnectorError(f"SOURCE_CONFIG is not JSON: {e}") from None
    if not isinstance(value, dict):
        raise ConnectorError("SOURCE_CONFIG must be a JSON object")
    return value


def as_list(value, default=()):
    """Read a list setting: a JSON list, or a comma-separated string.

    unified-api passes every config value as a string, so "a, b" and ["a", "b"]
    both give ["a", "b"].
    """
    if value is None or value == "":
        return list(default)
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item) for item in value]


def as_bool(value, default=False):
    """Read a flag setting: true, 1 and yes (any case) are true, anything else false."""
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


def as_int(value, default):
    """Read a whole-number setting.

    Raises:
        ConnectorError: The value is not a whole number.
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConnectorError(f"not a whole number: {value!r}") from None


def credential_sets(prefix, required, optional=(), environ=None):
    """Find every complete set of credentials for one kind of system.

    A set is PREFIX_<TAG>_<FIELD> variables sharing a TAG, which is any name.
    unified-api hands them over as CREDENTIAL_PREFIX_<TAG>_<FIELD>; an Ansible
    controller sets PREFIX_<TAG>_<FIELD>. The unified-api form wins, so a system
    exposed both ways is used once.

    Args:
        prefix: The system's prefix, such as UCS.
        required: Fields a set must have, the first of which marks a set.
        optional: Fields read when present.
        environ: The environment to read; os.environ by default.

    Returns:
        One dict per set, sorted by tag: "tag" plus each field in lowercase.
    """
    environ = os.environ if environ is None else environ
    marker = f"_{required[0]}"
    found, seen = [], set()
    for start in (f"CREDENTIAL_{prefix}_", f"{prefix}_"):
        for key in sorted(environ):
            if not (key.startswith(start) and key.endswith(marker)):
                continue
            tag = key[len(start) : -len(marker)]
            if not tag or tag in seen:
                continue
            values = {
                f.lower(): environ.get(f"{start}{tag}_{f}", "").strip()
                for f in (*required, *optional)
            }
            if all(values[f.lower()] for f in required):
                seen.add(tag)
                found.append({"tag": tag, **values})
    return found


# --- end shared helpers ---

PAGE_SIZE = 1000
# A configuration page costs about the same at 5000 rows as at 1000.
CONFIG_PAGE_SIZE = 5000
TIMEOUT = 180
GROUP_PREFIX = "centreon"

# Network errors and timeouts are OSError (URLError and HTTPError among them).
FAILURES = (OSError, ValueError)


def log(message):
    """Write progress to stderr; stdout carries the inventory and nothing else."""
    sys.stderr.write(f"[INFO] {message}\n")


def group_name(name):
    """Turn a Centreon group name into an Ansible group name."""
    return re.sub(r"[^a-z0-9_]+", "_", str(name).casefold()).strip("_")


def heads_of(domain):
    """Return every dot-delimited head of a suffix, longest first.

    .prod.east.example.com gives .prod.east.example.com, .prod.east.example,
    .prod.east and .prod. Centreon stores whichever the host was registered
    under, so the longest that matches is trusted, and what it leaves over is
    appended to rebuild the FQDN.
    """
    parts = domain.lstrip(".").split(".")
    return ["." + ".".join(parts[:n]) for n in range(len(parts), 0, -1)]


def build_matchers(domains, datacenter_pattern=None):
    """Build the matchers and the server-side patterns.

    Which head Centreon stores cannot be known in advance, so every head is
    offered to the server as a LIKE and the longest match decides here.

    Args:
        domains: The estate's FQDN suffixes.
        datacenter_pattern: A compiled expression whose first group names a
            suffix's datacenter, or None.

    Returns:
        (head, tail, datacenter) triples, longest head first, and the patterns.
    """
    matchers = []
    for domain in domains:
        found = datacenter_pattern.search(domain) if datacenter_pattern else None
        dc = found.group(1) if found else ""
        for head in heads_of(domain):
            matchers.append((head, domain[len(head) :], dc))
    matchers.sort(key=lambda triple: len(triple[0]), reverse=True)
    patterns = sorted({head for head, _, _ in matchers}, key=len, reverse=True)
    return matchers, patterns


def match(name, matchers):
    """Return (fqdn, datacenter) for a Centreon host name, or (None, "") if it is not ours."""
    for head, tail, dc in matchers:
        if name.endswith(head):
            return name + tail, dc
    return None, ""


def search(patterns, field="h.name"):
    """Build the search: field LIKE '%pattern' for each pattern, OR'd.

    The field differs by endpoint: /monitoring/resources takes h.name and the
    host group catalogue takes host.name; each refuses the other's.
    """
    clauses = [{field: {"$lk": f"%{pattern}"}} for pattern in patterns]
    return clauses[0] if len(clauses) == 1 else {"$or": clauses}


class CentreonClient:
    """A logged-in session on the Centreon API."""

    def __init__(
        self, base_url, username, password, api_path, insecure=False, workers=8
    ):
        """Log in.

        Raises:
            ConnectorError: The login answered no token.
            OSError: Centreon could not be reached or refused the login.
        """
        self.api = base_url.rstrip("/") + api_path
        self.workers = workers
        self.context = ssl.create_default_context()
        if insecure:
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE
        self.headers = {"Accept": "application/json"}
        body = {"security": {"credentials": {"login": username, "password": password}}}
        answer = self._send("/login", data=json.dumps(body).encode())
        token = (answer.get("security") or {}).get("token")
        if not token:
            raise ConnectorError("login returned no token")
        self.headers["X-AUTH-TOKEN"] = token

    def _send(self, path, params=None, data=None):
        url = self.api + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(
            request, timeout=TIMEOUT, context=self.context
        ) as response:
            return json.loads(response.read() or b"{}")

    def get_page(self, path, params):
        """Return one page of a collection."""
        return self._send(path, params)

    def get_all(self, path, params=None, label="", page_size=PAGE_SIZE):
        """Return every row of a collection: page 1 gives the total, the rest run in a pool."""
        params = {**(params or {}), "limit": page_size, "page": 1}
        first = self.get_page(path, params)
        rows = list(first.get("result") or [])
        total = (first.get("meta") or {}).get("total", len(rows))
        pages = -(-total // page_size)
        if pages > 1:
            if label:
                log(f"{label}: {total} rows over {pages} pages")

            def fetch(page):
                return self.get_page(path, {**params, "page": page}).get("result") or []

            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for chunk in pool.map(fetch, range(2, pages + 1)):
                    rows.extend(chunk)
        return rows


def fetch_hosts(client, matchers, patterns):
    """Return the estate's hosts, keyed by their Centreon name."""
    rows = client.get_all(
        "/monitoring/resources",
        {"types": json.dumps(["host"]), "search": json.dumps(search(patterns))},
        label="hosts",
    )
    hosts = {}
    for row in rows:
        name = row.get("name") or ""
        fqdn, datacenter = match(name, matchers)
        if not fqdn:
            continue
        hosts[name] = {
            "fqdn": fqdn,
            "host_id": row.get("id"),
            "name": name,
            "alias": row.get("alias") or "",
            "address": row.get("fqdn") or "",
            "datacenter": datacenter,
            "monitoring_server": row.get("monitoring_server_name") or "",
            "status": (row.get("status") or {}).get("name") or "",
            "is_in_downtime": bool(row.get("is_in_downtime")),
            "is_acknowledged": bool(row.get("is_acknowledged")),
            "hostgroups": [],
            "servicegroups": [],
            "services": [],
            "disabled_services": [],
        }
    return hosts


def fetch_services(client, hosts, patterns):
    """Attach the services the pollers run to each host; return how many."""
    rows = client.get_all(
        "/monitoring/resources",
        {"types": json.dumps(["service"]), "search": json.dumps(search(patterns))},
        label="services",
    )
    kept = 0
    for row in rows:
        host = hosts.get((row.get("parent") or {}).get("name") or "")
        if host is None:
            continue
        host["services"].append(
            {
                "service_id": row.get("id"),
                "name": row.get("name") or "",
                "status": (row.get("status") or {}).get("name") or "",
                "is_in_downtime": bool(row.get("is_in_downtime")),
                "servicegroups": [],
            }
        )
        kept += 1
    for host in hosts.values():
        host["services"].sort(key=lambda s: s["name"])
    return kept


def fetch_disabled_services(client, hosts, patterns):
    """Attach the configured but deactivated services; return how many.

    The monitoring view only holds what the pollers run, so a deactivated
    service is only in the configuration API, which cannot filter on
    is_activated: every configured service of the estate is read and the
    inactive ones kept.
    """
    rows = client.get_all(
        "/configuration/services",
        {"search": json.dumps(search(patterns, "host.name"))},
        label="configured services",
        page_size=CONFIG_PAGE_SIZE,
    )
    kept = 0
    for row in rows:
        if row.get("is_activated", True):
            continue
        for parent in row.get("hosts") or []:
            host = hosts.get(parent.get("name") or "")
            if host is None:
                continue
            host["disabled_services"].append(
                {"service_id": row.get("id"), "name": row.get("name") or ""}
            )
            kept += 1
    for host in hosts.values():
        host["disabled_services"].sort(key=lambda s: s["name"])
    return kept


def fetch_group_members(
    client, catalogue_path, param, kind, patterns, resource_type, catalogue_filter=None
):
    """Return which of each group's members belong to the estate.

    catalogue_filter narrows the catalogue to groups already holding one of our
    hosts; the service group catalogue cannot take one, so it is probed whole.
    The catalogue only gives names, so membership costs one request per group,
    run in a pool. Groups with no member here are dropped.
    """
    params = {"search": json.dumps(catalogue_filter)} if catalogue_filter else None
    catalogue = client.get_all(catalogue_path, params, label=kind)
    log(f"{kind}: {len(catalogue)} to probe for members")

    def members(group):
        name = group.get("name") or ""
        try:
            rows = client.get_all(
                "/monitoring/resources",
                {
                    "types": json.dumps([resource_type]),
                    param: json.dumps([name]),
                    "search": json.dumps(search(patterns)),
                },
            )
        except urllib.error.HTTPError as e:
            sys.stderr.write(f"[WARN] {kind} {name!r}: {e}\n")
            return name, []
        return name, rows

    found = {}
    with ThreadPoolExecutor(max_workers=client.workers) as pool:
        for name, rows in pool.map(members, catalogue):
            if rows:
                found[name] = rows
    log(f"{kind}: {len(found)} hold members")
    return found


def centreon_names(target, matchers):
    """Return every name Centreon may hold for an inventory host name, longest first.

    Empty when the target is in none of the configured domains.
    """
    names = []
    for head, tail, _ in matchers:
        short = target[: -len(tail)] if tail and target.endswith(tail) else target
        if short.endswith(head) and short not in names:
            names.append(short)
    return names


def build_inventory(
    client, domains, datacenter_pattern=None, include_services=False, only=None
):
    """Build the inventory, or with only (host names) the inventory of those hosts.

    Raises:
        ConnectorError: No host matched the domains in a full build; an empty
            inventory would empty every source that replaces its hosts.
    """
    matchers, patterns = build_matchers(domains, datacenter_pattern)
    if only is not None:
        patterns = []
        for target in only:
            names = centreon_names(target, matchers)
            if not names:
                log(f"{target} is not in the configured domains")
            patterns.extend(n for n in names if n not in patterns)
        if not patterns:
            return {"_meta": {"hostvars": {}}}
    log(
        f"{len(domains)} domain(s), matching Centreon names ending in "
        + ", ".join(patterns)
    )

    hosts = fetch_hosts(client, matchers, patterns)
    if only is not None:
        # The patterns are LIKE '%name', so a longer name ending in one matches
        # too; keep only hosts that are exactly a candidate.
        hosts = {k: v for k, v in hosts.items() if k in patterns}
        if not hosts:
            log(f"{', '.join(only)}: not in Centreon")
            return {"_meta": {"hostvars": {}}}
    log(f"{len(hosts)} host(s) in the estate")
    if not hosts:
        raise ConnectorError("no host matched the configured domains")

    # Each fetch writes its own keys of the host dicts, so they run side by side.
    with ThreadPoolExecutor(max_workers=4) as pool:
        if include_services:
            services = pool.submit(fetch_services, client, hosts, patterns)
            disabled = pool.submit(fetch_disabled_services, client, hosts, patterns)
        hostgroups = pool.submit(
            fetch_group_members,
            client,
            "/monitoring/hostgroups",
            "hostgroup_names",
            "hostgroups",
            patterns,
            "host",
            catalogue_filter=search(patterns, "host.name"),
        )
        servicegroups = pool.submit(
            fetch_group_members,
            client,
            "/monitoring/servicegroups",
            "servicegroup_names",
            "servicegroups",
            patterns,
            "service",
        )
        if include_services:
            log(f"{services.result()} service(s) attached")
            log(f"{disabled.result()} disabled service(s) attached")
        hostgroups = hostgroups.result()
        servicegroups = servicegroups.result()

    inventory = {"_meta": {"hostvars": {}}, GROUP_PREFIX: {"hosts": []}}

    def group(name):
        return inventory.setdefault(name, {"hosts": []})

    for name, rows in hostgroups.items():
        key = f"{GROUP_PREFIX}_hostgroup_{group_name(name)}"
        for row in rows:
            host = hosts.get(row.get("name") or "")
            if host is not None:
                host["hostgroups"].append(name)
                group(key)["hosts"].append(host["fqdn"])

    services_by_id = {s["service_id"]: s for h in hosts.values() for s in h["services"]}
    for name, rows in servicegroups.items():
        key = f"{GROUP_PREFIX}_servicegroup_{group_name(name)}"
        for row in rows:
            host = hosts.get((row.get("parent") or {}).get("name") or "")
            if host is None:
                continue
            if name not in host["servicegroups"]:
                host["servicegroups"].append(name)
            service = services_by_id.get(row.get("id"))
            if service is not None and name not in service["servicegroups"]:
                service["servicegroups"].append(name)
            group(key)["hosts"].append(host["fqdn"])

    for host in hosts.values():
        fqdn = host.pop("fqdn")
        host["hostgroups"].sort()
        host["servicegroups"].sort()
        inventory["_meta"]["hostvars"][fqdn] = {"centreon": host}
        inventory[GROUP_PREFIX]["hosts"].append(fqdn)
        if host["disabled_services"]:
            group(f"{GROUP_PREFIX}_disabled_services")["hosts"].append(fqdn)
        if host["datacenter"]:
            group(f"{GROUP_PREFIX}_dc_{group_name(host['datacenter'])}")[
                "hosts"
            ].append(fqdn)

    for key, value in inventory.items():
        if key != "_meta":
            value["hosts"] = sorted(set(value["hosts"]))
    inventory["all"] = {
        "children": sorted(k for k in inventory if k not in ("_meta", "all"))
    }
    log(
        f"emitting {len(inventory['_meta']['hostvars'])} host(s) in "
        f"{len(inventory['all']['children'])} group(s)"
    )
    return inventory


def credentials(environ):
    """Return (base_url, username, password) from unified-api's or a controller's variables.

    Raises:
        ConnectorError: Neither set is complete.
    """
    for prefix in ("CREDENTIAL", "CENTREON"):
        values = [
            environ.get(f"{prefix}_{k}", "").strip()
            for k in ("BASE_URL", "USERNAME", "PASSWORD")
        ]
        if all(values):
            return values
    raise ConnectorError(
        "no Centreon credentials: set CREDENTIAL_BASE_URL, CREDENTIAL_USERNAME and "
        "CREDENTIAL_PASSWORD (or CENTREON_*)"
    )


def main(argv=None, environ=None, connect=CentreonClient):
    """Print the inventory, one host's variables, or the named hosts, as JSON."""
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="the whole inventory")
    mode.add_argument("--host", metavar="HOSTNAME", help="one host's variables")
    mode.add_argument(
        "--host-with-groups",
        metavar="HOSTNAMES",
        help="the inventory of these hosts only (comma-separated), groups included",
    )
    args = parser.parse_args(argv)
    try:
        config = settings(environ)
        domains = as_list(config.get("domains"))
        if not domains:
            raise ConnectorError(
                "domains is not set; refusing an inventory that would match every host"
            )
        raw_pattern = config.get("datacenter_pattern") or ""
        try:
            pattern = re.compile(raw_pattern) if raw_pattern else None
        except re.error as e:
            raise ConnectorError(
                f"datacenter_pattern is not a regular expression: {e}"
            ) from None
        if pattern is not None and pattern.groups < 1:
            raise ConnectorError(
                "datacenter_pattern needs a group naming the datacenter"
            )
        base_url, username, password = credentials(environ)
        client = connect(
            base_url,
            username,
            password,
            config.get("api_path") or "/centreon/api/latest",
            insecure=as_bool(config.get("insecure")),
            workers=as_int(config.get("workers"), 8),
        )
        include = as_bool(config.get("include_services"))
        if args.list:
            output = build_inventory(client, domains, pattern, include)
        elif args.host_with_groups:
            wanted = [h.strip() for h in args.host_with_groups.split(",") if h.strip()]
            output = build_inventory(client, domains, pattern, include, only=wanted)
        else:
            hostvars = build_inventory(
                client, domains, pattern, include, only=[args.host]
            )
            output = next(iter(hostvars["_meta"]["hostvars"].values()), {})
    except ConnectorError as e:
        sys.stderr.write(f"[ERROR] {e}\n")
        return 1
    except ssl.SSLError as e:
        sys.stderr.write(
            f"[ERROR SSL] {e}\n  -> set insecure to true for a self-signed certificate.\n"
        )
        return 1
    except urllib.error.HTTPError as e:
        sys.stderr.write(
            f"[ERROR HTTP {e.code}] {e.read()[:300].decode(errors='replace')}\n"
        )
        return 1
    except FAILURES as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, ssl.SSLError):
            sys.stderr.write(
                f"[ERROR SSL] {reason}\n  -> set insecure to true for a self-signed certificate.\n"
            )
        else:
            sys.stderr.write(f"[ERROR] cannot reach Centreon: {reason}\n")
        return 1
    print(json.dumps(output, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
