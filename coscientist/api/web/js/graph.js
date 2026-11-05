/** Journal graph in provenance layers; cyclic components remain inspectable. */
const KIND_STYLE = {
  study:       { color: "#4cc2a9", shape: "circle",  size: 26 },
  question:    { color: "#a08bd0", shape: "rect",    size: 14 },
  criterion:   { color: "#d9a84e", shape: "rect",    size: 14 },
  hypothesis:  { color: "#d07aa0", shape: "rect",    size: 14 },
  evidence:    { color: "#5b9bd5", shape: "rect",    size: 12 },
  artifact:    { color: "#4c9bd5", shape: "rect",    size: 12 },
  claim:       { color: "#c9704e", shape: "rect",    size: 14 },
  conclusion:  { color: "#58c476", shape: "rect",    size: 14 },
  spec:        { color: "#8b98a8", shape: "rect",    size: 14 },
  report:      { color: "#dbe4ee", shape: "rect",    size: 16 },
};
const COL_W = 300;
const ROW_H = 128;
/** Levels per serpentine band: deeper chains wrap into rows, not one endless strip. */
export const WRAP_AFTER = 7;
/** Rows per column chunk: a wide fan-in (per-fact evidence) wraps sideways —
 * it never stacks into one screen-tall column that auto-fit shrinks to dust. */
export const MAX_COL_ROWS = 8;
/** Empty rows between wrapped bands, so rows read as separate lines of text. */
const BAND_GAP_ROWS = 3;
const KIND_LABELS = {study:"Исследование",spec:"Постановка",question:"Вопрос",criterion:"Критерий",hypothesis:"Гипотеза",experiment:"Эксперимент",evidence:"Доказательство",artifact:"Артефакт",claim:"Утверждение",conclusion:"Вывод",report:"Отчёт"};
const EDGE_LABELS = {
  derived_from: "Получено из",
  relates_to: "Относится к",
  about: "О предмете",
  supports: "Подтверждает",
  refutes: "Опровергает",
  satisfies: "Удовлетворяет",
  contains: "Содержит",
  depends_on: "Зависит от",
  addresses: "Отвечает на",
  asks: "Ставит вопрос",
  answers: "Отвечает",
  measured_by: "Измеряется критерием",
  plans: "Планирует",
  intent: "Намерение",
  uses_input: "Использует вход",
  uses_claim: "Опирается на утверждение",
  contradicts: "Противоречит",
  produced: "Произвёл",
  cites: "Ссылается на",
  based_on: "Основан на",
  renders: "Визуализирует",
  supersedes: "Заменяет",
  decomposes: "Разбивает на",
  origin: "Исходит из",
  constrained_by: "Ограничен",
};
/** Stroke per edge kind — single source for arrows and the toolbar legend
 * (hex mirrors the console palette in console.css :root). */
export const EDGE_COLORS = {
  derived_from:"#8bb5bc", relates_to:"#32679c", about:"#8263a5", addresses:"#246b78",
  supports:"#28715b", satisfies:"#92621d", contains:"#aa6386", produced:"#aa6386",
  refutes:"#a23f43", contradicts:"#a23f43", depends_on:"#60717f",
};
const EDGE_DEFAULT_COLOR = "#99abbc";

/** Per-fact provenance: every journal fact of one tool result is its own node
 * with a digest id. Legitimate graph content, but as a VIEW it drowns the
 * science nodes 31-to-8 — the default science focus hides it; the
 * Доказательства focus and «Все узлы» keep it one click away. */
const isFactEvidence = node => node.kind === "evidence" && node.node_id.includes(":fact:");

/** A digest-only artifact carries no human-readable descriptor anywhere —
 * journal bookkeeping rather than science content. */
const DISPLAY_FIELDS = [
  "display_name", "purpose", "summary", "filename", "media_type", "title", "name", "text",
];
const isServiceArtifact = node => node.kind === "artifact"
  && !DISPLAY_FIELDS.some(key => typeof (node.attrs ?? {})[key] === "string" && node.attrs[key]);

/** @param opts.showServiceArtifacts keep digest-only artifacts in the science focus */
export function focusGraph(graph, kind, hiddenKinds, opts = {}) {
  const hidden = hiddenKinds ?? null;
  const shown = new Set(
    [...graph.nodes].filter(([, node]) => !hidden || !hidden.has(node.kind)).map(([id]) => id),
  );
  const cut = (ids) => ({
    nodes: new Map(
      [...graph.nodes].filter(([id]) => ids.has(id) && shown.has(id)),
    ),
    edges: new Map(
      [...graph.edges].filter(([, edge]) => (
        ids.has(edge.src_id) && ids.has(edge.dst_id)
        && shown.has(edge.src_id) && shown.has(edge.dst_id)
      )),
    ),
  });
  if (kind === "all") {
    if (!hidden) return graph;
    return cut(new Set(graph.nodes.keys()));
  }
  const scienceNode = node => !isFactEvidence(node)
    && (opts.showServiceArtifacts || !isServiceArtifact(node));
  const selected = new Set(
    [...graph.nodes]
      .filter(([id, node]) => (kind ? node.kind === kind : scienceNode(node)) && shown.has(id))
      .map(([id]) => id),
  );
  if (!kind) {
    // The science focus is a pure filter: widening to the neighbourhood would
    // pull the whole fact fan straight back in.
    return cut(selected);
  }
  const included = new Set(selected);
  for (const edge of graph.edges.values()) {
    if (selected.has(edge.src_id) || selected.has(edge.dst_id)) {
      included.add(edge.src_id); included.add(edge.dst_id);
    }
  }
  return cut(included);
}

/** Collapsible clusters. A cluster is anchored on a hypothesis (or, failing
 * that, a claim node) and keeps the criterion/evidence/artifact cards linked
 * to it; nodes not attached to any head group into one bucket per kind.
 * study/question stay outside as hubs. Membership: node_id -> cluster key,
 * where the key is the head's node_id or `kind:<kind>` for buckets. */
const MEMBER_KINDS = ["criterion", "evidence", "artifact"];
const HEAD_PRIORITY = {hypothesis: 0, claim: 1};
const isHubKind = kind => kind === "study" || kind === "question";

export function assignClusters(graph) {
  const incident = new Map([...graph.nodes.keys()].map(id => [id, []]));
  for (const edge of graph.edges.values()) {
    incident.get(edge.src_id)?.push(edge.dst_id);
    incident.get(edge.dst_id)?.push(edge.src_id);
  }
  const membership = new Map();
  for (const [id, node] of graph.nodes) {
    if (node.kind in HEAD_PRIORITY) membership.set(id, id);
  }
  const headRank = id => HEAD_PRIORITY[graph.nodes.get(id).kind];
  for (const [id, node] of graph.nodes) {
    if (membership.has(id) || isHubKind(node.kind) || !MEMBER_KINDS.includes(node.kind)) continue;
    const heads = (incident.get(id) ?? []).filter(other => membership.get(other) === other)
      .sort((a, b) => headRank(a) - headRank(b) || a.localeCompare(b));
    if (heads.length) membership.set(id, heads[0]);
  }
  for (const [id, node] of graph.nodes) {
    if (!membership.has(id) && !isHubKind(node.kind)) membership.set(id, `kind:${node.kind}`);
  }
  return membership;
}

/** Collapse every non-expanded cluster into one card node carrying the head's
 * (or the bucket's first descriptive) label and the member count; cross-cluster
 * edges aggregate per (src, kind, dst) into one edge with a count. Returns
 * {graph, clusters}: clusters maps key -> member ids of the collapsed clusters. */
export function clusterGraph(graph, expanded, membership = assignClusters(graph)) {
  const members = new Map();
  for (const [id, key] of membership) {
    if (!graph.nodes.has(id)) continue;
    if (!members.has(key)) members.set(key, []);
    members.get(key).push(id);
  }
  const place = id => {
    const key = membership.get(id);
    return key !== undefined && !expanded.has(key) ? `cluster:${key}` : id;
  };
  const repOf = (key, ids) => graph.nodes.get(key)
    ?? ids.map(id => graph.nodes.get(id)).find(node => !isServiceArtifact(node))
    ?? graph.nodes.get(ids[0]);
  const nodes = new Map(), edges = new Map(), aggregated = new Map();
  for (const [key, ids] of members) {
    if (expanded.has(key)) continue;
    ids.sort();
    const rep = repOf(key, ids);
    nodes.set(`cluster:${key}`, {
      ...rep,
      node_id: `cluster:${key}`,
      cluster_key: key,
      cluster_count: ids.length,
    });
  }
  for (const [id, node] of graph.nodes) {
    if (place(id) === id) nodes.set(id, node);
  }
  for (const [id, edge] of graph.edges) {
    const src = place(edge.src_id), dst = place(edge.dst_id);
    if (src === edge.src_id && dst === edge.dst_id) { edges.set(id, edge); continue; }
    if (src === dst) continue;   // internal to one collapsed cluster
    const aggKey = `${src}|${dst}|${edge.kind}`;
    const seen = aggregated.get(aggKey);
    if (seen) { seen.edge_count += 1; continue; }
    const card = {...edge, src_id: src, dst_id: dst, edge_count: 1};
    aggregated.set(aggKey, card);
    edges.set(`agg:${aggKey}`, card);
  }
  return {
    graph: {nodes, edges},
    clusters: new Map([...members].filter(([key]) => !expanded.has(key))),
  };
}

/** Rank the actual directed graph, condensing cycles without deleting any links. */
export function layoutEvidenceGraph(graph) {
  const ids = [...graph.nodes.keys()].sort();
  const outgoing = new Map(ids.map(id => [id, []])), incoming = new Map(ids.map(id => [id, []]));
  for (const edge of graph.edges.values()) {
    if (!outgoing.has(edge.src_id) || !incoming.has(edge.dst_id)) continue;
    outgoing.get(edge.src_id).push(edge.dst_id); incoming.get(edge.dst_id).push(edge.src_id);
  }
  for (const list of [...outgoing.values(), ...incoming.values()]) list.sort();
  const visited = new Set(), order = [];
  for (const root of ids) {
    if (visited.has(root)) continue;
    visited.add(root);
    const stack = [{id:root, next:0}];
    while (stack.length) {
      const frame = stack.at(-1), children = outgoing.get(frame.id);
      if (frame.next === children.length) { order.push(frame.id); stack.pop(); continue; }
      const child = children[frame.next++];
      if (!visited.has(child)) { visited.add(child); stack.push({id:child, next:0}); }
    }
  }
  const component = new Map(), groups = [];
  for (const root of order.reverse()) {
    if (component.has(root)) continue;
    const index = groups.length, members = [], stack = [root];
    component.set(root,index);
    while (stack.length) {
      const id = stack.pop(); members.push(id);
      for (const parent of incoming.get(id)) if (!component.has(parent)) {
        component.set(parent,index); stack.push(parent);
      }
    }
    groups.push(members.sort());
  }
  const children = groups.map(() => new Set()), indegree = groups.map(() => 0), levels = groups.map(() => 0);
  for (const [id, targets] of outgoing) for (const target of targets) {
    const a = component.get(id), b = component.get(target);
    if (a !== b && !children[a].has(b)) { children[a].add(b); indegree[b]++; }
  }
  const queue = groups.map((_, index) => index).filter(index => indegree[index] === 0);
  for (let cursor=0; cursor<queue.length; cursor++) for (const child of children[queue[cursor]]) {
    levels[child] = Math.max(levels[child],levels[queue[cursor]]+1);
    if (--indegree[child] === 0) queue.push(child);
  }
  const positions = new Map(), columns = new Map();
  for (const id of ids) {
    const level = levels[component.get(id)];
    if (!columns.has(level)) columns.set(level,[]);
    columns.get(level).push(id);
  }
  const ordered = [...columns].sort(([a],[b]) => a-b);
  // A deep journal chain must not render as one endless strip, and a wide
  // fan-in must not render as one screen-tall column. Levels pack into
  // serpentine bands: a level occupies ceil(size / MAX_COL_ROWS) columns
  // (rows beyond one chunk wrap sideways), bands hold up to WRAP_AFTER
  // columns and alternate direction like text lines. Band columns are
  // ALLOCATED (a level never shares a column with another level), and each
  // band stacks below the tallest previous band plus a generous gap.
  const chunksOf = members => Math.max(1, Math.ceil(members.length / MAX_COL_ROWS));
  const bands = [];   // {levels: [{level, members, width}], width, rows}
  for (const [level, members] of ordered) {
    const width = chunksOf(members);
    let band = bands.at(-1);
    if (!band || band.width + width > WRAP_AFTER) {
      band = {levels: [], width: 0, rows: 0};
      bands.push(band);
    }
    band.levels.push({level, members, width});
    band.width += width;
    band.rows = Math.max(band.rows, Math.min(members.length, MAX_COL_ROWS));
  }
  const bandTop = new Map();
  let top = 0;
  for (const [index, band] of bands.entries()) {
    bandTop.set(index, top);
    top += (band.rows + BAND_GAP_ROWS) * ROW_H;
  }
  for (const [index, band] of bands.entries()) {
    // Even bands flow left-to-right, odd bands right-to-left (serpentine).
    // Backward bands start from the full WRAP_AFTER track, not band.width —
    // a partial last band then hugs the right edge and the chain continues
    // straight down instead of jumping left.
    const backward = index % 2 === 1;
    let cursor = backward ? WRAP_AFTER : 0;
    for (const {level, members, width} of band.levels) {
      const barycenter = id => {
        const rows = incoming.get(id).filter(parent => positions.has(parent)).map(parent => positions.get(parent).row);
        return rows.length ? rows.reduce((a,b)=>a+b,0)/rows.length : Infinity;
      };
      members.sort((a,b) => {
        const centerA=barycenter(a), centerB=barycenter(b);
        if (centerA !== centerB) return centerA-centerB;
        const degree = id => outgoing.get(id).length+incoming.get(id).length;
        return degree(b)-degree(a) || a.localeCompare(b);
      });
      const base = backward ? cursor - width : cursor;
      members.forEach((id,row) => {
        const chunk = Math.floor(row / MAX_COL_ROWS);
        const chunkRow = row % MAX_COL_ROWS;
        positions.set(id,{level,band:index,row:chunkRow,x:(base + chunk)*COL_W,y:bandTop.get(index)+chunkRow*ROW_H});
      });
      cursor += backward ? -width : width;
    }
  }
  // An entirely disconnected journal still gets a usable overview, not one tall strip.
  if (![...outgoing.values()].some(targets => targets.length)) {
    const width = Math.max(1,Math.min(4,Math.ceil(Math.sqrt(ids.length))));
    ids.forEach((id,index) => positions.set(id,{level:index%width,row:Math.floor(index/width),x:(index%width)*COL_W,y:Math.floor(index/width)*ROW_H}));
  }
  return positions;
}

export class GraphView {
  /** @param {HTMLElement} host container; sized by CSS (100% × 100%) */
  constructor(host, { onSelect, idPrefix = "graph" } = {}) {
    this.host = host;
    this.onSelect = onSelect;
    this.idPrefix = idPrefix;
    this.model = null;
    this.selectedId = null;
    this.kind = "";
    this.hiddenKinds = new Set();   // node kinds unchecked in the toolbar filter
    this.showServiceArtifacts = false; // «Показать служебные» (science focus)
    this.expanded = new Set();     // cluster keys clicked open; default all collapsed
    this.focusNeighbors = true;     // selection dims non-neighbours (WorkflowView opts out)
    this.pos = new Map();      // node_id -> {x, y, level}
    this.zoom = 1;
    this.panX = 0;
    this.panY = 0;
    this.autoFit = true;
    this._buildDom();
    this._wirePointer();
    this._observeResize();
  }

  _buildDom() {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const root = document.createElementNS("http://www.w3.org/2000/svg", "g");
    this.gBands = document.createElementNS("http://www.w3.org/2000/svg", "g");
    this.gEdges = document.createElementNS("http://www.w3.org/2000/svg", "g");
    this.gNodes = document.createElementNS("http://www.w3.org/2000/svg", "g");
    root.setAttribute("id", `${this.idPrefix}-root`);
    root.append(this.gBands, this.gEdges, this.gNodes);
    const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    const marker = document.createElementNS("http://www.w3.org/2000/svg", "marker");
    marker.setAttribute("id", `${this.idPrefix}-arrow`); marker.setAttribute("viewBox", "0 0 10 10");
    marker.setAttribute("refX", "10"); marker.setAttribute("refY", "5");
    marker.setAttribute("markerWidth", "7"); marker.setAttribute("markerHeight", "7");
    marker.setAttribute("orient", "auto-start-reverse");
    const arrow = document.createElementNS("http://www.w3.org/2000/svg", "path");
    arrow.setAttribute("d", "M 0 0 L 10 5 L 0 10 z"); arrow.setAttribute("fill", "#8b98a8");
    marker.append(arrow); defs.append(marker); svg.append(defs, root);
    this.svg = svg;
    this.root = root;
    this.host.replaceChildren(svg);
  }

  _wirePointer() {
    let dragging = false;
    let sx = 0, sy = 0;
    this.svg.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".gnode")) return;
      dragging = true; sx = e.clientX; sy = e.clientY;
      this.svg.setPointerCapture(e.pointerId);
    });
    this.svg.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const dx = e.clientX - sx, dy = e.clientY - sy;
      if (Math.abs(dx) + Math.abs(dy) > 3) { this.autoFit = false; }
      this.panX += dx; this.panY += dy;
      sx = e.clientX; sy = e.clientY;
      this._applyTransform();
    });
    this.svg.addEventListener("pointerup", () => { dragging = false; });
    this.svg.addEventListener("pointercancel", () => { dragging = false; });
    this.svg.addEventListener("wheel", (e) => {
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.12 : 0.89;
      const rect = this.svg.getBoundingClientRect();
      const cx = e.clientX - rect.left;
      const cy = e.clientY - rect.top;
      this.zoomBy(factor, cx, cy);
    }, { passive: false });
  }

  zoomBy(factor, cx = this.host.clientWidth / 2, cy = this.host.clientHeight / 2) {
    const previous = this.zoom;
    this.zoom = Math.min(4, Math.max(0.02, previous * factor));
    const ratio = this.zoom / previous;
    this.autoFit = false;
    this.panX = cx - (cx - this.panX) * ratio;
    this.panY = cy - (cy - this.panY) * ratio;
    this._applyTransform();
  }

  _observeResize() {
    // Window resize / panel collapse: keep the drawing centered in the host.
    // Until the user pans or zooms manually, re-fit outright; afterwards shift
    // the pan by half the size delta so the content stays put (a stale
    // transform here is what made the old console drift out of the window).
    this._lastW = this.host.clientWidth;
    this._lastH = this.host.clientHeight;
    const ro = new ResizeObserver(() => {
      const dw = this.host.clientWidth - this._lastW;
      const dh = this.host.clientHeight - this._lastH;
      this._lastW = this.host.clientWidth;
      this._lastH = this.host.clientHeight;
      if (this.pos.size === 0) return;
      if (this.autoFit) {
        this.fit();
      } else {
        this.panX += dw / 2;
        this.panY += dh / 2;
        this._applyTransform();
      }
    });
    ro.observe(this.host);
  }

  setModel(model) {
    if (this.model !== model) {
      this.selectedId = null;
      this.kind = "";
      this.pos.clear();
      this.gNodes.replaceChildren(); this.gEdges.replaceChildren();
      this.autoFit = true; this.zoom = 1; this.panX = 0; this.panY = 0;
    }
    this.model = model;
    this._place();
    this._syncDom();
    if (this.autoFit) this.fit();
  }

  /** Apply new journal events (already folded into the model). */
  update() {
    if (!this.model) return;
    this._place();
    this._syncDom();
    if (this.autoFit) this.fit();
  }

  setKind(kind) {
    this.kind = kind;
    this.autoFit = true;
    this.update();
  }

  /** Toolbar kind checkboxes: hide whole kinds on top of the focus select. */
  setHiddenKinds(kinds) {
    this.hiddenKinds = kinds instanceof Set ? kinds : new Set(kinds);
    this.autoFit = true;
    this.update();
  }

  /** Toolbar «Показать служебные»: digest-only artifacts of science focus. */
  setShowServiceArtifacts(show) {
    this.showServiceArtifacts = show === true;
    this.autoFit = true;
    this.update();
  }

  /** Clicking a collapsed cluster card opens it; clicking an expanded cluster's
   * own card folds it back. Expansion survives model updates. */
  toggleCluster(key) {
    if (this.expanded.has(key)) this.expanded.delete(key);
    else this.expanded.add(key);
    this.autoFit = true;
    this.update();
  }

  select(id) {
    this.selectedId = id;
    for (const child of this.gNodes.children) {
      child.classList.toggle("selected", child.dataset.id === id);
      child.setAttribute("aria-pressed", String(child.dataset.id === id));
    }
    this._applyEmphasis();
  }

  /** Node ids directly linked to `id` in the currently visible graph. */
  neighborsOf(id) {
    const ids = new Set([id]);
    for (const edge of this.visible.edges.values()) {
      if (edge.src_id === id) ids.add(edge.dst_id);
      if (edge.dst_id === id) ids.add(edge.src_id);
    }
    return ids;
  }

  /** Selection focus: dim non-neighbours, brighten incident edges and reveal
   * their labels. WorkflowView edges carry no endpoint data and are skipped. */
  _applyEmphasis() {
    const neighbors = this.focusNeighbors && this.selectedId && this.visible
      ? this.neighborsOf(this.selectedId) : null;
    for (const node of this.gNodes.children) {
      node.classList.toggle("dimmed", neighbors !== null && !neighbors.has(node.dataset.id));
    }
    for (const edge of this.gEdges.children) {
      const {src, dst} = edge.dataset;
      if (src === undefined) continue;
      const incident = this.selectedId != null && (src === this.selectedId || dst === this.selectedId);
      edge.classList.toggle("lit", incident);
      edge.classList.toggle("dimmed", neighbors !== null && !incident);
      edge.querySelector(".edge-label")?.classList.toggle("show", incident);
    }
  }

  _place() {
    const focused = focusGraph(this.model.graph, this.kind, this.hiddenKinds,
      {showServiceArtifacts: this.showServiceArtifacts});
    this.visible = clusterGraph(focused, this.expanded).graph;
    this.pos = layoutEvidenceGraph(this.visible);
  }

  _syncDom() {
    const focused = this.svg.contains(document.activeElement) ? document.activeElement.dataset.id : null;
    const m = {graph: this.visible};
    this.gNodes.replaceChildren();
    this.gEdges.replaceChildren();
    for (const [id, node] of m.graph.nodes) {
      const p = this.pos.get(id);
      if (!p) continue;
      this.gNodes.append(this._renderNode(id, node, p));
    }
    for (const [id, edge] of m.graph.edges) {
      const a = this.pos.get(edge.src_id);
      const b = this.pos.get(edge.dst_id);
      if (!a || !b) continue;
      this.gEdges.append(this._renderEdge(id, edge, a, b));
    }
    this._renderBands();
    this._applyEmphasis();
    this._applyTransform();
    this._meta();
    if (focused) [...this.gNodes.children].find(node => node.dataset.id === focused)?.focus();
  }

  /** Faint separators + level ranges above each serpentine band of a wrapped chain. */
  _renderBands() {
    this.gBands.replaceChildren();
    if (this.visible.edges.size === 0) return;
    const bands = new Map();
    for (const p of this.pos.values()) {
      const band = p.band ?? Math.floor(p.level / WRAP_AFTER);
      const b = bands.get(band) ?? {minLevel: p.level, maxLevel: p.level, minX: p.x, maxX: p.x, minY: p.y};
      b.minLevel = Math.min(b.minLevel, p.level); b.maxLevel = Math.max(b.maxLevel, p.level);
      b.minX = Math.min(b.minX, p.x); b.maxX = Math.max(b.maxX, p.x);
      b.minY = Math.min(b.minY, p.y);
      bands.set(band, b);
    }
    if (bands.size < 2) return;
    for (const [, b] of [...bands].sort(([a],[c]) => a-c)) {
      const y = b.minY - ROW_H;
      const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
      line.setAttribute("class", "band-line");
      line.setAttribute("x1", b.minX - 140); line.setAttribute("x2", b.maxX + 140);
      line.setAttribute("y1", y); line.setAttribute("y2", y);
      const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
      label.setAttribute("class", "band-label");
      label.setAttribute("x", b.minX - 140); label.setAttribute("y", y - 8);
      label.textContent = `Уровни ${b.minLevel}–${b.maxLevel}`;
      this.gBands.append(line, label);
    }
  }

  _renderNode(id, node, p) {
    const style = KIND_STYLE[node.kind] ?? { color: "#8b98a8", shape: "rect", size: 12 };
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.setAttribute("class", "gnode");
    g.setAttribute("data-id", id);
    g.setAttribute("data-kind", node.kind);
    g.setAttribute("transform", `translate(${p.x},${p.y})`);
    const label = nodeLabel(node);
    const text = node.cluster_count > 1 ? `${label} · ${node.cluster_count}` : label;
    g.classList.add("evidence-node");
    g.classList.toggle("selected", this.selectedId === id);
    g.setAttribute("aria-pressed", String(this.selectedId === id));
    if (node.cluster_key) {
      g.classList.add("cluster");
      g.setAttribute("aria-expanded", String(this.expanded.has(node.cluster_key)));
    }
    {
      const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      r.setAttribute("x", -110); r.setAttribute("y", -50);
      r.setAttribute("width", 220); r.setAttribute("height", 100);
      r.setAttribute("rx", 7);
      r.setAttribute("fill", "var(--bg-panel)");
      r.setAttribute("stroke", style.color);
      g.append(r);
    }
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    title.textContent = `${text}\n${id}`; g.append(title);
    for (const [index, line] of labelLines(text).entries()) {
      const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
      t.setAttribute("x", -96); t.setAttribute("y", -4 + index * 17);
      t.textContent = line; g.append(t);
    }
    const sub = document.createElementNS("http://www.w3.org/2000/svg", "text");
    sub.setAttribute("class", "gsub");
    sub.setAttribute("x", -96);
    sub.setAttribute("y", -30);
    sub.textContent = KIND_LABELS[node.kind] ?? node.kind;
    g.append(sub);
    const select = () => {
      if (node.cluster_key) { this.toggleCluster(node.cluster_key); return; }
      this.select(id);
      this.onSelect?.({type: "node", node});
    };
    g.setAttribute("tabindex", "0"); g.setAttribute("role", "button");
    g.setAttribute("aria-label", `${KIND_LABELS[node.kind] ?? node.kind}: ${text}`);
    g.addEventListener("click", select);
    g.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); } });
    return g;
  }

  _renderEdge(id, edge, a, b) {
    const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
    group.setAttribute("class", "gedge-group");
    group.setAttribute("data-src", edge.src_id);
    group.setAttribute("data-dst", edge.dst_id);
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("class", `gedge ${edge.kind}`);
    path.setAttribute("data-id", id);
    path.setAttribute("marker-end", `url(#${this.idPrefix}-arrow)`);
    path.style.stroke = EDGE_COLORS[edge.kind] ?? EDGE_DEFAULT_COLOR;
    const direction = b.x >= a.x ? 1 : -1;
    const x1 = a.x + direction * 110, y1 = a.y, x2 = b.x - direction * 110, y2 = b.y;
    const mx = (x1 + x2) / 2;
    const sameColumn = a.x === b.x;
    const lane = a.x + 145 + (a.y > b.y ? 18 : 0);
    const loop = edge.src_id === edge.dst_id;
    const d = loop
      ? `M ${a.x+110} ${a.y-20} H ${lane} V ${a.y+20} H ${a.x+110}`
      : sameColumn
        ? `M ${a.x+110} ${y1} H ${lane} V ${y2} H ${b.x+110}`
        : `M ${x1} ${y1} H ${mx} V ${y2} H ${x2}`;
    path.setAttribute("d", d);
    const count = edge.edge_count > 1 ? ` ×${edge.edge_count}` : "";
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    title.textContent = `${edge.kind}: ${edge.src_id} → ${edge.dst_id}${count}`;
    path.append(title);
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("class", "edge-label"); label.setAttribute("text-anchor", "middle");
    label.setAttribute("x", sameColumn ? lane : mx); label.setAttribute("y", (y1+y2)/2-7);
    label.textContent = `${EDGE_LABELS[edge.kind] ?? edge.kind}${count}`;
    group.append(path,label);
    return group;
  }

  _applyTransform() {
    this.root.setAttribute("transform", `translate(${this.panX},${this.panY}) scale(${this.zoom})`);
  }

  fit() {
    if (this.pos.size === 0) return;
    const rect = this.host.getBoundingClientRect();
    if (rect.width < 40 || rect.height < 40) return;
    // SVG content bounds include routed links and their labels, in local coordinates.
    // Estimating only card extents clips cycles and long relation labels.
    const bounds = this.root.getBBox();
    const minX = bounds.x - 4, minY = bounds.y - 4;
    const w = Math.max(1, bounds.width + 8), h = Math.max(1, bounds.height + 8);
    this.zoom = Math.min(1.6, Math.min((rect.width - 24) / w, (rect.height - 24) / h));
    this.panX = rect.width / 2 - (minX + w / 2) * this.zoom;
    this.panY = rect.height / 2 - (minY + h / 2) * this.zoom;
    this._applyTransform();
  }

  /** Toolbar «К выбранному»: center the selected card at a readable zoom;
   * without a selection it degrades to «Вписать». */
  fitSelected() {
    const p = this.selectedId ? this.pos.get(this.selectedId) : null;
    if (!p) {
      this.autoFit = true;
      this.fit();
      return;
    }
    const rect = this.host.getBoundingClientRect();
    if (rect.width < 40 || rect.height < 40) return;
    this.autoFit = false;
    if (this.zoom < 0.5) this.zoom = 1;
    this.panX = rect.width / 2 - p.x * this.zoom;
    this.panY = rect.height / 2 - p.y * this.zoom;
    this._applyTransform();
  }

  _meta() {
    const meta = document.getElementById("graph-meta");
    if (meta && this.model) {
      const mode = this.kind === "all" ? "все узлы журнала"
        : this.kind ? "фокус включает соседние источники"
        : this.showServiceArtifacts
          ? "пер-фактовый провенанс скрыт (фокус «Все узлы» покажет)"
          : "пер-фактовый провенанс и служебные артефакты скрыты"
            + " («Все узлы» покажет)";
      meta.textContent = `${this.visible.nodes.size} из ${this.model.graph.nodes.size} узлов · ${this.visible.edges.size} связей · ${mode}`;
    }
  }
}

export function nodeLabel(node) {
  const a = node.attrs ?? {};
  if (typeof a.statement === "string" && a.statement) return a.statement;
  if (typeof a.formulation === "string" && a.formulation) return a.formulation;
  if (typeof a.text === "string" && a.text) return a.text;
  if (typeof a.title === "string" && a.title) return a.title;
  if (typeof a.name === "string" && a.name) return a.name;
  // Artifact-ish nodes carry descriptors instead of prose — prefer them over
  // the raw id segment, which is usually an opaque digest.
  for (const key of ["display_name", "purpose", "summary", "filename", "media_type"]) {
    if (typeof a[key] === "string" && a[key]) return a[key];
  }
  if (typeof a.goal?.statement === "string" && a.goal.statement) return a.goal.statement;
  if (typeof a.request === "string" && a.request) return a.request;
  if (typeof a.check === "string" && a.check) return `Проверка: ${a.check}`;
  return node.node_id.split(":").pop() ?? node.node_id;
}

export const nodeKindLabel = kind => KIND_LABELS[kind] ?? kind;
export const edgeKindLabel = kind => EDGE_LABELS[kind] ?? kind;

export function labelLines(text) {
  const words = String(text).match(/.{1,27}(?:\s|$)|\S{1,27}/gu) ?? [];
  return words.slice(0,3).map((word,index) => index===2 && words.length>3 ? `${word.trim().slice(0,24)}…` : word.trim());
}
