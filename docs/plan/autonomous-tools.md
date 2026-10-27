# Autonomous MCP execution boundary

Status: implemented, offline CLI/mock verification; independent product review and
full release gates pending. Architecture independently reviewed k=0. Base `7aa37d9`.

The existing adapter will select a typed call or finish action, observe the actual
result, and repeat within existing call/token/tool/deadline limits. This replaces
operator argument recipes for the automatic mode; fixed recipes remain an explicit
compatibility input. Executor and researcher share this loop. Coder generation,
adaptive plan expansion and hypothesis-to-test binding are separate incomplete work.

`TaskIntent.output_names` declares output identities independently of optional tool
hints. Empty names retain historical hint-derived outputs. Declared outputs keep
the existing required rubric; scientific questions still need substantive catalogue
checks. Output names and check bindings survive the existing continue roundtrip.

## Authorization

Full pinned MCP bindings remain intact because discovery validates their exact set.
An independent trusted `allowed_tool_ids` mask uses qualified `server:tool` IDs:
`None` retains all trusted bindings, an empty sequence denies all, a sequence selects
a subset. Neither discovery nor model output supplies effect or fetch authority.
Nonempty accepted task hints further restrict this surface; absent hints do not
require an operator recipe. Researcher role restrictions still apply.

The existing closed loader registers trusted MCP declarations before the first
runtime freezes the process registry. Reopening an identical/subset configuration
is permitted; a new or changed declaration after freeze refuses and requires a
restart. A server loads the union of pinned declarations before opening studies.
There is no registry thaw or additional effect engine.

## Runtime boundary

Agent contracts cannot import the tools contract or registry. The CLI therefore
injects one runtime tool executor protocol into the existing adapter. It validates
study/task/attempt binding, open state, lease, cancellation and deadline before
dispatch, then calls existing `invoke_tool` with the actual bound tool, writer and
CAS. Arguments are validated against actual discovery JSON Schema locally; remote
references are rejected before RPC. Effect/taint/HITL policy remains authoritative.
The automatic input includes the original journaled study request for both roles,
including the literature stage; a generic stage label is not substituted for it.

Every invocation has a distinct attempt-scoped call sequence, including repeated
calls of the same tool. All actual outputs are retained with their CAS digest and
receipt identity before another LLM step. MCP `isError` outputs are retained as
failed observations; transport failures do not invent data. Model values retain
agent provenance. A finish action explicitly maps expected output names to actual
receipt IDs; unknown or ambiguous mappings refuse rather than selecting the latest
tool result. Literature citations can trace through every recorded result instead
of an overwritten per-tool dictionary.
Failed receipts cannot satisfy required outputs. Fixed recipes preserve their former
failure/retry behavior while retaining the actual failed response in CAS. Receipt
metadata uses the recorded ToolCalled reference, including its actual argument
digest and timestamps. Recording a result verifies the receipt/body/digest binding
instead of creating replacement provenance.

The implementation does not promise exactly-once external provider execution,
durable monetary accounting, live scientific correctness or autonomous coder jobs.
Existing global LLM budget limitations are not disguised as hard spending ceilings.

## Verification

Offline mocked MCP/LLM tests must demonstrate dependent search/read calls, repeated
tool preservation, explicit finish selection, empty-hint execution, allow-mask
denial, invalid/remote-ref schema refusal before dispatch, budget/deadline/cancel
stops, genuine failed observations, and unchanged scientific acceptance gates.
Run targeted and full tests, ruff, mypy, import contracts, and post-commit heuristic
audit. An independent exact-tip review is required before release.

Context preflight: CodeGraph `1.6.2`, LeanCTX `3.11.4`; own index init, queries
`invoke_tool`, callers `invoke_tool`, `ensure_core_tools_ready`, `record_executor_route`,
`derive_task_specs`, `TaskIntent`, impact `ExecutorRouteIn`, and anchored LeanCTX
source reads all executed. Focused CLI/agent suite: 314 passed, one slow deselected
(`/tmp/csn-autotools-focused-final.log`, exit 0). No paid or live calls were made.
