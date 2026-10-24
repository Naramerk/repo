"""HTTP/MCP adapters for optional external services (DESIGN §6.5, ≤300 lines each)."""

from coscientist.tools.integrations.a2a_remote import (
    A2A_REMOTE_INTEGRATION_NAME,
    RemoteA2AAgentTool,
)

__all__ = [
    "A2A_REMOTE_INTEGRATION_NAME",
    "RemoteA2AAgentTool",
]
