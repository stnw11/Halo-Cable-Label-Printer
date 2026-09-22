"""Queue layout, job naming, and atomic file handoff.

This module is imported by BOTH halves -- the Docker service that writes
jobs and the Windows agent that prints them. It is deliberately dependency
-light (stdlib plus jsonschema) so the Windows side does not inherit the
Docker side's rendering or HTTP dependencies.

The atomicity rules here are the whole reason this module exists. A folder
queue is only safe if a consumer can never observe a half-written job, and
that is not something each half should re-derive. See spec section 6.2.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1

# The agent watches inbox/ for these extensions ONLY. The sidecar shares the
# stem but ends in .json, and is always renamed into place first, so by the
# time a payload appears its sidecar is guaranteed complete (6.2).
PAYLOAD_EXTENSIONS = ("png",)

SUBDIRS = ("inbox", "processing", "done", "failed", "results", ".tmp")

SCHEMA_PATH = Path(__file__).resolve().parent / "job_schema.json"

# ts_prefix_first-last_jobid8. The prefix is validated elsewhere against
# PREFIX_PATTERN (alphanumeric only), which is what makes a 4-way split on
# "_" unambiguous.
_STEM_RE = re.compile(
    r"^(?P<ts>\d{8}T\d{6}Z)_(?P<prefix>[A-Za-z0-9]+)_(?P<first>\d+)-(?P<last>\d+)_(?P<job_id_short>[0-9a-f]{8})$"
)

_STALE_TMP_SECONDS = 3600


class QueueError(RuntimeError):
    """A job could not be written to, or read from, the queue.

    Raised for filesystem-level failures (the share is gone, a rename
    failed, a file is unreadable). Schema failures raise
    jsonschema.ValidationError instead, so callers can tell "the share
    broke" apart from "this job is malformed" -- the first is retryable,
    the second never is.
    """


_schema_cache: dict | None = None


def _schema() -> dict:
    global _schema_cache
    if _schema_cache is None:
        with open(SCHEMA_PATH, encoding="utf-8") as f:
            _schema_cache = json.load(f)
    return _schema_cache


def _validate_against(payload: dict, defn: str) -> dict:
    """Validate against one named $defs entry.

    The schema's top level is a oneOf over job and result, which is useful
    for "is this a valid protocol file at all" but produces useless error
    messages -- a malformed job reports that it failed to match *both*
    branches. Validating against the branch the caller already knows it
    wants gives an error that names the actual offending field.
    """
    schema = _schema()
    sub = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": f"#/$defs/{defn}"}
    jsonschema.validate(instance=payload, schema=sub)
    return payload


def validate_job(sidecar: dict) -> dict:
    """Validate a job sidecar. Raises jsonschema.ValidationError."""
    _validate_against(sidecar, "job")
    # Cross-field invariants the schema cannot express on its own.
    errors = []
    if sidecar["last_number"] < sidecar["first_number"]:
        errors.append(
            f"last_number {sidecar['last_number']} is below first_number {sidecar['first_number']}"
        )
    span = sidecar["last_number"] - sidecar["first_number"] + 1
    if span != sidecar["cable_count"]:
        errors.append(
            f"cable_count {sidecar['cable_count']} does not match the range "
            f"{sidecar['first_number']}..{sidecar['last_number']} (span {span})"
        )
    expected_labels = sidecar["cable_count"] * sidecar["labels_per_cable"]
    if expected_labels != sidecar["label_count"]:
        errors.append(
            f"label_count {sidecar['label_count']} != cable_count * labels_per_cable "
            f"({expected_labels})"
        )
    if errors:
        raise jsonschema.ValidationError("; ".join(errors))
    return sidecar


def validate_result(result: dict) -> dict:
    """Validate a result sidecar. Raises jsonschema.ValidationError."""
    return _validate_against(result, "result")


def utc_now_iso(when: datetime | None = None) -> str:
    """The protocol's timestamp format: UTC, second precision, Z-suffixed."""
    when = when or datetime.now(timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    """Checksum a payload without reading it all into memory.

    Used by the agent to detect a truncated or partially replicated file on
    a share that was mid-reconnect -- the case a folder queue is otherwise
    blind to.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def format_stem(created: datetime, prefix: str, first_label: str, last_label: str, job_id: str) -> str:
    """Build the shared filename stem for a job.

    Sortable by time, greppable by cable number, unique by job id. The
    numbers are the ZERO-PADDED forms, so searching the queue for the
    identifier printed on a cable finds the job that produced it.
    """
    ts = created.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}_{prefix}_{first_label}-{last_label}_{job_id[:8]}"


def parse_stem(stem: str) -> dict:
    """Inverse of format_stem. Raises ValueError on anything unparseable.

    The agent uses this only for logging and for pairing files; it trusts
    the sidecar, never the filename, for anything that matters.
    """
    match = _STEM_RE.match(stem)
    if not match:
        raise ValueError(f"not a valid job stem: {stem!r}")
    parts = match.groupdict()
    return {
        "ts": parts["ts"],
        "prefix": parts["prefix"],
        "first": int(parts["first"]),
        "last": int(parts["last"]),
        "first_label": parts["first"],
        "last_label": parts["last"],
        "job_id_short": parts["job_id_short"],
    }


def stem_of(path: Path) -> str:
    return path.stem


def ensure_queue(root: Path) -> Path:
    """Create the queue layout if absent and confirm it is writable.

    Called at startup by both halves. A queue root that exists but cannot
    be written is the single most common deployment failure here (a share
    mounted read-only, or mounted as the wrong user), and it must surface
    at startup rather than on the first job.
    """
    root = Path(root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        for sub in SUBDIRS:
            (root / sub).mkdir(exist_ok=True)
    except OSError as exc:
        raise QueueError(f"cannot create the queue layout under {root}: {exc}") from exc

    probe = root / ".tmp" / f".writetest-{os.getpid()}"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        raise QueueError(
            f"queue root {root} is not writable: {exc}. Check the share is mounted "
            f"read-write and that this process's user owns it."
        ) from exc
    return root


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    """Best effort. Directory fsync is meaningless on Windows and not
    supported on some network filesystems; a failure here does not make the
    rename any less atomic, so it is logged and swallowed."""
    try:
        _fsync_path(path)
    except (OSError, PermissionError) as exc:  # pragma: no cover - platform dependent
        logger.debug("directory fsync skipped for %s: %s", path, exc)


def _write_bytes_durable(path: Path, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def write_job(root: Path, sidecar: dict, payload: bytes) -> dict:
    """Write one job into inbox/ so that a consumer can never see it partly.

    The order is load-bearing and is spelled out in spec 6.2:

      1. payload  -> .tmp/<stem>.<ext>   (fsync)
      2. sidecar  -> .tmp/<stem>.json    (fsync)
      3. rename sidecar -> inbox/<stem>.json
      4. rename payload -> inbox/<stem>.<ext>

    The agent triggers on the payload extension, so step 4 is what publishes
    the job, and by then the sidecar is already complete and in place.

    Returns the paths written. Raises QueueError after cleaning up any
    partial .tmp files, so a failed write never leaves debris that a later
    sweep has to reason about.
    """
    root = Path(root)
    validate_job(sidecar)

    if sha256_bytes(payload) != sidecar["payload_sha256"]:
        raise QueueError(
            f"payload checksum does not match the sidecar for job {sidecar['job_id']} -- "
            "the sidecar must be built from the exact bytes being written"
        )
    if len(payload) != sidecar["payload_bytes"]:
        raise QueueError(
            f"payload_bytes {sidecar['payload_bytes']} != actual {len(payload)} "
            f"for job {sidecar['job_id']}"
        )

    stem = Path(sidecar["payload_file"]).stem
    ext = Path(sidecar["payload_file"]).suffix.lstrip(".")
    if ext not in PAYLOAD_EXTENSIONS:
        raise QueueError(f"payload extension {ext!r} is not one the agent watches for")

    tmp_dir, inbox = root / ".tmp", root / "inbox"
    tmp_payload = tmp_dir / f"{stem}.{ext}"
    tmp_sidecar = tmp_dir / f"{stem}.json"
    final_payload = inbox / f"{stem}.{ext}"
    final_sidecar = inbox / f"{stem}.json"

    try:
        _write_bytes_durable(tmp_payload, payload)
        _write_bytes_durable(
            tmp_sidecar, json.dumps(sidecar, indent=2, sort_keys=True).encode("utf-8")
        )
        os.replace(tmp_sidecar, final_sidecar)
        os.replace(tmp_payload, final_payload)
        _fsync_dir(inbox)
    except OSError as exc:
        for leftover in (tmp_payload, tmp_sidecar):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort cleanup
                pass
        raise QueueError(f"failed writing job {sidecar['job_id']} to {inbox}: {exc}") from exc

    return {"sidecar": final_sidecar, "payload": final_payload, "stem": stem}


def write_result(root: Path, result: dict) -> Path:
    """Write a result sidecar to results/ with the same tmp-then-rename
    discipline, so the Docker half never reads a half-written result."""
    root = Path(root)
    validate_result(result)
    tmp = root / ".tmp" / f"result-{result['job_id']}.json"
    final = root / "results" / f"{result['job_id']}.json"
    try:
        _write_bytes_durable(tmp, json.dumps(result, indent=2, sort_keys=True).encode("utf-8"))
        os.replace(tmp, final)
        _fsync_dir(root / "results")
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass
        raise QueueError(f"failed writing result for job {result['job_id']}: {exc}") from exc
    return final


def read_json(path: Path) -> dict:
    """Read a protocol file. Raises QueueError on IO trouble and
    json.JSONDecodeError on malformed content, so callers can tell a
    vanished file apart from a corrupt one."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except OSError as exc:
        raise QueueError(f"cannot read {path}: {exc}") from exc


def iter_inbox_jobs(root: Path):
    """Yield (payload_path, sidecar_path) for every job in inbox/, oldest
    first by filename (which sorts by timestamp).

    Used by the agent's startup sweep to pick up jobs that arrived while it
    was not running -- the case a pure filesystem watcher misses entirely.
    """
    inbox = Path(root) / "inbox"
    if not inbox.is_dir():
        return
    payloads = [p for p in inbox.iterdir() if p.suffix.lstrip(".") in PAYLOAD_EXTENSIONS]
    for payload in sorted(payloads, key=lambda p: p.name):
        yield payload, payload.with_suffix(".json")


def move_job(root: Path, stem: str, src: str, dest: str) -> None:
    """Move a job's payload and sidecar between queue folders.

    Missing files are tolerated: a job whose payload the agent already moved
    but whose sidecar it has not is a state worth completing rather than
    failing on, and a partially moved job on the previous run's crash is
    exactly what the orphan recovery path is trying to tidy up.
    """
    root = Path(root)
    src_dir, dest_dir = root / src, root / dest
    moved = 0
    for candidate in list(src_dir.glob(f"{stem}.*")):
        try:
            os.replace(candidate, dest_dir / candidate.name)
            moved += 1
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                continue
            raise QueueError(f"cannot move {candidate} to {dest_dir}: {exc}") from exc
    if moved == 0:
        logger.warning("nothing to move for stem %s from %s to %s", stem, src, dest)


def sweep_stale_tmp(root: Path, max_age_seconds: int = _STALE_TMP_SECONDS) -> int:
    """Delete .tmp debris left by a crash mid-write. Returns the count.

    Only files older than max_age_seconds are removed, so a sweep can never
    race a write that is still in progress in another process.
    """
    tmp_dir = Path(root) / ".tmp"
    if not tmp_dir.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    removed = 0
    for path in tmp_dir.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:  # pragma: no cover - best effort
            logger.debug("could not sweep %s: %s", path, exc)
    if removed:
        logger.info("swept %d stale file(s) from %s", removed, tmp_dir)
    return removed
