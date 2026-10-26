/** Live journal stream: one WebSocket per study, server-push, zero polling.
 *
 * Reconnect resumes from the last applied seq (`?after=`), so a dropped
 * connection replays only the gap — the fold in model.js stays monotonic.
 */
export class StudyStream {
  /**
   * @param {string} studyId
   * @param {(events: object[]) => void} onBatch applied events, in seq order
   * @param {(connected: boolean) => void} onState
   */
  constructor(studyId, onBatch, onState) {
    this.studyId = studyId;
    this.onBatch = onBatch;
    this.onState = onState;
    this.lastSeq = 0;
    this.closed = false;
    this.ws = null;
    this.retryMs = 1000;
  }

  start() {
    this.closed = false;
    this._connect();
  }

  stop() {
    this.closed = true;
    if (this.ws) { try { this.ws.close(); } catch { /* already gone */ } }
    this.ws = null;
    this.onState(false);
  }

  _connect() {
    if (this.closed) return;
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = `${proto}//${location.host}/console/studies/${encodeURIComponent(this.studyId)}/stream?after=${this.lastSeq}`;
    const ws = new WebSocket(url);
    this.ws = ws;
    ws.onopen = () => { this.retryMs = 1000; this.onState(true); };
    ws.onmessage = (msg) => {
      let parsed;
      try { parsed = JSON.parse(msg.data); } catch { return; }
      // The lane sends one EventWire object per message; tolerate arrays too.
      const batch = Array.isArray(parsed) ? parsed : [parsed];
      if (batch.length === 0 || typeof batch[0]?.seq !== "number") return;
      // Journal seqs are monotonic per study; keep the resume cursor exact.
      const last = batch[batch.length - 1];
      if (typeof last?.seq === "number" && last.seq > this.lastSeq) this.lastSeq = last.seq;
      this.onBatch(batch);
    };
    ws.onclose = () => {
      this.onState(false);
      if (!this.closed) {
        setTimeout(() => this._connect(), this.retryMs);
        this.retryMs = Math.min(this.retryMs * 2, 15000);
      }
    };
    ws.onerror = () => { try { ws.close(); } catch { /* noop */ } };
  }
}
