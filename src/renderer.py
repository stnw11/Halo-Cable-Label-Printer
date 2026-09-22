"""Turn a reservation into a print-ready document.

The Docker service produces a finished document, not a data file for a
Brady template to merge. That keeps the label layout in version control and
under test, rather than in a binary template on one Windows box that nobody
can diff or review.

This module is PURE: identifiers and config in, bytes out. No Halo, no
filesystem, no clock. That is what makes it testable without a printer, and
it is why the poll loop can call it between the reserve and the enqueue
without any ordering surprises.

Two rules from spec 5.4 carry the weight:

  * One page per label, at exactly the configured media size, with no
    scaling anywhere. The Windows agent prints at 100%; "fit to page"
    silently resizes labels and is the classic way a correct document comes
    out the wrong physical size.

  * One font size for the whole block, driven by its widest identifier.
    Every label in a run is then visually identical, and the run cannot
    contain a label that silently shrank. The sibling project shipped a
    physical-clipping bug by sizing each label independently.
"""
from __future__ import annotations

import io
import logging

from .errors import LayoutError
from .layout import (
    POINTS_PER_INCH,
    LabelStyle,
    Media,
    block_font_px,
    line_height_pt,
    load_font,
    px,
)
from .models import Reservation

logger = logging.getLogger(__name__)


def render(reservation: Reservation, media: Media, style: LabelStyle) -> bytes:
    """Render every label for a reservation into one image.

    Page order is adjacent pairs -- BL-0100, BL-0100, BL-0101, BL-0101 --
    so the operator takes two labels in a row for the two ends of one cable.
    Emitting all the first ends and then all the second ends would force
    them to run the batch twice and match numbers by hand.
    """
    labels = reservation.label_sequence
    if not labels:
        raise LayoutError("reservation contains no labels")

    return _render_png(reservation, labels, media, style)


# --- the raster path ----------------------------------------------------------

def _render_png(reservation: Reservation, labels, media: Media, style: LabelStyle) -> bytes:
    """Every label as ONE tall image: label pages stacked top to bottom.

    One payload file keeps the queue protocol as it is -- a job is a file
    and a sidecar -- and the agent slices the strip back into pages by
    label_count, which the sidecar already carries. The alternative, a file
    per label, would multiply the queue's moving parts by 24 for a dozen
    cables.

    Greyscale, not 1-bit: the driver thresholds for the thermal head, and
    antialiased edges give it more to work with than pre-flattened ones.
    """
    from PIL import Image, ImageDraw

    page_w, page_h = px(media.label_width_in, media.dpi), px(media.label_height_in, media.dpi)
    size_px = block_font_px(reservation.identifiers, media, style)
    font = load_font(size_px)

    zone_w = px(media.print_area_width_in, media.dpi)
    zone_h = px(media.print_area_height_in, media.dpi)
    zone_x = (page_w - zone_w) // 2 if media.print_area_x_in is None else px(media.print_area_x_in, media.dpi)
    # The config places the zone from the BOTTOM of the label (PDF style);
    # an image's origin is its top-left corner.
    if media.print_area_y_in is None:
        zone_top = (page_h - zone_h) // 2
    else:
        zone_top = page_h - px(media.print_area_y_in, media.dpi) - zone_h

    margin = px(media.margin_in, media.dpi)
    gap = px(style.legend_gap_in, media.dpi)
    repeats = max(1, style.legend_repeat)
    line_h = (zone_h - 2 * margin - (repeats - 1) * gap) / repeats

    logger.debug(
        "rendering %d label(s) for %s..%s at %dpx (%.2fpt) on %dx%d px pages",
        len(labels), reservation.identifiers[0], reservation.identifiers[-1],
        size_px, size_px * POINTS_PER_INCH / media.dpi, page_w, page_h,
    )

    strip = Image.new("L", (page_w, page_h * len(labels)), 255)
    draw = ImageDraw.Draw(strip)
    for index, text in enumerate(labels):
        box = font.getbbox(text)
        text_w, text_h = box[2] - box[0], box[3] - box[1]
        x = zone_x + (zone_w - text_w) // 2 - box[0]
        for line in range(repeats):
            # Position WITHIN the page, then offset by whole pages: rounding
            # the sum instead would land a pixel differently on alternate
            # pages, because round() breaks .5 ties towards even.
            line_top = zone_top + margin + line * (line_h + gap)
            y = int(round(line_top + (line_h - text_h) / 2)) - box[1]
            draw.text((x, y + index * page_h), text, font=font, fill=0)

    buffer = io.BytesIO()
    # No text metadata: the file can sit on a share other people read.
    strip.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
