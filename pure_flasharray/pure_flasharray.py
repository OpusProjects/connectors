#!/usr/bin/env python3
"""Ansible dynamic inventory for a Pure Storage FlashArray, through REST API 1.19.

Lists the hosts defined on the array and the volumes (LUNs) mapped to them, so a
play can find a host's block devices by serial or multipath WWID.

  --list    every host, in purearray_<array> > purehostgroup_<host group>
  --host    one host's variables

Each host group's group carries pure_hgroup, pure_group, pure_array,
pure_luns_shared and pure_lun_count. Each host carries pure_array, pure_host,
pure_host_short, pure_hgroups, pure_groups, pure_luns_individual,
pure_lun_count, pure_iqns, pure_wwns, pure_nqns and pure_host_personality.

A set of LUNs is a dict of:
  volumes_all    every volume in one flat list, with its volume_group (or null)
  volumes        the volumes outside any volume group
  volume_groups  volume group name -> {"volumes": [...]}, each volume named
                 without the group prefix and with full_volume for the full name
and every volume has lun_id, serial, multipath_wwid, size_bytes and size_human.

The array comes from the environment: CREDENTIAL_PURE_<TAG>_BASE_URL, _USERNAME
and _PASSWORD as unified-api hands them over, or PURE_<TAG>_... as an Ansible
controller would. One array per source: run one source per array.

Settings, from the source's config (SOURCE_CONFIG, JSON; strings are fine):
  resolve_fqdn     true to name hosts by the fully qualified name DNS gives
  domain           with resolve_fqdn, the suffix for names DNS does not know
  insecure         true to skip TLS verification (self-signed arrays)
  workers          parallel requests, default 20
  timeout_seconds  per request, default 60
"""

import argparse
import http.cookiejar
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlparse

# The SDK sits at the repository root, one folder up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unified_connector import (
    ConnectorError,
    as_bool,
    as_int,
    credential_sets,
    settings,
)

API_VERSION = "1.19"
HGROUP_PREFIX = "purehostgroup_"
ARRAY_PREFIX = "purearray_"
DNS_WORKERS = 50

# Pure volume serials become multipath WWIDs behind this NAA prefix.
WWID_PREFIX = "3624a9370"

# What an array that is down, slow, refusing or answering nonsense raises:
# network errors and timeouts are OSError, a garbled answer a ValueError (JSON),
# a refused request a RuntimeError.
FAILURES = (OSError, ValueError, RuntimeError)


class NotFound(RuntimeError):
    """The array answered 404: the object has nothing mapped, or does not exist."""


class FlashArrayClient:
    """A signed-in session on one FlashArray."""

    def __init__(self, base_url, username, password, insecure=False, timeout=60):
        """Sign in: trade the password for an API token, then the token for a session.

        Args:
            base_url: The array's address, such as https://array.example.com.
            username: The account to sign in with.
            password: Its password.
            insecure: Skip TLS verification.
            timeout: Seconds per request.

        Raises:
            RuntimeError: The array refused the sign-in.
        """
        if "://" not in base_url:
            base_url = f"https://{base_url}"
        self.base_url = f"{base_url.rstrip('/')}/api/{API_VERSION}"
        self.timeout = timeout
        context = ssl.create_default_context()
        if insecure:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
            urllib.request.HTTPSHandler(context=context),
        )
        token = (
            self._call(
                "POST", "/auth/apitoken", {"username": username, "password": password}
            )
            or {}
        ).get("api_token")
        if not token:
            raise RuntimeError("sign-in answered without an API token")
        self._call("POST", "/auth/session", {"api_token": token})

    def get(self, path, missing_is_empty=False):
        """Return a list from the API; [] for a 404 when missing_is_empty."""
        try:
            result = self._call("GET", path)
        except NotFound:
            if missing_is_empty:
                return []
            raise
        return result if isinstance(result, list) else []

    def logout(self):
        """End the session; a failure here changes nothing for the inventory."""
        try:
            self._call("DELETE", "/auth/session")
        except FAILURES:
            return

    def _call(self, method, path, body=None):
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method=method,
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            error = NotFound if e.code == 404 else RuntimeError
            raise error(f"HTTP {e.code} for {method} {path}") from None
        return json.loads(raw) if raw else None


def human_size(size):
    """Format a byte count the way the array's own tools do, such as 1.00 TiB."""
    if not size:
        return "0 B"
    for unit, divisor in (
        ("TiB", 2**40),
        ("GiB", 2**30),
        ("MiB", 2**20),
        ("KiB", 2**10),
    ):
        if size >= divisor:
            return f"{size / divisor:.2f} {unit}"
    return f"{size} B"


def count_luns(luns):
    """Count the LUNs in a classified set."""
    return len(luns.get("volumes", [])) + sum(
        len(group.get("volumes", []))
        for group in luns.get("volume_groups", {}).values()
    )


def classify_luns(mapped, volumes, vgroup_of):
    """Turn a list of mapped volumes into the classified set of LUNs.

    Membership of a volume group comes from the array's own list of groups,
    never from the slash in a volume's name.

    Args:
        mapped: Entries from a host or host group volume list, each with vol and lun.
        volumes: Volume name -> its catalogue entry (serial, size).
        vgroup_of: Full volume name -> the volume group it belongs to.

    Returns:
        The dict of volumes_all, volumes and volume_groups.
    """
    flat, standalone, groups = [], [], {}
    for entry in mapped:
        full = entry.get("vol", "")
        info = volumes.get(full, {})
        serial = info.get("serial", "")
        size = info.get("size", 0) or 0
        base = {
            "lun_id": entry.get("lun"),
            "serial": serial,
            "multipath_wwid": f"{WWID_PREFIX}{serial.lower()}" if serial else "",
            "size_bytes": size,
            "size_human": human_size(size),
        }
        group = vgroup_of.get(full)
        if group:
            short = full.removeprefix(f"{group}/")
            groups.setdefault(group, {"volumes": []})["volumes"].append(
                {"volume": short, "full_volume": full, **base}
            )
            flat.append(
                {"volume": short, "full_volume": full, "volume_group": group, **base}
            )
        else:
            standalone.append({"volume": full, **base})
            flat.append({"volume": full, "volume_group": None, **base})
    return {"volumes_all": flat, "volumes": standalone, "volume_groups": groups}


def resolve_fqdn(short, domain):
    """Name a host by its fully qualified name.

    A name with a dot is kept. Otherwise DNS is asked; when it has no longer
    name, domain is appended, or the name is kept when there is no domain.
    """
    if "." in short:
        return short
    try:
        fqdn = socket.getfqdn(short)
    except OSError:
        fqdn = short
    if fqdn and fqdn != short and "." in fqdn:
        return fqdn
    return f"{short}.{domain.lstrip('.')}" if domain else short


def _members(hgroup):
    return [
        h.get("name", str(h)) if isinstance(h, dict) else str(h)
        for h in hgroup.get("hosts", [])
    ]


def _catalogue(client):
    """Return the volume catalogue and the volume-to-group map."""
    volumes = {
        v["name"]: {
            "serial": v.get("serial", ""),
            "size": v.get("size", 0),
            "source": v.get("source", ""),
            "created": v.get("created", ""),
        }
        for v in client.get("/volume")
        if v.get("name")
    }
    vgroup_of = {
        volume: group.get("name", "")
        for group in client.get("/vgroup", missing_is_empty=True)
        for volume in group.get("volumes", [])
        if volume
    }
    return volumes, vgroup_of


def _host_vars(array, name, short, hgroups, luns, data):
    return {
        "pure_array": array,
        "pure_host": name,
        "pure_host_short": short,
        "pure_hgroups": hgroups,
        "pure_groups": [f"{HGROUP_PREFIX}{g}" for g in hgroups],
        "pure_luns_individual": luns,
        "pure_lun_count": count_luns(luns),
        "pure_iqns": data.get("iqn", []),
        "pure_wwns": data.get("wwn", []),
        "pure_nqns": data.get("nqn", []),
        "pure_host_personality": data.get("personality", ""),
    }


def build_inventory(client, array, resolve=False, domain="", workers=20):
    """Read the array and build the inventory.

    Args:
        client: A signed-in FlashArrayClient, or anything answering get.
        array: The array's host name, for pure_array and the group name.
        resolve: Name hosts by their fully qualified name.
        domain: With resolve, the suffix for names DNS does not know.
        workers: Parallel requests.

    Returns:
        The inventory.
    """
    with ThreadPoolExecutor(max_workers=4) as pool:
        catalogue = pool.submit(_catalogue, client)
        hgroups_future = pool.submit(client.get, "/hgroup")
        hosts_future = pool.submit(client.get, "/host")
        volumes, vgroup_of = catalogue.result()
        hgroups, hosts = hgroups_future.result(), hosts_future.result()

    members = {g.get("name", ""): _members(g) for g in hgroups}
    names = [h["name"] for h in hosts if h.get("name")]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        shared = dict(
            zip(
                members,
                pool.map(
                    lambda g: classify_luns(
                        client.get(f"/hgroup/{quote(g)}/volume", missing_is_empty=True),
                        volumes,
                        vgroup_of,
                    ),
                    members,
                ),
                strict=True,
            )
        )
        individual = dict(
            zip(
                names,
                pool.map(
                    lambda h: classify_luns(
                        client.get(f"/host/{quote(h)}/volume", missing_is_empty=True),
                        volumes,
                        vgroup_of,
                    ),
                    names,
                ),
                strict=True,
            )
        )

    every = set(names).union(*members.values()) if members else set(names)
    if resolve:
        with ThreadPoolExecutor(max_workers=DNS_WORKERS) as pool:
            fqdn = dict(
                zip(
                    every,
                    pool.map(lambda n: resolve_fqdn(n, domain), every),
                    strict=True,
                )
            )
    else:
        fqdn = {n: n for n in every}

    array_group = f"{ARRAY_PREFIX}{array}"
    inventory = {
        "_meta": {"hostvars": {}},
        "all": {"children": ["ungrouped", array_group]},
        array_group: {"hosts": [], "vars": {"pure_array": array}, "children": []},
    }
    for group, hosts_in in members.items():
        name = f"{HGROUP_PREFIX}{group}"
        luns = shared.get(group, {"volumes": [], "volume_groups": {}})
        inventory[name] = {
            "hosts": [fqdn[h] for h in hosts_in],
            "vars": {
                "pure_hgroup": group,
                "pure_group": name,
                "pure_array": array,
                "pure_luns_shared": luns,
                "pure_lun_count": count_luns(luns),
            },
        }
        inventory[array_group]["children"].append(name)
        if name not in inventory["all"]["children"]:
            inventory["all"]["children"].append(name)

    data = {h.get("name", ""): h for h in hosts}
    for short in names:
        name = fqdn[short]
        if name not in inventory[array_group]["hosts"]:
            inventory[array_group]["hosts"].append(name)
        groups = [g for g, m in members.items() if short in m]
        inventory["_meta"]["hostvars"][name] = _host_vars(
            array, name, short, groups, individual[short], data.get(short, {})
        )
    # Hosts that appear in a host group without a host object of their own.
    for short in every - set(names):
        name = fqdn[short]
        if name not in inventory["_meta"]["hostvars"]:
            groups = [g for g, m in members.items() if short in m]
            inventory["_meta"]["hostvars"][name] = _host_vars(
                array, name, short, groups, {"volumes": [], "volume_groups": {}}, {}
            )
    inventory["ungrouped"] = {"hosts": [], "vars": {}}
    return inventory


def host_vars(client, array, host, resolve=False, domain=""):
    """Return one host's variables, found by its short or resolved name."""
    try:
        volumes, vgroup_of = _catalogue(client)
    except FAILURES:
        volumes, vgroup_of = {}, {}

    def named(short):
        return resolve_fqdn(short, domain) if resolve else short

    data = next(
        (
            h
            for h in client.get("/host")
            if host in (h.get("name"), named(h.get("name", "")))
        ),
        {},
    )
    short = data.get("name", host)
    luns = classify_luns(
        client.get(f"/host/{quote(short)}/volume", missing_is_empty=True),
        volumes,
        vgroup_of,
    )
    groups = [g.get("name", "") for g in client.get("/hgroup") if short in _members(g)]
    return _host_vars(array, named(short), short, groups, luns, data)


def main(argv=None, environ=None, connect=FlashArrayClient):
    """Print the inventory, or one host's variables, as JSON."""
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="the whole inventory")
    mode.add_argument("--host", help="one host's variables")
    args = parser.parse_args(argv)

    arrays = credential_sets(
        "PURE", ("BASE_URL", "USERNAME", "PASSWORD"), environ=environ
    )
    if len(arrays) != 1:
        print(
            "need exactly one array: set CREDENTIAL_PURE_<TAG>_BASE_URL, _USERNAME and"
            f" _PASSWORD for one TAG (found {len(arrays)})",
            file=sys.stderr,
        )
        return 1
    array = arrays[0]
    name = urlparse(
        array["base_url"]
        if "://" in array["base_url"]
        else f"https://{array['base_url']}"
    ).netloc
    client = None
    try:
        config = settings(environ)
        resolve = as_bool(config.get("resolve_fqdn"))
        domain = config.get("domain") or ""
        client = connect(
            array["base_url"],
            array["username"],
            array["password"],
            insecure=as_bool(config.get("insecure")),
            timeout=as_int(config.get("timeout_seconds"), 60),
        )
        if args.list:
            result = build_inventory(
                client, name, resolve, domain, as_int(config.get("workers"), 20)
            )
        else:
            result = host_vars(client, name, args.host, resolve, domain)
    except (ConnectorError, *FAILURES) as e:
        print(f"{name}: {e}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.logout()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
