/** Journal-only plan history. Proposed plans and recorded approvals are not acceptance. */
const object = value => value !== null && typeof value === "object" && !Array.isArray(value);
const text = value => typeof value === "string" && value.length > 0;
const array = value => Array.isArray(value) ? value : [];

function candidatePlan(prompt) {
  if (!object(prompt) || !text(prompt.question) || !text(prompt.attempt_id)
      || !/^[a-f0-9]{64}$/u.test(prompt.context_sha256) || !Array.isArray(prompt.options) || !prompt.options.length
      || !prompt.options.every(option => option === "accept" || option === "reject")
      || !text(prompt.context_json) || prompt.context_json.length > 1024 * 1024) return null;
  let plan;
  try { plan = JSON.parse(prompt.context_json); }
  catch (error) { if (error instanceof SyntaxError) return null; throw error; }
  const need = value => object(value) && value.kind === "need" && typeof value.needed === "boolean" && text(value.reason);
  if (!object(plan) || !Array.isArray(plan.tasks) || !Array.isArray(plan.questions)
      || !need(plan.hypotheses) || !need(plan.literature)) return null;
  if (!plan.tasks.every(task => object(task) && ["task_key", "title", "goal", "route"].every(key => text(task[key]))
      && ["depends_on", "addresses", "tools_hint", "expected_outputs", "criteria"].every(key => Array.isArray(task[key])))) return null;
  if (!plan.questions.every(question => object(question) && text(question.id) && text(question.text)
      && ["verdict", "artifact", "both"].includes(question.final_type))) return null;
  return plan;
}

export function projectPlan(model) {
  const history = [], proposals = new Map(), states = new Map(), attempts = new Map();
  let current = null;
  const seen = new Set();
  for (const event of model.events) {
    if (seen.has(event.seq)) continue;
    seen.add(event.seq);
    const p = event.payload;
    if (!object(p)) continue;
    if (event.type === "TaskCreated" || event.type === "TaskInherited") {
      states.set(p.task_id, {state: text(p.state) ? p.state : null, outcome: p.outcome ?? null});
    } else if (event.type === "AttemptStarted") {
      attempts.set(p.attempt_id, p.task_id);
      states.set(p.task_id, {state: "RUNNING", outcome: null});
    } else if (event.type === "AttemptClosed" && text(p.retry_at)) {
      const taskId = p.task_id ?? attempts.get(p.attempt_id);
      if (taskId && states.get(taskId)?.state !== "DONE") {
        states.set(taskId, {state: "PENDING", outcome: null, retryAt: p.retry_at});
      }
    } else if (event.type === "TaskDone") {
      states.set(p.task_id, {state: "DONE", outcome: p.outcome ?? null});
    } else if (event.type === "PlanAccepted" || event.type === "PlanExpanded") {
      current = {id: p.plan_id, revision: p.revision, seq: event.seq, snapshot: p, tasks: []};
      history.push({type: event.type, seq: event.seq, ts: event.ts, id: p.plan_id, revision: p.revision,
        reason: p.reason, added: array(p.added_tasks).map(task => task.task_key),
        superseded: array(p.superseded_tasks)});
    } else if (event.type === "PlanRefused" || event.type === "ExpansionRefused") {
      history.push({type: event.type, seq: event.seq, ts: event.ts, reason: p.reason, detail: p.detail});
    } else if (event.type === "HitlOpened" && p.kind === "plan_approval") {
      const plan = candidatePlan(p.prompt);
      if (plan) proposals.set(p.request_id, {requestId: p.request_id, seq: event.seq, plan,
        source: p.prompt.context_json, status: "pending", decision: null});
    } else if (event.type === "HitlDecided" && proposals.has(p.request_id)) {
      const proposal = proposals.get(p.request_id);
      proposal.decision = p.decision;
      proposal.status = "answered";
    } else if (event.type === "HitlTimedOut" && proposals.has(p.request_id)) {
      proposals.get(p.request_id).status = "timed_out";
    } else if (event.type === "StudyFinalized") {
      for (const proposal of proposals.values()) if (proposal.status === "pending") proposal.status = "study_closed";
    }
  }
  if (current) {
    const byKey = new Map();
    for (const task of model.tasks.values()) byKey.set(task.key, byKey.has(task.key) ? null : task);
    current.tasks = array(current.snapshot.tasks).map(spec => {
      const task = byKey.get(spec.task_key);
      const observed = task ? states.get(task.id) : null;
      return {key: spec.task_key, spec, id: task?.id ?? null,
        state: observed?.state ?? null, outcome: observed?.outcome ?? null, retryAt: observed?.retryAt ?? null};
    });
    current.completed = current.tasks.filter(task => task.state === "DONE").length;
    current.unknown = current.tasks.filter(task => !["PENDING", "RUNNING", "DONE"].includes(task.state)).length;
  }
  return {current, history, proposals: [...proposals.values()]};
}

function element(tag, value, className) {
  const node = document.createElement(tag);
  if (value != null) node.textContent = String(value);
  if (className) node.className = className;
  return node;
}

function disclosure(label, key, children, opened, focusTargets) {
  const details = element("details");
  details.open = opened.has(key);
  const summary = element("summary", label);
  summary.dataset.planFocus = key;
  focusTargets.set(key, summary);
  details.append(summary, ...children);
  details.addEventListener("toggle", () => {
    if (details.open) opened.add(key); else opened.delete(key);
  });
  return details;
}

const STATES = {PENDING: "Ожидает выполнения", RUNNING: "Выполняется", DONE: "Завершена"};
const OUTCOMES = {SUCCESS: "успешно", PARTIAL: "частично", FAILED: "ошибка", SKIPPED: "пропущена"};
const HISTORY = {PlanAccepted: "План принят", PlanExpanded: "План расширен",
  PlanRefused: "План отклонён", ExpansionRefused: "Расширение отклонено"};

export class PlanView {
  constructor(host, onSelectTask) {
    this.host = host;
    this.onSelectTask = onSelectTask;
    this.model = null;
    this.signature = null;
    this.opened = new Set();
  }

  render(model) {
    const projection = projectPlan(model);
    const signature = JSON.stringify(projection);
    if (this.model === model && this.signature === signature) return;
    const active = document.activeElement;
    const focusKey = this.model === model && this.host.contains(active) ? active?.dataset?.planFocus : null;
    if (this.model !== model) this.opened = new Set();
    this.model = model;
    this.signature = signature;
    const focusTargets = new Map();
    const reveal = (label, key, ...children) => disclosure(label, key, children, this.opened, focusTargets);
    const source = (label, key, value) => reveal(label, key, element("pre", value));
    const view = element("section", null, "plan-view");
    const current = projection.current;
    if (!current) view.append(element("h2", "План ещё не принят"));
    else {
      const revision = current.revision == null ? "не записана" : current.revision;
      view.append(element("h2", `Принятый план · ревизия ${revision}`));
      view.append(element("p", `${current.tasks.length} задач · завершено ${current.completed}`, "plan-summary"));
      if (current.unknown) view.append(element("p", `Состояние ${current.unknown} задач неизвестно.`, "muted"));
      const tasks = element("ol", null, "plan-tasks");
      for (const task of current.tasks) {
        const row = element("li", null, "plan-task");
        const title = task.spec.title || task.key;
        if (task.id) {
          const button = element("button", title, "plan-task-select");
          button.type = "button";
          button.dataset.planFocus = `task:${task.id}`;
          focusTargets.set(button.dataset.planFocus, button);
          button.addEventListener("click", () => this.onSelectTask(task.id));
          row.append(button);
        } else row.append(element("h3", title));
        let status = task.state == null ? "Состояние не записано" : (STATES[task.state] ?? `Состояние: ${task.state}`);
        if (task.outcome != null) status += ` · ${OUTCOMES[task.outcome] ?? task.outcome}`;
        row.append(element("p", `${task.key} · ${status}`, "plan-task-state"));
        if (task.retryAt) row.append(element("p", `Повторная попытка после ${task.retryAt}`, "muted"));
        if (task.spec.goal) row.append(element("p", task.spec.goal));
        if (array(task.spec.depends_on).length) row.append(element("p", `Зависит от: ${task.spec.depends_on.join(", ")}`, "muted"));
        tasks.append(row);
      }
      view.append(reveal(`Задачи плана · ${current.tasks.length}`, "plan-tasks", tasks),
        source("Полная запись принятого плана: вопросы, потребности и критерии", "plan-source",
          JSON.stringify(current.snapshot, null, 2)));
    }
    const proposal = projection.proposals.at(-1);
    if (proposal) {
      const candidate = element("section", null, "plan-candidate");
      candidate.append(element("h2", "Предложенный план · не является принятым планом"));
      const status = {pending: "Ожидает решения человека", timed_out: "Время согласования истекло",
        study_closed: "Исследование закрыто без ответа на этот запрос", answered: `Решение записано: ${proposal.decision}`};
      candidate.append(element("p", `${status[proposal.status]} · ${proposal.plan.tasks.length} задач`));
      if (proposal.decision === "accept") candidate.append(element("p", "Согласование не заменяет событие принятия плана.", "muted"));
      candidate.append(source("Полная запись предложенного плана", "plan-candidate", proposal.source));
      view.append(candidate);
    }
    const history = element("section", null, "plan-history");
    if (!projection.history.length) history.append(element("p", "Изменения принятого плана ещё не записаны.", "muted"));
    const list = element("ol");
    for (const change of projection.history) {
      const row = element("li");
      row.append(element("h3", HISTORY[change.type]));
      const meta = [`Событие ${change.seq}`];
      if (change.revision != null) meta.push(`ревизия ${change.revision}`);
      if (change.ts) meta.push(change.ts);
      row.append(element("p", meta.join(" · "), "muted"));
      if (change.reason) row.append(element("p", `Причина: ${change.reason}`));
      if (change.detail) row.append(element("p", change.detail));
      if (change.added?.length) row.append(element("p", `Добавлены задачи: ${change.added.join(", ")}`));
      if (change.superseded?.length) row.append(element("p", `Заменены задачи: ${change.superseded.join(", ")}`));
      list.append(row);
    }
    history.append(list);
    const versions = projection.history.filter(change => change.type === "PlanAccepted" || change.type === "PlanExpanded").length;
    view.append(reveal(`История плана · ${versions} ревизий · ${projection.history.length} записей`, "plan-history", history));
    this.host.replaceChildren(view);
    if (focusKey) focusTargets.get(focusKey)?.focus({preventScroll: true});
  }
}
