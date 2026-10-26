"""``python -m coscientist.api.serve`` — uvicorn entry for the study API.

Every option is an explicit required flag (no env/defaults). ``uvicorn`` is a
runtime-only dependency supplied by the launcher (``bench/lab/driver/
run_web.sh`` via ``uv --with``); the repo dependency surface is unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from coscientist.api.app import bind_app_deps, create_app
from coscientist.api.run import require_run_wait_s
from coscientist.api.stream import require_poll_interval_s
from coscientist.checks.catalog import load_catalog
from coscientist.core.claims.graph import default_execution_registry
from coscientist.core.events.clock import SystemClock


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="coscientist-api-serve")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument(
        "--catalog",
        type=Path,
        required=True,
        help="Catalog JSON for views/cancel (run requests pin their own catalog ref)",
    )
    parser.add_argument("--host", type=str, required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--stream-poll-interval-s", type=float, required=True)
    parser.add_argument(
        "--run-wait-s",
        type=float,
        required=True,
        help="Bound for a POSTed run to become journal-visible (and for interrupt)",
    )
    args = parser.parse_args(argv)
    app = create_app(
        bind_app_deps(
            args.data_root,
            catalog=load_catalog(args.catalog, kind_registry=default_execution_registry()),
            clock=SystemClock(),
            stream_poll_interval_s=require_poll_interval_s(args.stream_poll_interval_s),
            stream_sleep=asyncio.sleep,
            run_wait_s=require_run_wait_s(args.run_wait_s),
        )
    )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
