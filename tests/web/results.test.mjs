import {test} from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
const load = async name => {
  const source=readFileSync(new URL(`../../coscientist/api/web/js/${name}.js`,import.meta.url),"utf8");
  return import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
};
const {StudyModel}=await load("model");
const {valueAtPointer,jsonEvidence}=await load("results");
const {focusGraph}=await load("graph");
const event=(seq,type,payload)=>({seq,type,payload,study_id:"s",ts:`t${seq}`});

test("attempt/result history never hides a failed attempt behind a later success",()=>{
  const model=new StudyModel();
  model.apply([
    event(1,"TaskCreated",{task_id:"s/t",spec:{task_key:"t",title:"Measured result"}}),
    event(2,"AttemptStarted",{task_id:"s/t",attempt_id:"s/t#1"}),
    event(3,"ResultRecorded",{result_id:"r1",task_id:"s/t",attempt_id:"s/t#1",computed_outcome:"FAILED",check_results:[{status:"FAILED",role:"prediction"}]}),
    event(4,"AttemptClosed",{attempt_id:"s/t#1",close_reason:"scientific"}),
    event(5,"AttemptStarted",{task_id:"s/t",attempt_id:"s/t#2"}),
    event(6,"ResultRecorded",{result_id:"r2",task_id:"s/t",attempt_id:"s/t#2",computed_outcome:"SUCCESS",check_results:[{status:"MET",role:"validity"}]})
  ]);
  model.apply([event(6,"ResultRecorded",{result_id:"r2"})]);
  assert.equal(model.resultRows().length,2);
  assert.equal(model.resultRows()[1].check_results[0].role,"prediction");
  assert.equal(model.attempts.get("s/t#1").close_reason,"scientific");
  assert.equal(model.attempts.get("s/t#2").closed,false);
  assert.equal(model.resultRows("another-task").length,0);
});

test("RFC6901 selects exact escaped keys and own properties only",()=>{
  const value={"a/b":{"~key":[null,{observed:0}]}};
  assert.deepEqual(valueAtPointer(value,"/a~1b/~0key/1/observed"),{found:true,value:0});
  assert.equal(valueAtPointer(value,"/a~1b/~0key/00").found,false);
  assert.equal(valueAtPointer(value,"/__proto__").found,false);
  assert.equal(valueAtPointer(value,"/a~2b").found,false);
  assert.deepEqual(valueAtPointer(false,""),{found:true,value:false});
});

test("interrupted and open attempts stay visible without invented results",()=>{
  const model=new StudyModel();
  model.apply([
    event(1,"AttemptStarted",{task_id:"s/t",attempt_id:"s/t#1"}),
    event(2,"AttemptClosed",{task_id:"s/t",attempt_id:"s/t#1",close_reason:"cancelled"}),
    event(3,"AttemptStarted",{task_id:"s/t",attempt_id:"s/t#2"}),
    event(4,"ResultRecorded",{result_id:"r",task_id:"s/t",attempt_id:"s/t#2",computed_outcome:"SUCCESS"}),
    event(5,"AttemptClosed",{task_id:"s/t",attempt_id:"s/t#2",close_reason:"result"}),
    event(6,"AttemptStarted",{task_id:"s/other",attempt_id:"s/other#1"}),
  ]);
  const rows=model.attemptRows();
  assert.equal(rows.length,3);
  assert.equal(rows[0].attempt.closed,false);
  assert.equal(rows[0].results.length,0);
  assert.equal(rows[2].attempt.close_reason,"cancelled");
  assert.equal(rows[2].results.length,0);
  assert.equal(model.resultRows().length,1);
  assert.equal(model.attemptRows("s/t").length,2);
  assert.equal(rows[1].results[0].computed_outcome,"SUCCESS");
});

test("invalid/missing/nonfinite source never becomes an invented evidence value",()=>{
  assert.deepEqual(jsonEvidence("banner {\"x\":7}","/x"),{found:false,reason:"invalid_json"});
  assert.equal(jsonEvidence('{"x":1e999}',"/x").reason,"invalid_json");
  assert.equal(jsonEvidence('{"x":0}',"/y").reason,"pointer_missing");
  assert.deepEqual(jsonEvidence('{"x":false}',"/x"),{found:true,source:"false"});
});

test("evidence selection preserves numeric tokens and rejects ambiguous JSON",()=>{
  for (const token of ["9007199254740993", "0.123456789012345678901", "-0", "1.2300e-30"]) {
    assert.deepEqual(jsonEvidence(`{"a/b":{"~x":${token}}}`,"/a~1b/~0x"),{found:true,source:token});
  }
  assert.equal(jsonEvidence('{"x":1,"x":2}',"/x").reason,"invalid_json");
  assert.equal(jsonEvidence('{"x":1,"\\u0078":2}',"/x").reason,"invalid_json");
  assert.equal(jsonEvidence('{"x":1,"unselected":{"a":1,"a":2}}',"/x").reason,"invalid_json");
  assert.equal(jsonEvidence('{"x":[10]}',"/x/00").found,false);
  assert.equal(jsonEvidence('['.repeat(66)+'0'+']'.repeat(66),"").reason,"parse_limit");
  assert.equal(jsonEvidence('"'+"x".repeat(2*1024*1024)+'"',"").reason,"parse_limit");
  for (const invalid of ['{"x":01}', '{"x":1,}', '[1,]', '{"x":"\\q"}', '{"x":true}junk']) {
    assert.equal(jsonEvidence(invalid,"/x").reason,"invalid_json");
  }
  assert.deepEqual(jsonEvidence('{"x": {"y": 9007199254740993}}',"/x"),{found:true,source:'{"y": 9007199254740993}'});
});

test("graph focus preserves direct source links without mutating full graph",()=>{
  const graph={nodes:new Map([["q",{kind:"question"}],["e",{kind:"evidence"}],["a",{kind:"artifact"}],["other",{kind:"study"}]]),edges:new Map([["eq",{src_id:"e",dst_id:"q"}],["ea",{src_id:"e",dst_id:"a"}]])};
  const focused=focusGraph(graph,"evidence");
  assert.deepEqual([...focused.nodes.keys()],["q","e","a"]);
  assert.equal(focused.edges.size,2);
  assert.equal(graph.nodes.size,4);
  assert.equal(focusGraph(graph,"").nodes.size,4);
  assert.equal(focusGraph(graph,"absent").nodes.size,0);
});

test("late CAS snapshot cannot erase journal registration or advance event cursor",()=>{
  const model=new StudyModel();
  model.apply([event(7,"ArtifactRegistered",{artifact_id:"a",name:"data.json",sha256:"digest"})]);
  model.mergeArtifactSnapshot([{artifact_id:"a",name:"data.json",registered_in_journal:false},{artifact_id:"report",name:"science.md",registered_in_journal:false}]);
  assert.equal(model.artifacts.get("a").registered_in_journal,true);
  assert.equal(model.artifacts.get("report").registered_in_journal,false);
  assert.equal(model.lastSeq,7);
  assert.equal(model.events.length,1);
});
