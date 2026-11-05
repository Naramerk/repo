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
const {focusGraph, assignClusters, clusterGraph, nodeLabel, edgeKindLabel, EDGE_COLORS} = mod;
function graph(kinds, edges = []) {
  const nodes = new Map(
    kinds.map((entry, index) => {
      const spec = typeof entry === "string" ? {kind: entry} : entry;
      return [`n${index}`, Object.freeze({node_id: `n${index}`, ...spec})];
    }),
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
    ["study", "question", "evidence", {kind: "artifact", attrs: {name: "Итоги"}}],
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
test("science focus hides digest-only artifacts until the service checkbox", () => {
  const input = graph(
    ["study", "evidence", {kind: "artifact", attrs: {display_name: "Сводка"}},
     {kind: "artifact", attrs: {}}, {kind: "artifact", attrs: {byte_size: 4096}}],
    [["n0", "n1"], ["n1", "n2"], ["n1", "n3"], ["n1", "n4"]],
  );
  const science = focusGraph(input, "");
  assert.deepEqual([...science.nodes.keys()].sort(), ["n0", "n1", "n2"]);
  assert.equal(science.edges.size, 2);
  const shown = focusGraph(input, "", null, {showServiceArtifacts: true});
  assert.deepEqual([...shown.nodes.keys()].sort(), ["n0", "n1", "n2", "n3", "n4"]);
  assert.equal(shown.edges.size, 4);
  // Kind focus and «Все узлы» keep service artifacts visible (kind focus adds
  // the linked evidence as a neighbour source).
  assert.equal(focusGraph(input, "artifact").nodes.size, 4);
  assert.equal(focusGraph(input, "all").nodes.size, 5);
});
test("cluster membership anchors on hypotheses with claim fallback", () => {
  const input = graph(
    ["study", "question", "hypothesis", "claim", "criterion", "evidence",
     "evidence", "artifact", "conclusion"],
    [["n2", "n4"], ["n2", "n5"], ["n3", "n5"], ["n3", "n6"], ["n4", "n7"],
     ["n2", "n1"], ["n0", "n2"]],
  );
  const membership = assignClusters(input);
  assert.equal(membership.get("n2"), "n2");
  assert.equal(membership.get("n3"), "n3");
  assert.equal(membership.get("n4"), "n2");
  // Evidence linked to both a hypothesis and a claim prefers the hypothesis.
  assert.equal(membership.get("n5"), "n2");
  assert.equal(membership.get("n6"), "n3");
  // Nodes without a head group into per-kind buckets.
  assert.equal(membership.get("n7"), "kind:artifact");
  assert.equal(membership.get("n8"), "kind:conclusion");
  assert.equal(membership.has("n0"), false);
  assert.equal(membership.has("n1"), false);
});
test("clusterGraph collapses unexpanded clusters and aggregates their edges", () => {
  const input = graph(
    ["study", "question", "hypothesis", "criterion", "evidence", "evidence",
     "claim", {kind: "artifact", attrs: {display_name: "Сводка"}}],
    [["n0", "n1"], ["n1", "n2"], ["n2", "n3"], ["n2", "n4"], ["n2", "n5"],
     ["n6", "n4"], ["n4", "n7"], ["n5", "n7"]],
  );
  const collapsed = clusterGraph(input, new Set());
  assert.deepEqual([...collapsed.graph.nodes.keys()].sort(),
    ["cluster:kind:artifact", "cluster:n2", "cluster:n6", "n0", "n1"]);
  const card = collapsed.graph.nodes.get("cluster:n2");
  assert.equal(card.cluster_count, 4);
  assert.equal(card.cluster_key, "n2");
  // A bucket card keeps a descriptive label instead of a digest id.
  assert.equal(collapsed.graph.nodes.get("cluster:kind:artifact").attrs.display_name,
    "Сводка");
  // Intra-cluster links vanish; cross-cluster links aggregate with a count.
  const fan = collapsed.graph.edges.get("agg:cluster:n2|cluster:kind:artifact|derived_from");
  assert.equal(fan.edge_count, 2);
  assert.deepEqual(
    [...collapsed.graph.edges.values()].map(edge => edge.edge_count ?? 1).sort((a, b) => a - b),
    [1, 1, 1, 2],
  );
  assert.deepEqual([...collapsed.clusters.keys()].sort(), ["kind:artifact", "n2", "n6"]);
  const expanded = clusterGraph(input, new Set(["n2"]));
  assert.deepEqual([...expanded.graph.nodes.keys()].sort(),
    ["cluster:kind:artifact", "cluster:n6", "n0", "n1", "n2", "n3", "n4", "n5"]);
  // Opening the hypothesis collapses nothing else: real links inside, the
  // claim and the artifact bucket stay folded and keep their aggregated links.
  assert.equal(expanded.graph.edges.size, 8);
  assert.equal(expanded.clusters.size, 2);
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
