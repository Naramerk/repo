/** Study Console app: hash routing, views, wiring. ES module, no build step.
 *
 * Routes: #/runs (default) · #/study/<id>
 * Data: GET /studies, GET /studies/{id}, WS /studies/{id}/stream,
 *       GET /studies/{id}/artifacts/{artifact_id}. Everything live comes from
 *       the journal stream — the console never polls and never invents state.
 */

import { StudyStream } from "./stream.js";
import { StudyModel } from "./model.js";
import { GraphView } from "./graph.js";
import { HitlPanel } from "./hitl.js";

const state = {
  stream: null,
  hitl: null,
  model: null,
  graph: null,
  studyId: null,
  tab: "activity",
  selectedDoc: null,
};

const $ = (id) => document.getElementById(id);

/* ---------------- routing ---------------- */

window.addEventListener("hashchange", route);

function route() {
  const hash = location.hash || "#/runs";
  const study = hash.match(/^#\/study\/(.+)$/);
  if (study) {
    openStudy(decodeURIComponent(study[1]));
  } else {
    openRuns();
  }
}

/* ---------------- runs list ---------------- */

async function openRuns() {
  state.stream?.stop();
  state.hitl?.stop();
  state.stream = null;
  state.model = null;
  show("runs");
  $("view-tabs").hidden = true;
  const list = $("runs-list");
  list.replaceChildren(loading());
  try {
    const res = await fetch("/studies");
    if (!res.ok) throw new Error(`GET /studies → ${res.status}`);
    const rows = await res.json();
    list.replaceChildren(...rows.map(runCard));
    if (rows.length === 0) list.replaceChildren(muted("Журналов пока нет — запустите `coscientist run …` или POST /studies/{id}/run."));
  } catch (err) {
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
  chip.textContent = row.verdict ?? "открыто";
  const request = document.createElement("p");
  request.textContent = row.request ?? "";
  el.append(id, request, chip);
  el.addEventListener("click", () => { location.hash = `#/study/${encodeURIComponent(row.study_id)}`; });
  return el;
}

/* ---------------- study view ---------------- */

function openStudy(studyId) {
  if (state.studyId !== studyId || !state.model) {
    state.stream?.stop();
    state.hitl?.stop();
    state.hitl = new HitlPanel($("hitl-requests"), studyId);
    state.hitl.refresh();
    state.model = new StudyModel();
    state.model.onUpdate = renderStudy;
    state.studyId = studyId;
    state.selectedDoc = null;
    $("details").replaceChildren();
    $("graph-details").textContent = "Выберите узел графа для просмотра связей и данных.";
    $("doc-title").textContent = "Просмотр";
    $("doc-view").textContent = "Выберите документ слева.";
  }
  show("study");
  $("view-tabs").hidden = false;
  $("study-id").textContent = studyId;
  const verdictChip = $("study-verdict");
  verdictChip.hidden = false;
  verdictChip.className = "chip";
  verdictChip.textContent = "загрузка…";
  $("study-live").hidden = false;

  const model = state.model;
  const active = () => state.model === model && state.studyId === studyId;
  state.stream = new StudyStream(
    studyId,
    (batch) => {
      if (!active()) return;
      model.apply(batch);
      if (batch.some(event => event.type.startsWith("Hitl") || event.type === "StudyFinalized")) state.hitl.refresh();
      if (state.tab === "graph") state.graph?.update();
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
    chip.textContent = m.verdict;
  } else {
    chip.className = "chip";
    chip.textContent = "идёт исследование";
  }
  renderTasks();
  renderFeed();
  renderDocs();
  renderEventsTable();
}

/* ---------------- tabs ---------------- */

for (const btn of document.querySelectorAll("#view-tabs .tab")) {
  btn.addEventListener("click", () => setTab(btn.dataset.tab));
}

function setTab(tab) {
  state.tab = tab;
  if (tab === "hitl") state.hitl?.refresh();
  for (const btn of document.querySelectorAll("#view-tabs .tab")) {
    btn.classList.toggle("active", btn.dataset.tab === tab);
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

function renderTasks() {
  const m = state.model;
  const ul = $("task-list");
  ul.replaceChildren(...m.taskRows().map((t) => {
    const li = document.createElement("li");
    li.className = "task-row";
    const key = document.createElement("span");
    key.className = "task-key";
    key.textContent = t.key;
    const st = document.createElement("span");
    st.className = `task-state-${t.outcome ?? t.state}`;
    st.textContent = t.outcome ?? t.state;
    li.append(key, st);
    li.addEventListener("click", () => showTaskDetails(t));
    return li;
  }));
}

function renderFeed() {
  const m = state.model;
  const ul = $("event-feed");
  const prevFirst = ul.firstElementChild?.dataset?.seq;
  const rows = m.events.slice(-200).reverse();
  ul.replaceChildren(...rows.map((ev) => {
    const li = document.createElement("li");
    li.className = "event-row";
    li.dataset.seq = String(ev.seq);
    if (prevFirst && Number(ev.seq) > Number(prevFirst)) li.classList.add("new");
    const seq = document.createElement("span");
    seq.className = "event-seq";
    seq.textContent = String(ev.seq);
    const type = document.createElement("span");
    type.className = "event-type";
    type.textContent = ev.type;
    const entity = document.createElement("span");
    entity.className = "event-entity";
    entity.textContent = shortEntity(ev);
    li.append(seq, type, entity);
    li.addEventListener("click", () => showEventDetails(ev));
    return li;
  }));
}

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
    li.tabIndex = 0;
    li.setAttribute("role", "button");
    li.addEventListener("keydown", (event) => { if (event.key === "Enter") li.click(); });
    if (state.selectedDoc === a.id) li.classList.add("active");
    li.addEventListener("click", () => { state.selectedDoc = a.id; renderDocs(); openDoc(a); });
    return li;
  }));
}

async function openDoc(a) {
  const sid = state.studyId;
  const current = () => state.studyId === sid && state.selectedDoc === a.id;
  const url = `/studies/${encodeURIComponent(sid)}/artifacts/${encodeURIComponent(a.id)}`;
  $("doc-title").textContent = a.name || a.id;
  const view = $("doc-view");
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
    const meta = document.createElement("p");
    meta.className = "muted";
    meta.textContent = `${a.media_type} · ${blob.size} байт · SHA-256 ${a.sha256} · ${a.provenance || a.producer || "источник не указан"}`;
    view.replaceChildren(download, meta);
    if (a.media_type?.startsWith("image/")) {
      const image = document.createElement("img");
      image.src = objectUrl; image.alt = a.name || a.id;
      view.append(image);
    } else if (a.media_type?.startsWith("text/") || a.media_type?.includes("json")) {
      const text = await blob.text();
      if (!current()) { URL.revokeObjectURL(objectUrl); return; }
      const content = document.createElement("div");
      content.className = "document-content";
      if (a.media_type.includes("markdown")) renderMarkdown(content, text);
      else { const pre = document.createElement("pre"); pre.textContent = text; content.append(pre); }
      view.append(content);
    }
    // Revoke previous preview URLs when a different document replaces this view.
    const previous = view.dataset.objectUrl;
    if (previous) URL.revokeObjectURL(previous);
    view.dataset.objectUrl = objectUrl;
  } catch (err) {
    if (current()) view.textContent = `Не удалось прочитать: ${err.message}`;
  }
}

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
  const d = $("details");
  const kv = kvList([
    ["node_id", node.node_id],
    ["kind", node.kind],
    ["ref", node.ref_id ?? "—"],
  ]);
  d.replaceChildren(kv);
  const related = [...state.model.graph.edges.values()].filter(e => e.src_id === node.node_id || e.dst_id === node.node_id);
  $("graph-details").textContent = `${node.kind} · ${node.node_id}\n${JSON.stringify(node.attrs, null, 2)}\n` + related.map(e => `${e.src_id} —${e.kind}→ ${e.dst_id}`).join("\n");
  const stmt = node.attrs?.statement;
  if (typeof stmt === "string" && stmt) {
    const pre = document.createElement("pre");
    pre.textContent = stmt;
    d.append(pre);
  }
}

function showTaskDetails(t) {
  const d = $("details");
  const spec = t.spec ?? {};
  d.replaceChildren(
    kvList([
      ["task", t.key],
      ["статус", t.outcome ?? t.state],
      ["kind", t.kind],
      ["цель", spec.goal ?? "—"],
      ["route", spec.route ?? "—"],
      ["depends_on", (spec.depends_on ?? []).join(", ") || "—"],
      ["outputs", (spec.expected_outputs ?? []).map((o) => o.name).join(", ") || "—"],
    ]),
  );
  if (typeof spec.route_hint === "string" && spec.route_hint) {
    const pre = document.createElement("pre");
    pre.textContent = spec.route_hint;
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
  $("view-runs").hidden = name !== "runs";
  $("view-study").hidden = name !== "study";
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
route();
