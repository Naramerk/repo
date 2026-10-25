#!/usr/bin/env bash
# Idempotent install of CodeGraph + LeanCTX CLI pins for CoScientist agents.
# Safe to re-run. Does not replace Python/sandbox CI deps — layer this after them.
set -euo pipefail

PIN_CODEGRAPH="${COSCIENTIST_CODEGRAPH_VERSION:-1.6.2}"
PIN_LEANCTX="${COSCIENTIST_LEANCTX_VERSION:-3.11.4}"
PREFIX="${COSCIENTIST_CONTEXT_TOOLS_PREFIX:-$HOME/.local/share/coscientist-context-tools}"
BIN="$PREFIX/node_modules/.bin"

mkdir -p "$PREFIX"
export PATH="$BIN:$PATH"

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "setup_context_tools: node/npm required" >&2
  exit 1
fi

need_codegraph=1
if command -v codegraph >/dev/null 2>&1; then
  if codegraph --version 2>/dev/null | grep -q "$PIN_CODEGRAPH"; then
    need_codegraph=0
  fi
fi

need_lean=1
if command -v lean-ctx >/dev/null 2>&1; then
  if lean-ctx --version 2>/dev/null | grep -q "$PIN_LEANCTX"; then
    need_lean=0
  fi
fi

if [[ "$need_codegraph" -eq 1 || "$need_lean" -eq 1 ]]; then
  # Write package.json then install pins (avoids floating ^ ranges).
  python3 - "$PREFIX" "$PIN_CODEGRAPH" "$PIN_LEANCTX" <<'PY'
import json, pathlib, sys
prefix, cg, lc = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
pkg = {
    "name": "coscientist-context-tools",
    "private": True,
    "dependencies": {
        "@colbymchenry/codegraph": cg,
        "lean-ctx-bin": lc,
    },
}
(prefix / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")
PY
  npm --prefix "$PREFIX" install --no-fund --no-audit
fi

# Ensure workspace is readable by lean-ctx path jail when present.
if [[ -d "${COSCIENTIST_REPO_ROOT:-$PWD}" ]]; then
  lean-ctx allow-path "${COSCIENTIST_REPO_ROOT:-$PWD}" >/dev/null || true
fi

echo "setup_context_tools: PATH export → $BIN"
echo "setup_context_tools: codegraph → $(codegraph --version 2>&1 | tr '\n' ' ')"
echo "setup_context_tools: lean-ctx → $(lean-ctx --version 2>&1 | tr '\n' ' ')"

# Optional: index current repo if caller is inside a checkout.
if [[ "${COSCIENTIST_CONTEXT_INDEX:-0}" == "1" && -d .git ]]; then
  if [[ ! -d .codegraph ]]; then
    codegraph init -y "$PWD"
  else
    codegraph sync "$PWD"
  fi
  codegraph status "$PWD" | head -40
fi
