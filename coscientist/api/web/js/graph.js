/** Journal graph in stable semantic columns; links may be cyclic and arrive late. */
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
const COL_W = 250;
const ROW_H = 74;

export class GraphView {
  /** @param {HTMLElement} host container; sized by CSS (100% × 100%) */
  constructor(host, { onSelect } = {}) {
    this.host = host;
    this.onSelect = onSelect;
    this.model = null;
    this.pos = new Map();      // node_id -> {x, y, level}
    this.pending = new Set();  // node_ids needing placement
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
    root.setAttribute("id", "graph-root");
    root.append(this.gEdges, this.gNodes);
    const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
    const marker = document.createElementNS("http://www.w3.org/2000/svg", "marker");
    marker.setAttribute("id", "arrow"); marker.setAttribute("viewBox", "0 0 10 10");
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
    this.svg.addEventListener("wheel", (e) => {
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.12 : 0.89;
      const rect = this.svg.getBoundingClientRect();
      const cx = e.clientX - rect.left;
      const cy = e.clientY - rect.top;
      const previousZoom = this.zoom;
      this.zoom = Math.min(4, Math.max(0.02, this.zoom * factor));
      const ratio = this.zoom / previousZoom;
      this.autoFit = false;
      // zoom around the cursor: keep the point under it fixed
      this.panX = cx - (cx - this.panX) * ratio;
      this.panY = cy - (cy - this.panY) * ratio;
      this._applyTransform();
    }, { passive: false });
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
      this.pos.clear(); this.pending.clear();
      this.gNodes.replaceChildren(); this.gEdges.replaceChildren();
      this.autoFit = true; this.zoom = 1; this.panX = 0; this.panY = 0;
    }
    this.model = model;
    for (const id of model.graph.nodes.keys()) if (!this.pos.has(id)) this.pending.add(id);
    this._placePending();
    this._syncDom();
    if (this.autoFit) this.fit();
  }

  /** Apply new journal events (already folded into the model). */
  update() {
    if (!this.model) return;
    for (const id of this.model.graph.nodes.keys()) if (!this.pos.has(id)) this.pending.add(id);
    this._placePending();
    this._syncDom();
    if (this.autoFit) this.fit();
  }

  _levelOf(id) {
    const kinds = ["study", "spec", "question", "criterion", "hypothesis", "experiment", "evidence", "artifact", "conclusion", "report"];
    const present = [...new Set([...this.model.graph.nodes.values()].map(node => node.kind))];
    present.sort((a, b) => (kinds.indexOf(a) < 0 ? kinds.length : kinds.indexOf(a)) - (kinds.indexOf(b) < 0 ? kinds.length : kinds.indexOf(b)) || a.localeCompare(b));
    return present.indexOf(this.model.graph.nodes.get(id)?.kind);
  }

  _placePending() {
    const rows = new Map();
    for (const id of this.model.graph.nodes.keys()) {
      const level = this._levelOf(id), row = rows.get(level) ?? 0;
      rows.set(level, row + 1);
      this.pos.set(id, {level, row, x: level * COL_W, y: row * ROW_H});
    }
    this.pending.clear();
  }

  _syncDom() {
    const m = this.model;
    if (!m) return;
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
  }

  _renderNode(id, node, p) {
    const style = KIND_STYLE[node.kind] ?? { color: "#8b98a8", shape: "rect", size: 12 };
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
    g.setAttribute("class", "gnode");
    g.setAttribute("data-id", id);
    g.setAttribute("data-kind", node.kind);
    g.setAttribute("transform", `translate(${p.x},${p.y})`);
    const label = nodeLabel(node);
    if (style.shape === "circle") {
      const c = document.createElementNS("http://www.w3.org/2000/svg", "circle");
      c.setAttribute("r", style.size);
      c.setAttribute("fill", "none");
      c.setAttribute("stroke", style.color);
      g.append(c);
    } else {
      const w = Math.min(210, 16 + label.length * 6.4);
      const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      r.setAttribute("x", -w / 2); r.setAttribute("y", -style.size);
      r.setAttribute("width", w); r.setAttribute("height", style.size * 2);
      r.setAttribute("rx", 7);
      r.setAttribute("fill", "rgba(21,26,33,.92)");
      r.setAttribute("stroke", style.color);
      g.append(r);
    }
    const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
    t.setAttribute("text-anchor", "middle");
    t.setAttribute("y", style.shape === "circle" ? 4 : -1);
    t.textContent = truncate(label, 30);
    g.append(t);
    const sub = document.createElementNS("http://www.w3.org/2000/svg", "text");
    sub.setAttribute("class", "gsub");
    sub.setAttribute("text-anchor", "middle");
    sub.setAttribute("y", style.shape === "circle" ? 18 : 12);
    sub.textContent = node.kind;
    g.append(sub);
    const select = () => this.onSelect?.({type: "node", node});
    g.setAttribute("tabindex", "0"); g.setAttribute("role", "button");
    g.setAttribute("aria-label", `${node.kind}: ${label}`);
    g.addEventListener("click", select);
    g.addEventListener("keydown", event => { if (event.key === "Enter") select(); });
    return g;
  }

  _renderEdge(id, edge, a, b) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("class", `gedge ${edge.kind}`);
    path.setAttribute("data-id", id);
    path.setAttribute("marker-end", "url(#arrow)");
    const direction = b.x >= a.x ? 1 : -1;
    const x1 = a.x + direction * 40, y1 = a.y, x2 = b.x - direction * 60, y2 = b.y;
    const mx = (x1 + x2) / 2;
    path.setAttribute("d", `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`);
    const t = document.createElementNS("http://www.w3.org/2000/svg", "text");
    t.setAttribute("class", "glabel");
    t.setAttribute("x", mx);
    t.setAttribute("y", (y1 + y2) / 2);
    t.textContent = edge.kind;
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    title.textContent = `${edge.kind}: ${edge.src_id} → ${edge.dst_id}`;
    path.append(title);
    return path;
  }

  _applyTransform() {
    this.root.setAttribute("transform", `translate(${this.panX},${this.panY}) scale(${this.zoom})`);
  }

  fit() {
    if (this.pos.size === 0) return;
    const rect = this.host.getBoundingClientRect();
    if (rect.width < 40 || rect.height < 40) return;
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const p of this.pos.values()) {
      minX = Math.min(minX, p.x - 120); maxX = Math.max(maxX, p.x + 120);
      minY = Math.min(minY, p.y - 60); maxY = Math.max(maxY, p.y + 60);
    }
    const w = Math.max(1, maxX - minX), h = Math.max(1, maxY - minY);
    this.zoom = Math.min(1.6, Math.min((rect.width - 24) / w, (rect.height - 24) / h));
    this.panX = rect.width / 2 - (minX + w / 2) * this.zoom;
    this.panY = rect.height / 2 - (minY + h / 2) * this.zoom;
    this._applyTransform();
  }

  _meta() {
    const meta = document.getElementById("graph-meta");
    if (meta && this.model) {
      meta.textContent = `${this.model.graph.nodes.size} узлов · ${this.model.graph.edges.size} связей · колонки по типу · направление связей в деталях`;
    }
  }
}

function nodeLabel(node) {
  const a = node.attrs ?? {};
  if (typeof a.statement === "string" && a.statement) return a.statement;
  if (typeof a.text === "string" && a.text) return a.text;
  if (typeof a.title === "string" && a.title) return a.title;
  if (typeof a.name === "string" && a.name) return a.name;
  return node.node_id.split(":").pop() ?? node.node_id;
}

function truncate(s, n) {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}
