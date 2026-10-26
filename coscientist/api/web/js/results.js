/** Read-only research records. Display recorded checks; never infer scientific success. */
const STATUS = {MET: "выполнено", FAILED: "не выполнено", PENDING: "ожидается", UNASSESSED: "не оценено"};
const ROLE = {validity: "корректность", prediction: "проверка предсказания"};

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}

function artifactButton(id, label, pointer, openArtifact) {
  const button = element("button", label, "artifact-link");
  button.type = "button";
  button.title = id;
  button.addEventListener("click", () => openArtifact(id, pointer));
  return button;
}

export class ResultsView {
  constructor(host, openArtifact) {
    this.host = host;
    this.openArtifact = openArtifact;
    this.model = null;
    this.taskId = "";
    this.openRecords = new Set();
  }

  render(model) {
    if (this.model !== model) {
      this.model = model;
      this.taskId = "";
      this.openRecords.clear();
    }
    const toolbar = element("div", null, "results-toolbar");
    const label = element("label", "Задача ");
    const select = element("select");
    select.setAttribute("aria-label", "Результаты задачи");
    const all = element("option", "Все задачи"); all.value = ""; select.append(all);
    const taskIds = new Set(model.attemptRows().map(row => row.task_id));
    for (const id of taskIds) {
      const task = model.tasks.get(id);
      const option = element("option", task?.title || task?.key || id);
      option.value = id; select.append(option);
    }
    select.value = this.taskId;
    select.addEventListener("change", () => { this.taskId = select.value; this.render(model); });
    label.append(select);
    const rows = model.attemptRows(this.taskId);
    const count = rows.reduce((total, row) => total + row.results.length, 0);
    toolbar.append(label, element("span", `${rows.length} попыток · ${count} записей результата`, "muted"));
    const records = element("div", null, "result-records");
    if (!rows.length) records.append(element("p", "Попытки ещё не начаты.", "muted"));
    for (const row of rows) {
      const group = element("section", null, "attempt-record");
      const attempt = row.attempt;
      group.append(element("h2", row.attempt_id));
      if (attempt) {
        group.append(element("p", `${attempt.closed ? "Попытка закрыта" : "Попытка открыта"} · ${attempt.started_at ?? ""} ${attempt.closed_at ?? ""}`, "record-meta"));
        if (attempt.close_reason) group.append(element("p", `Причина завершения попытки: ${attempt.close_reason}`, "muted"));
      } else group.append(element("p", "События начала и завершения попытки отсутствуют.", "muted"));
      if (!row.results.length) {
        const task = model.tasks.get(row.task_id);
        group.append(element("h3", task?.title || task?.key || row.task_id),
          element("p", "Результат не записан. Вычисленный итог и проверки отсутствуют.", "muted"));
      }
      for (const result of row.results) group.append(this.record(result, model));
      records.append(group);
    }
    this.host.replaceChildren(toolbar, records);
  }

  record(row, model) {
    const card = element("article", null, "result-record");
    const task = model.tasks.get(row.task_id);
    const head = element("header", null, "result-heading");
    head.append(element("h2", task?.title || task?.key || row.task_id),
      element("span", row.computed_outcome, `chip result-${row.computed_outcome}`));
    card.append(head);
    if (task?.spec.goal) card.append(element("p", task.spec.goal));
    card.append(element("p", `${row.attempt_id} · ${row.recorded_at ?? ""} · источник записи: ${row.provenance}`, "record-meta"));
    if (row.agent_claim != null && row.agent_claim !== row.computed_outcome) {
      card.append(element("p", `Заявление исполнителя: ${row.agent_claim}. Вычисленный итог: ${row.computed_outcome}.`, "record-discrepancy"));
    }
    if (row.reason || row.detail) card.append(element("p", [row.reason, row.detail].filter(Boolean).join(" · ")));
    if (row.check_results?.length) card.append(this.checks(row.check_results));
    else card.append(element("p", "В этой записи нет результатов проверок.", "muted"));
    if (row.unassessed?.length) card.append(element("p", `Не оценено: ${row.unassessed.join(", ")}`, "record-discrepancy"));
    const outputs = element("div", null, "record-artifacts");
    const named = new Map(Object.entries(row.output_map ?? {}).map(([name, id]) => [id, name]));
    for (const id of new Set([...(row.artifacts ?? []), ...named.keys()])) {
      const meta = model.artifacts.get(id);
      outputs.append(artifactButton(id, named.get(id) || meta?.name || id, "", this.openArtifact));
    }
    if (outputs.childNodes.length) card.append(element("h3", "Исходные артефакты"), outputs);
    const raw = element("details");
    raw.open = this.openRecords.has(row.result_id);
    raw.append(element("summary", "Полная запись и происхождение"), element("pre", JSON.stringify(row, null, 2)));
    raw.addEventListener("toggle", () => {
      if (raw.open) this.openRecords.add(row.result_id); else this.openRecords.delete(row.result_id);
    });
    card.append(raw);
    return card;
  }

  checks(rows) {
    const table = element("table", null, "check-results");
    const header = element("tr");
    for (const label of ["Проверка", "Итог", "Роль", "Доказательства"]) header.append(element("th", label));
    const thead = element("thead"); thead.append(header); table.append(thead);
    const body = element("tbody");
    for (const check of rows) {
      const tr = element("tr");
      const name = element("td", check.template_id);
      name.append(element("small", `${check.required ? "обязательная" : "необязательная"} · ${check.is_code ? "код" : "ревью"}`));
      if (check.detail) name.append(element("p", check.detail));
      const status = element("td", STATUS[check.status] ?? check.status, `check-status-${check.status}`);
      const role = element("td", ROLE[check.role] ?? check.role);
      const refs = element("td");
      for (const ref of check.evidence ?? []) {
        const label = ref.json_pointer ? `JSON ${ref.json_pointer}` : "Исходные данные";
        refs.append(artifactButton(ref.artifact_id, label, ref.json_pointer ?? "", this.openArtifact));
        if (ref.digest) refs.append(element("small", `digest ссылки: ${ref.digest}`));
      }
      if (!refs.childNodes.length) refs.textContent = "ссылки не записаны";
      tr.append(name, status, role, refs); body.append(tr);
    }
    table.append(body);
    return table;
  }
}

export function valueAtPointer(value, pointer) {
  if (pointer === "") return {found: true, value};
  if (!pointer.startsWith("/")) return {found: false};
  for (const encoded of pointer.slice(1).split("/")) {
    if (/~(?![01])/u.test(encoded)) return {found: false};
    const key = encoded.replace(/~1/g, "/").replace(/~0/g, "~");
    if (value == null || typeof value !== "object" || !Object.hasOwn(value, key)) return {found: false};
    if (Array.isArray(value) && !/^(0|[1-9][0-9]*)$/u.test(key)) return {found: false};
    value = value[key];
  }
  return {found: true, value};
}

export function jsonEvidence(text, pointer) {
  // Select source spans, never convert scientific numeric tokens to JS doubles.
  if (text.length > 2 * 1024 * 1024) return {found: false, reason: "parse_limit"};
  if (pointer !== "" && !pointer.startsWith("/")) return {found: false, reason: "pointer_missing"};
  const keys = pointer === "" ? [] : pointer.slice(1).split("/");
  if (keys.some(key => /~(?![01])/u.test(key))) return {found: false, reason: "pointer_missing"};
  const target = keys.map(key => key.replace(/~1/g, "/").replace(/~0/g, "~"));
  let cursor = 0, nodes = 0, selected;
  const whitespace = () => { while (/[\x20\t\r\n]/u.test(text[cursor] ?? "")) cursor++; };
  const invalid = () => { throw new SyntaxError("invalid or ambiguous JSON"); };
  const string = () => {
    const start = cursor++;
    while (cursor < text.length) {
      const char = text[cursor++];
      if (char === '"') return JSON.parse(text.slice(start, cursor));
      if (char === "\\") cursor++;
    }
    return invalid();
  };
  const parse = (path, depth) => {
    if (depth > 64 || ++nodes > 100000) throw new RangeError("JSON parse budget");
    whitespace();
    const start = cursor, char = text[cursor];
    if (char === "{") {
      cursor++; whitespace();
      const names = new Set();
      if (text[cursor] !== "}") {
        while (true) {
          if (text[cursor] !== '"') invalid();
          const key = string();
          if (names.has(key)) invalid();
          names.add(key); whitespace();
          if (text[cursor++] !== ":") invalid();
          parse([...path, key], depth + 1); whitespace();
          if (text[cursor] === "}") break;
          if (text[cursor++] !== ",") invalid();
          whitespace();
        }
      }
      cursor++;
    } else if (char === "[") {
      cursor++; whitespace();
      let index = 0;
      if (text[cursor] !== "]") {
        while (true) {
          parse([...path, String(index++)], depth + 1); whitespace();
          if (text[cursor] === "]") break;
          if (text[cursor++] !== ",") invalid();
          whitespace();
        }
      }
      cursor++;
    } else if (char === '"') {
      string();
    } else {
      const token = /^(?:true|false|null|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)/u.exec(text.slice(cursor));
      if (!token) invalid();
      if (/^-?[0-9]/u.test(token[0]) && !Number.isFinite(Number(token[0]))) invalid();
      cursor += token[0].length;
    }
    if (path.length === target.length && path.every((key, index) => key === target[index])) {
      selected = text.slice(start, cursor);
    }
  };
  try {
    parse([], 0); whitespace();
    if (cursor !== text.length) invalid();
  } catch (error) {
    if (error instanceof RangeError) return {found: false, reason: "parse_limit"};
    if (!(error instanceof SyntaxError)) throw error;
    return {found: false, reason: "invalid_json"};
  }
  return selected === undefined ? {found: false, reason: "pointer_missing"} : {found: true, source: selected};
}
