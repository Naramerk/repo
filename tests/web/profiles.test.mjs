import {test} from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
const load = async name => {
  const source = readFileSync(new URL(`../../coscientist/api/web/js/${name}.js`, import.meta.url), "utf8");
  return import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
};
const {LocalProfilePanel} = await load("profiles");
const {PresetForm} = await load("presets");
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.textContent = ""; }
  set innerHTML(_) { throw new Error("No HTML execution"); }
  setAttribute() {}
  append(...nodes) {
    this.children.push(...nodes);
    if (this.tag === "select" && this.value === undefined) this.value = nodes[0].value;
  }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
}
const elements = node => [node, ...node.children.flatMap(elements)];
const find = (node, tag) => elements(node).find(child => child.tag === tag);
globalThis.document = {createElement: tag => new Element(tag)};
const local = new Map(), session = new Map();
const storage = map => ({getItem:key => map.get(key), setItem:(key,value) => map.set(key,value), removeItem:key => map.delete(key)});
globalThis.localStorage = storage(local);
globalThis.sessionStorage = storage(session);
const first = {id:"lp_"+"a".repeat(32),nickname:"<script>local</script>"};
const second = {id:"lp_"+"b".repeat(32),nickname:"Second"};
const ok = body => ({ok:true,json:async()=>body});

test("selection persists, defaults to all studies, and never registers a guessed user", async () => {
  local.clear(); const calls = [];
  globalThis.fetch = async (url, options) => { calls.push([url,options]); return ok({profiles:[first,second]}); };
  const host = new Element("main"), panel = new LocalProfilePanel(host, () => {});
  assert.equal(panel.current, null);
  await panel.load(); panel.select(first.id);
  assert.equal(panel.studiesURL(), `/console/profiles/${first.id}/studies`);
  const restored = new LocalProfilePanel(new Element("main"), () => {});
  await restored.load();
  assert.deepEqual(restored.current, first);
  assert.ok(elements(host).some(node => node.textContent === first.nickname));
  assert.ok(calls.every(([url,options]) => url === "/console/profiles" && options === undefined));
  restored.select(null);
  assert.equal(restored.studiesURL(), "/studies");
  assert.equal(restored.current, null);
});

test("late profile fetch cannot replace newer registry state; missing stored profile is explicit", async () => {
  local.clear(); local.set("cosnext.local-profile", first.id);
  let resolveOld;
  globalThis.fetch = () => new Promise(resolve => { resolveOld = resolve; });
  const panel = new LocalProfilePanel(new Element("main"), () => {});
  const old = panel.load();
  globalThis.fetch = async () => ok({profiles:[second]});
  await panel.load(); resolveOld(ok({profiles:[first]})); await old;
  assert.deepEqual(panel.profiles, [second]);
  assert.equal(panel.current.id, first.id);
  assert.match(panel.current.nickname, /Недоступный/);
  assert.equal(panel.studiesURL(), `/console/profiles/${first.id}/studies`);
});

test("explicit attach pins profile and actual journal; late response cannot label a different selection", async () => {
  local.clear(); const requests = []; let resolveAttach;
  globalThis.fetch = async (url, options) => {
    if (!options) return ok({profiles:[first,second]});
    requests.push([url,JSON.parse(options.body)]);
    return await new Promise(resolve => { resolveAttach = resolve; });
  };
  const panel = new LocalProfilePanel(new Element("main"), () => {});
  await panel.load(); panel.select(first.id); panel.setStudyId("actual-journal-r1");
  assert.equal(requests.length, 0);
  const attached = panel.attachButton.listeners.click();
  panel.select(second.id); panel.setStudyId("different-study");
  resolveAttach(ok({study_id:"root"})); await attached;
  assert.deepEqual(requests, [[`/console/profiles/${first.id}/studies`,{study_id:"actual-journal-r1"}]]);
  assert.equal(panel.message.textContent, "");
});

test("create response does not override a profile selected while it was pending", async () => {
  local.clear(); let resolveCreate;
  globalThis.fetch = async (_url, options) => options
    ? await new Promise(resolve => { resolveCreate = resolve; }) : ok({profiles:[first,second]});
  const host = new Element("main"), panel = new LocalProfilePanel(host, () => {});
  await panel.load(); find(host,"input").value = "New";
  const creating = find(host,"form").listeners.submit({preventDefault(){}});
  panel.select(second.id);
  resolveCreate(ok({id:"lp_"+"c".repeat(32),nickname:"New"})); await creating;
  assert.equal(panel.current.id, second.id);
});

const presets = [{id:"p",label:"Preset",description:"Configured",models:{planner:"glm-4.7",executor:"glm-4.7"},deadline_s:30,max_llm_calls:2,max_tokens:100,max_tool_calls:2}];
test("uncertain launch retries retain original profile across reload and selection change", async () => {
  session.clear(); const requests = [];
  globalThis.fetch = async (_url, options) => {
    requests.push(JSON.parse(options.body));
    if (requests.length === 1) throw new Error("response lost");
    return ok({study_id:"same-owner"});
  };
  const host = new Element("main"), form = new PresetForm(host, () => {}, first);
  form.render(presets); find(host,"textarea").value = "Research";
  await find(host,"form").listeners.submit({preventDefault(){}}); form.stop();
  const nextHost = new Element("main"), next = new PresetForm(nextHost, () => {}, second);
  next.render(presets);
  await find(nextHost,"form").listeners.submit({preventDefault(){}});
  assert.deepEqual(requests[0], requests[1]);
  assert.equal(requests[1].profile_id, first.id);
});

test("new selected-profile request is explicit and receives a fresh launch key", async () => {
  session.set("cosnext.pending-launch", JSON.stringify({preset_id:"p",task_text:"Research",idempotency_key:"old",profile_id:first.id}));
  let body;
  globalThis.fetch = async (_url, options) => { body = JSON.parse(options.body); return ok({study_id:"new"}); };
  const host = new Element("main"); new PresetForm(host, () => {}, second).render(presets);
  const fresh = elements(host).find(node => node.tag === "button" && node.textContent.startsWith("Новый запрос"));
  assert.equal(fresh.hidden, false);
  fresh.listeners.click();
  await find(host,"form").listeners.submit({preventDefault(){}});
  assert.equal(body.profile_id, second.id);
  assert.notEqual(body.idempotency_key, "old");
});
