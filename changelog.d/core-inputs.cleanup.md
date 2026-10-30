inputs: in value types of snapshot/claim/result dicts, replaced `Any` with
`object` (`verdict_binding_digest`, `bind_inputs`, `claim_nodes_from_attempt`,
`collect_bindable_claims`). Annotation-only; no behavior change; `object`
accepts every value that reaches these parameters.

Counters (occurrences / вхождения, coscientist/): `Any` −5, `getattr` 0,
`cast(` 0, `type: ignore` 0, `audit-ok` 0.

Kept byte-for-byte as base (rule 19/21: no annotation stricter than real sources):
`graph: Any`, `template: Any`, `catalog: Any` and the `getattr` reads on
`template` / `ref` / `tmpl` in `pinned_criterion_output` /
`validate_plan_criteria` — callers include `agents/derive.py` and test
fixtures whose objects are not proven to match `GraphStore` /
`RequirementTemplate` / `Catalog`.
