"""The watchdog that turns silence into a status line.

Every other outcome writes something to Halo. A print host that goes away
writes nothing: the job waits in inbox/, the asset still reads QUEUED, and
nobody can tell that from "printing shortly". These tests pin when that
becomes a STALLED line -- and, as importantly, when it does not, because a
watchdog that cries wolf during a long run gets ignored.
"""
import time
from dataclasses import replace

import pytest

from src.enqueue import write as enqueue_write
from src.models import Plan, Reservation
from src.reconcile import Reconciler


class FakeHalo:
    def __init__(self):
        self.status_writes = []

    def write_status(self, asset_id, field_id, line):
        self.status_writes.append((asset_id, line))


@pytest.fixture
def cfg(app_config, queue_root):
    return replace(app_config, queue=replace(app_config.queue, root=queue_root), stalled_after_minutes=30)


def _queue_a_job(cfg, payload, first=100, last=102, asset_id=4711):
    plan = Plan(
        asset_id=asset_id, prefix="BL", first_number=first, last_number=last,
        cable_count=last - first + 1, labels_per_cable=2, requested_qty=last - first + 1,
    )
    reservation = Reservation(
        plan=plan,
        identifiers=tuple(f"BL-{n:04d}" for n in range(first, last + 1)),
        reserved_utc="2026-09-21T12:00:00Z",
    )
    return enqueue_write(reservation, payload, cfg)


def _complete_a_job(cfg, written):
    """A real result for a queued job, as the agent would leave behind."""
    import json

    from protocol import move_job, utc_now_iso, write_result

    job = json.loads(written["sidecar"].read_text())
    move_job(cfg.queue.root, written["payload"].stem, "inbox", "done")   # as the agent does
    write_result(cfg.queue.root, {
        "protocol_version": 1,
        "job_id": job["job_id"],
        "status": "success",
        "agent_host": "print-host",
        "finished_utc": utc_now_iso(),
        "labels_submitted": job["label_count"],
        "job": job,
    })


def _go_quiet_for(reconciler, minutes):
    reconciler._last_progress = time.time() - minutes * 60


def test_nothing_queued_is_never_a_stall(cfg):
    """An idle system is not a broken one."""
    reconciler = Reconciler(cfg)
    _go_quiet_for(reconciler, 120)
    halo = FakeHalo()
    assert reconciler.check_for_stalls(halo) == 0
    assert halo.status_writes == []


def test_a_job_waiting_less_than_the_threshold_is_not_a_stall(cfg, payload):
    _queue_a_job(cfg, payload)
    reconciler = Reconciler(cfg)
    _go_quiet_for(reconciler, 29)
    halo = FakeHalo()
    assert reconciler.check_for_stalls(halo) == 0
    assert halo.status_writes == []


def test_silence_while_work_waits_writes_a_stalled_line(cfg, payload):
    _queue_a_job(cfg, payload)
    reconciler = Reconciler(cfg)
    _go_quiet_for(reconciler, 31)
    halo = FakeHalo()

    assert reconciler.check_for_stalls(halo) == 1
    asset_id, line = halo.status_writes[0]
    assert asset_id == 4711
    assert line.startswith("STALLED BL-0100..BL-0102")
    assert "3 cables, 6 labels" in line
    assert "no response from the print host" in line


def test_each_waiting_job_is_reported_once_not_every_poll(cfg, payload):
    """A stall lasts until someone fixes it. Rewriting the same line every
    15 seconds would bury the field's history and hammer the API."""
    _queue_a_job(cfg, payload)
    reconciler = Reconciler(cfg)
    _go_quiet_for(reconciler, 31)
    halo = FakeHalo()

    assert reconciler.check_for_stalls(halo) == 1
    assert reconciler.check_for_stalls(halo) == 0
    assert len(halo.status_writes) == 1


def test_every_waiting_asset_hears_about_it(cfg, payload):
    """Two technicians waiting on two cable types both need telling."""
    _queue_a_job(cfg, payload, first=100, last=101, asset_id=1)
    _queue_a_job(cfg, payload, first=200, last=201, asset_id=2)
    reconciler = Reconciler(cfg)
    _go_quiet_for(reconciler, 31)
    halo = FakeHalo()

    assert reconciler.check_for_stalls(halo) == 2
    assert sorted(asset for asset, _ in halo.status_writes) == [1, 2]


def test_a_long_healthy_run_is_not_a_stall(cfg, payload, queue_root):
    """The case that would make this useless: a backlog of batches printing
    a minute apart. Each completion resets the clock, so the jobs still
    waiting their turn are never called stalled."""
    written = _queue_a_job(cfg, payload)
    reconciler = Reconciler(cfg)
    halo = FakeHalo()

    _go_quiet_for(reconciler, 31)
    _complete_a_job(cfg, written)                              # a batch finishes
    reconciler.run(halo)                                       # ... and is reconciled
    assert reconciler.check_for_stalls(halo) == 0, "a completed batch resets the clock"
    assert [line for _, line in halo.status_writes if "STALLED" in line] == []


def test_recovery_then_a_new_stall_is_reported_again(cfg, payload, queue_root):
    written = _queue_a_job(cfg, payload)
    reconciler = Reconciler(cfg)
    halo = FakeHalo()

    _go_quiet_for(reconciler, 31)
    assert reconciler.check_for_stalls(halo) == 1

    _complete_a_job(cfg, written)
    reconciler.run(halo)                                       # progress clears the memory
    _queue_a_job(cfg, payload, first=300, last=301)            # more work arrives
    _go_quiet_for(reconciler, 31)
    assert reconciler.check_for_stalls(halo) == 1, "a second outage must be reported too"
    assert sum("STALLED" in line for _, line in halo.status_writes) == 2


def test_a_test_job_with_no_asset_is_logged_but_not_written(cfg, payload, caplog):
    """Test jobs carry no asset id, so there is nothing to write to."""
    _queue_a_job(cfg, payload, asset_id=None)
    reconciler = Reconciler(cfg)
    _go_quiet_for(reconciler, 31)
    halo = FakeHalo()

    with caplog.at_level("ERROR"):
        assert reconciler.check_for_stalls(halo) == 1
    assert halo.status_writes == []
    assert "STALLED" in caplog.text
