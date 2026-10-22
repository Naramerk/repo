# tests

- `refmodel.py` — исполняемая эталонная модель правил RESEARCH_MODEL.md (уровень валидатора, без runtime).
- Фикстура `impl` (conftest.py) прогоняет тесты правил на `refmodel` и на ядре `coscientist.core.rules`;
  ядро пропускается (`importorskip`), пока модуля нет.

## Требуемый API `coscientist.core.rules`

Те же имена и сигнатуры, что в `refmodel.py`:

```python
edge_allowed(name: str, src_kind: str, dst_kind: str) -> bool
class Graph:                       # add(nid, kind, **f) -> nid; link(name, src, dst, **attrs); out/inc(name, nid) -> list[str]
                                   # of(kind) -> list[str]; add_paper(producer, doi_or_url, attempt_n=1) -> nid
class PlanInvalid(ValueError)
validate_plan(gr: Graph, plan_id: str) -> None                      # raises PlanInvalid (RT1, покрытие question)
check_cardinality(gr: Graph) -> list[str]                           # "<edge> out|in <node>=<count>"
claim_stance(criterion_status: str) -> Literal["supports","refutes","inconclusive"]
class Claim:                       # Claim(criterion_status, accepted=True, evidence_kind="computational",
                                   #       provenance="real", frozen=True, attempt_outcome="SUCCESS",
                                   #       attempt_id="X1#1", artifact_id="A1", lit_own_criterion=False); .stance вычисляется
verdict(claims: list[Claim], any_experiment_done: bool, k: int = 1) -> tuple[str, bool]   # (вердикт, «по литературе»)
study_fulfilled(question_verdicts: dict[str, str], all_required_success: bool) -> bool
class DelegatedBudget:             # DelegatedBudget(limit_rub); .spent_rub, .remaining_rub, .blocked
                                   # refuse_reason(estimate_rub) -> str | None; debit(actual_rub, estimate_rub) -> float
costly_gate(mode, answer, cost_estimate_rub=0.0, budget: DelegatedBudget | None = None,
            critic_accepts=True) -> tuple[str, dict]                # effect, event (decided_by, delegated_by, reason,
                                                                    # spent_rub, human_message?)
settle(event: dict, budget: DelegatedBudget | None, actual_rub: float) -> dict
study_view_verdict(all_required_success: bool, question_verdicts: dict[str, str]) -> str   # обёртка над study_view()
```
