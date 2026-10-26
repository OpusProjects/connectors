# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- The connector SDK (the `unified_connectors` package) and the `kv` example gitops connector.
- Inventory connectors are standalone files: each carries the settings and credential helpers (settings from the source's config as strings or JSON, per-system credential sets), checked in CI against one reference copy.
- `centreon`: Centreon monitored hosts as an Ansible inventory, with their services when asked.
- `cisco_ucs`: Cisco UCS Manager blades as an Ansible inventory, servers and spares apart.
- `dell_avamar`: Dell Avamar backup clients as an Ansible inventory, with their recent backups.
- `device42`: Device42 devices as an Ansible inventory, selected and grouped by DOQL filter files.
- `infinidat_infinibox`: InfiniBox hosts as an Ansible inventory, with their mapped volumes.
- `pure_flasharray`: Pure Storage FlashArray hosts as an Ansible inventory, with their volumes.
- `vmware_vcenter`: vCenter virtual machines as an Ansible inventory, through the PropertyCollector (needs pyvmomi).
- One folder per system (`vendor_product`), with its README, example and `tests/`; CI runs ruff, pytest and a check that nothing identifies where a connector runs.
