"""uploadctl - the whole interface, since nothing here draws a window.

    uploadctl run                 watch outbox/, POST, back off, repeat
    uploadctl status              the tray icon, as text
    uploadctl verify              ask the Pi what it actually holds
    uploadctl retry --rejected    requeue after fixing a bad record

status exits non-zero whenever anything is pending or rejected, so it drops
straight into a shell prompt or `watch -n5 uploadctl status`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from uploader.outbox import Outbox
from uploader.sender import Config, Sender

DEFAULT_CONFIG = Path("uploader.json")


def load_config(path: Path) -> tuple[Config, dict]:
    if not path.exists():
        sys.exit(f"no config at {path} - copy uploader.example.json and edit it")
    raw = json.loads(path.read_text())
    missing = [k for k in ("server", "token", "year", "competition") if k not in raw]
    if missing:
        sys.exit(f"{path} is missing: {', '.join(missing)}")
    config = Config(
        server=raw["server"].rstrip("/"),
        token=raw["token"],
        outbox=raw.get("outbox", "."),
        device_id=raw.get("device_id", "scout"),
        timeout=float(raw.get("timeout", 10.0)),
    )
    return config, raw


def cmd_run(args) -> int:
    config, raw = load_config(args.config)
    print(f"{config.device_id}: watching {Outbox(config.outbox).pending_dir} -> {config.server}")
    sender = Sender(config)
    try:
        sender.run(once=args.once)
    except KeyboardInterrupt:
        print("\nstopped - queued records stay in the outbox", flush=True)
    return 0


def cmd_status(args) -> int:
    config, raw = load_config(args.config)
    outbox = Outbox(config.outbox)
    sender = Sender(config)
    counts = outbox.counts()
    up, detail = sender.reachable()

    pending = outbox.pending()
    print()
    print(f"  device      {config.device_id}")
    print(f"  pi          {config.server}  {'reachable' if up else 'UNREACHABLE'} ({detail})")
    print(f"  competition {raw['year']} / {raw['competition']}")
    # pending first: mid-match it is the only line anyone reads.
    print(f"  pending     {counts['pending']}")
    if pending:
        oldest = pending[0]
        print(f"              oldest: {oldest.path.name}")
    print(f"  sent        {counts['sent']}")
    print(f"  rejected    {counts['rejected']}")
    if counts["rejected"]:
        for path in sorted(outbox.rejected_dir.glob("*.json"))[:5]:
            print(f"              {path.name}")
    print()
    return 1 if (counts["pending"] or counts["rejected"]) else 0


def cmd_verify(args) -> int:
    config, raw = load_config(args.config)
    sender = Sender(config)
    try:
        tables = sender.tables(raw["year"], raw["competition"])
    except httpx.HTTPError as err:
        print(f"cannot reach the Pi: {type(err).__name__}: {err}")
        return 1
    print()
    print(f"  {raw['year']} / {raw['competition']} on {config.server}")
    if not tables:
        print("  (no tables yet)")
    for name, count in sorted(tables.items()):
        print(f"  {name:20} {count:>5} rows")
    print()
    return 0


def cmd_retry(args) -> int:
    config, _ = load_config(args.config)
    moved = Outbox(config.outbox).requeue_rejected()
    print(f"requeued {moved} record(s)")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="uploadctl")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="watch the outbox and upload")
    run.add_argument("--once", action="store_true", help="single pass, then exit")
    run.set_defaults(func=cmd_run)

    sub.add_parser("status", help="queue depth and Pi reachability").set_defaults(func=cmd_status)
    sub.add_parser("verify", help="row counts on the Pi").set_defaults(func=cmd_verify)

    retry = sub.add_parser("retry", help="requeue rejected records")
    retry.add_argument("--rejected", action="store_true", required=True)
    retry.set_defaults(func=cmd_retry)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
