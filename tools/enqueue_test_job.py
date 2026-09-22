#!/usr/bin/env python3
"""Drop a known-good job on the queue. No Halo, no counter, no printer.

This is how the Windows agent is tested in isolation: run it here, watch it
print (or, with backend: null, watch it report) on the other side. Nothing
it does is visible in Halo, and no numbers are consumed.

    tools/enqueue_test_job.py --prefix TEST --from 1 --to 3
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import _bootstrap  # noqa: F401

from protocol import QueueError, ensure_queue

from src.config import load_config
from src.enqueue import write as enqueue_write
from src.errors import LayoutError, StartupError
from src.models import Plan, Reservation
from src.naming import format_block
from src.renderer import render


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prefix", default="TEST")
    parser.add_argument("--from", dest="first", type=int, default=1)
    parser.add_argument("--to", dest="last", type=int, default=2)
    args = parser.parse_args(argv)

    if args.last < args.first:
        print("--to must not be below --from", file=sys.stderr)
        return 2

    try:
        # writes to the queue and never contacts Halo, so no tenant credentials are needed.
        cfg = load_config(require_halo=False)
    except StartupError as exc:
        print(exc, file=sys.stderr)
        return 2

    count = args.last - args.first + 1
    identifiers = format_block(args.prefix, args.first, args.last, cfg.style.separator, cfg.style.pad_width)
    plan = Plan(
        asset_id=0,
        prefix=args.prefix,
        first_number=args.first,
        last_number=args.last,
        cable_count=count,
        labels_per_cable=cfg.labels_per_cable,
        requested_qty=count,
    )
    reservation = Reservation(
        plan=plan,
        identifiers=identifiers,
        reserved_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    try:
        payload = render(reservation, cfg.media, cfg.style)
        ensure_queue(cfg.queue.root)
        # asset_id=None: a test job must never write a status line to Halo.
        written = enqueue_write(reservation, payload, cfg, source="test", asset_id=None)
    except (LayoutError, QueueError) as exc:
        print(exc, file=sys.stderr)
        return 1

    print(f"enqueued {written['payload'].name}")
    print(f"  {identifiers[0]}..{identifiers[-1]}, {count * cfg.labels_per_cable} labels")
    print(f"  queue: {cfg.queue.root}")
    print("  Halo:  untouched, no numbers consumed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
