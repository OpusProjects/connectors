# dell_avamar

Dell Avamar backups as an Ansible dynamic inventory, read through the Avamar REST API: every
virtual machine registered on each server, with its most recent backups, so a play or a form can
see what is protected and pick a backup to restore.

| Command | Hosts | Groups |
| ------- | ----- | ------ |
| `--list` | Registered VMs (retired ones and agent clients left out) | `avamar_registered` > `avamar_server_<server>` |
| `--host <name>` | One VM's variables | |

Options: `--max-backups N` (default 20), `--skip-backups` (registrations only), `--workers N`
(parallel backup requests per server, default 8) and `--debug` (progress on stderr). The
settings below win over the first three.

## Host names

A VM name with a dot is kept as it is. A short name gets the server's own domain, or the first of
`domains` that resolves; when none resolves, the server's domain is used anyway, so the name stays
the same from run to run.

## Host variables

`avamar_registered`, `avamar_client_id`, `avamar_server`, `avamar_domain`, `avamar_client_type`,
`avamar_enabled`, `avamar_vm_name`, `avamar_backup_count`, `avamar_latest_backup` and
`avamar_backups`, newest first, each with `id`, `created`, `size_gb`, `type`, `plugin_name`,
`retention`, `server` and `tier_status`.

`created` keeps its zone: `2026-07-22 22:10 UTC`. Avamar reports UTC while servers schedule in
local time, so a bare time would put a late-evening backup under the wrong day.

## Settings

From the source's `config:`, all as strings:

| Setting | Default | Meaning |
| ------- | ------- | ------- |
| `domains` | none | Comma-separated suffixes tried after the server's own domain |
| `max_backups` | `20` | Backups listed per VM |
| `skip_backups` | `false` | `true` lists registrations only |
| `workers` | `8` | Parallel backup requests per server |
| `insecure` | `false` | `true` skips TLS verification, for servers with a self-signed certificate |
| `timeout_seconds` | `30` | Per request |

## Credentials

One set per server, any number of servers; `TAG` is any name:

- From unified-api: `CREDENTIAL_AVAMAR_<TAG>_URL`, `_USERNAME`, `_PASSWORD` (see `example.yaml`).
- From an Ansible controller: `AVAMAR_<TAG>_URL`, `_USERNAME`, `_PASSWORD`.
- Optional `_CLIENT_ID` and `_CLIENT_SECRET` sign in through OAuth2; without them every request
  uses basic authentication.

A read-only account is enough. One server failing is reported on stderr and the others are still
listed; when none answers, the script exits 1 instead of printing an empty inventory.

Standard library only.
