"""Study threshold bounds — single source for Field(ge=…) and refuse paths.

``Study`` / ``CreateStudyBody`` use these constants in ``Field(ge=…)``.
Upcasters and ``hypothesis_verdict`` call ``require_*`` so ge/type rules are
not duplicated as literals.
"""

from __future__ import annotations

from coscientist.core.events.errors import CatalogError

CONFIRMATIONS_NEEDED_GE = 1
REFUTATIONS_NEEDED_GE = 1
SUPPORTED_NEEDED_GE = 2
MAX_EXPANSIONS_GE = 0
MAX_ATTEMPTS_GE = 1


def require_threshold_int(
    value: object,
    *,
    field: str,
    ge: int,
    code_prefix: str,
) -> int:
    """Exact int (not bool), ``>= ge``.

    Callers that need a present key use ``require_present`` first.
    ``None`` is refused as wrong type (``must_be_int_got_NoneType``), not
    ``*_required``.
    """
    if type(value) is not int:
        raise CatalogError(
            _code(
                code_prefix,
                field,
                f"must_be_int_got_{type(value).__name__}",
            )
        )
    if value < ge:
        raise CatalogError(_code(code_prefix, field, f"must_be_ge_{ge}"))
    return value


def require_threshold_bool(
    value: object,
    *,
    field: str,
    code_prefix: str,
) -> bool:
    """Exact bool (not int).

    Callers that need a present key use ``require_present`` first.
    ``None`` is refused as wrong type (``must_be_bool_got_NoneType``).
    """
    if type(value) is not bool:
        raise CatalogError(
            _code(
                code_prefix,
                field,
                f"must_be_bool_got_{type(value).__name__}",
            )
        )
    return value


def require_present(payload: dict[str, object], field: str, *, code_prefix: str) -> object:
    """Key present and not None; else ``{prefix:}field_required``."""
    if field not in payload or payload[field] is None:
        raise CatalogError(_code(code_prefix, field, "required"))
    return payload[field]


def _code(prefix: str, field: str, kind: str) -> str:
    """Single refuse-code format: ``{prefix:}{field}_required`` / ``{prefix:}{field}_{kind}``.

    Empty ``prefix`` → no colon (verdict style): ``refutations_needed_must_be_int_got_bool``.
    Upcasters: ``StudyCreated.v3:refutations_needed_must_be_int_got_bool``.
    """
    if kind == "required":
        body = f"{field}_required"
    else:
        body = f"{field}_{kind}"
    if prefix:
        return f"{prefix}:{body}"
    return body
