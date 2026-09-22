"""Configuration loading and fail-fast validation.

A misconfigured print service must not start, and it must say everything
that is wrong in one pass rather than one error per restart.
"""
import pytest

from src.config import load_config
from src.errors import StartupError

GOOD_ENV = {
    "HALO_BASE_URL": "https://tenant.example",
    "HALO_AUTH_URL": "https://tenant.example/auth/token",
    "HALO_CLIENT_ID": "id",
    "HALO_CLIENT_SECRET": "secret",
    "QUEUE_ROOT": "/mnt/cable-queue",
}

PRINTERS = """
printer:
  configured: true
  dpi: 300
  label_width_in: 2.0
  label_height_in: 0.75
  print_area_width_in: 1.0
  print_area_height_in: 0.5
  margin_in: 0.03
"""

LAYOUT = """
layout:
  separator: "-"
  pad_width: 4
  prefix_pattern: "^[A-Za-z0-9]{1,8}$"
  legend_repeat: 2
  legend_gap_in: 0.08
"""

FIELDS = """
fields:
  qty:     {id: 811, name: "Cables to Label"}
  prefix:  {id: 812, name: "Cable ID Prefix"}
  next_id: {id: 813, name: "Next Cable ID"}
  status:  {id: 814, name: "Last Label Run"}
"""


@pytest.fixture
def config_dir(tmp_path):
    d = tmp_path / "config"
    d.mkdir()
    (d / "printers.yaml").write_text(PRINTERS)
    (d / "layout.yaml").write_text(LAYOUT)
    (d / "fields.yaml").write_text(FIELDS)
    return d


def test_a_good_config_loads(config_dir):
    cfg = load_config(config_dir, dict(GOOD_ENV))
    assert cfg.halo.nextid_field_id == 813
    assert cfg.halo.prefix_field_name == "Cable ID Prefix"
    assert cfg.media.label_width_in == 2.0
    assert cfg.labels_per_cable == 2


def test_missing_everything_reports_everything_at_once(tmp_path):
    """One run should surface every problem, not the first one."""
    with pytest.raises(StartupError) as exc:
        load_config(tmp_path / "nope", {})
    message = str(exc.value)
    for expected in ("printers.yaml", "HALO_BASE_URL", "fields.yaml", "QUEUE_ROOT"):
        assert expected in message
    assert message.count("\n  - ") >= 8


def test_unmeasured_media_refuses_to_print(config_dir):
    """Media geometry cannot be guessed or validated by software, only
    measured. A forgotten measurement must fail at startup rather than
    produce a run of unreadable labels."""
    (config_dir / "printers.yaml").write_text(PRINTERS.replace("configured: true", "configured: false"))
    with pytest.raises(StartupError, match="configured: true"):
        load_config(config_dir, dict(GOOD_ENV))


def test_unmeasured_media_is_allowed_in_shadow_mode(config_dir):
    """`configured` means "safe to print with", not "safe to start". Shadow
    mode reaches no media by construction, and the Halo side has to be
    brought up before the printer exists. Requiring someone to tick "I
    measured it" in order to do that only trains them to tick it."""
    (config_dir / "printers.yaml").write_text(PRINTERS.replace("configured: true", "configured: false"))
    cfg = load_config(config_dir, dict(GOOD_ENV, SHADOW_MODE="1"))
    assert cfg.media_configured is False
    assert cfg.shadow_mode is True


def test_measured_media_is_flagged_as_such(config_dir):
    assert load_config(config_dir, dict(GOOD_ENV)).media_configured is True


def test_field_ids_are_required_with_no_default(config_dir):
    (config_dir / "fields.yaml").write_text(FIELDS.replace("id: 813", "id: "))
    with pytest.raises(StartupError, match="next_id.id is required") as exc:
        load_config(config_dir, dict(GOOD_ENV))
    assert "Next Cable ID" in str(exc.value)   # names the field the way Halo shows it


def test_non_numeric_field_id_is_rejected(config_dir):
    (config_dir / "fields.yaml").write_text(FIELDS.replace("id: 812", 'id: "BU"'))
    with pytest.raises(StartupError, match="prefix.id must be the numeric"):
        load_config(config_dir, dict(GOOD_ENV))


def test_duplicate_field_ids_are_rejected(config_dir):
    """Reusing one id would overwrite the wrong field -- and one of these
    fields is the counter."""
    (config_dir / "fields.yaml").write_text(FIELDS.replace("id: 814", "id: 813"))
    with pytest.raises(StartupError, match="same"):
        load_config(config_dir, dict(GOOD_ENV))


def test_field_names_default_when_omitted(config_dir):
    (config_dir / "fields.yaml").write_text(
        "fields:\n  qty: {id: 811}\n  prefix: {id: 812}\n  next_id: {id: 813}\n  status: {id: 814}\n"
    )
    assert load_config(config_dir, dict(GOOD_ENV)).halo.status_field_name == "Last Label Run"


def test_tools_that_never_contact_halo_need_no_field_map(config_dir):
    """render_test_block.py and friends must work before Halo is set up."""
    (config_dir / "fields.yaml").unlink()
    env = {"QUEUE_ROOT": "/mnt/cable-queue"}
    cfg = load_config(config_dir, env, require_halo=False)
    assert cfg.halo.nextid_field_id == 0


def test_malformed_yaml_names_the_file(config_dir):
    (config_dir / "layout.yaml").write_text("layout:\n  - [unclosed\n")
    with pytest.raises(StartupError, match="malformed YAML"):
        load_config(config_dir, dict(GOOD_ENV))


def test_printed_zone_larger_than_the_label_is_rejected(config_dir):
    (config_dir / "printers.yaml").write_text(PRINTERS.replace("print_area_width_in: 1.0", "print_area_width_in: 9.0"))
    with pytest.raises(StartupError, match="exceeds label_width_in"):
        load_config(config_dir, dict(GOOD_ENV))


def test_margins_that_leave_no_print_area_are_rejected(config_dir):
    (config_dir / "printers.yaml").write_text(PRINTERS.replace("margin_in: 0.03", "margin_in: 0.9"))
    with pytest.raises(StartupError, match="no usable"):
        load_config(config_dir, dict(GOOD_ENV))


def test_a_separator_the_prefix_pattern_allows_is_rejected(config_dir):
    """Otherwise BL-0100 cannot be read back unambiguously, and the queue
    filename stem stops parsing."""
    (config_dir / "layout.yaml").write_text(LAYOUT.replace('separator: "-"', 'separator: "A"'))
    with pytest.raises(StartupError, match="separator"):
        load_config(config_dir, dict(GOOD_ENV))


def test_invalid_prefix_regex_is_caught_at_startup(config_dir):
    (config_dir / "layout.yaml").write_text(LAYOUT.replace('"^[A-Za-z0-9]{1,8}$"', '"^[unclosed"'))
    with pytest.raises(StartupError, match="not a valid regex"):
        load_config(config_dir, dict(GOOD_ENV))


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("POLL_INTERVAL_SECONDS", "0", "at least 1"),
        ("LABELS_PER_CABLE", "0", "at least 1"),
        ("STATUS_FIELD_MAX_CHARS", "10", "no room"),
        ("MAX_ASSETS_PER_POLL", "abc", "whole number"),
    ],
)
def test_operational_settings_are_validated(config_dir, key, value, expected):
    with pytest.raises(StartupError, match=expected):
        load_config(config_dir, dict(GOOD_ENV, **{key: value}))


def test_old_multi_profile_printers_file_is_explained(config_dir):
    """The pre-flattening `printers: default:` shape must fail with a
    pointer to the new shape, not with every dimension reported missing."""
    (config_dir / "printers.yaml").write_text(
        "printers:\n  default:\n" + "".join("  " + line + "\n" for line in PRINTERS.splitlines()[2:])
    )
    with pytest.raises(StartupError, match="top-level `printer:` section"):
        load_config(config_dir, dict(GOOD_ENV))


def test_shadow_and_dry_run_parse_as_booleans(config_dir):
    cfg = load_config(config_dir, dict(GOOD_ENV, SHADOW_MODE="1", DRY_RUN="yes"))
    assert cfg.shadow_mode is True
    assert cfg.dry_run is True
    cfg = load_config(config_dir, dict(GOOD_ENV, SHADOW_MODE="0", DRY_RUN=""))
    assert cfg.shadow_mode is False
    assert cfg.dry_run is False
