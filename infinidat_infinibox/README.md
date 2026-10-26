# infinidat_infinibox

An Infinidat InfiniBox as an Ansible dynamic inventory, read through its REST API. Each host the
array maps volumes to becomes an inventory host carrying those volumes, with each volume's WWID:
the value multipath reports on the host, so a disk can be matched to its volume by WWID alone.

| Command | Prints |
| ------- | ------ |
| `--list` | Every host with mapped volumes, in one group per array: `iboxarray_<array>` |
| `--host <name>` | One host's variables |

## Host variables

```yaml
infinibox:
  system: array01.example.com          # the array
  host: igrp-web01-prod-shop-east      # the host object on the array
  volumes:                             # sorted by LUN
    - volume: vol-web01-boot
      serial: 742b0f000000000000000000000001
      wwid: 36742b0f000000000000000000000001
      size: 107374182400
      size_gb: 100
      pool: pool-a
      provtype: THIN
      lun: 1
```

The group's `infinibox_array` variable holds the array's name unchanged; the group name is the
same name with every character Ansible refuses replaced by `_`. Volumes mapped to no host, such
as snapshots, are left out.

## Settings

From the source's `config:`, all as strings:

| Setting | Default | Meaning |
| ------- | ------- | ------- |
| `url` | required | The array's address |
| `insecure` | `false` | `true` skips TLS verification, for an array with a self-signed certificate |
| `timeout_seconds` | `180` | Per request |
| `host_prefix` | none | A prefix to strip from host objects' names; hosts without it are skipped |
| `dotted_segments` | `0` | How many trailing `-` parts are domain labels: with `3`, `web01-prod-shop-east` becomes `web01.prod.shop.east` |
| `domain` | none | A suffix appended to every host name |

With none of the last three, host objects keep their names on the array.

## Credentials

- From unified-api: `CREDENTIAL_USERNAME` and `CREDENTIAL_PASSWORD` (see `example.yaml`).
- From an Ansible controller: `INFINIBOX_USERNAME` and `INFINIBOX_PASSWORD`, and the address may
  come as `INFINIBOX_BASE_URL` instead of `url`.

A read-only account is enough. Standard library only.
