"""Renderer tests. No printer involved -- these assert the image's
structure and physical size, which is what a correct print depends on.

The renderer draws the labels the Windows agent blits onto the printer:
one image per job, one page per label, at the media's dpi. Nothing here
reads text back (there is none to read); a page is compared with another
page, and ink is located by where it falls.
"""
import io
from dataclasses import replace

import pytest
from PIL import Image

from src.allocator import reserve, validate
from src.errors import LayoutError
from src.layout import LabelStyle, block_font_px, px
from src.models import CableType
from src.renderer import render
from tests.conftest import FakeHalo

# The deployment's media, so the numbers below are the real ones.
WRAP = dict(
    dpi=300, label_width_in=1.0, label_height_in=1.25,
    print_area_width_in=1.0, print_area_height_in=0.5,
    print_area_x_in=0.0, print_area_y_in=0.75, margin_in=0.03,
)


def _reservation(app_config, qty=3, prefix="BL", next_id=100):
    asset = CableType(id=4711, prefix=prefix, next_id=next_id, qty=qty)
    plan = validate(asset, app_config)
    return reserve(FakeHalo(counter=next_id), asset, plan, app_config)


def _image(app_config, qty=3, media=None, style=None, next_id=100):
    media = media or app_config.media
    style = style or app_config.style
    data = render(_reservation(app_config, qty=qty, next_id=next_id), media, style)
    return Image.open(io.BytesIO(data))


def _pages(image, media):
    height = px(media.label_height_in, media.dpi)
    return [
        image.crop((0, i * height, image.width, (i + 1) * height))
        for i in range(image.height // height)
    ]


def _ink(image):
    """Bounding box of anything darker than mid grey, or None if blank."""
    return image.point(lambda v: 255 if v < 128 else 0).getbbox()


def test_renders_two_pages_per_cable(app_config):
    """Criterion 1 and 2: N cables, 2N label pages."""
    media = replace(app_config.media, **WRAP)
    image = _image(app_config, qty=3, media=media)
    assert len(_pages(image, media)) == 6


def test_page_size_is_the_configured_media_size(app_config):
    """An image that is the wrong size prints wrong whatever the driver
    does, so the pixel size is asserted against the media directly."""
    media = replace(app_config.media, **WRAP)
    image = _image(app_config, qty=1, media=media)
    assert image.width == px(media.label_width_in, media.dpi)
    assert _pages(image, media)[0].height == px(media.label_height_in, media.dpi)


def test_the_image_is_greyscale_with_no_metadata(app_config):
    """The queue is a share other people can read (spec section 8), so the
    file carries no machine or user identity."""
    media = replace(app_config.media, **WRAP)
    data = render(_reservation(app_config, qty=1), media, app_config.style)
    image = Image.open(io.BytesIO(data))
    assert image.mode == "L"
    assert not getattr(image, "text", {}), "the PNG carries text chunks"
    assert not {k: v for k, v in image.info.items() if k not in ("dpi",)}


def test_each_cable_gets_its_own_pair_of_identical_pages(app_config):
    """Both ends of one cable carry the same identifier, and two different
    cables must not."""
    media = replace(app_config.media, **WRAP)
    pages = [p.tobytes() for p in _pages(_image(app_config, qty=3, media=media), media)]
    assert pages[0] == pages[1] and pages[2] == pages[3] and pages[4] == pages[5]
    assert len({pages[0], pages[2], pages[4]}) == 3


def test_label_order_is_adjacent_pairs(app_config):
    """Spec 5.4. The operator takes two labels in a row for one cable, so
    page order must be A A B B C C, never A B C A B C."""
    media = replace(app_config.media, **WRAP)
    pages = [p.tobytes() for p in _pages(_image(app_config, qty=3, media=media), media)]
    assert pages[0] != pages[2], "first two pages must belong to the same cable"
    assert [pages.index(p) for p in pages] == [0, 0, 2, 2, 4, 4]


def test_single_end_labelling_halves_the_pages(app_config):
    cfg = replace(app_config, labels_per_cable=1)
    media = replace(cfg.media, **WRAP)
    assert len(_pages(_image(cfg, qty=3, media=media), media)) == 3


def test_ink_stays_inside_the_printed_zone(app_config):
    """On wrap stock the rest of the label is the clear tail that folds over
    the print; anything drawn there is invisible or on the wrong face."""
    media = replace(app_config.media, **WRAP)
    page = _pages(_image(app_config, qty=1, media=media), media)[0]
    _, top, _, bottom = _ink(page)
    assert top >= 0 and bottom <= 150                     # the zone is the top 0.5in
    assert _ink(page.crop((0, 150, 300, 375))) is None    # the tail is blank


def test_the_legend_is_two_stacked_lines(app_config):
    media = replace(app_config.media, **WRAP)
    page = _pages(_image(app_config, qty=1, media=media), media)[0]
    rows = [y for y in range(150) if _ink(page.crop((0, y, page.width, y + 1)))]
    gaps = [b - a for a, b in zip(rows, rows[1:]) if b - a > 1]
    assert len(gaps) == 1, f"expected one gap between two lines of text, got {gaps}"


def test_legend_repeat_of_one_draws_it_once(app_config):
    media = replace(app_config.media, **WRAP)
    style = LabelStyle(legend_repeat=1)
    page = _pages(_image(app_config, qty=1, media=media, style=style), media)[0]
    rows = [y for y in range(150) if _ink(page.crop((0, y, page.width, y + 1)))]
    assert [b - a for a, b in zip(rows, rows[1:]) if b - a > 1] == []


def test_each_line_is_centred_across_the_zone(app_config):
    media = replace(app_config.media, **WRAP)
    page = _pages(_image(app_config, qty=1, media=media), media)[0]
    left, _, right, _ = _ink(page.crop((0, 0, page.width, 150)))
    assert abs(left - (page.width - right)) <= 4, f"left {left}, right {page.width - right}"


def test_stacking_gives_each_line_the_full_width(app_config):
    """Stacking divides the height, never the width, so a long identifier
    that fits on one line still fits with two."""
    media = replace(app_config.media, **WRAP)
    one = block_font_px(("FB-0007",), media, LabelStyle(legend_repeat=1))
    two = block_font_px(("FB-0007",), media, LabelStyle(legend_repeat=2))
    assert two <= one


def test_one_font_size_for_the_whole_block(app_config):
    """A block spanning a digit boundary must not contain a label that
    silently shrank -- the sibling's physical-clipping bug."""
    media = replace(app_config.media, **WRAP)
    spanning = _reservation(app_config, qty=4, next_id=9998)   # BL-9998..BL-10001
    assert block_font_px(spanning.identifiers, media, app_config.style) == \
        block_font_px(("BL-10001",), media, app_config.style)


def test_an_identifier_that_cannot_be_read_is_refused(app_config):
    """Below the legibility floor the run is refused rather than shrunk."""
    tiny = replace(app_config.media, **{**WRAP, "print_area_width_in": 0.2, "label_width_in": 0.2})
    with pytest.raises(LayoutError, match="legibility floor"):
        render(_reservation(app_config, qty=1), tiny, app_config.style)
