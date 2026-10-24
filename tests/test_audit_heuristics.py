"""Unit tests for scripts/audit_heuristics.py anti-template rules."""

from __future__ import annotations

from pathlib import Path

import scripts.audit_heuristics as audit


def _write(tmp: Path, name: str, src: str) -> Path:
    p = tmp / name
    p.write_text(src, encoding="utf-8")
    return p


def test_repeated_reason_rejected(tmp_path: Path) -> None:
    # Four identical reasons → repeated_reason (max 3).
    src = "def f():\n" + "\n".join(
        f"    try:\n        x={i}\n    except Exception:\n"
        f"        return None  # audit-ok[except_silent_return]: same reason text"
        for i in range(4)
    )
    p = _write(tmp_path, "rep.py", src)
    assert audit.main([str(p)]) == 1


def test_duplicate_marker_on_line_rejected(tmp_path: Path) -> None:
    src = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception:  # audit-ok[broad_except]: a  # audit-ok[broad_except]: b\n"
        "        raise\n"
    )
    p = _write(tmp_path, "dup.py", src)
    hits, _ = audit.audit_file(p)
    assert any(h.rule == "duplicate_audit_ok" for h in hits)


def test_marker_in_string_literal_rejected(tmp_path: Path) -> None:
    src = 'MSG = "audit-ok[broad_except]: fake"\n'
    p = _write(tmp_path, "lit.py", src)
    hits, _ = audit.audit_file(p)
    assert any(h.rule == "marker_in_literal" for h in hits)


def test_broad_except_marker_must_be_on_except_line(tmp_path: Path) -> None:
    src = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception:\n"
        "        x = 1  # audit-ok[broad_except]: wrong line\n"
    )
    p = _write(tmp_path, "be.py", src)
    hits, markers = audit.audit_file(p)
    assert any(h.rule == "broad_except" for h in hits)
    assert any(h.rule == "orphan_marker" for h in hits)


def test_hand_marker_suppresses_broad_except(tmp_path: Path) -> None:
    src = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception:  # audit-ok[broad_except]: re-raise path after cleanup\n"
        "        raise\n"
    )
    p = _write(tmp_path, "ok.py", src)
    hits, markers = audit.audit_file(p)
    assert markers
    assert not any(h.rule == "broad_except" for h in hits)


def test_list_rules_exits_zero() -> None:
    assert audit.main(["--list-rules"]) == 0
