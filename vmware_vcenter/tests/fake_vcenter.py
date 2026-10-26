"""A vCenter faked from fixtures/vcenter.json, answering the PropertyCollector."""

import json
from pathlib import Path

from pyVmomi import vim

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "vcenter.json"
PAGE = 2
VM_PATHS_SCAN = ["name", "guest.hostName"]


class Prop:
    def __init__(self, name, val):
        self.name, self.val = name, val


class ObjectContent:
    def __init__(self, obj, props):
        self.obj = obj
        self.propSet = [Prop(k, v) for k, v in props.items()]


class Result:
    def __init__(self, objects, token):
        self.objects, self.token = objects, token


class View(vim.view.ContainerView):
    destroyed = 0

    def Destroy(self):
        View.destroyed += 1


def _disk(d):
    backing = vim.vm.device.VirtualDisk.FlatVer2BackingInfo(
        fileName=d["file"], thinProvisioned=d["thin"], diskMode="persistent"
    )
    disk = vim.vm.device.VirtualDisk(
        key=2000,
        deviceInfo=vim.Description(label=d["label"], summary=""),
        backing=backing,
    )
    if "bytes" in d:
        disk.capacityInBytes = d["bytes"]
    else:
        disk.capacityInKB = d["kb"]
    return disk


def _nic(n):
    addresses = [
        vim.net.IpConfigInfo.IpAddress(ipAddress=a, prefixLength=24) for a in n["ipv4s"]
    ]
    return vim.vm.GuestInfo.NicInfo(
        macAddress=n["mac"],
        network=n["network"],
        connected=True,
        deviceConfigId=4000,
        ipConfig=vim.net.IpConfigInfo(ipAddress=addresses),
    )


class FakeContent:
    def __init__(self):
        data = json.loads(FIXTURE.read_text())
        self.datacenters = {
            d["id"]: type(
                "Datacenter", (vim.Datacenter,), {"name": d["name"], "parent": None}
            )(d["id"])
            for d in data["datacenters"]
        }
        self.clusters, self.cluster_props = {}, {}
        for c in data["clusters"]:
            folder = type(
                "Folder", (vim.Folder,), {"parent": self.datacenters[c["datacenter"]]}
            )(f"group-h{c['id']}")
            obj = vim.ClusterComputeResource(c["id"])
            self.clusters[c["id"]] = obj
            self.cluster_props[obj] = {"name": c["name"], "parent": folder}
        self.host_props = {}
        self.hosts = {}
        for h in data["hosts"]:
            obj = vim.HostSystem(h["id"])
            self.hosts[h["id"]] = obj
            parent = (
                self.clusters[h["cluster"]]
                if h["cluster"]
                else vim.ComputeResource(f"domain-s{h['id']}")
            )
            self.host_props[obj] = {"name": h["name"], "parent": parent}
        self.vm_props = {}
        for v in data["vms"]:
            obj = vim.VirtualMachine(v["id"])
            self.vm_props[obj] = {
                "name": v["name"],
                "config.uuid": v["uuid"],
                "config.guestFullName": v["guest_fullname"],
                "runtime.powerState": getattr(vim.VirtualMachinePowerState, v["power"]),
                "runtime.host": self.hosts[v["host"]],
                "guest.ipAddress": v["ip"],
                "guest.net": [_nic(n) for n in v["nics"]],
                "summary.config.numCpu": v["cpu"],
                "summary.config.memorySizeMB": v["memory_mb"],
                "config.hardware.device": [_disk(d) for d in v["disks"]],
                "guest.hostName": v["guest_hostname"],
                "summary.guest.hostName": v["guest_hostname"],
            }
        self.rootFolder = vim.Folder("group-d1")
        self.viewManager = self
        self.propertyCollector = self
        self.searchIndex = self
        self.pages = {}
        self.continued = 0

    def CreateContainerView(self, container, types, recursive):
        return View("session[1]view-1")

    def FindByDnsName(self, dnsName, vmSearch):
        for obj, props in self.vm_props.items():
            if (props["guest.hostName"] or "").lower() == dnsName.lower():
                return obj
        return None

    def RetrievePropertiesEx(self, specs, options):
        spec = specs[0]
        prop = spec.propSet[0]
        objs = [o.obj for o in spec.objectSet]
        if prop.type is vim.VirtualMachine:
            source = self.vm_props
            if isinstance(objs[0], View):
                objs = list(source)
        elif prop.type is vim.HostSystem:
            source = self.host_props
        else:
            source = self.cluster_props
        rows = [ObjectContent(o, {k: source[o][k] for k in prop.pathSet}) for o in objs]
        return self._page(rows, 0)

    def ContinueRetrievePropertiesEx(self, token):
        self.continued += 1
        rows, start = self.pages.pop(token)
        return self._page(rows, start)

    def _page(self, rows, start):
        end = start + PAGE
        token = None
        if end < len(rows):
            token = f"t{len(self.pages) + self.continued}-{end}"
            self.pages[token] = (rows, end)
        return Result(rows[start:end], token)


class FakeServiceInstance:
    def __init__(self):
        self.content = FakeContent()

    def RetrieveContent(self):
        return self.content
