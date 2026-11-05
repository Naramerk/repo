Every terminal run now leaves report documents, even when the LLM is
unreachable. Two floors close the live gaps: (1) a typed refusal that ends a
run before finalize (plan-derivation refuse) left the study CAS with zero
documents — the CliRefuse boundary now writes a deterministic `report.md`
naming the refusal and the stages that ran; (2) a post-finalize report-lane
transport failure left only the thin minimal report.md — the lane now also
writes the full document set (science report, GOST ТЗ/НИР) assembled
deterministically from the graph digest, with an explicit unavailability
note in the prose slots and verbatim graph facts. Both floors are
best-effort and never shadow the study's own typed outcome.
