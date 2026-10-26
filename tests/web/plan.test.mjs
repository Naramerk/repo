import {test} from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
const load = async name => {
  const source = readFileSync(new URL(`../../coscientist/api/web/js/${name}.js`, import.meta.url), "utf8");
  return import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
};
const {StudyModel} = await load("model");
const {projectPlan, PlanView} = await load("plan");
const ev = (seq, type, payload) => ({seq, type, payload, study_id: "s", ts: `time-${seq}`});
const spec = key => ({task_key: key, title: `Task ${key}`, goal: "Measured research", route: "experiment",
  depends_on: [], addresses: ["Q1"], tools_hint: [], expected_outputs: [], criteria: []});
const need = {kind: "need", needed: false, reason: "not needed"};
const plan = (...keys) => ({tasks: keys.map(spec), questions: [{id: "Q1", text: "Question", final_type: "artifact"}],
  hypotheses: need, literature: need});
const accepted = (revision, ...keys) => ({...plan(...keys), plan_id: `p${revision}`, revision});
const created = (key, state = "PENDING") => ({task_id: `s/${key}`, state, spec: spec(key)});
const proposal = (id, snapshot = plan("candidate")) => ({request_id: id, kind: "plan_approval", prompt: {
  question: "Approve?", options: ["accept", "reject"], attempt_id: "s/PLAN#1",
  context_json: JSON.stringify(snapshot), context_sha256: "a".repeat(64),
}});

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.ownText = ""; this.dataset = {}; }
  set textContent(value) { this.ownText = String(value); }
  get textContent() { return this.ownText + this.children.map(node => node.textContent).join(" "); }
  set innerHTML(_) { throw new Error("HTML injection forbidden"); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  focus() { document.activeElement = this; }
}
globalThis.document = {createElement: tag => new Element(tag)};
const elements = node => [node, ...node.children.flatMap(elements)];

test("PLAN stage is not acceptance; approval also cannot create an accepted plan", () => {
  const model = new StudyModel();
  model.apply([ev(1, "TaskCreated", created("PLAN")), ev(2, "HitlOpened", proposal("h")),
    ev(3, "HitlDecided", {request_id: "h", decision: "accept"})]);
  const projection = projectPlan(model);
  assert.equal(projection.current, null);
  assert.equal(projection.proposals[0].status, "answered");
  assert.equal(projection.proposals[0].decision, "accept");
  const host = new Element("main");
  new PlanView(host, () => assert.fail("no task selected")).render(model);
  assert.match(host.textContent, /План ещё не принят/);
  assert.match(host.textContent, /Согласование не заменяет событие принятия/);
});

test("expanded full snapshot replaces prior membership, with exact revision and retained history", () => {
  const model = new StudyModel();
  model.apply([ev(1, "PlanAccepted", accepted(4, "old", "keep")),
    ev(2, "PlanExpanded", {...accepted(7, "keep", "new"), plan_rev: 7, reason: "new measurement",
      added_tasks: [spec("new")], superseded_tasks: ["old"]}),
    ev(3, "ExpansionRefused", {reason: "budget", detail: "No expansion left"})]);
  const projection = projectPlan(model);
  assert.equal(projection.current.revision, 7);
  assert.deepEqual(projection.current.tasks.map(task => task.key), ["keep", "new"]);
  assert.equal(projection.current.unknown, 2);
  assert.deepEqual(projection.history[1].added, ["new"]);
  assert.deepEqual(projection.history[1].superseded, ["old"]);
  assert.equal(projection.history[2].detail, "No expansion left");
  model.apply([ev(4, "PlanAccepted", accepted(12, "replacement"))]);
  assert.deepEqual(projectPlan(model).current.tasks.map(task => task.key), ["replacement"]);
});

test("only observed lifecycle gives progress, failed completion is not scientific success", () => {
  const model = new StudyModel();
  model.apply([ev(1, "PlanAccepted", accepted(1, "failed", "active", "missing", "unrecorded")),
    ev(2, "TaskCreated", created("failed")), ev(3, "TaskCreated", created("active")),
    ev(4, "TaskCreated", {...created("unrecorded"), state: undefined}),
    ev(5, "AttemptStarted", {task_id: "s/active", attempt_id: "s/active#1"}),
    ev(6, "ResultRecorded", {task_id: "s/active", result_id: "r", computed_outcome: "SUCCESS"}),
    ev(7, "TaskDone", {task_id: "s/failed", outcome: "FAILED"})]);
  const current = projectPlan(model).current;
  assert.equal(current.completed, 1);
  assert.equal(current.unknown, 2);
  assert.equal(current.tasks[0].outcome, "FAILED");
  assert.equal(current.tasks[1].state, "RUNNING");
  assert.equal(current.tasks[2].id, null);
  assert.equal(current.tasks[3].state, null, "model's default PENDING is not a recorded state");
});

test("candidate context must be PlanOut shaped; rejection and timeout remain proposal history", () => {
  const model = new StudyModel();
  model.apply([ev(1, "HitlOpened", proposal("bad", {tasks: [spec("x")]})),
    ev(2, "HitlOpened", {...proposal("wrong-kind"), kind: "custom_check"}),
    ev(3, "HitlOpened", {...proposal("json"), prompt: {context_json: "{bad"}}),
    ev(4, "HitlOpened", proposal("rejected")), ev(5, "HitlDecided", {request_id: "rejected", decision: "reject"}),
    ev(6, "HitlOpened", proposal("timeout")), ev(7, "HitlTimedOut", {request_id: "timeout"}),
    ev(8, "PlanRefused", {reason: "human_rejected"})]);
  const projection = projectPlan(model);
  assert.equal(projection.current, null);
  assert.deepEqual(projection.proposals.map(row => [row.requestId, row.status, row.decision]),
    [["rejected", "answered", "reject"], ["timeout", "timed_out", null]]);
  model.apply([ev(9, "HitlOpened", proposal("closed")), ev(10, "StudyFinalized", {verdict: "failed"})]);
  assert.equal(projectPlan(model).proposals.at(-1).status, "study_closed");
});

test("read-only semantic DOM selects actual task, safely retains full source and clears previous study", () => {
  const model = new StudyModel();
  const snapshot = accepted(3, "known", "absent");
  snapshot.tasks[0].title = "<img src=x onerror=alert(1)>";
  snapshot.tasks[0].criteria = [{template_id: "real-check"}];
  model.apply([ev(1, "PlanAccepted", snapshot), ev(2, "TaskCreated", created("known", "UNRECOGNIZED"))]);
  const host = new Element("main"), selected = [];
  const view = new PlanView(host, id => selected.push(id));
  view.render(model);
  const buttons = elements(host).filter(node => node.tag === "button");
  assert.equal(buttons.length, 1);
  assert.equal(buttons[0].type, "button");
  buttons[0].listeners.click();
  assert.deepEqual(selected, ["s/known"]);
  assert.match(host.textContent, /<img src=x onerror=alert\(1\)>/);
  assert.match(host.textContent, /UNRECOGNIZED/);
  assert.match(host.textContent, /real-check/);
  assert.match(host.textContent, /Question/);
  assert.equal(elements(host).some(node => node.tag === "progress"), false);
  view.render(new StudyModel());
  assert.match(host.textContent, /План ещё не принят/);
  assert.doesNotMatch(host.textContent, /real-check/);
});

test("reconnect replay and projection never mutate journal or task state", () => {
  const model = new StudyModel();
  const batch = [ev(1, "PlanAccepted", accepted(1, "x")), ev(2, "TaskCreated", created("x")),
    ev(3, "TaskDone", {task_id: "s/x", outcome: "SKIPPED"})];
  model.apply(batch);
  model.apply(batch);
  const before = JSON.stringify([model.events, [...model.tasks]]);
  assert.equal(projectPlan(model).history.length, 1);
  assert.equal(projectPlan(model).current.completed, 1);
  assert.equal(JSON.stringify([model.events, [...model.tasks]]), before);
});

test("recorded retry closes the attempt into waiting, without reviving a terminal task", () => {
  const model = new StudyModel();
  model.apply([ev(1, "PlanAccepted", accepted(1, "retry")), ev(2, "TaskCreated", created("retry")),
    ev(3, "AttemptStarted", {task_id: "s/retry", attempt_id: "a"}),
    ev(4, "AttemptClosed", {attempt_id: "a", retry_at: "tomorrow"})]);
  let task = projectPlan(model).current.tasks[0];
  assert.equal(task.state, "PENDING");
  assert.equal(task.retryAt, "tomorrow");
  model.apply([ev(5, "TaskDone", {task_id: "s/retry", outcome: "FAILED"}),
    ev(6, "AttemptClosed", {attempt_id: "a", retry_at: "later"})]);
  task = projectPlan(model).current.tasks[0];
  assert.equal(task.state, "DONE");
  assert.equal(task.retryAt, null);
});

test("compact disclosures and keyboard focus survive meaningful updates; unrelated stream events leave DOM intact", () => {
  const model = new StudyModel();
  model.apply([ev(1, "PlanAccepted", accepted(1, "x")), ev(2, "TaskCreated", created("x"))]);
  const host = new Element("main"), view = new PlanView(host, () => {});
  view.render(model);
  assert.ok(elements(host).filter(node => node.tag === "details").every(node => !node.open));
  const taskDetails = elements(host).find(node => node.tag === "details" && node.children[0].dataset.planFocus === "plan-tasks");
  taskDetails.open = true; taskDetails.listeners.toggle();
  const button = elements(host).find(node => node.tag === "button");
  button.focus();
  const original = host.children[0];
  model.apply([ev(3, "ToolCalled", {name: "unrelated"})]);
  view.render(model);
  assert.equal(host.children[0], original);
  assert.equal(document.activeElement, button);
  model.apply([ev(4, "TaskDone", {task_id: "s/x", outcome: "SUCCESS"})]);
  view.render(model);
  assert.notEqual(host.children[0], original);
  assert.equal(document.activeElement.dataset.planFocus, "task:s/x");
  assert.ok(elements(host).find(node => node.tag === "details" && node.children[0].dataset.planFocus === "plan-tasks").open);
  view.render(new StudyModel());
  assert.ok(elements(host).filter(node => node.tag === "details").every(node => !node.open));
});
