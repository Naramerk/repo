# Source-addressed Evidence projections

Tool-provenance artifacts remain in CAS unchanged. JSON parsing has independent
byte, depth and node safety budgets; a report display limit is not a parse limit.
Invalid JSON, duplicate object keys, invalid Unicode, nonfinite/overflowing
numbers and exhausted parse budgets produce reference-only coverage. Mixed
coder logs plus JSON remain invalid: this projection does not extract substrings.

The journal's ToolOutput envelope and MCP CallToolResult select machine-readable
`structuredContent` when available. Its duplicate `content[].text` is not parsed
or projected ahead of the structured result. Selection is by protocol shape,
not scenario, tool name, scientific keywords or model-authored summaries.
Plain application JSON is projected from its root. MCP error/text-only results
remain reference-only rather than being treated as parsed structured science.

Required check EvidenceRefs select source pointers first, including observations
from failed prediction checks. Selectors, receipt digests and computed effects
are not trusted values: every displayed fact is re-resolved from the actual CAS
bytes. Stored references contain the original artifact digest and exact RFC6901
pointer. Decimal tokens retain their source representation. This projection
neither validates scientific truth nor changes claim countability or stance.

Each selected scalar has its own bounded Evidence node and source reference.
This prevents the report digest's per-field display limit from discarding the
tail of a large compound statement. The original artifact-level Evidence node
records selected source scope, coverage, omitted fields, unrepresented required
references and refusal reason. Output facts and required selectors are bounded;
long values are omitted with incomplete coverage, never silently clipped into
new apparent values. Source arrays are not fabricated into summary statistics.

Reports still consume graph Evidence only. Omitted source rows remain available
through the artifact locator; a partial projection must not be described as full
source coverage. Historical journal records are not rewritten and this change
alone does not demonstrate end-to-end scientific scenario readiness.
