import importlib.util
from pathlib import Path

import pytest

FOLDER = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("inventory", FOLDER / "inventory.py")
sdk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sdk)


def test_settings_read_source_config():
    assert sdk.settings({"SOURCE_CONFIG": '{"a": "1"}'}) == {"a": "1"}
    assert sdk.settings({}) == {}


@pytest.mark.parametrize("raw", ["not json", "[1]"])
def test_settings_refuse_anything_but_an_object(raw):
    with pytest.raises(sdk.ConnectorError):
        sdk.settings({"SOURCE_CONFIG": raw})


def test_values_read_from_strings_or_json_types():
    assert sdk.as_list("a.example.com, b.example.com") == [
        "a.example.com",
        "b.example.com",
    ]
    assert sdk.as_list(["x"]) == ["x"]
    assert sdk.as_list(None, ["d"]) == ["d"]
    assert sdk.as_bool("TRUE") and sdk.as_bool(True) and not sdk.as_bool("no")
    assert sdk.as_bool(None, True)
    assert sdk.as_int("5", 1) == 5 and sdk.as_int("", 1) == 1
    with pytest.raises(sdk.ConnectorError):
        sdk.as_int("five", 1)


def test_credential_sets_prefer_unified_api_and_need_every_required_field():
    environ = {
        "CREDENTIAL_SYS_A_URL": "https://a.example.com",
        "CREDENTIAL_SYS_A_USERNAME": "reader",
        "CREDENTIAL_SYS_A_PASSWORD": "secret",
        "SYS_A_URL": "https://other.example.com",
        "SYS_A_USERNAME": "reader",
        "SYS_A_PASSWORD": "secret",
        "SYS_B_URL": "https://b.example.com",
        "SYS_B_USERNAME": "reader",
        "SYS_C_URL": "https://c.example.com",
        "SYS_C_USERNAME": "reader",
        "SYS_C_PASSWORD": "secret",
        "SYS_C_TOKEN": "t",
    }
    sets = sdk.credential_sets(
        "SYS", ("URL", "USERNAME", "PASSWORD"), ("TOKEN",), environ
    )
    assert [(s["tag"], s["url"], s["token"]) for s in sets] == [
        ("A", "https://a.example.com", ""),
        ("C", "https://c.example.com", "t"),
    ]
