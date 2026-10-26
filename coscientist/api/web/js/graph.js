/** Journal graph in provenance layers; cyclic components remain inspectable. */
const KIND_STYLE = {
  study:       { color: "#4cc2a9", shape: "circle",  size: 26 },
  question:    { color: "#a08bd0", shape: "rect",    size: 14 },
  criterion:   { color: "#d9a84e", shape: "rect",    size: 14 },
  hypothesis:  { color: "#d07aa0", shape: "rect",    size: 14 },
  evidence:    { color: "#5b9bd5", shape: "rect",    size: 12 },
  artifact:    { color: "#4c9bd5", shape: "rect",    size: 12 },
  conclusion:  { color: "#58c476", shape: "rect",    size: 14 },
  spec:        { color: "#8b98a8", shape: "rect",    size: 14 },
  report:      { color: "#dbe4ee", shape: "rect",    size: 16 },
};
const COL_W = 300;
const ROW_H = 128;
const KIND_LABELS = {study:"Исследование",spec:"Постановка",question:"Вопрос",criterion:"Критерий",hypothesis:"Гипотеза",experiment:"Эксперимент",evidence:"Доказательство",artifact:"Артефакт",conclusion:"Вывод",report:"Отчёт"};
const EDGE_LABELS = {derived_from:"Получено из",relates_to:"Относится к",about:"О предмете",supports:"Подтверждает",refutes:"Опровергает",satisfies:"Удовлетворяет",contains:"Содержит",depends_on:"Зависит от"};

export function focusGraph(graph, kind) {
  if (!kind) return graph;
  const selected = new Set([...graph.nodes].filter(([, node]) => node.kind === kind).map(([id]) => id));
  const included = new Set(selected);
  for (const edge of graph.edges.values()) {
    if (selected.has(edge.src_id) || selected.has(edge.dst_id)) {
      included.add(edge.src_id); included.add(edge.dst_id);
    }
  }
  return {
    nodes: new Map([...graph.nodes].filter(([id]) => included.has(id))),
    edges: new Map([...graph.edges].filter(([, edge]) => included.has(edge.src_id) && included.has(edge.dst_id))),
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
  for (const [level, members] of [...columns].sort(([a],[b]) => a-b)) {
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
    members.forEach((id,row) => positions.set(id,{level,row,x:level*COL_W,y:row*ROW_H}));
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
    this.gEdges = document.createElementNS("http://www.w3.org/2000/svg", "g");
    this.gNodes = document.createElementNS("http://www.w3.org/2000/svg", "g");
    root.setAttribute("id", `${this.idPrefix}-root`);
    root.append(this.gEdges, this.gNodes);
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

  select(id) {
    this.selectedId = id;
    for (const child of this.gNodes.children) {
      child.classList.toggle("selected", child.dataset.id === id);
      child.setAttribute("aria-pressed", String(child.dataset.id === id));
    }
  }

  _place() {
    this.visible = focusGraph(this.model.graph, this.kind);
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
    this._applyTransform();
    this._meta();
    if (focused) [...this.gNodes.children].find(node => node.dataset.id === focused)?.focus();
  }

  _renderNode(id, node, p) {
    const style = KIND_STYLE[node.kind] ?? { color: "#8b98a8", shape: "rect", size: 12 };
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.setAttribute("class", "gnode");
    g.setAttribute("data-id", id);
    g.setAttribute("data-kind", node.kind);
    g.setAttribute("transform", `translate(${p.x},${p.y})`);
    const label = nodeLabel(node);
    g.classList.add("evidence-node");
    g.classList.toggle("selected", this.selectedId === id);
    g.setAttribute("aria-pressed", String(this.selectedId === id));
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
    title.textContent = `${label}\n${id}`; g.append(title);
    for (const [index, line] of labelLines(label).entries()) {
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
      this.select(id);
      this.onSelect?.({type: "node", node});
    };
    g.setAttribute("tabindex", "0"); g.setAttribute("role", "button");
    g.setAttribute("aria-label", `${KIND_LABELS[node.kind] ?? node.kind}: ${label}`);
    g.addEventListener("click", select);
    g.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); } });
    return g;
  }

  _renderEdge(id, edge, a, b) {
    const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("class", `gedge ${edge.kind}`);
    path.setAttribute("data-id", id);
    path.setAttribute("marker-end", `url(#${this.idPrefix}-arrow)`);
    const direction = b.x >= a.x ? 1 : -1;
    const x1 = a.x + direction * 110, y1 = a.y, x2 = b.x - direction * 110, y2 = b.y;
    const mx = (x1 + x2) / 2;
    const sameColumn = a.x === b.x;
    const lane = a.x + 145 + (a.y > b.y ? 18 : 0);
    const loop = edge.src_id === edge.dst_id;
    path.setAttribute("d", loop ? `M ${a.x+110} ${a.y-20} H ${lane} V ${a.y+20} H ${a.x+110}` : sameColumn ? `M ${a.x+110} ${y1} H ${lane} V ${y2} H ${b.x+110}` : `M ${x1} ${y1} H ${mx} V ${y2} H ${x2}`);
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    title.textContent = `${edge.kind}: ${edge.src_id} → ${edge.dst_id}`;
    path.append(title);
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("class", "edge-label"); label.setAttribute("text-anchor", "middle");
    label.setAttribute("x", sameColumn ? lane : mx); label.setAttribute("y", (y1+y2)/2-7);
    label.textContent = EDGE_LABELS[edge.kind] ?? edge.kind;
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

  _meta() {
    const meta = document.getElementById("graph-meta");
    if (meta && this.model) {
      meta.textContent = `${this.visible.nodes.size} из ${this.model.graph.nodes.size} узлов · ${this.visible.edges.size} связей · фокус включает соседние источники`;
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
