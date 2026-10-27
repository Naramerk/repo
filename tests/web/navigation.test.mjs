import {test} from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
const load = async name => import(`data:text/javascript;base64,${Buffer.from(readFileSync(new URL(`../../coscientist/api/web/js/${name}.js`, import.meta.url), "utf8")).toString("base64")}`);
const {parseRoute, studyRoute} = await load("routes");
const {observedCalls, literatureRecords, codeArtifacts, ObservationView} = await load("observation");
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.textContent = ""; this.listeners = {}; }
  append(...children) { this.children.push(...children); if (this.tag === "select" && this.value === undefined) this.value = children[0].value; }
  replaceChildren(...children) { this.children = children; }
  addEventListener(type, callback) { this.listeners[type] = callback; }
  setAttribute() {}
  set innerHTML(_) { throw new Error("HTML injection forbidden"); }
}
const elements = node => [node, ...node.children.flatMap(elements)];
const text = node => elements(node).map(child => child.textContent).join(" ");
globalThis.document = {createElement: tag => new Element(tag)};
const event = (seq, type, payload) => ({seq, type, payload});
const model = () => ({events: [], tasks: new Map(), results: new Map(), artifacts: new Map()});

test("new research and study catalog are separate pages with stable study deep links", () => {
  assert.deepEqual(parseRoute("#/new"), {page:"new"});
  assert.deepEqual(parseRoute("#/runs"), {page:"runs"});
  assert.deepEqual(parseRoute(studyRoute("cli-2026-r1", "tools")), {page:"study",id:"cli-2026-r1",tab:"tools"});
  assert.deepEqual(parseRoute("#/study/old-cli"), {page:"study",id:"old-cli",tab:"activity"});
  assert.equal(parseRoute("#/study/id/unknown").tab, "activity");
  assert.equal(parseRoute("#/study/%oops").page, "runs");
  assert.deepEqual(parseRoute("#/settings/mcp"), {page:"runs"});
  const html = readFileSync(new URL("../../coscientist/api/web/index.html", import.meta.url), "utf8");
  assert.ok(html.indexOf('id="view-new"') > html.indexOf('id="runs-list"'));
  assert.ok(html.indexOf('id="new-run"') > html.indexOf('id="view-new"'));
  const navigation = [...html.matchAll(/<button data-tab="([a-z]+)" class="tab" aria-label="([^"]+)">([^<]+)<\/button>/g)];
  assert.equal(navigation.length, 10);
  assert.ok(navigation.every(([, , label, visible]) => label === visible && label.trim()));
});

test("calls join actual MCP receipts by identity and preserve missing-response uncertainty", () => {
  const m = model();
  m.events = [event(1,"McpRequestRecorded",{call_id:"a",tool:"search",server_name:"trusted",args_digest:"d"}),
    event(2,"McpResponseRecorded",{call_id:"a",tool:"search",server_name:"trusted",is_error:false,content_digest:"reply"}),
    event(3,"ToolCalled",{ref:{call_id:"a",tool:"search"},outcome:"ok",duration_ms:42}),
    event(4,"McpRequestRecorded",{call_id:"b",tool:"pending",server_name:"trusted"})];
  const calls = observedCalls(m);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].id, "b"); assert.equal(calls[0].outcome, undefined);
  assert.equal(calls[1].response.content_digest, "reply"); assert.equal(calls[1].duration_ms, 42);
  const host = new Element("main"); new ObservationView(host, () => {}).render(m,"tools");
  assert.match(text(host), /pending · итог не записан/);
  assert.match(text(host), /0 ошибок ToolCalled/);
});

test("literature counts exact stage records while code opens actual CAS identity without inferring papers", () => {
  const m = model();
  m.tasks.set("lit",{id:"lit",key:"LIT",title:"Literature",state:"DONE",outcome:"SUCCESS"});
  m.results.set("r",{result_id:"r",task_id:"lit",artifacts:["json"],output_map:{papers:"json"}});
  m.results.set("other",{result_id:"other",task_id:"other",artifacts:["unrelated"]});
  m.artifacts.set("code",{id:"code",name:"<script>.py",media_type:"text/x-python",registered_in_journal:false});
  assert.deepEqual(literatureRecords(m).artifacts,["json"]);
  assert.deepEqual(codeArtifacts(m).map(row => row.id),["code"]);
  const host = new Element("main"); let opened;
  new ObservationView(host,id => { opened = id; }).render(m,"code");
  elements(host).find(node => node.tag === "button").listeners.click();
  assert.equal(opened,"code"); assert.match(text(host),/метаданные CAS/);
  const empty = new Element("main"); new ObservationView(empty,()=>{}).render(model(),"literature");
  assert.match(text(empty),/не зарегистрирован/);
});
