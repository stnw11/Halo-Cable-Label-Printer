"""Queue protocol tests -- spec acceptance criteria 16 through 20.

These need no Halo, no share, and no printer, which is why the build order
puts them first: the interface between the two halves can be proven correct
before either half exists.
"""
import json
import os
import time
from datetime import datetime, timezone

import jsonschema
import pytest

from protocol import (
    PAYLOAD_EXTENSIONS,
    QueueError,
    format_stem,
    iter_inbox_jobs,
    move_job,
    parse_stem,
    sha256_bytes,
    sha256_file,
    sweep_stale_tmp,
    utc_now_iso,
    validate_job,
    validate_result,
    write_job,
    write_result,
)
from tests.conftest import FIXED_TIME, JOB_ID


# --- naming ------------------------------------------------------------------

def test_stem_round_trips():
    stem = format_stem(FIXED_TIME, "BL", "0100", "0111", JOB_ID)
    assert stem == "20260918T141205Z_BL_0100-0111_1f4c9a2e"
    parsed = parse_stem(stem)
    assert parsed["prefix"] == "BL"
    assert parsed["first"] == 100
    assert parsed["last"] == 111
    assert parsed["first_label"] == "0100"       # padding survives the round trip
    assert parsed["job_id_short"] == "1f4c9a2e"


def test_stem_is_greppable_by_printed_identifier():
    """The number in the stem is the padded form, so grepping the queue for
    what is physically on a cable finds the job that produced it."""
    stem = format_stem(FIXED_TIME, "BL", "0100", "0111", JOB_ID)
    assert "0100" in stem


def test_stems_sort_chronologically():
    early = format_stem(datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc), "BL", "0001", "0002", JOB_ID)
    late = format_stem(FIXED_TIME, "BL", "0001", "0002", JOB_ID)
    assert sorted([late, early]) == [early, late]


@pytest.mark.parametrize("bad", ["", "nonsense", "20260918T141205Z_BL_0100-0111", "x_BL_1-2_1f4c9a2e"])
def test_parse_stem_rejects_junk(bad):
    with pytest.raises(ValueError):
        parse_stem(bad)


def test_utc_now_iso_matches_schema_shape():
    assert utc_now_iso(FIXED_TIME) == "2026-09-18T14:12:05Z"


# --- schema ------------------------------------------------------------------

def test_valid_job_passes(sidecar):
    assert validate_job(sidecar) is sidecar


def test_job_rejects_unknown_field(sidecar):
    sidecar["surprise"] = 1
    with pytest.raises(jsonschema.ValidationError):
        validate_job(sidecar)


def test_job_rejects_wrong_protocol_version(sidecar):
    sidecar["protocol_version"] = 99
    with pytest.raises(jsonschema.ValidationError):
        validate_job(sidecar)


def test_job_rejects_label_count_inconsistent_with_cables(sidecar):
    """The 2N doubling is the thing most likely to be got wrong, so the
    protocol refuses to carry a sidecar whose arithmetic disagrees."""
    sidecar["label_count"] = 12          # forgot to double for both ends
    with pytest.raises(jsonschema.ValidationError, match="label_count"):
        validate_job(sidecar)


def test_job_rejects_range_inconsistent_with_cable_count(sidecar):
    sidecar["last_number"] = 200
    with pytest.raises(jsonschema.ValidationError, match="cable_count"):
        validate_job(sidecar)


def test_job_rejects_reversed_range(sidecar):
    sidecar["first_number"], sidecar["last_number"] = 111, 100
    with pytest.raises(jsonschema.ValidationError):
        validate_job(sidecar)


def test_job_allows_null_asset_id_for_reprints(sidecar):
    sidecar["halo_asset_id"] = None
    sidecar["source"] = "reprint"
    sidecar["reprint"] = True
    assert validate_job(sidecar)


def test_result_schema(sidecar):
    result = {
        "protocol_version": 1,
        "job_id": JOB_ID,
        "status": "success",
        "agent_host": "PRINTSRV01",
        "finished_utc": "2026-09-18T14:12:19Z",
        "labels_submitted": 24,
        "backend": "null",
        "error": None,
    }
    assert validate_result(result)
    result["status"] = "exploded"
    with pytest.raises(jsonschema.ValidationError):
        validate_result(result)


# --- atomic write (criterion 16) ---------------------------------------------

def test_write_job_publishes_both_files(queue_root, sidecar, payload):
    written = write_job(queue_root, sidecar, payload)
    assert written["payload"].exists() and written["sidecar"].exists()
    assert written["payload"].read_bytes() == payload
    assert json.loads(written["sidecar"].read_text())["job_id"] == JOB_ID


def test_tmp_is_empty_after_a_successful_write(queue_root, sidecar, payload):
    write_job(queue_root, sidecar, payload)
    assert list((queue_root / ".tmp").iterdir()) == []


def test_sidecar_is_in_place_before_the_payload_appears(queue_root, sidecar, payload, monkeypatch):
    """Criterion 16. The agent triggers on the payload, so the sidecar must
    already be complete when the payload becomes visible. Interrupting the
    final rename proves the ordering rather than assuming it."""
    real_replace = os.replace
    calls = []

    def spy(src, dst):
        calls.append(str(dst))
        if str(dst).endswith(".png"):
            raise OSError("interrupted before publishing the payload")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    with pytest.raises(QueueError):
        write_job(queue_root, sidecar, payload)

    assert calls[0].endswith(".json"), "the sidecar must be renamed in first"
    inbox = list((queue_root / "inbox").iterdir())
    assert [p.suffix for p in inbox] == [".json"]
    assert not any(p.suffix == ".png" for p in inbox), "no payload may be visible"


def test_failed_write_leaves_no_tmp_debris(queue_root, sidecar, payload, monkeypatch):
    def boom(src, dst):
        raise OSError("share went away")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(QueueError):
        write_job(queue_root, sidecar, payload)
    assert list((queue_root / ".tmp").iterdir()) == []


def test_write_job_refuses_a_checksum_that_does_not_match(queue_root, sidecar, payload):
    sidecar["payload_sha256"] = sha256_bytes(b"different bytes entirely")
    with pytest.raises(QueueError, match="checksum"):
        write_job(queue_root, sidecar, payload)
    assert list((queue_root / "inbox").iterdir()) == []


def test_write_job_refuses_a_size_that_does_not_match(queue_root, sidecar, payload):
    sidecar["payload_bytes"] = len(payload) + 1
    with pytest.raises(QueueError, match="payload_bytes"):
        write_job(queue_root, sidecar, payload)


def test_write_job_refuses_an_extension_the_agent_ignores(queue_root, sidecar, payload):
    sidecar["payload_file"] = sidecar["payload_file"].replace(".png", ".txt")
    with pytest.raises(QueueError, match="extension"):
        write_job(queue_root, sidecar, payload)


def test_checksum_detects_truncation(queue_root, sidecar, payload):
    """Criterion 19: a payload corrupted after its sidecar was written."""
    written = write_job(queue_root, sidecar, payload)
    written["payload"].write_bytes(payload[:5])
    assert sha256_file(written["payload"]) != sidecar["payload_sha256"]


# --- queue movement ----------------------------------------------------------

def test_iter_inbox_jobs_pairs_and_orders(queue_root, sidecar, payload):
    write_job(queue_root, sidecar, payload)
    jobs = list(iter_inbox_jobs(queue_root))
    assert len(jobs) == 1
    payload_path, sidecar_path = jobs[0]
    assert payload_path.suffix.lstrip(".") in PAYLOAD_EXTENSIONS
    assert sidecar_path.exists() and sidecar_path.suffix == ".json"


def test_move_job_moves_both_files(queue_root, sidecar, payload):
    written = write_job(queue_root, sidecar, payload)
    move_job(queue_root, written["stem"], "inbox", "processing")
    assert list((queue_root / "inbox").iterdir()) == []
    assert len(list((queue_root / "processing").iterdir())) == 2


def test_move_job_tolerates_a_half_moved_job(queue_root, sidecar, payload):
    """A crash between the two moves leaves one file behind; finishing the
    move is more useful than refusing to."""
    written = write_job(queue_root, sidecar, payload)
    os.replace(written["sidecar"], queue_root / "processing" / written["sidecar"].name)
    move_job(queue_root, written["stem"], "inbox", "processing")
    assert list((queue_root / "inbox").iterdir()) == []
    assert len(list((queue_root / "processing").iterdir())) == 2


# --- results -----------------------------------------------------------------

def test_write_result_round_trips(queue_root):
    result = {
        "protocol_version": 1,
        "job_id": JOB_ID,
        "status": "failed",
        "agent_host": "PRINTSRV01",
        "finished_utc": "2026-09-18T14:12:19Z",
        "error": "printer offline",
    }
    path = write_result(queue_root, result)
    assert path.name == f"{JOB_ID}.json"
    assert json.loads(path.read_text())["status"] == "failed"
    assert list((queue_root / ".tmp").iterdir()) == []


# --- housekeeping ------------------------------------------------------------

def test_sweep_removes_only_old_tmp_files(queue_root):
    old = queue_root / ".tmp" / "old.png"
    fresh = queue_root / ".tmp" / "fresh.png"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    assert sweep_stale_tmp(queue_root) == 1
    assert fresh.exists() and not old.exists()


def test_ensure_queue_reports_an_unwritable_root(tmp_path):
    from protocol import ensure_queue

    root = tmp_path / "ro"
    ensure_queue(root)
    os.chmod(root / ".tmp", 0o500)
    try:
        with pytest.raises(QueueError, match="not writable"):
            ensure_queue(root)
    finally:
        os.chmod(root / ".tmp", 0o700)
