"""The Portainer connector against an in-memory Portainer API."""

import copy
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "portainer.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

spec = importlib.util.spec_from_file_location("portainer", SCRIPT)
portainer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(portainer)

CONFIG = {"url": "https://portainer.example.com"}
CREDS = {"api_key": "k", "ldap_bind_password": "bind-secret"}


class FakePortainer:
    """Answers the handful of routes the connector calls, from fixtures."""

    def __init__(self):
        self.state = json.loads((FIXTURES / "portainer.json").read_text())
        self.calls = []

    def call(self, method, path, body=None, form=None):
        self.calls.append((method, path, body if form is None else form))
        s = self.state
        if (method, path) == ("GET", "/api/settings"):
            return copy.deepcopy(s["settings"])
        if (method, path) == ("PUT", "/api/settings"):
            s["settings"] = portainer._merge(s["settings"], body)
            return None
        if (method, path) == ("GET", "/api/endpoints"):
            return copy.deepcopy(s["endpoints"])
        if (method, path) == ("POST", "/api/endpoints"):
            new_id = max(e["Id"] for e in s["endpoints"]) + 1
            s["endpoints"].append(
                {"Id": new_id, "Name": form["Name"], "Type": 2, "URL": form["URL"]}
            )
            return None
        if path.startswith("/api/endpoints/"):
            eid = int(path.rsplit("/", 1)[1])
            if method == "DELETE":
                s["endpoints"] = [e for e in s["endpoints"] if e["Id"] != eid]
            else:
                env = next(e for e in s["endpoints"] if e["Id"] == eid)
                env.update(body)
            return None
        if (method, path) == ("GET", "/api/users"):
            return copy.deepcopy(s["users"])
        if (method, path) == ("POST", "/api/users"):
            new_id = max(u["Id"] for u in s["users"]) + 1
            s["users"].append(
                {"Id": new_id, "Username": body["Username"], "Role": body["Role"]}
            )
            return None
        if path.startswith("/api/users/"):
            uid = int(path.rsplit("/", 1)[1])
            if method == "DELETE":
                s["users"] = [u for u in s["users"] if u["Id"] != uid]
            else:
                next(u for u in s["users"] if u["Id"] == uid).update(body)
            return None
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def fake(monkeypatch):
    server = FakePortainer()
    monkeypatch.setattr(portainer, "Api", lambda config, credentials: server)
    return server


def test_settings_are_one_object_named_settings(fake):
    (settings,) = portainer.list_objects("settings", CONFIG, CREDS)
    assert settings["name"] == "settings"
    assert settings["AuthenticationMethod"] == 1


def test_settings_update_sends_the_bind_password_git_never_holds(fake):
    obj = {
        "name": "settings",
        "AuthenticationMethod": 2,
        "LDAPSettings": {"URL": "ldap.example.com:636"},
    }
    portainer.apply("settings", "update", obj, CONFIG, CREDS)
    method, path, body = fake.calls[-1]
    assert (method, path) == ("PUT", "/api/settings")
    assert body["LDAPSettings"]["Password"] == "bind-secret"
    assert "name" not in body


def test_settings_with_ldap_need_the_bind_password(fake):
    obj = {"name": "settings", "LDAPSettings": {"URL": "ldap.example.com:636"}}
    with pytest.raises(portainer.ConnectorError, match="ldap_bind_password"):
        portainer.apply("settings", "update", obj, CONFIG, {"api_key": "k"})


def test_settings_are_never_deleted(fake):
    with pytest.raises(portainer.ConnectorError, match="cannot be deleted"):
        portainer.apply("settings", "delete", {"name": "settings"}, CONFIG, CREDS)


def test_environments_are_listed_by_name(fake):
    names = [e["Name"] for e in portainer.list_objects("environment", CONFIG, CREDS)]
    assert names == sorted(names)


def test_an_environment_update_keeps_the_live_block_it_does_not_declare(fake):
    obj = {"Name": "Local", "Kubernetes": {"Configuration": {"UseServerMetrics": True}}}
    portainer.apply("environment", "update", obj, CONFIG, CREDS)
    method, path, body = fake.calls[-1]
    assert (method, path) == ("PUT", "/api/endpoints/1")
    k8s = body["Kubernetes"]
    assert k8s["Configuration"]["UseServerMetrics"] is True
    assert k8s["Snapshots"] == [{"KubernetesVersion": "v1.30.0"}]


def test_an_agent_is_created_then_given_what_it_declares(fake):
    obj = {
        "Name": "agent-a",
        "Type": 2,
        "URL": "tcp://agent-a.example.com:9001",
        "ContainerEngine": "podman",
        "TLSConfig": {"TLS": True, "TLSSkipVerify": True},
        "TagIds": [1],
    }
    portainer.apply("environment", "create", obj, CONFIG, CREDS)
    posts = [c for c in fake.calls if c[0] == "POST"]
    form = posts[0][2]
    assert form["EndpointCreationType"] == 2
    assert form["ContainerEngine"] == "podman"
    assert form["TLSSkipClientVerify"] is True
    method, _, body = fake.calls[-1]
    assert method == "PUT" and body == {"URL": obj["URL"], "TagIds": [1]}


def test_a_local_environment_is_not_created_here(fake):
    obj = {"Name": "Other", "Type": 5}
    with pytest.raises(portainer.ConnectorError, match="only agents"):
        portainer.apply("environment", "create", obj, CONFIG, CREDS)


def test_the_local_environment_is_never_deleted(fake):
    live = portainer.list_objects("environment", CONFIG, CREDS)
    local = next(e for e in live if e["Name"] == "Local")
    with pytest.raises(portainer.ConnectorError, match="local"):
        portainer.apply("environment", "delete", local, CONFIG, CREDS)


def test_users_leave_out_the_builtin_admin_and_name_their_role(fake):
    users = portainer.list_objects("user", CONFIG, CREDS)
    assert [u["Username"] for u in users] == ["jane.doe", "john.roe"]
    assert users[0]["Role"] == "administrator"
    assert users[1]["Role"] == "standard"


def test_a_user_is_created_without_a_password(fake):
    obj = {"Username": "new.user", "Role": "standard"}
    portainer.apply("user", "create", obj, CONFIG, CREDS)
    assert fake.calls[-1] == (
        "POST",
        "/api/users",
        {"Username": "new.user", "Password": "", "Role": 2},
    )


def test_a_role_change_is_one_put(fake):
    portainer.apply(
        "user",
        "update",
        {"Username": "john.roe", "Role": "administrator"},
        CONFIG,
        CREDS,
    )
    assert fake.calls[-1] == ("PUT", "/api/users/3", {"Role": 1})


def test_a_user_is_deleted_by_id(fake):
    portainer.apply("user", "delete", {"Username": "john.roe"}, CONFIG, CREDS)
    assert fake.calls[-1] == ("DELETE", "/api/users/3", None)


def test_the_builtin_admin_is_refused(fake):
    with pytest.raises(portainer.ConnectorError, match="built-in admin"):
        portainer.apply("user", "create", {"Username": "admin"}, CONFIG, CREDS)


def test_the_url_must_be_https():
    with pytest.raises(portainer.ConnectorError, match="https"):
        portainer.Api({"url": "http://portainer.example.com"}, CREDS)


def test_describe_and_the_write_guard():
    env = {"PATH": os.environ["PATH"], "APPLICATION_CONFIG": json.dumps(CONFIG)}
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "describe"],
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    kinds = {k["kind"]: k["key"] for k in json.loads(out.stdout)["kinds"]}
    assert kinds == {"settings": "name", "environment": "Name", "user": "Username"}
    write = json.dumps({"op": "create", "object": {"Username": "x"}})
    out = subprocess.run(
        [sys.executable, str(SCRIPT), "apply", "--kind", "user"],
        input=write,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert out.returncode != 0 and "UNIFIED_ALLOW_WRITE" in out.stderr
