#!/usr/bin/env python3
"""Render a block of labels to a local file. No Halo, no share, no printer.

The bring-up tool: it is how you check the layout against real measurements
before anything is wired up. Verify the output by MEASURING it (spec
criterion 24), not by eyeballing it on screen -- a PDF viewer will happily
show you a correct-looking label at the wrong physical size.

    tools/render_test_block.py --prefix BL --from 100 --to 103
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401

from src.config import load_config
from src.errors import StartupError
from src.layout import POINTS_PER_INCH, block_font_size
from src.models import Plan, Reservation
from src.naming import format_block
from src.renderer import render


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--prefix", default="BL")
    parser.add_argument("--from", dest="first", type=int, default=100)
    parser.add_argument("--to", dest="last", type=int, default=103)
    parser.add_argument("--labels-per-cable", type=int, default=None)
    parser.add_argument("--out", default="out/test-block.pdf")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.last < args.first:
        print("--to must not be below --from", file=sys.stderr)
        return 2

    try:
        # renders locally and never contacts Halo, so no tenant credentials are needed.
        cfg = load_config(require_halo=False)
    except StartupError as exc:
        print(exc, file=sys.stderr)
        return 2

    per_cable = args.labels_per_cable or cfg.labels_per_cable
    count = args.last - args.first + 1
    plan = Plan(
        asset_id=0,
        prefix=args.prefix,
        first_number=args.first,
        last_number=args.last,
        cable_count=count,
        labels_per_cable=per_cable,
        requested_qty=count,
    )
    identifiers = format_block(args.prefix, args.first, args.last, cfg.style.separator, cfg.style.pad_width)
    reservation = Reservation(
        plan=plan,
        identifiers=identifiers,
        reserved_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )

    payload = render(reservation, cfg.media, cfg.style)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(payload)

    font_pt = block_font_size(identifiers, cfg.media, cfg.style)
    print(f"wrote {out} ({len(payload):,} bytes)")
    print(f"  identifiers   {identifiers[0]}..{identifiers[-1]} ({count} cables)")
    print(f"  pages         {count * per_cable} ({per_cable} per cable)")
    print(f"  page size     {cfg.media.label_width_in}in x {cfg.media.label_height_in}in "
          f"({cfg.media.label_width_in * POINTS_PER_INCH:.1f}pt x "
          f"{cfg.media.label_height_in * POINTS_PER_INCH:.1f}pt)")
    print(f"  printed zone  {cfg.media.print_area_width_in}in x {cfg.media.print_area_height_in}in")
    print(f"  legend        {cfg.style.legend_repeat}x at {font_pt:.2f}pt")
    print("\nMeasure the printed output. Do not trust an on-screen preview for size.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
