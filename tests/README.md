# tests

- `refmodel.py` — исполняемая эталонная модель правил RESEARCH_MODEL.md (уровень валидатора, без runtime).
- Фикстура `impl` (conftest.py) прогоняет тесты правил на `refmodel` и на ядре `coscientist.core.rules`.
- `Graph` / `add_paper` / `Claim` (с тестовыми дефолтами X1#1/A1) живут только в `refmodel`;
  runtime `coscientist.core.rules` держит edge_allowed, validate_plan/check_cardinality (duck-typed),
  claim_stance/verdict/study_*, DelegatedBudget/costly_gate/settle.

## Требуемый API `coscientist.core.rules` (runtime)

```python
edge_allowed(name: str, src_kind: str, dst_kind: str) -> bool
class PlanInvalid(ValueError)
validate_plan(gr, plan_id: str) -> None                      # duck-typed graph; raises PlanInvalid
check_cardinality(gr) -> list[str]
claim_stance(criterion_status: str) -> Literal["supports","refutes","inconclusive"]
verdict(claims, any_experiment_done: bool, k: int = 1) -> tuple[str, bool]
study_fulfilled(question_verdicts: dict[str, str], all_required_success: bool) -> bool
class DelegatedBudget:             # DelegatedBudget(limit_rub); .spent_rub, .remaining_rub, .blocked
                                   # refuse_reason(estimate_rub) -> str | None; debit(actual_rub, estimate_rub) -> float
costly_gate(mode, answer, cost_estimate_rub=0.0, budget: DelegatedBudget | None = None,
            critic_accepts=True) -> tuple[str, dict]
settle(event: dict, budget: DelegatedBudget | None, actual_rub: float) -> dict
study_view_verdict(all_required_success: bool, question_verdicts: dict[str, str]) -> str
```

## Oracle-only API (`tests/refmodel.py`)

```python
class Graph:                       # add/link/out/inc/of; add_paper(producer, doi_or_url, attempt_n=1)
class Claim:                       # Claim(criterion_status, … defaults attempt_id="X1#1", artifact_id="A1")
```
