"""Typed errors for optional external MAS integrations (no string heuristics)."""

from __future__ import annotations


class IntegrationError(Exception):
    """Base class for external-MAS integration failures."""


class IntegrationDisabled(IntegrationError):
    """Requested integration name is absent from ``enabled_integrations``."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"integration_disabled:{name}")


class IntegrationConfigError(IntegrationError):
    """Config invalid or incomplete (fail closed; never invent defaults)."""


class IntegrationProtocolError(IntegrationError):
    """Peer message violated the expected A2A/MCP shape."""


class IntegrationTransportError(IntegrationError):
    """Transport failed before a usable peer response was obtained."""

    def __init__(self, detail: str, *, retryable: bool) -> None:
        self.detail = detail
        self.retryable = retryable
        super().__init__(detail)
