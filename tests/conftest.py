import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from protocol import ensure_queue, format_stem, sha256_bytes  # noqa: E402

FIXED_TIME = datetime(2026, 9, 18, 14, 12, 5, tzinfo=timezone.utc)
JOB_ID = "1f4c9a2e-7b3d-4c51-9a0e-2b6f8d1c4e77"


@pytest.fixture
def repo_root():
    """The checkout itself, for tests that guard the shipped templates."""
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def queue_root(tmp_path):
    """A real queue layout under tmp_path. Never a real share -- see spec 8.5."""
    return ensure_queue(tmp_path / "queue")


@pytest.fixture
def payload():
    return b"%PDF-1.4 fake payload for tests\n"


@pytest.fixture
def sidecar(payload):
    stem = format_stem(FIXED_TIME, "BL", "0100", "0111", JOB_ID)
    return {
        "protocol_version": 1,
        "job_id": JOB_ID,
        "created_utc": "2026-09-18T14:12:05Z",
        "source": "poll",
        "reprint": False,
        "halo_asset_id": 4711,
        "job_name": "Blue CAT6 - BL-0100...BL-0111",
        "prefix": "BL",
        "first_number": 100,
        "last_number": 111,
        "cable_count": 12,
        "labels_per_cable": 2,
        "label_count": 24,
        "payload_file": f"{stem}.png",
        "payload_sha256": sha256_bytes(payload),
        "payload_bytes": len(payload),
        "render_format": "png",
        "dpi": 300,
        "page_width_in": 1.0,
        "page_height_in": 0.5,
    }


from src.config import AppConfig, HaloConfig, QueueConfig  # noqa: E402
from src.layout import LabelStyle, Media  # noqa: E402
from src.models import CableType  # noqa: E402

QTY_FIELD, PREFIX_FIELD, NEXTID_FIELD, STATUS_FIELD = 811, 812, 813, 814


@pytest.fixture
def app_config(tmp_path):
    """A complete, valid AppConfig with no IO behind it."""
    return AppConfig(
        halo=HaloConfig(
            base_url="https://example.invalid",
            auth_url="https://example.invalid/auth/token",
            client_id="id",
            client_secret="secret",
            qty_field_id=QTY_FIELD,
            prefix_field_id=PREFIX_FIELD,
            nextid_field_id=NEXTID_FIELD,
            status_field_id=STATUS_FIELD,
        ),
        queue=QueueConfig(root=tmp_path / "queue"),
        media=Media(),
        style=LabelStyle(),
    )


@pytest.fixture
def cable_type():
    return CableType(id=4711, prefix="BL", next_id=100, qty=12, name="Blue patch")


class FakeHalo:
    """Records writes and serves read-backs, so allocator tests never touch
    HTTP. Deliberately dumb: the point is to assert on what the allocator
    sent and in what order, not to simulate Halo."""

    def __init__(self, counter=100, write_error=None, read_error=None, counter_after=None):
        self.counter = counter
        self.write_error = write_error
        self.read_error = read_error
        self.counter_after = counter_after
        self.writes = []
        self.reads = 0

    def update_fields(self, asset_id, updates):
        if self.write_error:
            raise self.write_error
        self.writes.append((asset_id, updates))
        applied = {u["id"]: u["value"] for u in updates}
        if NEXTID_FIELD in applied:
            self.counter = int(applied[NEXTID_FIELD])
        if self.counter_after is not None:
            self.counter = self.counter_after

    def read_counter(self, asset_id, field_id):
        self.reads += 1
        if self.read_error:
            raise self.read_error
        return self.counter
