# Research workspace reference and scope

User reference: `/Users/nargiza/Downloads/borshchevik-65s.mp4` (65s, 1920×1080,
24fps). Inspected key frames across 0–64s and full-size frames at 24s/48s/64s.
The video is a visual reference; its annotations, numbers, agents and conclusions
are not inputs or evidence for new studies.

Adopted interaction principles:

- Stable left navigation, a light workspace and a compact study/context header.
- A task dependency canvas as the primary study view, with readable stage cards.
- A right inspector for the selected task's goal, actual attempts, checks and CAS
  materials. Reuse the existing ResultsView rather than a second evaluator.
- Keep the scientific provenance graph distinct from the task workflow. Show only
  journaled tasks and their explicit `spec.depends_on`; disclose missing/ambiguous
  dependencies and cyclic/downstream unresolved tasks without creating links.
- Keep events and technical identities in diagnostic views/disclosures. Selection
  and keyboard focus survive streaming updates; changing studies resets context.
- Fit on resize until manual pan/zoom, then preserve manual navigation. Use the
  same graph viewport implementation, with separate SVG identifier namespaces.
- Render scientific provenance as readable cards and labelled orthogonal links,
  with full node records and navigation to recorded neighbours/source artifacts.
- Show accepted plan snapshots, real task states and expansion/refusal history;
  distinguish a human-approved candidate from actual `PlanAccepted`. Never infer
  acceptance from a successful PLAN task or invent ongoing LLM generation.
- Provide full-width document reading and the complete source text/size alongside
  the verified downloadable original. Historical generator omissions remain gaps.
- Make navigation groups collapsible and expose a persistent new-study entry.
  Launch still requires a server-owned trusted preset; empty presets cannot launch.

The reference's agent subgraphs, methodology inspector, chart-rich scientific
comparison tables and integrated report preview are not reproduced with fictional
data. They require actual backend records and are follow-up capabilities.

Verification uses pure task-projection tests, existing browser-model/HITL/preset
tests, real historical journals in a local browser, keyboard and responsive checks,
independent exact-tip review and repository CI. No new paid research is needed to
verify presentation; scientific readiness requires separate live acceptance.
