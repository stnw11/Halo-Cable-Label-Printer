"""Read custom field values out of a Halo asset's JSON.

Assets fetched with includeassetfields=true carry a `fields` array of
{id, name, value, display, lookup, ...}. Fields are looked up by numeric id
only (config/fields.yaml): Halo custom field names are case-sensitive and a
mismatch fails silently by matching nothing, forever; ids do not drift.

Assets also carry a separate `customfields` array. On the live tenant it
holds unrelated fields and never the cable fields, so it is not searched.
"""
from __future__ import annotations

from typing import Any


def field_entry(asset: dict, field_id: int) -> dict | None:
    """The whole `fields` entry with this id, or None. Halo omits a field
    with no value from the list endpoint, so None is normal for an empty
    field, not evidence of a wrong id on its own."""
    fields = asset.get("fields")
    if isinstance(fields, list):
        for entry in fields:
            if isinstance(entry, dict) and entry.get("id") == field_id:
                return entry
    return None


def field_by_id(asset: dict, field_id: int) -> Any:
    """A custom field's raw `value`, looked up by numeric Halo id."""
    entry = field_entry(asset, field_id)
    return None if entry is None else entry.get("value")


def field_text_by_id(asset: dict, field_id: int) -> str:
    """A custom field's human-readable text, looked up by numeric id.

    For a dropdown (a non-zero `lookup`), Halo's `value` is the selected
    option's INDEX and the text lives in `display`: measured on the live
    tenant, a prefix of "BL" arrives as, e.g., value 2, display "BL". Reading
    `value` there silently prints "2-0100". A dropdown with nothing selected
    resolves to "" rather than falling back to the index, so the allocator
    refuses it instead of printing a number as a prefix.
    """
    entry = field_entry(asset, field_id)
    if entry is None:
        return ""
    if entry.get("lookup"):
        return as_text(entry.get("display"))
    return as_text(entry.get("value"))


def as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def as_int(value: Any, default: Any = None) -> Any:
    """Coerce a Halo field value to int, or return `default` unchanged.

    Halo returns integers as strings often enough that a strict int() would
    reject perfectly good counters. Anything genuinely non-numeric is passed
    through untouched, so the allocator can refuse it with a message naming
    what it actually found rather than a coercion error.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else default
    text = as_text(value)
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        try:
            as_float = float(text)
        except ValueError:
            return default
        return int(as_float) if as_float.is_integer() else default
