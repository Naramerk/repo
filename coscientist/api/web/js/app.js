/** Study Console app: hash routing, views, wiring. ES module, no build step.
 *
 * Routes: #/runs (default) · #/study/<id>
 * Data: GET /studies, GET /studies/{id}, WS /studies/{id}/stream,
 *       GET /studies/{id}/artifacts/{artifact_id}. Everything live comes from
 *       the journal stream — the console never polls and never invents state.
 */

import { StudyStream } from "./stream.js";
import { StudyModel } from "./model.js";
import { GraphView, nodeLabel, nodeKindLabel, edgeKindLabel } from "./graph.js";
import { HitlPanel } from "./hitl.js";
import { ResultsView, jsonEvidence, decodeArtifactText } from "./results.js";
import { PresetForm, RunControls } from "./presets.js";
import { LocalProfilePanel } from "./profiles.js";
import { WorkflowView, taskTitle } from "./workflow.js";
import { PlanView } from "./plan.js";
import {parseRoute, studyRoute} from "./routes.js";
import {ObservationView} from "./observation.js";

const VERDICT_LABELS = {fulfilled: "Выполнено", partial: "Частично выполнено", failed: "Не выполнено", refused: "Остановлено с отказом"};

const state = {
  stream: null,
  hitl: null,
  form: null,
  runControls: null,
  model: null,
  graph: null,
  studyId: null,
  tab: "activity",
  selectedDoc: null,
  results: null,
  docPointer: "",
  workflow: null,
  workspaceResults: null,
  workspacePlan: null,
  selectedTaskId: null,
  plan: null,
  plannerPlan: null,
  observations: null,
};

const $ = (id) => document.getElementById(id);
let runsGeneration = 0;
const launchDraft = {};
const localProfiles = new LocalProfilePanel($("local-profiles"), () => {
  if (!location.hash.startsWith("#/study/")) route();
});

/* ---------------- routing ---------------- */

window.addEventListener("hashchange", route);

function route() {
  const target = parseRoute(location.hash);
  if (target.page === "study") {
    state.tab = target.tab;
    if (state.studyId === target.id && state.model) { show("study"); setTab(target.tab); }
    else openStudy(target.id);
  } else if (target.page === "new") openNew();
  else openRuns();
}

function leaveStudy() {
  runsGeneration += 1;
  localProfiles.setStudyId(null);
  state.stream?.stop(); state.hitl?.stop(); state.runControls?.stop();
  state.stream = null; state.model = null;
}

function openNew() {
  leaveStudy(); state.form?.stop();
  state.form = new PresetForm($("new-run"), result => {
    location.hash = studyRoute(result.active_study_id || result.study_id);
  }, localProfiles.current, launchDraft);
  state.form.load(); show("new");
}

/* ---------------- runs list ---------------- */

async function openRuns() {
  leaveStudy();
  const generation = runsGeneration;
  state.form?.stop(); show("runs");
  const list = $("runs-list");
  list.replaceChildren(loading());
  try {
    const res = await fetch(localProfiles.studiesURL());
    if (!res.ok) throw new Error(`GET /studies → ${res.status}`);
    const rows = await res.json();
    if (generation !== runsGeneration) return;
    list.replaceChildren(...rows.map(runCard));
    if (rows.length === 0) list.replaceChildren(muted(localProfiles.current
      ? "В этом локальном профиле пока нет исследований. Создайте новое или явно добавьте существующее."
      : "Исследований пока нет. Начните новое исследование выше после настройки параметров запуска."));
  } catch (err) {
    if (generation !== runsGeneration) return;
    list.replaceChildren(muted(`Не удалось получить список: ${err.message}`));
  }
}

function runCard(row) {
  const el = document.createElement("a");
  el.href = `#/study/${encodeURIComponent(row.study_id)}`;
  el.className = "run-card";
  const id = document.createElement("div");
  id.className = "run-id";
  id.textContent = row.study_id;
  const chip = document.createElement("span");
  chip.className = `chip ${row.verdict ?? ""}`;
  chip.textContent = VERDICT_LABELS[row.verdict] ?? row.verdict ?? "Открыто";
  chip.title = row.verdict ? `Статус выполнения: ${row.verdict}` : "Финальный статус ещё не записан";
  const request = document.createElement("p");
  request.textContent = row.request ?? "";
  el.append(id, request, chip);
  el.addEventListener("click", () => { location.hash = `#/study/${encodeURIComponent(row.study_id)}`; });
  return el;
}

/* ---------------- study view ---------------- */

function openStudy(studyId) {
  runsGeneration += 1;
  localProfiles.setStudyId(studyId);
  if (state.studyId !== studyId || !state.model) {
    state.stream?.stop();
    state.hitl?.stop();
    state.form?.stop();
    state.runControls?.stop();
    state.runControls = new RunControls($("run-controls"), studyId, activeId => {
      location.hash = `#/study/${encodeURIComponent(activeId)}`;
    });
    state.runControls.start();
    state.hitl = new HitlPanel($("hitl-requests"), studyId);
    state.hitl.refresh();
    state.model = new StudyModel();
    state.model.onUpdate = renderStudy;
    state.studyId = studyId;
    state.selectedDoc = null;
    state.docPointer = "";
    state.results = new ResultsView($("research-results"), openArtifact);
    state.plannerPlan = new PlanView($("planner-plan"), id => {
      const task = state.model.tasks.get(id); if (task) selectWorkspaceTask(task);
    }, {tasksOpen: true});
    state.observations = new Map([["tools", "tool-observation"], ["literature", "literature-observation"], ["code", "code-observation"]]
      .map(([kind, host]) => [kind, new ObservationView($(host), openArtifact)]));
    state.workspaceResults = new ResultsView($("workspace-results"), openArtifact);
    state.workspacePlan = new PlanView($("workspace-plan"), id => {
      const task = state.model.tasks.get(id);
      if (task) selectWorkspaceTask(task);
    }, {tasksOpen: true});
    state.selectedTaskId = null;
    $("workspace-title").textContent = "Выберите этап исследования";
    $("workspace-goal").textContent = "На схеме показаны задачи и зависимости из журнала. Выберите карточку, чтобы увидеть материалы и проверки.";
    $("workspace-results").replaceChildren();
    $("workspace-plan").replaceChildren();
    $("workspace-plan").hidden = true;
    $("details").replaceChildren();
    $("graph-kind").value = "";
    $("graph-details-body").textContent = "Выберите узел графа для просмотра связей и данных.";
    $("doc-title").textContent = "Просмотр";
    $("doc-view").closest(".study-docs").classList.remove("reading-wide");
    $("doc-view").textContent = "Выберите документ слева.";
    refreshArtifactInventory(studyId, state.model);
  }
  show("study");

  $("study-id").textContent = studyId;
  const verdictChip = $("study-verdict");
  verdictChip.hidden = false;
  verdictChip.className = "chip";
  verdictChip.textContent = "загрузка…";
  $("study-live").hidden = false;

  const model = state.model;
  const active = () => state.model === model && state.studyId === studyId;
  const pending = [];
  let frame = null;
  state.stream = new StudyStream(
    studyId,
    (batch) => {
      if (!active()) return;
      pending.push(...batch);
      if (frame !== null) return;
      frame = requestAnimationFrame(() => {
        frame = null;
        if (!active()) return;
        const events = pending.splice(0);
        model.apply(events);
        if (events.some(event => event.type.startsWith("Hitl") || event.type === "StudyFinalized")) state.hitl.refresh();
        if (state.tab === "graph") state.graph?.update();
      });
    },
    (connected) => {
      if (!active()) return;
      const el = $("conn-state");
      el.className = `conn ${connected ? "on" : "off"}`;
      el.title = connected ? "WS журнала подключён" : "переподключение…";
      $("study-live").hidden = !connected;
    },
  );
  state.stream.start();
  setTab(state.tab);
}

function renderStudy() {
  const m = state.model;
  const chip = $("study-verdict");
  if (m.verdict) {
    chip.className = `chip ${m.verdict}`;
    chip.textContent = VERDICT_LABELS[m.verdict] ?? m.verdict;
    chip.title = `Статус выполнения: ${m.verdict}`;
  } else {
    chip.className = "chip";
    chip.textContent = "Итог ещё не записан";
    chip.title = "Финальный статус ещё не записан; это не подтверждение активного исполнителя";
  }
  $("study-request").hidden = !m.request;
  $("study-request-text").textContent = m.request ?? "";
  renderDocs();
  renderEventsTable();
  if (state.tab === "activity") {
    if (!state.plan) state.plan = new PlanView($("plan-progress"), id => {
      const task = state.model.tasks.get(id);
      if (task) selectWorkspaceTask(task);
    });
    state.plan.render(m);
    if (!state.workflow) state.workflow = new WorkflowView($("workflow-host"), selectWorkspaceTask, $("workflow-meta"));
    state.workflow.setModel(m);
    if (state.selectedTaskId) {
      const task = m.tasks.get(state.selectedTaskId);
      if (task) renderWorkspaceTask(task);
    }
  }
  if (state.tab === "plan") state.plannerPlan?.render(m);
  state.observations?.get(state.tab)?.render(m, state.tab);
  if (state.tab === "results") state.results?.render(m);
  if (state.tab === "graph") renderGraphKinds(m);
}

function renderGraphKinds(model) {
  const select = $("graph-kind");
  const kinds = [...new Set([...model.graph.nodes.values()].map(node => node.kind))].sort();
  const signature = kinds.join(",");
  if (select.dataset.kinds === signature) return;
  const previous = select.value;
  const labels = {study: "Исследование", question: "Вопросы", criterion: "Критерии", hypothesis: "Гипотезы", experiment: "Эксперименты", evidence: "Доказательства", artifact: "Артефакты", conclusion: "Выводы"};
  const options = [["", "Все узлы"], ...kinds.map(kind => [kind, labels[kind] ?? kind])];
  select.replaceChildren(...options.map(([value, label]) => {
    const option = document.createElement("option"); option.value = value; option.textContent = label; return option;
  }));
  select.value = kinds.includes(previous) ? previous : "";
  select.dataset.kinds = signature;
}

$("graph-kind").addEventListener("change", event => state.graph?.setKind(event.target.value));
$("graph-in").addEventListener("click", () => state.graph?.zoomBy(1.25));
$("graph-out").addEventListener("click", () => state.graph?.zoomBy(0.8));

/* ---------------- tabs ---------------- */

for (const btn of document.querySelectorAll("#view-tabs button.tab")) {
  btn.addEventListener("click", () => { if (state.model) location.hash = studyRoute(state.studyId, btn.dataset.tab); });
}

function setTab(tab) {
  state.tab = tab;
  if (tab === "hitl") state.hitl?.refresh();
  for (const btn of document.querySelectorAll("#view-tabs button.tab")) {
    btn.classList.toggle("active", btn.dataset.tab === tab);
    if (btn.dataset.tab === tab) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  }
  for (const pane of document.querySelectorAll("#view-study > [data-tab]")) {
    pane.hidden = pane.dataset.tab !== tab;
  }
  if (tab === "graph") {
    if (!state.graph) {
      state.graph = new GraphView($("graph-host"), { onSelect: showNodeDetails });
      state.graph.setModel(state.model);
    } else {
      state.graph.setModel(state.model);
    }
  }
  renderStudy();
}

/* ---------------- tasks / feed ---------------- */

function selectWorkspaceTask(task) {
  state.selectedTaskId = task.id;
  if (state.tab !== "activity") { location.hash = studyRoute(state.studyId, "activity"); return; }
  renderWorkspaceTask(task);
}

function renderWorkspaceTask(task) {
  state.workflow.select(task.id);
  $("workspace-title").textContent = taskTitle(task);
  const isPlan = task.key === "PLAN";
  $("workspace-goal").textContent = isPlan
    ? "Принятый план и его изменения из журнала исследования."
    : task.spec?.goal || "Цель в журнале не указана.";
  $("workspace-plan").hidden = !isPlan;
  $("workspace-results").hidden = isPlan;
  if (isPlan) state.workspacePlan.render(state.model);
  else state.workspaceResults.render(state.model, task.id);
}

$("workflow-fit").addEventListener("click", () => {
  if (state.workflow) { state.workflow.autoFit = true; state.workflow.fit(); }
});
$("workflow-in").addEventListener("click", () => state.workflow?.zoomBy(1.25));
$("workflow-out").addEventListener("click", () => state.workflow?.zoomBy(0.8));

function renderEventsTable() {
  const m = state.model;
  const tbody = $("events-tbody");
  tbody.replaceChildren(...m.events.slice(-500).reverse().map((ev) => {
    const tr = document.createElement("tr");
    const seq = td(String(ev.seq));
    const type = td(ev.type);
    const entity = td(shortEntity(ev));
    entity.className = "dim";
    const ts = td(String(ev.ts ?? "").replace("T", " ").slice(0, 19));
    ts.className = "dim";
    tr.append(seq, type, entity, ts);
    tr.tabIndex = 0;
    tr.addEventListener("click", () => showEventDetails(ev));
    tr.addEventListener("keydown", event => { if (event.key === "Enter") showEventDetails(ev); });
    return tr;
  }));
}

/* ---------------- documents ---------------- */

function renderDocs() {
  const m = state.model;
  const ul = $("doc-list");
  ul.replaceChildren(...m.docRows().map((a) => {
    const li = document.createElement("li");
    li.textContent = `${a.name || a.id} · ${a.media_type || "байты"}`;
    if (a.registered_in_journal === false) {
      const note = document.createElement("small");
      note.className = "muted";
      note.textContent = "Файл без записи в журнале";
      li.append(note);
    }
    li.tabIndex = 0;
    li.setAttribute("role", "button");
    li.addEventListener("keydown", (event) => { if (event.key === "Enter") li.click(); });
    if (state.selectedDoc === a.id) li.classList.add("active");
    li.addEventListener("click", () => { state.selectedDoc = a.id; renderDocs(); openDoc(a); });
    return li;
  }));
}

function openArtifact(id, pointer = "") {
  const artifact = state.model.artifacts.get(id);
  state.selectedDoc = id;
  state.docPointer = pointer;
  setTab("docs");
  if (artifact) openDoc(artifact, pointer);
  else $("doc-view").textContent = `В журнале нет доступного описания артефакта ${id}. Ссылка сохранена в полной записи результата.`;
}

async function openDoc(a, pointer = "") {
  const sid = state.studyId;
  state.docPointer = pointer;
  const current = () => state.studyId === sid && state.selectedDoc === a.id && state.docPointer === pointer;
  const url = `/studies/${encodeURIComponent(sid)}/artifacts/${encodeURIComponent(a.id)}`;
  $("doc-title").textContent = a.name || a.id;
  const view = $("doc-view");
  view.closest(".study-docs").classList.remove("reading-wide");
  view.replaceChildren(document.createTextNode("Загрузка…"));
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
    const blob = await res.blob();
    if (!current()) return;
    const download = document.createElement("a");
    const objectUrl = URL.createObjectURL(blob);
    download.href = objectUrl;
    download.download = a.name || a.id;
    download.textContent = "Скачать оригинал";
    const width = document.createElement("button");
    width.type = "button"; width.textContent = "Читать на всю ширину";
    width.setAttribute("aria-pressed", "false");
    width.addEventListener("click", () => {
      const expanded = view.closest(".study-docs").classList.toggle("reading-wide");
      width.setAttribute("aria-pressed", String(expanded));
      width.textContent = expanded ? "Показать список документов" : "Читать на всю ширину";
    });
    const meta = document.createElement("p");
    meta.className = "muted";
    const registration = a.registered_in_journal === false ? "метаданные CAS; в журнале не зарегистрирован" : "описание в журнале";
    meta.textContent = `${a.media_type} · ${blob.size} байт · SHA-256 ${a.sha256} · ${a.provenance || a.producer || "источник не указан"} · ${registration}`;
    const tools = document.createElement("div"); tools.className="document-tools"; tools.append(download,width);
    view.replaceChildren(tools, meta);
    if (a.media_type?.startsWith("image/")) {
      const image = document.createElement("img");
      image.src = objectUrl; image.alt = a.name || a.id;
      view.append(image);
    } else if (a.media_type?.startsWith("text/") || a.media_type?.includes("json")) {
      const decoded = decodeArtifactText(await blob.arrayBuffer());
      const text = decoded.text;
      if (!current()) { URL.revokeObjectURL(objectUrl); return; }
      if (!decoded.validUtf8) {
        const notice = document.createElement("p");
        notice.textContent = "Некорректный UTF-8: предпросмотр содержит символы замены. Доказательство не выбирается; оригинальные байты доступны для скачивания.";
        view.append(notice);
      }
      if (pointer && a.media_type.includes("json")) {
        const selected = decoded.validUtf8 ? jsonEvidence(text, pointer) : {found: false, reason: "invalid_utf8"};
        const selection = document.createElement("section");
        selection.className = "evidence-selection";
        const title = document.createElement("h3");
        title.textContent = `Доказательство: ${pointer}`;
        const value = document.createElement("pre");
        value.textContent = selected.found ? selected.source : selected.reason === "pointer_missing" ? "Указатель отсутствует в исходном артефакте." : "JSON некорректен, неоднозначен или превышает безопасный бюджет разбора. Ниже сохранён исходный текст.";
        selection.append(title, value); view.append(selection);
      }
      const content = document.createElement("div");
      content.className = "document-content";
      if (a.media_type.includes("markdown")) renderMarkdown(content, text);
      else { const pre = document.createElement("pre"); pre.textContent = text; content.append(pre); }
      view.append(content);
      const raw = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = `Полный исходный текст · ${text.length.toLocaleString("ru-RU")} символов · ${blob.size.toLocaleString("ru-RU")} байт`;
      const source = document.createElement("pre"); source.textContent=text;
      raw.append(summary,source); view.append(raw);
    }
    // Revoke previous preview URLs when a different document replaces this view.
    const previous = view.dataset.objectUrl;
    if (previous) URL.revokeObjectURL(previous);
    view.dataset.objectUrl = objectUrl;
  } catch (err) {
    if (current()) view.textContent = `Не удалось прочитать: ${err.message}`;
  }
}

async function refreshArtifactInventory(studyId, model) {
  const active = () => state.studyId === studyId && state.model === model;
  const status = $("document-inventory-state");
  status.textContent = "Чтение метаданных CAS…";
  try {
    const response = await fetch(`/console/studies/${encodeURIComponent(studyId)}/artifacts`);
    if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
    const rows = await response.json();
    if (!active()) return;
    model.mergeArtifactSnapshot(rows);
    status.textContent = `${rows.length} артефактов. Документы после завершения доступны по обновлению.`;
  } catch (error) {
    if (active()) status.textContent = `Не удалось прочитать метаданные: ${error.message}`;
  }
}

$("refresh-documents").addEventListener("click", () => refreshArtifactInventory(state.studyId, state.model));

function renderMarkdown(host, text) {
  // Render a small document subset with textContent: embedded HTML/scripts never execute.
  let code = null;
  for (const line of text.split("\n")) {
    if (line.startsWith("```")) {
      if (code) code = null;
      else { code = document.createElement("pre"); host.append(code); }
    } else if (code) code.textContent += line + "\n";
    else {
      const heading = line.match(/^(#{1,6})\s+(.*)$/);
      const element = document.createElement(heading ? `h${heading[1].length}` : "p");
      element.textContent = heading ? heading[2] : line;
      host.append(element);
    }
  }
}

/* ---------------- details ---------------- */

function showNodeDetails({ node }) {
  state.graph?.select(node.node_id);
  const d = $("details");
  const kv = kvList([
    ["node_id", node.node_id],
    ["kind", node.kind],
    ["ref", node.ref_id ?? "—"],
  ]);
  d.replaceChildren(kv);
  const related = [...state.model.graph.edges.values()].filter(e => e.src_id === node.node_id || e.dst_id === node.node_id);
  const panel = $("graph-details-body");
  const title = document.createElement("h3"); title.textContent = nodeKindLabel(node.kind);
  const text = document.createElement("p"); text.textContent = nodeLabel(node);
  const technical = document.createElement("details"), summary = document.createElement("summary"), raw = document.createElement("pre");
  summary.textContent = "Полная запись узла";
  raw.textContent = JSON.stringify(node, null, 2); technical.append(summary,raw);
  panel.replaceChildren(title,text,technical);
  const links = document.createElement("div"); links.className="node-relations";
  for (const edge of related) {
    const outgoing = edge.src_id === node.node_id;
    const other = state.model.graph.nodes.get(outgoing ? edge.dst_id : edge.src_id);
    const relation = document.createElement("button");
    relation.textContent = `${outgoing ? "→" : "←"} ${edgeKindLabel(edge.kind)} · ${other ? nodeLabel(other) : "узел отсутствует в журнале"}`;
    relation.disabled = !other;
    relation.addEventListener("click", () => { if (other) showNodeDetails({node:other}); });
    links.append(relation);
  }
  panel.append(links);
  const artifactId = node.attrs?.ref?.artifact_id ?? node.attrs?.artifact_id;
  if (artifactId) {
    const source = document.createElement("button");
    source.textContent = "Открыть источник";
    source.addEventListener("click", () => openArtifact(artifactId, node.attrs?.ref?.json_pointer ?? ""));
    panel.append(source);
  }
  const stmt = node.attrs?.statement;
  if (typeof stmt === "string" && stmt) {
    const pre = document.createElement("pre");
    pre.textContent = stmt;
    d.append(pre);
  }
}

function showEventDetails(ev) {
  const d = $("details");
  d.replaceChildren(
    kvList([
      ["seq", ev.seq],
      ["тип", ev.type],
      ["сущность", ev.entity_key],
      ["время", ev.ts],
      ["epoch", ev.epoch],
    ]),
  );
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(ev.payload, null, 2);
  d.append(pre);
}

/* ---------------- helpers ---------------- */

function show(name) {
  for (const page of ["runs", "new", "study"]) $("view-" + page).hidden = name !== page;
  for (const button of document.querySelectorAll("#view-tabs button.tab")) {
    button.disabled = name !== "study";
    button.title = name === "study" ? "" : "Откройте исследование из списка";
  }
  for (const link of document.querySelectorAll("#view-tabs a")) {
    if (link.getAttribute("href") === location.hash) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
}

function kvList(pairs) {
  const dl = document.createElement("dl");
  dl.className = "kv";
  for (const [k, v] of pairs) {
    const dt = document.createElement("dt");
    dt.textContent = k;
    const dd = document.createElement("dd");
    dd.textContent = String(v);
    dl.append(dt, dd);
  }
  return dl;
}

function td(text) {
  const el = document.createElement("td");
  el.textContent = text;
  return el;
}

function loading() {
  const el = document.createElement("p");
  el.className = "muted";
  el.textContent = "загрузка…";
  return el;
}

function muted(text) {
  const el = document.createElement("p");
  el.className = "muted";
  el.textContent = text;
  return el;
}

function shortEntity(ev) {
  const e = ev.entity_key ?? "";
  return e.startsWith(`${ev.study_id}:`) ? e.slice(ev.study_id.length + 1) : e;
}

$("graph-fit").addEventListener("click", () => {
  if (state.graph) { state.graph.autoFit = true; state.graph.fit(); }
});

/* ---------------- resizable / collapsible panels ---------------- */
/* Panes live in CSS grid/flex layouts, where `resize:` does not propagate to
   the track, so each panel gets a small edge handle that moves the boundary;
   sizes and collapsed state persist in localStorage. Narrow layouts (<=850px,
   where the media query stacks the panes) ignore inline overrides entirely. */

const PANEL_PREFS_KEY = "coscientist-console-panels";
const panelPrefs = (() => {
  try { return JSON.parse(localStorage.getItem(PANEL_PREFS_KEY)) ?? {}; } catch { return {}; }
})();
if (!Array.isArray(panelPrefs.collapsed)) panelPrefs.collapsed = [];
const persistPanels = () => { try { localStorage.setItem(PANEL_PREFS_KEY, JSON.stringify(panelPrefs)); } catch { /* storage unavailable → layout stays session-only */ } };
const wideLayout = matchMedia("(min-width: 851px)");

const workspace = document.querySelector(".research-workspace");
const inspector = document.querySelector(".workspace-inspector");
const docsPane = document.querySelector(".study-docs");
const docsList = document.querySelector(".panel-docs");
const docView = document.querySelector(".panel-docview");
const graphPane = document.querySelector(".study-graph");
const graphDetails = $("graph-details");
const resultsPanel = document.querySelector(".study-results");

function applyPanelLayout() {
  workspace.style.gridTemplateColumns = "";
  docsPane.style.gridTemplateColumns = "";
  graphPane.style.gridTemplateRows = "";
  resultsPanel.style.flex = "";
  resultsPanel.style.height = "";
  if (!wideLayout.matches) return;
  if (inspector.classList.contains("collapsed")) workspace.style.gridTemplateColumns = "minmax(0,1fr) 36px";
  else if (panelPrefs.workspaceSplit) workspace.style.gridTemplateColumns = `${panelPrefs.workspaceSplit}px minmax(350px,1fr)`;
  if (docsList.classList.contains("collapsed")) docsPane.style.gridTemplateColumns = "36px minmax(0,1fr)";
  else if (docView.classList.contains("collapsed")) docsPane.style.gridTemplateColumns = "minmax(0,1fr) 36px";
  else if (panelPrefs.docsSplit) docsPane.style.gridTemplateColumns = `${panelPrefs.docsSplit}px minmax(0,1fr)`;
  if (!graphDetails.classList.contains("collapsed") && panelPrefs.detailsHeight) graphPane.style.gridTemplateRows = `auto auto minmax(380px,1fr) ${panelPrefs.detailsHeight}px`;
  if (!resultsPanel.classList.contains("collapsed") && panelPrefs.resultsHeight) {
    resultsPanel.style.flex = "0 0 auto";
    resultsPanel.style.height = `${panelPrefs.resultsHeight}px`;
  }
}

/** Drag strip on a panel edge; the handler moves the owning grid/flex boundary. */
function edgeHandle(panel, edge, onDrag) {
  const grip = document.createElement("div");
  grip.className = `edge-handle ${edge}`;
  grip.setAttribute("aria-hidden", "true");
  panel.append(grip);
  grip.addEventListener("pointerdown", (event) => {
    event.preventDefault();
    try { grip.setPointerCapture(event.pointerId); } catch { /* synthetic events carry no active pointer */ }
    onDrag(event);
    const move = (e) => onDrag(e);
    const stop = () => {
      grip.removeEventListener("pointermove", move);
      grip.removeEventListener("pointerup", stop);
      grip.removeEventListener("pointercancel", stop);
      persistPanels();
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", stop);
    grip.addEventListener("pointercancel", stop);
  });
}

/** Chevron toggle: a collapsed panel keeps only its heading visible. */
function collapseToggle(panel, key) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "panel-collapse";
  const apply = (collapsed) => {
    panel.classList.toggle("collapsed", collapsed);
    button.setAttribute("aria-expanded", String(!collapsed));
    button.setAttribute("aria-label", collapsed ? "Развернуть панель" : "Свернуть панель");
    button.title = collapsed ? "Развернуть" : "Свернуть";
  };
  apply(panelPrefs.collapsed.includes(key));
  button.addEventListener("click", () => {
    const collapsed = !panel.classList.contains("collapsed");
    if (collapsed) panelPrefs.collapsed.push(key);
    else panelPrefs.collapsed.splice(panelPrefs.collapsed.indexOf(key), 1);
    persistPanels();
    apply(collapsed);
    applyPanelLayout();
  });
  panel.append(button);
}

if (workspace && inspector) {
  edgeHandle(inspector, "left", (event) => {
    if (!wideLayout.matches || inspector.classList.contains("collapsed")) return;
    const rect = workspace.getBoundingClientRect();
    const px = Math.round(Math.min(Math.max(event.clientX - rect.left, 320), Math.max(rect.width - 400, 320)));
    panelPrefs.workspaceSplit = px;
    workspace.style.gridTemplateColumns = `${px}px minmax(350px,1fr)`;
  });
  collapseToggle(inspector, "inspector");
}
if (docsPane && docsList && docView) {
  edgeHandle(docsList, "right", (event) => {
    if (!wideLayout.matches || docsList.classList.contains("collapsed")) return;
    const rect = docsPane.getBoundingClientRect();
    const px = Math.round(Math.min(Math.max(event.clientX - rect.left, 170), Math.max(rect.width - 340, 170)));
    panelPrefs.docsSplit = px;
    docsPane.style.gridTemplateColumns = `${px}px minmax(0,1fr)`;
  });
  collapseToggle(docsList, "docs");
  collapseToggle(docView, "docview");
}
if (graphPane && graphDetails) {
  edgeHandle(graphDetails, "top", (event) => {
    if (!wideLayout.matches) return;
    const rect = graphPane.getBoundingClientRect();
    const px = Math.round(Math.min(Math.max(rect.bottom - event.clientY, 46), Math.max(rect.height * 0.7, 46)));
    panelPrefs.detailsHeight = px;
    graphPane.style.gridTemplateRows = `auto auto minmax(380px,1fr) ${px}px`;
  });
  collapseToggle(graphDetails, "graph-details");
}
if (resultsPanel) {
  edgeHandle(resultsPanel, "bottom", (event) => {
    const px = Math.round(Math.min(Math.max(event.clientY - resultsPanel.getBoundingClientRect().top, 200), Math.max(window.innerHeight - 140, 200)));
    panelPrefs.resultsHeight = px;
    resultsPanel.style.flex = "0 0 auto";
    resultsPanel.style.height = `${px}px`;
  });
  collapseToggle(resultsPanel, "results");
}
wideLayout.addEventListener("change", applyPanelLayout);
applyPanelLayout();
for (const link of document.querySelectorAll(".all-studies, .back-link")) {
  link.addEventListener("click", event => {
    event.preventDefault(); localProfiles.select(null); location.hash = "#/runs";
  });
}
localProfiles.load();
route();
