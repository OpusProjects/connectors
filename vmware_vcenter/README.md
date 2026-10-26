# vmware_vcenter

VMware vCenter virtual machines as an Ansible dynamic inventory. Properties are fetched in batched
PropertyCollector calls rather than VM by VM, so a large vCenter answers in seconds.

| Command | Prints |
| ------- | ------ |
| `--list` | Every VM of every configured vCenter |
| `--host-with-groups <a,b>` | The `--list` shape holding only these VMs, groups included: what a host-scoped sync needs |
| `--host <name>` | One VM's variables, without groups |

## Groups

```
all > virtual > virtualization_type_vmware > vmware_vcenter_<vcenter>
    > vmware_datacenter_<datacenter> > vmware_cluster_<cluster>
```

`vmware_all_vms` holds every VM. A VM on a host outside any cluster sits in its datacenter's group,
and one whose datacenter is unknown in its vCenter's group. Group variables: `virtualization_type`
(`vmware`), `vmware_vcenter`, `vmware_datacenter` (parentheses removed), `vmware_datacenter_original`
and `vmware_cluster`. Characters Ansible refuses in group names become `_`; case is kept.

## Host variables

`vmware_uuid`, `vmware_moid`, `vmware_power_state`, `vmware_guest_fullname`, `vmware_num_cpu`,
`vmware_memory_mb`, `vmware_memory` (`8G`), `vmware_vm_name`, `vmware_guest_hostname`,
`vmware_vcenter`, `vmware_nics` (`mac`, `network`, `ipv4s`), `vmware_disks` (`label`,
`capacity_bytes`, `thin`, `backing_file`) and `vmware_ansible_host`: the FQDN when the host is
named by one, else its first IPv4 address.

## Host names

| Flag | Default | Effect |
| ---- | ------- | ------ |
| `--prefer-guest-dns` / `--no-prefer-guest-dns` | on | Use the name the guest reports when it is an FQDN |
| `--resolve-fqdn` / `--no-resolve-fqdn` | on | Expand short names through DNS: `default_domain`, the resolver, reverse lookups, then each of `domains` |
| `--default-domain <suffix>` | none | A suffix tried first on short names |

Without lookups a run is fast; with them each unresolved name can cost a DNS timeout, which is why
`example.yaml` keeps one quick source and one with DNS names on a longer interval. The
`PREFER_GUEST_DNS` and `RESOLVE_FQDN` variables change the defaults for an Ansible controller.

## Settings

From the source's `config:`, all as strings:

| Setting | Default | Meaning |
| ------- | ------- | ------- |
| `domains` | none | Comma-separated suffixes tried, in order, on short guest names |
| `default_domain` | none | A suffix tried first (also `--default-domain` or `DEFAULT_DOMAIN`) |
| `insecure` | `false` | `true` skips TLS verification, for a vCenter with a self-signed certificate (also `VMWARE_INSECURE`) |
| `port` | `443` | The SDK port, unless the address carries one (also `--port`) |
| `timeout_seconds` | `30` | Socket timeout (also `--timeout`) |

## Credentials

Any number of vCenters, each polled once even when given twice:

- From unified-api: `CREDENTIAL_VCENTER_<TAG>_BASE_URL`, `_USERNAME`, `_PASSWORD` (see `example.yaml`).
- From an Ansible controller: `VCENTER_<TAG>_BASE_URL`, `_USERNAME`, `_PASSWORD`, or one vCenter as
  `VMWARE_HOST` (or `--vcenter`), `VMWARE_USER` and `VMWARE_PASSWORD`.

A read-only vCenter role is enough. Any vCenter failing fails the whole run: half an inventory
would read as VMs that disappeared.

Needs `pyvmomi` (`requirements.txt`).
