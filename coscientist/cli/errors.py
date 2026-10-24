"""Typed CLI refuse (non-zero exit; no silent skip)."""

from __future__ import annotations


class CliRefuse(Exception):
    """Orchestrator refused to continue — caller maps ``code`` to process exit."""

    def __init__(self, detail: str, *, code: int) -> None:
        if type(code) is not int or code == 0:
            raise ValueError("CliRefuse.code must be a non-zero int")
        self.detail = detail
        self.code = code
        super().__init__(detail)
