import {test} from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
const source = readFileSync(
  new URL("../../coscientist/api/web/js/graph.js", import.meta.url),
  "utf8",
);
const mod = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`
);
const {focusGraph, nodeLabel, edgeKindLabel, EDGE_COLORS} = mod;
function graph(kinds, edges = []) {
  const nodes = new Map(
    kinds.map((kind, index) => [
      `n${index}`,
      Object.freeze({node_id: `n${index}`, kind}),
    ]),
  );
  const edgeMap = new Map(
    edges.map(([src_id, dst_id], index) => [
      String(index),
      Object.freeze({src_id, dst_id, kind: "derived_from"}),
    ]),
  );
  return {nodes, edges: edgeMap};
}
test("hidden kinds filter nodes and edges in science and all", () => {
  const input = graph(
    ["study", "question", "evidence", "artifact"],
    [["n0", "n1"], ["n1", "n2"], ["n2", "n3"]],
  );
  const hidden = new Set(["question"]);
  const science = focusGraph(input, "", hidden);
  assert.deepEqual([...science.nodes.keys()].sort(), ["n0", "n2", "n3"]);
  assert.equal(science.edges.size, 1);
  assert.deepEqual(
    [...science.edges.values()].map(e => `${e.src_id}>${e.dst_id}`),
    ["n2>n3"],
  );
  const all = focusGraph(input, "all", hidden);
  assert.deepEqual([...all.nodes.keys()].sort(), ["n0", "n2", "n3"]);
  assert.equal(all.edges.size, 1);
  assert.equal(focusGraph(input, "all"), input);
});
test("kind focus keeps neighbours unless their kind is hidden", () => {
  const input = graph(
    ["study", "question", "evidence"],
    [["n0", "n1"], ["n0", "n2"]],
  );
  assert.equal(focusGraph(input, "study").nodes.size, 3);
  const filtered = focusGraph(input, "study", new Set(["evidence"]));
  assert.deepEqual([...filtered.nodes.keys()].sort(), ["n0", "n1"]);
  assert.equal(filtered.edges.size, 1);
});
test("edge kinds get Russian labels and legend colors", () => {
  assert.equal(edgeKindLabel("addresses"), "Отвечает на");
  assert.equal(edgeKindLabel("supersedes"), "Заменяет");
  assert.equal(edgeKindLabel("made_up_kind"), "made_up_kind");
  assert.equal(EDGE_COLORS.addresses, "#246b78");
  assert.equal(EDGE_COLORS.supports, "#28715b");
});
test("artifact nodes prefer human descriptors over id", () => {
  const id = "s:artifact:9f2a";
  assert.equal(
    nodeLabel({node_id: id, attrs: {filename: "results.csv"}}),
    "results.csv",
  );
  assert.equal(
    nodeLabel({node_id: id, attrs: {media_type: "text/markdown"}}),
    "text/markdown",
  );
  assert.equal(
    nodeLabel({node_id: id, attrs: {display_name: "Таблица"}}),
    "Таблица",
  );
  assert.equal(
    nodeLabel({node_id: id, attrs: {purpose: "Сравнить метрики"}}),
    "Сравнить метрики",
  );
  assert.equal(
    nodeLabel({node_id: id, attrs: {summary: "Итоги прогона"}}),
    "Итоги прогона",
  );
  assert.equal(
    nodeLabel({node_id: id, attrs: {display_name: "x", statement: "Real claim"}}),
    "Real claim",
  );
  assert.equal(nodeLabel({node_id: id, attrs: {}}), "9f2a");
});
