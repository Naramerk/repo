"""Private attempt-frame ContextVar (PR19).

Only ``executor`` may push/reset. ``attempt_active()`` is readable by the
ingest gate so ``put_user`` cannot run mid-attempt. import-linter forbids
other packages from importing this module.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_attempt_active: ContextVar[bool] = ContextVar("attempt_active", default=False)


def push_attempt_active(value: bool = True) -> Token[bool]:
    return _attempt_active.set(bool(value))


def reset_attempt_active(token: Token[bool]) -> None:
    _attempt_active.reset(token)


def attempt_active() -> bool:
    return bool(_attempt_active.get())
