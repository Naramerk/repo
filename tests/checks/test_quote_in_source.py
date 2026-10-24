"""M1 PR14 review fixes: SourceFetched capability + quote matching rules."""

from __future__ import annotations

import json
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coscientist.checks.catalog import load_catalog
from coscientist.checks.evaluate import evaluate_code_check, evaluate_task_checks
from coscientist.checks.kinds.quote import (
    MIN_QUOTE_WORD_TOKENS,
    NORMALIZATION_VERSION,
    normalize_for_quote_match,
    quote_digest,
)
from coscientist.checks.kinds.quote_norm import (
    MIN_QUOTE_CHARS_NOSPACE,
    normalize_variants,
    quote_in_normalized_source,
    token_boundary_match,
)
from coscientist.core.events.digest import payload_digest
from coscientist.core.events.errors import ArtifactRejected
from coscientist.core.execution.artifacts import LocalArtifactStore, make_tool_ref
from coscientist.core.execution.domain import (
    AgentTaskOutput,
    ArtifactHandle,
    CodeCheck,
    HitlPolicy,
    Outcome,
    PlanRole,
    ReqStatus,
    RequirementRef,
    SourceQuote,
    TaskSpec,
)
from coscientist.core.execution.source_fetch import store_fetched_source
from coscientist.core.execution.tool_registry import (
    RegisteredToolSpec,
    _register_tool_for_tests,
    _reset_registry_for_tests,
    freeze_registry,
)
from coscientist.tools.contract import CallCtx, ToolOutput, ToolSpec
from coscientist.tools.effects import (
    _mint_fetch_capability_for_tests,
    invoke_tool,
)
from tests.helpers import attach_executor, close_runtime, open_runtime, record_success


def _cap(
    call: str = "fetch:1",
    tool: str = "papers_get",
    *,
    study_id: str = "st_test",
):
    return _mint_fetch_capability_for_tests(
        tool=tool, call_id=call, study_id=study_id
    )


def _put_paper(
    arts: LocalArtifactStore,
    content: bytes,
    *,
    call: str = "f",
    tool: str = "papers_get",
    study_id: str = "st_test",
):
    from coscientist.tools.effects import _invalidate_fetch_capability

    cap = _cap(call, tool, study_id=study_id)
    attempt_id = call.rsplit(":", 1)[0] if ":" in call else call
    try:
        return arts.put_fetched(
            content,
            make_tool_ref(call, tool=tool),
            name="paper.txt",
            media_type="text/plain",
            role="paper",
            capability=cap,
            expected_tool=tool,
            expected_call_id=call,
            expected_study_id=study_id,
            expected_attempt_id=attempt_id,
        )
    finally:
        # Unit helper: put only asserts (no consume) — drop the test mint.
        _invalidate_fetch_capability(cap)


def _catalog_quote() -> object:
    return load_catalog(
        {
            "catalog_version": "0.1.0-pr14",
            "edition": "pr14",
            "sources_sha256": {},
            "rules": [
                {
                    "rule_id": "R11",
                    "source": "reviewed-rules",
                    "excerpt": "evidence locator",
                    "digest": "abc",
                }
            ],
            "templates": [
                {
                    "id": "quote_ok",
                    "title": "Quote in source",
                    "text_ru": "Цитата есть в тексте источника.",
                    "rule_ids": ["R11"],
                    "method_sources": ["test"],
                    "applies_to": ["experiment"],
                    "check": {"kind": "quote_in_source", "output": "quote"},
                    "role": "validity",
                    "kind": "catalog",
                    "required": True,
                    "version": "1",
                    "uses_sesoi": False,
                    "predicts_null": False,
                    "predicted_direction": "two_sided",
                }
            ],
            "defaults": {"experiment": ["quote_ok"]},
        }
    )


def _put_quote_json(arts: LocalArtifactStore, payload: dict, *, call: str):
    return arts.put(
        json.dumps(payload).encode("utf-8"),
        make_tool_ref(call, tool="test"),
        name="quote.json",
        media_type="application/json",
    )


def _quote_payload(paper, quote: str, **extra) -> dict:
    return {
        "artifact_id": paper.artifact_id,
        "locator": "p.1",
        "quote": quote,
        "digest": quote_digest(quote),
        "source_sha256": paper.sha256,
        "normalization_version": NORMALIZATION_VERSION,
        **extra,
    }


def _eval(arts, paper, quote: str, *, fetched=None, call="q1", **extra):
    payload = _quote_payload(paper, quote, **extra)
    qart = _put_quote_json(arts, payload, call=call)
    out = AgentTaskOutput(
        outputs={"quote": ArtifactHandle(artifact_id=qart.artifact_id, name="quote")}
    )
    digests = fetched if fetched is not None else frozenset({paper.sha256})
    return evaluate_code_check(
        CodeCheck(kind="quote_in_source", output="quote"),
        template_id="quote_ok",
        check_id="quote_ok#0",
        out=out,
        artifacts=arts,
        values={},
        role="validity",
        attempt_id=call,
        fetched_digests=digests,
    )


# --- normalization ---


def test_normalize_super_sub_before_nfkc():
    """BLOCKER 3: 10² must not become 102; map to ^2 before NFKC."""
    assert normalize_for_quote_match("10²") == "10^2"
    assert "102" not in normalize_for_quote_match("10²")
    assert normalize_for_quote_match("10⁻³") == "10^-3"
    assert normalize_for_quote_match("H₂O") == "h_2o"


def test_normalize_super_parens_and_letters():
    """MINOR: ⁽⁾ⁿⁱᵃ map before NFKC."""
    n = normalize_for_quote_match("x⁽ⁿⁱᵃ⁾")
    assert n == "x^(nia)"


def test_normalize_strips_joiners_and_bom():
    assert "abc" in normalize_for_quote_match("a\u200cb\u200dc\u2060\ufeff")


def test_normalize_letter_hyphen_join_variants():
    """MAJOR 1: join -\\n only between letters; try joined and kept."""
    joined, kept = normalize_variants("well-\nknown result here now")
    assert joined == "wellknown result here now"
    assert kept == "well-known result here now"
    j2, _k2 = normalize_variants("value -\n5 units more text")
    assert j2 == "value - 5 units more text"


def test_normalize_strips_soft_hyphen_zwsp():
    assert "abc" in normalize_for_quote_match("a\u00adb\u200bc")


def test_normalize_version_includes_unidata():
    assert unicodedata.unidata_version in NORMALIZATION_VERSION


def test_en_dash_range_indistinguishable():
    """MAJOR 5 doc: 5–10 and 5-10 collapse after dash unify."""
    assert normalize_for_quote_match("5–10") == normalize_for_quote_match("5-10")


def test_ellipsis_nfkc_to_three_dots():
    assert "..." in normalize_for_quote_match("wait… now")


def test_full_casefold_merges_met():
    """MINOR: full Unicode casefold — MET ≡ met (documented)."""
    assert normalize_for_quote_match("MET") == normalize_for_quote_match("met")


# --- token boundaries ---


def test_token_boundary_not_inside_cannot():
    """BLOCKER 2: 'not' must not match inside 'cannot'."""
    assert not token_boundary_match("not", "cannot")
    assert token_boundary_match("not", "do not go")


def test_token_boundary_percent_not_inside():
    """BLOCKER 2: '5%' must not match inside '15%'."""
    assert not token_boundary_match("5%", "rate 15% overall")
    assert token_boundary_match("5%", "only 5% survived")


def test_digit_edge_no_sign_or_decimal():
    """MAJOR: '5 mg' must not match inside '-5 mg' or '0.5 mg'."""
    assert not token_boundary_match("5 mg", "dose -5 mg daily")
    assert not token_boundary_match("5 mg", "dose 0.5 mg daily")
    assert token_boundary_match("5 mg", "dose 5 mg daily")
    assert not token_boundary_match("5", "15 mg")
    assert not token_boundary_match("5", "5.0 mg")


def test_quote_normalized_exact_match(tmp_path: Path):
    arts = LocalArtifactStore(tmp_path / "arts")
    paper = _put_paper(
        arts,
        ("Intro text here. The well-\nknown result was confirmed today.\nOutro.").encode(),
        call="fetch1",
    )
    quote = "the wellknown result was"
    cr = _eval(arts, paper, quote, fetched={paper.sha256}, call="q1")
    assert cr.status is ReqStatus.MET, cr.detail
    assert cr.effect is not None
    assert cr.effect["countable"] is True
    assert cr.effect["source_fetched"] is True
    assert cr.effect["trusted"] is False


def test_one_char_quote_not_met(tmp_path: Path):
    arts = LocalArtifactStore(tmp_path / "arts")
    paper = _put_paper(arts, b"a solitary letter appears here now")
    cr = _eval(arts, paper, "a", fetched={paper.sha256})
    assert cr.status is ReqStatus.FAILED
    assert cr.detail == "quote_too_short"


def test_empty_quote_rejected(tmp_path: Path):
    arts = LocalArtifactStore(tmp_path / "arts")
    paper = _put_paper(arts, b"Four word tokens appear right here now.")
    cr = _eval(arts, paper, "   ", fetched={paper.sha256})
    assert cr.status is ReqStatus.FAILED
    assert cr.detail == "quote_empty"


def test_source_too_large_rejected(tmp_path: Path):
    from coscientist.checks.kinds.quote_norm import MAX_SOURCE_BYTES

    arts = LocalArtifactStore(tmp_path / "arts")
    # Keep meta.size honest: put real oversized bytes (capped slightly over limit).
    body = ("word " * 20 + "\n").encode() + b"x" * (MAX_SOURCE_BYTES + 1)
    paper = _put_paper(arts, body)
    cr = _eval(
        arts,
        paper,
        "word word word word",
        fetched={paper.sha256},
    )
    assert cr.status is ReqStatus.FAILED
    assert cr.detail == "source_too_large"


def test_digits_only_quote_rejected(tmp_path: Path):
    arts = LocalArtifactStore(tmp_path / "arts")
    paper = _put_paper(arts, b"the rate was 5% in the trial")
    cr = _eval(arts, paper, "5%", fetched={paper.sha256})
    assert cr.status is ReqStatus.FAILED
    assert cr.detail == "quote_not_meaningful"


def test_negation_presence_not_polarity(tmp_path: Path):
    """MAJOR: quote_in_source checks presence, not surrounding polarity.

    Pin: quoting the affirmative span still MET when the source negates it.
    Agents must choose quotes that carry the intended claim.
    """
    arts = LocalArtifactStore(tmp_path / "arts")
    paper = _put_paper(
        arts,
        b"Overall the trial result was not positive under careful review today.",
    )
    quote = "the trial result was not positive"  # full span including negation → MET
    cr = _eval(arts, paper, quote, fetched={paper.sha256})
    assert cr.status is ReqStatus.MET, cr.detail

    # Affirmative substring without the surrounding "not" also MET (documented limit).
    quote_aff = "the trial result was"
    # need ≥4 tokens — extend
    quote_aff = "trial result was not positive"
    cr2 = _eval(arts, paper, quote_aff, fetched={paper.sha256}, call="q2")
    assert cr2.status is ReqStatus.MET, cr2.detail


def test_absent_quote_not_in_source(tmp_path: Path):
    """Quote text that does not appear fails (presence check)."""
    arts = LocalArtifactStore(tmp_path / "arts")
    paper = _put_paper(
        arts,
        b"The trial result was clearly positive overall today.",
    )
    quote = "the trial result was clearly negative"
    cr = _eval(arts, paper, quote, fetched={paper.sha256})
    assert cr.status is ReqStatus.FAILED
    assert cr.detail == "quote_not_in_source"


def test_min_tokens_documented():
    assert MIN_QUOTE_WORD_TOKENS == 4
    assert MIN_QUOTE_CHARS_NOSPACE == 8


def test_cjk_quote_char_min_no_word_boundary(tmp_path: Path):
    """MAJOR: CJK uses ≥8 chars and skips word-boundary."""
    arts = LocalArtifactStore(tmp_path / "arts")
    # 研究结果证实了这一假设成立 — 14 chars, no spaces
    src = "前言研究结果证实了这一假设成立结语"
    paper = _put_paper(arts, src.encode("utf-8"))
    quote = "研究结果证实了这一假设成立"
    assert len(normalize_for_quote_match(quote)) >= MIN_QUOTE_CHARS_NOSPACE
    cr = _eval(arts, paper, quote, fetched={paper.sha256})
    assert cr.status is ReqStatus.MET, cr.detail

    short = "研究"  # too short
    cr2 = _eval(arts, paper, short, fetched={paper.sha256}, call="q2")
    assert cr2.status is ReqStatus.FAILED
    assert cr2.detail == "quote_too_short"


def test_japanese_quote_char_min(tmp_path: Path):
    arts = LocalArtifactStore(tmp_path / "arts")
    src = "前書きこの結果は有意であった後書き"
    paper = _put_paper(arts, src.encode("utf-8"))
    quote = "この結果は有意であった"
    cr = _eval(arts, paper, quote, fetched={paper.sha256})
    assert cr.status is ReqStatus.MET, cr.detail


def test_thai_quote_char_min(tmp_path: Path):
    arts = LocalArtifactStore(tmp_path / "arts")
    # Thai without spaces between words
    src = "บทนำผลการทดลองยืนยันสมมติฐานบทสรุป"
    paper = _put_paper(arts, src.encode("utf-8"))
    quote = "ผลการทดลองยืนยันสมมติฐาน"
    cr = _eval(arts, paper, quote, fetched={paper.sha256})
    assert cr.status is ReqStatus.MET, cr.detail


def test_multi_quote_effect_aggregates(tmp_path: Path):
    """MINOR: last_effect aggregates across all quotes."""
    arts = LocalArtifactStore(tmp_path / "arts")
    paper = _put_paper(
        arts,
        b"Alpha word tokens appear here now. Beta word tokens appear here too.",
    )
    q1 = "alpha word tokens appear"
    q2 = "beta word tokens appear"
    payload = {
        "quotes": [
            _quote_payload(paper, q1),
            _quote_payload(paper, q2),
        ]
    }
    qart = _put_quote_json(arts, payload, call="mq")
    cr = evaluate_code_check(
        CodeCheck(kind="quote_in_source", output="quote"),
        template_id="quote_ok",
        check_id="c",
        out=AgentTaskOutput(
            outputs={"quote": ArtifactHandle(artifact_id=qart.artifact_id, name="quote")}
        ),
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="mq",
        fetched_digests={paper.sha256},
    )
    assert cr.status is ReqStatus.MET, cr.detail
    assert cr.effect is not None
    assert "quotes" in cr.effect
    assert len(cr.effect["quotes"]) == 2
    assert len(cr.evidence) == 2


# --- SourceFetched / spoof / capability ---


def test_put_fetched_without_capability_rejected(tmp_path: Path):
    """BLOCKER: spoof via put_fetched directly (no capability) rejected."""
    arts = LocalArtifactStore(tmp_path / "arts")
    kw = dict(
        name="fake.txt",
        media_type="text/plain",
        role="paper",
        expected_tool="papers_get",
        expected_call_id="spoof:1",
        expected_study_id="st",
        expected_attempt_id="spoof",
    )
    with pytest.raises(ArtifactRejected, match="fetch_capability_required"):
        arts.put_fetched(
            b"Forged fetch with four word tokens here.",
            make_tool_ref("spoof", tool="papers_get"),
            capability=None,
            **kw,
        )
    with pytest.raises(ArtifactRejected, match="fetch_capability_required"):
        arts.put_fetched(
            b"Forged fetch with four word tokens here.",
            make_tool_ref("spoof", tool="papers_get"),
            capability=object(),
            **kw,
        )


def test_agent_put_source_spoof_rejected(tmp_path: Path):
    """BLOCKER 1: arts.put source must not MET even if digest passed ad hoc."""
    arts = LocalArtifactStore(tmp_path / "arts")
    paper = arts.put(
        b"The forged paper claims four word tokens here.",
        make_tool_ref("spoof", tool="agent"),
        name="fake.txt",
        media_type="text/plain",
        role="paper",
    )
    cr = _eval(
        arts,
        paper,
        "the forged paper claims four",
        fetched={paper.sha256},
    )
    assert cr.status is ReqStatus.FAILED
    assert cr.detail == "source_not_fetched"


def test_put_sandbox_source_rejected(tmp_path: Path):
    arts = LocalArtifactStore(tmp_path / "arts")
    paper = arts.put_sandbox(
        b"Sandbox text with four word tokens present.",
        make_tool_ref("sb", tool="sandbox"),
        name="s.txt",
        media_type="text/plain",
        role="paper",
        net_isolation="hard",
        fs_isolation="hard",
    )
    cr = _eval(
        arts,
        paper,
        "sandbox text with four",
        fetched={paper.sha256},
    )
    assert cr.status is ReqStatus.FAILED
    assert cr.detail == "source_not_fetched"


def test_put_user_source_rejected(tmp_path: Path):
    """PR19: producer=user is never a quote_in_source source (is_quote_source_producer)."""
    from coscientist.core.execution._ingest_gate import (
        mint_user_put_capability,
        put_user,
        release_user_put_capability,
    )

    arts = LocalArtifactStore(tmp_path / "arts")
    cap = mint_user_put_capability()
    try:
        paper = put_user(
            arts,
            b"User upload with four word tokens present.",
            make_tool_ref("u", tool="user"),
            capability=cap,
            name="u.txt",
            media_type="text/plain",
            role="paper",  # forced to data; still not a quote source
        )
    finally:
        release_user_put_capability(cap)
    assert paper.producer == "user"
    cr = _eval(
        arts,
        paper,
        "user upload with four",
        fetched={paper.sha256},
    )
    assert cr.status is ReqStatus.FAILED
    assert cr.detail == "source_not_fetched"


def test_tainted_fetched_accepted_when_journaled(tmp_path: Path):
    """BLOCKER: network content stays tainted; journal match still allows MET."""
    arts = LocalArtifactStore(tmp_path / "arts")
    paper = _put_paper(arts, b"Tainted fetch with four word tokens here.")
    assert paper.tainted is True
    cr = _eval(arts, paper, "tainted fetch with four", fetched={paper.sha256})
    assert cr.status is ReqStatus.MET, cr.detail
    assert cr.effect is not None
    assert cr.effect["trusted"] is False
    assert cr.effect["tainted"] is True
    assert cr.effect["countable"] is True


def test_missing_journal_fetch_rejected(tmp_path: Path):
    arts = LocalArtifactStore(tmp_path / "arts")
    paper = _put_paper(arts, b"Real tool text with four word tokens here.")
    cr = _eval(arts, paper, "real tool text with", fetched=frozenset())
    assert cr.status is ReqStatus.FAILED
    assert cr.detail == "source_not_fetched"


def test_source_sha256_required(tmp_path: Path):
    arts = LocalArtifactStore(tmp_path / "arts")
    paper = _put_paper(arts, b"Four word tokens appear right here now.")
    payload = {
        "artifact_id": paper.artifact_id,
        "locator": "p.1",
        "quote": "four word tokens appear",
        "digest": quote_digest("four word tokens appear"),
        "normalization_version": NORMALIZATION_VERSION,
    }
    qart = _put_quote_json(arts, payload, call="q")
    cr = evaluate_code_check(
        CodeCheck(kind="quote_in_source", output="quote"),
        template_id="quote_ok",
        check_id="c",
        out=AgentTaskOutput(
            outputs={"quote": ArtifactHandle(artifact_id=qart.artifact_id, name="quote")}
        ),
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="q",
        fetched_digests={paper.sha256},
    )
    assert cr.status is ReqStatus.FAILED
    assert cr.detail == "quote_payload_invalid"


def test_values_source_quote_path_removed(tmp_path: Path):
    """MAJOR 2: values.source_quote cannot yield MET."""
    arts = LocalArtifactStore(tmp_path / "arts")
    paper = _put_paper(arts, b"Four word tokens appear right here now.")
    out = AgentTaskOutput(
        values={
            "source_quote": _quote_payload(paper, "four word tokens appear"),
        }
    )
    cr = evaluate_code_check(
        CodeCheck(kind="quote_in_source"),
        template_id="quote_ok",
        check_id="c",
        out=out,
        artifacts=arts,
        values=out.values,
        role="validity",
        fetched_digests={paper.sha256},
    )
    assert cr.status is ReqStatus.FAILED
    assert cr.detail == "output_not_submitted"
    assert cr.effect is None


def test_missing_normalization_version_unassessed(tmp_path: Path):
    """MAJOR 4: None version ≠ current → UNASSESSED."""
    arts = LocalArtifactStore(tmp_path / "arts")
    paper = _put_paper(arts, b"Four word tokens appear right here now.")
    payload = _quote_payload(paper, "four word tokens appear")
    payload["normalization_version"] = None
    qart = _put_quote_json(arts, payload, call="q")
    cr = evaluate_code_check(
        CodeCheck(kind="quote_in_source", output="quote"),
        template_id="quote_ok",
        check_id="c",
        out=AgentTaskOutput(
            outputs={"quote": ArtifactHandle(artifact_id=qart.artifact_id, name="quote")}
        ),
        artifacts=arts,
        values={},
        role="validity",
        attempt_id="q",
        fetched_digests={paper.sha256},
    )
    assert cr.status is ReqStatus.UNASSESSED
    assert cr.detail == "normalization_version_mismatch"


def test_wrong_normalization_version_unassessed(tmp_path: Path):
    arts = LocalArtifactStore(tmp_path / "arts")
    paper = _put_paper(arts, b"Four word tokens appear right here now.")
    cr = _eval(
        arts,
        paper,
        "four word tokens appear",
        fetched={paper.sha256},
        normalization_version="1+0.0.0",
    )
    assert cr.status is ReqStatus.UNASSESSED
    assert cr.detail == "normalization_version_mismatch"


@pytest.fixture(autouse=True)
def _clear_fetch_cap_registries():
    """Keep effects issued/permit maps empty across tests (no manual .clear())."""
    from coscientist.tools import effects as effects_mod

    effects_mod._ISSUED_FETCH_CAPS.clear()
    effects_mod._SOURCE_FETCHED_WRITE_PERMITS.clear()
    yield
    effects_mod._ISSUED_FETCH_CAPS.clear()
    effects_mod._SOURCE_FETCHED_WRITE_PERMITS.clear()


@pytest.fixture
def fetch_registry():
    _reset_registry_for_tests()
    _register_tool_for_tests(
        RegisteredToolSpec(name="papers_get", effect="read", is_fetch=True)
    )
    _register_tool_for_tests(
        RegisteredToolSpec(name="catalog_read", effect="read", is_fetch=False)
    )
    _register_tool_for_tests(RegisteredToolSpec(name="write_note", effect="write"))
    freeze_registry()
    yield
    _reset_registry_for_tests()


class _FetchTool:
    def __init__(self, arts: LocalArtifactStore, body: bytes):
        self.spec = ToolSpec(name="papers_get", effect="read", is_fetch=True)
        self.arts = arts
        self.body = body

    async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
        assert ctx.fetch_capability is not None
        call_id = f"{ctx.attempt_id}:{ctx.call_seq}"
        art = self.arts.put_fetched(
            self.body,
            make_tool_ref(call_id, tool=self.spec.name),
            name="paper.txt",
            media_type="text/plain",
            role="paper",
            capability=ctx.fetch_capability,
            expected_tool=self.spec.name,
            expected_call_id=call_id,
            expected_study_id=ctx.study_id,
            expected_attempt_id=ctx.attempt_id,
        )
        # Normalized result carries url (not raw args for journaling).
        return ToolOutput(data={"url": "https://normalized.example/paper"}, artifacts=[art])


class _ReadNonFetchTool:
    def __init__(self, arts: LocalArtifactStore, body: bytes):
        self.spec = ToolSpec(name="catalog_read", effect="read", is_fetch=False)
        self.arts = arts
        self.body = body

    async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
        # Non-fetch read must not receive a capability.
        assert ctx.fetch_capability is None
        # Attempting put_fetched without cap must fail.
        call_id = f"{ctx.attempt_id}:{ctx.call_seq}"
        with pytest.raises(ArtifactRejected, match="fetch_capability_required"):
            self.arts.put_fetched(
                self.body,
                make_tool_ref(call_id, tool=self.spec.name),
                name="x.txt",
                media_type="text/plain",
                role="paper",
                capability=ctx.fetch_capability,
                expected_tool=self.spec.name,
                expected_call_id=call_id,
                expected_study_id=ctx.study_id,
                expected_attempt_id=ctx.attempt_id,
            )
        return ToolOutput(data={"ok": True}, artifacts=[])


@pytest.mark.asyncio
async def test_invoke_tool_fetch_accepted(tmp_path: Path, clock, fetch_registry):
    """BLOCKER: real is_fetch tool via invoke_tool → SourceFetched + MET."""
    catalog = _catalog_quote()
    sid = "st_fetch_invoke"
    rt = await open_runtime(tmp_path, sid, clock=clock, catalog=catalog)
    await rt.create_study(
        profile="research",
        request="quote",
        catalog_version="0.1.0-pr14",
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
    )
    ex, arts = attach_executor(rt, tmp_path)
    rt.artifacts = arts
    for key in ("SPEC", "HYP", "LIT"):
        await record_success(rt, await rt.start_attempt(key), arts)
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="LIT-0",
                title="lit",
                goal="g",
                route="literature",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="quote_ok")],
            )
        ],
        questions=[],
    )
    aid = await rt.start_attempt("LIT-0")
    assert aid is not None
    body = b"The known result was confirmed under careful review today."
    tool = _FetchTool(arts, body)
    ctx = CallCtx(
        study_id=sid,
        task_key="LIT-0",
        attempt_id=aid,
        hitl_policy=HitlPolicy(),
        call_seq=1,
    )
    agent_url = "https://agent-supplied.example/ignored"
    out = await invoke_tool(
        tool,
        {"url": agent_url},
        ctx,
        writer=rt.writer,
        artifacts=arts,
    )
    assert out.artifacts
    paper = out.artifacts[0]
    assert paper.tainted is True
    assert paper.producer == "tool"
    events = await rt.store.read_all()
    # AttemptTainted must precede SourceFetched for this call.
    taint_seq = next(
        e.seq
        for e in events
        if e.type == "AttemptTainted" and e.payload.get("call_id") == f"{aid}:1"
    )
    sf = next(
        e
        for e in events
        if e.type == "SourceFetched" and e.payload.get("digest") == paper.sha256
    )
    assert taint_seq is not None and sf.seq is not None
    assert taint_seq < sf.seq
    # Agent-supplied url must not appear in SourceFetched.
    assert sf.payload.get("url") == "https://normalized.example/paper"
    assert sf.payload.get("url") != agent_url
    qart = _put_quote_json(
        arts,
        _quote_payload(paper, "the known result was confirmed"),
        call=f"{aid}:quote",
    )
    result = await ex.record(
        aid,
        AgentTaskOutput(
            claim=Outcome.SUCCESS,
            outputs={
                "quote": ArtifactHandle(artifact_id=qart.artifact_id, name="quote")
            },
        ),
    )
    assert result.outcome == Outcome.SUCCESS
    await rt.fail_attempt(await rt.start_attempt("REPORT"), retryable=False)
    await rt.tick()
    assert rt.view().phase == "closed"
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_invoke_tool_non_fetch_read_rejected(
    tmp_path: Path, clock, fetch_registry
):
    """BLOCKER: invoke_tool with non-fetch read must not create SourceFetched."""
    catalog = _catalog_quote()
    sid = "st_nonfetch"
    rt = await open_runtime(tmp_path, sid, clock=clock, catalog=catalog)
    await rt.create_study(
        profile="research",
        request="quote",
        catalog_version="0.1.0-pr14",
        deadline=clock.now() + timedelta(seconds=60),
        sid=sid,
        finalize_reserve_s=5,
    )
    _ex, arts = attach_executor(rt, tmp_path)
    rt.artifacts = arts
    for key in ("SPEC", "HYP", "LIT"):
        await record_success(rt, await rt.start_attempt(key), arts)
    tool = _ReadNonFetchTool(arts, b"Should not be journaled as fetch at all here.")
    ctx = CallCtx(
        study_id=sid,
        task_key="LIT",
        attempt_id="att_nf",
        hitl_policy=HitlPolicy(),
        call_seq=1,
    )
    await invoke_tool(
        tool, {}, ctx, writer=rt.writer, artifacts=arts
    )
    events = await rt.store.read_all()
    assert not any(e.type == "SourceFetched" for e in events)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_store_fetched_source_journals_and_matches(
    tmp_path: Path, clock, fetch_registry
):
    """Happy path: store_fetched_source (with capability) → MET + countable."""
    catalog = _catalog_quote()
    sid = "st_fetch_ok"
    rt = await open_runtime(tmp_path, sid, clock=clock, catalog=catalog)
    await rt.create_study(
        profile="research",
        request="quote",
        catalog_version="0.1.0-pr14",
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
    )
    ex, arts = attach_executor(rt, tmp_path)
    rt.artifacts = arts
    for key in ("SPEC", "HYP", "LIT"):
        await record_success(rt, await rt.start_attempt(key), arts)
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="LIT-0",
                title="lit",
                goal="g",
                route="literature",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="quote_ok")],
            )
        ],
        questions=[],
    )
    # Spoof-path study: digests attempt-scoped; unit spoof uses producer=agent.
    cap = _cap("spoofatt:1", "papers_get", study_id=sid)
    paper = await store_fetched_source(
        rt.writer,
        arts,
        b"The known result was confirmed under careful review today.",
        study_id=sid,
        call_id="spoofatt:1",
        tool="papers_get",
        url="https://example.org/paper",
        capability=cap,
    )
    assert paper.tainted is True
    events = await rt.store.read_all()
    assert any(
        e.type == "SourceFetched" and e.payload.get("digest") == paper.sha256
        for e in events
    )
    spoof = arts.put(
        b"The known result was confirmed under careful review today.",
        make_tool_ref("spoof", tool="agent"),
        name="spoof.txt",
        media_type="text/plain",
        role="paper",
    )
    assert spoof.sha256 == paper.sha256
    aid = await rt.start_attempt("LIT-0")
    assert aid is not None
    q_spoof = _put_quote_json(
        arts,
        _quote_payload(spoof, "the known result was confirmed"),
        call=f"{aid}:qs",
    )
    result = await ex.record(
        aid,
        AgentTaskOutput(
            claim=Outcome.SUCCESS,
            outputs={
                "quote": ArtifactHandle(artifact_id=q_spoof.artifact_id, name="quote")
            },
        ),
    )
    assert result.outcome in (Outcome.FAILED, Outcome.PARTIAL)
    if "REPORT" in {t.spec.task_key for t in rt.tasks.values()}:
        rid = await rt.start_attempt("REPORT")
        if rid:
            await rt.fail_attempt(rid, retryable=False)
    await rt.tick()
    assert rt.view().phase == "closed"
    fin = next(e for e in await rt.store.read_all() if e.type == "StudyFinalized")
    assert "report_artifact_id" in fin.payload
    assert "contributing_reasons" in fin.payload
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_source_fetched_survives_crash_reopen(
    tmp_path: Path, clock, fetch_registry
):
    """Checklist §1: SourceFetched + CAS meta survive crash; no recreate."""
    catalog = _catalog_quote()
    sid = "st_fetch_reopen"
    arts_root = tmp_path / "arts"
    rt = await open_runtime(tmp_path, sid, clock=clock, catalog=catalog, owner="t1")
    await rt.create_study(
        profile="research",
        request="quote",
        catalog_version="0.1.0-pr14",
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
    )
    ex, arts = attach_executor(rt, tmp_path)
    # Force stable arts root so reopen can reload meta from disk.
    if arts.root != arts_root:
        arts = LocalArtifactStore(arts_root)
        rt.artifacts = arts
        if getattr(ex, "artifacts", None) is not None:
            ex.artifacts = arts
    rt.artifacts = arts
    for key in ("SPEC", "HYP", "LIT"):
        await record_success(rt, await rt.start_attempt(key), arts)
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="LIT-0",
                title="lit",
                goal="g",
                route="literature",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="quote_ok")],
            )
        ],
        questions=[],
    )
    aid = await rt.start_attempt("LIT-0")
    assert aid is not None
    call_id = f"{aid}:1"
    paper = await store_fetched_source(
        rt.writer,
        arts,
        b"The known result was confirmed under careful review today.",
        study_id=sid,
        call_id=call_id,
        tool="papers_get",
        url="https://example.org/reopen",
        capability=_cap(call_id, "papers_get", study_id=sid),
    )
    paper_id = paper.artifact_id
    paper_sha = paper.sha256
    assert any(
        e.type == "SourceFetched" and e.payload.get("digest") == paper_sha
        for e in await rt.store.read_all()
    )
    await close_runtime(rt)

    rt2 = await open_runtime(tmp_path, sid, clock=clock, catalog=catalog, owner="t2")
    ex2, _arts_new = attach_executor(rt2, tmp_path)
    arts2 = LocalArtifactStore(arts_root)
    rt2.artifacts = arts2
    ex2.artifacts = arts2
    from coscientist.core.execution.source_fetch import load_source_fetch_index_from_writer

    idx = await load_source_fetch_index_from_writer(rt2.writer)
    assert paper_sha in idx.digests_for_attempt(aid)
    # Read stored meta — must not recreate via put_fetched.
    meta = arts2.get_meta(paper_id)
    assert meta is not None
    assert meta.sha256 == paper_sha
    assert meta.producer == "tool"
    assert meta.tainted is True
    qart = _put_quote_json(
        arts2,
        {
            "artifact_id": paper_id,
            "locator": "p.1",
            "quote": "the known result was confirmed",
            "digest": quote_digest("the known result was confirmed"),
            "source_sha256": paper_sha,
            "normalization_version": NORMALIZATION_VERSION,
        },
        call=f"{aid}:quote",
    )
    result = await ex2.record(
        aid,
        AgentTaskOutput(
            claim=Outcome.SUCCESS,
            outputs={
                "quote": ArtifactHandle(artifact_id=qart.artifact_id, name="quote")
            },
        ),
    )
    assert result.outcome == Outcome.SUCCESS
    await rt2.fail_attempt(await rt2.start_attempt("REPORT"), retryable=False)
    await rt2.tick()
    assert rt2.view().phase == "closed"
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_fetched_quote_met_closes_study(tmp_path: Path, clock, fetch_registry):
    catalog = _catalog_quote()
    sid = "st_quote_met"
    rt = await open_runtime(tmp_path, sid, clock=clock, catalog=catalog)
    await rt.create_study(
        profile="research",
        request="quote",
        catalog_version="0.1.0-pr14",
        deadline=clock.now() + timedelta(seconds=120),
        sid=sid,
        finalize_reserve_s=5,
    )
    ex, arts = attach_executor(rt, tmp_path)
    rt.artifacts = arts
    for key in ("SPEC", "HYP", "LIT"):
        await record_success(rt, await rt.start_attempt(key), arts)
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="LIT-0",
                title="lit",
                goal="g",
                route="literature",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="quote_ok")],
            )
        ],
        questions=[],
    )
    aid = await rt.start_attempt("LIT-0")
    assert aid is not None
    call_id = f"{aid}:1"
    paper = await store_fetched_source(
        rt.writer,
        arts,
        b"The known result was confirmed under careful review today.",
        study_id=sid,
        call_id=call_id,
        tool="papers_get",
        url="https://example.org/ok",
        capability=_cap(call_id, "papers_get", study_id=sid),
    )
    qart = _put_quote_json(
        arts,
        _quote_payload(paper, "the known result was confirmed"),
        call=f"{aid}:quote",
    )
    result = await ex.record(
        aid,
        AgentTaskOutput(
            claim=Outcome.SUCCESS,
            outputs={
                "quote": ArtifactHandle(artifact_id=qart.artifact_id, name="quote")
            },
        ),
    )
    assert result.outcome == Outcome.SUCCESS
    recorded = [
        e
        for e in await rt.store.read_all()
        if e.type == "ResultRecorded" and e.payload.get("attempt_id") == aid
    ]
    assert recorded
    crs = recorded[-1].payload.get("check_results") or []
    met = next(c for c in crs if c.get("status") == "MET")
    assert met.get("effect", {}).get("countable") is True
    assert met.get("effect", {}).get("source_fetched") is True
    assert met.get("effect", {}).get("trusted") is False
    await rt.fail_attempt(await rt.start_attempt("REPORT"), retryable=False)
    await rt.tick()
    assert rt.view().phase == "closed"
    await close_runtime(rt)


def test_source_quote_model_requires_sha():
    with pytest.raises(ValidationError):
        SourceQuote(
            artifact_id="a",
            locator="p.1",
            quote="hi there four tokens xx",
            digest=quote_digest("hi there four tokens xx"),
        )


def test_hyphen_kept_variant_matches():
    source = "A well-\nknown method works quite well today."
    quote_joined = normalize_for_quote_match("a wellknown method works")
    quote_kept = normalize_for_quote_match("a well-known method works")
    assert quote_in_normalized_source(quote_joined, source)
    assert quote_in_normalized_source(quote_kept, source)


def test_digit_edge_in_normalized_source():
    assert not quote_in_normalized_source(
        normalize_for_quote_match("5 mg daily dose"),
        "patients received -5 mg daily dose overall",
    )
    assert quote_in_normalized_source(
        normalize_for_quote_match("5 mg daily dose"),
        "patients received 5 mg daily dose overall",
    )


def test_evaluate_task_checks_passes_fetched(tmp_path: Path):
    catalog = _catalog_quote()
    arts = LocalArtifactStore(tmp_path / "arts")
    paper = _put_paper(
        arts,
        b"Matched body text with four tokens here.",
        call="att:paper",
    )
    quote = "matched body text with"
    qart = _put_quote_json(
        arts, _quote_payload(paper, quote), call="att:quote"
    )
    results = evaluate_task_checks(
        catalog,  # type: ignore[arg-type]
        TaskSpec(
            task_key="LIT-0",
            title="lit",
            goal="g",
            route="literature",
            criteria=[RequirementRef(template_id="quote_ok")],
        ),
        AgentTaskOutput(
            outputs={
                "quote": ArtifactHandle(artifact_id=qart.artifact_id, name="quote")
            }
        ),
        arts,
        attempt_id="att",
        plan_role=PlanRole.NONE,
        fetched_digests={paper.sha256},
    )
    assert any(r.status is ReqStatus.MET for r in results)


@pytest.mark.asyncio
async def test_fetch_then_write_is_taint_deny(tmp_path: Path, clock, fetch_registry):
    """MAJOR: after fetch, same attempt cannot invoke write (taint_deny)."""
    from coscientist.tools.contract import ToolError

    catalog = _catalog_quote()
    sid = "st_fetch_taint"
    rt = await open_runtime(tmp_path, sid, clock=clock, catalog=catalog)
    await rt.create_study(
        profile="research",
        request="quote",
        catalog_version="0.1.0-pr14",
        deadline=clock.now() + timedelta(seconds=60),
        sid=sid,
        finalize_reserve_s=5,
    )
    _ex, arts = attach_executor(rt, tmp_path)
    rt.artifacts = arts
    for key in ("SPEC", "HYP", "LIT"):
        await record_success(rt, await rt.start_attempt(key), arts)
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="LIT-0",
                title="lit",
                goal="g",
                route="literature",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="quote_ok")],
            )
        ],
        questions=[],
    )
    aid = await rt.start_attempt("LIT-0")
    assert aid is not None
    fetch = _FetchTool(arts, b"Four word tokens appear right here now.")
    ctx = CallCtx(
        study_id=sid,
        task_key="LIT-0",
        attempt_id=aid,
        hitl_policy=HitlPolicy(),
        call_seq=1,
    )
    out = await invoke_tool(
        fetch,
        {"url": "https://agent-supplied.example/ignored"},
        ctx,
        writer=rt.writer,
        artifacts=arts,
    )
    assert out.artifacts
    assert any(e.type == "AttemptTainted" for e in await rt.store.read_all())
    assert ctx.fetch_capability is None  # cleared in finally

    class _Write:
        spec = ToolSpec(name="write_note", effect="write")

        async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
            return ToolOutput(data={"ok": True})

    ctx2 = CallCtx(
        study_id=sid,
        task_key="LIT-0",
        attempt_id=aid,
        hitl_policy=HitlPolicy(),
        call_seq=2,
    )
    with pytest.raises(ToolError, match="tainted context"):
        await invoke_tool(_Write(), {}, ctx2, writer=rt.writer, artifacts=arts)
    denied = [
        e
        for e in await rt.store.read_all()
        if e.type == "SideEffectDenied"
        and e.payload.get("reason") == "taint_deny"
    ]
    assert denied
    await close_runtime(rt)


def test_contextvar_taint_isolated_across_tasks_and_threads():
    """ContextVar cell isolation + inheritance into create_task / to_thread."""
    import asyncio
    import concurrent.futures

    from coscientist.core.execution.taint import (
        current_attempt_taint,
        mark_attempt_tainted,
        push_attempt_taint,
        reset_attempt_taint,
    )

    results: dict[str, bool] = {}

    async def _worker(name: str, start: bool) -> None:
        tok = push_attempt_taint(start)
        try:
            if name == "a":
                mark_attempt_tainted()
            await asyncio.sleep(0)
            results[name] = current_attempt_taint()
        finally:
            reset_attempt_taint(tok)

    async def _main() -> None:
        await asyncio.gather(_worker("a", False), _worker("b", False))
        # Inheritance: child create_task sees parent's taint at spawn time.
        tok = push_attempt_taint(True)
        try:

            async def _child() -> bool:
                return current_attempt_taint()

            assert await asyncio.create_task(_child()) is True
            # to_thread copies ContextVar on 3.12+.
            assert (
                await asyncio.to_thread(current_attempt_taint) is True
            )
        finally:
            reset_attempt_taint(tok)

    asyncio.run(_main())
    assert results["a"] is True
    assert results["b"] is False

    def _thread_worker() -> bool:
        tok = push_attempt_taint(False)
        try:
            mark_attempt_tainted()
            return current_attempt_taint()
        finally:
            reset_attempt_taint(tok)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(_thread_worker)
        f2 = pool.submit(lambda: current_attempt_taint())
        assert f1.result() is True
        assert f2.result() is False


@pytest.mark.asyncio
async def test_attempt_taint_survives_replay(tmp_path: Path, clock, fetch_registry):
    """AttemptTainted in journal → reopen → write taint_deny; meta from disk."""
    from coscientist.tools.contract import ToolError
    from coscientist.tools.effects import _tainted_from_journal

    catalog = _catalog_quote()
    sid = "st_taint_replay"
    arts_root = tmp_path / "arts"
    rt = await open_runtime(tmp_path, sid, clock=clock, catalog=catalog, owner="t1")
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0-pr14",
        deadline=clock.now() + timedelta(seconds=60),
        sid=sid,
        finalize_reserve_s=5,
    )
    _ex, arts = attach_executor(rt, tmp_path)
    arts = LocalArtifactStore(arts_root)
    rt.artifacts = arts
    for key in ("SPEC", "HYP", "LIT"):
        await record_success(rt, await rt.start_attempt(key), arts)
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="LIT-0",
                title="lit",
                goal="g",
                route="literature",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="quote_ok")],
            )
        ],
        questions=[],
    )
    aid = await rt.start_attempt("LIT-0")
    assert aid is not None
    out = await invoke_tool(
        _FetchTool(arts, b"Four word tokens appear right here now."),
        {"url": "https://x.example/p"},
        CallCtx(
            study_id=sid,
            task_key="LIT-0",
            attempt_id=aid,
            hitl_policy=HitlPolicy(),
            call_seq=1,
        ),
        writer=rt.writer,
        artifacts=arts,
    )
    assert out.artifacts
    paper_id = out.artifacts[0].artifact_id
    paper_sha = out.artifacts[0].sha256
    await close_runtime(rt)

    rt2 = await open_runtime(tmp_path, sid, clock=clock, catalog=catalog, owner="t2")
    arts2 = LocalArtifactStore(arts_root)
    rt2.artifacts = arts2
    assert await _tainted_from_journal(rt2.writer, aid) is True
    meta = arts2.get_meta(paper_id)
    assert meta is not None
    assert meta.sha256 == paper_sha
    assert meta.producer == "tool"
    assert meta.tainted is True

    class _Write:
        spec = ToolSpec(name="write_note", effect="write")

        async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
            return ToolOutput(data={"ok": True})

    with pytest.raises(ToolError, match="tainted context"):
        await invoke_tool(
            _Write(),
            {},
            CallCtx(
                study_id=sid,
                task_key="LIT-0",
                attempt_id=aid,
                hitl_policy=HitlPolicy(),
                call_seq=2,
            ),
            writer=rt2.writer,
            artifacts=arts2,
        )
    await close_runtime(rt2)


@pytest.mark.asyncio
async def test_direct_writer_append_source_fetched_rejected(tmp_path: Path, clock):
    """MAJOR: StudyWriter.append / store.append_many SourceFetched without permit."""
    from coscientist.core.events.errors import ArtifactRejected
    from coscientist.core.events.events import EventEnvelope, make_idem
    from coscientist.core.execution.source_fetch import FetchCapability

    rt = await open_runtime(tmp_path, "st_sf_deny", clock=clock)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0-pr14",
        deadline=clock.now() + timedelta(seconds=30),
        sid="st_sf_deny",
        finalize_reserve_s=5,
    )
    ev = EventEnvelope(
        study_id="st_sf_deny",
        type="SourceFetched",
        entity_key="spoof",
        idem_key=make_idem("SourceFetched", "spoof"),
        payload={
            "call_id": "a:1",
            "tool": "papers_get",
            "digest": "abc",
            "url": "https://x",
        },
        ts=datetime.now(UTC),
        schema_version=1,
epoch=0, payload_digest=payload_digest({
            "call_id": "a:1",
            "tool": "papers_get",
            "digest": "abc",
            "url": "https://x",
        }),
)
    with pytest.raises(ArtifactRejected, match="fetch_capability_required"):
        await rt.writer.append(ev)
    with pytest.raises(ArtifactRejected, match="fetch_capability_required"):
        await rt.store.append_many([ev])
    # Bool bypass gone: forged capability id without effects write permit refused.
    with pytest.raises(ArtifactRejected, match="fetch_capability_required"):
        await rt.store.append_many(
            [ev], _source_fetched_cap_id="forged-not-issued"
        )
    forged = FetchCapability(id="forged-not-issued")
    from coscientist.core.execution.source_fetch import verify_fetch_capability

    with pytest.raises(ArtifactRejected, match="fetch_capability_required"):
        verify_fetch_capability(
            forged, tool="papers_get", call_id="a:1", study_id="st_sf_deny", attempt_id="a"
        )
    await close_runtime(rt)


def test_fetch_capability_not_forgeable_via_replace():
    """MAJOR: opaque id + registry — replace/copy of fields cannot mint."""
    from dataclasses import replace

    from coscientist.core.events.errors import ArtifactRejected
    from coscientist.core.execution.source_fetch import (
        FetchCapability,
        assert_fetch_capability_bound,
        verify_fetch_capability,
    )

    cap = _cap("att:1", "papers_get", study_id="st_x")
    # Only id is readable; replace cannot invent a valid registry entry.
    twin = replace(cap, id=cap.id)
    assert_fetch_capability_bound(
        twin,
        tool="papers_get",
        call_id="att:1",
        study_id="st_x",
        attempt_id="att",
    )
    verify_fetch_capability(
        twin,
        tool="papers_get",
        call_id="att:1",
        study_id="st_x",
        attempt_id="att",
    )
    # Consumed — original handle and any copy of the same id are spent.
    with pytest.raises(ArtifactRejected, match="fetch_capability_required"):
        verify_fetch_capability(
            cap,
            tool="papers_get",
            call_id="att:1",
            study_id="st_x",
            attempt_id="att",
        )
    with pytest.raises(ArtifactRejected, match="fetch_capability_required"):
        assert_fetch_capability_bound(
            FetchCapability(id=cap.id),
            tool="papers_get",
            call_id="att:1",
            study_id="st_x",
            attempt_id="att",
        )


@pytest.mark.asyncio
async def test_is_fetch_failure_still_taints(tmp_path: Path, clock, fetch_registry):
    """MAJOR: AttemptTainted before call — even when the tool raises."""
    from coscientist.tools.contract import ToolError

    catalog = _catalog_quote()
    sid = "st_fetch_fail_taint"
    rt = await open_runtime(tmp_path, sid, clock=clock, catalog=catalog)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0-pr14",
        deadline=clock.now() + timedelta(seconds=60),
        sid=sid,
        finalize_reserve_s=5,
    )
    _ex, arts = attach_executor(rt, tmp_path)
    rt.artifacts = arts
    for key in ("SPEC", "HYP", "LIT"):
        await record_success(rt, await rt.start_attempt(key), arts)
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="LIT-0",
                title="lit",
                goal="g",
                route="literature",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="quote_ok")],
            )
        ],
        questions=[],
    )
    aid = await rt.start_attempt("LIT-0")
    assert aid is not None

    class _BoomFetch:
        spec = ToolSpec(name="papers_get", effect="read", is_fetch=True)

        async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
            raise ToolError("opaque", "fetch failed")

    with pytest.raises(ToolError, match="fetch failed"):
        await invoke_tool(
            _BoomFetch(),
            {"url": "https://agent.example/x"},
            CallCtx(
                study_id=sid,
                task_key="LIT-0",
                attempt_id=aid,
                hitl_policy=HitlPolicy(),
                call_seq=1,
            ),
            writer=rt.writer,
            artifacts=arts,
        )
    events = await rt.store.read_all()
    assert any(
        e.type == "AttemptTainted"
        and e.payload.get("call_id") == f"{aid}:1"
        and e.payload.get("reason") == "source_fetch"
        for e in events
    )
    assert not any(e.type == "SourceFetched" for e in events)

    class _Write:
        spec = ToolSpec(name="write_note", effect="write")

        async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
            return ToolOutput(data={"ok": True})

    with pytest.raises(ToolError, match="tainted context"):
        await invoke_tool(
            _Write(),
            {},
            CallCtx(
                study_id=sid,
                task_key="LIT-0",
                attempt_id=aid,
                hitl_policy=HitlPolicy(),
                call_seq=2,
            ),
            writer=rt.writer,
            artifacts=arts,
        )
    await close_runtime(rt)


def test_projection_source_fetched_requires_fetch_call_pairing():
    """SourceFetched without prior AttemptTainted fetch_call is not folded."""
    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.events.projections import _get, _put, apply_to_map

    proj: dict = {}
    sf = EventEnvelope(
        study_id="s",
        type="SourceFetched",
        entity_key="x",
        idem_key="SourceFetched:x",
        payload={
            "call_id": "att:1",
            "tool": "papers_get",
            "digest": "deadbeef",
            "url": "https://x",
        },
        ts=datetime.now(UTC),
        schema_version=1,
        seq=1,
epoch=0, payload_digest=payload_digest({
            "call_id": "att:1",
            "tool": "papers_get",
            "digest": "deadbeef",
            "url": "https://x",
        }),
)
    apply_to_map(proj, sf)
    assert not proj.get("source_fetches")
    _put(proj, "attempts", "att", {"attempt_id": "att", "reason": "keep_me"})
    at = EventEnvelope(
        study_id="s",
        type="AttemptTainted",
        entity_key="att",
        idem_key="AttemptTainted:att:att:1",
        payload={
            "attempt_id": "att",
            "reason": "source_fetch",
            "call_id": "att:1",
            "tool": "papers_get",
            "is_fetch": True,
        },
        ts=datetime.now(UTC),
        schema_version=1,
        seq=2,
epoch=0, payload_digest=payload_digest({
            "attempt_id": "att",
            "reason": "source_fetch",
            "call_id": "att:1",
            "tool": "papers_get",
            "is_fetch": True,
        }),
)
    # Registry must know papers_get as is_fetch for the marker check.
    _reset_registry_for_tests()
    _register_tool_for_tests(
        RegisteredToolSpec(name="papers_get", effect="read", is_fetch=True)
    )
    freeze_registry()
    apply_to_map(proj, at)
    _reset_registry_for_tests()
    row = _get(proj, "attempts", "att")
    assert row is not None
    assert row.get("reason") == "keep_me"
    assert row.get("tainted_context") is True
    apply_to_map(proj, sf.model_copy(update={"seq": 3}))
    assert "deadbeef" in (proj.get("source_fetches") or {})


@pytest.mark.asyncio
async def test_fetch_data_only_still_taints(tmp_path: Path, clock, fetch_registry):
    """Fetch returning only output.data (no artifacts) still taints + journals."""

    class _DataOnlyFetch:
        spec = ToolSpec(name="papers_get", effect="read", is_fetch=True)

        async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
            assert ctx.fetch_capability is not None
            return ToolOutput(data={"url": "https://normalized.example/empty"})

    catalog = _catalog_quote()
    sid = "st_data_only"
    rt = await open_runtime(tmp_path, sid, clock=clock, catalog=catalog)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0-pr14",
        deadline=clock.now() + timedelta(seconds=60),
        sid=sid,
        finalize_reserve_s=5,
    )
    _ex, arts = attach_executor(rt, tmp_path)
    rt.artifacts = arts
    for key in ("SPEC", "HYP", "LIT"):
        await record_success(rt, await rt.start_attempt(key), arts)
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="LIT-0",
                title="lit",
                goal="g",
                route="literature",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="quote_ok")],
            )
        ],
        questions=[],
    )
    aid = await rt.start_attempt("LIT-0")
    assert aid is not None
    out = await invoke_tool(
        _DataOnlyFetch(),
        {"url": "https://agent.example/x"},
        CallCtx(
            study_id=sid,
            task_key="LIT-0",
            attempt_id=aid,
            hitl_policy=HitlPolicy(),
            call_seq=1,
        ),
        writer=rt.writer,
        artifacts=arts,
    )
    assert not out.artifacts
    events = await rt.store.read_all()
    assert any(
        e.type == "AttemptTainted"
        and e.payload.get("call_id") == f"{aid}:1"
        and e.payload.get("is_fetch") is True
        for e in events
    )
    assert not any(e.type == "SourceFetched" for e in events)
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_repeated_fetch_remint_path(tmp_path: Path, clock, fetch_registry):
    """Two tool-producer artifacts in one fetch → remint + both SourceFetched."""
    from coscientist.tools import effects as effects_mod

    class _MultiFetch:
        def __init__(self, arts: LocalArtifactStore):
            self.spec = ToolSpec(name="papers_get", effect="read", is_fetch=True)
            self.arts = arts

        async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
            call_id = f"{ctx.attempt_id}:{ctx.call_seq}"
            arts_out = []
            for i, body in enumerate(
                (
                    b"First paper has four word tokens right here now.",
                    b"Second paper has four word tokens right here now.",
                )
            ):
                arts_out.append(
                    self.arts.put_fetched(
                        body,
                        make_tool_ref(f"{call_id}:{i}", tool=self.spec.name),
                        name=f"p{i}.txt",
                        media_type="text/plain",
                        role="paper",
                        capability=ctx.fetch_capability,
                        expected_tool=self.spec.name,
                        expected_call_id=call_id,
                        expected_study_id=ctx.study_id,
                        expected_attempt_id=ctx.attempt_id,
                    )
                )
            return ToolOutput(
                data={"url": "https://normalized.example/multi"},
                artifacts=arts_out,
            )

    catalog = _catalog_quote()
    sid = "st_remint"
    rt = await open_runtime(tmp_path, sid, clock=clock, catalog=catalog)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0-pr14",
        deadline=clock.now() + timedelta(seconds=60),
        sid=sid,
        finalize_reserve_s=5,
    )
    _ex, arts = attach_executor(rt, tmp_path)
    rt.artifacts = arts
    for key in ("SPEC", "HYP", "LIT"):
        await record_success(rt, await rt.start_attempt(key), arts)
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="LIT-0",
                title="lit",
                goal="g",
                route="literature",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="quote_ok")],
            )
        ],
        questions=[],
    )
    aid = await rt.start_attempt("LIT-0")
    assert aid is not None
    out = await invoke_tool(
        _MultiFetch(arts),
        {"url": "https://agent.example/multi"},
        CallCtx(
            study_id=sid,
            task_key="LIT-0",
            attempt_id=aid,
            hitl_policy=HitlPolicy(),
            call_seq=1,
        ),
        writer=rt.writer,
        artifacts=arts,
    )
    assert len(out.artifacts) == 2
    digests = {a.sha256 for a in out.artifacts}
    events = await rt.store.read_all()
    sf = [
        e
        for e in events
        if e.type == "SourceFetched" and e.payload.get("digest") in digests
    ]
    assert len(sf) == 2
    # Caps minted for this invoke_tool call must be gone (issued + write permits).
    assert effects_mod._ISSUED_FETCH_CAPS == {}
    assert effects_mod._SOURCE_FETCHED_WRITE_PERMITS == {}
    await close_runtime(rt)


def test_core_cannot_bypass_source_fetched_with_bool_flag(tmp_path: Path, clock):
    """Direct core store path cannot grant SourceFetched without effects permit."""
    import asyncio

    from coscientist.core.events.errors import ArtifactRejected
    from coscientist.core.events.events import EventEnvelope, make_idem

    async def _run() -> None:
        rt = await open_runtime(tmp_path, "st_core_bypass", clock=clock)
        await rt.create_study(
            profile="research",
            request="q",
            catalog_version="0.1.0-pr14",
            deadline=clock.now() + timedelta(seconds=30),
            sid="st_core_bypass",
            finalize_reserve_s=5,
        )
        ev = EventEnvelope(
            study_id="st_core_bypass",
            type="SourceFetched",
            entity_key="bypass",
            idem_key=make_idem("SourceFetched", "bypass"),
            payload={
                "call_id": "a:1",
                "tool": "papers_get",
                "digest": "abc",
                "url": "https://x",
            },
            ts=datetime.now(UTC),
            schema_version=1,
epoch=0, payload_digest=payload_digest({
                "call_id": "a:1",
                "tool": "papers_get",
                "digest": "abc",
                "url": "https://x",
            }),
)
        # No kwarg / empty id / random id — all refused (effects registry).
        with pytest.raises(ArtifactRejected, match="fetch_capability_required"):
            await rt.store.append_many([ev])
        with pytest.raises(ArtifactRejected, match="fetch_capability_required"):
            await rt.store.append_many([ev], _source_fetched_cap_id="")
        with pytest.raises(ArtifactRejected, match="fetch_capability_required"):
            await rt.store.append_many(
                [ev], _source_fetched_cap_id="not-from-effects-registry"
            )
        await close_runtime(rt)

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_append_failure_on_second_digest_clears_permits(
    tmp_path: Path, clock, fetch_registry, monkeypatch
):
    """Remint inside invoke_tool (effects.py) fails on 2nd SourceFetched → permits cleared."""
    import coscientist.core.execution.source_fetch as sf_mod
    from coscientist.tools import effects as effects_mod

    catalog = _catalog_quote()
    sid = "st_append_fail"
    rt = await open_runtime(tmp_path, sid, clock=clock, catalog=catalog)
    await rt.create_study(
        profile="research",
        request="q",
        catalog_version="0.1.0-pr14",
        deadline=clock.now() + timedelta(seconds=60),
        sid=sid,
        finalize_reserve_s=5,
    )
    _ex, arts = attach_executor(rt, tmp_path)
    rt.artifacts = arts
    for key in ("SPEC", "HYP", "LIT"):
        await record_success(rt, await rt.start_attempt(key), arts)
    await rt.accept_plan(
        [
            TaskSpec(
                task_key="LIT-0",
                title="lit",
                goal="g",
                route="literature",
                depends_on=["PLAN"],
                criteria=[RequirementRef(template_id="quote_ok")],
            )
        ],
        questions=[],
    )
    aid = await rt.start_attempt("LIT-0")
    assert aid is not None

    class _MultiFetch:
        def __init__(self, store: LocalArtifactStore):
            self.spec = ToolSpec(name="papers_get", effect="read", is_fetch=True)
            self.arts = store

        async def call(self, args: dict[str, Any], ctx: CallCtx) -> ToolOutput:
            call_id = f"{ctx.attempt_id}:{ctx.call_seq}"
            arts_out = []
            for i, body in enumerate(
                (
                    b"First paper has four word tokens right here now.",
                    b"Second paper has four word tokens right here now.",
                )
            ):
                arts_out.append(
                    self.arts.put_fetched(
                        body,
                        make_tool_ref(f"{call_id}:{i}", tool=self.spec.name),
                        name=f"p{i}.txt",
                        media_type="text/plain",
                        role="paper",
                        capability=ctx.fetch_capability,
                        expected_tool=self.spec.name,
                        expected_call_id=call_id,
                        expected_study_id=ctx.study_id,
                        expected_attempt_id=ctx.attempt_id,
                    )
                )
            return ToolOutput(
                data={"url": "https://normalized.example/multi"},
                artifacts=arts_out,
            )

    real_append = sf_mod.append_source_fetched
    n = {"k": 0}

    async def _boom_second(*a, **k):
        n["k"] += 1
        if n["k"] >= 2:
            raise RuntimeError("simulated append failure")
        return await real_append(*a, **k)

    monkeypatch.setattr(sf_mod, "append_source_fetched", _boom_second)
    with pytest.raises(RuntimeError, match="simulated append failure"):
        await invoke_tool(
            _MultiFetch(arts),
            {"url": "https://agent.example/multi"},
            CallCtx(
                study_id=sid,
                task_key="LIT-0",
                attempt_id=aid,
                hitl_policy=HitlPolicy(),
                call_seq=1,
            ),
            writer=rt.writer,
            artifacts=arts,
        )
    assert n["k"] >= 2  # remint path reached second append_source_fetched
    assert effects_mod._ISSUED_FETCH_CAPS == {}
    assert effects_mod._SOURCE_FETCHED_WRITE_PERMITS == {}
    await close_runtime(rt)


def test_attempt_tainted_without_is_fetch_does_not_pair():
    """Negative: AttemptTainted missing is_fetch must not create fetch_calls."""
    from coscientist.core.events.events import EventEnvelope
    from coscientist.core.events.projections import apply_to_map
    from coscientist.core.execution.source_fetch import (
        SourceFetchIndex,
        apply_source_fetch_events,
        note_fetch_call_from_attempt_tainted,
    )

    payload = {
        "attempt_id": "att",
        "reason": "source_fetch",
        "call_id": "att:1",
        "tool": "papers_get",
        # is_fetch omitted
    }
    assert note_fetch_call_from_attempt_tainted(payload) is None
    idx = SourceFetchIndex()
    apply_source_fetch_events(
        idx,
        [
            EventEnvelope(
                study_id="s",
                type="AttemptTainted",
                entity_key="att",
                idem_key="AttemptTainted:att:att:1",
                payload=payload,
                ts=datetime.now(UTC),
                schema_version=1,
                seq=1,
                    epoch=0,
                    payload_digest=payload_digest(payload),
                ),
            EventEnvelope(
                study_id="s",
                type="SourceFetched",
                entity_key="x",
                idem_key="SourceFetched:x",
                payload={
                    "call_id": "att:1",
                    "tool": "papers_get",
                    "digest": "deadbeef",
                    "url": "https://x",
                },
                ts=datetime.now(UTC),
                schema_version=1,
                seq=2,
epoch=0, payload_digest=payload_digest({
                    "call_id": "att:1",
                    "tool": "papers_get",
                    "digest": "deadbeef",
                    "url": "https://x",
                }),
),
        ],
    )
    assert "deadbeef" not in idx.digests()
    proj: dict = {}
    apply_to_map(
        proj,
        EventEnvelope(
            study_id="s",
            type="AttemptTainted",
            entity_key="att",
            idem_key="AttemptTainted:att:att:1b",
            payload=payload,
            ts=datetime.now(UTC),
            schema_version=1,
            seq=1,
                    epoch=0,
                    payload_digest=payload_digest(payload),
                ),
    )
    assert not proj.get("fetch_calls")


def test_attempt_tainted_renamed_tool_still_pairs_from_journal(fetch_registry):
    """Unregistered (renamed) tool with journaled is_fetch still pairs — no silent drop."""
    from coscientist.core.execution.source_fetch import note_fetch_call_from_attempt_tainted

    marked = note_fetch_call_from_attempt_tainted(
        {
            "reason": "source_fetch",
            "is_fetch": True,
            "tool": "papers_get_old_name",
            "call_id": "att:9",
        }
    )
    assert marked == ("att:9", "papers_get_old_name")


def test_attempt_tainted_reclassified_raises_domain(fetch_registry):
    """Registered tool with is_fetch=False vs journaled True → FetchToolReclassified."""
    from coscientist.core.events.errors import FetchToolReclassified
    from coscientist.core.execution.source_fetch import note_fetch_call_from_attempt_tainted

    with pytest.raises(FetchToolReclassified) as ei:
        note_fetch_call_from_attempt_tainted(
            {
                "reason": "source_fetch",
                "is_fetch": True,
                "tool": "catalog_read",
                "call_id": "att:1",
            }
        )
    assert ei.value.tool == "catalog_read"
    assert ei.value.call_id == "att:1"


_RECLASS_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "journals"
    / "reclassified_fetch_old"
)

# Raw schema_version values committed in events.db (schema v1 baseline).
_RECLASS_RAW_SCHEMA_VERSIONS = (
    ("StudyCreated", 1),
    ("PlanAccepted", 1),
    ("TaskCreated", 1),
    ("AttemptStarted", 1),
    ("AttemptTainted", 1),
    ("SourceFetched", 1),
)


def _assert_raw_fixture_schema_versions(db_path: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT type, schema_version FROM events ORDER BY seq"
        ).fetchall()
    finally:
        conn.close()
    assert [(t, int(sv)) for t, sv in rows] == list(_RECLASS_RAW_SCHEMA_VERSIONS)


def _assert_single_study_finalized(events: list[Any]):
    """Exactly one StudyFinalized; report XOR minimal_report; non-empty conclusions."""
    fins = [e for e in events if e.type == "StudyFinalized"]
    assert len(fins) == 1
    fin = fins[0]
    p = fin.payload if isinstance(fin.payload, dict) else {}
    has_report = bool(p.get("report_artifact_id"))
    has_minimal = bool(p.get("minimal_report"))
    assert has_report ^ has_minimal, (
        f"expected report XOR minimal_report, got report={has_report} "
        f"minimal={has_minimal}"
    )
    cids = p.get("conclusion_ids")
    assert isinstance(cids, list) and len(cids) > 0
    return fin


def _install_reclassified_papers_get() -> None:
    _reset_registry_for_tests()
    _register_tool_for_tests(
        RegisteredToolSpec(name="papers_get", effect="read", is_fetch=False)
    )
    freeze_registry()


def _copy_reclass_fixture(tmp_path: Path) -> tuple[str, str, str, Path]:
    import shutil

    meta = json.loads(
        (_RECLASS_FIXTURE.parent / "reclassified_fetch_old.json").read_text()
    )
    sid = str(meta["study_id"])
    digest = str(meta["digest"])
    call_id = str(meta["call_id"])
    dest = tmp_path / sid
    dest.mkdir(parents=True)
    db = dest / "events.db"
    shutil.copy2(_RECLASS_FIXTURE / "events.db", db)
    return sid, digest, call_id, db


@pytest.mark.asyncio
async def test_reclassified_tool_study_opens_finalizes_uncounted(
    tmp_path: Path, clock
):
    """MAJOR: schema-v1 fixture opens under reclassified tool; natural finalize.

    Committed journal (complete v1 StudyCreated / PlanAccepted / TaskCreated +
    AttemptTainted is_fetch=True + SourceFetched). No production upcasters.
    Finalization via tick/deadline — not finalize(trigger=...).
    """
    from coscientist.core.events.projections import fold_events
    from coscientist.core.events.schema_versions import STUDY_CREATED_SCHEMA_VERSION
    from coscientist.core.execution.source_fetch import load_source_fetch_index_from_writer

    sid, digest, call_id, db = _copy_reclass_fixture(tmp_path)
    _assert_raw_fixture_schema_versions(db)

    _install_reclassified_papers_get()

    catalog = _catalog_quote()
    rt = await open_runtime(tmp_path, sid, clock=clock, catalog=catalog, owner="t2")
    events_open = await rt.store.read_all()
    sc = next(e for e in events_open if e.type == "StudyCreated")
    assert sc.schema_version == STUDY_CREATED_SCHEMA_VERSION == 1
    assert any(e.type == "FetchIntegrityRaised" for e in events_open)

    folded = fold_events(events_open)
    assert digest not in (folded.get("source_fetches") or {})
    assert call_id in (folded.get("fetch_integrity") or {})

    idx = await load_source_fetch_index_from_writer(rt.writer)
    assert digest not in idx.digests()
    assert digest not in idx.digests_for_attempt("att_lit")
    # Read path must not append further integrity events.
    n_integrity = sum(1 for e in events_open if e.type == "FetchIntegrityRaised")
    await load_source_fetch_index_from_writer(rt.writer)
    assert (
        sum(1 for e in await rt.store.read_all() if e.type == "FetchIntegrityRaised")
        == n_integrity
    )

    await rt.ensure_lease(ttl_s=300)
    clock.advance(timedelta(seconds=120))
    await rt.tick()
    assert rt.view().phase == "closed"
    events = await rt.store.read_all()
    fin = _assert_single_study_finalized(events)
    assert fin.payload.get("trigger") == "deadline"
    assert "internal_error" in (fin.payload.get("contributing_reasons") or [])
    assert digest not in (fold_events(events).get("source_fetches") or {})
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_reclassify_internal_error_survives_reconcile_failure(
    tmp_path: Path, clock, monkeypatch
):
    """MAJOR: fold_events fallback keeps internal_error when reconcile raises."""
    sid, _digest, _call_id, db = _copy_reclass_fixture(tmp_path)
    _assert_raw_fixture_schema_versions(db)
    _install_reclassified_papers_get()

    async def boom(_writer):
        raise RuntimeError("reconcile_boom")

    monkeypatch.setattr(
        "coscientist.core.execution.source_fetch.reconcile_fetch_integrity", boom
    )

    rt = await open_runtime(
        tmp_path, sid, clock=clock, catalog=_catalog_quote(), owner="t2"
    )
    await rt.ensure_lease(ttl_s=300)
    clock.advance(timedelta(seconds=120))
    await rt.tick()
    fin = _assert_single_study_finalized(await rt.store.read_all())
    assert "internal_error" in (fin.payload.get("contributing_reasons") or [])
    await close_runtime(rt)


@pytest.mark.asyncio
async def test_open_reconcile_lease_lost_propagates(tmp_path: Path, clock, monkeypatch):
    """LeaseLost from reconcile_fetch_integrity on open must not be swallowed."""
    from coscientist.core.events.errors import LeaseLost

    sid, _digest, _call_id, db = _copy_reclass_fixture(tmp_path)
    _assert_raw_fixture_schema_versions(db)
    _install_reclassified_papers_get()

    async def boom_lease(_writer):
        raise LeaseLost("reconcile lease lost")

    monkeypatch.setattr(
        "coscientist.core.execution.source_fetch.reconcile_fetch_integrity", boom_lease
    )
    with pytest.raises(LeaseLost, match="reconcile lease lost"):
        await open_runtime(
            tmp_path, sid, clock=clock, catalog=_catalog_quote(), owner="t2"
        )


@pytest.mark.asyncio
async def test_open_finalized_study_does_not_append_fetch_integrity(
    tmp_path: Path, clock
):
    """MAJOR: reopen of StudyFinalized must not journal FetchIntegrityRaised after it."""
    sid, _digest, _call_id, db = _copy_reclass_fixture(tmp_path)
    _assert_raw_fixture_schema_versions(db)
    _install_reclassified_papers_get()

    rt = await open_runtime(
        tmp_path, sid, clock=clock, catalog=_catalog_quote(), owner="t1"
    )
    await rt.ensure_lease(ttl_s=300)
    clock.advance(timedelta(seconds=120))
    await rt.tick()
    events_closed = await rt.store.read_all()
    fin = _assert_single_study_finalized(events_closed)
    fin_seq = int(fin.seq or 0)
    integrity_before = [
        e for e in events_closed if e.type == "FetchIntegrityRaised"
    ]
    assert integrity_before
    assert all(int(e.seq or 0) < fin_seq for e in integrity_before)
    await close_runtime(rt)

    _install_reclassified_papers_get()
    rt2 = await open_runtime(
        tmp_path, sid, clock=clock, catalog=_catalog_quote(), owner="t2"
    )
    assert rt2._finalized  # noqa: SLF001
    events2 = await rt2.store.read_all()
    assert len([e for e in events2 if e.type == "StudyFinalized"]) == 1
    after_fin = [e for e in events2 if int(e.seq or 0) > fin_seq]
    assert after_fin == [], f"events after StudyFinalized: {[e.type for e in after_fin]}"
    assert [
        e for e in events2 if e.type == "FetchIntegrityRaised"
    ] == integrity_before
    await close_runtime(rt2)


def test_fetch_integrity_row_key_without_call_id():
    """Projection must not drop fetch_integrity rows that lack call_id.

    ``_row_key`` order for fetch_integrity (first non-empty wins):
    ``call_id`` → ``tool`` → ``tool_name`` → ``row_key``.
    """
    from coscientist.core.events.events import EventEnvelope, make_idem
    from coscientist.core.events.projections import _row_key, apply_to_map, fold_events

    # Empty call_id → tool.
    assert (
        _row_key("fetch_integrity", {"tool": "papers_get", "call_id": ""})
        == "papers_get"
    )
    # call_id wins over tool and row_key.
    assert (
        _row_key(
            "fetch_integrity",
            {
                "call_id": "att:1",
                "tool": "papers_get",
                "tool_name": "other",
                "row_key": "rk",
            },
        )
        == "att:1"
    )
    # No call_id: tool wins over tool_name and row_key.
    assert (
        _row_key(
            "fetch_integrity",
            {
                "call_id": "",
                "tool": "papers_get",
                "tool_name": "other",
                "row_key": "rk",
            },
        )
        == "papers_get"
    )
    # tool empty → tool_name before row_key.
    assert (
        _row_key(
            "fetch_integrity",
            {"call_id": "", "tool": "", "tool_name": "tn", "row_key": "rk"},
        )
        == "tn"
    )

    ev = EventEnvelope(
        study_id="st",
        type="FetchIntegrityRaised",
        entity_key="papers_get",
        idem_key=make_idem("FetchIntegrityRaised", "papers_get"),
        payload={
            "kind": "fetch_tool_reclassified",
            "tool": "papers_get",
            "call_id": "",
            "internal_error": True,
        },
        ts=datetime.now(UTC),
        schema_version=1,
epoch=0, payload_digest=payload_digest({
            "kind": "fetch_tool_reclassified",
            "tool": "papers_get",
            "call_id": "",
            "internal_error": True,
        }),
)
    folded = fold_events([ev])
    assert "papers_get" in (folded.get("fetch_integrity") or {})
    # Round-trip via _row_key after store-shaped row.
    row = folded["fetch_integrity"]["papers_get"]
    assert _row_key("fetch_integrity", row) == "papers_get"
    proj: dict = {}
    apply_to_map(proj, ev)
    assert _row_key("fetch_integrity", proj["fetch_integrity"]["papers_get"])


def test_fetch_capability_mismatch_on_wrong_bind(fetch_registry):
    """verify/assert raise fetch_capability_mismatch on wrong tool/call/study."""
    from coscientist.core.events.errors import ArtifactRejected
    from coscientist.core.execution.source_fetch import (
        assert_fetch_capability_bound,
        verify_fetch_capability,
    )
    from coscientist.tools import effects as effects_mod

    cap = _cap("att:1", "papers_get", study_id="st_x")
    with pytest.raises(ArtifactRejected, match="fetch_capability_mismatch"):
        assert_fetch_capability_bound(
            cap,
            tool="wrong_tool",
            call_id="att:1",
            study_id="st_x",
            attempt_id="att",
        )
    with pytest.raises(ArtifactRejected, match="fetch_capability_mismatch"):
        verify_fetch_capability(
            cap,
            tool="papers_get",
            call_id="att:99",
            study_id="st_x",
            attempt_id="att",
        )
    # Cap still issued (mismatch fails in lookup before consume).
    assert cap.id in effects_mod._ISSUED_FETCH_CAPS
    effects_mod._invalidate_fetch_capability(cap)
    assert effects_mod._ISSUED_FETCH_CAPS == {}
    assert effects_mod._SOURCE_FETCHED_WRITE_PERMITS == {}


def test_take_source_fetched_write_permit_mismatches(fetch_registry):
    """_take_source_fetched_write_permit rejects foreign tool / call / study."""
    from coscientist.core.events.errors import ArtifactRejected
    from coscientist.core.execution.source_fetch import (
        take_source_fetched_write_permit,
        verify_fetch_capability,
    )
    from coscientist.tools import effects as effects_mod

    cap = _cap("att:1", "papers_get", study_id="st_x")
    verify_fetch_capability(
        cap,
        tool="papers_get",
        call_id="att:1",
        study_id="st_x",
        attempt_id="att",
    )
    assert cap.id in effects_mod._SOURCE_FETCHED_WRITE_PERMITS
    with pytest.raises(ArtifactRejected, match="fetch_capability_mismatch"):
        take_source_fetched_write_permit(
            cap.id, tool="foreign_tool", call_id="att:1", study_id="st_x"
        )
    # Mismatch must not consume the permit.
    assert cap.id in effects_mod._SOURCE_FETCHED_WRITE_PERMITS
    with pytest.raises(ArtifactRejected, match="fetch_capability_mismatch"):
        take_source_fetched_write_permit(
            cap.id, tool="papers_get", call_id="att:99", study_id="st_x"
        )
    with pytest.raises(ArtifactRejected, match="fetch_capability_mismatch"):
        take_source_fetched_write_permit(
            cap.id, tool="papers_get", call_id="att:1", study_id="st_other"
        )
    take_source_fetched_write_permit(
        cap.id, tool="papers_get", call_id="att:1", study_id="st_x"
    )
    assert cap.id not in effects_mod._SOURCE_FETCHED_WRITE_PERMITS


def test_release_without_handler_raises(monkeypatch):
    """release must not silently no-op when no handler is registered."""
    import coscientist.core.execution.source_fetch as sf_mod

    monkeypatch.setattr(sf_mod, "_release_write_permit_impl", None)
    with pytest.raises(ArtifactRejected, match="fetch_capability_required"):
        sf_mod.release_source_fetched_write_permit("orphan-cap-id")
