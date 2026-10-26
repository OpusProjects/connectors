# centreon

The hosts of one estate as Centreon monitors them, as an Ansible dynamic inventory read through
the Centreon REST API v2: which host groups and service groups exist, which hosts are in each,
and which services a host has.

| Command | Output |
| ------- | ------ |
| `--list` | The whole inventory |
| `--host <name>` | One host's variables |
| `--host-with-groups <a>,<b>` | The `--list` shape for the named hosts: what a host-scoped sync calls |

`--host` and `--host-with-groups` run the same build narrowed to the named hosts, so a host
refresh costs a few requests instead of a full build.

## Host names

Centreon often stores a short name (`web01.prod.east`), and its `fqdn` field is the monitoring
address. Each configured domain suffix is cut into its dot-delimited heads; a host matches on the
longest head its name ends in, and the rest of the suffix is appended back:

```
Centreon  web01.prod.east
head      .prod.east
emitted   web01.prod.east.example.com
```

A name that already carries the full domain is emitted unchanged.

## Output

Each host's variables sit under one key, `centreon`: `host_id`, `name` (as Centreon knows it),
`alias`, `address`, `datacenter`, `monitoring_server`, `status`, `is_in_downtime`,
`is_acknowledged`, `hostgroups`, `servicegroups`, and with `include_services` also `services`
(what the pollers run) and `disabled_services` (configured but deactivated).

| Group | Holds |
| ----- | ----- |
| `centreon` | Every host matched |
| `centreon_dc_<dc>` | Hosts by datacenter, with `datacenter_pattern` |
| `centreon_hostgroup_<name>` | One per host group with a member here |
| `centreon_servicegroup_<name>` | Hosts owning a service in that group |
| `centreon_disabled_services` | Hosts with a deactivated service |

## Keeping it cheap

Every request is narrowed on the server to the estate's names, because the platform can be far
larger than the estate. Group catalogues only give names, so each group's members cost one
request, run in a pool (`workers`). No endpoint selects fields, so a service row costs about 2 KB:
`include_services` is off by default, since the service group probes already return the
services that are in a group.

## Settings

From the source's `config:`, all as strings:

| Setting | Default | Meaning |
| ------- | ------- | ------- |
| `domains` | required | Comma-separated FQDN suffixes of the estate; without them nothing is listed |
| `datacenter_pattern` | none | Regular expression whose first group, found in a suffix, names its datacenter |
| `include_services` | `false` | Attach each host's services and deactivated services |
| `workers` | `8` | Parallel requests |
| `api_path` | `/centreon/api/latest` | The API's path on the server |
| `insecure` | `false` | `true` skips TLS verification |

## Credentials

`CREDENTIAL_BASE_URL`, `CREDENTIAL_USERNAME` and `CREDENTIAL_PASSWORD` from unified-api (see
`example.yaml`), or `CENTREON_BASE_URL`, `CENTREON_USERNAME` and `CENTREON_PASSWORD` from an
Ansible controller. A read-only API user is enough.

A full build that matches no host exits 1 rather than printing an empty inventory that would
empty the source. Standard library only.
