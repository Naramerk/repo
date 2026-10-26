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

# A numeric token: optional sign + digits with an optional single decimal
# part, NOT glued to word characters, dots or hyphens. The strict lookbehind
# keeps ``7.32-2017`` / ``2017-2020`` tails out (their hyphen follows a word
# char), while the sign INSIDE the capture lets a true minus (hyphen after
# space/start) form the token: fabricated ``-9.9`` refuses, honest ``-5.2``
# from the digest passes. The atomic dotted branch (``1.2.3``+) is matched
# whole or not at all — partials like ``1.2`` can never leak; such tokens
# require verbatim digest presence. Sentence-final digits ("составляет 5.")
# still validate.
_NUM_TOKEN_RE = re.compile(
    r"(?<![\w.\-])(?>(\d+(?:\.\d+){2,})|(-?\d+(?:[.,]\d+)?))(?![\w\-]|\.\d)"
)

# Markdown ordered lists are forbidden by the report-lane prompts («только
# маркированные „-"»), so NO list-marker stripping happens here: every
# line-initial number is checked as data. A structural slip («1. …») passes
# only because the digest always carries «1» in entity labels; a fabricated
# «42. …» refuses like any other number — nothing line-initial can hide.


def _normalize_number(raw: str) -> str:
    normalized = raw.replace(",", ".")
    if "." in normalized:
        stripped = normalized.strip("0.")
        if not stripped:
            return "0"
        trimmed = normalized.rstrip("0").rstrip(".")
        if not trimmed:
            return "0"
        return trimmed
    stripped = normalized.lstrip("0")
    if not stripped:
        return "0"
    return stripped


def extract_number_tokens(text: str) -> frozenset[str]:
    """Numeric claim tokens in ``text`` (normalized; identifiers excluded)."""
    return frozenset(
        _normalize_number(m.group(1) or m.group(2))
        for m in _NUM_TOKEN_RE.finditer(text)
    )


# Identifier-shaped tokens: H1, CC2, E3, EXP-2, TASK-3, LD50, ECFP4 …
_ID_TOKEN_RE = re.compile(r"\b[A-Z]{1,8}-?\d{1,8}\b")
# Journal hex ids: cn_/rp_/art_/st_ + long hex body.
_HEX_ID_RE = re.compile(r"\b[a-z]{1,6}_[0-9a-f]{8,}\b")

# Agent names / route words / tracker statuses — forbidden absolutely (the
# digest never contains them; facts are translated to Russian). Case-
# insensitive so REFUTED/Success/Done cannot slip through in upper case.
_FORBIDDEN_RE = re.compile(
    r"\b(?:"
    r"executor-route|tz_spec|"
    r"planner|executor|hypothesizer|researcher|"
    r"critic|reviewer|coder|adapter|journal|projection|"
    r"pending|running|done|skipped|success|partial|failed|"
    r"inconclusive|refuted|postponed|untested|supported"
    r")\b",
    re.IGNORECASE,
)


class ReportHonestyRefuse(ReportingError):
    """Typed refuse: prose violates the honesty contract (numbers/ids)."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


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
