# Research graph UX — spec (feature/research-graph-ux)

Readable console research graph. Base: origin/main @ 23d6f8bd.
Scope: `graph.js`, `app.js`, `workflow.js`, `css/console.css`, `index.html`.
No new JS deps. Human-decision comments live in `feature/human-decision-comments`.

1. **Layout.** Keep provenance serpentine (`layoutEvidenceGraph`); no second layout engine.
2. **Edge labels.** Hidden by default; show on edge hover and on incident edges of the selected node.
3. **Edge color + legend.** `EDGE_COLORS` drives stroke and the toolbar legend; Russian labels include `addresses` and other registry kinds.
4. **Human labels.** `nodeLabel` prefers `display_name` / `purpose` / `summary` / `filename` / `media_type`; full id only in `<title>` and details.
5. **Kind filter.** Collapsible «Типы узлов» checkboxes on top of the science/all focus select; default «Научный граф» unchanged.
6. **Focus neighbours.** Selection dims non-neighbours and lights incident edges; `WorkflowView` opts out.
7. **«К выбранному».** Centers the selected card; without selection acts as «Вписать».
8. **Details.** Kind, human label, muted full id, collapsible raw record, clickable relations.
9. **Clusters.** `assignClusters` / `clusterGraph` (pure, DOM-free): a cluster
   anchors on a hypothesis (claim node as fallback) and keeps the linked
   criterion/evidence/artifact cards inside; nodes not attached to any head
   collapse into one bucket per kind; study/question stay outside as hubs.
   A collapsed cluster is one card «… · N» (dashed border), cross-cluster edges
   aggregate per (src, kind, dst) with an ×N count; clusters start collapsed,
   a click on the card toggles. Kind checkboxes keep working on top.
10. **Service artifacts.** The science focus («Научный граф») hides digest-only
   artifacts — no display field among `display_name` / `purpose` / `summary` /
   `filename` / `media_type` / `title` / `name` / `text`
   (`focusGraph(…, {showServiceArtifacts})`). The «Показать служебные» toolbar
   checkbox, «Все узлы» and every kind focus keep them visible. Demo:
   `coscientist/api/web/demo-graph.html` (served at `/ui/demo-graph.html`).

Layout note: backward serpentine bands start from the full `WRAP_AFTER` track (not
the remaining width), so a partial last band hugs the right edge and a wrapped
chain continues downward without the jump back to the left.
