"""Drain the agent's results and write the outcome back to Halo.

This closes the only feedback loop the system has. The print happens on
another machine behind a driver, so without this the person who pressed the
button learns nothing and a failed run is invisible until someone reads a
log. See spec 5.5 and 3.3.

Two rules shape everything here:

  * **Log first, then write.** The log is the durable record; the Halo field
    is a convenience. If the status write fails, the outcome is still
    recorded somewhere permanent.

  * **Never auto-re-enqueue a failure.** A failed job has already consumed
    identifiers, and the agent cannot know how many labels came out before
    it failed. Re-enqueueing risks a duplicate physical label. Recovery is a
    human running tools/reprint_range.py with the range, which the FAILED
    status line carries verbatim.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

from protocol import QueueError, read_json, validate_result

from .config import AppConfig
from .status import FAILED, RESENT, SENT, STALLED, format_status_line, resolve_timezone

logger = logging.getLogger(__name__)


class Reconciler:
    """Holds the per-result retry counters across polls.

    In memory on purpose. A status write that never succeeds is already
    recorded in the log, so persisting the counter would add durability to
    the least important half of the outcome.
    """

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self._attempts: dict[str, int] = {}
        self._last_failed_reminder: float = 0.0
        # The watchdog measures silence, not age: a job waiting behind six
        # others is not stalled, it is queued. Progress is any result coming
        # back, so a healthy backlog keeps resetting this.
        self._last_progress: float = time.time()
        self._stall_reported: set[str] = set()

    # --- helpers -------------------------------------------------------------

    def _job_for(self, result: dict) -> dict | None:
        """The job this result refers to.

        The agent echoes the job sidecar into the result, which is the
        normal path. The fallback searches done/ and failed/, where the
        original sidecar will have been moved to, so a result from an older
        agent that does not echo still produces a status line.
        """
        echoed = result.get("job")
        if isinstance(echoed, dict) and echoed.get("job_id") == result["job_id"]:
            return echoed
        for folder in ("done", "failed", "processing"):
            for path in (self.cfg.queue.root / folder).glob("*.json"):
                try:
                    candidate = read_json(path)
                except (QueueError, json.JSONDecodeError):
                    continue
                if candidate.get("job_id") == result["job_id"]:
                    return candidate
        return None

    def _status_word(self, result: dict, job: dict) -> str:
        if result["status"] != "success":
            return FAILED
        return RESENT if job.get("reprint") else SENT

    def _describe(self, result: dict, job: dict | None) -> str:
        if not job:
            return f"job {result['job_id'][:8]}"
        return (
            f"{job['prefix']} {job['first_number']}..{job['last_number']} "
            f"({job['cable_count']} cables, {job['label_count']} labels)"
        )

    # --- the pass ------------------------------------------------------------

    def run(self, client) -> int:
        """Process every pending result. Returns how many were handled."""
        results_dir = self.cfg.queue.root / "results"
        if not results_dir.is_dir():
            return 0

        handled = 0
        for path in sorted(results_dir.glob("*.json"), key=lambda p: p.name):
            try:
                handled += 1 if self._handle_one(client, path) else 0
            except Exception:  # never let one bad result stop the rest
                logger.exception("unhandled error processing result %s", path.name)
        if handled:
            self._last_progress = time.time()
            self._stall_reported.clear()
        self._remind_about_failures()
        return handled

    def _handle_one(self, client, path: Path) -> bool:
        try:
            result = read_json(path)
            validate_result(result)
        except (QueueError, json.JSONDecodeError, jsonschema.ValidationError) as exc:
            logger.error(
                "result file %s is unreadable or invalid (%s) -- deleting it. The job's "
                "real outcome is whichever of done/ or failed/ it is sitting in.",
                path.name,
                exc,
            )
            path.unlink(missing_ok=True)
            return False

        job = self._job_for(result)
        described = self._describe(result, job)

        # Log FIRST. Everything after this is best-effort convenience.
        if result["status"] == "success":
            logger.info("printed %s on %s", described, result.get("agent_host", "?"))
        elif result["status"] == "rejected":
            logger.error(
                "agent REJECTED %s: %s. Nothing printed. These numbers are consumed; "
                "reprint them if the cables still need labels.",
                described,
                result.get("error") or "no reason given",
            )
        else:
            logger.error(
                "print FAILED for %s: %s. These numbers are consumed and will not be "
                "reissued. To reprint: tools/reprint_range.py --prefix %s --from %s "
                "--to %s%s",
                described,
                result.get("error") or "no reason given",
                job.get("prefix") if job else "?",
                job.get("first_number") if job else "?",
                job.get("last_number") if job else "?",
                f" --asset-id {job['halo_asset_id']}" if job and job.get("halo_asset_id") else "",
            )

        if not self._write_status(client, result, job):
            return False

        path.unlink(missing_ok=True)
        self._attempts.pop(result["job_id"], None)
        return True

    def _write_status(self, client, result: dict, job: dict | None) -> bool:
        """Returns True when the result file may be deleted."""
        job_id = result["job_id"]

        if not job:
            logger.warning(
                "no job sidecar found for result %s -- outcome logged, no status line "
                "written",
                job_id[:8],
            )
            return True

        asset_id = job.get("halo_asset_id")
        if not asset_id:
            # A reprint run without --asset-id, or a test job. Intentional.
            logger.debug("result %s has no asset id; nothing to write back", job_id[:8])
            return True

        line = format_status_line(
            self._status_word(result, job),
            job["prefix"],
            job["first_number"],
            job["last_number"],
            job["cable_count"],
            job["label_count"],
            reason=result.get("error"),
            tz=self.cfg.display_timezone,
            separator=self.cfg.style.separator,
            pad_width=self.cfg.style.pad_width,
            max_chars=self.cfg.status_field_max_chars,
        )

        try:
            client.write_status(asset_id, self.cfg.halo.status_field_id, line)
        except Exception as exc:
            attempts = self._attempts.get(job_id, 0) + 1
            self._attempts[job_id] = attempts
            if attempts >= self.cfg.status_write_max_attempts:
                logger.error(
                    "giving up writing the status line for %s to asset %s after %d "
                    "attempts (%s). The outcome is in the log above. Dropping the result "
                    "file so one unwritable asset cannot wedge the queue behind it.",
                    job_id[:8],
                    asset_id,
                    attempts,
                    exc,
                )
                self._attempts.pop(job_id, None)
                return True
            logger.warning(
                "status write for %s failed (attempt %d/%d): %s -- retrying next poll",
                job_id[:8],
                attempts,
                self.cfg.status_write_max_attempts,
                exc,
            )
            return False

        logger.debug("wrote status for %s to asset %s: %s", job_id[:8], asset_id, line)
        return True

    def check_for_stalls(self, client) -> int:
        """Say so in Halo when jobs are queued and nothing is moving.

        Every other outcome writes a status line. A print host that has gone
        away writes nothing at all: the job sits in inbox/, the asset still
        reads QUEUED, and the person who pressed the button has no way to
        tell that from "printing shortly". That silence is the failure mode
        an asynchronous pipeline has and a synchronous one does not, so it
        gets turned into a status word.

        Stalled means nothing has completed for stalled_after_minutes while
        work is outstanding -- not that a particular job is old, because a
        job waiting its turn behind a long run is perfectly healthy.
        """
        outstanding = self._outstanding_jobs()
        if not outstanding:
            self._last_progress = time.time()
            self._stall_reported.clear()
            return 0

        quiet_for = time.time() - self._last_progress
        if quiet_for < self.cfg.stalled_after_minutes * 60:
            return 0

        since = datetime.fromtimestamp(self._last_progress, tz=timezone.utc)
        reported = 0
        for job in outstanding:
            job_id = job.get("job_id", "")
            if job_id in self._stall_reported:
                continue
            asset_id = job.get("halo_asset_id")
            described = self._describe(None, job)
            logger.error(
                "STALLED: %s has been waiting %.0f minutes with no response from the "
                "print host. Check the agent service and the share.",
                described, quiet_for / 60,
            )
            self._stall_reported.add(job_id)
            reported += 1
            if not asset_id:
                continue
            line = format_status_line(
                STALLED,
                job["prefix"], job["first_number"], job["last_number"],
                job["cable_count"], job["label_count"],
                reason=(
                    "no response from the print host since "
                    + since.astimezone(resolve_timezone(self.cfg.display_timezone)).strftime("%H:%M")
                ),
                tz=self.cfg.display_timezone,
                separator=self.cfg.style.separator,
                pad_width=self.cfg.style.pad_width,
                max_chars=self.cfg.status_field_max_chars,
            )
            try:
                client.write_status(asset_id, self.cfg.halo.status_field_id, line)
            except Exception as exc:
                logger.warning("could not write the STALLED line for %s: %s", described, exc)
        return reported

    def _outstanding_jobs(self) -> list[dict]:
        """Every job sitting in inbox/ or processing/, oldest first."""
        jobs = []
        for folder in ("inbox", "processing"):
            directory = self.cfg.queue.root / folder
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    jobs.append(read_json(path))
                except (QueueError, json.JSONDecodeError):
                    continue
        return jobs

    def _remind_about_failures(self) -> None:
        """Keep a non-empty failed/ folder visible.

        A failure that nobody notices is the whole risk of an asynchronous
        print path, and the folder is the one signal that persists after the
        log has rolled.
        """
        failed_dir = self.cfg.queue.root / "failed"
        if not failed_dir.is_dir():
            return
        payloads = [p for p in failed_dir.iterdir() if p.suffix != ".json"]
        if not payloads:
            return
        now = time.time()
        if now - self._last_failed_reminder < self.cfg.queue.failed_reminder_minutes * 60:
            return
        self._last_failed_reminder = now
        oldest = min(payloads, key=lambda p: p.stat().st_mtime)
        age_hours = (now - oldest.stat().st_mtime) / 3600
        logger.warning(
            "%d job(s) are sitting in %s, oldest %.1f hours old (%s). These consumed "
            "cable numbers and did not print.",
            len(payloads),
            failed_dir,
            age_hours,
            oldest.name,
        )
