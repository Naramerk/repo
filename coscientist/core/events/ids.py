"""Id generation (core-owned prefixes; never from LLM)."""

from __future__ import annotations

import secrets
import time


def _ulid_like() -> str:
    """Monotonic-ish id without an external ulid dependency."""
    ms = int(time.time() * 1000)
    rand = secrets.token_hex(8)
    return f"{ms:011x}{rand}"


def study_id() -> str:
    return f"st_{_ulid_like()}"


def task_id(study: str, task_key: str) -> str:
    return f"{study}/{task_key}"


def attempt_id(tid: str, n: int) -> str:
    return f"{tid}#{n}"


def result_id(aid: str) -> str:
    return f"{aid}:result"


def call_id(aid: str, seq: int) -> str:
    return f"{aid}:{seq}"


def hitl_request_id() -> str:
    return f"hitl_{_ulid_like()}"


def proposal_id() -> str:
    """Core-owned id for CheckProposed (never from the agent)."""
    return f"prop_{_ulid_like()}"


def plan_id() -> str:
    return f"pl_{_ulid_like()}"


def experiment_node_id(study_id: str, task_key: str) -> str:
    """Stable graph node id for an experiment task (``{study}:experiment:{key}``)."""
    return f"{study_id}:experiment:{task_key}"


def claim_node_id(study_id: str, claim_key: str) -> str:
    """Stable graph node id for a claim (``{study}:claim:{key}``)."""
    return f"{study_id}:claim:{claim_key}"


def question_node_id(study_id: str, question_id: str) -> str:
    """Stable graph node id for a question (``{study}:question:{id}``)."""
    return f"{study_id}:question:{question_id}"


def hypothesis_node_id(study_id: str, hypothesis_id: str) -> str:
    """Stable graph node id for a hypothesis (``{study}:hypothesis:{id}``)."""
    return f"{study_id}:hypothesis:{hypothesis_id}"


def study_node_id(study_id: str) -> str:
    return f"{study_id}:study"


def criterion_node_id(study_id: str, criterion_key: str) -> str:
    """Stable graph node id for a criterion (``{study}:criterion:{key}``)."""
    return f"{study_id}:criterion:{criterion_key}"
