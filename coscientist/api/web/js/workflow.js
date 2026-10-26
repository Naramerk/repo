/** Read-only task dependency view. It is not the scientific provenance graph. */
import {GraphView} from "./graph.js";

const STAGES = {SPEC: "Постановка задачи", PLAN: "План исследования", HYP: "Гипотезы", LIT: "Анализ литературы", REPORT: "Этап отчётности"};
const STATES = {SUCCESS: "Готово", FAILED: "Не выполнено", SKIPPED: "Пропущено", PENDING: "В очереди", RUNNING: "В работе", STUBBED: "Заглушка"};
export const taskTitle = task => STAGES[task.key] ?? (task.title || task.key);

export function taskWorkflow(tasks) {
  const nodes = new Map(tasks.map(task => [task.id, {node_id: task.id, kind: task.kind, task}]));
  const keys = new Map();
  for (const task of tasks) keys.set(task.key, [...(keys.get(task.key) ?? []), task.id]);
  const edges = new Map(), missing = [];
  for (const task of tasks) {
    for (const dependency of task.spec?.depends_on ?? []) {
      const candidates = keys.get(dependency);
      const parent = nodes.has(dependency) ? dependency : candidates?.length === 1 ? candidates[0] : null;
      if (!parent) { missing.push({task_id: task.id, dependency}); continue; }
      const id = JSON.stringify([parent, task.id]);
      edges.set(id, {src_id: parent, dst_id: task.id, kind: "depends_on"});
    }
  }
  const indegree = new Map(tasks.map(task => [task.id, 0]));
  const children = new Map(tasks.map(task => [task.id, []]));
  for (const edge of edges.values()) {
    indegree.set(edge.dst_id, indegree.get(edge.dst_id) + 1);
    children.get(edge.src_id).push(edge.dst_id);
  }
  const levels = new Map(), queue = tasks.filter(task => indegree.get(task.id) === 0).map(task => task.id);
  for (const id of queue) levels.set(id, 0);
  for (let cursor = 0; cursor < queue.length; cursor++) {
    const id = queue[cursor];
    for (const child of children.get(id)) {
      levels.set(child, Math.max(levels.get(child) ?? 0, levels.get(id) + 1));
      indegree.set(child, indegree.get(child) - 1);
      if (indegree.get(child) === 0) queue.push(child);
    }
  }
  // A damaged/cyclic journal is still inspectable, with an explicit warning.
  const unresolved = tasks.filter(task => indegree.get(task.id) > 0).map(task => task.id);
  let last = 1;
  for (const level of levels.values()) last = Math.max(last, level + 1);
  for (const id of unresolved) levels.set(id, last);
  const rows = new Map();
  for (const task of tasks) {
    const level = levels.get(task.id);
    rows.set(level, [...(rows.get(level) ?? []), task.id]);
  }
  const positions = new Map();
  for (const [level, ids] of rows) ids.forEach((id, index) => {
    positions.set(id, {x: (index - (ids.length - 1) / 2) * 266, y: level * 164});
  });
  return {nodes, edges, positions, missing, unresolved};
}

function svg(tag, attrs, text) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, String(value));
  if (text !== undefined) el.textContent = text;
  return el;
}

function lines(text) {
  const result = [];
  for (const word of text.split(/\s+/u)) {
    const last = result.length - 1;
    if (last >= 0 && result[last].length + word.length < 27) result[last] += ` ${word}`;
    else result.push(word);
  }
  return result.slice(0, 3).map((line, index) => index === 2 && result.length > 3 ? `${line.slice(0, 24)}…` : line.slice(0, 28));
}

export class WorkflowView extends GraphView {
  constructor(host, onSelect, meta) {
    super(host, {onSelect, idPrefix: "workflow"});
    this.meta = meta;
    this.selectedId = null;
  }

  _place() {
    this.visible = taskWorkflow(this.model.taskRows());
    this.pos = this.visible.positions;
  }

  select(id) {
    this.selectedId = id;
    for (const node of this.gNodes.children) {
      const selected = node.dataset.id === id;
      node.classList.toggle("selected", selected);
      node.setAttribute("aria-pressed", String(selected));
    }
  }

  _syncDom() {
    const focused = this.svg.contains(document.activeElement) ? document.activeElement.dataset.id : null;
    super._syncDom();
    if (focused) [...this.gNodes.children].find(node => node.dataset.id === focused)?.focus();
  }

  _renderNode(id, node, position) {
    const task = node.task, title = taskTitle(task), outcome = task.outcome ?? task.state;
    const selected = id === this.selectedId;
    const group = svg("g", {class: `gnode workflow-node ${selected ? "selected" : ""}`, "data-id": id,
      "data-state": outcome, transform: `translate(${position.x},${position.y})`,
      tabindex: 0, role: "button", "aria-label": `${title}: ${STATES[outcome] ?? outcome}`, "aria-pressed": selected});
    group.append(svg("rect", {x: -114, y: -56, width: 228, height: 112, rx: 10}));
    group.append(svg("text", {x: -98, y: -34, class: "workflow-kind"}, STAGES[task.key] ? "ЭТАП ИССЛЕДОВАНИЯ" : "ЗАДАЧА"));
    for (const [index, line] of lines(title).entries()) group.append(svg("text", {x: -98, y: -12 + index * 17, class: "workflow-title"}, line));
    group.append(svg("text", {x: -98, y: 42, class: "workflow-status"}, STATES[outcome] ?? outcome));
    group.append(svg("title", {}, `${title}\n${task.id}`));
    const choose = () => this.onSelect?.(task);
    group.addEventListener("click", choose);
    group.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); choose(); }
    });
    return group;
  }

  _renderEdge(id, edge, a, b) {
    const mid = (a.y + b.y) / 2;
    const path = svg("path", {class: "gedge workflow-edge", "data-id": id,
      "marker-end": "url(#workflow-arrow)", d: `M ${a.x} ${a.y + 56} C ${a.x} ${mid}, ${b.x} ${mid}, ${b.x} ${b.y - 56}`});
    path.append(svg("title", {}, `Зависимость: ${edge.src_id} → ${edge.dst_id}`));
    return path;
  }

  _meta() {
    if (!this.meta) return;
    const graph = this.visible;
    const warning = graph.missing.length || graph.unresolved.length;
    this.meta.textContent = `${graph.nodes.size} задач · ${graph.edges.size} зависимостей${warning ? ` · есть неразрешённые зависимости (${graph.missing.length}) или циклы/зависимые от них задачи (${graph.unresolved.length})` : ""}`;
  }
}
