# Connectors

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg?logo=python)](https://www.python.org/)

Reusable connectors to infrastructure systems, written in Python.

A connector is a script that knows one kind of system. A service runs it as a command in a git
project it pulls, with its settings and credentials in the environment and JSON on stdout. This
repository is that project, shared, so each system's connector is written once.

There are two kinds: an inventory connector reads hosts and their facts, and a GitOps connector
lists the objects it manages and applies one change at a time.

---

## ✨ Features

- **Two kinds of connector**: inventory reads hosts; GitOps lists objects and applies changes
- **One system, one script**: plus the shared helpers it imports; nothing to install per system
- **No secrets in git**: addresses and credentials come from the service's config, in the environment
- **Read-only unless asked**: a gitops connector writes only on `apply`, when writing is allowed
- **JSON in, JSON out**: data on stdout, messages on stderr, exit 0 only with a whole document
- **Interruptible**: a run past its timeout is killed, so a script never leaves half-written state
- **Self-describing**: a gitops connector says which kinds it manages, their key and what goes first
- **Pulled, not packaged**: a service checks this repository out as a project; no image rebuild

---

## 🔌 Connectors

| Folder | System | Inventory | GitOps |
| ------ | ------ | :-------: | :----: |
| [`centreon`](centreon) | Centreon monitored hosts and their services | ✓ | |
| [`cisco_ucs`](cisco_ucs) | Cisco UCS Manager blades | ✓ | |
| [`dell_avamar`](dell_avamar) | Dell Avamar backup clients and their backups | ✓ | |
| [`device42`](device42) | Device42 devices, through DOQL | ✓ | |
| [`infinidat_infinibox`](infinidat_infinibox) | InfiniBox hosts and their volumes | ✓ | |
| [`kv`](kv) | Key-value store in a folder (example) | | ✓ |
| [`portainer`](portainer) | Portainer settings, environments and users | | ✓ |
| [`pure_flasharray`](pure_flasharray) | Pure Storage FlashArray hosts and their volumes | ✓ | |
| [`vmware_vcenter`](vmware_vcenter) | vCenter virtual machines | ✓ | |

Every inventory connector is a single file that imports nothing from this repository: copy it
anywhere, or point any tool that runs scripts at it in a checkout, Ascender included. To run it
on its own, set the source's settings as JSON in `SOURCE_CONFIG` and the credentials as the variables its README
lists, for example `UCS_<TAG>_BASE_URL`, `_USERNAME` and `_PASSWORD` from a credential type.
`vmware_vcenter` needs pyvmomi; `device42` uses PyYAML when its filter files are YAML.

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
