"""bench/lab/driver/webdrive.py — web-lane driver (gap 3: web lane parity).

Old-system contract (``webdrive.py`` mode=web over HTTP+WS, runs_final_20261023
``weblane.sh``): send the task over the web lane, collect status events while
the study processes, receive the final response, all bounded by a timeout;
write a JSON-lines event log plus a summary JSON ``{elapsed_s, n_events,
final}``. Transport mapping onto the new API (same observable contract):

- old WS ``chat_message`` send  -> ``POST /studies/{id}/run`` (``task_text`` +
  the exact CLI config refs: llm-config path, catalog path, profile name,
  model pins, tool-surface paths, budgets — required-explicit, no defaults);
- old processing/idle statuses  -> ``WS /studies/{id}/stream?after=0`` frames
  (the study journal ``EventWire`` rows);
- old ``final_response``        -> the ``StudyFinalized`` verdict event plus
  ``GET /studies/{id}/run`` (run status + the ``run_study`` summary).

``httpx`` + ``websockets`` are runtime-only deps supplied by
``run_web.sh`` (``uv run --with``); imports are lazy so the pure helpers
below stay importable without them (repo dependency surface unchanged).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TextIO

# Exit codes (mirrors the old driver's contract: nonzero unless fulfilled).
EXIT_OK = 0
EXIT_INPUT_REFUSED = 2
EXIT_RUN_REFUSED = 3
EXIT_TIMEOUT = 4

_FINAL_EVENT_TYPE = "StudyFinalized"
# Retry cadence while waiting for the server to accept connections.
_CONNECT_RETRY_SLEEP_S = 0.25


def is_final_event(event: Mapping[str, Any]) -> bool:
    """True for the terminal verdict event (``StudyFinalized``)."""
    return event.get("type") == _FINAL_EVENT_TYPE


def verdict_of(event: Mapping[str, Any]) -> str | None:
    """Verdict from a ``StudyFinalized`` payload, when present."""
    if not is_final_event(event):
        return None
    payload = event.get("payload")
    if isinstance(payload, Mapping):
        verdict = payload.get("verdict")
        if isinstance(verdict, str) and verdict:
            return verdict
    return None


def run_body(config: Mapping[str, Any], task_text: str) -> dict[str, Any]:
    """POST /studies/{id}/run body: config refs + the task text (verbatim)."""
    body: dict[str, Any] = dict(config)
    body["task_text"] = task_text
    return body


def ws_stream_url(base_url: str, study_id: str) -> str:
    """HTTP base URL -> WS stream URL for one study (http->ws, https->wss)."""
    ws_base = base_url.rstrip("/")
    if ws_base.startswith("https://"):
        ws_base = "wss://" + ws_base[len("https://") :]
    elif ws_base.startswith("http://"):
        ws_base = "ws://" + ws_base[len("http://") :]
    return f"{ws_base}/studies/{study_id}/stream?after=0"


def build_summary(
    *,
    study_id: str,
    elapsed_s: float,
    events: Sequence[Mapping[str, Any]],
    final_event: Mapping[str, Any] | None,
    run_status: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Summary JSON: elapsed, n_events, final (old webdrive_result.json shape)."""
    final: dict[str, Any] = {}
    if final_event is not None:
        final["verdict"] = verdict_of(final_event)
    if run_status is not None:
        final["run_status"] = run_status.get("run_status")
        summary = run_status.get("summary")
        if isinstance(summary, Mapping):
            final["summary"] = dict(summary)
        detail = run_status.get("detail")
        if isinstance(detail, str):
            final["detail"] = detail
    return {
        "study_id": study_id,
        "elapsed_s": round(elapsed_s, 3),
        "n_events": len(events),
        "final": final,
    }


class _InputRefused(Exception):
    """Typed input refusal (unreadable/invalid task file or config)."""


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="webdrive")
    parser.add_argument("--base-url", type=str, required=True, help="API base URL, e.g. http://127.0.0.1:8421")
    parser.add_argument("--study-id", type=str, required=True)
    parser.add_argument("--task-file", type=Path, required=True, help="Task text file (the study request)")
    parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="JSON with the POST /run config refs (everything but task_text)",
    )
    parser.add_argument("--timeout-s", type=float, required=True, help="Overall wall bound (status + final)")
    parser.add_argument("--events-out", type=Path, required=True, help="JSON-lines event log path")
    parser.add_argument("--summary-out", type=Path, required=True, help="Summary JSON path")
    parser.add_argument(
        "--connect-retries",
        type=int,
        required=True,
        help="POST /run connect retries while the server is starting up",
    )
    return parser.parse_args(argv)


def _read_inputs(args: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """Task text + config; typed ``_InputRefused`` on unreadable/invalid input."""
    try:
        task_text = args.task_file.read_text(encoding="utf-8").strip()
        config_raw = json.loads(args.config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise _InputRefused(f"input_refused:{type(exc).__name__}:{exc}") from exc
    if not task_text:
        raise _InputRefused("input_refused:empty_task_file")
    if not isinstance(config_raw, dict):
        raise _InputRefused("input_refused:config_must_be_json_object")
    return task_text, config_raw


async def _post_run(args: argparse.Namespace, body: Mapping[str, Any]) -> tuple[int, str]:
    """POST /studies/{id}/run with connect retries; returns (status_code, text)."""
    import httpx

    url = f"/studies/{args.study_id}/run"
    last_error = "unknown"
    for attempt in range(args.connect_retries + 1):
        try:
            async with httpx.AsyncClient(base_url=args.base_url, timeout=30.0) as client:
                response = await client.post(url, json=body)
                return response.status_code, response.text
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < args.connect_retries:
                await asyncio.sleep(_CONNECT_RETRY_SLEEP_S)
    return 0, f"connect_failed:{last_error}"


async def _get_run(args: argparse.Namespace) -> dict[str, Any] | None:
    """GET /studies/{id}/run (run status + summary); None on transport error."""
    import httpx

    try:
        async with httpx.AsyncClient(base_url=args.base_url, timeout=10.0) as client:
            response = await client.get(f"/studies/{args.study_id}/run")
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    loaded = json.loads(response.text)
    return loaded if isinstance(loaded, dict) else None


async def _get_run_terminal(args: argparse.Namespace) -> dict[str, Any] | None:
    """Poll GET run until the run task is terminal (bounded: 10s, 50ms step)."""
    deadline = time.monotonic() + 10.0
    while True:
        status = await _get_run(args)
        if status is None:
            return None
        if status.get("run_status") != "running":
            return status
        if time.monotonic() >= deadline:
            return status
        await asyncio.sleep(0.05)


async def _collect_events(
    args: argparse.Namespace,
    events_out: TextIO,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, bool]:
    """Stream WS events until the final event, timeout, or close.

    Returns ``(events, final_event, timed_out)``.
    """
    import websockets

    deadline = time.monotonic() + args.timeout_s
    events: list[dict[str, Any]] = []
    final_event: dict[str, Any] | None = None
    timed_out = False
    url = ws_stream_url(args.base_url, args.study_id)
    # open_timeout bounded by the remaining budget; the server may still be
    # creating the journal when POST /run returns, hence retries.
    while final_event is None and time.monotonic() < deadline:
        try:
            async with websockets.connect(
                url,
                open_timeout=max(1.0, deadline - time.monotonic()),
                close_timeout=1.0,
            ) as ws:
                while final_event is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0.0:
                        timed_out = True
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        timed_out = True
                        break
                    event = json.loads(raw)
                    if not isinstance(event, dict):
                        continue
                    events.append(event)
                    events_out.write(json.dumps(event, ensure_ascii=False) + "\n")
                    events_out.flush()
                    if is_final_event(event):
                        final_event = event
        except websockets.WebSocketException:
            # Journal not journal-visible yet (or transient close): retry
            # within the budget rather than failing the lane.
            await asyncio.sleep(_CONNECT_RETRY_SLEEP_S)
    return events, final_event, timed_out


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    started = time.perf_counter()

    task_text: str
    try:
        task_text, config = _read_inputs(args)
    except _InputRefused as exc:
        print(f"webdrive: {exc}", file=sys.stderr)
        return EXIT_INPUT_REFUSED

    body = run_body(config, task_text)
    status_code, text = asyncio.run(_post_run(args, body))
    events: list[dict[str, Any]] = []
    final_event: dict[str, Any] | None = None
    refused_detail: str | None = None
    if status_code != 200:
        refused_detail = f"post_run_status_{status_code}:{text[:400]}"
    else:
        with args.events_out.open("w", encoding="utf-8") as events_out:
            events, final_event, timed_out = asyncio.run(_collect_events(args, events_out))
        if final_event is None and timed_out:
            refused_detail = "stream_timeout_before_final_response"

    run_status = asyncio.run(_get_run_terminal(args))
    summary = build_summary(
        study_id=args.study_id,
        elapsed_s=time.perf_counter() - started,
        events=events,
        final_event=final_event,
        run_status=run_status,
    )
    if refused_detail is not None:
        summary["final"]["detail"] = refused_detail
    args.summary_out.write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False))

    final = summary["final"]
    if final_event is not None and final.get("run_status") == "done" and final.get("verdict") == "fulfilled":
        return EXIT_OK
    if refused_detail == "stream_timeout_before_final_response" or (
        refused_detail is None and final_event is None
    ):
        return EXIT_TIMEOUT
    return EXIT_RUN_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
