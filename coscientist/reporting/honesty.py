"""Honesty gates for graph-sourced documents (report lane).

Two hard rules, both enforced post-generation by :func:`validate_prose`:

1. A number may appear in generated prose only if it exists in the graph
   digest shown to the model (numbers in, numbers out — nothing invented).
2. Internal identifiers (``H1``, ``TASK-3``, ``cn_ab12…``), agent names and
   tracker statuses never enter document text. Letter+digit tokens that DO
   exist in the digest (``LD50``, ``ECFP4`` — names inside graph facts) are
   legitimate; only ones the model invents are violations.

A violation is a typed :class:`ReportHonestyRefuse`; the CLI lane runs one
bounded repair round before the refuse fires.
"""

from __future__ import annotations

import re

from coscientist.reporting.errors import ReportingError

# A numeric token: digits with an optional single decimal part, NOT glued to
# word characters, dots or hyphens — ``H1``, ``LD50``, ``7.32-2017``,
# ``file.png`` are identifiers/standard numbers, not numeric claims. The
# trailing ``\.\d`` alternative blocks the backtrack that would let the "7" of
# "7.32-2017" validate on its own. Sentence-final digits ("составляет 5.")
# still validate.
_NUM_TOKEN_RE = re.compile(r"(?<![\w.\-])(\d+(?:[.,]\d+)?)(?![\w\-]|\.\d)")

# Markdown ordered-list markers are structure, not data ("1. пункт").
_LIST_MARKER_RE = re.compile(r"^\s*\d{1,3}[.)]\s", re.MULTILINE)

# Identifier-shaped tokens: H1, CC2, E3, EXP-2, TASK-3, LD50, ECFP4 …
_ID_TOKEN_RE = re.compile(r"\b[A-Z]{1,8}-?\d{1,8}\b")
# Journal hex ids: cn_/rp_/art_/st_ + long hex body.
_HEX_ID_RE = re.compile(r"\b[a-z]{1,6}_[0-9a-f]{8,}\b")

# Agent names / route words / tracker statuses — forbidden absolutely (the
# digest never contains them; facts are translated to Russian).
_FORBIDDEN_RE = re.compile(
    r"\b(?:"
    r"planner|executor|executor-route|tz_spec|hypothesizer|researcher|"
    r"critic|reviewer|coder|adapter|journal|projection|"
    r"PENDING|RUNNING|DONE|SKIPPED|SUCCESS|PARTIAL|FAILED|"
    r"inconclusive|refuted|postponed|untested|supported"
    r")\b"
)


class ReportHonestyRefuse(ReportingError):
    """Typed refuse: prose violates the honesty contract (numbers/ids)."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


def _normalize_number(raw: str) -> str:
    normalized = raw.replace(",", ".")
    if "." in normalized:
        stripped = normalized.strip("0.")
        if not stripped:
            return "0"
        return normalized.rstrip("0").rstrip(".")
    stripped = normalized.lstrip("0")
    return stripped if stripped else "0"


def extract_number_tokens(text: str) -> frozenset[str]:
    """Numeric claim tokens in ``text`` (normalized; identifiers excluded)."""
    cleaned = _LIST_MARKER_RE.sub("", text)
    return frozenset(_normalize_number(m.group(1)) for m in _NUM_TOKEN_RE.finditer(cleaned))


def extract_id_tokens(text: str) -> frozenset[str]:
    """Identifier-shaped uppercase tokens in ``text`` (``H1``, ``LD50`` …)."""
    return frozenset(m.group(0) for m in _ID_TOKEN_RE.finditer(text))


class DigestFacts:
    """What the model saw: every number and id-shaped token of the digest."""

    def __init__(self, digest_text: str) -> None:
        self._numbers = extract_number_tokens(digest_text)
        self._ids = extract_id_tokens(digest_text)

    def untraceable_numbers(self, text: str) -> tuple[str, ...]:
        missing = sorted(extract_number_tokens(text) - self._numbers)
        return tuple(missing)

    def invented_ids(self, text: str) -> tuple[str, ...]:
        found = extract_id_tokens(text) - self._ids
        return tuple(sorted(found))

    def hex_ids(self, text: str) -> tuple[str, ...]:
        return tuple(sorted({m.group(0) for m in _HEX_ID_RE.finditer(text)}))

    def forbidden_words(self, text: str) -> tuple[str, ...]:
        return tuple(sorted({m.group(0) for m in _FORBIDDEN_RE.finditer(text)}))


def validate_prose(text: str, facts: DigestFacts) -> tuple[str, ...]:
    """Violation lines for ``text``; empty tuple means the prose is honest."""
    violations: list[str] = []
    numbers = facts.untraceable_numbers(text)
    if numbers:
        violations.append("числа вне графа (нет в дайджесте): " + ", ".join(numbers))
    ids = facts.invented_ids(text)
    if ids:
        violations.append("внутренние обозначения (нет в дайджесте): " + ", ".join(ids))
    hex_ids = facts.hex_ids(text)
    if hex_ids:
        violations.append("служебные идентификаторы журнала: " + ", ".join(hex_ids))
    words = facts.forbidden_words(text)
    if words:
        violations.append("имена агентов/статусы трекера (пишите по-русски): " + ", ".join(words))
    return tuple(violations)


__all__ = [
    "DigestFacts",
    "ReportHonestyRefuse",
    "extract_id_tokens",
    "extract_number_tokens",
    "validate_prose",
]
