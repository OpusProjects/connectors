# device42

Device42 devices as an Ansible dynamic inventory, read with DOQL queries. What the inventory
holds is decided by a folder of filter files, so one script serves several inventories: point
each source at its own folder.

| Command | Output |
| ------- | ------ |
| `--list` | The whole inventory |
| `--host <name>` | One host's variables, no groups |
| `--host-with-groups <a>,<b>` | The `--list` shape for the named hosts, looked up one by one: what a host-scoped sync calls |

## How a run works

1. Two DOQL queries in parallel: the devices joined with their addresses, and the custom fields.
2. Each address's type tells whether it is shared (a VIP) or the host's own; IPv6 is dropped.
3. `ansible_host` is `config_management_ip` when set, else the best address: `ens*`, then
   `eth*`, then any of the host's own, then unknown, then shared.
4. Hosts with several addresses of unknown type are looked up in DNS; hosts with several
   addresses get an SSH reachability probe, recorded in `host_all_ips_connectivity`.
5. A failed query exits 1; it never prints an empty inventory.

## Filter files

`config_dir` holds these, as YAML (`.yml`, `.yaml`; needs PyYAML) or JSON (`.json`; no
dependency). A missing file means empty. [`filters.example/`](filters.example) shows each one.

| File | Holds |
| ---- | ----- |
| `d42_domains` | Name suffixes to include; empty means every device |
| `d42_excluded_hosts` | Device names never listed |
| `d42_device_filters` | `avoid` and `required` substrings per device field |
| `d42_host_fields`, `d42_host_custom_fields` | Fields copied into host variables |
| `d42_group_fields`, `d42_group_custom_fields` | Fields that become `<field>_<value>` groups |
| `d42_field_aliases` | Output names for Device42 fields |
| `d42_settings` | `include_hosts_without_ip`, `hostname_as_fallback`, `inventory_source`, `datacenter_networks`, `datacenter_domains` |

Every host gets `sourcedfromdevice42: true`; `inventory_source` adds a group holding every host,
and the two datacenter maps add `datacenter_<name>` groups by network or by name suffix.

## Settings

From the source's `config:`, all as strings:

| Setting | Default | Meaning |
| ------- | ------- | ------- |
| `config_dir` | required | Absolute path of the filter folder |
| `credential_prefix` | `CREDENTIAL`, then `D42` | Read `<prefix>_BASE_URL`, `_USERNAME`, `_PASSWORD` |
| `insecure` | `false` | `true` skips TLS verification |
| `connect_timeout_seconds`, `read_timeout_seconds` | `30`, `120` | The larger one bounds each request |
| `ssh_probe` | `true` | Probe hosts with several addresses |
| `ssh_port`, `ssh_timeout_seconds`, `ssh_workers` | `22`, `0.3`, 32 per CPU (64 or more) | The probe |
| `dns_fallback`, `prefer_dns`, `ssh_skip_when_dns` | `true` | DNS lookups, and whether they win over the probe |
| `dns_workers` | `ssh_workers` | Parallel lookups |
| `log_level` | `WARNING` | Messages on stderr |

## Credentials

`CREDENTIAL_BASE_URL`, `CREDENTIAL_USERNAME` and `CREDENTIAL_PASSWORD` from unified-api (see
`example.yaml`), or `D42_BASE_URL`, `D42_USERNAME` and `D42_PASSWORD` from an Ansible controller.
A read-only Device42 user is enough.

Standard library only, plus PyYAML for YAML filter files.
