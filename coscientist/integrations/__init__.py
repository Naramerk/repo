"""Optional external MAS integrations (DESIGN §6.5 / capability #21).

A2A facade, remote-agent Tool, and MCP module publishing. Gated by
``ExternalMasConfig.enabled_integrations`` — missing names → typed refusal.
Depends on narrow Protocols; production wires API/journal; tests inject fakes.
"""

from coscientist.integrations.a2a_facade import (
    A2A_INTEGRATION_NAME,
    A2ACreateTaskRequest,
    A2AFacade,
    A2ATaskCreated,
)
from coscientist.integrations.config import (
    A2AAgentCardConfig,
    A2AFacadeConfig,
    A2ARemoteToolConfig,
    ExternalMasConfig,
    McpPublishedModule,
    McpServerFacadeConfig,
)
from coscientist.integrations.errors import (
    IntegrationConfigError,
    IntegrationDisabled,
    IntegrationError,
    IntegrationProtocolError,
    IntegrationTransportError,
)
from coscientist.integrations.mcp_server import (
    MCP_SERVER_INTEGRATION_NAME,
    McpModuleServer,
)
from coscientist.integrations.ports import (
    A2AHttpTransport,
    A2ATransportResponse,
    JournalEventView,
    JournalStreamPort,
    ModuleHandler,
    StudyCreatePort,
)

__all__ = [
    "A2A_INTEGRATION_NAME",
    "A2AAgentCardConfig",
    "A2ACreateTaskRequest",
    "A2AFacade",
    "A2AFacadeConfig",
    "A2AHttpTransport",
    "A2ARemoteToolConfig",
    "A2ATaskCreated",
    "A2ATransportResponse",
    "ExternalMasConfig",
    "IntegrationConfigError",
    "IntegrationDisabled",
    "IntegrationError",
    "IntegrationProtocolError",
    "IntegrationTransportError",
    "JournalEventView",
    "JournalStreamPort",
    "MCP_SERVER_INTEGRATION_NAME",
    "McpModuleServer",
    "McpPublishedModule",
    "McpServerFacadeConfig",
    "ModuleHandler",
    "StudyCreatePort",
]
