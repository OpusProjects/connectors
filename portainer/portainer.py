#!/usr/bin/env python3
"""Gitops connector for Portainer: its settings, environments and users.

Portainer keeps its own configuration in its database, reachable only through
its API, and this connector is the layer between that API and the YAML in git.
Field names are Portainer's own, as GET returns them, so a desired-state file
reads against the API reference without a translation table.

Kinds, described to unified-gitops by the describe command (see KINDS):
  settings     the global settings, one object named "settings"
  environment  an environment, found by Name; created only for agents
  user         a user who may sign in, found by Username; the built-in admin
               (user 1) is never listed or touched

Settings (the application's config):
  url       https URL of Portainer, as its certificate names it
  address   optional address to connect to instead, keeping the URL's name for
            TLS and the Host header (for a runner that cannot resolve it)
  insecure  true to skip certificate verification

Credentials:
  api_key             a Portainer access token; or username and password
  ldap_bind_password  the LDAP bind password, sent with every settings update
                      that carries LDAPSettings, because Portainer never
                      returns it and would otherwise blank it
"""

import http.client
import json
import os
import socket
import ssl
import sys
import uuid
from urllib.parse import urlsplit

# The SDK, the unified_connectors package, sits at the repository root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unified_connectors import ConnectorError, run

# Nothing in Portainer makes one kind depend on another.
KINDS = {
    "settings": {"key": "name"},
    "environment": {"key": "Name"},
    "user": {"key": "Username"},
}

SETTINGS_NAME = "settings"
BUILTIN_ADMIN = 1
ROLES = {"administrator": 1, "standard": 2}
ROLE_NAMES = {v: k for k, v in ROLES.items()}

# Environment Type, as GET returns it, to the EndpointCreationType that POST
# takes. Only agents are created from outside: a local environment is
# Portainer's to create, at install.
CREATION_TYPES = {2: 2, 6: 2}
LOCAL_TYPES = {1, 5}

FAILURES = (OSError, http.client.HTTPException, ValueError)


class _PinnedHTTPS(http.client.HTTPSConnection):
    """Connect to one address and present another name for TLS and Host."""

    def __init__(self, hostname, address, port, context):
        super().__init__(hostname, port, context=context, timeout=30)
        self._address = address

    def connect(self):
        """Open the socket to the pinned address, then wrap it for the hostname."""
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _as_bool(value):
    """Read a JSON boolean or a true, 1 or yes string."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


def _multipart(fields):
    """Encode plain fields as multipart/form-data, booleans as true or false."""
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; "
            f'name="{name}"\r\n\r\n{value}\r\n'
        )
    parts.append(f"--{boundary}--\r\n")
    return "".join(parts).encode(), f"multipart/form-data; boundary={boundary}"


class Api:
    """A signed-in client for one Portainer server."""

    def __init__(self, config, credentials):
        """Read where Portainer is and sign in.

        Args:
            config: The application's settings: url, address, insecure.
            credentials: api_key, or username and password.

        Raises:
            ConnectorError: The URL is missing or not https, or no credential is set.
        """
        url = urlsplit(str(config.get("url", "")))
        if url.scheme != "https" or not url.hostname:
            raise ConnectorError("application config needs 'url', an https:// URL")
        self.hostname = url.hostname
        self.port = url.port or 443
        self.address = config.get("address") or self.hostname
        self.context = ssl.create_default_context()
        if _as_bool(config.get("insecure", False)):
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE
        self.headers = {"Content-Type": "application/json"}
        if credentials.get("api_key"):
            self.headers["X-API-Key"] = credentials["api_key"]
        elif credentials.get("username") and credentials.get("password"):
            body = {
                "username": credentials["username"],
                "password": credentials["password"],
            }
            jwt = self.call("POST", "/api/auth", body)["jwt"]
            self.headers["Authorization"] = f"Bearer {jwt}"
        else:
            raise ConnectorError("credentials need api_key, or username and password")

    def call(self, method, path, body=None, form=None):
        """Send one request and return its JSON answer.

        Args:
            method: The HTTP method.
            path: The API path, starting with /api.
            body: A JSON body, if any.
            form: Fields to send as multipart/form-data instead of JSON.

        Returns:
            The decoded answer, or None when it is empty.

        Raises:
            ConnectorError: Portainer is unreachable or answers with an error.
        """
        headers = dict(self.headers)
        if form is not None:
            data, headers["Content-Type"] = _multipart(form)
        else:
            data = json.dumps(body) if body is not None else None
        conn = _PinnedHTTPS(self.hostname, self.address, self.port, self.context)
        try:
            conn.request(method, path, data, headers)
            resp = conn.getresponse()
            raw = resp.read()
        except FAILURES as e:
            raise ConnectorError(f"{method} {path}: {e}") from None
        finally:
            conn.close()
        if resp.status >= 400:
            # The answer carries Portainer's reason; never echo the request,
            # which may hold a credential.
            reason = raw[:300].decode(errors="replace")
            raise ConnectorError(f"{method} {path}: HTTP {resp.status} {reason}")
        return json.loads(raw) if raw else None


def _merge(base, over):
    """Lay over onto base: mappings merge, anything else is replaced."""
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _merge(base.get(k), v)
        return out
    return over


def _environments(api):
    """Return the live environments keyed by Name."""
    return {e["Name"]: e for e in api.call("GET", "/api/endpoints")}


def _users(api):
    """Return the live users keyed by lowercase Username, without the built-in admin."""
    return {
        u["Username"].lower(): u
        for u in api.call("GET", "/api/users")
        if u["Id"] != BUILTIN_ADMIN
    }


def list_objects(kind, config, credentials):
    """Return what Portainer has of one kind.

    Args:
        kind: settings, environment or user.
        config: The application's settings.
        credentials: The application's credentials.

    Returns:
        The objects of that kind, sorted by their key.
    """
    api = Api(config, credentials)
    if kind == "settings":
        return [{"name": SETTINGS_NAME, **api.call("GET", "/api/settings")}]
    if kind == "environment":
        envs = _environments(api)
        return [envs[name] for name in sorted(envs)]
    users = _users(api)
    return [
        {
            "Username": name,
            "Role": ROLE_NAMES.get(users[name]["Role"], users[name]["Role"]),
            "Id": users[name]["Id"],
        }
        for name in sorted(users)
    ]


def _apply_settings(api, op, obj, credentials):
    """Update the global settings; they always exist and cannot be deleted."""
    if obj.get("name") != SETTINGS_NAME:
        raise ConnectorError(
            f"settings: the one object is named '{SETTINGS_NAME}', not '{obj.get('name')}'"
        )
    if op == "delete":
        raise ConnectorError("settings cannot be deleted")
    body = {k: v for k, v in obj.items() if k != "name"}
    if "LDAPSettings" in body:
        password = credentials.get("ldap_bind_password")
        if not password:
            raise ConnectorError(
                "settings declare LDAPSettings: credentials need ldap_bind_password"
            )
        body["LDAPSettings"] = {**body["LDAPSettings"], "Password": password}
    api.call("PUT", "/api/settings", body)


def _create_environment(api, obj):
    """Create an agent environment, then apply what the object declares beyond it."""
    kind = obj.get("Type")
    if kind not in CREATION_TYPES:
        raise ConnectorError(
            f"environment '{obj['Name']}': only agents (Type 2 or 6) are created "
            "here; a local environment is Portainer's to create"
        )
    if not obj.get("URL"):
        raise ConnectorError(f"environment '{obj['Name']}': an agent needs its URL")
    tls = obj.get("TLSConfig") or {}
    # Portainer dials the agent while handling this, so it fails unless the
    # agent answers on the URL and shares the server's AGENT_SECRET. An agent
    # always speaks TLS with its own certificate, hence the two skips.
    form = {
        "Name": obj["Name"],
        "EndpointCreationType": CREATION_TYPES[kind],
        "URL": obj["URL"],
        "TLS": True,
        "TLSSkipVerify": _as_bool(tls.get("TLSSkipVerify", True)),
        "TLSSkipClientVerify": True,
    }
    if obj.get("ContainerEngine"):
        form["ContainerEngine"] = obj["ContainerEngine"]
    api.call("POST", "/api/endpoints", form=form)


def _apply_environment(api, op, obj):
    """Create, update or delete one environment."""
    name = obj["Name"]
    if op == "create":
        _create_environment(api, obj)
    live = _environments(api).get(name)
    if live is None:
        if op == "delete":
            return
        raise ConnectorError(f"environment '{name}' is not in Portainer")
    if op == "delete":
        if live.get("Type") in LOCAL_TYPES:
            raise ConnectorError(
                f"environment '{name}' is Portainer's own local one; not deleted"
            )
        api.call("DELETE", f"/api/endpoints/{live['Id']}")
        return
    # PUT replaces each top-level block it is given (the whole Kubernetes
    # block, snapshots included), so send the live block with the declared
    # one laid over it, never the declared fragment on its own. Fields that
    # only creation reads are left out.
    create_only = ("Name", "Type", "ContainerEngine", "TLSConfig")
    declared = {k: v for k, v in obj.items() if k not in create_only}
    body = {k: _merge(live.get(k), v) for k, v in declared.items()}
    if body:
        api.call("PUT", f"/api/endpoints/{live['Id']}", body)


def _apply_user(api, op, obj):
    """Create a user, change its role, or delete it."""
    name = str(obj["Username"]).lower()
    if name == "admin":
        raise ConnectorError("the built-in admin is not managed here")
    live = _users(api).get(name)
    if op == "delete":
        if live is not None:
            api.call("DELETE", f"/api/users/{live['Id']}")
        return
    role = obj.get("Role")
    if role not in ROLES:
        raise ConnectorError(
            f"user '{name}': Role is one of {', '.join(ROLES)}, not '{role}'"
        )
    if live is None:
        # No password: with LDAP sign-in on, the user authenticates there.
        body = {"Username": name, "Password": "", "Role": ROLES[role]}
        api.call("POST", "/api/users", body)
    elif live["Role"] != ROLES[role]:
        api.call("PUT", f"/api/users/{live['Id']}", {"Role": ROLES[role]})


def apply(kind, op, obj, config, credentials):
    """Create, update or delete one object.

    Args:
        kind: settings, environment or user.
        op: create, update or delete.
        obj: The object as git declares it, or as list returned it for a delete.
        config: The application's settings.
        credentials: The application's credentials.
    """
    api = Api(config, credentials)
    if kind == "settings":
        _apply_settings(api, op, obj, credentials)
    elif kind == "environment":
        _apply_environment(api, op, obj)
    else:
        _apply_user(api, op, obj)
    print(f"{op} {kind} '{obj.get(KINDS[kind]['key'])}'", file=sys.stderr)


if __name__ == "__main__":
    run(kinds=KINDS, list_objects=list_objects, apply=apply)
