"""How a cable identifier is spelled, and how a run is named.

One place decides what a label says, used by the allocator, the renderer,
the reprint tool and the status line. See spec 4.5. The same module names
the print job, because that name is built from the same identifiers.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


def format_identifier(prefix: str, number: int, separator: str = "-", pad_width: int = 4) -> str:
    """`BL` + `100` -> `BL-0100`.

    A number wider than pad_width is printed at its NATURAL width, never
    truncated and never wrapped to zero. Silently losing a digit would put
    two different cables in a rack under the same label, which is the one
    failure this project exists to prevent. The allocator's pre-reserve fit
    check (layout.check_fits) is what catches the case where the wider
    string no longer fits the media.
    """
    return f"{prefix}{separator}{number:0{pad_width}d}"


def format_block(
    prefix: str, first: int, last: int, separator: str = "-", pad_width: int = 4
) -> tuple[str, ...]:
    return tuple(
        format_identifier(prefix, n, separator, pad_width) for n in range(first, last + 1)
    )


def overflows_pad(number: int, pad_width: int) -> bool:
    return len(str(number)) > pad_width


def warn_on_overflow(first: int, last: int, pad_width: int) -> bool:
    """Log once per block rather than once per label. Returns whether the
    block overflows, so callers can record it without re-deriving it."""
    if overflows_pad(last, pad_width):
        logger.warning(
            "block %d..%d exceeds pad_width %d -- identifiers print at their natural "
            "width (never truncated). Confirm the wider text still fits your media.",
            first,
            last,
            pad_width,
        )
        return True
    return False


def format_job_name(
    color: str,
    type_name: str,
    prefix: str,
    first_number: int,
    last_number: int,
    separator: str = "-",
    pad_width: int = 4,
) -> str:
    """The name a person sees in the printer's stored-file list.

    `Blue CAT6 - BL-0100...BL-0102`, or without the colour when the asset
    has none: `Fibre - FB-0007...FB-0009`. The printer holds jobs until
    someone selects one, so this is read by a human standing at the printer
    -- it leads with what the cable IS, then the exact range, which is what
    they match against the request in Halo.

    A trailing "Cable" is dropped from the asset type: Halo names the types
    "CAT6 Cable", "DAC Cable" and so on, and on a printer that labels
    nothing but cables the word is width spent saying nothing. It stays if
    it is the whole name, since "- BL-0100..." alone would be worse.
    """
    first = format_identifier(prefix, first_number, separator, pad_width)
    last = format_identifier(prefix, last_number, separator, pad_width)
    kind = str(type_name).strip()
    trimmed = re.sub(r"\s+cables?$", "", kind, flags=re.IGNORECASE).strip()
    if trimmed:
        kind = trimmed
    what = " ".join(part for part in (str(color).strip(), kind) if part)
    return f"{what} - {first}...{last}" if what else f"{first}...{last}"
