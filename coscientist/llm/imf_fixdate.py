"""Strict RFC 7231 IMF-fixdate parser (locale-independent, GMT required)."""

from __future__ import annotations

from datetime import datetime, timezone

from coscientist.llm.errors import LlmRetryAfterError

# RFC 7231 §7.1.1.1 day-name / month protocol constants (not config).
_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_DAY_INDEX = {name: i for i, name in enumerate(_DAY_NAMES)}
_MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}


def parse_imf_fixdate(raw: str) -> datetime:
    """Parse RFC 7231 IMF-fixdate only (``day-name, date1 time-of-day GMT``).

    Rejects RFC 850 and asctime forms. Zone must be exactly ``GMT`` (not ``gmt``).
    Seconds must be 00–59 (leap second ``60`` is rejected).
    """
    parts = raw.split(" ")
    if len(parts) != 6:
        raise LlmRetryAfterError("Retry-After IMF-fixdate is invalid")
    day_name_comma, day_s, month_s, year_s, time_s, zone = parts
    if not day_name_comma.endswith(","):
        raise LlmRetryAfterError("Retry-After IMF-fixdate is invalid")
    day_name = day_name_comma[:-1]
    if day_name not in _DAY_INDEX:
        raise LlmRetryAfterError("Retry-After IMF-fixdate is invalid")
    if zone != "GMT":
        raise LlmRetryAfterError("Retry-After IMF-fixdate must use GMT")
    if len(day_s) != 2 or not _ascii_digits(day_s):
        raise LlmRetryAfterError("Retry-After IMF-fixdate is invalid")
    if month_s not in _MONTHS:
        raise LlmRetryAfterError("Retry-After IMF-fixdate is invalid")
    if len(year_s) != 4 or not _ascii_digits(year_s):
        raise LlmRetryAfterError("Retry-After IMF-fixdate is invalid")
    time_parts = time_s.split(":")
    if len(time_parts) != 3:
        raise LlmRetryAfterError("Retry-After IMF-fixdate is invalid")
    hour_s, minute_s, second_s = time_parts
    for piece in (hour_s, minute_s, second_s):
        if len(piece) != 2 or not _ascii_digits(piece):
            raise LlmRetryAfterError("Retry-After IMF-fixdate is invalid")
    second = int(second_s)
    if second > 59:
        raise LlmRetryAfterError("Retry-After IMF-fixdate is invalid")
    try:
        when = datetime(
            int(year_s),
            _MONTHS[month_s],
            int(day_s),
            int(hour_s),
            int(minute_s),
            second,
            tzinfo=timezone.utc,
        )
    except ValueError as exc:
        raise LlmRetryAfterError("Retry-After IMF-fixdate is invalid") from exc
    if when.weekday() != _DAY_INDEX[day_name]:
        raise LlmRetryAfterError("Retry-After IMF-fixdate day-name mismatch")
    return when


def _ascii_digits(value: str) -> bool:
    return value.isascii() and value.isdigit() and value != ""
