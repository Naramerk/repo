# Study Console

The API serves a local, read-only research console at `/ui/`. Start it with
`python -m coscientist.api.serve --data-root PATH --catalog CATALOG --host 127.0.0.1 --port 8455`.
The launcher needs FastAPI, uvicorn and a WebSocket backend. It uses the same journals
as CLI and API runs; it never starts a second runner.

Studies include CLI IDs and API IDs. A resumable WebSocket streams journal deltas;
the browser folds tasks, artifacts and graph nodes. Graph columns group node kinds,
not causal depth: research links may be cyclic or arrive after their nodes. Selecting
a node shows its attributes and directed links. Fit follows new data and container
resize until the user pans/zooms; the Fit button resumes automatic fitting.

Artifact identity comes from journaled `ArtifactRegistered` or artifact-kind
`GraphNodeUpserted` descriptors. Display names are metadata, never filesystem paths.
CAS requests validate containment and SHA-256 before returning bytes. Scientific
reports without graph nodes and CLI evidence without registration events both work.
Text/Markdown is rendered without executing HTML; images preview via blob URLs,
and all formats retain an original download. The document metadata states when its
source stamp is unavailable rather than inventing provenance.

This first surface is read-only. HITL actions, starting/continuing research, user
management and scientific-answer acceptance are separate capabilities. A green
execution verdict shown here is the recorded historical verdict, not a new audit.

Checks: `uv run --extra dev --extra mcp pytest tests/api/test_console.py -q`,
`node --test tests/web/model.test.mjs`; full repository gates still apply.
