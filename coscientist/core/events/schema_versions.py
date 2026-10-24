"""Journal schema version constants (writers must not hard-code literals)."""

from __future__ import annotations

# Re-export StudyCreated writer version (#23 = v11 after #24's v10 max_loop_iter).
from coscientist.core.events.events import STUDY_CREATED_SCHEMA_VERSION as STUDY_CREATED_SCHEMA_VERSION

# AttemptStarted: #23 inputs_digest (v8); PR20 attempt_role (v9).
ATTEMPT_STARTED_SCHEMA_VERSION = 9

# ArtifactRegistered payload (CAS uri + optional client source_uri).
ARTIFACT_REGISTERED_SCHEMA_VERSION = 1

# InputBinding digest formula version (PR19 JCS rows). Absent/old → drift unknown.
INPUTS_DIGEST_VERSION = 1

# M2 agent-layer events (new types start at v1; no upcasters).
LLM_CALL_RECORDED_SCHEMA_VERSION = 1
USAGE_RECORDED_SCHEMA_VERSION = 1
TOOL_CALLED_SCHEMA_VERSION = 1
MCP_REQUEST_RECORDED_SCHEMA_VERSION = 1
MCP_RESPONSE_RECORDED_SCHEMA_VERSION = 1
PLAN_EXPANSION_REQUESTED_SCHEMA_VERSION = 1
