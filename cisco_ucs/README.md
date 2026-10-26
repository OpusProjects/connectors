# cisco_ucs

Cisco UCS Manager blades as an Ansible dynamic inventory, read through the UCS XML API. One
script gives two inventories, so a play aimed at the servers can never reach an empty blade.

| Command | Hosts | Groups |
| ------- | ----- | ------ |
| `--list` | Blades running a server, named after their service profile | `physical` > `ucs_blades` > `ucs_manager_<manager>` |
| `--list --spares` | Blades with no service profile, named `<manager>_chassis<N>_blade<M>` | `ucs_free_blades`, `ucs_unusable_blades` |
| `--host <name>` | One host's variables (add `--spares` for a spare) | |

A spare is free when it is available and operable: a server could be put on it today.

## Host variables

`ucs_manager`, `ucs_service_profile`, `ucs_blade_dn`, `ucs_chassis`, `ucs_slot`, `ucs_model`,
`ucs_serial`, `ucs_uuid`, `ucs_cpu_sockets`, `ucs_cpu_cores`, `ucs_cpu_threads`, `ucs_memory`
(`384G`), `ucs_memory_mb`, `ucs_oper_state`, `ucs_operability`, `ucs_availability`, `ucs_power`,
`ucs_label` and `ucs_vnics`: each vNIC's `name`, `mac` (read from the adapter when the profile
derives it) and native `vlan` number.

## Settings

From the source's `config:`, all as strings:

| Setting | Default | Meaning |
| ------- | ------- | ------- |
| `domains` | none | Comma-separated suffixes tried in order; the first `<profile>.<suffix>` that resolves names the host |
| `insecure` | `false` | `true` skips TLS verification, for managers with a self-signed certificate |
| `timeout_seconds` | `60` | Per request |

## Credentials

One set per manager, any number of managers; `TAG` is any name:

- From unified-api: `CREDENTIAL_UCS_<TAG>_BASE_URL`, `_USERNAME`, `_PASSWORD` (see `example.yaml`).
- From an Ansible controller: `UCS_<TAG>_BASE_URL`, `_USERNAME`, `_PASSWORD`.

A read-only UCS account is enough. One manager failing is reported on stderr and the others are
still listed; when none answers, the script exits 1 instead of printing an empty inventory.

Standard library only.
