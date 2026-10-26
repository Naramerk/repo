/** User launch form and owner status; advanced configuration stays on the server. */
const draftKey = "cosnext.pending-launch";

function element(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}

async function responseJSON(response) {
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body;
}

export class PresetForm {
  constructor(host, onLaunch) {
    this.host = host;
    this.onLaunch = onLaunch;
    this.stopped = false;
  }
  stop() { this.stopped = true; }
  async load() {
    try {
      const metadata = await responseJSON(await fetch("/console/run-presets"));
      if (!this.stopped) this.render(metadata.presets);
    } catch (error) {
      if (!this.stopped) this.host.textContent = `Профили запуска недоступны: ${error.message}`;
    }
  }
  render(presets) {
    const title = element("h2", "Новое исследование");
    if (!presets.length) {
      this.host.replaceChildren(title, element("p", "Запуск из браузера доступен после настройки профиля на сервере."));
      return;
    }
    const form = element("form");
    const label = element("label", "Профиль исследования");
    const select = element("select");
    for (const preset of presets) {
      const option = element("option", preset.label);
      option.value = preset.id;
      select.append(option);
    }
    label.append(select);
    const budget = element("p");
    budget.className = "muted";
    const updateBudget = () => {
      const preset = presets.find(item => item.id === select.value);
      budget.textContent = `${preset.description} · модель плана ${preset.models.planner} · модель исполнения ${preset.models.executor} · срок ${preset.deadline_s} с · не более ${preset.max_llm_calls} запросов, ${preset.max_tokens} токенов и ${preset.max_tool_calls} вызовов инструментов`;
    };
    const taskLabel = element("label", "Задача исследования");
    const task = element("textarea");
    task.required = true;
    task.maxLength = 100000;
    task.rows = 5;
    taskLabel.append(task);
    const message = element("p");
    message.setAttribute("role", "status");
    let key = crypto.randomUUID();
    const saved = sessionStorage.getItem(draftKey);
    if (saved) {
      try {
        const draft = JSON.parse(saved);
        if (presets.some(preset => preset.id === draft.preset_id)) {
          task.value = draft.task_text;
          select.value = draft.preset_id;
          key = draft.idempotency_key;
          message.textContent = "Сохранён неподтверждённый запрос. Повторная отправка проверит тот же запуск.";
        }
      } catch (error) {
        message.textContent = "Сохранённый запрос повреждён. Введите задачу заново.";
      }
    }
    const edited = () => { key = crypto.randomUUID(); sessionStorage.removeItem(draftKey); };
    task.addEventListener("input", edited);
    select.addEventListener("change", () => { edited(); updateBudget(); });
    updateBudget();
    const launch = element("button", "Начать исследование");
    launch.type = "submit";
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (!task.value.trim()) { message.textContent = "Опишите задачу исследования."; return; }
      const body = {preset_id: select.value, task_text: task.value, idempotency_key: key};
      sessionStorage.setItem(draftKey, JSON.stringify(body));
      launch.disabled = true; task.disabled = true; select.disabled = true;
      message.textContent = "Запуск исследования…";
      try {
        const result = await responseJSON(await fetch("/console/runs", {
          method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body),
        }));
        sessionStorage.removeItem(draftKey);
        if (!this.stopped) this.onLaunch(result);
      } catch (error) {
        if (!this.stopped) message.textContent = `Запуск не подтверждён: ${error.message}. Можно повторить тот же запрос.`;
      } finally {
        launch.disabled = false; task.disabled = false; select.disabled = false;
      }
    });
    form.append(label, budget, taskLabel, launch, message);
    this.host.replaceChildren(title, form);
  }
}

export class RunControls {
  constructor(host, studyId, onActiveStudy) {
    this.host = host;
    this.studyId = studyId;
    this.onActiveStudy = onActiveStudy;
    this.generation = 0;
    this.stopped = false;
    this.timer = null;
  }
  stop() { this.stopped = true; this.generation += 1; clearTimeout(this.timer); }
  async start() {
    try {
      const metadata = await responseJSON(await fetch("/console/run-presets"));
      if (!Number.isFinite(metadata.poll_interval_ms) || metadata.poll_interval_ms <= 0) throw new Error("Некорректный интервал статуса");
      this.pollMs = Math.max(250, metadata.poll_interval_ms);
      if (!this.stopped) await this.refresh();
    } catch (error) {
      if (!this.stopped) this.host.textContent = `Статус запуска недоступен: ${error.message}`;
    }
  }
  async refresh() {
    if (this.stopped) return;
    clearTimeout(this.timer);
    const generation = ++this.generation;
    try {
      const status = await responseJSON(await fetch(`/console/runs/${encodeURIComponent(this.studyId)}`));
      if (this.stopped || generation !== this.generation) return;
      this.render(status);
      if (status.active_study_id !== this.studyId) this.onActiveStudy(status.active_study_id);
      if (status.run_status === "running") this.timer = setTimeout(() => this.refresh(), this.pollMs);
    } catch (error) {
      if (!this.stopped && generation === this.generation) this.host.textContent = `Статус запуска недоступен: ${error.message}`;
    }
  }
  render(status) {
    const signature = JSON.stringify(status);
    if (signature === this.lastStatus) return;
    this.lastStatus = signature;
    const names = {running:"Запуск работает", done:"Запуск завершён", cancelled:"Запуск остановлен", refused:"Запуск отклонён", failed:"Ошибка запуска", recorded_terminal:"Результат из журнала", owner_unavailable:"Владелец запуска недоступен; автоматическое продолжение отключено"};
    const text = element("span", `${names[status.run_status] || status.run_status}${status.verdict ? ` · ${status.verdict}` : ""}`);
    this.host.replaceChildren(text);
    if (status.journal_finalized === false && status.run_status !== "running") {
      this.host.append(element("span", "Исследование не завершено; окончательный итог отсутствует."));
    }
    if (status.can_cancel) {
      const button = element("button", "Остановить исследование");
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await responseJSON(await fetch(`/studies/${encodeURIComponent(status.study_id)}/cancel`, {
            method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({by:"console-human"}),
          }));
          if (!this.stopped) await this.refresh();
        } catch (error) {
          if (!this.stopped) { text.textContent = `Остановка не подтверждена: ${error.message}`; button.disabled = false; }
        }
      });
      this.host.append(button);
    }
  }
}
