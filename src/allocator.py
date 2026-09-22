"""Identifier allocation: validate, reserve, verify.

This is the part of the system that must not be wrong. Everything else is
conventional; this is not. See spec section 4.

The order is load-bearing and inverts what both sibling projects do:

    validate  ->  reserve  ->  render  ->  enqueue

The siblings render before claiming, so a layout error never eats the
trigger field. Here, reserving AFTER a successful print would reissue the
same identifiers if the service died mid-batch, and two cables in a rack
labelled BL-0104 is a fault that survives for years and is diagnosed the
hard way. Reserving first means a crash burns a block and leaves a gap in
the sequence, and nobody audits cable numbers for contiguity.

So: bias toward gaps, never toward collisions.

To keep that inversion from eating requests on trivial mistakes, EVERYTHING
that can be checked without writing is checked in validate(), including a
dry layout check on the widest identifier the block would contain. That
check is deterministic from the block's bounds, so it needs no rendering.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from .config import AppConfig
from .errors import LayoutError, ReserveError, ValidationError
from .guards import clamp_qty
from .layout import check_fits
from .models import CableType, Plan, Reservation
from .naming import format_block, format_identifier, warn_on_overflow
from .status import QUEUED, format_status_line

logger = logging.getLogger(__name__)


def validate(asset: CableType, cfg: AppConfig) -> Plan:
    """Turn a pending asset into a Plan, or raise ValidationError.

    Writes nothing and consumes nothing. A ValidationError means the asset
    stays flagged, so the request survives until a human fixes the value.
    """
    prefix = (asset.prefix or "").strip()
    if not prefix:
        raise ValidationError(
            f"asset {asset.id} has no {cfg.halo.prefix_field_name} value -- set the "
            f"cable type's prefix before requesting labels"
        )
    if not re.match(cfg.style.prefix_pattern, prefix):
        raise ValidationError(
            f"asset {asset.id} prefix {prefix!r} does not match layout.prefix_pattern "
            f"({cfg.style.prefix_pattern}) -- prefixes must be safe to put in a filename "
            f"and unambiguous next to the {cfg.style.separator!r} separator"
        )

    # The counter is the only durable state in the system. A blank or absurd
    # value must never fall back to "start from 1": silently restarting a
    # sequence reissues numbers that are already on installed cables.
    if not isinstance(asset.next_id, int) or isinstance(asset.next_id, bool):
        raise ValidationError(
            f"asset {asset.id} has a non-numeric {cfg.halo.nextid_field_name} "
            f"({asset.next_id!r}) -- set it to the next number to issue"
        )
    if asset.next_id < 1:
        raise ValidationError(
            f"asset {asset.id} has {cfg.halo.nextid_field_name} = {asset.next_id}; it must "
            f"be a positive integer. Refusing to guess a starting point, because guessing "
            f"would reissue numbers that may already be on installed cables."
        )

    if asset.qty < 1:
        raise ValidationError(f"asset {asset.id} has no positive quantity ({asset.qty})")

    qty, clamped = clamp_qty(asset)

    first = asset.next_id
    last = first + qty - 1
    plan = Plan(
        asset_id=asset.id,
        prefix=prefix,
        first_number=first,
        last_number=last,
        cable_count=qty,
        labels_per_cable=cfg.labels_per_cable,
        requested_qty=asset.qty,
        color=asset.color,
        type_name=asset.type_name,
        clamped=clamped,
    )

    warn_on_overflow(first, last, cfg.style.pad_width)

    # Dry layout check on the widest identifier in the block. Costs nothing,
    # writes nothing, and turns "the last label in a 500-cable run would have
    # been clipped" into a refusal before any number is consumed.
    widest = format_identifier(prefix, last, cfg.style.separator, cfg.style.pad_width)
    try:
        check_fits(widest, cfg.media, cfg.style)
    except LayoutError as exc:
        raise ValidationError(
            f"asset {asset.id}: block {plan.first_number}..{plan.last_number} cannot be "
            f"laid out -- {exc}"
        ) from exc

    return plan


def reserve(client, asset: CableType, plan: Plan, cfg: AppConfig, now: datetime | None = None) -> Reservation:
    """Consume the block in Halo and verify it stuck.

    One write carries all three field updates -- the advanced counter, the
    cleared trigger, and the QUEUED status line -- so there is no window in
    which the counter has moved but the trigger is still set (a duplicate
    print next poll) or the trigger is cleared but the counter has not (a
    collision next request).

    Then the asset is re-read and the counter compared. That costs one extra
    API call and is worth it: the counter is invisible to the people who
    depend on it, and a silent corruption is discovered months later in a
    cable tray.

    Raises ReserveError if anything is uncertain. Callers treat that as "no
    numbers consumed" and retry next poll -- which is safe precisely because
    nothing has been rendered or enqueued yet.
    """
    now = now or datetime.now(timezone.utc)
    expected_counter = plan.next_counter_value

    # What the technician asked for, minus what this batch takes. Writing
    # the remainder back (rather than zero) is what makes a request larger
    # than one batch (guards.BATCH_SIZE) finish by itself: the next poll picks the
    # asset up again and takes the next batch.
    remaining = max(0, plan.requested_qty - plan.cable_count)

    status_line = format_status_line(
        QUEUED,
        plan.prefix,
        plan.first_number,
        plan.last_number,
        plan.cable_count,
        plan.label_count,
        requested_qty=plan.requested_qty,
        remaining=remaining,
        when=now,
        tz=cfg.display_timezone,
        separator=cfg.style.separator,
        pad_width=cfg.style.pad_width,
        max_chars=cfg.status_field_max_chars,
    )

    updates = [
        {"id": cfg.halo.nextid_field_id, "value": str(expected_counter)},
        {"id": cfg.halo.qty_field_id, "value": str(remaining)},
        {"id": cfg.halo.status_field_id, "value": status_line},
    ]

    try:
        client.update_fields(asset.id, updates)
    except Exception as exc:
        raise ReserveError(
            f"asset {asset.id}: reserve write failed ({exc}). No numbers consumed; "
            f"retrying next poll."
        ) from exc

    try:
        observed = client.read_counter(asset.id, cfg.halo.nextid_field_id)
    except Exception as exc:
        raise ReserveError(
            f"asset {asset.id}: reserve write succeeded but the read-back failed ({exc}). "
            f"NOT rendering, because the counter's state is unknown. Check "
            f"{cfg.halo.nextid_field_name} on asset {asset.id} before re-flagging."
        ) from exc

    if observed != expected_counter:
        raise ReserveError(
            f"asset {asset.id}: read-back mismatch -- expected "
            f"{cfg.halo.nextid_field_name} = {expected_counter}, found {observed}. "
            f"Refusing to render. Something else is writing this counter, or the write "
            f"did not apply. Numbers {plan.first_number}..{plan.last_number} may or may "
            f"not be consumed; verify before re-flagging."
        )

    identifiers = format_block(
        plan.prefix, plan.first_number, plan.last_number, cfg.style.separator, cfg.style.pad_width
    )
    logger.info(
        "reserved %s..%s for asset %s (%d cables, %d labels)%s",
        identifiers[0],
        identifiers[-1],
        asset.id,
        plan.cable_count,
        plan.label_count,
        " [CLAMPED from %d]" % plan.requested_qty if plan.clamped else "",
    )
    return Reservation(
        plan=plan,
        identifiers=identifiers,
        reserved_utc=now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def recovery_hint(reservation: Reservation, reason: str) -> str:
    """The log line for a block that was reserved but never printed.

    This message IS the user interface for the failure, so it carries the
    copy-pasteable recovery command rather than describing one.
    """
    plan = reservation.plan
    return (
        f"RESERVED BUT NOT ENQUEUED -- asset {plan.asset_id}, "
        f"{reservation.identifiers[0]}..{reservation.identifiers[-1]}: {reason}. "
        f"These numbers are consumed and will not be reissued (a gap, by design). "
        f"To print them, run: tools/reprint_range.py --prefix {plan.prefix} "
        f"--from {plan.first_number} --to {plan.last_number} --asset-id {plan.asset_id}"
    )
