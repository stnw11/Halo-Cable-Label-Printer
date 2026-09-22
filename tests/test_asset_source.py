"""get_pending() against Halo-shaped responses."""

from src.asset_source import build_cable_type, get_pending
from tests.conftest import NEXTID_FIELD, PREFIX_FIELD, QTY_FIELD


def asset(asset_id=1, prefix="BL", next_id="100", qty="0", **extra):
    return {
        "id": asset_id,
        "inventory_number": f"cable-type-{asset_id}",
        "fields": [
            {"id": PREFIX_FIELD, "name": "Cable ID Prefix", "value": prefix},
            {"id": NEXTID_FIELD, "name": "Next Cable ID", "value": next_id},
            {"id": QTY_FIELD, "name": "Cables to Label", "value": qty},
        ],
        **extra,
    }


class Tenant:
    def __init__(self, assets):
        self.assets = assets
        self.asset_group_id_seen = "unset"

    def iter_assets(self, page_size=50, asset_group_id=None):
        self.asset_group_id_seen = asset_group_id
        yield from self.assets


def test_only_positive_quantities_are_pending(app_config):
    tenant = Tenant([
        asset(1, qty="0"),
        asset(2, qty="3"),
        asset(3, qty=""),
        asset(4, qty="-1"),
        asset(5, qty="12"),
    ])
    pending = get_pending(tenant, app_config)
    assert [c.id for c in pending] == [2, 5]


def test_empty_tenant_warns_about_the_usual_cause(app_config, caplog):
    """An empty list is far more often the API application's permissions
    than a genuinely empty tenant, so the log says so and names the group
    being asked for."""
    with caplog.at_level("WARNING"):
        assert get_pending(Tenant([]), _cfg_with_group(app_config, 42)) == []
    assert "PERMISSIONS" in caplog.text
    assert "group 42" in caplog.text


def test_values_are_coerced_from_halo_strings(app_config):
    cable = build_cable_type(asset(1, prefix=" BL ", next_id="250", qty="7"), app_config)
    assert cable.prefix == "BL"
    assert cable.next_id == 250
    assert cable.qty == 7


def test_unparseable_counter_survives_to_the_allocator(app_config):
    """build_cable_type must NOT coerce a bad counter to a number -- the
    allocator refuses it with a message naming what was actually there."""
    cable = build_cable_type(asset(1, next_id="not a number", qty="1"), app_config)
    assert cable.next_id == "not a number"


def _with_dropdown_prefix(index, display):
    """The prefix entry exactly as the live tenant returns a dropdown field:
    the option's index in `value`, its text in `display`, non-zero `lookup`."""
    a = asset(1, next_id="100", qty="3")
    a["fields"][0] = {
        "id": PREFIX_FIELD, "name": "Cable ID Prefix", "validate": "V",
        "value": index, "display": display, "lookup": 248, "typeinfo_id": 3,
    }
    return a


def test_dropdown_prefix_uses_the_text_not_the_option_index(app_config):
    """Measured regression: a dropdown prefix printed its option index, 2-0100, not BL-0100."""
    cable = build_cable_type(_with_dropdown_prefix(2, "BL"), app_config)
    assert cable.prefix == "BL"


def test_unset_dropdown_prefix_is_empty_not_an_index(app_config):
    """Nothing selected must reach the allocator as no prefix, which it
    refuses, rather than as a number it would happily print."""
    cable = build_cable_type(_with_dropdown_prefix(0, None), app_config)
    assert cable.prefix == ""


def test_plain_text_prefix_still_reads_value(app_config):
    """A text field has lookup 0 and a display that may be absent."""
    a = asset(1, prefix="GR")
    a["fields"][0].update({"lookup": 0, "display": None})
    assert build_cable_type(a, app_config).prefix == "GR"


def test_missing_fields_array_does_not_explode(app_config):
    cable = build_cable_type({"id": 9}, app_config)
    assert cable.prefix == ""
    assert cable.qty == 0


# --- asset group scoping -----------------------------------------------------

def _cfg_with_group(app_config, group_id):
    from dataclasses import replace

    return replace(app_config, halo=replace(app_config.halo, asset_group_id=group_id))


def test_group_id_is_what_scopes_the_poll(app_config):
    """Halo applies the group; the live tenant returns no group key on an
    asset, so there is nothing to re-check it against client-side."""
    tenant = Tenant([asset(1, qty="1")])
    get_pending(tenant, _cfg_with_group(app_config, 42))
    assert tenant.asset_group_id_seen == 42


def test_no_group_configured_asks_for_everything(app_config):
    tenant = Tenant([asset(1, qty="3")])
    assert [c.id for c in get_pending(tenant, app_config)] == [1]
    assert tenant.asset_group_id_seen is None
