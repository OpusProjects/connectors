#!/usr/bin/env python3
"""Ansible dynamic inventory for Dell Avamar backup servers, through the REST API.

Lists the virtual machines registered on each Avamar server, with their most
recent backups, so a play or a form can see what is protected and pick a
backup to restore.

  --list    every registered VM, in avamar_registered > avamar_server_<server>
  --host    one VM's variables

Every host carries: avamar_registered, avamar_client_id, avamar_server,
avamar_domain, avamar_client_type, avamar_enabled, avamar_vm_name,
avamar_backups, avamar_backup_count and avamar_latest_backup. Each backup is a
dict of id, created ("YYYY-MM-DD HH:MM UTC"), size_gb, type, plugin_name,
retention, server and tier_status, newest first.

Servers come from the environment, one set per server: CREDENTIAL_AVAMAR_<TAG>_URL,
_USERNAME and _PASSWORD as unified-api hands them over, or AVAMAR_<TAG>_... as
an Ansible controller would, plus optional _CLIENT_ID and _CLIENT_SECRET to sign
in through OAuth2. TAG is any name; every complete set is polled.

Settings, from the source's config (SOURCE_CONFIG, JSON; strings are fine):
  domains          comma-separated suffixes tried after the server's own domain
                   to turn a VM name into a host name that resolves
  max_backups      backups listed per VM, default 20
  skip_backups     true to list registrations only
  workers          parallel backup requests per server, default 8
  insecure         true to skip TLS verification (self-signed servers)
  timeout_seconds  per request, default 30
"""

import argparse
import base64
import json
import os
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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

PAGE_SIZE = 200
MAX_PAGES = 500

# What a server that is down, slow, refusing or answering nonsense raises:
# network errors and timeouts are OSError, a garbled answer a ValueError (JSON),
# a refused request a RuntimeError.
FAILURES = (OSError, ValueError, RuntimeError)


class AvamarClient:
    """A session on one Avamar server's REST API."""

    def __init__(
        self,
        base_url,
        username,
        password,
        client_id=None,
        client_secret=None,
        insecure=False,
        timeout=30,
    ):
        """Prepare a session; login() signs in.

        Args:
            base_url: The server's address, such as https://avamar.example.com.
            username: The account to sign in with.
            password: Its password.
            client_id: OAuth2 client id; without it, every request uses basic auth.
            client_secret: OAuth2 client secret.
            insecure: Skip TLS verification.
            timeout: Seconds per request.
        """
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.token = None
        self.context = ssl.create_default_context()
        if insecure:
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE

    @property
    def hostname(self):
        """The server's host name, the label its VMs are grouped under."""
        return urlparse(self.base_url).hostname or self.base_url

    def login(self):
        """Get an OAuth2 token when a client id and secret are set.

        Raises:
            RuntimeError: The server refused the sign-in.
        """
        if not (self.client_id and self.client_secret):
            self.token = None
            return
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        body = urlencode(
            {
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
                "scope": "read",
            }
        ).encode()
        data = self._request(
            "POST",
            "/api/v1/oauth/swagger",
            body,
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {basic}",
            },
        )
        self.token = (data or {}).get("access_token")
        if not self.token:
            raise RuntimeError("sign-in answered without an access token")

    def list_clients(self):
        """Return the registered virtual machines, retired ones left out."""
        found = []
        for page in range(MAX_PAGES):
            data = self._get(
                "/api/v1/clients",
                {
                    "domain": "/",
                    "recursive": "true",
                    "size": PAGE_SIZE,
                    "page": page,
                    "fields": "id,name,domainFqdn,clientType,enabled,activated",
                },
            )
            if isinstance(data, list):
                found.extend(data)
                break
            if not isinstance(data, dict) or "content" not in data:
                break
            items = data.get("content") or []
            found.extend(items)
            if data.get("last") is True or not items:
                break
        return [
            c
            for c in found
            if str(c.get("clientType", "")).upper() == "VMACHINE"
            and "RETIRED" not in (c.get("domainFqdn") or "").upper()
        ]

    def list_backups(self, client_id, max_backups):
        """Return one VM's most recent backups; empty when the server refuses."""
        try:
            data = self._get(
                f"/api/v1/clients/{client_id}/backups", {"size": max_backups, "page": 0}
            )
        except RuntimeError:
            return []
        if isinstance(data, dict) and "content" in data:
            return data.get("content") or []
        return data if isinstance(data, list) else []

    def _get(self, path, params):
        query = urlencode({k: v for k, v in params.items() if v is not None})
        return self._request("GET", f"{path}?{query}", None, self._auth())

    def _auth(self):
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        basic = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return {"Authorization": f"Basic {basic}"}

    def _request(self, method, path, body, headers):
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={"Accept": "application/json", **headers},
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self.context
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"HTTP {e.code} for {method} {path.split('?')[0]}"
            ) from None
        return json.loads(raw) if raw else None


def resolve_fqdn(short, preferred, domains):
    """Turn a VM name into a host name.

    A name with a dot is kept. Otherwise the server's own domain, then each of
    domains, is tried until one resolves; when none does, the server's domain
    is used anyway, so the name stays the same from one run to the next.
    """
    if "." in short:
        return short
    candidates = list(dict.fromkeys([d for d in [preferred, *domains] if d]))
    for domain in candidates:
        try:
            socket.getaddrinfo(f"{short}.{domain}", None, socket.AF_INET)
            return f"{short}.{domain}"
        except OSError:
            continue
    return f"{short}.{preferred}" if preferred else short


def normalize_backup(backup):
    """Reduce one backup to the fields the inventory carries.

    Avamar reports times in UTC (a trailing Z), while servers schedule in their
    own local time, so the time keeps its zone: "2026-07-22T22:10:54.000Z"
    becomes "2026-07-22 22:10 UTC". A bare time read as local picks the wrong
    backup for anything that ran between 22:00 and midnight UTC. A value in any
    other shape passes through unlabelled rather than being claimed as UTC.
    """
    date = (
        backup.get("date") or backup.get("created") or backup.get("createdTime") or ""
    )
    if isinstance(date, str) and "T" in date:
        utc = date.endswith("Z")
        date = date.replace("T", " ").split(".")[0][:16]
        if utc and date:
            date += " UTC"
    try:
        size = float(backup.get("size") or backup.get("sizeInBytes") or 0)
    except (TypeError, ValueError):
        size = 0
    tags = backup.get("retentionTags") or {}
    retention = ""
    if isinstance(tags, dict):
        retention = tags.get("displayValue") or tags.get("retentionTagsAvValue") or ""
    return {
        "id": str(
            backup.get("labelNumber")
            or backup.get("id")
            or backup.get("backupId")
            or ""
        ),
        "created": date,
        "size_gb": round(size / 1024**3, 2) if size > 0 else None,
        "type": backup.get("type") or "",
        "plugin_name": backup.get("pluginName") or backup.get("plugin") or "",
        "retention": retention,
        "server": backup.get("server") or "",
        "tier_status": backup.get("tierStatus") or "",
    }


def _log(verbose, message):
    if verbose:
        print(message, file=sys.stderr)


def _backups(client, clients, max_backups, workers, verbose):
    """Fetch every VM's backups in parallel, an empty list for any that fails."""
    found = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(client.list_backups, str(c["id"]), max_backups): str(c["id"])
            for c in clients
            if c.get("id")
        }
        for future in as_completed(futures):
            client_id = futures[future]
            try:
                found[client_id] = future.result()
            except FAILURES as e:
                _log(verbose, f"backups of client {client_id}: {e}")
                found[client_id] = []
    return found


def build_inventory(
    servers,
    connect=AvamarClient,
    domains=(),
    max_backups=20,
    fetch_backups=True,
    workers=8,
    insecure=False,
    timeout=30,
    verbose=False,
):
    """Poll every server and build the inventory.

    Args:
        servers: Credential sets, each with url, username, password, client_id
            and client_secret.
        connect: Builds a client; AvamarClient unless a test fakes it.
        domains: Suffixes tried after each server's own domain.
        max_backups: Backups listed per VM.
        fetch_backups: False to list registrations only.
        workers: Parallel backup requests per server.
        insecure: Skip TLS verification.
        timeout: Seconds per request.
        verbose: Report progress on stderr.

    Returns:
        The inventory, each host's variables by name, and the servers that failed.
    """
    inventory = {
        "all": {"children": ["avamar_registered"]},
        "avamar_registered": {"children": [], "hosts": []},
        "_meta": {"hostvars": {}},
    }
    by_name, failed = {}, []
    for server in servers:
        try:
            client = connect(
                server["url"],
                server["username"],
                server["password"],
                client_id=server.get("client_id") or None,
                client_secret=server.get("client_secret") or None,
                insecure=insecure,
                timeout=timeout,
            )
            client.login()
            started = time.monotonic()
            clients = client.list_clients()
            _log(verbose, f"{client.hostname}: {len(clients)} VMs")
            backups = (
                _backups(client, clients, max_backups, workers, verbose)
                if fetch_backups
                else {}
            )
            _log(
                verbose, f"{client.hostname}: read in {time.monotonic() - started:.1f}s"
            )
        except FAILURES as e:
            print(f"{server['url']}: {e}", file=sys.stderr)
            failed.append(server["url"])
            continue

        group = f"avamar_server_{client.hostname.replace('.', '_')}"
        inventory[group] = {"hosts": [], "vars": {"avamar_server": client.hostname}}
        inventory["avamar_registered"]["children"].append(group)
        own_domain = (
            client.hostname.split(".", 1)[1] if "." in client.hostname else None
        )
        for c in clients:
            name = c.get("name", "")
            if not name:
                continue
            host = resolve_fqdn(name, own_domain, domains)
            if host in inventory["_meta"]["hostvars"]:
                _log(verbose, f"{host} is registered twice; keeping the first")
                continue
            client_id = str(c.get("id", ""))
            listed = [normalize_backup(b) for b in backups.get(client_id, [])]
            listed.sort(key=lambda b: b.get("created") or "", reverse=True)
            hostvars = {
                "avamar_registered": True,
                "avamar_client_id": client_id,
                "avamar_server": client.hostname,
                "avamar_domain": c.get("domainFqdn", ""),
                "avamar_client_type": c.get("clientType", ""),
                "avamar_enabled": c.get("enabled", False),
                "avamar_vm_name": name,
                "avamar_backups": listed,
                "avamar_backup_count": len(listed),
                "avamar_latest_backup": listed[0]["created"] if listed else None,
            }
            inventory["_meta"]["hostvars"][host] = hostvars
            inventory[group]["hosts"].append(host)
            by_name[host] = hostvars
    return inventory, by_name, failed


def main(argv=None, environ=None, connect=AvamarClient):
    """Print the inventory, or one host's variables, as JSON."""
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="the whole inventory")
    mode.add_argument("--host", help="one host's variables")
    parser.add_argument("--debug", action="store_true", help="progress on stderr")
    parser.add_argument("--max-backups", type=int, default=20, help="backups per VM")
    parser.add_argument(
        "--skip-backups", action="store_true", help="registrations only"
    )
    parser.add_argument(
        "--workers", type=int, default=8, help="parallel backup requests"
    )
    args = parser.parse_args(argv)

    servers = credential_sets(
        "AVAMAR",
        ("URL", "USERNAME", "PASSWORD"),
        ("CLIENT_ID", "CLIENT_SECRET"),
        environ,
    )
    if not servers:
        print(
            "no Avamar credentials: set CREDENTIAL_AVAMAR_<TAG>_URL, _USERNAME and _PASSWORD",
            file=sys.stderr,
        )
        return 1
    try:
        config = settings(environ)
        inventory, by_name, failed = build_inventory(
            servers,
            connect,
            domains=as_list(config.get("domains")),
            max_backups=as_int(config.get("max_backups"), args.max_backups),
            fetch_backups=not (
                args.skip_backups or as_bool(config.get("skip_backups"))
            ),
            workers=as_int(config.get("workers"), args.workers),
            insecure=as_bool(config.get("insecure")),
            timeout=as_int(config.get("timeout_seconds"), 30),
            verbose=args.debug,
        )
    except ConnectorError as e:
        print(e, file=sys.stderr)
        return 1
    if len(failed) == len(servers):
        # Not one server answered. An empty inventory here would read as
        # "nothing is backed up" and sync green; a failure is what the caller records.
        print(f"no Avamar server answered ({', '.join(failed)})", file=sys.stderr)
        return 1
    if args.list:
        print(json.dumps(inventory, indent=2, sort_keys=True))
    else:
        print(json.dumps(by_name.get(args.host, {}), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
