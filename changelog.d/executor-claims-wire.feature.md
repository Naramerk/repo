feature(agents): the executor wire schema carries model-observed claims (#273 substrate)
- ``coscientist/agents/schemas.py``: ``ExecutorRouteOut.claims`` (defaulted
  tuple of ``ExecutorClaimDraft``: optional id, required target
  ``question:qX``/``hypothesis:HX``, nullable checked_against/reason/
  premise_idx). The automatic loop echoes this schema as the finish
  contract, so the executor model can finally OBSERVE claims about the
  questions and hypotheses its task addresses — the live chain produced
  zero claim nodes because the wire schema had no claims field at all.
  Defaulted (not strict-required): recorded cassettes predate the field,
  and the live automatic path only echoes the schema as text.
- ``coscientist/cli/journal.py``: ``record_executor_route`` passes the
  claims through to the recorded ``AgentTaskOutput`` as raw dicts — the
  graph fold (``claim_nodes_from_attempt``) applies the attr whitelist and
  drops unknown targets; the mapping neither invents nor filters.
- Open verification (run 11): whether core-derived ``criterion_status``
  counts claims on tasks whose catalogue criteria were derive-dropped —
  the honest fallback is rubric-derived status or the #273 route closure.
