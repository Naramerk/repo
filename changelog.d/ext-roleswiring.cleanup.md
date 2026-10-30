`tests/cli/test_roles_wiring.py`: `type: ignore` 13→12 вхождений (Any, getattr, cast,
audit-ok 0→0). `_critic_body(decision)` replaced `str` with `CriticDecision`; its three
callers (`codegraph callers _critic_body`) pass only the literals "accept"/"revise"
(rule 21). Issues are built as `CriticIssue(path=..., message=...)`.
The 12 summary-JSON ignores are kept: the summary is `json.loads` output, and no
runtime check proves every `run_study` usage row matches a typed shape.
