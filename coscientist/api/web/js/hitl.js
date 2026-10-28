/** Human decisions use the owner's inbox; only durable journal answers count. */
export class HitlPanel {
  constructor(host, studyId) {
    this.host = host;
    this.studyId = studyId;
    this.generation = 0;
    this.message = "";
    this.stopped = false;
  }

  stop() { this.stopped = true; this.generation += 1; }

  async refresh() {
    if (this.stopped) return;
    const generation = ++this.generation;
    try {
      const response = await fetch(`/console/studies/${encodeURIComponent(this.studyId)}/hitl`);
      if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
      const rows = await response.json();
      if (generation !== this.generation) return;
      this.render(rows);
    } catch (error) {
      if (generation === this.generation) this.host.textContent = `HITL: ${error.message}`;
    }
  }

  render(rows) {
    const message = document.createElement("p");
    message.setAttribute("role", "status");
    message.textContent = this.message || (rows.some(row => row.status === "pending")
      ? "Исследование ожидает решения человека." : "Открытых запросов к человеку нет.");
    const refresh = document.createElement("button");
    refresh.textContent = "Обновить запросы";
    refresh.addEventListener("click", () => this.refresh());
    this.host.replaceChildren(message, refresh);
    for (const row of rows) {
      const card = document.createElement("section");
      card.className = "hitl-card";
      const heading = document.createElement("h3");
      heading.textContent = row.prompt?.question || `Запрос ${row.kind} (вопрос не записан)`;
      const status = document.createElement("p");
      const statuses = {pending: "Ожидает ответа", answered: "Решение записано", timed_out: "Время ожидания истекло", study_closed: "Исследование закрыто"};
      status.textContent = `${statuses[row.status] || row.status} · срок ${row.deadline || "не указан"}`;
      const context = document.createElement("pre");
      if (row.prompt) {
        context.textContent = `${row.prompt.context_json}\n\nSHA-256: ${row.prompt.context_sha256}\nПопытка: ${row.prompt.attempt_id}`;
      } else context.textContent = JSON.stringify(row, null, 2);
      const record = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = "Точная запись запроса и проверка целостности";
      record.append(summary, context);
      card.append(heading, status);
      if (row.prompt) card.append(planSummary(row.prompt.context_json));
      card.append(record);
      if (row.answer) {
        const answer = document.createElement("p");
        const feedback = row.answer.feedback ? ` · замечания: ${row.answer.feedback}` : "";
        answer.textContent = `В журнале: ${row.answer.decision || row.answer.reason}${feedback}`;
        card.append(answer);
      }
      if (row.status === "pending") {
        const actions = document.createElement("div");
        const feedback = document.createElement("textarea");
        feedback.maxLength = 4000;
        feedback.placeholder = "Что не так (обязательно для возврата, до 4000 символов)";
        // HITL v2: free-text revision exists on the stage gates; the plan
        // gate keeps its accept/reject contract (the API refuses revise there).
        const revisable = ["spec_approval", "hypotheses", "literature", "report"].includes(row.kind);
        for (const decision of row.prompt?.options || []) {
          const button = document.createElement("button");
          button.textContent = decision === "accept" ? "Принять" : "Отклонить";
          button.disabled = !row.can_answer;
          button.addEventListener("click", () => this.answer(row, decision, actions, feedback.value));
          actions.append(button);
        }
        if (revisable) {
          const revise = document.createElement("button");
          revise.textContent = "Вернуть с замечаниями";
          revise.disabled = !row.can_answer;
          revise.addEventListener("click", () => this.answer(row, "revise", actions, feedback.value));
          actions.append(revise);
        }
        card.append(actions);
        card.append(feedback);
        if (!row.can_answer) {
          const unavailable = document.createElement("p");
          unavailable.textContent = "Владелец интерактивного запуска недоступен. Ответ не отправлен.";
          card.append(unavailable);
        }
      }
      this.host.append(card);
    }
  }

  async answer(row, decision, actions, feedback) {
    if (decision === "revise" && !(feedback || "").trim()) {
      this.message = "Опишите, что не так: возврат без замечаний не отправляется.";
      if (!this.stopped) await this.refresh();
      return;
    }
    for (const button of actions.querySelectorAll("button")) button.disabled = true;
    try {
      const body = { decision, opened_seq: row.opened_seq, context_sha256: row.prompt.context_sha256 };
      const text = (feedback || "").trim();
      if (text) body.feedback = text.slice(0, 4000);
      const response = await fetch(`/studies/${encodeURIComponent(row.answer_study_id || this.studyId)}/hitl/${encodeURIComponent(row.request_id)}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
      const ack = await response.json();
      this.message = `Решение ${ack.decision} записано в журнал, событие ${ack.seq}.`;
    } catch (error) {
      this.message = `Ответ не подтверждён: ${error.message}. Проверьте журнал перед повтором.`;
    }
    if (!this.stopped) await this.refresh();
  }
}

function planSummary(snapshot) {
  const plan = JSON.parse(snapshot);
  const host = document.createElement("div");
  host.className = "hitl-plan";
  for (const question of plan.questions || []) {
    const element = document.createElement("p");
    element.textContent = question.text;
    host.append(element);
  }
  for (const task of plan.tasks || []) {
    const item = document.createElement("article");
    const title = document.createElement("h4");
    title.textContent = `${task.task_key} · ${task.title}`;
    const goal = document.createElement("p");
    goal.textContent = task.goal;
    const outputs = document.createElement("p");
    outputs.textContent = `Результаты: ${(task.expected_outputs || []).map(output => output.name).join(", ") || "не заданы"}`;
    const checks = document.createElement("p");
    checks.textContent = `Проверки: ${(task.expected_outputs || []).flatMap(output => (output.rubric?.items || []).map(check => check.text)).join("; ") || "не заданы"}`;
    item.append(title, goal, outputs, checks);
    host.append(item);
  }
  return host;
}
