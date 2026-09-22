"""Configuration loading and fail-fast validation.

Two layers, in this order:
  1. config/*.yaml  -- printer geometry, label style, the Halo field map
  2. .env           -- secrets, the asset group, operational settings

There is no CLI layer here (unlike the sibling project's print tool),
because the poll loop is a service, not something anyone runs by hand with
flags. tools/ scripts build their own overrides on top of what this
returns.

A misconfigured print service must not start. Everything that can be
checked at startup is checked here, and the errors are collected so one
run surfaces all of them rather than one per restart. See spec 5.2.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import StartupError
from .layout import LabelStyle, Media

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"


@dataclass(frozen=True)
class HaloConfig:
    base_url: str
    auth_url: str
    client_id: str
    client_secret: str
    qty_field_id: int
    prefix_field_id: int
    nextid_field_id: int
    status_field_id: int
    # Optional: id 0 means the tenant has no colour field, and the job name
    # simply drops it.
    color_field_id: int = 0
    qty_field_name: str = "Cables to Label"
    prefix_field_name: str = "Cable ID Prefix"
    nextid_field_name: str = "Next Cable ID"
    status_field_name: str = "Last Label Run"
    color_field_name: str = "Cable Color"
    asset_group_id: int | None = None
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class QueueConfig:
    root: Path
    write_retries: int = 3
    write_retry_seconds: float = 5.0
    failed_reminder_minutes: int = 60


@dataclass(frozen=True)
class AppConfig:
    halo: HaloConfig
    queue: QueueConfig
    media: Media
    style: LabelStyle
    media_configured: bool = False
    poll_interval_seconds: int = 15
    max_assets_per_poll: int = 10
    labels_per_cable: int = 2
    display_timezone: str = "UTC"
    status_field_max_chars: int = 255
    status_write_max_attempts: int = 5
    stalled_after_minutes: int = 30
    shadow_mode: bool = False
    dry_run: bool = False
    log_level: str = "INFO"
    lock_path: str = "/tmp/halo-cable-label.lock"


# --- small helpers -----------------------------------------------------------

def env_bool(env: dict, name: str, default: bool = False) -> bool:
    val = env.get(name)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _env_int(env: dict, name: str, default: int, errors: list) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        errors.append(f"{name} must be a whole number, got {raw!r}")
        return default


def _env_float(env: dict, name: str, default: float, errors: list) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        errors.append(f"{name} must be a number, got {raw!r}")
        return default


def _load_yaml(path: Path, errors: list) -> dict:
    if not path.exists():
        errors.append(
            f"{path} not found -- copy {path.with_suffix('')}.example{path.suffix} "
            f"and edit it for your site"
        )
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        errors.append(f"malformed YAML in {path}: {exc}")
        return {}


# --- section loaders ---------------------------------------------------------

def _load_media(config_dir: Path, errors: list) -> tuple[Media, bool]:
    """Returns (media, configured). `configured` is the adopter's assertion
    that the geometry was MEASURED, which gates printing rather than startup."""
    data = _load_yaml(config_dir / "printers.yaml", errors)
    raw = data.get("printer")
    if not isinstance(raw, dict):
        if data:
            errors.append(
                f"{config_dir / 'printers.yaml'} needs a top-level `printer:` section "
                f"-- see printers.example.yaml"
            )
        return Media(), False

    # `configured` means "safe to PRINT with", not "safe to start". Media
    # geometry cannot be guessed or validated by software, only measured, so
    # leaving it false blocks anything that could reach media. It does NOT
    # block SHADOW_MODE, because the whole point of shadow mode is to bring
    # the Halo side up before the printer exists -- and requiring someone to
    # tick "I measured it" in order to do that just trains them to tick it.
    configured = bool(raw.get("configured", False))

    def num(key, default):
        value = raw.get(key, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            errors.append(f"printers.yaml: printer.{key} must be a number, got {value!r}")
            return default

    def optional_num(key):
        """None means "centre it", which is right for die-cut stock and
        wrong for self-laminating wrap."""
        value = raw.get(key)
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            errors.append(f"printers.yaml: printer.{key} must be a number or blank, got {value!r}")
            return None

    media = Media(
        dpi=int(raw.get("dpi", 300) or 300),
        label_width_in=num("label_width_in", Media.label_width_in),
        label_height_in=num("label_height_in", Media.label_height_in),
        print_area_width_in=num("print_area_width_in", Media.print_area_width_in),
        print_area_height_in=num("print_area_height_in", Media.print_area_height_in),
        margin_in=num("margin_in", Media.margin_in),
        print_area_x_in=optional_num("print_area_x_in"),
        print_area_y_in=optional_num("print_area_y_in"),
    )

    if media.dpi <= 0:
        errors.append(f"printers.yaml: printer.dpi must be positive, got {media.dpi}")
    for name in ("label_width_in", "label_height_in", "print_area_width_in", "print_area_height_in"):
        if getattr(media, name) <= 0:
            errors.append(f"printers.yaml: printer.{name} must be positive")
    if media.print_area_width_in > media.label_width_in:
        errors.append(
            f"printers.yaml: printer.print_area_width_in "
            f"({media.print_area_width_in}) exceeds label_width_in ({media.label_width_in}) "
            f"-- the printed zone is part of the label, not larger than it"
        )
    if media.print_area_height_in > media.label_height_in:
        errors.append(
            f"printers.yaml: printer.print_area_height_in "
            f"({media.print_area_height_in}) exceeds label_height_in ({media.label_height_in})"
        )
    if media.print_area_x_in is not None and (
        media.print_area_x_in < 0
        or media.print_area_x_in + media.print_area_width_in > media.label_width_in + 1e-6
    ):
        errors.append(
            f"printers.yaml: printer.print_area_x_in ({media.print_area_x_in}) puts the "
            f"printed zone off the label -- it must sit between 0 and "
            f"{media.label_width_in - media.print_area_width_in:.3f}"
        )
    if media.print_area_y_in is not None and (
        media.print_area_y_in < 0
        or media.print_area_y_in + media.print_area_height_in > media.label_height_in + 1e-6
    ):
        errors.append(
            f"printers.yaml: printer.print_area_y_in ({media.print_area_y_in}) puts the "
            f"printed zone off the label -- it must sit between 0 and "
            f"{media.label_height_in - media.print_area_height_in:.3f}"
        )
    if media.usable_width_pt <= 0 or media.usable_height_pt <= 0:
        errors.append(
            f"printers.yaml: printer.margin_in ({media.margin_in}) leaves no usable "
            f"print area inside the printed zone"
        )
    return media, configured


def _load_style(config_dir: Path, errors: list) -> LabelStyle:
    data = _load_yaml(config_dir / "layout.yaml", errors)
    raw = data.get("layout") or {}
    style = LabelStyle(
        separator=str(raw.get("separator", LabelStyle.separator)),
        pad_width=int(raw.get("pad_width", LabelStyle.pad_width) or LabelStyle.pad_width),
        prefix_pattern=str(raw.get("prefix_pattern", LabelStyle.prefix_pattern)),
        font=str(raw.get("font", LabelStyle.font)),
        min_font_pt=float(raw.get("min_font_pt", LabelStyle.min_font_pt)),
        max_font_pt=float(raw.get("max_font_pt", LabelStyle.max_font_pt)),
        legend_repeat=int(raw.get("legend_repeat", LabelStyle.legend_repeat) or 1),
        legend_gap_in=float(raw.get("legend_gap_in", LabelStyle.legend_gap_in)),
    )
    if style.pad_width < 1:
        errors.append(f"layout.yaml: pad_width must be at least 1, got {style.pad_width}")
    if style.legend_repeat < 1:
        errors.append(f"layout.yaml: legend_repeat must be at least 1, got {style.legend_repeat}")
    if style.min_font_pt <= 0 or style.max_font_pt < style.min_font_pt:
        errors.append(
            f"layout.yaml: need 0 < min_font_pt <= max_font_pt, got "
            f"{style.min_font_pt} and {style.max_font_pt}"
        )
    if not style.separator:
        errors.append("layout.yaml: separator must not be empty")
    try:
        compiled = re.compile(style.prefix_pattern)
    except re.error as exc:
        errors.append(f"layout.yaml: prefix_pattern is not a valid regex: {exc}")
    else:
        # A separator that the prefix pattern also admits makes a stem
        # ambiguous and an identifier unparseable by eye.
        if compiled.match(style.separator):
            errors.append(
                f"layout.yaml: separator {style.separator!r} is itself allowed by "
                f"prefix_pattern -- pick a separator that cannot appear in a prefix"
            )
    return style


# role in fields.yaml -> HaloConfig attribute stem. The default names on
# HaloConfig are only what the example file ships; the name that matters is
# the one the adopter writes, which tools/check_halo.py verifies against Halo.
FIELD_ROLES = {"qty": "qty", "prefix": "prefix", "next_id": "nextid", "status": "status"}
# Roles the service works without. Their id may be blank.
OPTIONAL_ROLES = ("color",)


def _load_fields(config_dir: Path, errors: list, required: bool) -> dict:
    """The four Halo custom fields, from config/fields.yaml.

    Fields are addressed by numeric id, never by name: a name mismatch fails
    silently by matching nothing, and ids do not drift. The name is recorded
    alongside so error messages name the field the way Halo shows it, and so
    check_halo.py can confirm each id really is the field it is meant to be.

    Returns HaloConfig keyword arguments. Tools that never contact Halo pass
    required=False and get zero ids rather than an error.
    """
    path = config_dir / "fields.yaml"
    if not required and not path.exists():
        return {f"{stem}_field_id": 0 for stem in FIELD_ROLES.values()}
    file_errors: list[str] = []
    data = _load_yaml(path, file_errors)
    if required:
        errors.extend(file_errors)
    raw = data.get("fields") or {}

    out = {}
    for role, stem in list(FIELD_ROLES.items()) + [(r, r) for r in OPTIONAL_ROLES]:
        optional = role in OPTIONAL_ROLES
        entry = raw.get(role)
        entry = entry if isinstance(entry, dict) else {}
        name = str(entry.get("name") or getattr(HaloConfig, f"{stem}_field_name")).strip()
        out[f"{stem}_field_name"] = name
        value = entry.get("id")
        field_id = 0
        if value is None or str(value).strip() == "":
            if required and not optional and not file_errors:
                errors.append(
                    f"fields.yaml: {role}.id is required and has no default -- find the "
                    f"numeric id of the {name!r} custom field in Halo "
                    f"(Configuration > Custom Fields)"
                )
        else:
            try:
                field_id = int(value)
            except (TypeError, ValueError):
                errors.append(f"fields.yaml: {role}.id must be the numeric Halo field id, got {value!r}")
            else:
                if field_id <= 0:
                    errors.append(f"fields.yaml: {role}.id must be a positive Halo field id, got {field_id}")
                    field_id = 0
        out[f"{stem}_field_id"] = field_id
    return out


# --- entry point -------------------------------------------------------------

def load_config(
    config_dir: Path | None = None,
    env: dict | None = None,
    require_halo: bool = True,
) -> AppConfig:
    """Build and validate the whole configuration, or raise StartupError
    listing every problem found.

    `require_halo=False` skips the tenant credentials and field ids. Several
    tools -- rendering a test block, enqueueing a test job, reprinting a
    known range -- never contact Halo at all, and demanding a client secret
    before they will draw a label is friction with no safety value. They
    still get the same media, layout and queue configuration as the service.
    """
    env = dict(os.environ if env is None else env)
    config_dir = Path(config_dir or env.get("CONFIG_DIR") or DEFAULT_CONFIG_DIR)
    errors: list[str] = []

    media, media_configured = _load_media(config_dir, errors)
    style = _load_style(config_dir, errors)
    field_settings = _load_fields(config_dir, errors, require_halo)

    if require_halo:
        for name in ("HALO_BASE_URL", "HALO_AUTH_URL", "HALO_CLIENT_ID", "HALO_CLIENT_SECRET"):
            if not (env.get(name) or "").strip():
                errors.append(f"{name} is required in .env")

    def optional_id(name):
        raw = (env.get(name) or "").strip()
        if not raw:
            return None
        try:
            value = int(raw)
        except ValueError:
            errors.append(f"{name} must be numeric, got {raw!r}")
            return None
        if value <= 0:
            errors.append(f"{name} must be a positive id, got {value}")
        return value

    asset_group_id = optional_id("HALO_CABLE_ASSET_GROUP_ID")

    halo = HaloConfig(
        base_url=(env.get("HALO_BASE_URL") or "").strip(),
        auth_url=(env.get("HALO_AUTH_URL") or "").strip(),
        client_id=(env.get("HALO_CLIENT_ID") or "").strip(),
        client_secret=(env.get("HALO_CLIENT_SECRET") or "").strip(),
        **field_settings,
        asset_group_id=asset_group_id,
        timeout_seconds=_env_float(env, "HALO_TIMEOUT_SECONDS", 10.0, errors),
    )

    field_ids = [halo.qty_field_id, halo.prefix_field_id, halo.nextid_field_id,
                 halo.status_field_id, halo.color_field_id]
    if len(set(f for f in field_ids if f)) != len([f for f in field_ids if f]):
        errors.append(
            "fields.yaml: two or more field ids are the same -- each of the four custom "
            "fields has its own id, and reusing one would overwrite the wrong field"
        )

    queue_root = (env.get("QUEUE_ROOT") or "").strip()
    if not queue_root:
        errors.append("QUEUE_ROOT is required in .env -- the mount point of the print queue share")
    queue = QueueConfig(
        root=Path(queue_root or "/mnt/cable-queue"),
        write_retries=_env_int(env, "QUEUE_WRITE_RETRIES", 3, errors),
        write_retry_seconds=_env_float(env, "QUEUE_WRITE_RETRY_SECONDS", 5.0, errors),
        failed_reminder_minutes=_env_int(env, "FAILED_REMINDER_MINUTES", 60, errors),
    )

    cfg = AppConfig(
        halo=halo,
        queue=queue,
        media=media,
        style=style,
        media_configured=media_configured,
        poll_interval_seconds=_env_int(env, "POLL_INTERVAL_SECONDS", 15, errors),
        max_assets_per_poll=_env_int(env, "MAX_ASSETS_PER_POLL", 10, errors),
        labels_per_cable=_env_int(env, "LABELS_PER_CABLE", 2, errors),
        display_timezone=(env.get("DISPLAY_TIMEZONE") or "UTC").strip(),
        status_field_max_chars=_env_int(env, "STATUS_FIELD_MAX_CHARS", 255, errors),
        status_write_max_attempts=_env_int(env, "STATUS_WRITE_MAX_ATTEMPTS", 5, errors),
        stalled_after_minutes=_env_int(env, "STALLED_AFTER_MINUTES", 30, errors),
        shadow_mode=env_bool(env, "SHADOW_MODE"),
        dry_run=env_bool(env, "DRY_RUN"),
        log_level=(env.get("LOG_LEVEL") or "INFO").strip().upper(),
        lock_path=(env.get("LOCK_PATH") or "/tmp/halo-cable-label.lock").strip(),
    )

    if cfg.poll_interval_seconds < 1:
        errors.append(f"POLL_INTERVAL_SECONDS must be at least 1, got {cfg.poll_interval_seconds}")
    if cfg.max_assets_per_poll < 1:
        errors.append(f"MAX_ASSETS_PER_POLL must be at least 1, got {cfg.max_assets_per_poll}")
    if cfg.labels_per_cable < 1:
        errors.append(
            f"LABELS_PER_CABLE must be at least 1, got {cfg.labels_per_cable} "
            f"(it is 2 for cables labelled at both ends)"
        )
    if cfg.status_field_max_chars < 40:
        errors.append(
            f"STATUS_FIELD_MAX_CHARS is {cfg.status_field_max_chars}; below about 40 there is "
            f"no room for a status word and an identifier range"
        )
    if cfg.status_write_max_attempts < 1:
        errors.append("STATUS_WRITE_MAX_ATTEMPTS must be at least 1")
    if cfg.stalled_after_minutes < 1:
        errors.append(f"STALLED_AFTER_MINUTES must be at least 1, got {cfg.stalled_after_minutes}")

    # Unmeasured media blocks printing, not startup. SHADOW_MODE reaches no
    # media by construction, so it is allowed through with a warning banner
    # (see main.startup_banner).
    if not cfg.media_configured and not cfg.shadow_mode:
        errors.append(
            f"{config_dir / 'printers.yaml'} still has "
            f"configured: false, so the media geometry has not been measured. Either "
            f"measure your label stock (overall size, the opaque printed zone, and the "
            f"printer's DPI), fill those in and set configured: true -- or set "
            f"SHADOW_MODE=1 to bring up the Halo side without printing anything."
        )

    if errors:
        raise StartupError(
            "Invalid configuration:\n  - " + "\n  - ".join(errors)
        )
    return cfg
