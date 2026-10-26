# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the
project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- The connector SDK (`unified_connector.py`) and the `kv` example gitops connector.
- One folder per system (`vendor_product`), with its README, example and `tests/`; CI runs ruff, pytest and a check that nothing identifies where a connector runs.
