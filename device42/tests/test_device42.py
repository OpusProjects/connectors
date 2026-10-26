import importlib.util
import json
import socket
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
spec = importlib.util.spec_from_file_location("device42", HERE.parent / "device42.py")
d42 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d42)

CREDENTIALS = {
    "CREDENTIAL_BASE_URL": "https://d42.example.com",
    "CREDENTIAL_USERNAME": "reader",
    "CREDENTIAL_PASSWORD": "secret",
}


def load(name):
    return json.loads((FIXTURES / name).read_text())


class FakeDevice42:
    """Answers DOQL and REST calls from the fixtures."""

    def __init__(self, config):
        self.queries = []

    def doql(self, sql):
        self.queries.append(sql)
        if "LIMIT 1" in sql:
            return load("doql_custom_fields_probe.json")
        if "SELECT cf.device_fk" in sql:
            return load("doql_custom_fields.json")
        if "view_customer_v1 AS c" in sql and "view_device_v2" not in sql:
            return load("doql_customers.json")
        return load("doql_devices.json")

    def device(self, name):
        return load("rest_db01.json") if name == "db01.example.com" else {}


@pytest.fixture(autouse=True)
def network(monkeypatch):
    def lookup(name, *args, **kwargs):
        if name == "web01.example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.10", 0))]
        raise socket.gaierror(name)

    def connect(address, timeout=None):
        if address[0] == "192.0.2.20":
            return open(__file__)  # anything usable as a context manager
        raise OSError("unreachable")

    monkeypatch.setattr(d42.socket, "getaddrinfo", lookup)
    monkeypatch.setattr(d42.socket, "create_connection", connect)


def environ(**config):
    return {
        **CREDENTIALS,
        "SOURCE_CONFIG": json.dumps(
            {"config_dir": str(FIXTURES / "filters"), **config}
        ),
    }


def run(capsys, *argv, **config):
    code = d42.main(list(argv), environ(**config), FakeDevice42)
    return code, capsys.readouterr()


def test_list_applies_the_filters(capsys):
    code, out = run(capsys, "--list")
    inventory = json.loads(out.out)
    assert code == 0
    assert inventory["all"]["hosts"] == [
        "app01.west.example.net",
        "db01.example.com",
        "web01.example.com",
    ]


def test_groups_come_from_fields_custom_fields_networks_and_domains(capsys):
    inventory = json.loads(run(capsys, "--list")[1].out)
    assert inventory["service_level_production"]["hosts"] == [
        "db01.example.com",
        "web01.example.com",
    ]
    assert inventory["layer_data_store"] == {
        "hosts": ["db01.example.com"],
        "vars": {"layer": "DataStore"},
    }
    assert inventory["datacenter_east"]["hosts"] == [
        "db01.example.com",
        "web01.example.com",
    ]
    assert inventory["datacenter_west"] == {
        "hosts": ["app01.west.example.net", "web01.example.com"],
        "vars": {"d42_datacenter": "west"},
    }
    assert inventory["d42_inventory_source_lab"]["vars"] == {
        "d42_inventory_source_name": "d42_inventory_source_lab"
    }


def test_host_variables_and_address_choice(capsys):
    hostvars = json.loads(run(capsys, "--list")[1].out)["_meta"]["hostvars"]
    web = hostvars["web01.example.com"]
    assert (
        web["nslookup_discovered_ip"] == "192.0.2.10"
        and web["ansible_host"] == "192.0.2.10"
    )
    assert (
        web["business_unit"] == "Retail"
        and web["customer_email"] == "team-a@example.com"
    )
    db = hostvars["db01.example.com"]
    assert db["ansible_host"] == "192.0.2.20"
    assert db["host_all_ips_connectivity"] == {
        "reachable": ["192.0.2.20"],
        "unreachable": ["198.51.100.5"],
    }
    app = hostvars["app01.west.example.net"]
    assert app["ansible_host"] == "203.0.113.8"
    assert app["ansible_host_source"] == "config_management_ip"


def test_the_probe_can_be_turned_off(capsys):
    hostvars = json.loads(run(capsys, "--list", ssh_probe="false")[1].out)["_meta"][
        "hostvars"
    ]
    assert "host_all_ips_connectivity" not in hostvars["db01.example.com"]
    assert hostvars["db01.example.com"]["ansible_host"] == "192.0.2.20"


def test_host_with_groups_looks_hosts_up_one_by_one(capsys):
    code, out = run(
        capsys, "--host-with-groups", "db01.example.com, missing.example.com"
    )
    inventory = json.loads(out.out)
    assert code == 0 and inventory["all"]["hosts"] == ["db01.example.com"]
    assert inventory["device_sub_type_rackmount"]["hosts"] == ["db01.example.com"]
    assert inventory["_meta"]["hostvars"]["db01.example.com"]["customer_email"] == (
        "team-a@example.com"
    )


def test_host_gives_one_hosts_variables(capsys):
    code, out = run(capsys, "--host", "db01.example.com")
    assert code == 0 and json.loads(out.out)["customer"] == "Team A"


def test_missing_settings_and_credentials_fail_clearly(capsys):
    code = d42.main(["--list"], {**CREDENTIALS, "SOURCE_CONFIG": "{}"}, FakeDevice42)
    assert code == 1 and "config_dir" in capsys.readouterr().err
    code = d42.main(
        ["--list"], {"SOURCE_CONFIG": environ()["SOURCE_CONFIG"]}, FakeDevice42
    )
    assert code == 1 and "CREDENTIAL_BASE_URL" in capsys.readouterr().err


def test_a_failed_query_is_a_failure_not_an_empty_inventory(capsys):
    class Down(FakeDevice42):
        def doql(self, sql):
            raise OSError("connection refused")

    code = d42.main(["--list"], environ(), Down)
    captured = capsys.readouterr()
    assert code == 1 and captured.out == "" and "connection refused" in captured.err


def test_the_query_escapes_configured_values():
    cfg = d42.Config({"config_dir": str(FIXTURES / "filters")}, CREDENTIALS)
    cfg.domains = ["o'brien.example.com"]
    assert "ILIKE '%o''brien.example.com'" in d42.build_device_ip_query(cfg)


def test_the_example_filters_load():
    pytest.importorskip("yaml")
    cfg = d42.Config({"config_dir": str(HERE.parent / "filters.example")}, CREDENTIALS)
    assert cfg.inventory["inventory_source"] == "lab"
    assert cfg.avoid["os"] == ["windows", "vmware esxi"]
