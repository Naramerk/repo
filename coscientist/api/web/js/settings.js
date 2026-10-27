/** Actual revisioned preference controls; choices come only from the server catalog. */
const element = (tag, text) => {
  const value = document.createElement(tag); if (text != null) value.textContent = String(text); return value;
};
async function json(response) {
  const body = await response.json();
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body;
}
export class SettingsPanel {
  constructor(host, profile) { this.host = host; this.profile = profile; this.generation = 0; }
  stop() { this.generation += 1; }
  async load(section) {
    const generation = ++this.generation;
    this.host.textContent = "Загрузка каталога и настроек…";
    try {
      const [catalog, saved] = await Promise.all([
        fetch("/console/settings-catalog").then(json),
        this.profile ? fetch(`/console/profiles/${encodeURIComponent(this.profile.id)}/settings`).then(json) : Promise.resolve(null),
      ]);
      if (generation !== this.generation) return;
      this.render(catalog, saved, section);
    } catch (error) {
      if (generation !== this.generation) return;
      const retry = element("button", "Обновить каталог и настройки"); retry.type = "button";
      retry.addEventListener("click", () => this.load(section));
      this.host.replaceChildren(element("p", `Настройки недоступны: ${error.message}`), retry);
    }
  }
  render(catalog, saved, section) {
    const generation = this.generation;
    const values = JSON.parse(JSON.stringify(saved?.settings ?? catalog.defaults));
    const form = element("form"); form.className = "settings-form";
    const message = element("p"); message.setAttribute("role", "status");
    form.append(element("p", this.profile ? `Локальный профиль: ${this.profile.nickname} · сохранённая ревизия ${saved.revision}` : "Каталог сервера. Выберите или создайте локальный профиль, чтобы сохранить персональные параметры."),
      element("p", "Локальные настройки не являются правами доступа. Они не изменяют уже запущенные исследования."));
    if (saved && saved.revision === 0) form.append(element("p", "Персональные настройки ещё не сохранены. Показаны значения из доверенного каталога сервера."));
    if (saved?.catalog_digest && saved.catalog_digest !== catalog.digest) {
      form.append(element("p", "Каталог сервера изменился. Проверьте выбор перед сохранением новой ревизии."));
    }
    const roleControls = new Map();
    const servers = new Map(), tools = new Map();
    const roleSection = element("fieldset"); roleSection.append(element("legend", "Модели по ролям (настройка сервера)")); roleSection.hidden = section !== "agents";
    for (const role of catalog.roles) {
      const label = element("label", role.label + " "), select = element("select");
      for (const choice of role.choices) {
        const option = element("option", `${choice.label} · ${choice.model}`); option.value = choice.id; select.append(option);
      }
      if (!role.choices.some(choice => choice.id === values.role_choices[role.id])) {
        const missing = element("option", "Сохранённый выбор недоступен — выберите модель"); missing.value = ""; select.append(missing);
        select.value = "";
      } else select.value = values.role_choices[role.id];
      select.required = section === "agents"; label.append(select); roleSection.append(label); roleControls.set(role.id, select);
    }
    const toolSection = element("fieldset"); toolSection.append(element("legend", "Серверы MCP и разрешённые инструменты")); toolSection.hidden = section !== "mcp";
    const checkbox = (labelText, checked) => {
      const label = element("label"), input = element("input"); input.type = "checkbox"; input.checked = checked;
      label.append(input, element("span", labelText)); return {label, input};
    };
    for (const server of catalog.mcp_servers) {
      const group = element("fieldset"), enabled = checkbox(server.label, values.enabled_server_ids.includes(server.id));
      group.append(enabled.label); servers.set(server.id, enabled.input);
      const childInputs = [];
      for (const tool of server.tools) {
        const control = checkbox(tool.label, values.enabled_tool_ids.includes(tool.id));
        control.input.disabled = !enabled.input.checked; tools.set(tool.id, control.input); childInputs.push(control.input); group.append(control.label);
      }
      enabled.input.addEventListener("change", () => {
        for (const input of childInputs) { input.disabled = !enabled.input.checked; if (!enabled.input.checked) input.checked = false; }
      });
      toolSection.append(group);
    }
    const coder = checkbox("Исполнитель кода", values.coder_enabled); coder.input.disabled = !catalog.coder.available;
    if (!catalog.coder.available) coder.label.append(element("span", " · не настроен на сервере"));
    toolSection.append(coder.label);
    const coderInputs = [];
    for (const id of catalog.coder.tool_ids) {
      const control = checkbox(id, values.enabled_tool_ids.includes(id)); control.input.disabled = !coder.input.checked;
      tools.set(id, control.input); coderInputs.push(control.input); toolSection.append(control.label);
    }
    coder.input.addEventListener("change", () => {
      for (const input of coderInputs) { input.disabled = !coder.input.checked; if (!coder.input.checked) input.checked = false; }
    });
    const save = element("button", "Сохранить настройки профиля"); save.type = "submit";
    save.disabled = !this.profile;
    form.addEventListener("submit", async event => {
      event.preventDefault(); if (!this.profile) return;
      if ([...roleControls.values()].some(control => !control.value)) {
        message.textContent = "Выберите доступные модели в разделе «Конфигурация MAS» перед сохранением."; return;
      }
      const settings = {
        role_choices: Object.fromEntries([...roleControls].map(([id, control]) => [id, control.value])),
        enabled_server_ids: [...servers].filter(([, control]) => control.checked).map(([id]) => id),
        enabled_tool_ids: [...tools].filter(([, control]) => control.checked && !control.disabled).map(([id]) => id),
        coder_enabled: coder.input.checked && catalog.coder.available,
      };
      save.disabled = true;
      try {
        const result = await json(await fetch(`/console/profiles/${encodeURIComponent(this.profile.id)}/settings`, {
          method: "PUT", headers: {"Content-Type": "application/json"},
          body: JSON.stringify({expected_revision: saved.revision, catalog_digest: catalog.digest, settings}),
        }));
        if (generation !== this.generation) return;
        saved = result; message.textContent = `Настройки сохранены · ревизия ${result.revision}. Уже запущенные исследования не изменены.`;
      } catch (error) { if (generation === this.generation) message.textContent = `Настройки не сохранены: ${error.message}. Обновите каталог и настройки перед повтором.`; }
      finally { if (generation === this.generation) save.disabled = false; }
    });
    const refresh = element("button", "Обновить каталог и настройки"); refresh.type = "button";
    refresh.addEventListener("click", () => this.load(section));
    form.append(roleSection, toolSection, save, refresh, message); this.host.replaceChildren(form);
  }
}
