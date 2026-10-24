"""HTTP-facing study API surface (DESIGN §9). Core must not import this package."""

from coscientist.api.studies import CreateStudyBody, post_studies

__all__ = ["CreateStudyBody", "post_studies"]
# A2A / MCP facades: import coscientist.api.a2a / coscientist.api.mcp_facade
# (optional; gated by ExternalMasConfig — DESIGN §6.5).
