"""Per-asset and per-poll guardrails, plus the duplicate-prefix veto.

Lighter than an auto-printing design would need, because every job traces
back to a person who typed a number. What remains guards against a
fat-fingered quantity, an over-broad mass update, and the one structural
mistake that would corrupt the numbering: two cable-type assets sharing a
prefix while each keeps its own counter. See spec 3.4.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from .models import CableType

logger = logging.getLogger(__name__)

# How many cables one job covers. NOT a setting, deliberately: it is a
# technical ceiling rather than a preference, and the failure it prevents
# lands on whoever is standing at the printer.
#
# One job is one image, one page per label. At 250 cables (500 labels, 1in
# x 1.25in at 300 dpi) that image is about 56 million pixels. Past roughly
# 400 cables it crosses Pillow's decompression-bomb limit and the agent
# refuses to open it -- so a configurable value would let someone set a
# number that renders fine here and fails on the print host, after the
# numbers have been consumed.
#
# Nobody has to think about it: a bigger request is taken a batch per poll
# (allocator.reserve writes the remainder back to the trigger field), so
# any size finishes on its own.
BATCH_SIZE = 250


def clamp_qty(asset: CableType, batch_size: int | None = None) -> tuple[int, bool]:
    """Return (effective_qty, was_clamped).

    Only the clamped count is ever reserved. Reserving the requested count
    and printing the clamped one would advance the counter past numbers
    that never reached a cable -- harmless as a gap, but confusing to
    anyone reconciling the field against reality.

    The excess is NOT lost: allocator.reserve writes it back to the trigger
    field, so the next poll takes the following batch.
    """
    limit = BATCH_SIZE if batch_size is None else batch_size
    if asset.qty > limit:
        logger.info(
            "asset %s requested %d cables; taking %d this batch, the rest stays "
            "on the asset for the next poll",
            asset.id,
            asset.qty,
            limit,
        )
        return limit, True
    return asset.qty, False


def apply_batch_cap(assets: list[CableType], max_assets_per_poll: int) -> list[CableType]:
    """Bound one poll's work. The remainder is not dropped -- nothing has
    been claimed yet, so it is simply picked up on a later poll."""
    if len(assets) > max_assets_per_poll:
        logger.info(
            "%d cable types pending, processing %d this poll (MAX_ASSETS_PER_POLL); "
            "the rest wait for the next poll",
            len(assets),
            max_assets_per_poll,
        )
        return assets[:max_assets_per_poll]
    return assets


def veto_duplicate_prefixes(assets: list[CableType]) -> list[CableType]:
    """Drop every asset in any group that shares a prefix with another.

    Two cable types with the same prefix issue colliding identifiers from
    two independent counters, which is the exact fault this project exists
    to prevent. Neither is processed: picking one would issue numbers that
    the other's counter knows nothing about, which is worse than doing
    nothing and saying so loudly.

    This catches the collision only among assets pending in the SAME poll.
    tools/check_prefixes.py audits the whole tenant, and should be run at
    setup and after adding any cable type.
    """
    by_prefix: dict[str, list[CableType]] = defaultdict(list)
    for asset in assets:
        by_prefix[(asset.prefix or "").strip().upper()].append(asset)

    safe: list[CableType] = []
    for prefix, group in by_prefix.items():
        if len(group) > 1:
            logger.error(
                "prefix %r is claimed by %d pending cable types (asset ids %s) -- "
                "reserving NONE of them. Two counters issuing the same prefix would "
                "produce duplicate cable identifiers. Fix the prefixes, then re-flag.",
                prefix,
                len(group),
                ", ".join(str(a.id) for a in group),
            )
            continue
        safe.extend(group)

    return [a for a in assets if a in safe]
