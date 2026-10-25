# Coordination fragment (embedded for agents without Mac bootstrap)

Source: coordinator checkpoint after 2026-10-25 restore. The original
`AGENTS.coordination.fragment.md` / `BOOTSTRAP-HANDOFF.json` bundle was **not**
available in Cloud VM; this fragment captures the binding rules to pass into
assignments.

## Dual tools

Every agent must use **CodeGraph** (`@colbymchenry/codegraph@1.6.2`) and **LeanCTX**
(`lean-ctx-bin@3.11.4`) together. Install via `scripts/setup_context_tools.sh`.
Per-checkout CodeGraph index; shared CLI prefix
`$HOME/.local/share/coscientist-context-tools`. Preflight: versions + status + one
substantive query + one source read. Record in handoff.

## Continuity

- One author per branch; check IDLE before takeover.
- Coordinator owns `docs/coordination/STATE.md`.
- Do not recreate merged #149/#151/#152.
- Do not merge without explicit user permission.
- Return structured handoff (status, SHA, checks+exit codes, tool evidence, next step).

## Critical path reminder

`#146` → `#147` launch composition; `#138` → `#65` MCP exact resolve + provenance;
`#150` sandbox coder; then production profile + durable fork; then legacy scenarios
when fixtures exist (Mac legacy paths are not mounted).
