"""`python -m council.server` — what the detached daemon process actually runs."""

from __future__ import annotations

import argparse

from .daemon import DEFAULT_PORT, serve
from .security import new_token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="council.server", description="Council daemon")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--token",
        default=None,
        help="shared secret; generated and printed if omitted (development only)",
    )
    parser.add_argument(
        "--dev-origin",
        default="",
        help="extra allowed Origin, e.g. http://localhost:5173 for the Vite dev server",
    )
    parser.add_argument(
        "--idle-seconds",
        type=float,
        default=0.0,
        help="exit after this many seconds with no open tab and nothing running. "
        "0 (default) means stay up until stopped.",
    )
    args = parser.parse_args(argv)

    token = args.token or new_token()
    if not args.token:
        print(f"token: {token}")
        print(f"open:  http://127.0.0.1:{args.port}/?token={token}")
    serve(
        port=args.port,
        token=token,
        host=args.host,
        dev_origin=args.dev_origin,
        idle_seconds=args.idle_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
