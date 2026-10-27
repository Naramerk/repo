/** Read-only projections of recorded calls, LIT results and CAS code metadata. */
const node = (tag, text, cls) => {
  const value = document.createElement(tag);
  if (text != null) value.textContent = String(text);
  if (cls) value.className = cls;
  return value;
};
const codeMedia = new Set(["text/x-python", "application/x-python", "text/javascript", "application/javascript", "text/x-shellscript", "text/x-r-source", "application/x-ipynb+json"]);

export function observedCalls(model) {
  const calls = new Map();
  for (const event of model.events) {
    const p = event.payload;
    if (event.type === "ToolCalled" && p.ref?.call_id) {
      const old = calls.get(p.ref.call_id) ?? {};
      calls.set(p.ref.call_id, {...old, id: p.ref.call_id, tool: p.ref.tool, ref: p.ref,
        outcome: p.outcome, error_kind: p.error_kind, duration_ms: p.duration_ms, seq: event.seq});
    } else if (["McpRequestRecorded", "McpResponseRecorded"].includes(event.type) && p.call_id) {
      const old = calls.get(p.call_id) ?? {};
      calls.set(p.call_id, {...old, id: p.call_id, tool: old.tool ?? p.tool, seq: event.seq,
        [event.type === "McpRequestRecorded" ? "request" : "response"]: p});
    }
  }
  return [...calls.values()].sort((a, b) => b.seq - a.seq);
}

export function literatureRecords(model) {
  const tasks = [...model.tasks.values()].filter(task => task.key === "LIT");
  const ids = new Set(tasks.map(task => task.id));
  const results = [...model.results.values()].filter(result => ids.has(result.task_id));
  const artifacts = [...new Set(results.flatMap(result => [...(result.artifacts ?? []), ...Object.values(result.output_map ?? {})]))];
  return {tasks, results, artifacts};
}

export function codeArtifacts(model) {
  return [...model.artifacts.values()].filter(artifact => codeMedia.has(artifact.media_type)
    || /\.(py|js|mjs|ts|tsx|sh|r|ipynb)$/i.test(artifact.name ?? ""));
}

export class ObservationView {
  constructor(host, openArtifact) { this.host = host; this.openArtifact = openArtifact; this.signature = null; this.model = null; this.opened = new Set(); }
  artifact(id, model) {
    const meta = model.artifacts.get(id);
    const button = node("button", meta?.name || id, "artifact-link"); button.type = "button";
    button.addEventListener("click", () => this.openArtifact(id, ""));
    return button;
  }
  render(model, kind) {
    const projection = kind === "tools" ? observedCalls(model) : kind === "literature" ? literatureRecords(model) : {
      artifacts: codeArtifacts(model), infra: model.events.filter(event => event.type === "SandboxInfraRaised")};
    const signature = JSON.stringify([kind, projection, kind === "literature" ? projection.artifacts.map(id => model.artifacts.get(id)) : null]);
    if (this.model === model && signature === this.signature) return;
    if (this.model !== model) this.opened.clear();
    this.model = model; this.signature = signature;
    const children = [];
    if (kind === "tools") {
      const calls = projection;
      const recorded = calls.filter(call => call.outcome != null);
      children.push(node("p", `${calls.length} записей вызовов · ${recorded.filter(call => call.outcome === "error").length} ошибок ToolCalled · ${calls.filter(call => call.request).length} MCP-запросов`, "inspection-summary"));
      children.push(node("p", "Счётчики относятся только к событиям журнала. Отсутствие ответа не означает ошибку или активное выполнение.", "muted"));
      if (!calls.length) children.push(node("p", "События вызовов инструментов и MCP-обмена в журнале отсутствуют."));
      if (calls.length > 200) children.push(node("p", "Ниже показаны последние 200 записей; полный журнал доступен в трассировке сессии.", "muted"));
      for (const call of calls.slice(0, 200)) {
        const details = node("details", null, "inspection-record"); details.open = this.opened.has(call.id);
        const status = call.outcome === "ok" ? "завершён" : call.outcome === "error" ? "ошибка" : call.response ? (call.response.is_error ? "MCP ответ с ошибкой" : "MCP ответ записан") : "итог не записан";
        details.append(node("summary", `${call.tool} · ${status}${call.duration_ms != null ? ` · ${call.duration_ms} мс` : ""}`),
          node("pre", JSON.stringify(call, null, 2)));
        details.addEventListener("toggle", () => { if (details.open) this.opened.add(call.id); else this.opened.delete(call.id); });
        children.push(details);
      }
    } else if (kind === "literature") {
      const {tasks, results, artifacts} = projection;
      children.push(node("p", `${tasks.length} этапов LIT · ${results.length} записей результата · ${artifacts.length} связанных артефактов`, "inspection-summary"),
        node("p", "Это учёт записанных результатов и материалов. Число статей, цитирований и научная пригодность не выводятся из количества файлов.", "muted"));
      if (!tasks.length) children.push(node("p", "Этап анализа литературы в журнале не зарегистрирован."));
      for (const task of tasks) children.push(node("p", `${task.title || task.key}: ${task.outcome ?? task.state ?? "состояние не записано"}`));
      for (const id of artifacts) children.push(this.artifact(id, model));
      for (const result of results) {
        const record = node("details"); record.append(node("summary", `Результат ${result.result_id}`), node("pre", JSON.stringify(result, null, 2))); children.push(record);
      }
    } else {
      children.push(node("p", "Рабочие материалы кода из CAS. Открытие показывает сохранённый файл; выполнение и системные файлы этим представлением не управляются.", "muted"));
      if (!projection.artifacts.length) children.push(node("p", "Артефакты с метаданными исходного кода в CAS не зарегистрированы."));
      for (const artifact of projection.artifacts) {
        const row = node("section", null, "inspection-record");
        row.append(this.artifact(artifact.id, model), node("p", `${artifact.media_type ?? "тип не записан"} · ${artifact.registered_in_journal ? "ссылка в журнале" : "метаданные CAS"}`, "muted"));
        children.push(row);
      }
      for (const event of projection.infra) children.push(node("pre", JSON.stringify({seq: event.seq, type: event.type, payload: event.payload}, null, 2)));
    }
    this.host.replaceChildren(...children);
  }
}
