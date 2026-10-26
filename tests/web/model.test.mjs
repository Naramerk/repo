import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";
const source = readFileSync(new URL("../../coscientist/api/web/js/model.js", import.meta.url), "utf8");
const { StudyModel } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const event = (seq, type, payload) => ({seq, type, payload, study_id: "s"});

test("inherited task keeps actual outcome and repeated events do not duplicate results", () => {
  const m = new StudyModel();
  m.apply([event(1,"TaskInherited",{task_id:"s/task",state:"DONE",outcome:"SUCCESS",spec:{task_key:"task"}}),event(2,"ResultRecorded",{})]);
  m.apply([event(1,"TaskInherited",{task_id:"s/task",state:"PENDING"}),event(2,"ResultRecorded",{})]);
  assert.equal(m.tasks.get("s/task").outcome,"SUCCESS");
  assert.equal(m.events.length,2);
  assert.equal(m.usage.record_results,1);
});

test("registered report appears without graph and metadata survives kind stamp", () => {
  const m = new StudyModel();
  m.apply([event(1,"ArtifactRegistered",{artifact_id:"a",name:"science.md",media_type:"text/markdown",sha256:"abc",producer:"agent"})]);
  m.apply([event(2,"ArtifactRegistered",{artifact_id:"a",name:"science.md",kind:"report",media_type:null})]);
  assert.equal(m.docRows()[0].media_type,"text/markdown");
  assert.equal(m.docRows()[0].name,"science.md");
  assert.equal(m.graph.nodes.size,0);
});

test("graph upsert and late edge update same identity", () => {
  const m = new StudyModel();
  m.apply([event(1,"GraphNodeUpserted",{node_id:"q",kind:"question",attrs:{title:"initial"}})]);
  m.apply([event(2,"GraphNodeUpserted",{node_id:"q",kind:"question",attrs:{title:"updated"}}),event(3,"GraphEdgeAdded",{edge_id:"e",src_id:"a",dst_id:"q",kind:"supports"})]);
  assert.equal(m.graph.nodes.size,1);
  assert.equal(m.graph.nodes.get("q").attrs.title,"updated");
  assert.equal(m.graph.edges.get("e").src_id,"a");
});
