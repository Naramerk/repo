/** Local organization only: every journal remains accessible without authentication. */
const storageKey = "cosnext.local-profile";
function element(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}
async function responseJSON(response) {
  const value = await response.json();
  if (!response.ok) throw new Error(value.detail || `HTTP ${response.status}`);
  return value;
}

export class LocalProfilePanel {
  constructor(host, onChange) {
    this.host = host;
    this.onChange = onChange;
    this.selectedId = localStorage.getItem(storageKey) || null;
    this.profiles = [];
    this.studyId = null;
    this.generation = 0;
    this.selectionVersion = 0;
  }
  get current() {
    if (!this.selectedId) return null;
    return this.profiles.find(profile => profile.id === this.selectedId)
      || {id: this.selectedId, nickname: "Недоступный локальный профиль"};
  }
  studiesURL() {
    return this.selectedId ? `/console/profiles/${encodeURIComponent(this.selectedId)}/studies` : "/studies";
  }
  select(id) {
    this.selectionVersion += 1;
    this.selectedId = id || null;
    if (this.selectedId) localStorage.setItem(storageKey, this.selectedId); else localStorage.removeItem(storageKey);
    if (this.selectNode) this.selectNode.value = this.selectedId || "";
    this.updateAttachment();
    this.onChange(this.current);
  }
  setStudyId(studyId) { this.studyId = studyId; this.updateAttachment(); }
  async load() {
    const generation = ++this.generation;
    try {
      const body = await responseJSON(await fetch("/console/profiles"));
      if (generation !== this.generation) return;
      this.profiles = body.profiles;
      this.render();
      this.onChange(this.current);
    } catch (error) {
      if (generation === this.generation) {
        this.host.textContent = `Локальные профили недоступны: ${error.message}. Исследования доступны по прямым ссылкам.`;
      }
    }
  }
  updateAttachment() {
    if (!this.attachButton) return;
    this.attachButton.hidden = !this.studyId;
    this.attachButton.disabled = !this.current || !this.profiles.some(profile => profile.id === this.selectedId);
    this.attachButton.textContent = this.current
      ? `Добавить открытое исследование в «${this.current.nickname}»` : "Выберите локальный профиль для добавления исследования";
  }
  render() {
    const label = element("label", "Локальный профиль ");
    const select = element("select");
    const all = element("option", "Все исследования · новый запуск без профиля");
    all.value = ""; select.append(all);
    for (const profile of this.profiles) {
      const option = element("option", profile.nickname); option.value = profile.id; select.append(option);
    }
    if (this.selectedId && !this.profiles.some(profile => profile.id === this.selectedId)) {
      const missing = element("option", "Сохранённый профиль недоступен — выберите другой");
      missing.value = this.selectedId; select.append(missing);
    }
    select.value = this.selectedId || "";
    select.addEventListener("change", () => this.select(select.value));
    this.selectNode = select;
    label.append(select);
    const note = element("p", "Никнеймы группируют исследования на этом компьютере. Это не вход в аккаунт: все исследования остаются доступны.");
    note.className = "muted";
    const details = element("details");
    details.append(element("summary", "Создать локальный профиль"));
    const form = element("form"), nameLabel = element("label", "Никнейм "), nickname = element("input");
    nickname.required = true; nickname.maxLength = 64; nickname.autocomplete = "off";
    nameLabel.append(nickname);
    const submit = element("button", "Создать профиль"); submit.type = "submit";
    const message = element("p"); message.setAttribute("role", "status"); this.message = message;
    form.addEventListener("submit", async event => {
      event.preventDefault(); submit.disabled = true;
      const selectionVersion = this.selectionVersion;
      try {
        const profile = await responseJSON(await fetch("/console/profiles", {
          method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({nickname: nickname.value}),
        }));
        if (selectionVersion === this.selectionVersion) {
          this.selectedId = profile.id; localStorage.setItem(storageKey, profile.id);
        }
        await this.load();
      } catch (error) { message.textContent = `Профиль не создан: ${error.message}. Уже созданный никнейм можно выбрать в списке.`; }
      finally { submit.disabled = false; }
    });
    form.append(nameLabel, submit); details.append(form);
    const attach = element("button"); attach.type = "button"; this.attachButton = attach;
    attach.addEventListener("click", async () => {
      const profile = this.current, studyId = this.studyId;
      if (!profile || !studyId) return;
      const generation = this.generation;
      attach.disabled = true;
      try {
        await responseJSON(await fetch(`/console/profiles/${encodeURIComponent(profile.id)}/studies`, {
          method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({study_id: studyId}),
        }));
        if (generation === this.generation && this.studyId === studyId && this.selectedId === profile.id) {
          message.textContent = `Исследование добавлено в «${profile.nickname}».`;
        }
      } catch (error) {
        if (generation === this.generation && this.studyId === studyId && this.selectedId === profile.id) {
          message.textContent = `Исследование не добавлено: ${error.message}`;
        }
      } finally { if (generation === this.generation) this.updateAttachment(); }
    });
    this.host.replaceChildren(label, note, details, attach, message);
    this.updateAttachment();
  }
}
