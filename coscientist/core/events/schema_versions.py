"""Journal schema version constants (writers must not hard-code literals)."""

from __future__ import annotations

# Re-export StudyCreated writer version (production collapse → v1).
from coscientist.core.events.events import STUDY_CREATED_SCHEMA_VERSION as STUDY_CREATED_SCHEMA_VERSION

# AttemptStarted: production writer v1 (collapsed).
ATTEMPT_STARTED_SCHEMA_VERSION = 1

# ArtifactRegistered payload (CAS uri + optional client source_uri).
ARTIFACT_REGISTERED_SCHEMA_VERSION = 1

# InputBinding digest formula version (PR19 JCS rows). Absent/old → drift unknown.
INPUTS_DIGEST_VERSION = 1
