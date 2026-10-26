import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "pure_flasharray", HERE.parent / "pure_flasharray.py"
)
pure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pure)


def fixture(path):
    """Map an API path to its fixture: /hgroup/x/volume -> hgroup-x-volume.json."""
    name = path.strip("/").replace("/", "-") + ".json"
    file = HERE / "fixtures" / name
    if not file.exists():
        raise pure.NotFound(f"HTTP 404 for GET {path}")
    return json.loads(file.read_text())


class FakeClient(pure.FlashArrayClient):
    """The real client, answering from the fixtures instead of the network."""

    def _call(self, method, path, body=None):
        if path == "/auth/apitoken":
            return {"api_token": "t"} if body["password"] == "good" else {}
        if method != "GET":
            return None
        return fixture(path)


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    monkeypatch.setattr(
        pure.socket,
        "getfqdn",
        lambda name: "web02.example.org" if name == "web02" else name,
    )


def client():
    return FakeClient("array.example.com", "reader", "good")


def test_hosts_and_host_groups_become_groups_under_the_array():
    inventory = pure.build_inventory(client(), "array.example.com")
    array = inventory["purearray_array.example.com"]
    assert array["vars"] == {"pure_array": "array.example.com"}
    assert array["children"] == ["purehostgroup_cluster-a", "purehostgroup_empty-group"]
    assert array["hosts"] == ["web01", "web02", "db01.example.net"]
    assert inventory["purehostgroup_cluster-a"]["hosts"] == ["web01", "web02", "web03"]
    assert inventory["all"]["children"][:2] == [
        "ungrouped",
        "purearray_array.example.com",
    ]


def test_shared_luns_split_into_volume_groups_and_standalone_volumes():
    inventory = pure.build_inventory(client(), "array.example.com")
    shared = inventory["purehostgroup_cluster-a"]["vars"]["pure_luns_shared"]
    assert inventory["purehostgroup_cluster-a"]["vars"]["pure_lun_count"] == 3
    assert shared["volumes"] == [
        {
            "volume": "scratch",
            "lun_id": 10,
            "serial": "0A1B2C3D4E5F000000000004",
            "multipath_wwid": "3624a93700a1b2c3d4e5f000000000004",
            "size_bytes": 512,
            "size_human": "512 B",
        }
    ]
    data01 = shared["volume_groups"]["shared-vg"]["volumes"][0]
    assert data01["volume"] == "data01" and data01["full_volume"] == "shared-vg/data01"
    assert data01["size_human"] == "10.00 TiB"
    assert [v["volume_group"] for v in shared["volumes_all"]] == [
        "shared-vg",
        "shared-vg",
        None,
    ]


def test_host_variables_carry_individual_luns_and_initiators():
    hostvars = pure.build_inventory(client(), "array.example.com")["_meta"]["hostvars"]
    web01 = hostvars["web01"]
    assert web01["pure_hgroups"] == ["cluster-a"] and web01["pure_groups"] == [
        "purehostgroup_cluster-a"
    ]
    assert web01["pure_lun_count"] == 1 and web01["pure_wwns"] == [
        "2100000000000001",
        "2100000000000002",
    ]
    assert hostvars["db01.example.net"]["pure_luns_individual"]["volumes"] == []


def test_a_host_group_member_without_a_host_object_still_has_variables():
    hostvars = pure.build_inventory(client(), "array.example.com")["_meta"]["hostvars"]
    assert hostvars["web03"]["pure_hgroups"] == ["cluster-a"]
    assert hostvars["web03"]["pure_lun_count"] == 0


def test_resolve_fqdn_uses_dns_then_the_domain():
    inventory = pure.build_inventory(
        client(), "array.example.com", resolve=True, domain="example.com"
    )
    assert inventory["purehostgroup_cluster-a"]["hosts"] == [
        "web01.example.com",
        "web02.example.org",
        "web03.example.com",
    ]
    assert (
        inventory["_meta"]["hostvars"]["web02.example.org"]["pure_host_short"]
        == "web02"
    )


def test_host_finds_a_host_by_its_resolved_name():
    found = pure.host_vars(
        client(), "array.example.com", "web02.example.org", resolve=True
    )
    assert found["pure_host_short"] == "web02" and found["pure_iqns"] == [
        "eui.02004567A425678D"
    ]


def test_a_refused_sign_in_raises():
    with pytest.raises(RuntimeError, match="API token"):
        FakeClient("array.example.com", "reader", "wrong")


ENV = {
    "PURE_A_BASE_URL": "https://array.example.com",
    "PURE_A_USERNAME": "reader",
    "PURE_A_PASSWORD": "good",
}


def test_list_prints_the_inventory(capsys):
    environ = {
        **ENV,
        "SOURCE_CONFIG": json.dumps({"resolve_fqdn": "false", "workers": "4"}),
    }
    assert pure.main(["--list"], environ, FakeClient) == 0
    assert "purearray_array.example.com" in json.loads(capsys.readouterr().out)


def test_two_arrays_in_one_source_are_refused(capsys):
    environ = {**ENV, **{k.replace("_A_", "_B_"): v for k, v in ENV.items()}}
    assert pure.main(["--list"], environ, FakeClient) == 1
    assert "exactly one array" in capsys.readouterr().err


def test_a_refused_sign_in_exits_1_with_nothing_on_stdout(capsys):
    environ = {**ENV, "PURE_A_PASSWORD": "wrong"}
    assert pure.main(["--list"], environ, FakeClient) == 1
    assert capsys.readouterr().out == ""
