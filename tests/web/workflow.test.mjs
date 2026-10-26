import {test} from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
const path = name => new URL(`../../coscientist/api/web/js/${name}.js`,import.meta.url);
const data = text => `data:text/javascript;base64,${Buffer.from(text).toString("base64")}`;
const graph = data(readFileSync(path("graph"),"utf8"));
const source = readFileSync(path("workflow"),"utf8").replace('"./graph.js"',JSON.stringify(graph));
const {taskWorkflow,taskTitle} = await import(data(source));
const task = (key,depends_on=[],id=`s/${key}`) => Object.freeze({id,key,title:`Task ${key}`,kind:"experiment",spec:Object.freeze({depends_on:Object.freeze(depends_on)})});

test("workflow represents only actual tasks and dependency edges without mutating sources",()=>{
  const rows=[task("a"),task("b",["a"]),task("c",["s/b"])];
  const before=JSON.stringify(rows), graph=taskWorkflow(rows);
  assert.equal(graph.nodes.size,3); assert.equal(graph.edges.size,2);
  assert.equal(graph.positions.get("s/a").y<graph.positions.get("s/b").y,true);
  assert.equal(graph.positions.get("s/b").y<graph.positions.get("s/c").y,true);
  assert.equal(JSON.stringify(rows),before);
});
test("unknown and ambiguous task dependencies are disclosed rather than invented",()=>{
  const graph=taskWorkflow([task("a",[],"s/a1"),task("a",[],"s/a2"),task("b",["a","missing"])]);
  assert.equal(graph.edges.size,0); assert.equal(graph.missing.length,2);
  assert.equal(graph.nodes.size,3);
});
test("cyclic and downstream tasks remain inspectable with explicit unresolved state",()=>{
  const graph=taskWorkflow([task("a",["b"]),task("b",["a"]),task("c",["b"]),task("independent")]);
  assert.equal(graph.positions.size,4); assert.equal(graph.edges.size,3);
  assert.deepEqual(graph.unresolved,["s/a","s/b","s/c"]);
  assert.equal(graph.positions.get("s/independent").y,0);
  assert.equal(taskWorkflow([]).positions.size,0);
});
test("titles use journal text or stage identity, never reference-video content",()=>{
  assert.equal(taskTitle({...task("PLAN"),title:""}),"План исследования");
  assert.equal(taskTitle({...task("custom"),title:""}),"custom");
  assert.equal(taskTitle({...task("custom"),title:"Actual observed task"}),"Actual observed task");
});
