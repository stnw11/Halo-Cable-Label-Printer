"""Write a rendered job onto the queue share.

Thin by design: all the atomicity lives in protocol/queue.py, which both
halves share. This module's job is to build a valid sidecar from a
Reservation and to retry a share that is briefly unavailable.

The retry matters. The one network hop on this side is an SMB write, and a
share that is mid-reconnect fails in a way that succeeds a few seconds
later. Every retry exhausted means a block of numbers is burned, so it is
worth a few attempts before giving up. See spec 6.2.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from protocol import QueueError, format_stem, sha256_bytes, utc_now_iso, write_job

from .config import AppConfig
from .models import Reservation
from .naming import format_identifier, format_job_name

# The renderer draws labels as an image; the agent prints it through the
# printer's own driver. There is no other format.
RENDER_FORMAT = "png"

logger = logging.getLogger(__name__)


def build_sidecar(
    reservation: Reservation,
    payload: bytes,
    cfg: AppConfig,
    *,
    job_id: str | None = None,
    created: datetime | None = None,
    source: str = "poll",
    asset_id: int | None = -1,
) -> dict:
    """Assemble the job sidecar.

    `asset_id` defaults to the reservation's own asset. Pass None explicitly
    for a job that must NOT write a status line back to Halo -- a reprint
    run without --asset-id, or a test job.
    """
    plan = reservation.plan
    created = created or datetime.now(timezone.utc)
    job_id = job_id or str(uuid.uuid4())

    first_label = format_identifier(plan.prefix, plan.first_number, cfg.style.separator, cfg.style.pad_width)
    last_label = format_identifier(plan.prefix, plan.last_number, cfg.style.separator, cfg.style.pad_width)
    # The stem carries the padded NUMBERS only; the prefix is already its own
    # component, and repeating it would make the filename read BL_BL-0100.
    stem = format_stem(
        created,
        plan.prefix,
        first_label.split(cfg.style.separator)[-1],
        last_label.split(cfg.style.separator)[-1],
        job_id,
    )

    return {
        "protocol_version": 1,
        "job_id": job_id,
        "created_utc": utc_now_iso(created),
        "source": source,
        "reprint": source == "reprint",
        "halo_asset_id": plan.asset_id if asset_id == -1 else asset_id,
        "job_name": format_job_name(
            plan.color, plan.type_name, plan.prefix, plan.first_number, plan.last_number,
            cfg.style.separator, cfg.style.pad_width,
        ),
        "prefix": plan.prefix,
        "first_number": plan.first_number,
        "last_number": plan.last_number,
        "cable_count": plan.cable_count,
        "labels_per_cable": plan.labels_per_cable,
        "label_count": plan.label_count,
        "payload_file": f"{stem}.{RENDER_FORMAT}",
        "payload_sha256": sha256_bytes(payload),
        "payload_bytes": len(payload),
        "render_format": RENDER_FORMAT,
        "dpi": cfg.media.dpi,
        "page_width_in": round(cfg.media.label_width_in, 4),
        "page_height_in": round(cfg.media.label_height_in, 4),
    }


def write(reservation: Reservation, payload: bytes, cfg: AppConfig, **sidecar_kwargs) -> dict:
    """Enqueue one job, retrying a transient share failure.

    Raises QueueError once the retries are exhausted. The caller logs that
    with the recovery command, because by then the numbers are gone.
    """
    sidecar = build_sidecar(reservation, payload, cfg, **sidecar_kwargs)
    attempts = max(1, cfg.queue.write_retries)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            written = write_job(cfg.queue.root, sidecar, payload)
            logger.info(
                "enqueued %s (%d labels) as %s",
                sidecar["job_id"][:8],
                sidecar["label_count"],
                written["payload"].name,
            )
            return written
        except QueueError as exc:
            last_error = exc
            if attempt < attempts:
                logger.warning(
                    "queue write attempt %d/%d failed (%s); retrying in %.1fs",
                    attempt,
                    attempts,
                    exc,
                    cfg.queue.write_retry_seconds,
                )
                time.sleep(cfg.queue.write_retry_seconds)

    raise QueueError(
        f"gave up writing job {sidecar['job_id']} after {attempts} attempt(s): {last_error}"
    )
