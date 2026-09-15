"""Run the upload server on the Pi.

    python -m server --root Database --token <event-token> --host 0.0.0.0

Binds 0.0.0.0 by default so the other machines on the switch can reach it; the
segment is isolated, so that is the whole of the exposure.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn

from server.app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="server", description="Scouting upload server")
    parser.add_argument("--root", default="Database", type=Path, help="competition data root")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument(
        "--token",
        default=os.environ.get("SCOUTING_TOKEN", ""),
        help="shared event token; defaults to $SCOUTING_TOKEN",
    )
    args = parser.parse_args()

    if not args.token:
        parser.error("no token: pass --token or set SCOUTING_TOKEN")

    root = args.root.resolve()
    print(f"serving {root} on http://{args.host}:{args.port}")
    uvicorn.run(create_app(root, args.token), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
