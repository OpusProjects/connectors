import importlib.util
import json
import socket
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("cisco_ucs", HERE.parent / "cisco_ucs.py")
ucs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ucs)


def fixture(name):
    return ET.parse(HERE / "fixtures" / name).getroot()


class FakeClient:
    """Answers the XML API calls from the fixtures."""

    def __init__(self, *args, **kwargs):
        self.logged_out = False

    def resolve_class(self, class_id):
        return fixture(f"{class_id}.xml")

    def resolve_children(self, dn, class_id):
        name = "web01" if "web01" in dn else "db01"
        return fixture(f"{class_id}-{name}.xml")

    def resolve_dns(self, dns):
        assert dns == ["sys/chassis-1/blade-2"]
        return fixture("blade-2.xml")

    def logout(self):
        self.logged_out = True


class DownClient:
    def __init__(self, url, *args, **kwargs):
        raise OSError("connection refused")


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    def lookup(name, *args):
        if name == "web01.example.com":
            return [()]
        raise socket.gaierror(name)

    monkeypatch.setattr(ucs.socket, "getaddrinfo", lookup)


def test_servers_are_named_after_their_profile_and_resolved():
    servers, _ = ucs.collect(FakeClient(), "ucs-a.example.com", ["example.com"])
    assert sorted(servers) == ["db01", "web01.example.com"]
    web = servers["web01.example.com"]
    assert web["ucs_memory"] == "384G" and web["ucs_memory_mb"] == 393216
    assert web["ucs_chassis"] == 1 and web["ucs_slot"] == 1
    assert web["ucs_service_profile"] == "org-root/ls-web01"


def test_vnics_carry_mac_and_native_vlan():
    servers, _ = ucs.collect(FakeClient(), "ucs-a.example.com", [])
    assert servers["web01"]["ucs_vnics"] == [
        {"name": "eth0", "mac": "00:25:B5:00:00:01", "vlan": 110}
    ]


def test_a_derived_mac_is_read_from_the_hardware():
    servers, _ = ucs.collect(FakeClient(), "ucs-a.example.com", [])
    assert servers["db01"]["ucs_vnics"] == [
        {"name": "eth0", "mac": "00:25:B5:00:00:02", "vlan": 120}
    ]


def test_servers_inventory_groups_blades_by_manager():
    servers, _ = ucs.collect(FakeClient(), "ucs-a.example.com", [])
    inventory = ucs.build_inventory(servers)
    assert inventory["all"] == {"children": ["physical"]}
    assert inventory["ucs_blades"]["children"] == ["ucs_manager_ucs_a_example_com"]
    group = inventory["ucs_manager_ucs_a_example_com"]
    assert group == {
        "hosts": ["db01", "web01"],
        "vars": {"ucs_manager": "ucs-a.example.com"},
    }


def test_spares_split_into_free_and_unusable():
    _, spares = ucs.collect(FakeClient(), "ucs-a.example.com", [])
    inventory = ucs.build_spare_inventory(spares)
    assert inventory["ucs_free_blades"]["hosts"] == [
        "ucs-a.example.com_chassis2_blade3"
    ]
    assert inventory["ucs_unusable_blades"]["hosts"] == [
        "ucs-a.example.com_chassis2_blade4"
    ]
    assert "physical" not in inventory


def test_unified_api_credentials_win_over_controller_ones():
    environ = {
        "CREDENTIAL_UCS_A_BASE_URL": "https://ucs-a.example.com",
        "CREDENTIAL_UCS_A_USERNAME": "reader",
        "CREDENTIAL_UCS_A_PASSWORD": "secret",
        "UCS_A_BASE_URL": "https://other.example.com",
        "UCS_A_USERNAME": "reader",
        "UCS_A_PASSWORD": "secret",
        "UCS_B_BASE_URL": "https://ucs-b.example.com",
        "UCS_B_USERNAME": "reader",
    }
    assert ucs.managers_from_env(environ) == [
        ("https://ucs-a.example.com", "reader", "secret")
    ]


def test_list_prints_the_inventory(capsys):
    environ = {
        "UCS_A_BASE_URL": "https://ucs-a.example.com",
        "UCS_A_USERNAME": "reader",
        "UCS_A_PASSWORD": "secret",
        "SOURCE_CONFIG": json.dumps({"domains": "example.com", "insecure": "true"}),
    }
    assert ucs.main(["--list"], environ, FakeClient) == 0
    inventory = json.loads(capsys.readouterr().out)
    assert inventory["ucs_blades"]["hosts"] == ["db01", "web01.example.com"]


def test_no_manager_answering_is_a_failure_not_an_empty_inventory(capsys):
    environ = {
        "UCS_A_BASE_URL": "https://ucs-a.example.com",
        "UCS_A_USERNAME": "reader",
        "UCS_A_PASSWORD": "secret",
    }
    assert ucs.main(["--list"], environ, DownClient) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "no UCS Manager answered" in captured.err


def test_a_refused_login_raises():
    class Refusing(ucs.UCSClient):
        def _post(self, body):
            return ET.fromstring(
                '<aaaLogin response="yes" errorDescr="Authentication failed"/>'
            )

    with pytest.raises(RuntimeError, match="Authentication failed"):
        Refusing("ucs-a.example.com", "reader", "wrong")


def test_settings_accept_strings_as_unified_api_passes_them():
    config = {
        "domains": "a.example.com, b.example.com",
        "insecure": "true",
        "timeout_seconds": "5",
    }
    assert ucs._settings({"SOURCE_CONFIG": json.dumps(config)}) == {
        "domains": ["a.example.com", "b.example.com"],
        "insecure": True,
        "timeout_seconds": 5,
    }
