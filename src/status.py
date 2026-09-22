"""The Last Label Run status line written back to the Halo asset.

This field is the only feedback the person who pressed the button ever
sees. The print happens on another machine, behind a driver, seconds to
minutes later, so a log line on the Docker host reaches nobody. See spec
3.3.

The field is treated as WRITE-ONLY: nothing in this service ever parses it
back. Someone editing it by hand can confuse a colleague but cannot
corrupt the counter.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .naming import format_identifier

logger = logging.getLogger(__name__)

# SENT, not PRINTED: the agent hands the job to the printer, which holds it
# in its stored-file list until someone selects it. Saying PRINTED would tell
# the technician the labels exist when they are still waiting at the printer.
QUEUED, SENT, FAILED, RESENT = "QUEUED", "SENT", "FAILED", "RESENT"
# Nothing failed and nothing arrived: the print host has gone quiet. A
# request that simply stops is the one outcome nobody notices, so it gets
# a word of its own rather than being left on QUEUED forever.
STALLED = "STALLED"

SEP = " · "  # middle dot, for scannability at a glance


def resolve_timezone(name: str):
    """Fall back to UTC rather than failing. A bad timezone name should not
    stop labels printing -- it should make the timestamps less convenient."""
    if not name or name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning("unknown DISPLAY_TIMEZONE %r, falling back to UTC", name)
        return timezone.utc


def format_status_line(
    status: str,
    prefix: str,
    first_number: int,
    last_number: int,
    cable_count: int,
    label_count: int,
    requested_qty: int | None = None,
    remaining: int = 0,
    when: datetime | None = None,
    reason: str | None = None,
    tz: str = "UTC",
    separator: str = "-",
    pad_width: int = 4,
    max_chars: int = 255,
) -> str:
    """Build one status line, truncating only what is safe to lose.

    The status word comes first so the field sorts and filters usefully in
    a Halo list view -- a saved list of assets whose Last Label Run starts
    with FAILED is the closest thing this system has to alerting. The
    identifier range is always preserved, including on failure, because it
    is what tools/reprint_range.py needs and the only place a technician can
    see which numbers a run consumed.

    If anything has to go, it is the tail of the failure reason. The full
    reason is always in the log.
    """
    when = (when or datetime.now(timezone.utc)).astimezone(resolve_timezone(tz))
    first = format_identifier(prefix, first_number, separator, pad_width)
    last = format_identifier(prefix, last_number, separator, pad_width)
    stamp = when.strftime("%Y-%m-%d %H:%M")

    head = f"{status} {first}..{last}"
    # A request bigger than one batch is split across several runs. Saying
    # "250 of 1000" and what is left is the only way the person who pressed
    # the button knows more is coming rather than that 750 cables vanished.
    if requested_qty and requested_qty > cable_count:
        body = f"{cable_count} of {requested_qty} cables, {label_count} labels"
    else:
        body = f"{cable_count} cables, {label_count} labels"
    line = f"{head}{SEP}{body}{SEP}{stamp}"
    if remaining:
        line = f"{line}{SEP}{remaining} still to print"

    if reason:
        reason = " ".join(str(reason).split())  # collapse newlines out of driver errors
        room = max_chars - len(line) - len(SEP)
        if room >= 8:
            line = f"{line}{SEP}{reason[:room]}"
        else:
            logger.debug("no room for a reason in the status line; log only")

    if len(line) > max_chars:
        # Only reachable with an absurdly small STATUS_FIELD_MAX_CHARS. Keep
        # the head intact so the range survives even a brutal truncation.
        line = line[:max_chars]
    return line
