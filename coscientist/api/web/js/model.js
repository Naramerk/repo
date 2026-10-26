/** Journal fold: raw events → console state (tasks, graph, artifacts, docs).
 *
 * Pure and monotonic: `apply(batch)` only adds/refines; nothing is re-derived
 * from scratch, so live updates are O(batch) and the graph stays stable.
 * Everything shown traces to a journaled event — the console invents nothing.
 */

const TASK_OPEN = new Set(["PENDING", "RUNNING"]);

export class StudyModel {
  constructor() {
    this.studyId = null;
    this.request = null;
    this.verdict = null;
    this.phase = null;
    this.tasks = new Map();       // task_id -> {key,title,state,outcome,spec}
    this.attempts = new Map();
    this.results = new Map();
    this.events = [];             // raw wire events, seq order
    this.graph = { nodes: new Map(), edges: new Map() };
    this.artifacts = new Map();   // artifact_id -> {id,media_type,sha256,node_id}
    this.usage = { prompt_tokens: 0, completion_tokens: 0 };
    this.onUpdate = null;
    this.lastSeq = 0;
  }

  apply(batch) {
    for (const ev of batch) this._applyOne(ev);
    this.onUpdate?.();
  }

  _applyOne(ev) {
    if (ev.seq <= this.lastSeq) return;
    this.lastSeq = ev.seq;
    this.events.push(ev);
    const { type, payload } = ev;
    switch (type) {
      case "StudyCreated":
        this.studyId = ev.study_id;
        this.request = payload.request;
        break;
      case "StudyFinalized":
        this.verdict = payload.verdict ?? this.verdict;
        this.phase = "closed";
        break;
      case "TaskCreated":
      case "TaskInherited": {
        const spec = payload.spec ?? {};
        this.tasks.set(payload.task_id, {
          id: payload.task_id,
          key: spec.task_key ?? payload.task_id.split("/").pop(),
          title: spec.title ?? spec.task_key ?? "",
          kind: payload.kind ?? "",
          state: payload.state ?? "PENDING",
          outcome: payload.outcome ?? null,
          spec,
        });
        break;
      }
      case "TaskDone": {
        const t = this.tasks.get(payload.task_id);
        if (t) {
          t.outcome = payload.outcome ?? null;
          t.state = "DONE";
          t.detail = payload.detail ?? null;
        }
        break;
      }
      case "AttemptStarted": {
        const tid = payload.task_id;
        const t = this.tasks.get(tid);
        if (t) t.state = "RUNNING";
        this.attempts.set(payload.attempt_id, {...payload, started_at: ev.ts, seq: ev.seq, closed: false});
        break;
      }
      case "AttemptClosed": {
        const old = this.attempts.get(payload.attempt_id) ?? {};
        this.attempts.set(payload.attempt_id, {...old, ...payload, seq: ev.seq, closed: true, closed_at: ev.ts});
        break;
      }
      case "GraphNodeUpserted": {
        const node = {
          node_id: payload.node_id ?? ev.entity_key,
          kind: payload.kind ?? "node",
          attrs: payload.attrs ?? {},
          ref_id: payload.ref_id ?? null,
          created_at: payload.created_at ?? ev.ts,
        };
        this.graph.nodes.set(node.node_id, node);
        if (node.kind === "artifact" && typeof node.attrs.artifact_id === "string") {
          const id = node.attrs.artifact_id;
          this.artifacts.set(id, {...this.artifacts.get(id), ...node.attrs, id,
            name: node.attrs.title ?? this.artifacts.get(id)?.name, node_id: node.node_id, registered_in_journal: true});
        }

        break;
      }
      case "ArtifactRegistered": {
        const old = this.artifacts.get(payload.artifact_id) ?? {};
        const fields = Object.fromEntries(Object.entries(payload).filter(([, v]) => v != null));
        this.artifacts.set(payload.artifact_id, {...old, ...fields, id: payload.artifact_id, registered_in_journal: true});
        break;
      }
      case "GraphEdgeAdded": {
        this.graph.edges.set(payload.edge_id, {
          edge_id: payload.edge_id,
          kind: payload.kind ?? "relates",
          src_id: payload.src_id,
          dst_id: payload.dst_id,
        });
        break;
      }
      case "ResultRecorded": {
        // token tallies live on the run summary server-side; the journal keeps
        // per-result rows — the console shows counts, not estimates.
        this.usage.record_results = (this.usage.record_results ?? 0) + 1;
        this.results.set(payload.result_id, {...payload, seq: ev.seq, recorded_at: ev.ts});
        break;
      }
      default:
        break;
    }
  }

  taskRows() {
    // plan tasks first (intake stages last), stable by id within groups
    const rows = [...this.tasks.values()];
    const rank = (t) => (/^[A-Z]+$/.test(t.key) ? 1 : 0);
    rows.sort((a, b) => rank(a) - rank(b) || a.id.localeCompare(b.id));
    return rows;
  }

  docRows() {
    // human-facing documents first (report/tz/nir by name), then artifacts
    const rows = [...this.artifacts.values()];
    rows.sort((a, b) => docRank(a) - docRank(b) || a.id.localeCompare(b.id));
    return rows;
  }

  resultRows(taskId = "") {
    return [...this.results.values()].filter(row => !taskId || row.task_id === taskId)
      .sort((a, b) => b.seq - a.seq);
  }

  attemptRows(taskId = "") {
    const rows = new Map([...this.attempts].map(([id, attempt]) => [id, {
      attempt_id: id, task_id: attempt.task_id, attempt, results: [], seq: attempt.seq,
    }]));
    for (const result of this.results.values()) {
      let row = rows.get(result.attempt_id);
      if (!row) {
        row = {attempt_id: result.attempt_id, task_id: result.task_id, attempt: null, results: [], seq: result.seq};
        rows.set(result.attempt_id, row);
      }
      row.results.push(result);
      row.seq = Math.max(row.seq, result.seq);
    }
    return [...rows.values()].filter(row => !taskId || row.task_id === taskId)
      .sort((a, b) => b.seq - a.seq);
  }

  mergeArtifactSnapshot(rows) {
    for (const row of rows) {
      const id = row.artifact_id;
      const old = this.artifacts.get(id) ?? {};
      const registered = old.registered_in_journal === true || row.registered_in_journal === true;
      this.artifacts.set(id, {...old, ...row, id, registered_in_journal: registered});
    }
    this.onUpdate?.();
  }

  openTasks() {
    return this.taskRows().filter((t) => t.outcome === null || TASK_OPEN.has(t.state));
  }
}

function docRank(a) {
  const name = String(a.media_type ?? "");
  if (name.includes("markdown") || name.includes("text")) return 0;
  if (name === "application/json") return 1;
  return 2;
}
