"""Label geometry: does this text fit, and at what size?

Separated from renderer.py so the allocator can run the same fit check
during validation WITHOUT pulling in the rendering pipeline, and so the
arithmetic is unit-testable on its own. See spec 4.1 and 5.4.

Geometry is described in inches and points, because that is how media is
specified; text is measured in pixels at the media's dpi, because that is
what actually gets printed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .errors import LayoutError

logger = logging.getLogger(__name__)

POINTS_PER_INCH = 72.0


@dataclass(frozen=True)
class Media:
    """The physical label. For self-laminating wrap stock, the printed zone
    is only part of the overall label -- the rest is the clear overlaminate
    tail that wraps over the printed portion. Only the printed zone is
    usable area, which is why it is configured separately."""

    dpi: int = 300
    # PLACEHOLDER geometry, in the shape of a common self-laminating cable
    # wrap: an overall label roughly 2in x 0.75in of which about 1in x 0.5in
    # is the opaque printed zone. Measure YOUR stock -- these defaults exist
    # so a fresh clone can render something, not because they are right.
    label_width_in: float = 2.0
    label_height_in: float = 0.75
    print_area_width_in: float = 1.0
    print_area_height_in: float = 0.5
    margin_in: float = 0.03

    # Where the printed zone sits on the label, as the offset of its
    # lower-left corner from the page's lower-left corner. None means
    # centred.
    #
    # This matters more than it looks. Die-cut stock is usually printed
    # edge to edge, so centring is right. SELF-LAMINATING WRAP is not: the
    # opaque printed area is at one END of the label and the clear
    # overlaminate tail extends from it, so that the tail can wrap over the
    # printed portion once the label is applied. Centring the legend on
    # that stock puts half the text on the laminate.
    print_area_x_in: float | None = None
    print_area_y_in: float | None = None

    # There is deliberately NO `orientation` setting. The page is whatever
    # label_width_in x label_height_in says, which already determines it --
    # a separate flag could only ever disagree with the dimensions. If the
    # legend ever needs to run across the label rather than along it, that
    # is text ROTATION, a different thing, and it should be added as its own
    # setting that actually does something.

    @property
    def zone_x_pt(self) -> float:
        if self.print_area_x_in is None:
            return (self.page_width_pt - self.print_area_width_in * POINTS_PER_INCH) / 2.0
        return self.print_area_x_in * POINTS_PER_INCH

    @property
    def zone_y_pt(self) -> float:
        if self.print_area_y_in is None:
            return (self.page_height_pt - self.print_area_height_in * POINTS_PER_INCH) / 2.0
        return self.print_area_y_in * POINTS_PER_INCH

    @property
    def page_width_pt(self) -> float:
        return self.label_width_in * POINTS_PER_INCH

    @property
    def page_height_pt(self) -> float:
        return self.label_height_in * POINTS_PER_INCH

    @property
    def usable_width_pt(self) -> float:
        return (self.print_area_width_in - 2 * self.margin_in) * POINTS_PER_INCH

    @property
    def usable_height_pt(self) -> float:
        return (self.print_area_height_in - 2 * self.margin_in) * POINTS_PER_INCH


@dataclass(frozen=True)
class LabelStyle:
    separator: str = "-"
    pad_width: int = 4
    prefix_pattern: str = r"^[A-Za-z0-9]{1,8}$"
    font: str = "Helvetica-Bold"
    min_font_pt: float = 5.0
    max_font_pt: float = 12.0
    legend_repeat: int = 2
    legend_gap_in: float = 0.08


def line_height_pt(media: Media, style: LabelStyle) -> float:
    """Height available to ONE line of the legend.

    The legend is repeated as lines stacked directly above one another,
    each the full width of the printed zone, so the identifier is readable
    from either side once the label is wrapped around a cable. Each line
    gets an equal share of the usable height left after the gaps between
    them; the width is never divided.
    """
    repeats = max(1, style.legend_repeat)
    gaps_pt = (repeats - 1) * style.legend_gap_in * POINTS_PER_INCH
    return (media.usable_height_pt - gaps_pt) / repeats




def check_fits(text: str, media: Media, style: LabelStyle) -> int:
    """Validation-time alias for fit_font_px, named for what the caller is
    asking. Costs nothing and writes nothing, so the allocator can call it
    before consuming any numbers."""
    return fit_font_px(text, media, style)


# --- drawing the label --------------------------------------------------------
#
# The Windows agent prints a bitmap through the printer's own driver, which
# needs no PDF reader on the print host. The layout decisions stay here, so
# both paths place text the same way; only the units differ (pixels at the
# media's dpi instead of points).

FONT_FILE = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "TeXGyreHeros-Bold.otf"


def px(inches: float, dpi: int) -> int:
    """Inches to whole device pixels, never below one."""
    return max(1, int(round(inches * dpi)))


def load_font(size_px: int):
    """The bundled font at a pixel size.

    The font travels with the project (assets/fonts, SIL OFL) so a label
    renders identically on the developer's machine, in the container, and
    in a test -- a system font would make output depend on what happens to
    be installed.
    """
    from PIL import ImageFont

    try:
        return ImageFont.truetype(str(FONT_FILE), size_px)
    except OSError as exc:  # pragma: no cover - only if the asset is missing
        raise LayoutError(f"cannot load the label font {FONT_FILE}: {exc}") from exc


def text_size_px(text: str, size_px: int) -> tuple[int, int]:
    """Ink width and cap height of `text`, in pixels, at this size."""
    left, top, right, bottom = load_font(size_px).getbbox(text)
    return right - left, bottom - top


def fit_font_px(text: str, media: Media, style: LabelStyle) -> int:
    """Largest pixel size at which `text` fits one legend line.

    The same rule as fit_font_size: never below min_font_pt, because a
    label nobody can read is worse than a refused request. Measured with
    the real font rather than solved arithmetically, since a rasterised
    glyph's advance is rounded to whole pixels.
    """
    if not text:
        raise LayoutError("cannot lay out an empty identifier")

    max_width = px(media.print_area_width_in - 2 * media.margin_in, media.dpi)
    max_height = px(line_height_pt(media, style) / POINTS_PER_INCH, media.dpi)
    floor_px = px(style.min_font_pt / POINTS_PER_INCH, media.dpi)
    ceiling_px = px(style.max_font_pt / POINTS_PER_INCH, media.dpi)
    if max_width <= 0 or max_height <= 0:
        raise LayoutError(
            f"no usable print area: {media.print_area_width_in}in x "
            f"{media.print_area_height_in}in with {media.margin_in}in margins and "
            f"{style.legend_repeat} legend line(s) leaves nothing to print in"
        )

    size = ceiling_px
    while size >= floor_px:
        width, height = text_size_px(text, size)
        if width <= max_width and height <= max_height:
            return size
        size -= 1
    raise LayoutError(
        f"identifier {text!r} ({len(text)} chars) does not fit a "
        f"{max_width / media.dpi:.3f}in x {max_height / media.dpi:.3f}in legend line at "
        f"the {style.min_font_pt}pt legibility floor. Shorten the prefix, reduce "
        f"layout.legend_repeat or legend_gap_in, or use larger media."
    )


def block_font_px(identifiers, media: Media, style: LabelStyle) -> int:
    """One pixel size for a whole block, driven by its widest member, so
    every label in a run is visually identical (see block_font_size)."""
    if not identifiers:
        raise LayoutError("cannot size an empty block")
    widest = max(identifiers, key=lambda s: text_size_px(s, 100)[0])
    return fit_font_px(widest, media, style)
