"""Coder backend: JobStore + ``run_sandbox`` (DESIGN §6.4 / CAPABILITIES #11)."""

from coscientist.tools.coder.attach import (
    CODER_TOOL,
    CoderTool,
    invoke_coder_for_study,
    require_supported_linux_sandbox,
)
from coscientist.tools.coder.run import (
    CoderOutcome,
    CoderOutputSpec,
    CoderRequest,
    run_coder_job,
    sandbox_status_to_coder,
)

__all__ = [
    "CODER_TOOL",
    "CoderOutcome",
    "CoderOutputSpec",
    "CoderRequest",
    "CoderTool",
    "invoke_coder_for_study",
    "require_supported_linux_sandbox",
    "run_coder_job",
    "sandbox_status_to_coder",
]
