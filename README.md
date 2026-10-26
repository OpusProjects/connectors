# Unified Connectors

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg?logo=python)](https://www.python.org/)

Reusable Python connectors for [unified-api](https://github.com/OpusProjects/unified-api) and
[unified-gitops](https://github.com/OpusProjects/unified-gitops).

A connector is a script that knows one kind of system. Both services run it the same way: as a
command in a git project they pull, with its settings and credentials in the environment and JSON
on stdout. This repository is that project, shared, so each system's connector is written once.

The contracts are each service's: [inventory connectors](https://github.com/OpusProjects/unified-api/blob/main/docs/connectors.md)
and [gitops connectors](https://github.com/OpusProjects/unified-gitops/blob/main/docs/connectors.md).

---

## ✨ Features

- **One connector, both services**: inventory reads for unified-api, list and apply for unified-gitops
- **One system, one script**: plus the shared helpers it imports; nothing to install per system
- **No secrets in git**: addresses and credentials come from the service's config, in the environment
- **Read-only unless asked**: a gitops connector writes only on `apply`, when writing is allowed
- **JSON in, JSON out**: data on stdout, messages on stderr, exit 0 only with a whole document
- **Interruptible**: a run past its timeout is killed, so a script never leaves half-written state
- **Self-describing**: a gitops connector says which kinds it manages, their key and what goes first
- **Pulled, not packaged**: the services check this repository out as a project; no image rebuild

---

## 🔌 Connectors

| Folder | System | unified-api | unified-gitops |
| ------ | ------ | :---------: | :------------: |
| [`centreon`](centreon) | Centreon monitored hosts and their services | ✓ | |
| [`cisco_ucs`](cisco_ucs) | Cisco UCS Manager blades | ✓ | |
| [`dell_avamar`](dell_avamar) | Dell Avamar backup clients and their backups | ✓ | |
| [`device42`](device42) | Device42 devices, through DOQL | ✓ | |
| [`infinidat_infinibox`](infinidat_infinibox) | InfiniBox hosts and their volumes | ✓ | |
| [`kv`](kv) | Key-value store in a folder (example) | | ✓ |
| [`pure_flasharray`](pure_flasharray) | Pure Storage FlashArray hosts and their volumes | ✓ | |
| [`vmware_vcenter`](vmware_vcenter) | vCenter virtual machines | ✓ | |

---

## 🤝 Contributing

Contributions are welcome: [CONTRIBUTING.md](CONTRIBUTING.md) covers the PR workflow, commit style, checks and connector rules.

Security issues: see [SECURITY.md](SECURITY.md) for private reporting.

---

## 👥 Authors

- [Fernando Roca](https://github.com/fernandorocagonzalez)
- [Blai Peidro](https://github.com/blaipr)

---

## ⚖️ License

[Apache 2.0](LICENSE)
