"""Journal schema version constants (writers must not hard-code literals)."""

from __future__ import annotations

# Re-export StudyCreated writer version (#23 = v11 after #24's v10 max_loop_iter).
from coscientist.core.events.events import STUDY_CREATED_SCHEMA_VERSION as STUDY_CREATED_SCHEMA_VERSION

# AttemptStarted: #23 inputs_digest (v8); PR20 attempt_role (v9); R16a provenance (v10);
# v11 = omitted forever-fields (except provenance) → AttemptUnrecorded (no invent).
ATTEMPT_STARTED_SCHEMA_VERSION = 11

# ArtifactRegistered payload (CAS uri + optional client source_uri).
ARTIFACT_REGISTERED_SCHEMA_VERSION = 1

TOOL_RESOLVED_SCHEMA_VERSION = 1

# MCP-J1 McpRequestRecorded / McpResponseRecorded — shared until an upcast lands.
MCP_EVENTS_SCHEMA_VERSION = 1
TOOL_CALLED_SCHEMA_VERSION = 1

# RC9c HypothesisProposed — writer version until an upcast lands.
HYPOTHESIS_PROPOSED_SCHEMA_VERSION = 1

# InputBinding digest formula version (PR19 JCS rows). Absent/old → drift unknown.
INPUTS_DIGEST_VERSION = 1

# Job* events (DESIGN §6.4) — shared writer version until an upcast lands.
JOB_EVENTS_SCHEMA_VERSION = 1
