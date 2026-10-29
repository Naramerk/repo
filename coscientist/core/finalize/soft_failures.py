"""Explicit fail-open / degrade exception set for finalize totality paths.

Soft failures never sink StudyFinalized / tick / recover. ``LeaseLost`` and
other hard control-flow errors are always re-raised by callers first.
Not ``except Exception``.
"""

from __future__ import annotations

from pydantic import ValidationError

from coscientist.core.events.errors import CoreError

FINALIZE_SOFT_FAILURES: tuple[type[BaseException], ...] = (
    OSError,
    UnicodeError,
    TypeError,
    ValueError,
    AttributeError,
    KeyError,
    IndexError,
    ArithmeticError,
    RecursionError,
    RuntimeError,
    ValidationError,
    CoreError,
)
