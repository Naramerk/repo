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
