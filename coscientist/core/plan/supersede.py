"""Question/criterion supersede helpers (M1 PR9 / C10 / §11 / MJ-b)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Protocol

from coscientist.core.claims.graph import GraphStore
from coscientist.core.events.ids import (
    criterion_node_id,
    experiment_node_id,
    hypothesis_node_id,
    question_node_id,
)
from coscientist.core.execution.domain import (
    Catalog,
    RequirementTemplate,
    Study,
    Task,
    TaskSpec,
    TaskState,
)

TestsIntent = Literal["confirm", "refute"]


class SupersedeRuntime(Protocol):
    """Read surface of the execution runtime (machine.py ``StudyRuntime``).

    Every attribute is created unconditionally in ``StudyRuntime.__init__``;
    read-only properties keep the structural match covariant. Helpers here take
    this protocol so plan/supersede never imports execution.machine (the
    claims-side caller would close a forbidden agents→tools import chain).
    """

    @property
    def study(self) -> Study | None: ...

    @property
    def graph(self) -> GraphStore: ...

    @property
    def results(self) -> Mapping[str, Mapping[str, object]]: ...

    @property
    def catalog(self) -> Catalog: ...

    @property
    def tasks(self) -> Mapping[str, Task]: ...


def plan_node_id(study_id: str, pid: str) -> str:
    return f"{study_id}:plan:{pid}"


def question_is_superseded(graph: GraphStore | None, study_id: str, question_id: str) -> bool:
    """True when some newer question −supersedes→ this question."""
    if graph is None or not question_id:
        return False
    qnid = question_node_id(study_id, question_id)
    for e in graph.all_edges():
        if e.kind == "supersedes" and e.dst_id == qnid:
            src = graph.get_node(e.src_id)
            if src is not None and src.kind == "question":
                return True
    return False


def criterion_is_superseded(graph: GraphStore | None, study_id: str, criterion_key: str) -> bool:
    if graph is None or not criterion_key:
        return False
    cnid = criterion_node_id(study_id, criterion_key)
    for e in graph.all_edges():
        if e.kind == "supersedes" and e.dst_id == cnid:
            src = graph.get_node(e.src_id)
            if src is not None and src.kind == "criterion":
                return True
    return False


def countable_refutation_n(runtime: SupersedeRuntime, target_id: str) -> int:
    """Count claims that ``counts()`` and are inconsistent for this target."""
    study = runtime.study
    graph = runtime.graph
    if study is None or graph is None or not target_id:
        return 0
    from coscientist.core.claims.claims import RuntimeClaim, filtered_claims_by_target
    from coscientist.core.claims.verdict import effective_outcome

    by = filtered_claims_by_target(graph, study_id=study.study_id, results=runtime.results)
    claims: list[RuntimeClaim] = []
    for key in (f"question:{target_id}", f"hypothesis:{target_id}", target_id):
        found = by.get(key)
        if found:
            claims.extend(found)
    n = 0
    for c in claims:
        if c.counts() and effective_outcome(c) == "inconsistent":
            n += 1
    return n


def _criteria_prediction_capable(runtime: SupersedeRuntime, spec: TaskSpec) -> bool:
    """True when any criterion has catalog role prediction (refutation-capable)."""
    by_id: dict[str, RequirementTemplate] = {t.id: t for t in runtime.catalog.templates}
    for ref in spec.criteria:
        tid = ref.template_id
        tmpl = by_id.get(tid) if tid else None
        if tmpl is None:
            if ref.role == "prediction":
                return True
            continue
        if tmpl.role == "prediction":
            return True
        if tmpl.predicts_null:
            return True
    return False


def _task_links_to_target(
    runtime: SupersedeRuntime,
    task: Task,
    target_id: str | None = None,
) -> bool:
    """Linked via addresses, tests edge, or declared target."""
    spec = task.spec
    addrs = list(spec.addresses)
    if target_id is None:
        if addrs:
            return True
    elif target_id in addrs:
        return True
    study = runtime.study
    graph = runtime.graph
    if study is None or graph is None:
        return bool(addrs) if target_id is None else False
    key = spec.task_key
    if not key:
        return False
    exp_id = experiment_node_id(study.study_id, str(key))
    for e in graph.edges_from(exp_id):
        if e.kind != "tests":
            continue
        if target_id is None:
            return True
        hid = hypothesis_node_id(study.study_id, target_id)
        if e.dst_id == hid:
            return True
    return False


def _has_refute_tests_edge(runtime: SupersedeRuntime, task: Task) -> bool:
    study = runtime.study
    graph = runtime.graph
    if study is None or graph is None:
        return False
    key = task.spec.task_key
    if not key:
        return False
    exp_id = experiment_node_id(study.study_id, str(key))
    for e in graph.edges_from(exp_id):
        if e.kind == "tests" and (e.attrs or {}).get("intent") == "refute":
            return True
    return False


def task_protected_from_agent_skip(runtime: SupersedeRuntime, task: Task) -> bool:
    """True when agent path must not SKIP/supersede this open task (MJ-b).

    Structure: linked (tests/addresses/declared target) to a hypothesis/question
    with an open prediction / refutation-capable check → protected.
    Planner ``confirm`` never removes protection. Planner ``refute`` only ADDS it
    (even without a prediction role or tests edge).
    """
    if task.state == TaskState.DONE:
        return False
    spec = task.spec
    # Structure-based protection (confirm cannot strip this).
    if _task_links_to_target(runtime, task) and _criteria_prediction_capable(runtime, spec):
        return True
    # Graph tests.intent=refute.
    if _has_refute_tests_edge(runtime, task):
        return True
    # Planner refute only ADDS protection.
    if spec.tests_intent == "refute":
        return True
    return False


# Back-compat alias used by older call sites.
def task_has_refute_intent(runtime: SupersedeRuntime, task: Task) -> bool:
    return task_protected_from_agent_skip(runtime, task)


def has_open_refute_task(runtime: SupersedeRuntime, target_id: str) -> bool:
    """True when an open protected task is linked to this question/hypothesis."""
    if not target_id:
        return False
    for task in runtime.tasks.values():
        if task.state == TaskState.DONE:
            continue
        if not task_protected_from_agent_skip(runtime, task):
            continue
        if _task_links_to_target(runtime, task, target_id):
            return True
    return False


def target_blocks_agent_supersede(runtime: SupersedeRuntime, target_id: str) -> str | None:
    """Agent-path block reason, or None if supersede/SKIP is allowed."""
    if countable_refutation_n(runtime, target_id) >= 1:
        return "has_refutation"
    if has_open_refute_task(runtime, target_id):
        return "open_refute_task"
    return None


def question_has_countable_refutation(runtime: SupersedeRuntime, question_id: str) -> bool:
    return target_blocks_agent_supersede(runtime, question_id) is not None


def tasks_needed_by_active_questions(
    *,
    tasks: list[TaskSpec],
    superseded_old: set[str],
    active_q_after: set[str],
) -> set[str]:
    """Task keys that active (non-superseded / new) questions' tasks depend on."""
    by_key: dict[str, TaskSpec] = {}
    for t in tasks:
        if t.task_key:
            by_key[t.task_key] = t

    keepers = {k for k, t in by_key.items() if set(t.addresses) & active_q_after}
    for k, t in by_key.items():
        addrs = set(t.addresses)
        if addrs and not (addrs <= superseded_old):
            keepers.add(k)

    needed: set[str] = set(keepers)
    changed = True
    while changed:
        changed = False
        for k in list(needed):
            dep = by_key.get(k)
            if dep is None:
                continue
            for d in dep.depends_on:
                if d not in needed and d in by_key:
                    needed.add(d)
                    changed = True
    return needed


def should_skip_for_question_supersede(
    *,
    task: TaskSpec,
    superseded_old: set[str],
    active_q_after: set[str],
    protected_keys: set[str],
    blocked_questions: set[str] | None = None,
    refute_keys: set[str] | None = None,
) -> bool:
    """True when an open task solely addresses superseded questions and is not shared."""
    key = task.task_key
    if key and key in protected_keys:
        return False
    if key and refute_keys and key in refute_keys:
        return False
    addrs = set(task.addresses)
    if not addrs:
        return False
    if blocked_questions and (addrs & blocked_questions):
        return False
    if addrs <= superseded_old and not (addrs & active_q_after):
        return True
    return False
