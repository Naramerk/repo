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


def plan_id() -> str:
    return f"pl_{_ulid_like()}"


def conclusion_id() -> str:
    """Random conclusion id (prefer finalize_nodes.conclusion_id_for_*)."""
    return f"cn_{_ulid_like()}"


def report_node_id() -> str:
    """Random report node id (prefer finalize_nodes.report_node_id_for)."""
    return f"rp_{_ulid_like()}"
