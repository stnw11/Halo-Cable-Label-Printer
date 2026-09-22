"""The whole pipeline with the null backend -- spec criterion 23.

Halo is faked, the share is a tmp_path, and nothing prints. Everything
between those two ends is the real code: the poll loop, the allocator, the
renderer, the queue protocol, the agent, and the reconciler.

The spec puts this before any media is loaded on purpose. If this passes,
the only thing left to verify on real hardware is the physical layout.
"""
from dataclasses import replace

import pytest

from src import guards
from src.config import AppConfig
from src.main import poll_once
from src.reconcile import Reconciler
from tests.conftest import NEXTID_FIELD, PREFIX_FIELD, QTY_FIELD, STATUS_FIELD
from winagent.src.agent import Agent


class FakeTenant:
    """A Halo stand-in that stores assets as Halo actually returns them:
    a `fields` array of {id, name, value}, with values as strings."""

    def __init__(self, assets: list[dict]):
        self.assets = {a["id"]: a for a in assets}
        self.status_writes = []
        self.fail_status_writes = 0

    @staticmethod
    def make_asset(asset_id=4711, prefix="BL", next_id=100, qty=0, status=""):
        return {
            "id": asset_id,
            "inventory_number": "Blue patch",
            "fields": [
                {"id": PREFIX_FIELD, "name": "Cable ID Prefix", "value": prefix},
                {"id": NEXTID_FIELD, "name": "Next Cable ID", "value": str(next_id)},
                {"id": QTY_FIELD, "name": "Cables to Label", "value": str(qty)},
                {"id": STATUS_FIELD, "name": "Last Label Run", "value": status},
            ],
        }

    def _field(self, asset_id, field_id):
        for entry in self.assets[asset_id]["fields"]:
            if entry["id"] == field_id:
                return entry
        raise KeyError(field_id)

    def value(self, asset_id, field_id):
        return self._field(asset_id, field_id)["value"]

    # --- the client interface the service uses ------------------------------

    def iter_assets(self, page_size=50, asset_group_id=None):
        yield from self.assets.values()

    def update_fields(self, asset_id, updates):
        for update in updates:
            self._field(asset_id, update["id"])["value"] = str(update["value"])

    def read_counter(self, asset_id, field_id):
        return int(self.value(asset_id, field_id))

    def write_status(self, asset_id, field_id, line):
        if self.fail_status_writes > 0:
            self.fail_status_writes -= 1
            raise RuntimeError("Halo returned 503")
        self.status_writes.append((asset_id, line))
        self._field(asset_id, field_id)["value"] = line


@pytest.fixture
def cfg(app_config, queue_root) -> AppConfig:
    return replace(app_config, queue=replace(app_config.queue, root=queue_root))


@pytest.fixture
def agent(queue_root, tmp_path):
    return Agent(
        {
            "queue_root": queue_root,
            "printer_name": "Brady Wraptor A6200 (test)",
            "backend": "null",
            "sidecar_grace_seconds": 1,
            "print_timeout_seconds": 10,
            "retain_done_days": 30,
            "poll_seconds": 1,
            "job_interval_seconds": 0,   # no pacing: these tests print nothing
            "ledger_path": tmp_path / "ledger.sqlite",
            "log_path": tmp_path / "agent.log",
            "log_level": "INFO",
        }
    )


def _files(queue_root, folder, suffix=None):
    paths = list((queue_root / folder).iterdir())
    if suffix:
        paths = [p for p in paths if p.suffix == suffix]
    return paths


# --- the happy path ----------------------------------------------------------

def test_full_cycle_from_halo_field_to_status_line(cfg, queue_root, agent):
    """Criterion 23, plus 1, 2 and 10. Set the field, and the whole system
    runs to a SENT status line without anything physical happening."""
    tenant = FakeTenant([FakeTenant.make_asset(qty=12)])
    reconciler = Reconciler(cfg)

    # 1. The service sees the request, reserves, renders, enqueues.
    poll_once(tenant, cfg, reconciler)

    assert tenant.value(4711, NEXTID_FIELD) == "112", "counter must advance by 12"
    assert tenant.value(4711, QTY_FIELD) == "0", "trigger must be cleared"
    assert tenant.value(4711, STATUS_FIELD).startswith("QUEUED BL-0100..BL-0111")
    assert len(_files(queue_root, "inbox", ".png")) == 1

    # 2. The agent prints it (null backend) and reports.
    agent.sweep_inbox()

    assert _files(queue_root, "inbox") == []
    assert len(_files(queue_root, "done", ".png")) == 1
    assert _files(queue_root, "failed") == []
    assert len(_files(queue_root, "results")) == 1

    # 3. The service reconciles and writes the terminal status.
    poll_once(tenant, cfg, reconciler)

    assert _files(queue_root, "results") == []
    status = tenant.value(4711, STATUS_FIELD)
    assert status.startswith("SENT BL-0100..BL-0111")
    assert "12 cables, 24 labels" in status


def test_a_second_request_continues_the_sequence(cfg, queue_root, agent):
    """The core guarantee, through the real loop rather than a unit test."""
    tenant = FakeTenant([FakeTenant.make_asset(qty=3)])
    reconciler = Reconciler(cfg)

    poll_once(tenant, cfg, reconciler)
    tenant._field(4711, QTY_FIELD)["value"] = "2"
    poll_once(tenant, cfg, reconciler)

    assert tenant.value(4711, NEXTID_FIELD) == "105"
    stems = sorted(p.stem for p in _files(queue_root, "inbox", ".png"))
    assert "_BL_0100-0102_" in stems[0]
    assert "_BL_0103-0104_" in stems[1]


def test_nothing_pending_does_nothing(cfg, queue_root):
    tenant = FakeTenant([FakeTenant.make_asset(qty=0)])
    poll_once(tenant, cfg, Reconciler(cfg))
    assert _files(queue_root, "inbox") == []
    assert tenant.value(4711, NEXTID_FIELD) == "100"


# --- safety valves -----------------------------------------------------------

def test_shadow_mode_reserves_nothing(cfg, queue_root):
    """Criterion 8."""
    tenant = FakeTenant([FakeTenant.make_asset(qty=5)])
    poll_once(tenant, replace(cfg, shadow_mode=True), Reconciler(cfg))
    assert tenant.value(4711, NEXTID_FIELD) == "100"
    assert tenant.value(4711, QTY_FIELD) == "5"
    assert _files(queue_root, "inbox") == []


def test_dry_run_consumes_numbers_but_enqueues_nothing(cfg, queue_root):
    """DRY_RUN is not harmless, and the test records that it is not."""
    tenant = FakeTenant([FakeTenant.make_asset(qty=5)])
    poll_once(tenant, replace(cfg, dry_run=True), Reconciler(cfg))
    assert tenant.value(4711, NEXTID_FIELD) == "105"
    assert _files(queue_root, "inbox") == []


def test_invalid_asset_stays_flagged(cfg, queue_root):
    """Criterion 7, through the loop: a bad value must not eat the request."""
    tenant = FakeTenant([FakeTenant.make_asset(prefix="", qty=4)])
    poll_once(tenant, cfg, Reconciler(cfg))
    assert tenant.value(4711, QTY_FIELD) == "4", "the request must survive"
    assert tenant.value(4711, NEXTID_FIELD) == "100"
    assert _files(queue_root, "inbox") == []


def test_duplicate_prefixes_block_both_assets(cfg, queue_root):
    """Criterion 6, through the loop."""
    tenant = FakeTenant([
        FakeTenant.make_asset(asset_id=1, prefix="BL", next_id=100, qty=1),
        FakeTenant.make_asset(asset_id=2, prefix="BL", next_id=500, qty=1),
    ])
    poll_once(tenant, cfg, Reconciler(cfg))
    assert tenant.value(1, QTY_FIELD) == "1"
    assert tenant.value(2, QTY_FIELD) == "1"
    assert _files(queue_root, "inbox") == []


def test_a_request_bigger_than_one_batch_is_split_not_truncated(cfg, queue_root, agent, monkeypatch):
    """Criterion 3, and the bug it used to hide: a request of 500 with a
    batch size of 100 reserved 100 and threw the other 400 away, leaving a
    technician looking at a finished-looking run and 400 unlabelled cables.
    The remainder now stays on the asset for the next poll."""
    monkeypatch.setattr(guards, "BATCH_SIZE", 100)
    tenant = FakeTenant([FakeTenant.make_asset(qty=500)])

    poll_once(tenant, cfg, Reconciler(cfg))
    assert tenant.value(4711, NEXTID_FIELD) == "200"        # 100 taken, starting at 100
    assert tenant.value(4711, QTY_FIELD) == "400"           # the rest is still queued
    status = tenant.value(4711, STATUS_FIELD)
    assert "100 of 500 cables, 200 labels" in status
    assert "400 still to print" in status

    poll_once(tenant, cfg, Reconciler(cfg))
    assert tenant.value(4711, NEXTID_FIELD) == "300"
    assert tenant.value(4711, QTY_FIELD) == "300"


def test_batches_run_until_the_request_is_done(cfg, queue_root, agent, monkeypatch):
    """Five polls finish a 500 request at 100 a batch, and the counter has
    advanced exactly 500 with no gaps or repeats."""
    monkeypatch.setattr(guards, "BATCH_SIZE", 100)
    tenant = FakeTenant([FakeTenant.make_asset(qty=500)])

    for _ in range(5):
        poll_once(tenant, cfg, Reconciler(cfg))

    assert tenant.value(4711, QTY_FIELD) == "0"
    assert tenant.value(4711, NEXTID_FIELD) == "600"        # started at 100
    assert "still to print" not in tenant.value(4711, STATUS_FIELD)
    assert len(_files(queue_root, "inbox", ".png")) == 5     # one job per batch


def test_a_request_that_fits_in_one_batch_says_nothing_about_batches(cfg, queue_root, agent):
    tenant = FakeTenant([FakeTenant.make_asset(qty=3)])
    poll_once(tenant, cfg, Reconciler(cfg))
    status = tenant.value(4711, STATUS_FIELD)
    assert "3 cables, 6 labels" in status and "of" not in status.split("·")[1]
    assert tenant.value(4711, QTY_FIELD) == "0"


# --- agent-side protection ---------------------------------------------------

def test_replayed_job_is_rejected_and_not_reprinted(cfg, queue_root, agent):
    """Criterion 18. Copy a completed job back into inbox/ by hand."""
    tenant = FakeTenant([FakeTenant.make_asset(qty=2)])
    poll_once(tenant, cfg, Reconciler(cfg))
    agent.sweep_inbox()

    for path in _files(queue_root, "done"):
        (queue_root / "inbox" / path.name).write_bytes(path.read_bytes())
    for path in _files(queue_root, "results"):
        path.unlink()

    agent.sweep_inbox()

    assert _files(queue_root, "inbox") == []
    assert len(_files(queue_root, "failed", ".png")) == 1
    result = __import__("json").loads(_files(queue_root, "results")[0].read_text())
    assert result["status"] == "rejected"
    assert "replay" in result["error"]


def test_corrupted_payload_is_rejected(cfg, queue_root, agent):
    """Criterion 19."""
    tenant = FakeTenant([FakeTenant.make_asset(qty=2)])
    poll_once(tenant, cfg, Reconciler(cfg))
    _files(queue_root, "inbox", ".png")[0].write_bytes(b"truncated")

    agent.sweep_inbox()

    result = __import__("json").loads(_files(queue_root, "results")[0].read_text())
    assert result["status"] == "rejected"
    assert "checksum" in result["error"]
    assert _files(queue_root, "done") == []


def test_unknown_protocol_version_is_rejected_not_guessed(cfg, queue_root, agent):
    """Criterion 20. This is what lets the halves upgrade independently."""
    import json

    tenant = FakeTenant([FakeTenant.make_asset(qty=2)])
    poll_once(tenant, cfg, Reconciler(cfg))
    sidecar_path = _files(queue_root, "inbox", ".json")[0]
    sidecar = json.loads(sidecar_path.read_text())
    sidecar["protocol_version"] = 99
    sidecar_path.write_text(json.dumps(sidecar))

    agent.sweep_inbox()

    result = json.loads(_files(queue_root, "results")[0].read_text())
    assert result["status"] == "rejected"
    assert "protocol_version" in result["error"]


def test_orphan_in_processing_is_failed_not_reprinted(cfg, queue_root, agent):
    """Criterion 21. The agent cannot know how many labels already came out
    of the applicator, so it never retries one."""
    import json

    tenant = FakeTenant([FakeTenant.make_asset(qty=2)])
    poll_once(tenant, cfg, Reconciler(cfg))

    payload = _files(queue_root, "inbox", ".png")[0]
    sidecar = _files(queue_root, "inbox", ".json")[0]
    job_id = json.loads(sidecar.read_text())["job_id"]
    agent.ledger.claim(job_id, payload.stem)
    for path in (payload, sidecar):
        path.rename(queue_root / "processing" / path.name)

    agent.recover_orphans()

    assert _files(queue_root, "processing") == []
    assert len(_files(queue_root, "failed", ".png")) == 1
    result = json.loads(_files(queue_root, "results")[0].read_text())
    assert result["status"] == "failed"
    assert "interrupted" in result["error"]


# --- failure reporting -------------------------------------------------------

def test_print_failure_reaches_the_halo_status_field(cfg, queue_root, agent, monkeypatch):
    """Criterion 11. The FAILED line must carry the range, because that
    range is what the reprint tool needs."""
    from winagent.src.backends import PrintError

    tenant = FakeTenant([FakeTenant.make_asset(qty=6)])
    reconciler = Reconciler(cfg)
    poll_once(tenant, cfg, reconciler)

    def boom(payload_path, sidecar):
        raise PrintError("printer offline")

    monkeypatch.setattr(agent.backend, "print", boom)
    agent.sweep_inbox()
    poll_once(tenant, cfg, reconciler)

    status = tenant.value(4711, STATUS_FIELD)
    assert status.startswith("FAILED BL-0100..BL-0105")
    assert "printer offline" in status
    assert len(_files(queue_root, "failed", ".png")) == 1
    assert tenant.value(4711, NEXTID_FIELD) == "106", "the numbers stay consumed"


def test_failed_status_write_is_retried_then_abandoned(cfg, queue_root, agent):
    """Criterion 13. One unwritable asset must not wedge the queue."""
    tenant = FakeTenant([FakeTenant.make_asset(qty=2)])
    reconciler = Reconciler(replace(cfg, status_write_max_attempts=3))
    poll_once(tenant, cfg, reconciler)
    agent.sweep_inbox()

    tenant.fail_status_writes = 99

    reconciler.run(tenant)
    assert len(_files(queue_root, "results")) == 1, "the result survives for a retry"
    reconciler.run(tenant)
    assert len(_files(queue_root, "results")) == 1
    reconciler.run(tenant)
    assert _files(queue_root, "results") == [], "dropped after the attempt cap"


def test_status_write_recovers_on_a_later_poll(cfg, queue_root, agent):
    tenant = FakeTenant([FakeTenant.make_asset(qty=2)])
    reconciler = Reconciler(cfg)
    poll_once(tenant, cfg, reconciler)
    agent.sweep_inbox()

    tenant.fail_status_writes = 1
    reconciler.run(tenant)
    assert len(_files(queue_root, "results")) == 1

    reconciler.run(tenant)
    assert _files(queue_root, "results") == []
    assert tenant.value(4711, STATUS_FIELD).startswith("SENT")


def test_halo_unreachable_does_not_crash_the_poll(cfg, queue_root):
    """Criterion 28. A Halo outage delays labelling; it must never kill the
    container."""
    class Broken(FakeTenant):
        def iter_assets(self, page_size=50, asset_group_id=None):
            raise RuntimeError("connection refused")

    poll_once(Broken([FakeTenant.make_asset(qty=1)]), cfg, Reconciler(cfg))
    assert _files(queue_root, "inbox") == []


def test_share_unavailable_burns_the_block_and_says_so(cfg, queue_root, caplog, monkeypatch):
    """Criterion 22. The numbers are gone; the log must carry the recovery
    command, because that log line is the user interface for this failure."""
    from protocol import QueueError

    tenant = FakeTenant([FakeTenant.make_asset(qty=4)])
    monkeypatch.setattr(
        "src.enqueue.write_job",
        lambda *a, **k: (_ for _ in ()).throw(QueueError("share unreachable")),
    )
    monkeypatch.setattr("src.enqueue.time.sleep", lambda s: None)

    with caplog.at_level("ERROR"):
        poll_once(tenant, cfg, Reconciler(cfg))

    assert tenant.value(4711, NEXTID_FIELD) == "104", "the numbers stay consumed"
    assert "RESERVED BUT NOT ENQUEUED" in caplog.text
    assert "--prefix BL --from 100 --to 103 --asset-id 4711" in caplog.text

    # The log reaches whoever is watching the Docker host. The technician is
    # watching Halo, where the reserve has already written QUEUED -- and no
    # job file exists, so the stall watchdog cannot see this one either.
    status = tenant.value(4711, STATUS_FIELD)
    assert status.startswith("FAILED BL-0100..BL-0103"), status
    assert "could not write to the print queue" in status
    assert "needs a reprint" in status


def test_a_share_outage_is_reported_even_though_halo_is_reachable(cfg, queue_root, monkeypatch):
    """The two connections fail independently. A dead share must not stop the
    service using the live Halo connection to say so."""
    from protocol import QueueError

    tenant = FakeTenant([FakeTenant.make_asset(qty=2)])
    monkeypatch.setattr(
        "src.enqueue.write_job",
        lambda *a, **k: (_ for _ in ()).throw(QueueError("share unreachable")),
    )
    monkeypatch.setattr("src.enqueue.time.sleep", lambda s: None)

    poll_once(tenant, cfg, Reconciler(cfg))

    # QUEUED is not a separate write: the reserve bundles it with the counter
    # and the trigger in one update, so there is no window where the numbers
    # are taken and the asset does not say so. The only write_status here is
    # the correction.
    words = [line.split()[0] for _, line in tenant.status_writes]
    assert words == ["FAILED"]
    assert tenant.value(4711, STATUS_FIELD).startswith("FAILED"), "the asset ends on the truth"


def test_halo_down_as_well_as_the_share_still_does_not_crash(cfg, queue_root, caplog, monkeypatch):
    """Both connections gone at once. The log line with the reprint command
    is the last resort, so it must survive the status write failing too."""
    from protocol import QueueError

    tenant = FakeTenant([FakeTenant.make_asset(qty=2)])
    tenant.fail_status_writes = 99
    monkeypatch.setattr(
        "src.enqueue.write_job",
        lambda *a, **k: (_ for _ in ()).throw(QueueError("share unreachable")),
    )
    monkeypatch.setattr("src.enqueue.time.sleep", lambda s: None)

    with caplog.at_level("ERROR"):
        poll_once(tenant, cfg, Reconciler(cfg))

    assert "RESERVED BUT NOT ENQUEUED" in caplog.text


def test_the_heartbeat_beats_between_assets_not_once_per_poll(cfg, queue_root, monkeypatch):
    """The healthcheck calls the container unhealthy when the heartbeat is
    older than 45s. A poll of MAX_ASSETS_PER_POLL assets, each rendering a
    full batch, takes tens of seconds: measured at 3.5s per 250-cable asset,
    ten of them exceed the budget on render time alone. So the beat has to
    fall between assets, or the service fails its own check for doing
    exactly what it was configured to do."""
    import src.main as main_module

    events = []
    monkeypatch.setattr(main_module, "beat", lambda: events.append("beat"))
    real_process = main_module.process_asset
    monkeypatch.setattr(
        main_module,
        "process_asset",
        lambda asset, client, config: (events.append(f"asset {asset.id}"), real_process(asset, client, config))[1],
    )

    tenant = FakeTenant([
        FakeTenant.make_asset(asset_id=100 + i, prefix=f"P{i}", next_id=1, qty=1)
        for i in range(3)
    ])
    poll_once(tenant, cfg, Reconciler(cfg))

    assert events == [
        "asset 100", "beat",
        "asset 101", "beat",
        "asset 102", "beat",
    ], events
