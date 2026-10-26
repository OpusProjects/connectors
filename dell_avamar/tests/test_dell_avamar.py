import importlib.util
import json
import socket
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "dell_avamar", HERE.parent / "dell_avamar.py"
)
avamar = importlib.util.module_from_spec(spec)
spec.loader.exec_module(avamar)


def fixture(name):
    return json.loads((HERE / "fixtures" / name).read_text())


class FakeClient(avamar.AvamarClient):
    """The real client, answering from the fixtures instead of the network."""

    def _request(self, method, path, body, headers):
        if path.startswith("/api/v1/oauth"):
            return {"access_token": "t"}
        if path.startswith("/api/v1/clients?"):
            return fixture(
                "clients-page1.json" if "page=1" in path else "clients-page0.json"
            )
        client_id = path.split("/")[4]
        if client_id == "c5":
            raise RuntimeError("HTTP 500")
        return fixture(f"backups-{client_id}.json")


class DownClient(FakeClient):
    def login(self):
        raise OSError("connection refused")


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    def lookup(name, *args):
        if name == "app01.example.org":
            return [()]
        raise socket.gaierror(name)

    monkeypatch.setattr(avamar.socket, "getaddrinfo", lookup)


SERVER = {"url": "https://avamar.example.com", "username": "r", "password": "p"}


def build(**kwargs):
    return avamar.build_inventory([SERVER], FakeClient, **kwargs)


def test_lists_registered_vms_across_pages_without_retired_or_agents():
    inventory, _, failed = build(domains=["example.org"])
    assert failed == []
    group = inventory["avamar_server_avamar_example_com"]
    assert group["vars"] == {"avamar_server": "avamar.example.com"}
    assert sorted(group["hosts"]) == [
        "app01.example.org",
        "db01.example.net",
        "web01.example.com",
    ]
    assert inventory["avamar_registered"]["children"] == [
        "avamar_server_avamar_example_com"
    ]


def test_a_name_that_resolves_nowhere_takes_the_server_domain():
    _, by_name, _ = build()
    assert "web01.example.com" in by_name


def test_backups_are_normalised_newest_first():
    _, by_name, _ = build()
    web = by_name["web01.example.com"]
    assert web["avamar_backup_count"] == 2
    assert web["avamar_latest_backup"] == "2026-07-22 22:10 UTC"
    assert web["avamar_backups"][0] == {
        "id": "42",
        "created": "2026-07-22 22:10 UTC",
        "size_gb": 5.0,
        "type": "SCHEDULED",
        "plugin_name": "Linux VMware Image",
        "retention": "daily",
        "server": "avamar",
        "tier_status": "ACTIVE",
    }


def test_a_time_without_zone_is_not_claimed_as_utc():
    _, by_name, _ = build()
    backup = by_name["db01.example.net"]["avamar_backups"][0]
    assert backup["created"] == "2026-07-20 03:00" and backup["size_gb"] is None


def test_a_refused_backup_list_leaves_the_vm_with_none():
    _, by_name, _ = build(domains=["example.org"])
    assert by_name["app01.example.org"]["avamar_backups"] == []


def test_skip_backups_lists_registrations_only():
    _, by_name, _ = build(fetch_backups=False)
    assert all(h["avamar_backup_count"] == 0 for h in by_name.values())


ENV = {
    "AVAMAR_A_URL": "https://avamar.example.com",
    "AVAMAR_A_USERNAME": "r",
    "AVAMAR_A_PASSWORD": "p",
    "AVAMAR_A_CLIENT_ID": "id",
    "AVAMAR_A_CLIENT_SECRET": "s",
}


def test_list_reads_settings_given_as_strings(capsys):
    environ = {
        **ENV,
        "SOURCE_CONFIG": json.dumps({"skip_backups": "true", "domains": "example.org"}),
    }
    assert avamar.main(["--list"], environ, FakeClient) == 0
    inventory = json.loads(capsys.readouterr().out)
    assert inventory["_meta"]["hostvars"]["app01.example.org"]["avamar_backups"] == []


def test_host_prints_one_vm(capsys):
    assert avamar.main(["--host", "web01.example.com"], ENV, FakeClient) == 0
    assert json.loads(capsys.readouterr().out)["avamar_client_id"] == "c1"


def test_no_server_answering_is_a_failure_not_an_empty_inventory(capsys):
    assert avamar.main(["--list"], ENV, DownClient) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "no Avamar server answered" in captured.err


def test_no_credentials_is_a_failure(capsys):
    assert avamar.main(["--list"], {}, FakeClient) == 1
