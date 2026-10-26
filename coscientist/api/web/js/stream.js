/** One resumable socket; stopped generations can never deliver to another study. */
export class StudyStream {
  constructor(studyId, onBatch, onState) {
    this.studyId = studyId; this.onBatch = onBatch; this.onState = onState;
    this.lastSeq = 0; this.closed = true; this.ws = null;
    this.retryMs = 1000; this.timer = null; this.generation = 0;
  }

  start() {
    if (!this.closed) return;
    this.closed = false;
    this._connect();
  }

  stop() {
    this.closed = true;
    this.generation++;
    clearTimeout(this.timer); this.timer = null;
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
      ws.close();
    }
    this.onState(false);
  }

  _connect() {
    if (this.closed) return;
    const generation = this.generation;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/console/studies/${encodeURIComponent(this.studyId)}/stream?after=${this.lastSeq}`);
    this.ws = ws;
    const active = () => !this.closed && this.ws === ws && this.generation === generation;
    ws.onopen = () => { if (active()) { this.retryMs = 1000; this.onState(true); } };
    ws.onmessage = (msg) => {
      if (!active()) return;
      const parsed = JSON.parse(msg.data);
      const rows = Array.isArray(parsed) ? parsed : [parsed];
      const batch = rows.filter(row => typeof row?.seq === "number" && row.seq > this.lastSeq);
      if (!batch.length) return;
      this.onBatch(batch);
      this.lastSeq = batch[batch.length - 1].seq;
    };
    ws.onclose = () => {
      if (!active()) return;
      this.ws = null;
      this.onState(false);
      this.timer = setTimeout(() => {
        this.timer = null;
        if (!this.closed && this.generation === generation) this._connect();
      }, this.retryMs);
      this.retryMs = Math.min(this.retryMs * 2, 15000);
    };
    ws.onerror = () => { if (active()) ws.close(); };
  }
}
