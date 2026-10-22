# tests

- `refmodel.py` — исполняемая эталонная модель правил RESEARCH_MODEL.md (уровень валидатора, без runtime; **без** импортов `coscientist.*`).
- Фикстура `impl` (conftest.py) прогоняет тесты правил на `refmodel` и на ядре `coscientist.core.rules`.
- `impl=core` использует `core_rules_adapter.py`: Graph/Claim из refmodel + функции из `core.rules`
  (verdict / validate_plan / cardinality). **Paper dedupe** (`add_paper`) — только refmodel
  (`test_paper_dedupe_two_lits_same_doi`); под `impl=core` адаптер всё равно даёт refmodel.Graph,
  поэтому dual-param не проверял бы ядро. Дедуп в core — M1 GraphBuilder.
- `Graph` / `add_paper` / `Claim` (с тестовыми дефолтами X1#1/A1) живут только в `refmodel`;
  runtime `coscientist.core.rules` держит edge_allowed, validate_plan/check_cardinality (duck-typed),
  claim_stance/verdict/study_*.
- HITL / `DelegatedBudget` / `costly_gate` / report — `coscientist.experimental` (pre-M2/M5);
  тесты в `tests/experimental/`. Каталог — `coscientist.checks` (M1); тесты в `tests/checks/`.
- `critic_accepts` в `costly_gate` — **stub** (нет реального critic до M2); см. DESIGN §15.

## Требуемый API `coscientist.core.rules` (M0 runtime)

```python
edge_allowed(name: str, src_kind: str, dst_kind: str) -> bool
class PlanInvalid(ValueError)
validate_plan(gr, plan_id: str) -> None                      # duck-typed graph; raises PlanInvalid
check_cardinality(gr) -> list[str]
claim_stance(criterion_status: str) -> Literal["supports","refutes","inconclusive"]
verdict(claims, any_experiment_done: bool, k: int = 1) -> tuple[str, bool]
study_fulfilled(question_verdicts: dict[str, str], all_required_success: bool) -> bool
study_view_verdict(all_required_success: bool, question_verdicts: dict[str, str]) -> str
```

## Experimental API (`coscientist.experimental`, pre-M2/M5)

```python
class DelegatedBudget:             # DelegatedBudget(limit_rub); .spent_rub, .remaining_rub, .blocked
                                   # refuse_reason(estimate_rub) -> str | None; debit(actual_rub, estimate_rub) -> float
costly_gate(mode, answer, cost_estimate_rub=0.0, budget: DelegatedBudget | None = None,
            critic_accepts=True) -> tuple[str, dict]   # critic_accepts is a stub until M2
settle(event: dict, budget: DelegatedBudget | None, actual_rub: float) -> dict
render(events) -> dict             # HITL-spend report stub
```

## Oracle-only API (`tests/refmodel.py`)

```python
class Graph:                       # add/link/out/inc/of; add_paper(producer, doi_or_url, attempt_n=1)
class Claim:                       # Claim(criterion_status, … defaults attempt_id="X1#1", artifact_id="A1")
# plus oracle copies of costly_gate / DelegatedBudget for dual-impl comparison
```
