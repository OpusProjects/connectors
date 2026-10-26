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

## 🤝 Contributing

Contributions are welcome: [CONTRIBUTING.md](CONTRIBUTING.md) covers the PR workflow, commit style and the rules a connector follows.

## 📄 License

[Apache 2.0](LICENSE)
