"""Id generation (core-owned prefixes; never from LLM)."""

from __future__ import annotations

import re
import secrets
import time

from coscientist.core.events.digest import sha256_hex

# study_id() body: 11 hex ms + token_hex(8); idem: st_idem_ + 24 hex of key digest.
_STUDY_ULID_HEX_LEN = 11 + 16
_IDEM_STUDY_HEX_LEN = 24
STUDY_ID_RE = re.compile(
    rf"^st_(?:[0-9a-f]{{{_STUDY_ULID_HEX_LEN}}}"
    rf"|idem_[0-9a-f]{{{_IDEM_STUDY_HEX_LEN}}})$"
)


def _ulid_like() -> str:
    """Monotonic-ish id without an external ulid dependency."""
    ms = int(time.time() * 1000)
    rand = secrets.token_hex(8)
    return f"{ms:011x}{rand}"


def is_study_id(value: str) -> bool:
    """True iff ``value`` matches ``study_id()`` / ``study_id_from_idempotency_key``."""
    return STUDY_ID_RE.fullmatch(value) is not None


def study_id() -> str:
    """``st_`` + 27 hex (11 ms + 16 rand). Must satisfy ``is_study_id``."""
    body = _ulid_like()
    if len(body) != _STUDY_ULID_HEX_LEN:
        raise RuntimeError(f"study_id body length {len(body)} != {_STUDY_ULID_HEX_LEN}")
    sid = f"st_{body}"
    if not is_study_id(sid):
        raise RuntimeError("study_id() drifted from STUDY_ID_RE")
    return sid


def study_id_from_idempotency_key(key: str) -> str:
    """Deterministic ``st_idem_<24 hex>`` from the client Idempotency-Key."""
    digest = sha256_hex(f"Idempotency-Key:{key}")
    sid = f"st_idem_{digest[:_IDEM_STUDY_HEX_LEN]}"
    if not is_study_id(sid):
        raise RuntimeError("study_id_from_idempotency_key drifted from STUDY_ID_RE")
    return sid


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
