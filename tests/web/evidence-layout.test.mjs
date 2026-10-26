import {test} from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
const source=readFileSync(new URL("../../coscientist/api/web/js/graph.js",import.meta.url),"utf8");
const {layoutEvidenceGraph,nodeLabel,GraphView}=await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
function graph(ids,edges=[]) {
  return {nodes:new Map(ids.map((id,index)=>[id,Object.freeze({node_id:id,kind:["study","question","criterion","evidence","artifact"][index%5]})])),
    edges:new Map(edges.map(([src_id,dst_id],index)=>[String(index),Object.freeze({src_id,dst_id,kind:"derived_from"})]))};
}
test("provenance layers follow actual links, not an empty column for every node type",()=>{
  const input=graph(["study","question","criterion","e1","a1","e2","a2"],[["e1","a1"],["e1","question"],["e2","a2"],["e2","question"]]);
  const before=JSON.stringify([...input.edges]), positions=layoutEvidenceGraph(input);
  assert.equal(positions.size,7);
  assert.equal(new Set([...positions.values()].map(p=>p.x)).size,2);
  assert.ok(positions.get("e1").x<positions.get("a1").x);
  assert.equal(JSON.stringify([...input.edges]),before);
});
test("cycles, downstream edges, self links and late endpoints keep finite distinct positions",()=>{
  const input=graph(["a","b","c","d"],[["a","b"],["b","a"],["b","c"],["c","c"],["absent","d"]]);
  const positions=layoutEvidenceGraph(input);
  assert.equal(positions.get("a").x,positions.get("b").x);
  assert.ok(positions.get("c").x>positions.get("b").x);
  assert.equal(new Set([...positions.values()].map(p=>`${p.x},${p.y}`)).size,4);
  assert.ok([...positions.values()].every(p=>Number.isFinite(p.x)&&Number.isFinite(p.y)));
  assert.equal(input.edges.size,5);
  assert.equal(positions.has("absent"),false);
});
test("input ordering does not change layout and disconnected journals use a balanced overview",()=>{
  const input=graph(["d","c","b","a"],[["a","b"],["b","c"]]);
  const reversed={nodes:new Map([...input.nodes].reverse()),edges:new Map([...input.edges].reverse())};
  assert.deepEqual([...layoutEvidenceGraph(input)].sort(),[...layoutEvidenceGraph(reversed)].sort());
  const isolated=layoutEvidenceGraph(graph(["a","b","c","d","e","f","g","h","i"]));
  assert.equal(new Set([...isolated.values()].map(p=>p.x)).size,3);
  assert.equal(new Set([...isolated.values()].map(p=>p.y)).size,3);
  assert.equal(layoutEvidenceGraph(graph([])).size,0);
});
test("long provenance chains and cycles do not exhaust the JavaScript call stack",()=>{
  const ids=Array.from({length:12000},(_,index)=>`n${index}`);
  const edges=ids.slice(1).map((id,index)=>[ids[index],id]);
  const positions=layoutEvidenceGraph(graph(ids,edges));
  assert.equal(positions.size,ids.length);
  assert.ok(positions.get(ids.at(-1)).x>positions.get(ids[0]).x);
  edges.push([ids.at(-1),ids[0]]);
  const cycle=layoutEvidenceGraph(graph(ids,edges));
  assert.equal(cycle.size,ids.length);
  assert.equal(new Set([...cycle.values()].map(p=>p.x)).size,1);
});
test("question and study cards show canonical journal wording instead of opaque ids",()=>{
  assert.equal(nodeLabel({node_id:"s:question:q1",attrs:{formulation:"Observed question?"}}),"Observed question?");
  assert.equal(nodeLabel({node_id:"s:study",attrs:{goal:{statement:"Actual user goal"}}}),"Actual user goal");
  assert.equal(nodeLabel({node_id:"s:study",attrs:{request:"Recorded request"}}),"Recorded request");
  assert.equal(nodeLabel({node_id:"s:question:q1",attrs:{formulation:null}}),"q1");
});
test("fit contains the entire rendered drawing, including cycle routes and wide labels",()=>{
  for (const bounds of [{x:-110,y:-50,width:273,height:100},{x:-190,y:-50,width:620,height:228}]) {
    const view=Object.assign(Object.create(GraphView.prototype),{
      pos:new Map([["a",{x:0,y:0}]]),host:{getBoundingClientRect:()=>({width:342,height:390})},
      root:{getBBox:()=>bounds,setAttribute(){}},
    });
    view.fit();
    assert.ok(view.panX+bounds.x*view.zoom>=0);
    assert.ok(view.panX+(bounds.x+bounds.width)*view.zoom<=342);
    assert.ok(view.panY+bounds.y*view.zoom>=0);
    assert.ok(view.panY+(bounds.y+bounds.height)*view.zoom<=390);
  }
});
