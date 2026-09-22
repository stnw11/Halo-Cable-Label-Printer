"""Zero padding, pinned as a stated requirement.

Every printed number is padded to `layout.pad_width` digits, so a counter
value of 7 prints as 0007. This has its own test file because it is a rule
the user asked for by name, and because the ONE case where padding gives
way (a number too wide for the pad) is a correctness decision rather than a
formatting one.
"""
from dataclasses import replace

import pytest
from PIL import Image, ImageDraw
import io

from src.allocator import reserve, validate
from src.layout import LabelStyle
from src.models import CableType
from src.naming import format_block, format_identifier
from src.layout import block_font_px, load_font
from src.renderer import render
from tests.conftest import FakeHalo


@pytest.mark.parametrize(
    "number,expected",
    [(1, "BL-0001"), (7, "BL-0007"), (42, "BL-0042"), (100, "BL-0100"),
     (999, "BL-0999"), (1234, "BL-1234"), (9999, "BL-9999")],
)
def test_numbers_are_padded_to_four_digits(number, expected):
    assert format_identifier("BL", number, "-", 4) == expected


def test_a_block_pads_every_member_including_across_a_digit_boundary():
    assert format_block("BL", 8, 12, "-", 4) == (
        "BL-0008", "BL-0009", "BL-0010", "BL-0011", "BL-0012",
    )


def test_padding_reaches_the_printed_page(app_config):
    """The rule has to hold on what is drawn, not just in the helper. The
    image carries no text to read back, so this compares it against images
    rendered from the padded and unpadded identifiers directly."""
    asset = CableType(id=1, prefix="BL", next_id=7, qty=1)
    reservation = reserve(FakeHalo(counter=7), asset, validate(asset, app_config), app_config)
    printed = render(reservation, app_config.media, app_config.style)

    def page_of(text):
        size = block_font_px((text,), app_config.media, app_config.style)
        font = load_font(size)
        image = Image.new("L", font.getbbox(text)[2:], 255)
        ImageDraw.Draw(image).text((0, 0), text, font=font, fill=0)
        return image.tobytes()

    assert page_of("BL-0007") != page_of("BL-7")          # the two differ at all
    page = Image.open(io.BytesIO(printed))
    ink = page.point(lambda v: 255 if v < 128 else 0).getbbox()
    padded_width = load_font(block_font_px(("BL-0007",), app_config.media, app_config.style)).getbbox("BL-0007")[2]
    unpadded_width = load_font(block_font_px(("BL-0007",), app_config.media, app_config.style)).getbbox("BL-7")[2]
    assert abs((ink[2] - ink[0]) - padded_width) < abs((ink[2] - ink[0]) - unpadded_width)


def test_padding_reaches_the_queue_filename(app_config, queue_root):
    """The stem carries the PADDED number, so grepping the queue for what is
    physically on a cable finds the job that produced it."""
    from src.enqueue import build_sidecar

    asset = CableType(id=1, prefix="BL", next_id=7, qty=2)
    reservation = reserve(FakeHalo(counter=7), asset, validate(asset, app_config), app_config)
    sidecar = build_sidecar(reservation, b"payload", app_config)
    assert "_BL_0007-0008_" in sidecar["payload_file"]


def test_the_sidecar_keeps_the_raw_numbers_unpadded(app_config):
    """Padding is a PRESENTATION rule. The protocol carries integers, so the
    agent and the reconciler do arithmetic rather than string handling."""
    from src.enqueue import build_sidecar

    asset = CableType(id=1, prefix="BL", next_id=7, qty=2)
    reservation = reserve(FakeHalo(counter=7), asset, validate(asset, app_config), app_config)
    sidecar = build_sidecar(reservation, b"payload", app_config)
    assert sidecar["first_number"] == 7 and sidecar["last_number"] == 8


def test_pad_width_is_configurable(app_config):
    """Four digits is this deployment's choice, not a constant."""
    for width, expected in ((3, "BL-007"), (4, "BL-0007"), (6, "BL-000007")):
        cfg = replace(app_config, style=LabelStyle(pad_width=width))
        assert format_identifier("BL", 7, cfg.style.separator, cfg.style.pad_width) == expected


def test_a_number_wider_than_the_pad_widens_and_never_truncates(caplog):
    """The one case where padding gives way, and the reason it must.

    Truncating 10000 to four digits would print BL-0000, which is a number
    already issued at the start of the sequence. Two different cables in a
    rack under one identifier is the exact fault this project exists to
    prevent, so the label gets longer instead. The allocator's pre-reserve
    fit check refuses the block if the wider text no longer fits the media.
    """
    assert format_identifier("BL", 10000, "-", 4) == "BL-10000"
    assert format_identifier("BL", 123456, "-", 4) == "BL-123456"


def test_crossing_ten_thousand_warns_once_for_the_block(app_config, caplog):
    asset = CableType(id=1, prefix="BL", next_id=9999, qty=3)
    with caplog.at_level("WARNING"):
        plan = validate(asset, app_config)
    assert plan.last_number == 10001
    assert caplog.text.count("exceeds pad_width") == 1, "warn per block, not per label"
