"""The poll loop.

Reads spec 5.3 almost line for line. The ordering rules it enforces are the
difference between "a crash costs a few label numbers" and "a crash puts
two cables in a rack under the same identifier":

    validate  ->  reserve  ->  render  ->  enqueue

Everything that can fail cheaply fails before the reserve. Everything after
the reserve has already consumed numbers, so it logs the burned range with
the recovery command rather than pretending nothing happened.
"""
from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from protocol import QueueError, ensure_queue, sweep_stale_tmp

from . import allocator, asset_source, enqueue, guards, renderer
from .config import AppConfig, load_config
from .errors import LayoutError, ReserveError, StartupError, ValidationError
from .halo_client import HaloClient
from .reconcile import Reconciler
from .status import FAILED, format_status_line

logger = logging.getLogger("halo_cable_label")

HEARTBEAT_PATH = Path("/tmp/heartbeat")
_running = True


def beat() -> None:
    """Mark the loop alive.

    The healthcheck reads this file's age, so it must be touched on a
    schedule the WORK cannot stretch. Once per poll is not that: a poll of
    MAX_ASSETS_PER_POLL assets, each rendering a full batch, takes tens of
    seconds, and the container would report unhealthy for doing exactly what
    it was configured to do. Touching it per asset keeps the check measuring
    what it claims -- a loop that has stopped making progress -- while a
    genuinely stuck asset still goes stale and is still caught.
    """
    try:
        HEARTBEAT_PATH.touch()
    except OSError as exc:  # pragma: no cover - a full or read-only /tmp
        logger.debug("could not touch the heartbeat file: %s", exc)


def _handle_signal(signum, _frame):
    global _running
    logger.info("received signal %s, finishing this poll then stopping", signum)
    _running = False


class SingleInstanceLock:
    """An exclusive lock, held for the process's lifetime.

    The reserve is safe ONLY because there is exactly one writer. Two
    instances polling the same tenant can both read the same counter before
    either writes it, and then both issue the same block. No replicas
    without real distributed locking. See spec 4.4.
    """

    def __init__(self, path: str):
        self.path = Path(path)
        self._fd = None

    def acquire(self) -> None:
        import fcntl

        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._fd)
            self._fd = None
            raise StartupError(
                f"another instance already holds {self.path} ({exc}). Exactly one "
                f"instance may run against a tenant: two would issue overlapping cable "
                f"numbers. If you are certain no other instance is running, remove that "
                f"file and start again."
            ) from exc
        os.write(self._fd, f"{os.getpid()}\n".encode())

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def startup_banner(cfg: AppConfig) -> None:
    logger.info(
        "queue=%s poll=%ss batch=%d cables, %d assets/poll, %d labels/cable",
        cfg.queue.root,
        cfg.poll_interval_seconds,
        guards.BATCH_SIZE,
        cfg.max_assets_per_poll,
        cfg.labels_per_cable,
    )
    logger.info(
        "media: %.3fin x %.3fin, printed zone %.3fin x %.3fin at %d dpi, legend x%d",
        cfg.media.label_width_in,
        cfg.media.label_height_in,
        cfg.media.print_area_width_in,
        cfg.media.print_area_height_in,
        cfg.media.dpi,
        cfg.style.legend_repeat,
    )
    if cfg.shadow_mode:
        logger.warning(
            "=== SHADOW_MODE: no numbers will be reserved, nothing will be written to "
            "Halo, and nothing will be enqueued ==="
        )
    if not cfg.media_configured:
        logger.warning(
            "=== printers.yaml has configured: false -- the media geometry above is a "
            "PLACEHOLDER, not measured. This is allowed only because SHADOW_MODE is on. "
            "Measure your stock and set configured: true before printing anything. ==="
        )
    if cfg.dry_run:
        logger.warning(
            "=== DRY_RUN: numbers WILL be reserved and consumed, but nothing will be "
            "enqueued. Blocks reserved in this mode are gaps. ==="
        )


def process_asset(asset, client, cfg: AppConfig) -> None:
    """One cable type, start to finish. Never raises: a bad asset must not
    stop the rest of the batch."""
    try:
        plan = allocator.validate(asset, cfg)
    except ValidationError as exc:
        logger.error("%s -- leaving it flagged for a human", exc)
        return

    if cfg.shadow_mode:
        logger.info(
            "SHADOW: would reserve %s %d..%d (%d cables, %d labels) on asset %s",
            plan.prefix,
            plan.first_number,
            plan.last_number,
            plan.cable_count,
            plan.label_count,
            asset.id,
        )
        return

    try:
        reservation = allocator.reserve(client, asset, plan, cfg)
    except ReserveError as exc:
        logger.error("%s", exc)
        return

    # Past this point the numbers are gone. Every failure below is a gap,
    # and every log line has to carry enough to recover from it.
    try:
        payload = renderer.render(reservation, cfg.media, cfg.style)
    except LayoutError as exc:
        logger.error("%s", allocator.recovery_hint(reservation, f"render failed: {exc}"))
        return

    if cfg.dry_run:
        logger.info(
            "DRY_RUN: rendered %d bytes for %s..%s, not enqueueing",
            len(payload),
            reservation.identifiers[0],
            reservation.identifiers[-1],
        )
        return

    try:
        enqueue.write(reservation, payload, cfg)
    except QueueError as exc:
        logger.error("%s", allocator.recovery_hint(reservation, str(exc)))
        _report_enqueue_failure(client, reservation, cfg, exc)


def _report_enqueue_failure(client, reservation, cfg: AppConfig, exc: Exception) -> None:
    """Tell Halo when the queue could not be written.

    This is the one failure that leaves no trace anywhere a technician
    looks. The numbers are consumed and the asset already reads QUEUED from
    the reserve, but no job file was ever written -- so the stall watchdog,
    which looks for jobs waiting in the queue, cannot see it either. Without
    this the run would read "printing shortly" forever.

    The share being unreachable does not mean Halo is, so the report goes
    out over the connection that is still working. If that one is down too,
    the log line from recovery_hint is all that is left, and it carries the
    reprint command.
    """
    plan = reservation.plan
    if not plan.asset_id:
        return
    line = format_status_line(
        FAILED,
        plan.prefix, plan.first_number, plan.last_number,
        plan.cable_count, plan.label_count,
        reason=f"could not write to the print queue ({exc}); numbers consumed, needs a reprint",
        tz=cfg.display_timezone,
        separator=cfg.style.separator,
        pad_width=cfg.style.pad_width,
        max_chars=cfg.status_field_max_chars,
    )
    try:
        client.write_status(plan.asset_id, cfg.halo.status_field_id, line)
    except Exception as write_exc:
        logger.warning(
            "could not write the FAILED line for asset %s: %s", plan.asset_id, write_exc
        )


def poll_once(client, cfg: AppConfig, reconciler: Reconciler) -> None:
    # Drain results first, so outcomes reach the asset even when the Halo
    # poll below fails.
    try:
        reconciler.run(client)
        reconciler.check_for_stalls(client)
    except Exception:
        logger.exception("reconcile pass failed; continuing to the poll")

    try:
        pending = asset_source.get_pending(client, cfg)
    except Exception as exc:
        logger.warning("could not read pending cable types from Halo: %s", exc)
        return

    if not pending:
        return

    pending = guards.veto_duplicate_prefixes(pending)
    pending = guards.apply_batch_cap(pending, cfg.max_assets_per_poll)

    for asset in pending:
        process_asset(asset, client, cfg)
        beat()


def main(argv=None) -> int:
    load_dotenv()
    try:
        cfg = load_config()
    except StartupError as exc:
        print(exc, file=sys.stderr)
        return 2

    configure_logging(cfg.log_level)

    lock = SingleInstanceLock(cfg.lock_path)
    try:
        lock.acquire()
        ensure_queue(cfg.queue.root)
    except (StartupError, QueueError) as exc:
        logger.error("%s", exc)
        return 2

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    startup_banner(cfg)
    sweep_stale_tmp(cfg.queue.root)

    reconciler = Reconciler(cfg)
    # Surface a backlog of failures at startup rather than waiting for the
    # first reminder interval -- a restart is exactly when someone is looking.
    reconciler._remind_about_failures()

    try:
        with HaloClient(
            base_url=cfg.halo.base_url,
            auth_url=cfg.halo.auth_url,
            client_id=cfg.halo.client_id,
            client_secret=cfg.halo.client_secret,
            timeout=cfg.halo.timeout_seconds,
        ) as client:
            while _running:
                beat()

                try:
                    poll_once(client, cfg, reconciler)
                except Exception:
                    # A poll must never kill the loop. The container exits
                    # only on startup misconfiguration or a lost lock.
                    logger.exception("unhandled error in poll; continuing")

                for _ in range(cfg.poll_interval_seconds):
                    if not _running:
                        break
                    time.sleep(1)
    finally:
        lock.release()

    logger.info("stopped cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
