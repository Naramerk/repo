import {test} from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
const source = readFileSync(new URL("../../coscientist/api/web/js/presets.js", import.meta.url), "utf8");
const {PresetForm, RunControls} = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; }
  setAttribute() {}
  append(...children) {
    this.children.push(...children);
    if (this.tag === "select" && this.value === undefined) this.value = children[0].value;
  }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
}
globalThis.document = {createElement: tag => new Element(tag)};
const storage = new Map();
globalThis.sessionStorage = {
  getItem:key=>storage.get(key), setItem:(key,value)=>storage.set(key,value), removeItem:key=>storage.delete(key),
};
const presets = [{id:"p",label:"Profile",description:"Description",models:{planner:"glm-4.7",executor:"glm-4.7"},deadline_s:30,max_llm_calls:2,max_tokens:100,max_tool_calls:2}];
const find = (node, tag) => node.tag === tag ? node : node.children.map(child => find(child, tag)).find(Boolean);

test("network-uncertain launch survives reload with same key and only three public fields", async () => {
  storage.clear();
  const bodies = [];
  globalThis.fetch = async (url, options) => {
    bodies.push(JSON.parse(options.body));
    if (bodies.length === 1) throw new Error("connection lost");
    return {ok:true,json:async()=>({study_id:"stable"})};
  };
  const firstHost = new Element("main"), form = new PresetForm(firstHost, ()=>{});
  form.render(presets);
  find(firstHost,"textarea").value = "Exact task <script>";
  await find(firstHost,"form").listeners.submit({preventDefault(){}});
  form.stop();
  const nextHost = new Element("main"), launched = [];
  const next = new PresetForm(nextHost, response=>launched.push(response));
  next.render(presets);
  assert.equal(find(nextHost,"textarea").value, "Exact task <script>");
  await find(nextHost,"form").listeners.submit({preventDefault(){}});
  assert.deepEqual(bodies[0], bodies[1]);
  assert.deepEqual(Object.keys(bodies[0]).sort(), ["idempotency_key","preset_id","task_text"]);
  assert.equal(launched[0].study_id,"stable");
});

test("stopped owner-status request cannot overwrite another study", async () => {
  let resolve;
  globalThis.fetch = async url => url === "/console/run-presets"
    ? {ok:true,json:async()=>({poll_interval_ms:1000})}
    : new Promise(done=>{resolve=done;});
  const host = new Element("main"), controls = new RunControls(host,"A",()=>{});
  const started = controls.start();
  while (!resolve) await Promise.resolve();
  controls.stop();
  resolve({ok:true,json:async()=>({run_status:"running",study_id:"A",active_study_id:"A"})});
  await started;
  assert.equal(host.children.length,0);
});

test("cancel targets original strict owner ID while viewing replan journal", async () => {
  let request;
  globalThis.fetch = async (url, options) => {
    request = [url, JSON.parse(options.body)];
    return {ok:true,json:async()=>({study_id:"root"})};
  };
  const host = new Element("main"), controls = new RunControls(host,"root-r1",()=>{});
  controls.refresh = async ()=>controls.stop();
  controls.render({run_status:"running",study_id:"root",active_study_id:"root-r1",can_cancel:true});
  await find(host,"button").listeners.click();
  assert.deepEqual(request,["/studies/root/cancel",{by:"console-human"}]);
});
