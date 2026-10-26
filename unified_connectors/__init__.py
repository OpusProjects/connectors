"""The shared SDK for gitops connectors; the code is in unified_connectors.py."""

from .unified_connectors import PROTOCOL, ConnectorError, run

__all__ = ["PROTOCOL", "ConnectorError", "run"]
