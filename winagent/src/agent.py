"""The Windows agent: watch inbox/, print, report.

Runs on the machine with the Brady driver installed. It never talks to
Halo and never holds Halo credentials -- it knows only the queue protocol.

The two rules that carry the weight (spec 7.2):

  * **Record the job id before printing, not after.** A crash mid-print then
    looks like a replay on restart and is refused. Losing a job is
    recoverable; duplicating one puts two cables under the same identifier.

  * **Orphans in processing/ are never auto-retried.** The agent cannot know
    how many labels already came out of the applicator. A human reads the
    result and decides.

Watching is done on a LOCAL folder. That is why the share is hosted by this
machine: Windows change notifications drop events over SMB under load and
after reconnects, which is not a bug class you want in a print queue.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path

import jsonschema
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from protocol import (  # noqa: E402
    PROTOCOL_VERSION,
    QueueError,
    ensure_queue,
    iter_inbox_jobs,
    move_job,
    parse_stem,
    read_json,
    sha256_file,
    sweep_stale_tmp,
    utc_now_iso,
    validate_job,
    write_result,
)
from winagent.src.backends import PrintError, build_backend, list_printers  # noqa: E402
from winagent.src.ledger import Ledger  # noqa: E402

logger = logging.getLogger("cable_label_agent")

DEFAULTS = {
    "queue_root": None,
    "printer_name": None,
    "backend": "null",
    "sidecar_grace_seconds": 10,
    "print_timeout_seconds": 120,
    "retain_done_days": 30,
    "poll_seconds": 5,
    # Seconds to leave between handing one job to the printer and the next.
    # The Wraptor takes a while to ingest a large job and silently drops
    # ones that arrive while it is busy: four 500-page jobs sent within a
    # minute left only the first on the printer, while the same jobs spaced
    # out all arrived. Measured on a Wraptor A6200, 2026-09-21.
    "job_interval_seconds": 60,
    "ledger_path": None,
    "log_path": None,
    "log_level": "INFO",
    "skip_printer_check": False,
}


class AgentConfigError(RuntimeError):
    pass


def load_agent_config(path: Path) -> dict:
    if not path.exists():
        raise AgentConfigError(
            f"{path} not found -- copy agent.example.yaml to agent.yaml and edit it"
        )
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise AgentConfigError(f"malformed YAML in {path}: {exc}") from exc

    cfg = {**DEFAULTS, **raw}
    errors = []
    if not cfg["queue_root"]:
        errors.append("queue_root is required -- the LOCAL path to the queue, not a UNC path")
    if not cfg["printer_name"]:
        errors.append("printer_name is required -- the exact Windows printer name")
    if cfg["backend"] not in ("null", "gdi", "brady"):
        errors.append(f"backend {cfg['backend']!r} is not one of: null, gdi, brady")
    if errors:
        raise AgentConfigError("Invalid agent configuration:\n  - " + "\n  - ".join(errors))

    root = Path(cfg["queue_root"])
    cfg["queue_root"] = root
    cfg["ledger_path"] = Path(cfg["ledger_path"] or root / ".agent" / "ledger.sqlite")
    cfg["log_path"] = Path(cfg["log_path"]) if cfg["log_path"] else root / ".agent" / "agent.log"
    return cfg


def configure_logging(cfg: dict) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        cfg["log_path"].parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(cfg["log_path"], encoding="utf-8"))
    except OSError as exc:
        print(f"warning: file logging unavailable ({exc}); logging to console only")
    logging.basicConfig(
        level=getattr(logging, str(cfg["log_level"]).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(message)s",
        handlers=handlers,
    )


class Agent:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.root = cfg["queue_root"]
        self.host = socket.gethostname()
        self.backend = build_backend(
            cfg["backend"],
            cfg["printer_name"],
            timeout_seconds=int(cfg["print_timeout_seconds"]),
        )
        self.ledger = Ledger(cfg["ledger_path"])
        # The folder watcher and the periodic sweep both call sweep_inbox,
        # from different threads. One at a time: the ledger would refuse a
        # double claim anyway, but a job half-moved between folders by two
        # threads is not worth reasoning about.
        self._sweeping = threading.Lock()
        self._last_print_finished = 0.0

    # --- startup -------------------------------------------------------------

    def preflight(self) -> None:
        ensure_queue(self.root)
        self.backend.preflight()

        # The null backend prints nothing, so printer visibility is
        # irrelevant to it. Skipping the check here is what lets the whole
        # pipeline be proven on a machine where the driver is not installed
        # yet -- which is the normal case, since the Brady driver cannot be
        # installed without the printer on the network.
        if self.backend.name == "null":
            logger.info(
                "backend is 'null': skipping the printer visibility check. Nothing will "
                "print. Switch backends once the driver is installed."
            )
        elif self.cfg.get("skip_printer_check"):
            logger.warning(
                "skip_printer_check is set -- not verifying that %r is visible. Remove "
                "it once the driver is installed.",
                self.cfg["printer_name"],
            )
        else:
            printers = list_printers()
            if printers and self.cfg["printer_name"] not in printers:
                raise AgentConfigError(
                    f"printer {self.cfg['printer_name']!r} is not visible to this account.\n"
                    f"Printers this process CAN see: {', '.join(printers) or '(none)'}\n"
                    f"Printer drivers are per-user: a driver installed under an interactive "
                    f"admin session is often invisible to a service account. Install or "
                    f"connect the printer as the account this service runs under.\n"
                    f"If the driver is not installed yet, run with backend: null instead -- "
                    f"the whole pipeline can be verified without it."
                )
            if not printers:
                logger.warning(
                    "cannot enumerate printers (pywin32 missing, or not running on Windows) "
                    "-- skipping the printer visibility check"
                )

        sweep_stale_tmp(self.root)
        self.recover_orphans()
        pruned = self.ledger.prune(int(self.cfg["retain_done_days"]))
        if pruned:
            logger.info("pruned %d old ledger entries", pruned)
        self.prune_done()

    def recover_orphans(self) -> None:
        """Anything left in processing/ died mid-print on a previous run.

        Moved to failed/ with reason 'interrupted' and NEVER reprinted: some
        labels may already have come out of the applicator, and the agent
        has no way to know how many.
        """
        for job_id, stem in self.ledger.claimed_but_unfinished():
            logger.error(
                "job %s (%s) was claimed but never finished -- a previous run died "
                "mid-print. Moving it to failed/. It will NOT be reprinted: some labels "
                "may already have been applied. Check the printer, then use "
                "tools/reprint_range.py if the cables still need labels.",
                job_id[:8],
                stem,
            )
            try:
                move_job(self.root, stem, "processing", "failed")
            except QueueError as exc:
                logger.error("could not move orphan %s to failed/: %s", stem, exc)
            self.ledger.mark(job_id, "failed")
            self.emit_result(job_id, "failed", error="interrupted: the agent stopped mid-print")

        # Belt and braces: anything physically in processing/ that the ledger
        # does not know about (a hand-moved file, a ledger that was deleted).
        processing = self.root / "processing"
        stems = {p.stem for p in processing.iterdir()} if processing.is_dir() else set()
        for stem in sorted(stems):
            logger.error("untracked file left in processing/ (%s) -- moving to failed/", stem)
            move_job(self.root, stem, "processing", "failed")

    def prune_done(self) -> None:
        cutoff = time.time() - int(self.cfg["retain_done_days"]) * 86400
        removed = 0
        done = self.root / "done"
        if not done.is_dir():
            return
        for path in done.iterdir():
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        if removed:
            logger.info("pruned %d file(s) from done/", removed)

    # --- results -------------------------------------------------------------

    def emit_result(self, job_id, status, *, error=None, job=None, started=None, submitted=None) -> None:
        result = {
            "protocol_version": PROTOCOL_VERSION,
            "job_id": job_id,
            "status": status,
            "agent_host": self.host,
            "started_utc": started,
            "finished_utc": utc_now_iso(),
            "labels_submitted": submitted,
            "backend": self.backend.name,
            "error": error,
            # Echo the job back so the Docker half can build a status line
            # without hunting for a sidecar that has already been moved.
            "job": job,
        }
        try:
            write_result(self.root, result)
        except (QueueError, jsonschema.ValidationError) as exc:
            logger.error("could not write the result for %s: %s", job_id[:8], exc)

    def reject(self, stem: str, job_id: str | None, reason: str, job=None) -> None:
        logger.error("REJECTED %s: %s", stem, reason)
        try:
            move_job(self.root, stem, "inbox", "failed")
        except QueueError as exc:
            logger.error("could not move %s to failed/: %s", stem, exc)
        if job_id:
            self.emit_result(job_id, "rejected", error=reason, job=job)

    # --- the main path -------------------------------------------------------

    def handle(self, payload_path: Path, sidecar_path: Path) -> None:
        stem = payload_path.stem

        if not sidecar_path.exists():
            # The Docker half renames the sidecar in first, so this should be
            # impossible. Wait anyway rather than rejecting on a filesystem hiccup.
            deadline = time.time() + int(self.cfg["sidecar_grace_seconds"])
            while time.time() < deadline and not sidecar_path.exists():
                time.sleep(0.5)
        if not sidecar_path.exists():
            self.reject(stem, None, f"no sidecar appeared for {payload_path.name}")
            return

        try:
            job = read_json(sidecar_path)
        except (QueueError, json.JSONDecodeError) as exc:
            self.reject(stem, None, f"unreadable sidecar: {exc}")
            return

        job_id = job.get("job_id")
        if job.get("protocol_version") != PROTOCOL_VERSION:
            self.reject(
                stem,
                job_id,
                f"protocol_version {job.get('protocol_version')!r} is not {PROTOCOL_VERSION} "
                f"-- this agent will not guess at a format it does not know. Upgrade the agent.",
            )
            return

        try:
            validate_job(job)
        except jsonschema.ValidationError as exc:
            self.reject(stem, job_id, f"sidecar failed validation: {exc.message}", job=job)
            return

        if self.ledger.seen(job_id):
            self.reject(
                stem,
                job_id,
                f"replay: job {job_id[:8]} has been processed before "
                f"(status {self.ledger.seen(job_id)}). Refusing to print it twice.",
                job=job,
            )
            return

        actual = sha256_file(payload_path)
        if actual != job["payload_sha256"]:
            self.reject(
                stem,
                job_id,
                f"checksum mismatch: the payload is not the file the sidecar describes "
                f"(expected {job['payload_sha256'][:12]}, got {actual[:12]}). The file may "
                f"be truncated or still replicating.",
                job=job,
            )
            return

        # Claim BEFORE printing. See the module docstring.
        if not self.ledger.claim(job_id, stem):
            self.reject(stem, job_id, "replay: lost a race to claim this job", job=job)
            return

        started = utc_now_iso()
        try:
            move_job(self.root, stem, "inbox", "processing")
        except QueueError as exc:
            self.ledger.mark(job_id, "failed")
            self.emit_result(job_id, "failed", error=f"could not move into processing/: {exc}", job=job)
            return

        moved = self.root / "processing" / payload_path.name
        self._wait_for_the_printer()
        try:
            self.backend.print(moved, job)
        except PrintError as exc:
            logger.error("print failed for %s: %s", stem, exc)
            self.ledger.mark(job_id, "failed")
            move_job(self.root, stem, "processing", "failed")
            self.emit_result(job_id, "failed", error=str(exc), job=job, started=started)
            return
        except Exception as exc:  # a backend bug must still produce a result
            logger.exception("unexpected error printing %s", stem)
            self.ledger.mark(job_id, "failed")
            move_job(self.root, stem, "processing", "failed")
            self.emit_result(job_id, "failed", error=f"unexpected: {exc}", job=job, started=started)
            return

        self._last_print_finished = time.monotonic()
        self.ledger.mark(job_id, "done")
        move_job(self.root, stem, "processing", "done")
        self.emit_result(
            job_id, "success", job=job, started=started, submitted=job["label_count"]
        )
        logger.info(
            "printed %s %s..%s (%d labels)",
            job["prefix"],
            job["first_number"],
            job["last_number"],
            job["label_count"],
        )

    def _wait_for_the_printer(self) -> None:
        """Leave a gap between jobs, so one does not arrive while the
        printer is still taking the last.

        The printer accepts a job, stores it, and offers it in a menu for
        someone to select. Handing it another while it is still ingesting a
        big one gets that job dropped without an error anywhere: Windows
        reports it printed, and it simply never appears. See
        job_interval_seconds in the config.
        """
        interval = int(self.cfg["job_interval_seconds"])
        if interval <= 0 or not self._last_print_finished:
            return
        waited = time.monotonic() - self._last_print_finished
        remaining = interval - waited
        if remaining > 0:
            logger.info("waiting %.0fs before the next job, so the printer keeps up", remaining)
            time.sleep(remaining)

    def sweep_inbox(self) -> int:
        with self._sweeping:
            return self._sweep_inbox()

    def _sweep_inbox(self) -> int:
        handled = 0
        for payload_path, sidecar_path in iter_inbox_jobs(self.root):
            try:
                parse_stem(payload_path.stem)
            except ValueError:
                self.reject(payload_path.stem, None, "filename is not a valid job stem")
                continue
            try:
                self.handle(payload_path, sidecar_path)
                handled += 1
            except Exception:
                logger.exception("unhandled error on %s", payload_path.name)
        return handled

    def run_forever(self) -> None:
        """Watch inbox/ and print what lands in it.

        Uses watchdog when available and a polling sweep otherwise. The
        sweep is not a lesser fallback: it is also what catches jobs that
        arrived while the agent was stopped, which a pure event watcher
        misses entirely.
        """
        self.sweep_inbox()
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            logger.warning("watchdog is not installed -- falling back to polling")
            self._poll_forever()
            return

        agent = self

        class Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    agent.sweep_inbox()

            def on_moved(self, event):
                if not event.is_directory:
                    agent.sweep_inbox()

        observer = Observer()
        observer.schedule(Handler(), str(self.root / "inbox"), recursive=False)
        observer.start()
        logger.info("watching %s", self.root / "inbox")
        try:
            while True:
                time.sleep(int(self.cfg["poll_seconds"]))
                # A periodic sweep alongside the watcher, because dropped
                # events are the known failure mode of filesystem watchers.
                self.sweep_inbox()
        except KeyboardInterrupt:
            logger.info("stopping")
        finally:
            observer.stop()
            observer.join()
            self.ledger.close()

    def _poll_forever(self) -> None:
        try:
            while True:
                self.sweep_inbox()
                time.sleep(int(self.cfg["poll_seconds"]))
        except KeyboardInterrupt:
            logger.info("stopping")
        finally:
            self.ledger.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Halo cable label print agent")
    parser.add_argument(
        "--config", default=str(Path(__file__).resolve().parents[1] / "config" / "agent.yaml")
    )
    parser.add_argument("--once", action="store_true", help="process the inbox once and exit")
    args = parser.parse_args(argv)

    try:
        cfg = load_agent_config(Path(args.config))
    except AgentConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    configure_logging(cfg)
    logger.info("backend=%s printer=%r queue=%s", cfg["backend"], cfg["printer_name"], cfg["queue_root"])

    try:
        agent = Agent(cfg)
        agent.preflight()
    except (AgentConfigError, PrintError, QueueError) as exc:
        logger.error("%s", exc)
        return 2

    if args.once:
        handled = agent.sweep_inbox()
        agent.ledger.close()
        logger.info("processed %d job(s)", handled)
        return 0

    agent.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
