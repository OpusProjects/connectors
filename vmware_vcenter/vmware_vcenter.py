#!/usr/bin/env python3
"""Ansible dynamic inventory for VMware vCenter, through the PropertyCollector.

Every virtual machine of every configured vCenter, fetched in batched
PropertyCollector calls rather than object by object, in this hierarchy:

  all > virtual > virtualization_type_vmware > vmware_vcenter_<vcenter>
      > vmware_datacenter_<datacenter> > vmware_cluster_<cluster>

plus vmware_all_vms, which holds every VM. Group variables: virtualization_type
(vmware), vmware_vcenter, vmware_datacenter, vmware_datacenter_original and
vmware_cluster. A VM on a host outside any cluster sits in its datacenter's
group, and one whose datacenter is unknown in its vCenter's group.

Host variables: vmware_uuid, vmware_moid, vmware_power_state,
vmware_guest_fullname, vmware_num_cpu, vmware_memory_mb, vmware_memory (8G),
vmware_nics (mac, network, ipv4s), vmware_disks (label, capacity_bytes, thin,
backing_file), vmware_vm_name, vmware_guest_hostname, vmware_vcenter and
vmware_ansible_host (the FQDN when the host has one, else its IPv4).

Host names: with --prefer-guest-dns (on by default) the name the guest reports
is used when it is an FQDN; with --resolve-fqdn (on by default) short names are
expanded through DNS, trying default_domain and then each of domains. The
--no-prefer-guest-dns and --no-resolve-fqdn flags turn them off, and the
PREFER_GUEST_DNS and RESOLVE_FQDN variables change the defaults.

vCenters: --vcenter or VMWARE_HOST with VMWARE_USER and VMWARE_PASSWORD; then
every set of CREDENTIAL_VCENTER_<TAG>_BASE_URL, _USERNAME and _PASSWORD as
unified-api hands them over, or VCENTER_<TAG>_... as an Ansible controller
would. A vCenter reachable under two names in the same form is polled once.
Any vCenter failing fails the run: half an inventory would read as VMs gone.

Settings, from the source's config (unified-api passes strings):
  domains          comma-separated suffixes tried, in order, on short guest names
  default_domain   a suffix tried first (also --default-domain or DEFAULT_DOMAIN)
  insecure         true to skip TLS verification (also VMWARE_INSECURE)
  port             the SDK port, default 443 (also --port)
  timeout_seconds  socket timeout, default 30 (also --timeout)
"""

import argparse
import contextlib
import json
import os
import re
import socket
import ssl
import sys
import time
from urllib.parse import urlparse

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

# The SDK, the unified_connectors package, sits at the repository root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unified_connectors import (
    ConnectorError,
    as_bool,
    as_int,
    as_list,
    credential_sets,
    settings,
)

# What a failed name lookup raises: gaierror and herror are OSErrors, and a
# label too long for IDNA encoding is a UnicodeError.
LOOKUP_FAILURES = (OSError, UnicodeError)
# What a vCenter that is down, refusing or faulting raises.
VCENTER_FAILURES = (vmodl.MethodFault, OSError)
# What one malformed VM raises while its variables are read; it is skipped.
VM_FAILURES = (AttributeError, KeyError, TypeError, ValueError)

VM_PATHS = [
    "name",
    "config.uuid",
    "config.guestFullName",
    "runtime.powerState",
    "runtime.host",
    "guest.ipAddress",
    "guest.net",
    "summary.config.numCpu",
    "summary.config.memorySizeMB",
    "config.hardware.device",
    "guest.hostName",
    "summary.guest.hostName",
]

PC = vmodl.query.PropertyCollector


def _log(debug, message):
    if debug:
        print(f"* {message}", file=sys.stderr)


def sanitize_dc_name(name):
    """Drop the parentheses vCenter allows in datacenter names."""
    if name is None:
        return None
    return name.replace("(", "").replace(")", "")


def safe_group_name(name):
    """Replace every character Ansible refuses in a group name with an underscore.

    Case is kept: group names already referenced elsewhere would change.
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", str(name or ""))


def looks_fqdn(name):
    """Tell whether a name has a dot, the cheap test for a fully qualified name."""
    return bool(name and "." in name)


def ptr_lookup(ip):
    """Return the FQDN a reverse lookup of the address gives, or None."""
    try:
        name, _, _ = socket.gethostbyaddr(ip)
    except LOOKUP_FAILURES:
        return None
    return name if looks_fqdn(name) else None


def forward_fqdn(name):
    """Return the canonical FQDN for a name, or the reverse lookup of its address."""
    try:
        infos = socket.getaddrinfo(name, None, flags=socket.AI_CANONNAME)
    except LOOKUP_FAILURES:
        return None
    for info in infos:
        if looks_fqdn(info[3]):
            return info[3]
    for info in infos:
        address = info[4][0] if isinstance(info[4], tuple) else None
        if address:
            reverse = ptr_lookup(address)
            if looks_fqdn(reverse):
                return reverse
    return None


def _resolves(name):
    try:
        socket.getaddrinfo(name, None)
    except LOOKUP_FAILURES:
        return False
    return True


def fqdn_from_short(short, default_domain):
    """Expand a short name: default_domain first, then the resolver, then getfqdn."""
    if default_domain:
        candidate = f"{short}.{default_domain}".rstrip(".")
        if _resolves(candidate):
            return candidate
    candidate = forward_fqdn(short)
    if looks_fqdn(candidate):
        return candidate
    try:
        candidate = socket.getfqdn(short)
    except LOOKUP_FAILURES:
        return None
    return candidate if looks_fqdn(candidate) else None


def choose_inventory_name(vm_name, guest_dns, ip, options):
    """Pick a VM's inventory name, an FQDN whenever one can be found.

    In order: the guest's name when it is an FQDN; the guest's short name
    expanded through DNS, or the reverse lookup of the VM's address; the VM's
    name when it is an FQDN; the reverse lookup or DNS expansion of the VM's
    name; the guest's short name with each domain appended, the first that
    resolves; and last the VM's name as it is. Every lookup needs resolve_fqdn.

    Args:
        vm_name: The VM's name in vCenter.
        guest_dns: The host name the guest reports, or None.
        ip: The VM's first IPv4 address, or None.
        options: prefer_guest_dns, resolve_fqdn, default_domain and domains.

    Returns:
        The inventory name.
    """
    prefer, resolve = options["prefer_guest_dns"], options["resolve_fqdn"]
    default_domain = options["default_domain"]
    if prefer and looks_fqdn(guest_dns):
        return guest_dns
    if prefer and guest_dns and resolve:
        name = fqdn_from_short(guest_dns, default_domain)
        if looks_fqdn(name):
            return name
        if ip:
            name = ptr_lookup(ip)
            if looks_fqdn(name):
                return name
    if looks_fqdn(vm_name):
        return vm_name
    if resolve:
        if ip:
            name = ptr_lookup(ip)
            if looks_fqdn(name):
                return name
        name = fqdn_from_short(vm_name, default_domain)
        if looks_fqdn(name):
            return name
    if prefer and guest_dns and resolve:
        domains = list(options["domains"])
        if default_domain and default_domain not in domains:
            domains.insert(0, default_domain)
        for domain in domains:
            candidate = f"{guest_dns}.{domain}".rstrip(".")
            if _resolves(candidate):
                return candidate
        return guest_dns
    return vm_name


def _retrieve(pc, filter_spec):
    """Yield every object a filter returns, following the result pages."""
    result = pc.RetrievePropertiesEx([filter_spec], PC.RetrieveOptions())
    while result:
        yield from result.objects
        if not result.token:
            return
        result = pc.ContinueRetrievePropertiesEx(result.token)


def _view_spec(view):
    """An object spec walking from a container view into every object it holds."""
    return PC.ObjectSpec(
        obj=view,
        skip=True,
        selectSet=[
            PC.TraversalSpec(
                name="tSpec", path="view", skip=False, type=vim.view.ContainerView
            )
        ],
    )


def collect_vm_props(content, vm_objs=None):
    """Fetch the VM properties in one batched query, for every VM or only vm_objs.

    Asking for given VMs uses one object spec each and no traversal, so a
    host-scoped run asks vCenter about those VMs and nothing else; the rows
    have the same shape either way.

    Returns:
        The VM rows (a dict of property path to value per VM, plus _obj and a
        few aliases) and the set of hosts those VMs run on.
    """
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        prop_spec = PC.PropertySpec(
            type=vim.VirtualMachine, all=False, pathSet=VM_PATHS
        )
        if vm_objs is not None:
            object_set = [PC.ObjectSpec(obj=vm, skip=False) for vm in vm_objs]
        else:
            object_set = [_view_spec(view)]
        filter_spec = PC.FilterSpec(objectSet=object_set, propSet=[prop_spec])
        rows, hosts = [], set()
        for oc in _retrieve(content.propertyCollector, filter_spec):
            row = {"_obj": oc.obj, **{p.name: p.val for p in oc.propSet}}
            row["vm_name"] = row.get("name")
            row["uuid"] = row.get("config.uuid")
            row["guest_fullname"] = row.get("config.guestFullName")
            row["power_state"] = str(row.get("runtime.powerState"))
            row["host_moref"] = row.get("runtime.host")
            if row["host_moref"]:
                hosts.add(row["host_moref"])
            rows.append(row)
        return rows, hosts
    finally:
        view.Destroy()


def _names_and_parents(pc, kind, objects):
    names, parents = {}, {}
    spec = PC.FilterSpec(
        objectSet=[PC.ObjectSpec(obj=o) for o in objects],
        propSet=[PC.PropertySpec(type=kind, all=False, pathSet=["name", "parent"])],
    )
    for oc in _retrieve(pc, spec):
        props = {p.name: p.val for p in oc.propSet}
        if props.get("name"):
            names[oc.obj] = props["name"]
        parents[oc.obj] = props.get("parent")
    return names, parents


def collect_host_cluster_map(content, hosts):
    """Find each host's name and cluster, and each cluster's parent.

    Returns:
        host name by host, cluster name by host, cluster by host, and parent by
        cluster. Hosts outside any cluster are absent from the last three.
    """
    if not hosts:
        return {}, {}, {}, {}
    pc = content.propertyCollector
    host_names, host_parents = _names_and_parents(pc, vim.HostSystem, hosts)
    host_cluster = {
        h: p
        for h, p in host_parents.items()
        if isinstance(p, vim.ClusterComputeResource)
    }
    cluster_names, cluster_parents = {}, {}
    if host_cluster:
        clusters = set(host_cluster.values())
        cluster_names, cluster_parents = _names_and_parents(
            pc, vim.ClusterComputeResource, clusters
        )
    host_cluster_name = {h: cluster_names.get(c) for h, c in host_cluster.items()}
    return host_names, host_cluster_name, host_cluster, cluster_parents


def _ipv4s(nic):
    addresses = getattr(getattr(nic, "ipConfig", None), "ipAddress", None) or []
    return [a.ipAddress for a in addresses if a.ipAddress and "." in a.ipAddress]


def pick_ipv4(row):
    """Return the VM's first IPv4 address: guest.ipAddress, else any NIC's."""
    ip = row.get("guest.ipAddress")
    if ip and "." in ip:
        return ip
    for nic in row.get("guest.net") or []:
        addresses = _ipv4s(nic)
        if addresses:
            return addresses[0]
    return None


def extract_nics(row):
    """Return each NIC's MAC, network and IPv4 addresses, as the guest reports them."""
    return [
        {
            "mac": getattr(nic, "macAddress", None),
            "network": getattr(nic, "network", None),
            "ipv4s": _ipv4s(nic),
        }
        for nic in row.get("guest.net") or []
    ]


def extract_disks(row):
    """Return each virtual disk's label, size, thin provisioning and backing file."""
    disks = []
    for device in row.get("config.hardware.device") or []:
        if not isinstance(device, vim.vm.device.VirtualDisk):
            continue
        info = getattr(device, "deviceInfo", None)
        capacity = getattr(device, "capacityInBytes", None)
        if capacity is None:
            kb = getattr(device, "capacityInKB", None)
            capacity = int(kb) * 1024 if kb is not None else None
        backing = getattr(device, "backing", None)
        disks.append(
            {
                "label": info.label if info else None,
                "capacity_bytes": capacity,
                "thin": getattr(backing, "thinProvisioned", None) if backing else None,
                "backing_file": getattr(backing, "fileName", None) if backing else None,
            }
        )
    return disks


def find_dc_name(cluster, cluster_parents, cache):
    """Walk up from a cluster to its datacenter.

    Returns:
        The datacenter's name without parentheses and as vCenter has it, or
        two Nones when no datacenter is found.
    """
    if cluster in cache:
        return cache[cluster]
    name = None
    for start in (cluster_parents.get(cluster), getattr(cluster, "parent", None)):
        parent = start
        while parent is not None and not isinstance(parent, vim.Datacenter):
            parent = getattr(parent, "parent", None)
        if isinstance(parent, vim.Datacenter):
            name = getattr(parent, "name", None)
            break
    cache[cluster] = (sanitize_dc_name(name), name)
    return cache[cluster]


def find_vms_by_name(content, names, debug=False):
    """Find VMs by inventory name, cheaply.

    First the search index's lookup by guest DNS name, one fast call per name;
    then, for what is still missing, one scan of every VM's name and guest host
    name. Matching ignores case, and a short name matches an FQDN's first label.

    Returns:
        The VM for each requested name this vCenter has.
    """
    found, wanted = {}, {n.lower(): n for n in names}
    for original in wanted.values():
        try:
            vm = content.searchIndex.FindByDnsName(dnsName=original, vmSearch=True)
        except vmodl.MethodFault as e:
            _log(debug, f"lookup by DNS name failed for {original}: {e}")
            vm = None
        if vm:
            found[original] = vm
    missing = {k: v for k, v in wanted.items() if v not in found}
    if not missing:
        return found
    _log(debug, f"{len(missing)} name(s) not found by DNS name; scanning names")
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim.VirtualMachine], True
    )
    try:
        spec = PC.FilterSpec(
            objectSet=[_view_spec(view)],
            propSet=[
                PC.PropertySpec(
                    type=vim.VirtualMachine,
                    all=False,
                    pathSet=["name", "guest.hostName"],
                )
            ],
        )
        for oc in _retrieve(content.propertyCollector, spec):
            props = {p.name: p.val for p in oc.propSet}
            candidates = {
                str(props.get("name") or "").lower(),
                str(props.get("guest.hostName") or "").lower(),
            }
            candidates |= {c.split(".", 1)[0] for c in candidates if c}
            for lowered, original in list(missing.items()):
                if lowered in candidates or lowered.split(".", 1)[0] in candidates:
                    found[original] = oc.obj
                    missing.pop(lowered)
            if not missing:
                break
    finally:
        view.Destroy()
    return found


def _vm_vars(row, vcenter, name, ip, guest_dns):
    memory_mb = row.get("summary.config.memorySizeMB")
    obj = row.get("_obj")
    hostvars = {
        "vmware_uuid": row.get("config.uuid") or row.get("uuid"),
        "vmware_moid": getattr(obj, "_moId", None),
        "vmware_power_state": row.get("power_state"),
        "vmware_guest_fullname": row.get("guest_fullname"),
        "vmware_num_cpu": row.get("summary.config.numCpu"),
        "vmware_memory_mb": memory_mb,
        "vmware_memory": f"{-(-memory_mb // 1024)}G" if memory_mb else None,
        "vmware_nics": extract_nics(row),
        "vmware_disks": extract_disks(row),
        "vmware_vm_name": row.get("vm_name"),
        "vmware_guest_hostname": guest_dns,
        "vmware_vcenter": urlparse(
            vcenter if "://" in vcenter else f"https://{vcenter}"
        ).hostname
        or vcenter,
    }
    if looks_fqdn(name):
        hostvars["vmware_ansible_host"] = name
    elif ip:
        hostvars["vmware_ansible_host"] = ip
    return hostvars


def build_vcenter_inventory(content, vcenter, options, only_vms=None):
    """Build the part of the inventory that one vCenter holds.

    Args:
        content: The vCenter's service content.
        vcenter: The vCenter's name, for its group and vmware_vcenter.
        options: prefer_guest_dns, resolve_fqdn, default_domain, domains, debug.
        only_vms: Build from these VMs alone; everything else is identical, so a
            host-scoped run places a VM in exactly the groups a full run does.

    Returns:
        The inventory fragment, hostvars by name, the vCenter's group name and
        every host name, in discovery order.
    """
    debug = options["debug"]
    started = time.time()
    rows, hosts = collect_vm_props(content, only_vms)
    _log(debug, f"{len(rows)} VMs in {time.time() - started:.2f}s")
    _, host_cluster_name, host_cluster, cluster_parents = collect_host_cluster_map(
        content, hosts
    )
    dc_cache = {}
    vcenter_group = f"vmware_vcenter_{safe_group_name(vcenter)}"
    inventory = {
        vcenter_group: {
            "children": [],
            "hosts": [],
            "vars": {"vmware_vcenter": vcenter},
        },
        "_meta": {"hostvars": {}},
    }
    groups_dc, groups_cluster, by_name, all_hosts = {}, {}, {}, []
    for row in rows:
        try:
            vm_name = row.get("vm_name")
            if not vm_name:
                continue
            host = row.get("host_moref")
            cluster = host_cluster.get(host)
            cluster_name = host_cluster_name.get(host) if cluster else None
            dc_name, dc_original = (
                find_dc_name(cluster, cluster_parents, dc_cache)
                if cluster
                else (None, None)
            )
            ip = pick_ipv4(row)
            summary_name = row.get("summary.guest.hostName")
            guest_dns = row.get("guest.hostName") or (
                summary_name if isinstance(summary_name, str) else None
            )
            name = choose_inventory_name(vm_name, guest_dns, ip, options)
            hostvars = _vm_vars(row, vcenter, name, ip, guest_dns)
            inventory["_meta"]["hostvars"][name] = hostvars
            all_hosts.append(name)
            by_name[name] = hostvars
            if not dc_name:
                inventory[vcenter_group]["hosts"].append(name)
                continue
            dc_group = f"vmware_datacenter_{safe_group_name(dc_name)}"
            if dc_group not in groups_dc:
                groups_dc[dc_group] = {
                    "children": [],
                    "hosts": [],
                    "vars": {
                        "vmware_datacenter": dc_name,
                        "vmware_datacenter_original": dc_original,
                    },
                }
                inventory[vcenter_group]["children"].append(dc_group)
            if not cluster_name:
                groups_dc[dc_group]["hosts"].append(name)
                continue
            cluster_group = f"vmware_cluster_{safe_group_name(cluster_name)}"
            if cluster_group not in groups_cluster:
                groups_cluster[cluster_group] = {
                    "hosts": [],
                    "vars": {"vmware_cluster": cluster_name},
                }
            if cluster_group not in groups_dc[dc_group]["children"]:
                groups_dc[dc_group]["children"].append(cluster_group)
            groups_cluster[cluster_group]["hosts"].append(name)
        except VM_FAILURES as e:
            _log(debug, f"skipping VM {row.get('vm_name')!r}: {e}")
    inventory.update(groups_dc)
    inventory.update(groups_cluster)
    return inventory, by_name, vcenter_group, all_hosts


def _hostname(url):
    return urlparse(url if "://" in url else f"https://{url}").hostname or url


def vcenters_from_env(environ, cli_vcenter=None):
    """Find every vCenter to poll, once each.

    Returns:
        A list of (host, username, password); the host keeps a port when its
        address has one.
    """
    found = []
    host = cli_vcenter or environ.get("VMWARE_HOST")
    user, password = environ.get("VMWARE_USER"), environ.get("VMWARE_PASSWORD")
    if host and user and password:
        found.append((host, user, password))
    for credential in credential_sets(
        "VCENTER", ("BASE_URL", "USERNAME", "PASSWORD"), environ=environ
    ):
        url = credential["base_url"]
        parsed = urlparse(url if "://" in url else f"https://{url}")
        name = parsed.hostname or url
        if any(name == _hostname(f[0]) for f in found):
            continue
        found.append(
            (
                f"{name}:{parsed.port}" if parsed.port else name,
                credential["username"],
                credential["password"],
            )
        )
    return found


def connect_vsphere(host, user, password, port=443, timeout=30.0, insecure=False):
    """Log in to a vCenter.

    Returns:
        The service instance.

    Raises:
        ConnectorError: The vCenter is unreachable or refused the login.
    """
    parsed = urlparse(host if "://" in host else f"https://{host}")
    socket.setdefaulttimeout(timeout)
    context = ssl.create_default_context()
    if insecure:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        return SmartConnect(
            host=parsed.hostname,
            user=user,
            pwd=password,
            port=parsed.port or port,
            sslContext=context,
        )
    except VCENTER_FAILURES as e:
        hint = (
            " (set insecure to true for a self-signed vCenter)"
            if isinstance(e, ssl.SSLError)
            else ""
        )
        raise ConnectorError(
            f"cannot connect to vCenter {parsed.hostname}: {e}{hint}"
        ) from None


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="the whole inventory")
    mode.add_argument("--host", help="one host's variables, without its groups")
    mode.add_argument(
        "--host-with-groups",
        metavar="HOSTS",
        help="the --list shape for these comma-separated VMs only",
    )
    parser.add_argument(
        "--vcenter", help="a vCenter host name or URL (with VMWARE_USER)"
    )
    parser.add_argument("--port", type=int, help="the SDK port (default 443)")
    parser.add_argument(
        "--timeout", type=float, help="socket timeout in seconds (default 30)"
    )
    parser.add_argument("--debug", action="store_true", help="progress on stderr")
    parser.add_argument(
        "--prefer-guest-dns",
        action="store_true",
        help="name hosts after the guest's FQDN",
    )
    parser.add_argument(
        "--resolve-fqdn", action="store_true", help="expand short names through DNS"
    )
    parser.add_argument(
        "--no-resolve-fqdn", action="store_true", help="never look up DNS"
    )
    parser.add_argument(
        "--no-prefer-guest-dns",
        action="store_true",
        help="name hosts after the VM name",
    )
    parser.add_argument("--default-domain", help="a suffix tried first on short names")
    return parser.parse_args(argv)


def _env_flag(environ, name):
    return as_bool(environ.get(name), True)


def _options(args, environ, config):
    return {
        "prefer_guest_dns": not args.no_prefer_guest_dns
        and (args.prefer_guest_dns or _env_flag(environ, "PREFER_GUEST_DNS")),
        "resolve_fqdn": not args.no_resolve_fqdn
        and (args.resolve_fqdn or _env_flag(environ, "RESOLVE_FQDN")),
        "default_domain": args.default_domain
        or config.get("default_domain")
        or environ.get("DEFAULT_DOMAIN"),
        "domains": as_list(config.get("domains")),
        "debug": args.debug,
    }


def _new_inventory():
    return {
        "all": {"children": ["virtual"]},
        "virtualization_type_vmware": {
            "children": [],
            "vars": {"virtualization_type": "vmware"},
        },
        "virtual": {"children": ["virtualization_type_vmware"]},
        "vmware_all_vms": {"hosts": []},
        "_meta": {"hostvars": {}},
    }


def gather(
    vcenters,
    targets,
    options,
    connection,
    connect=connect_vsphere,
    disconnect=Disconnect,
):
    """Poll every vCenter and merge what they hold.

    Returns:
        The inventory and every host's variables by name.

    Raises:
        ConnectorError: A vCenter could not be reached or failed mid-way.
    """
    inventory, by_name = _new_inventory(), {}
    for vcenter, user, password in vcenters:
        si = connect(vcenter, user, password, **connection)
        try:
            content = si.RetrieveContent()
            only_vms = None
            if targets:
                hits = find_vms_by_name(content, targets, options["debug"])
                if not hits:
                    _log(options["debug"], f"none of {targets} is in {vcenter}")
                    continue
                only_vms = list(hits.values())
            part, part_by_name, group, hosts = build_vcenter_inventory(
                content, vcenter, options, only_vms
            )
            inventory["virtualization_type_vmware"]["children"].append(group)
            for name, value in part.items():
                if name == "_meta":
                    inventory["_meta"]["hostvars"].update(value["hostvars"])
                else:
                    inventory[name] = value
            inventory["vmware_all_vms"]["hosts"].extend(hosts)
            by_name.update(part_by_name)
        except VCENTER_FAILURES as e:
            raise ConnectorError(f"vCenter {vcenter}: {e}") from None
        finally:
            with contextlib.suppress(*VCENTER_FAILURES):  # the session expires anyway
                disconnect(si)
    return inventory, by_name


def main(argv=None, environ=None, connect=connect_vsphere, disconnect=Disconnect):
    """Print the inventory, a host-scoped part of it, or one host's variables."""
    environ = os.environ if environ is None else environ
    args = _parse_args(argv)
    try:
        config = settings(environ)
        targets = None
        if args.host_with_groups is not None:
            targets = [n.strip() for n in args.host_with_groups.split(",") if n.strip()]
            if not targets:
                raise ConnectorError("--host-with-groups was given no host name")
        vcenters = vcenters_from_env(environ, args.vcenter)
        if not vcenters:
            raise ConnectorError(
                "no vCenter credentials: set CREDENTIAL_VCENTER_<TAG>_BASE_URL, _USERNAME "
                "and _PASSWORD, or VMWARE_HOST, VMWARE_USER and VMWARE_PASSWORD"
            )
        connection = {
            "port": args.port or as_int(config.get("port"), 443),
            "timeout": args.timeout or float(as_int(config.get("timeout_seconds"), 30)),
            "insecure": as_bool(
                config.get("insecure"), as_bool(environ.get("VMWARE_INSECURE"))
            ),
        }
        options = _options(args, environ, config)
        inventory, by_name = gather(
            vcenters, targets, options, connection, connect, disconnect
        )
        if targets and not inventory["vmware_all_vms"]["hosts"]:
            # An empty answer to a host-scoped run would read as "this VM has
            # no groups"; it is an error instead.
            raise ConnectorError(f"none of {targets} was found in any vCenter")
    except ConnectorError as e:
        print(e, file=sys.stderr)
        return 1
    if args.host is not None:
        print(json.dumps(by_name.get(args.host, {}), indent=2, sort_keys=True))
    else:
        print(json.dumps(inventory, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
