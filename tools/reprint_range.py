#!/usr/bin/env python3
"""Reprint a block of cable identifiers WITHOUT advancing the counter.

The counter is view-only and this service is its sole allocator, so without
this tool the only way to recover a lost, jammed, or misprinted run would be
to issue fresh numbers for cables that are already installed.

    tools/reprint_range.py --prefix BL --from 100 --to 111 --asset-id 4711

This tool NEVER writes Next Cable ID and NEVER writes Cables to Label, under
any flag. The one thing --asset-id does is let the job's result update that
asset's Last Label Run to a RESENT line, so the record in Halo matches what
physically exists. Give it when recovering a real run; leave it off
when printing test media, where the reprint should stay invisible in Halo.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import _bootstrap  # noqa: F401

from src.config import load_config
from src.enqueue import write as enqueue_write
from src.errors import LayoutError, StartupError
from src.models import Plan, Reservation
from src.naming import format_block
from src.renderer import render
from protocol import QueueError, ensure_queue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--from", dest="first", type=int, required=True)
    parser.add_argument("--to", dest="last", type=int, required=True)
    parser.add_argument("--copies", type=int, default=None, help="labels per cable; defaults to LABELS_PER_CABLE")
    parser.add_argument(
        "--asset-id",
        type=int,
        default=None,
        help="update this asset's Last Label Run to RESENT when the job completes. "
             "The counter and the trigger field are never touched.",
    )
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    # An open-ended or reversed range is almost always a typo, and a typo here
    # burns media and confuses whoever is at the printer.
    if args.last < args.first:
        print("--to must not be below --from", file=sys.stderr)
        return 2
    if args.first < 0:
        print("--from must not be negative", file=sys.stderr)
        return 2

    try:
        # never advances the counter and never contacts Halo, so no tenant credentials are needed.
        cfg = load_config(require_halo=False)
    except StartupError as exc:
        print(exc, file=sys.stderr)
        return 2

    per_cable = args.copies or cfg.labels_per_cable
    count = args.last - args.first + 1
    identifiers = format_block(
        args.prefix, args.first, args.last, cfg.style.separator, cfg.style.pad_width
    )

    print(f"Reprint {identifiers[0]}..{identifiers[-1]}")
    print(f"  {count} cable(s), {per_cable} label(s) each = {count * per_cable} labels")
    print(f"  queue:    {cfg.queue.root}")
    print(f"  Halo:     {'status line -> asset ' + str(args.asset_id) if args.asset_id else 'untouched'}")
    print("  counter:  NOT advanced (this is a reprint)")
    if not args.yes:
        if input("\nProceed? [y/N] ").strip().lower() not in ("y", "yes"):
            print("aborted")
            return 1

    plan = Plan(
        asset_id=args.asset_id or 0,
        prefix=args.prefix,
        first_number=args.first,
        last_number=args.last,
        cable_count=count,
        labels_per_cable=per_cable,
        requested_qty=count,
    )
    reservation = Reservation(
        plan=plan,
        identifiers=identifiers,
        reserved_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    try:
        payload = render(reservation, cfg.media, cfg.style)
    except LayoutError as exc:
        print(f"cannot render: {exc}", file=sys.stderr)
        return 1

    try:
        ensure_queue(cfg.queue.root)
        written = enqueue_write(
            reservation, payload, cfg, source="reprint", asset_id=args.asset_id
        )
    except QueueError as exc:
        print(f"could not enqueue: {exc}", file=sys.stderr)
        return 1

    print(f"\nenqueued {written['payload'].name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
