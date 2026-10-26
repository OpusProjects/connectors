#!/usr/bin/env python3
"""Ansible dynamic inventory for Cisco UCS Manager, through its XML API.

Two inventories from one script, so a play aimed at the servers can never
reach an empty blade:

  --list            the blades running a server, named after their service
                    profile, in physical > ucs_blades > ucs_manager_<manager>
  --list --spares   the blades with no service profile, named
                    <manager>_chassis<N>_blade<M>, in ucs_free_blades when one
                    could take a server today and ucs_unusable_blades otherwise

Every host carries these variables: ucs_cpu_sockets, ucs_memory, ucs_memory_mb,
ucs_vnics, ucs_manager, ucs_service_profile, ucs_blade_dn, ucs_chassis,
ucs_slot, ucs_model, ucs_serial, ucs_uuid, ucs_cpu_cores, ucs_cpu_threads,
ucs_oper_state, ucs_operability, ucs_availability, ucs_power and ucs_label.

Managers come from the environment, one set of three variables per manager:
CREDENTIAL_UCS_<TAG>_BASE_URL, _USERNAME and _PASSWORD as unified-api hands
them over, or UCS_<TAG>_BASE_URL, _USERNAME and _PASSWORD as an Ansible
controller would. TAG is any name; every complete set is polled.

Settings, from the source's config (SOURCE_CONFIG, JSON; unified-api passes
every value as a string):
  domains          comma-separated suffixes tried in order to turn a service
                   profile name into a host name that resolves
  insecure         true to skip TLS verification (self-signed managers)
  timeout_seconds  per request, default 60
"""

import argparse
import contextlib
import json
import os
import socket
import ssl
import sys
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

# The SDK sits at the repository root, one folder up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from unified_connector import (
    ConnectorError,
    as_bool,
    as_int,
    as_list,
    credential_sets,
    settings,
)

# What a manager that is down, slow, refusing or answering nonsense raises:
# network errors and timeouts are OSError, a garbled answer a ParseError, a
# refused login a RuntimeError.
FAILURES = (OSError, ET.ParseError, ValueError, RuntimeError)


def _xml_escape(value):
    """Escape a value for an XML attribute."""
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


class UCSClient:
    """A logged-in session on one UCS Manager."""

    def __init__(self, base_url, user, password, insecure=False, timeout=60):
        """Log in to the manager.

        Args:
            base_url: The manager's address, with or without https://.
            user: The account to log in with.
            password: Its password.
            insecure: Skip TLS verification.
            timeout: Seconds per request.

        Raises:
            RuntimeError: The manager refused the login.
        """
        if "://" not in base_url:
            base_url = f"https://{base_url}"
        self.url = base_url.rstrip("/") + "/nuova"
        self.timeout = timeout
        self.context = ssl.create_default_context()
        if insecure:
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE
        self.cookie = None
        root = self._post(
            f'<aaaLogin inName="{_xml_escape(user)}" '
            f'inPassword="{_xml_escape(password)}"/>'
        )
        self.cookie = root.get("outCookie", "")
        if root.get("response") != "yes" or not self.cookie:
            raise RuntimeError(f"login refused: {root.get('errorDescr', 'unknown')}")

    def logout(self):
        """End the session; a failure here changes nothing for the inventory."""
        if self.cookie:
            with contextlib.suppress(*FAILURES):  # the session expires anyway
                self._post(f'<aaaLogout inCookie="{_xml_escape(self.cookie)}"/>')
            self.cookie = None

    def resolve_class(self, class_id):
        """Return every object of one class."""
        return self._post(
            f'<configResolveClass cookie="{_xml_escape(self.cookie)}" '
            f'classId="{class_id}" inHierarchical="false"/>'
        )

    def resolve_children(self, dn, class_id):
        """Return the children of one object that belong to one class."""
        return self._post(
            f'<configResolveChildren cookie="{_xml_escape(self.cookie)}" '
            f'inDn="{_xml_escape(dn)}" classId="{class_id}" '
            f'inHierarchical="false"/>'
        )

    def resolve_dns(self, dns):
        """Return the objects at these distinguished names, with their children."""
        dn_xml = "".join(f'<dn value="{_xml_escape(dn)}"/>' for dn in dns)
        return self._post(
            f'<configResolveDns cookie="{_xml_escape(self.cookie)}" '
            f'inHierarchical="true"><inDns>{dn_xml}</inDns></configResolveDns>'
        )

    def _post(self, body):
        request = urllib.request.Request(
            self.url,
            data=body.encode(),
            headers={"Content-Type": "application/xml"},
            method="POST",
        )
        with urllib.request.urlopen(
            request, timeout=self.timeout, context=self.context
        ) as response:
            return ET.fromstring(response.read())


def _find(root, tag):
    """Find every element with this tag, wherever the answer nests it."""
    return root.findall(f".//{tag}") or [e for e in root.iter() if e.tag == tag]


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def resolve_fqdn(short, domains):
    """Return the first short.domain that resolves, or short when none does."""
    for domain in domains:
        candidate = f"{short}.{domain}"
        try:
            socket.getaddrinfo(candidate, None)
            return candidate
        except OSError:
            continue
    return short


def manager_name(base_url):
    """Return the manager's host name, the label its blades are grouped under."""
    parsed = urlparse(base_url if "://" in base_url else f"https://{base_url}")
    return parsed.hostname or base_url


def blade_vars(blade, manager, vnics):
    """Return the host variables for one blade, running a server or not."""
    memory_mb = _int(blade.get("totalMemory"))
    return {
        "ucs_cpu_sockets": _int(blade.get("numOfCpus")),
        "ucs_memory": f"{-(-memory_mb // 1024)}G",
        "ucs_memory_mb": memory_mb,
        "ucs_vnics": vnics,
        "ucs_manager": manager,
        "ucs_service_profile": blade.get("assignedToDn", ""),
        "ucs_blade_dn": blade.get("dn", ""),
        "ucs_chassis": _int(blade.get("chassisId")),
        "ucs_slot": _int(blade.get("slotId")),
        "ucs_model": blade.get("model", ""),
        "ucs_serial": blade.get("serial", ""),
        "ucs_uuid": blade.get("uuid", ""),
        "ucs_cpu_cores": _int(blade.get("numOfCores")),
        "ucs_cpu_threads": _int(blade.get("numOfThreads")),
        "ucs_oper_state": blade.get("operState", ""),
        "ucs_operability": blade.get("operability", ""),
        "ucs_availability": blade.get("availability", ""),
        "ucs_power": blade.get("operPower", ""),
        "ucs_label": blade.get("usrLbl", ""),
    }


def spare_name(manager, blade):
    """Name a blade with no service profile, and so no host name.

    manager_chassisN_bladeM is unique across managers and free of the slash and
    colon a blade's distinguished name would bring into a host pattern.
    """
    return (
        f"{manager}_chassis{blade.get('chassisId', '')}_blade{blade.get('slotId', '')}"
    )


def is_free(hostvars):
    """Tell whether a spare blade could take a server today."""
    return (
        hostvars.get("ucs_service_profile") == ""
        and hostvars.get("ucs_availability") == "available"
        and hostvars.get("ucs_operability") == "operable"
    )


def _vlan_ids(client):
    """Map each VLAN name to its number; empty when the manager will not say."""
    ids = {}
    try:
        for vlan in _find(client.resolve_class("fabricVlan"), "fabricVlan"):
            name, number = vlan.get("name", ""), vlan.get("id", "")
            if name and number.isdigit():
                ids[name] = int(number)
    except FAILURES:  # VLANs are a detail, never worth the inventory
        return {}
    return ids


def _default_vlan(client, vnic_dn, vlan_ids):
    """Return the number of a vNIC's native VLAN, or None."""
    interfaces = _find(client.resolve_children(vnic_dn, "vnicEtherIf"), "vnicEtherIf")
    name = next(
        (i.get("name") for i in interfaces if i.get("defaultNet") == "yes"), None
    )
    if not name and len(interfaces) == 1:
        name = interfaces[0].get("name")
    return vlan_ids.get(name)


def _vnics(client, profiles):
    """Return each service profile's vNICs, with MAC and native VLAN when known."""
    vlan_ids = _vlan_ids(client)
    by_profile = {}
    try:
        vnics = _find(client.resolve_class("vnicEther"), "vnicEther")
    except FAILURES:  # a blade without vNICs is still a blade
        return by_profile
    for vnic in vnics:
        dn, name, mac = vnic.get("dn", ""), vnic.get("name", ""), vnic.get("addr", "")
        profile = dn.rsplit("/ether-", 1)[0] if "/ether-" in dn else ""
        if not name or profile not in profiles:
            continue
        entry = {"name": name}
        if mac and mac.lower() != "derived":
            entry["mac"] = mac
        if vlan_ids:
            try:
                vlan = _default_vlan(client, dn, vlan_ids)
            except FAILURES:
                vlan = None
            if vlan is not None:
                entry["vlan"] = vlan
        by_profile.setdefault(profile, []).append(entry)
    return by_profile


def _fill_derived_macs(client, blade, vnics):
    """Read the hardware MACs for vNICs whose address the profile derives."""
    if all("mac" in v for v in vnics) or not blade.get("dn"):
        return
    try:
        answer = client.resolve_dns([blade.get("dn")])
    except FAILURES:  # the MAC stays unknown
        return
    hardware = {
        n.get("name"): n.get("mac")
        for n in _find(answer, "adaptorHostEthIf")
        if n.get("name") and n.get("mac")
    }
    for vnic in vnics:
        if "mac" not in vnic and vnic["name"] in hardware:
            vnic["mac"] = hardware[vnic["name"]]


def collect(client, manager, domains):
    """Read one manager's blades.

    Args:
        client: A logged-in UCSClient, or anything answering the same calls.
        manager: The manager's host name, for ucs_manager and the group name.
        domains: Suffixes tried to turn a service profile name into a host name.

    Returns:
        Two dicts of host variables by inventory name: the blades running a
        server, and the spare blades.
    """
    servers, spares, associated = {}, {}, []
    for blade in _find(client.resolve_class("computeBlade"), "computeBlade"):
        profile = blade.get("assignedToDn", "")
        if blade.get("association") != "associated" or not profile:
            spares[spare_name(manager, blade)] = blade_vars(blade, manager, [])
            continue
        name = profile.rsplit("/", 1)[-1].removeprefix("ls-").lower()
        if name:
            associated.append((name, blade))
    by_profile = _vnics(client, {b.get("assignedToDn") for _, b in associated})
    for name, blade in associated:
        vnics = by_profile.get(blade.get("assignedToDn"), [])
        _fill_derived_macs(client, blade, vnics)
        servers[resolve_fqdn(name, domains)] = blade_vars(blade, manager, vnics)
    return servers, spares


def build_inventory(servers):
    """Build the servers inventory: physical > ucs_blades > one group per manager."""
    inventory = {
        "all": {"children": ["physical"]},
        "physical": {"children": ["ucs_blades"]},
        "ucs_blades": {"hosts": [], "children": []},
        "_meta": {"hostvars": {}},
    }
    for name, hostvars in sorted(servers.items()):
        inventory["ucs_blades"]["hosts"].append(name)
        inventory["_meta"]["hostvars"][name] = hostvars
        manager = hostvars.get("ucs_manager", "")
        if not manager:
            continue
        group = "ucs_manager_" + manager.replace(".", "_").replace("-", "_")
        if group not in inventory:
            inventory[group] = {"hosts": [], "vars": {"ucs_manager": manager}}
            inventory["ucs_blades"]["children"].append(group)
        inventory[group]["hosts"].append(name)
    return inventory


def build_spare_inventory(spares):
    """Build the spares inventory, kept apart from the servers on purpose.

    A play aimed at all or physical in the servers inventory must never reach a
    blade with no service profile: there is no machine there to configure. So
    this inventory has no physical group either.
    """
    inventory = {
        "all": {"children": ["ucs_free_blades", "ucs_unusable_blades"]},
        "ucs_free_blades": {"hosts": [], "children": []},
        "ucs_unusable_blades": {"hosts": [], "children": []},
        "_meta": {"hostvars": {}},
    }
    for name, hostvars in sorted(spares.items()):
        group = "ucs_free_blades" if is_free(hostvars) else "ucs_unusable_blades"
        inventory[group]["hosts"].append(name)
        inventory["_meta"]["hostvars"][name] = hostvars
    return inventory


def gather(managers, config, connect=UCSClient):
    """Poll every manager.

    Args:
        managers: Credential sets, each with base_url, username and password.
        config: The source's settings.
        connect: Builds a logged-in client; UCSClient unless a test fakes it.

    Returns:
        The servers, the spares, and the managers that failed.
    """
    domains = as_list(config.get("domains"))
    insecure = as_bool(config.get("insecure"))
    timeout = as_int(config.get("timeout_seconds"), 60)
    servers, spares, failed = {}, {}, []
    for manager in managers:
        url = manager["base_url"]
        try:
            client = connect(
                url,
                manager["username"],
                manager["password"],
                insecure=insecure,
                timeout=timeout,
            )
        except FAILURES as e:  # reported, the others still count
            print(f"{manager_name(url)}: {e}", file=sys.stderr)
            failed.append(manager_name(url))
            continue
        try:
            found, spare = collect(client, manager_name(url), domains)
            servers.update(found)
            spares.update(spare)
        except FAILURES as e:
            print(f"{manager_name(url)}: {e}", file=sys.stderr)
            failed.append(manager_name(url))
        finally:
            client.logout()
    return servers, spares, failed


def main(argv=None, environ=None, connect=UCSClient):
    """Print the inventory, or one host's variables, as JSON."""
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="the whole inventory")
    mode.add_argument("--host", help="one host's variables")
    parser.add_argument(
        "--spares", action="store_true", help="the blades with no service profile"
    )
    args = parser.parse_args(argv)

    managers = credential_sets(
        "UCS", ("BASE_URL", "USERNAME", "PASSWORD"), environ=environ
    )
    if not managers:
        print(
            "no UCS Manager credentials: set CREDENTIAL_UCS_<TAG>_BASE_URL, "
            "_USERNAME and _PASSWORD",
            file=sys.stderr,
        )
        return 1
    try:
        config = settings(environ)
        servers, spares, failed = gather(managers, config, connect)
    except ConnectorError as e:
        print(e, file=sys.stderr)
        return 1
    if len(failed) == len(managers):
        # Not one manager answered. An empty inventory here would read as
        # "no blades" and sync green; a failure is what the caller records.
        print(f"no UCS Manager answered ({', '.join(failed)})", file=sys.stderr)
        return 1
    found = spares if args.spares else servers
    if args.list:
        build = build_spare_inventory if args.spares else build_inventory
        print(json.dumps(build(found), indent=2, sort_keys=True))
    else:
        print(json.dumps(found.get(args.host, {}), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
