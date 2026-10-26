import importlib.util
import json
import re
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("centreon", HERE.parent / "centreon.py")
centreon = importlib.util.module_from_spec(spec)
spec.loader.exec_module(centreon)

DOMAINS = ".prod.east.example.com,.prod.west.example.com"
DATACENTER = r"(?:^|\.)(east|west)(?:\.|$)"
CREDENTIALS = {
    "CREDENTIAL_BASE_URL": "https://centreon.example.com",
    "CREDENTIAL_USERNAME": "reader",
    "CREDENTIAL_PASSWORD": "secret",
}


def load(name):
    return json.loads((HERE / "fixtures" / name).read_text())


class FakeClient:
    """Answers the collection calls from the fixtures, recording each request."""

    def __init__(self, *args, workers=8, **kwargs):
        self.workers = workers
        self.calls = []

    def get_all(self, path, params=None, label="", page_size=1000):
        params = params or {}
        self.calls.append((path, params))
        if path == "/monitoring/hostgroups":
            return load("hostgroups.json")
        if path == "/monitoring/servicegroups":
            return load("servicegroups.json")
        if path == "/configuration/services":
            return load("configured_services.json")
        if "hostgroup_names" in params:
            return load("hostgroup_members.json")[
                json.loads(params["hostgroup_names"])[0]
            ]
        if "servicegroup_names" in params:
            return load("servicegroup_members.json")[
                json.loads(params["servicegroup_names"])[0]
            ]
        kind = json.loads(params["types"])[0]
        return load("hosts.json" if kind == "host" else "services.json")


def environ(**config):
    settings = {"domains": DOMAINS, "datacenter_pattern": DATACENTER, **config}
    return {**CREDENTIALS, "SOURCE_CONFIG": json.dumps(settings)}


def run(capsys, *argv, **config):
    code = centreon.main(list(argv), environ(**config), FakeClient)
    return code, capsys.readouterr()


def test_short_names_get_their_domain_back():
    matchers, patterns = centreon.build_matchers([".prod.east.example.com"])
    assert patterns == [
        ".prod.east.example.com",
        ".prod.east.example",
        ".prod.east",
        ".prod",
    ]
    assert (
        centreon.match("web01.prod.east", matchers)[0] == "web01.prod.east.example.com"
    )
    assert (
        centreon.match("web01.prod.east.example.com", matchers)[0]
        == "web01.prod.east.example.com"
    )
    assert centreon.match("web01.lab.example.org", matchers) == (None, "")


def test_list_groups_hosts(capsys):
    code, out = run(capsys, "--list")
    inventory = json.loads(out.out)
    assert code == 0
    assert inventory["centreon"]["hosts"] == [
        "db01.prod.west.example.com",
        "web01.prod.east.example.com",
    ]
    assert inventory["centreon_dc_east"]["hosts"] == ["web01.prod.east.example.com"]
    assert inventory["centreon_hostgroup_linux_servers"]["hosts"] == [
        "db01.prod.west.example.com",
        "web01.prod.east.example.com",
    ]
    assert "centreon_hostgroup_empty_group" not in inventory
    assert inventory["centreon_servicegroup_web_checks"]["hosts"] == [
        "web01.prod.east.example.com"
    ]
    assert inventory["all"]["children"] == sorted(
        k for k in inventory if k not in ("_meta", "all")
    )


def test_services_only_when_asked(capsys):
    web = json.loads(run(capsys, "--list")[1].out)["_meta"]["hostvars"][
        "web01.prod.east.example.com"
    ]
    assert web["centreon"]["services"] == [] and web["centreon"]["servicegroups"] == [
        "Web-Checks"
    ]
    inventory = json.loads(run(capsys, "--list", include_services="true")[1].out)
    web = inventory["_meta"]["hostvars"]["web01.prod.east.example.com"]["centreon"]
    assert [s["name"] for s in web["services"]] == ["HTTP", "Ping"]
    assert web["services"][0]["servicegroups"] == ["Web-Checks"]
    assert web["disabled_services"] == [{"service_id": 301, "name": "Backup"}]
    assert inventory["centreon_disabled_services"]["hosts"] == [
        "web01.prod.east.example.com"
    ]


def test_no_datacenter_groups_without_a_pattern(capsys):
    code = centreon.main(
        ["--list"],
        {**CREDENTIALS, "SOURCE_CONFIG": json.dumps({"domains": DOMAINS})},
        FakeClient,
    )
    inventory = json.loads(capsys.readouterr().out)
    assert code == 0 and not [k for k in inventory if k.startswith("centreon_dc_")]


def test_host_narrows_every_request_to_its_names(capsys):
    client = FakeClient()
    inventory = centreon.build_inventory(
        client,
        DOMAINS.split(","),
        re.compile(DATACENTER),
        only=["web01.prod.east.example.com"],
    )
    assert list(inventory["_meta"]["hostvars"]) == ["web01.prod.east.example.com"]
    host_search = json.loads(client.calls[0][1]["search"])
    assert {"h.name": {"$lk": "%web01.prod.east"}} in host_search["$or"]
    code, out = run(capsys, "--host", "web01.prod.east.example.com")
    assert code == 0 and json.loads(out.out)["centreon"]["host_id"] == 101


def test_a_host_outside_the_domains_is_empty_not_an_error(capsys):
    code, out = run(capsys, "--host-with-groups", "web01.lab.example.org")
    assert code == 0 and json.loads(out.out) == {"_meta": {"hostvars": {}}}


def test_no_domains_is_refused(capsys):
    code = centreon.main(["--list"], {**CREDENTIALS, "SOURCE_CONFIG": "{}"}, FakeClient)
    assert code == 1 and "domains is not set" in capsys.readouterr().err


@pytest.mark.parametrize("pattern", ["(", "east"])
def test_a_bad_datacenter_pattern_is_refused(capsys, pattern):
    code, out = run(capsys, "--list", datacenter_pattern=pattern)
    assert code == 1 and "datacenter_pattern" in out.err


def test_an_empty_estate_is_a_failure(capsys):
    class Empty(FakeClient):
        def get_all(self, path, params=None, label="", page_size=1000):
            return []

    code = centreon.main(["--list"], environ(), Empty)
    captured = capsys.readouterr()
    assert code == 1 and captured.out == "" and "no host matched" in captured.err
