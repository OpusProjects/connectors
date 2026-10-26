import importlib.util
import json
import socket
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vc = load("vmware_vcenter", HERE.parent / "vmware_vcenter.py")
fake = load("fake_vcenter", HERE / "fake_vcenter.py")

ENVIRON = {
    "CREDENTIAL_VCENTER_A_BASE_URL": "https://vc-a.example.com",
    "CREDENTIAL_VCENTER_A_USERNAME": "reader@vsphere.local",
    "CREDENTIAL_VCENTER_A_PASSWORD": "secret",
}


class Session:
    """Hands out fake service instances and remembers what was asked."""

    def __init__(self):
        self.connected, self.disconnected = [], 0
        self.si = fake.FakeServiceInstance()

    def connect(self, host, user, password, **kwargs):
        self.connected.append((host, kwargs))
        return self.si

    def disconnect(self, si):
        self.disconnected += 1


def refuse(host, *args, **kwargs):
    raise vc.ConnectorError(f"cannot connect to vCenter {host}: refused")


@pytest.fixture(autouse=True)
def dns(monkeypatch):
    """No real lookups: only the names in this set resolve."""
    known = set()

    def getaddrinfo(name, *args, **kwargs):
        if name in known:
            return [(socket.AF_INET, 0, 0, "", ("192.0.2.200", 0))]
        raise socket.gaierror(name)

    def gethostbyaddr(ip):
        raise socket.herror(ip)

    monkeypatch.setattr(vc.socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(vc.socket, "gethostbyaddr", gethostbyaddr)
    monkeypatch.setattr(vc.socket, "getfqdn", lambda name: name)
    monkeypatch.setattr(vc.socket, "setdefaulttimeout", lambda timeout: None)
    return known


def run(argv, capsys, environ=ENVIRON, session=None):
    session = session or Session()
    code = vc.main(argv, environ, session.connect, session.disconnect)
    out = capsys.readouterr()
    return code, (json.loads(out.out) if out.out else None), out.err, session


def test_the_hierarchy_runs_from_vcenter_to_cluster(capsys):
    code, inventory, _, session = run(["--list", "--no-resolve-fqdn"], capsys)
    assert code == 0 and session.disconnected == 1
    assert inventory["all"] == {"children": ["virtual"]}
    assert inventory["virtualization_type_vmware"] == {
        "children": ["vmware_vcenter_vc_a_example_com"],
        "vars": {"virtualization_type": "vmware"},
    }
    vcenter = inventory["vmware_vcenter_vc_a_example_com"]
    assert vcenter["children"] == ["vmware_datacenter_East_main"]
    assert vcenter["hosts"] == ["lab01"]
    datacenter = inventory["vmware_datacenter_East_main"]
    assert datacenter["vars"] == {
        "vmware_datacenter": "East main",
        "vmware_datacenter_original": "East (main)",
    }
    assert datacenter["children"] == [
        "vmware_cluster_compute_01",
        "vmware_cluster_edge_02",
    ]
    assert inventory["vmware_cluster_compute_01"] == {
        "hosts": ["web01.example.com", "db01"],
        "vars": {"vmware_cluster": "compute-01"},
    }
    assert inventory["vmware_cluster_edge_02"]["vars"] == {"vmware_cluster": "edge:02"}
    assert inventory["vmware_all_vms"]["hosts"] == [
        "web01.example.com",
        "db01",
        "app01.example.net",
        "lab01",
    ]


def test_every_page_of_vms_is_read(capsys):
    _, _, _, session = run(["--list", "--no-resolve-fqdn"], capsys)
    assert session.si.content.continued >= 2


def test_host_variables(capsys):
    _, inventory, _, _ = run(["--list", "--no-resolve-fqdn"], capsys)
    web = inventory["_meta"]["hostvars"]["web01.example.com"]
    assert web["vmware_memory"] == "8G" and web["vmware_memory_mb"] == 8192
    assert web["vmware_power_state"] == "poweredOn" and web["vmware_moid"] == "vm-1"
    assert web["vmware_ansible_host"] == "web01.example.com"
    assert web["vmware_vcenter"] == "vc-a.example.com"
    assert web["vmware_nics"] == [
        {"mac": "00:50:56:00:00:01", "network": "frontend", "ipv4s": ["192.0.2.11"]}
    ]
    assert web["vmware_disks"][1] == {
        "label": "Hard disk 2",
        "capacity_bytes": 1073741824,
        "thin": False,
        "backing_file": "[datastore1] web01/web01_1.vmdk",
    }
    db = inventory["_meta"]["hostvars"]["db01"]
    assert db["vmware_ansible_host"] == "192.0.2.21"
    assert inventory["_meta"]["hostvars"]["app01.example.net"]["vmware_memory"] == "3G"


def test_short_guest_names_take_the_first_domain_that_resolves(capsys, dns):
    dns.add("db01.example.org")
    environ = {
        **ENVIRON,
        "SOURCE_CONFIG": json.dumps({"domains": "example.net, example.org"}),
    }
    _, inventory, _, _ = run(["--list"], capsys, environ)
    assert "db01.example.org" in inventory["vmware_cluster_compute_01"]["hosts"]


def test_default_domain_is_tried_first(capsys, dns):
    dns.add("db01.example.net")
    dns.add("db01.example.org")
    environ = {
        **ENVIRON,
        "SOURCE_CONFIG": json.dumps(
            {"domains": "example.org", "default_domain": "example.net"}
        ),
    }
    _, inventory, _, _ = run(["--list"], capsys, environ)
    assert "db01.example.net" in inventory["_meta"]["hostvars"]


def test_without_guest_dns_hosts_are_named_after_the_vm(capsys):
    _, inventory, _, _ = run(
        ["--list", "--no-prefer-guest-dns", "--no-resolve-fqdn"], capsys
    )
    assert inventory["vmware_cluster_compute_01"]["hosts"] == ["web01", "db01"]


def test_host_with_groups_keeps_the_full_runs_groups(capsys):
    code, inventory, _, _ = run(
        ["--host-with-groups", "db01", "--no-resolve-fqdn"], capsys
    )
    assert code == 0
    assert inventory["vmware_all_vms"]["hosts"] == ["db01"]
    assert inventory["vmware_cluster_compute_01"]["hosts"] == ["db01"]
    assert inventory["vmware_datacenter_East_main"]["children"] == [
        "vmware_cluster_compute_01"
    ]


def test_host_with_groups_finds_a_vm_by_its_name_when_dns_does_not(capsys):
    code, inventory, _, _ = run(
        ["--host-with-groups", "LAB01", "--no-resolve-fqdn"], capsys
    )
    assert code == 0 and inventory["vmware_all_vms"]["hosts"] == ["lab01"]


def test_host_with_groups_finding_nothing_fails(capsys):
    code, inventory, err, _ = run(
        ["--host-with-groups", "nowhere", "--no-resolve-fqdn"], capsys
    )
    assert code == 1 and inventory is None and "nowhere" in err


def test_host_prints_only_variables(capsys):
    _, hostvars, _, _ = run(["--host", "lab01", "--no-resolve-fqdn"], capsys)
    assert hostvars["vmware_vm_name"] == "lab01"
    assert hostvars["vmware_ansible_host"] == "198.51.100.4"


def test_settings_reach_the_connection(capsys):
    environ = {
        **ENVIRON,
        "SOURCE_CONFIG": json.dumps(
            {"insecure": "true", "port": "8443", "timeout_seconds": "5"}
        ),
    }
    _, _, _, session = run(["--list", "--no-resolve-fqdn"], capsys, environ)
    assert session.connected == [
        ("vc-a.example.com", {"port": 8443, "timeout": 5.0, "insecure": True})
    ]


def test_a_vcenter_failing_fails_the_run(capsys):
    session = Session()
    session.connect = refuse
    code, inventory, err, _ = run(["--list"], capsys, session=session)
    assert code == 1 and inventory is None and "refused" in err


def test_vcenters_are_polled_once_each():
    environ = {
        **ENVIRON,
        "VCENTER_A_BASE_URL": "vc-a.example.com",
        "VCENTER_A_USERNAME": "reader",
        "VCENTER_A_PASSWORD": "secret",
        "VCENTER_B_BASE_URL": "https://vc-b.example.com:8443/sdk",
        "VCENTER_B_USERNAME": "reader",
        "VCENTER_B_PASSWORD": "secret",
        "VMWARE_HOST": "vc-c.example.com",
        "VMWARE_USER": "admin",
        "VMWARE_PASSWORD": "secret",
    }
    assert vc.vcenters_from_env(environ) == [
        ("vc-c.example.com", "admin", "secret"),
        ("vc-a.example.com", "reader@vsphere.local", "secret"),
        ("vc-b.example.com:8443", "reader", "secret"),
    ]


def test_no_credentials_is_a_failure(capsys):
    code, _, err, _ = run(["--list"], capsys, environ={})
    assert code == 1 and "no vCenter credentials" in err
