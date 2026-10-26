#!/usr/bin/env python3
"""Ansible dynamic inventory for an Infinidat InfiniBox, through its REST API.

Each host the array maps volumes to becomes an inventory host carrying those
volumes. The key that ties a host's disk to its volume is the WWID: the array
serial prefixed with 36, the same value multipath reports on the host, so a
form or playbook can match them without relying on volume names.

  web01.prod.shop.east.example.com:
    infinibox:
      system:  array01.example.com
      host:    igrp-web01-prod-shop-east      (the host object on the array)
      volumes:
        - volume:   vol-web01-data
          serial:   742b0f000000000000000000000001
          wwid:     36742b0f000000000000000000000001
          size:     107374182400
          size_gb:  100
          pool:     pool-a
          provtype: THIN
          lun:      1

Hosts land in one group per array, iboxarray_<array name>, whose
infinibox_array variable holds the array's name unchanged. Only volumes
mapped to a host are listed: the array keeps many snapshots that say nothing
about the inventory.

Host names. By default a host object's name is used as it is. Arrays whose
host objects follow a naming convention can turn them into host names:
  host_prefix      a prefix to strip, such as igrp-; hosts without it are skipped
  dotted_segments  how many trailing dash-separated parts are domain labels:
                   with 3, web01-prod-shop-east becomes web01.prod.shop.east
  domain           a suffix appended to every host name

Other settings, from the source's config (unified-api passes strings):
  url              the array's address (required)
  insecure         true to skip TLS verification (self-signed arrays)
  timeout_seconds  per request, default 180

Credentials: CREDENTIAL_USERNAME and CREDENTIAL_PASSWORD from unified-api, or
INFINIBOX_USERNAME and INFINIBOX_PASSWORD from an Ansible controller, which
may also give the address as INFINIBOX_BASE_URL.
"""

import argparse
import base64
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlparse

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
GROUP_PREFIX = "iboxarray_"
WWID_PREFIX = "36"

# What an array that is down, slow, refusing or answering nonsense raises:
# network errors, HTTP errors and timeouts are OSError, bad JSON a ValueError.
FAILURES = (OSError, ValueError)


class InfiniBoxClient:
    """A minimal REST client for one array, reading every page of a list."""

    def __init__(self, url, username, password, insecure=False, timeout=180):
        """Prepare the client; nothing is sent until the first read.

        Args:
            url: The array's address, with or without https://.
            username: The API account.
            password: Its password.
            insecure: Skip TLS verification.
            timeout: Seconds per request.
        """
        self.url = url if "://" in url else f"https://{url}"
        self.url = self.url.rstrip("/")
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        self.timeout = timeout
        self.context = ssl.create_default_context()
        if insecure:
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE

    def _get(self, endpoint, params):
        query = urlencode(params)
        request = urllib.request.Request(
            f"{self.url}/api/rest/{endpoint}?{query}", headers=self.headers
        )
        with urllib.request.urlopen(
            request, timeout=self.timeout, context=self.context
        ) as response:
            return json.loads(response.read())

    def get_paged(self, endpoint, fields=""):
        """Return every result of a list endpoint, page by page."""
        out, page = [], 1
        while True:
            params = {"page_size": PAGE_SIZE, "page": page}
            if fields:
                params["fields"] = fields
            body = self._get(endpoint, params)
            out.extend(body.get("result") or [])
            if page >= ((body.get("metadata") or {}).get("pages_total") or 1):
                return out
            page += 1

    def get_pools(self):
        """Return the pools, id and name."""
        return self.get_paged("pools", "id,name")

    def get_hosts(self):
        """Return the host objects with their ports and mapped LUNs."""
        return self.get_paged("hosts", "id,name,luns,ports")

    def get_volumes(self):
        """Return the volumes with serial, size, pool and provisioning type."""
        return self.get_paged("volumes", "id,name,serial,size,pool_id,provtype,type")


def safe_group_name(name):
    """Turn a name into a valid Ansible group name.

    Group names take letters, digits and underscores only; an array named by
    its FQDN would otherwise warn in every job that loads the inventory. Runs
    of other characters become one underscore, none left at either end. Only
    the group name changes: the array's real name stays in infinibox_array and
    in each host's infinibox.system.
    """
    return re.sub(r"[^a-z0-9_]+", "_", str(name).casefold()).strip("_")


def host_name(name, host_prefix="", dotted_segments=0, domain=""):
    """Turn the name of a host object on the array into an inventory host name.

    Args:
        name: The host object's name, such as igrp-web01-prod-shop-east.
        host_prefix: A prefix to strip; a name without it is skipped.
        dotted_segments: How many trailing dash-separated parts are domain
            labels; a name with no part left before them is skipped.
        domain: A suffix appended to the result.

    Returns:
        The host name, or an empty string to skip the host rather than invent
        a name that does not resolve.
    """
    if host_prefix:
        if not name.startswith(host_prefix):
            return ""
        name = name[len(host_prefix) :]
    if dotted_segments:
        parts = name.split("-")
        if len(parts) < dotted_segments + 1:
            return ""
        # The short name may itself contain dashes: everything before the
        # trailing labels is the short name.
        short = "-".join(parts[:-dotted_segments])
        if not short:
            return ""
        name = ".".join([short, *parts[-dotted_segments:]])
    if not name:
        return ""
    return f"{name}.{domain}" if domain else name


def build_inventory(client, array_name, naming=None):
    """Build the inventory of one array.

    Args:
        client: An InfiniBoxClient, or anything answering get_pools, get_hosts
            and get_volumes.
        array_name: The array's name, for the group and infinibox.system.
        naming: host_name's keyword arguments: host_prefix, dotted_segments
            and domain.

    Returns:
        The Ansible inventory as a dict.
    """
    naming = naming or {}
    group = f"{GROUP_PREFIX}{safe_group_name(array_name)}"
    inventory = {
        "_meta": {"hostvars": {}},
        "all": {"children": ["ungrouped", group]},
        group: {"hosts": [], "vars": {"infinibox_array": array_name}},
    }

    print("[INFO] Reading pools, hosts and volumes...", file=sys.stderr)
    pools = {p["id"]: p.get("name", "") for p in client.get_pools()}
    hosts = client.get_hosts()
    volumes = {v["id"]: v for v in client.get_volumes()}
    print(
        f"[INFO] {len(pools)} pools, {len(hosts)} hosts, {len(volumes)} volumes on the array.",
        file=sys.stderr,
    )

    mapped = skipped = 0
    for host in hosts:
        luns = host.get("luns") or []
        if not luns:
            continue
        name = host_name(host.get("name", ""), **naming)
        if not name:
            skipped += 1
            continue
        entries = []
        for lun in luns:
            volume = volumes.get(lun.get("volume_id"))
            if not volume:
                continue
            serial = volume.get("serial") or ""
            size = volume.get("size") or 0
            entries.append(
                {
                    "volume": volume.get("name", ""),
                    "serial": serial,
                    "wwid": f"{WWID_PREFIX}{serial}" if serial else "",
                    "size": size,
                    "size_gb": int(size / 1024**3) if size else 0,
                    "pool": pools.get(volume.get("pool_id"), ""),
                    "provtype": volume.get("provtype", ""),
                    "lun": lun.get("lun"),
                }
            )
        if not entries:
            continue
        entries.sort(key=lambda e: (e["lun"] is None, e["lun"]))
        inventory["_meta"]["hostvars"][name] = {
            "infinibox": {
                "system": array_name,
                "host": host.get("name", ""),
                "volumes": entries,
            }
        }
        inventory[group]["hosts"].append(name)
        mapped += 1

    inventory[group]["hosts"].sort()
    tail = f", {skipped} skipped for an unconventional name." if skipped else "."
    print(f"[INFO] {mapped} hosts with mapped volumes{tail}", file=sys.stderr)
    return inventory


def _first(environ, *keys):
    for key in keys:
        value = environ.get(key, "").strip()
        if value:
            return value
    return ""


def main(argv=None, environ=None, connect=InfiniBoxClient):
    """Print the inventory, or one host's variables, as JSON."""
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="the whole inventory")
    mode.add_argument("--host", metavar="HOSTNAME", help="one host's variables")
    args = parser.parse_args(argv)

    try:
        config = settings(environ)
        url = (config.get("url") or _first(environ, "INFINIBOX_BASE_URL")).rstrip("/")
        username = _first(environ, "CREDENTIAL_USERNAME", "INFINIBOX_USERNAME")
        password = _first(environ, "CREDENTIAL_PASSWORD", "INFINIBOX_PASSWORD")
        problems = [
            message
            for value, message in (
                (url, "no array address: set url in the source's config"),
                (username, "no user: set CREDENTIAL_USERNAME or INFINIBOX_USERNAME"),
                (
                    password,
                    "no password: set CREDENTIAL_PASSWORD or INFINIBOX_PASSWORD",
                ),
            )
            if not value
        ]
        if problems:
            raise ConnectorError("; ".join(problems))
        naming = {
            "host_prefix": config.get("host_prefix", ""),
            "dotted_segments": as_int(config.get("dotted_segments"), 0),
            "domain": config.get("domain", ""),
        }
        client = connect(
            url,
            username,
            password,
            insecure=as_bool(config.get("insecure")),
            timeout=as_int(config.get("timeout_seconds"), 180),
        )
        array_name = urlparse(url if "://" in url else f"https://{url}").hostname or url
        inventory = build_inventory(client, array_name, naming)
    except ConnectorError as e:
        print(e, file=sys.stderr)
        return 1
    except urllib.error.HTTPError as e:
        print(f"{url}: HTTP {e.code} {e.reason}", file=sys.stderr)
        return 1
    except FAILURES as e:
        hint = (
            " (set insecure to true for a self-signed array)"
            if isinstance(getattr(e, "reason", e), ssl.SSLError)
            else ""
        )
        print(f"{url}: {e}{hint}", file=sys.stderr)
        return 1
    if args.list:
        print(json.dumps(inventory, indent=2, default=str))
    else:
        print(
            json.dumps(
                inventory["_meta"]["hostvars"].get(args.host, {}), indent=2, default=str
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
