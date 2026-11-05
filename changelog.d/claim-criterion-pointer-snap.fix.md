Claim derivation resolves a `checked_against` receipt/output pointer to the
task's sole check or plan criterion. The claims-gate repair contract allows a
receipt call_id or output name as `checked_against`; such a pointer named none
of the attempt's checks, so every claim carried `unknown_role`/`unassessed`
and never counted (live №29: judge prediction check MET, all claims excluded).
An unresolvable pointer now falls through to the documented sole-check /
sole-criterion resolution; multi-check tasks without a declared criterion
stay fail-closed (`unknown_role`).
