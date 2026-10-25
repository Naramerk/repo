"""Minimal MCP streamable-HTTP smoke probe (deps: httpx only, via the `mcp` extra).

Performs the full client handshake against one server URL and prints raw results:

1. ``initialize`` (reads ``Mcp-Session-Id`` from the response)
2. ``notifications/initialized`` notification
3. ``tools/list``
4. optional single ``tools/call`` (``--call TOOL [--args-json JSON]``)

Mirrors the repo client's wire behavior (``coscientist/mcp/http_transport.py``):
``Accept: application/json, text/event-stream``, ``MCP-Protocol-Version`` header,
SSE ``data:`` parsing. Use sparingly against real internal servers.

Usage (from repo root):

    uv run --extra mcp python bench/lab/mcp/probe.py http://host:port/mcp
    uv run --extra mcp python bench/lab/mcp/probe.py URL --call tool_name --args-json '{"k": 1}'

Exit codes: 0 = initialize + tools/list (and the call, when requested) succeeded;
1 = transport/HTTP failure; 2 = JSON-RPC error response.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"


class ProbeError(Exception):
    """Transport / HTTP-level failure (exit 1)."""


class RpcError(Exception):
    """JSON-RPC error response (exit 2)."""


def _headers(session_id: str | None, protocol_version: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": protocol_version,
    }
    if session_id is not None:
        headers["Mcp-Session-Id"] = session_id
    return headers


def _parse_sse_data(line: str) -> str | None:
    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :]
    return payload[1:] if payload.startswith(" ") else payload


def post_rpc(
    client: httpx.Client,
    url: str,
    payload: dict[str, Any],
    *,
    session_id: str | None,
    protocol_version: str,
    timeout_s: float,
) -> tuple[dict[str, Any] | None, str | None, int, str]:
    """POST one JSON-RPC message; return (message, new_session_id, status, content_type).

    Handles both ``application/json`` and ``text/event-stream`` (SSE) responses.
    For SSE, streams until the first JSON-RPC message whose ``id`` matches (or any
    response when the payload is a notification) and stops reading there.
    """
    body = json.dumps(payload)
    with client.stream(
        "POST",
        url,
        headers=_headers(session_id, protocol_version),
        content=body,
        timeout=timeout_s,
    ) as response:
        status = response.status_code
        new_session = response.headers.get("mcp-session-id") or session_id
        content_type = response.headers.get("content-type", "")
        media = content_type.split(";", 1)[0].strip().lower()
        if status >= 400:
            text = response.read().decode("utf-8", "replace")
            raise ProbeError(f"HTTP {status} ({media}): {text[:2000]}")
        if media == "application/json":
            raw = response.read().decode("utf-8")
            if not raw.strip():  # 202 notification ack: empty body, no message
                return None, new_session, status, media
            return json.loads(raw), new_session, status, media
        if media != "text/event-stream":
            raise ProbeError(f"unsupported Content-Type: {content_type!r}")
        data_lines: list[str] = []
        for line in response.iter_lines():
            if line == "":
                if data_lines:
                    event = "\n".join(data_lines)
                    data_lines = []
                    message = json.loads(event)
                    if "method" in message:  # server request / notification: skip
                        continue
                    return message, new_session, status, media
                continue
            piece = _parse_sse_data(line)
            if piece is not None:
                data_lines.append(piece)
        if data_lines:  # final event without trailing blank line
            return json.loads("\n".join(data_lines)), new_session, status, media
        return None, new_session, status, media


def unwrap_result(message: dict[str, Any] | None, label: str) -> dict[str, Any]:
    if message is None:
        raise ProbeError(f"{label}: no JSON-RPC response on SSE stream")
    if "error" in message:
        raise RpcError(f"{label}: JSON-RPC error {message['error']!r}")
    result = message.get("result")
    if not isinstance(result, dict):
        raise ProbeError(f"{label}: result is not an object: {message!r}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url", help="MCP streamable-HTTP endpoint, e.g. http://10.32.11.45:7336/mcp")
    parser.add_argument("--call", help="tool name for a single tools/call after tools/list")
    parser.add_argument("--args-json", default="{}", help='args for --call as JSON (default "{}")')
    parser.add_argument("--timeout-s", type=float, default=30.0, help="per-request timeout")
    args = parser.parse_args()

    protocol_version = DEFAULT_PROTOCOL_VERSION
    session_id: str | None = None
    next_id = 1

    try:
        args_json = json.loads(args.args_json)
        if not isinstance(args_json, dict):
            print("--args-json must be a JSON object", file=sys.stderr)
            return 1
    except json.JSONDecodeError as exc:
        print(f"--args-json is not valid JSON: {exc}", file=sys.stderr)
        return 1

    with httpx.Client(trust_env=False, follow_redirects=False) as client:
        try:
            message, session_id, status, media = post_rpc(
                client,
                args.url,
                {
                    "jsonrpc": "2.0",
                    "id": next_id,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": protocol_version,
                        "capabilities": {},
                        "clientInfo": {"name": "coscientist-probe", "version": "0.1.0"},
                    },
                },
                session_id=None,
                protocol_version=protocol_version,
                timeout_s=args.timeout_s,
            )
            next_id += 1
            result = unwrap_result(message, "initialize")
            print("=== initialize (raw result) ===")
            print(json.dumps(result, indent=2, ensure_ascii=False))
            negotiated = result.get("protocolVersion")
            if isinstance(negotiated, str) and negotiated:
                protocol_version = negotiated
            print(f"# session_id={session_id!r} negotiated_protocol={protocol_version!r}")

            post_rpc(  # notifications/initialized: expect 202 + empty body
                client,
                args.url,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                session_id=session_id,
                protocol_version=protocol_version,
                timeout_s=args.timeout_s,
            )

            message, session_id, _, _ = post_rpc(
                client,
                args.url,
                {"jsonrpc": "2.0", "id": next_id, "method": "tools/list", "params": {}},
                session_id=session_id,
                protocol_version=protocol_version,
                timeout_s=args.timeout_s,
            )
            next_id += 1
            tools_result = unwrap_result(message, "tools/list")
            print("=== tools/list (raw result) ===")
            print(json.dumps(tools_result, indent=2, ensure_ascii=False))
            names = [t.get("name") for t in tools_result.get("tools", [])]
            print(f"# tools ({len(names)}): {names}")

            if args.call:
                message, _, _, _ = post_rpc(
                    client,
                    args.url,
                    {
                        "jsonrpc": "2.0",
                        "id": next_id,
                        "method": "tools/call",
                        "params": {"name": args.call, "arguments": args_json},
                    },
                    session_id=session_id,
                    protocol_version=protocol_version,
                    timeout_s=args.timeout_s,
                )
                call_result = unwrap_result(message, f"tools/call {args.call}")
                print(f"=== tools/call {args.call} args={json.dumps(args_json)} (raw result) ===")
                print(json.dumps(call_result, indent=2, ensure_ascii=False))
        except RpcError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except (ProbeError, httpx.HTTPError, json.JSONDecodeError) as exc:
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
