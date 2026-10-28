import {test} from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
const source = readFileSync(new URL("../../coscientist/api/web/js/hitl.js", import.meta.url), "utf8");
const {HitlPanel} = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; }
  setAttribute() {}
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  querySelectorAll() { return this.children.filter(child => child.tag === "button"); }
}
globalThis.document = {createElement: tag => new Element(tag)};

test("journal context renders as text and unavailable owner cannot receive answer", () => {
  const host = new Element("main"), panel = new HitlPanel(host, "study");
  panel.render([{request_id:"hitl", status:"pending", can_answer:false, prompt:{
    question:"<script>question</script>", context_json:JSON.stringify({questions:[{text:"<img onerror=bad()>"}], tasks:[]}),
    options:["accept","reject"], context_sha256:"abc", attempt_id:"a",
  }}]);
  const card = host.children.at(-1);
  assert.equal(card.children[0].textContent, "<script>question</script>");
  assert.match(card.children[2].children[0].textContent, /<img onerror=bad\(\)>/);
  assert.ok(card.children[4].children.every(button => button.disabled));
});

test("stopped panel ignores in-flight fetch from another study", async () => {
  let resolve;
  globalThis.fetch = () => new Promise(done => { resolve = done; });
  const host = new Element("main"), panel = new HitlPanel(host, "old");
  const request = panel.refresh(); panel.stop();
  resolve({ok:true,json:async()=>[{status:"pending"}]});
  await request;
  assert.equal(host.children.length, 0);
});

test("answer submits opening identity and reports only durable ack", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push([url, options]);
    return options ? {ok:true,json:async()=>({decision:"accept",seq:42})}
      : {ok:true,json:async()=>[]};
  };
  const host = new Element("main"), panel = new HitlPanel(host, "study");
  const actions = new Element("div"); actions.append(new Element("button"));
  await panel.answer({request_id:"r",opened_seq:11,prompt:{context_sha256:"digest"}}, "accept", actions);
  assert.deepEqual(JSON.parse(calls[0][1].body), {decision:"accept",opened_seq:11,context_sha256:"digest"});
  assert.match(host.children[0].textContent, /событие 42/);
});

test("feedback rides the POST body; revise without text is not sent", async () => {
  const calls = [];
  globalThis.fetch = async (url, options) => {
    calls.push([url, options]);
    return options ? {ok:true,json:async()=>({decision:"revise",seq:7})}
      : {ok:true,json:async()=>[]};
  };
  const host = new Element("main"), panel = new HitlPanel(host, "study");
  const actions = new Element("div"); actions.append(new Element("button"));
  await panel.answer({request_id:"r",opened_seq:11,prompt:{context_sha256:"digest"}}, "revise", actions, "   ");
  assert.ok(calls.every(([url, options]) => !options), "empty revise must not POST");
  assert.match(host.children[0].textContent, /что не так/);
  const posts = () => calls.filter(([, options]) => options);
  await panel.answer({request_id:"r",opened_seq:11,prompt:{context_sha256:"digest"}}, "revise", actions, "  сузь объём  ");
  assert.deepEqual(
    JSON.parse(posts()[0][1].body),
    {decision:"revise",opened_seq:11,context_sha256:"digest",feedback:"сузь объём"},
  );
  await panel.answer({request_id:"r",opened_seq:11,prompt:{context_sha256:"digest"}}, "accept", actions, "ок");
  assert.deepEqual(
    JSON.parse(posts()[1][1].body),
    {decision:"accept",opened_seq:11,context_sha256:"digest",feedback:"ок"},
  );
});

test("stage gate cards offer the revise button; the plan gate does not", () => {
  const host = new Element("main"), panel = new HitlPanel(host, "study");
  const mk = (kind) => ({
    request_id:"hitl", kind, status:"pending", can_answer:true,
    prompt:{question:"?", context_json:"{}", options:["accept","reject"], context_sha256:"abc", attempt_id:"a"},
  });
  panel.render([mk("spec_approval"), mk("plan_approval")]);
  const stageCard = host.children[2];
  const planCard = host.children[3];
  const stageLabels = stageCard.children[4].children.map(b => b.textContent);
  const planLabels = planCard.children[4].children.map(b => b.textContent);
  assert.deepEqual(stageLabels, ["Принять", "Отклонить", "Вернуть с замечаниями"]);
  assert.deepEqual(planLabels, ["Принять", "Отклонить"]);
  const textarea = stageCard.children[5];
  assert.equal(textarea.tag, "textarea");
  assert.equal(textarea.maxLength, 4000);
});
