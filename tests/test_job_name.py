"""The name a person reads at the printer.

The Wraptor holds each job in a stored-file list until someone selects it,
so the job name is a user interface: it has to say what the cable is and
which numbers are in the run, and it has to survive being used as a
filename on Windows.
"""
from pathlib import Path

import pytest

from protocol import validate_job
from src.enqueue import build_sidecar
from src.models import CableType, Plan, Reservation
from src.naming import format_job_name
from winagent.src.backends import MAX_JOB_NAME, job_name


def test_colour_and_type_lead_then_the_range():
    assert format_job_name("Blue", "CAT6 Cable", "BL", 100, 102) == "Blue CAT6 - BL-0100...BL-0102"


def test_a_trailing_cable_is_dropped_from_the_type():
    """Halo names the types "CAT6 Cable", "DAC Cable". On a printer that
    labels nothing but cables, the word is width spent saying nothing."""
    assert format_job_name("", "DAC Cable", "DC", 1, 2) == "DAC - DC-0001...DC-0002"
    assert format_job_name("", "Display Cables", "DS", 1, 2) == "Display - DS-0001...DS-0002"


def test_a_type_that_is_only_the_word_cable_keeps_it():
    """Dropping it would leave the name starting with a dash."""
    assert format_job_name("Blue", "Cable", "BL", 100, 102) == "Blue Cable - BL-0100...BL-0102"


def test_a_missing_colour_is_dropped_without_leaving_a_gap():
    assert format_job_name("", "Fibre Cable", "FB", 7, 9) == "Fibre - FB-0007...FB-0009"
    assert format_job_name("   ", "Fibre Cable", "FB", 7, 9) == "Fibre - FB-0007...FB-0009"


def test_with_neither_the_range_stands_alone():
    """An asset type the tenant does not name still produces a usable job."""
    assert format_job_name("", "", "BL", 100, 111) == "BL-0100...BL-0111"


def test_a_single_cable_names_the_same_number_twice():
    assert format_job_name("Green", "CAT6 Cable", "GN", 5, 5) == "Green CAT6 - GN-0005...GN-0005"


def _reservation(app_config, color="Blue", type_name="CAT6 Cable"):
    plan = Plan(
        asset_id=4711, prefix="BL", first_number=100, last_number=102, cable_count=3,
        labels_per_cable=2, requested_qty=3, color=color, type_name=type_name,
    )
    return Reservation(plan=plan, identifiers=("BL-0100", "BL-0101", "BL-0102"), reserved_utc="2026-09-21T12:00:00Z")


def test_the_sidecar_carries_the_job_name(app_config, payload):
    sidecar = build_sidecar(_reservation(app_config), payload, app_config)
    assert sidecar["job_name"] == "Blue CAT6 - BL-0100...BL-0102"
    validate_job(sidecar)


def test_a_sidecar_without_a_job_name_is_still_valid():
    """It is optional in the protocol, so an agent that predates it, or a
    Docker half that cannot read the colour field, still interoperates."""
    sidecar = {
        "protocol_version": 1, "job_id": "1f4c9a2e-7b3d-4c51-9a0e-2b6f8d1c4e77",
        "created_utc": "2026-09-21T12:00:00Z", "source": "poll", "reprint": False,
        "halo_asset_id": None, "prefix": "BL", "first_number": 100, "last_number": 100,
        "cable_count": 1, "labels_per_cable": 2, "label_count": 2,
        "payload_file": "x.png", "payload_sha256": "a" * 64, "payload_bytes": 1,
        "render_format": "png", "dpi": 300, "page_width_in": 1.0, "page_height_in": 1.25,
    }
    validate_job(sidecar)


# --- the agent's side: it becomes a filename ----------------------------------

FALLBACK = Path("20260919T032949Z_BL_0100-0102_418a8d09.png")


def test_the_agent_uses_the_name_from_the_sidecar():
    assert job_name({"job_name": "Blue CAT6 - BL-0100...BL-0102"}, FALLBACK) == \
        "Blue CAT6 - BL-0100...BL-0102"


@pytest.mark.parametrize("raw", ["a/b", "a\\b", "a:b", 'a"b', "a<b>c", "a|b", "a?b", "a*b", "a\x01b"])
def test_characters_windows_forbids_in_a_filename_are_removed(raw):
    cleaned = job_name({"job_name": raw}, FALLBACK)
    assert not set(cleaned) & set('<>:"/\\|?*')
    assert cleaned


def test_a_missing_or_blank_name_falls_back_to_the_payload_name():
    assert job_name({}, FALLBACK) == FALLBACK.stem
    assert job_name({"job_name": "   "}, FALLBACK) == FALLBACK.stem
    assert job_name({"job_name": "///"}, FALLBACK) == FALLBACK.stem


def test_a_long_name_is_truncated_to_a_usable_filename():
    cleaned = job_name({"job_name": "N" * 500}, FALLBACK)
    assert len(cleaned) == MAX_JOB_NAME


def test_trailing_dots_and_spaces_go_because_windows_drops_them():
    assert job_name({"job_name": "Blue CAT6 . "}, FALLBACK) == "Blue CAT6"
