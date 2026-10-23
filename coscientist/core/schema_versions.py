"""Journal schema version constants (writers must not hard-code literals)."""

from __future__ import annotations

# Re-export StudyCreated writer version (#23 = v11 after #24's v10 max_loop_iter).
from coscientist.core.events import STUDY_CREATED_SCHEMA_VERSION as STUDY_CREATED_SCHEMA_VERSION

# AttemptStarted: PR19 inputs_digest + binding snapshot fields (v8).
ATTEMPT_STARTED_SCHEMA_VERSION = 8

# ArtifactRegistered payload (CAS uri + optional client source_uri).
ARTIFACT_REGISTERED_SCHEMA_VERSION = 1

# InputBinding digest formula version (PR19 JCS rows). Absent/old → drift unknown.
INPUTS_DIGEST_VERSION = 1
