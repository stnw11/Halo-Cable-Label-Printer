"""Field lookup against the shapes Halo actually returns.

This is the layer that meets the real tenant first, and its failures are
silent by nature: a wrong id or a misread dropdown produces a plausible
value, not an error. The fixture below is shaped like a live list response.
"""
import pytest

from src.fields import as_int, as_text, field_by_id, field_entry, field_text_by_id

ASSET = {
    "id": 4711,
    "inventory_number": "cable-type-1",
    "fields": [
        # A dropdown: `value` is the option index, `display` the text.
        {"id": 812, "name": "Cable ID Prefix", "value": 2, "display": "BL", "lookup": 248},
        {"id": 813, "name": "Next Cable ID", "value": "100", "display": "100", "lookup": 0},
        {"id": 811, "name": "Cables to Label", "value": "", "display": "", "lookup": 0},
    ],
    # Assets ALSO carry a customfields array. On the live tenant it holds
    # unrelated fields, so it must never satisfy a lookup -- even by an id
    # that happens to match.
    "customfields": [{"id": 999, "name": "Accountable", "value": "x"}],
}


def test_lookup_by_id_returns_the_raw_value():
    assert field_by_id(ASSET, 812) == 2
    assert field_by_id(ASSET, 813) == "100"


def test_customfields_is_not_searched():
    assert field_by_id(ASSET, 999) is None
    assert field_entry(ASSET, 999) is None


def test_missing_id_is_none_not_an_error():
    """Halo leaves empty fields out of the list response entirely."""
    assert field_by_id(ASSET, 12345) is None
    assert field_text_by_id(ASSET, 12345) == ""


def test_field_entry_exposes_the_name_halo_uses():
    """check_halo.py compares this against config/fields.yaml."""
    assert field_entry(ASSET, 812)["name"] == "Cable ID Prefix"


def test_text_of_a_dropdown_is_its_display():
    assert field_text_by_id(ASSET, 812) == "BL"


def test_text_of_a_plain_field_is_its_value():
    assert field_text_by_id(ASSET, 813) == "100"


def test_missing_fields_array_is_not_an_error():
    assert field_by_id({"id": 1}, 812) is None
    assert field_by_id({"id": 1, "fields": None}, 812) is None


@pytest.mark.parametrize(
    "value,expected",
    [
        (100, 100),
        ("100", 100),        # Halo returns integers as strings often enough
        ("  100  ", 100),
        (100.0, 100),
        ("100.0", 100),
        (0, 0),
        ("-5", -5),
    ],
)
def test_as_int_coerces_what_it_should(value, expected):
    assert as_int(value, default="SENTINEL") == expected


@pytest.mark.parametrize("value", ["", "   ", None, "abc", "1.5", 1.5, True, False])
def test_as_int_passes_junk_through_untouched(value):
    """Returning the default UNCHANGED is what lets the allocator report
    what it actually found rather than a coercion error."""
    assert as_int(value, default="SENTINEL") == "SENTINEL"


def test_as_text_normalises():
    assert as_text(None) == ""
    assert as_text("  BL  ") == "BL"
    assert as_text(100) == "100"
