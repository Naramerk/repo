# Remote coder: first bounded slice

Status: proposed, mock verification only; deployment URL and model policy are not verified.
Base: `4a9b924ef95bc757df7a8a88a9ebda3b53808273`; author branch `lab/remote-coder`.

1. Reuse the legacy OpenHands task service, not the separate shell-only CODE_EXEC service.
2. No local generator, Docker setup, CLI scheduler, second research runner or scientific acceptance changes.
3. New files are `tools/coder/remote*.py`, focused tests, docs and source inventories only.
4. Existing `CoderRequest`, `attach`, JobStore, CLI run and agent adapter remain unchanged in this slice.
5. Future runtime integration must use `invoke_tool(coder)` and the existing JobStore authority.

## Confirmed legacy wire contract

6. Source root: `/Users/nargiza/CoScientist_dev-dev_1-2026-10-22/CoScientist/tools/coder_tools`.
7. `openhands_sandbox.py:1323-1405` builds JSON `POST /api/v1/run` with task, wait and inputs.
8. Submit always sends explicit `new_container=true`; no follow-up or old session attachment is supported.
9. Inputs use exactly url, path, sha256, size, required; this slice does not unpack archives.
10. `openhands_sandbox.py:1517-1583` requires submit response task_id; optional links are not invented.
11. `:479-500` reads `GET /api/v1/status?task_id=...` and the current_task envelope.
12. `:2070-2118` shows cooldown/completed can still carry error or turn_error.
13. `:2346-2382` sends `POST /api/v1/stop?task_id=...`; its acknowledgement is not termination proof.
14. `:2532-2593` streams `GET /api/v1/files/download?task_id=...&path=...`.
15. Wire DTOs parse documented identity/status/error/input-receipt fields and preserve bounded raw bytes.
16. Unknown status or missing required identity is a typed protocol error, never inferred success.

## Request, policy and identity

17. RemoteCoderTaskRequest binds study, attempt, call identity, task and expected output descriptors.
18. Each input binds immutable local artifact_id, sha256, size, destination and required flag.
19. Names are explicit safe workspace-relative paths; duplicate destinations and traversal are refused.
20. No synthetic input, summary-derived URL, fabricated dataset or client filesystem path is accepted.
21. Request digest covers endpoint, task, expected outputs, immutable inputs and policy identity.
22. Expiring input URL is excluded only because its exact artifact digest, size and destination are included.
23. Changing any semantic input or task under an existing call identity is a typed conflict.
24. A required injected policy callback runs before any submit POST and before creating its reservation.
25. No callback or denied/unverified policy means typed refusal with zero task requests.
26. The trusted composition must verify provider/model, allowed quota, tools and budget; this module invents none.
27. Policy evidence is bound to the request/endpoint; a changed policy conflicts with an existing reservation.
28. No actual submit is permitted during this assignment; tests use only fake transport/policy implementations.

## Durable submission metadata

29. SQLite submission store is infrastructure identity metadata, not a duplicate JobStore lifecycle.
30. Reserve one request digest per study/attempt/call before POST, using an atomic unique constraint.
31. Same identity with a bound remote task returns that task without resubmitting.
32. Same identity reserved without a remote task is indeterminate; never automatically POST again.
33. Bind only the actual server-returned task_id, monotonically; conflicting bindings are refused.
34. Store bounded append-only HTTP receipts with actual bytes/digest and operation identity.
35. Secrets/auth headers and expiring input URLs are excluded from durable request metadata and errors.
36. A failed stop, transport timeout or vanished status never deletes the binding.

## Bounded client operations

37. Inject existing `coscientist.mcp.transport.HttpTransport`; no direct httpx import or copied legacy wrapper.
38. Submit, status, stop and download are single bounded operations, with no background polling scheduler.
39. Bound response bytes, nesting, JSON parsing and transport deadlines; reject duplicate JSON keys.
40. Close every response stream, including overflow, invalid JSON, HTTP errors and cancellation.
41. HTTP redirects are refused; service endpoint and auth configuration are server-owned.
42. Download only a declared output path for an actual bound task; cap bytes and verify expected size/hash.
43. Download returns actual bytes and computed digest; later owner integration registers them in study CAS.
44. Successful remote execution, downloaded source and receipts do not establish scientific validity.
45. Stop returns its actual acknowledgement; status must independently confirm cancellation/termination.

## Verification and remaining integration

46. Tests cover concurrent duplicate submit, restart, changed body, refreshed URL and lost submit response.
47. Tests cover policy denial, stale input receipts, digest mismatch and cooldown with turn_error.
48. Tests cover missing status identity, malformed/oversized JSON, redirects and stream cleanup.
49. Tests cover stop uncertainty retaining binding, bounded downloads and cancellation propagation.
50. Full pytest, ruff, mypy, imports, postcommit heuristic audit and own CodeGraph sync remain required.
51. Real URL, OpenAPI, service authentication and model/quota policy require metadata-only verification first.
52. Actual input publishing needs approved remote-reachable URLs; local CAS URLs cannot reach the service.
53. Runtime/JobStore/CAS integration is a separate reviewed task after the MCP writer's shared files stabilize.
54. Operator coder_jobs compatibility remains; this first slice does not silently activate remote execution.
55. No availability, resource isolation or paid-run readiness is claimed from mocked protocol tests.
