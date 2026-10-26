# pure_flasharray

A Pure Storage FlashArray's hosts and the volumes (LUNs) mapped to them, as an Ansible dynamic
inventory read through REST API 1.19, so a play can find a host's block devices by serial or
multipath WWID.

| Command | Hosts | Groups |
| ------- | ----- | ------ |
| `--list` | Every host on the array, and every host group member | `purearray_<array>` > `purehostgroup_<host group>` |
| `--host <name>` | One host's variables, by short or resolved name | |

## Variables

A host group's group: `pure_hgroup`, `pure_group`, `pure_array`, `pure_luns_shared` (the LUNs
mapped to the whole group) and `pure_lun_count`.

A host: `pure_array`, `pure_host`, `pure_host_short`, `pure_hgroups`, `pure_groups`,
`pure_luns_individual` (the LUNs mapped to the host alone), `pure_lun_count`, `pure_iqns`,
`pure_wwns`, `pure_nqns` and `pure_host_personality`.

A set of LUNs has `volumes_all` (every volume, flat, with its `volume_group` or null), `volumes`
(those outside any volume group) and `volume_groups` (group name to its volumes, each named
without the group prefix and with `full_volume`). Every volume has `lun_id`, `serial`,
`multipath_wwid`, `size_bytes` and `size_human`. Group membership comes from the array's own list
of volume groups, never from the slash in a volume name.

## Settings

From the source's `config:`, all as strings:

| Setting | Default | Meaning |
| ------- | ------- | ------- |
| `resolve_fqdn` | `false` | `true` names hosts by the fully qualified name DNS gives |
| `domain` | none | With `resolve_fqdn`, the suffix for names DNS does not know |
| `insecure` | `false` | `true` skips TLS verification, for arrays with a self-signed certificate |
| `workers` | `20` | Parallel requests |
| `timeout_seconds` | `60` | Per request |

## Credentials

One array per source; run one source per array. `TAG` is any name:

- From unified-api: `CREDENTIAL_PURE_<TAG>_BASE_URL`, `_USERNAME`, `_PASSWORD` (see `example.yaml`).
- From an Ansible controller: `PURE_<TAG>_BASE_URL`, `_USERNAME`, `_PASSWORD`.

A read-only array user is enough. A failure exits 1 with nothing on stdout.

Standard library only.
