import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "infinidat_infinibox", HERE.parent / "infinidat_infinibox.py"
)
ibox = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ibox)

NAMING = {"host_prefix": "igrp-", "dotted_segments": 3, "domain": "example.com"}


class FakeClient(ibox.InfiniBoxClient):
    """Serves the REST answers from the fixtures, page by page."""

    def __init__(self, *args, **kwargs):
        super().__init__("array01.example.com", "reader", "secret")
        self.requests = []

    def _get(self, endpoint, params):
        self.requests.append((endpoint, params["page"]))
        name = endpoint if endpoint != "volumes" else f"volumes-{params['page']}"
        return json.loads((HERE / "fixtures" / f"{name}.json").read_text())


class DownClient:
    def __init__(self, *args, **kwargs):
        raise OSError("connection refused")


ENVIRON = {
    "CREDENTIAL_USERNAME": "reader",
    "CREDENTIAL_PASSWORD": "secret",
    "SOURCE_CONFIG": json.dumps(
        {
            "url": "https://array01.example.com",
            "insecure": "true",
            "host_prefix": "igrp-",
            "dotted_segments": "3",
            "domain": "example.com",
        }
    ),
}


def test_every_volume_page_is_read():
    client = FakeClient()
    ibox.build_inventory(client, "array01.example.com", NAMING)
    assert ("volumes", 1) in client.requests and ("volumes", 2) in client.requests


def test_hosts_carry_their_volumes_by_lun_with_the_wwid():
    inventory = ibox.build_inventory(FakeClient(), "array01.example.com", NAMING)
    web = inventory["_meta"]["hostvars"]["web01.prod.shop.east.example.com"][
        "infinibox"
    ]
    assert web["system"] == "array01.example.com"
    assert web["host"] == "igrp-web01-prod-shop-east"
    assert [v["lun"] for v in web["volumes"]] == [1, 2]
    boot = web["volumes"][0]
    assert boot["wwid"] == "36742b0f000000000000000000000001"
    assert (
        boot["size_gb"] == 100
        and boot["pool"] == "pool-a"
        and boot["provtype"] == "THIN"
    )


def test_short_names_may_contain_dashes():
    inventory = ibox.build_inventory(FakeClient(), "array01.example.com", NAMING)
    assert "db-01.test.shop.west.example.com" in inventory["_meta"]["hostvars"]


def test_hosts_without_volumes_or_with_other_names_are_left_out():
    inventory = ibox.build_inventory(FakeClient(), "array01.example.com", NAMING)
    assert inventory["iboxarray_array01_example_com"] == {
        "hosts": [
            "db-01.test.shop.west.example.com",
            "web01.prod.shop.east.example.com",
        ],
        "vars": {"infinibox_array": "array01.example.com"},
    }


def test_without_naming_settings_host_objects_keep_their_names():
    inventory = ibox.build_inventory(FakeClient(), "array01.example.com")
    hosts = inventory["iboxarray_array01_example_com"]["hosts"]
    assert hosts == [
        "igrp-db-01-test-shop-west",
        "igrp-web01-prod-shop-east",
        "legacy-host",
    ]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("igrp-web01-prod-shop-east", "web01.prod.shop.east.example.com"),
        ("igrp-prod-shop-east", ""),
        ("other-web01-prod-shop-east", ""),
    ],
)
def test_host_name(name, expected):
    assert ibox.host_name(name, **NAMING) == expected


def test_list_prints_the_inventory(capsys):
    assert ibox.main(["--list"], ENVIRON, FakeClient) == 0
    inventory = json.loads(capsys.readouterr().out)
    assert inventory["all"]["children"] == [
        "ungrouped",
        "iboxarray_array01_example_com",
    ]


def test_host_prints_one_hosts_variables(capsys):
    assert (
        ibox.main(["--host", "db-01.test.shop.west.example.com"], ENVIRON, FakeClient)
        == 0
    )
    assert (
        json.loads(capsys.readouterr().out)["infinibox"]["host"]
        == "igrp-db-01-test-shop-west"
    )


def test_missing_settings_are_named(capsys):
    assert ibox.main(["--list"], {}, FakeClient) == 1
    err = capsys.readouterr().err
    assert "no array address" in err and "no user" in err and "no password" in err


def test_an_unreachable_array_fails(capsys):
    assert ibox.main(["--list"], ENVIRON, DownClient) == 1
    assert "connection refused" in capsys.readouterr().err
