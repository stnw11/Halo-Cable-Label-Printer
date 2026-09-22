"""Allocator tests -- the heart of the suite (spec 5.6, criteria 1 to 9).

No Halo, no share, no printer. If these pass, the numbering is correct;
everything downstream can only lose a job, never duplicate an identifier.
"""
from dataclasses import replace

import pytest

from src.allocator import recovery_hint, reserve, validate
from src.errors import ReserveError, ValidationError
from src import guards
from src.guards import apply_batch_cap, veto_duplicate_prefixes
from src.layout import LabelStyle, Media
from src.models import CableType
from tests.conftest import NEXTID_FIELD, QTY_FIELD, STATUS_FIELD, FakeHalo


# --- validate: the checks that cost nothing ---------------------------------

def test_plan_spans_the_requested_block(cable_type, app_config):
    plan = validate(cable_type, app_config)
    assert (plan.first_number, plan.last_number) == (100, 111)
    assert plan.cable_count == 12
    assert plan.next_counter_value == 112


def test_quantity_counts_cables_and_labels_double(cable_type, app_config):
    """Criterion 2. The 2N doubling is the thing most likely to be got
    backwards, so it is asserted directly rather than implied."""
    plan = validate(cable_type, app_config)
    assert plan.cable_count == 12
    assert plan.label_count == 24
    assert len(plan.numbers) == 12


def test_single_cable_consumes_one_number(cable_type, app_config):
    """Criterion 1."""
    plan = validate(replace(cable_type, qty=1), app_config)
    assert plan.first_number == plan.last_number == 100
    assert plan.next_counter_value == 101
    assert plan.label_count == 2


@pytest.mark.parametrize("prefix", ["", "   ", None])
def test_blank_prefix_is_refused(cable_type, app_config, prefix):
    """Criterion 7."""
    with pytest.raises(ValidationError, match="prefix"):
        validate(replace(cable_type, prefix=prefix), app_config)


@pytest.mark.parametrize("prefix", ["BL-X", "TOOLONGPREFIX", "B L", "../etc", "BL_1"])
def test_malformed_prefix_is_refused(cable_type, app_config, prefix):
    with pytest.raises(ValidationError, match="prefix_pattern"):
        validate(replace(cable_type, prefix=prefix), app_config)


@pytest.mark.parametrize("counter", [0, -1, -999])
def test_non_positive_counter_is_refused(cable_type, app_config, counter):
    """Criterion 7. Never fall back to 1 -- that reissues live numbers."""
    with pytest.raises(ValidationError, match="positive integer"):
        validate(replace(cable_type, next_id=counter), app_config)


@pytest.mark.parametrize("counter", ["", "abc", None, 1.5, True])
def test_non_numeric_counter_is_refused(cable_type, app_config, counter):
    with pytest.raises(ValidationError, match="non-numeric"):
        validate(replace(cable_type, next_id=counter), app_config)


@pytest.mark.parametrize("qty", [0, -3])
def test_non_positive_quantity_is_refused(cable_type, app_config, qty):
    with pytest.raises(ValidationError, match="quantity"):
        validate(replace(cable_type, qty=qty), app_config)


def test_oversized_request_is_clamped(cable_type, app_config, monkeypatch):
    """Criterion 3. Only the batch is reserved, so the counter never
    advances past numbers that were never printed."""
    monkeypatch.setattr(guards, "BATCH_SIZE", 100)
    plan = validate(replace(cable_type, qty=500), app_config)
    assert plan.cable_count == 100
    assert plan.requested_qty == 500
    assert plan.clamped is True
    assert plan.next_counter_value == 200          # 100 + 100, not 100 + 500


def test_block_too_wide_for_the_media_is_refused_before_reserving(cable_type, app_config):
    """The pre-reserve fit check. A block whose LAST identifier would not fit
    is refused whole, rather than printing until it clips."""
    narrow = replace(app_config, media=Media(print_area_width_in=0.25, label_width_in=0.5))
    with pytest.raises(ValidationError, match="cannot be laid out"):
        validate(cable_type, narrow)


def test_fit_check_uses_the_widest_number_in_the_block(app_config):
    """A run that crosses into an extra digit must be judged on its widest
    member, not its first -- otherwise the tail of the run clips.

    The geometry here is chosen so a 5-character identifier clears the 5pt
    floor and a 6-character one does not, which is exactly the boundary the
    check exists to catch.
    """
    cfg = replace(
        app_config,
        style=LabelStyle(pad_width=2, legend_repeat=1),
        media=Media(print_area_width_in=0.28, label_width_in=1.0),
    )
    ok = CableType(id=1, prefix="BL", next_id=1, qty=2)          # BL-01..BL-02
    assert validate(ok, cfg).last_number == 2

    spanning = CableType(id=1, prefix="BL", next_id=98, qty=20)  # runs to BL-117
    with pytest.raises(ValidationError, match="cannot be laid out"):
        validate(spanning, cfg)


def test_numbers_past_pad_width_widen_rather_than_truncate(app_config, caplog):
    """Never truncate and never wrap to zero: either would put two cables
    under one label."""
    asset = CableType(id=1, prefix="BL", next_id=9999, qty=3)
    with caplog.at_level("WARNING"):
        plan = validate(asset, app_config)
    assert plan.last_number == 10001
    assert "exceeds pad_width" in caplog.text


# --- reserve: the write that consumes numbers --------------------------------

def test_reserve_advances_the_counter_by_exactly_the_block(cable_type, app_config):
    """Criterion 1 and 2."""
    halo = FakeHalo(counter=100)
    plan = validate(cable_type, app_config)
    reservation = reserve(halo, cable_type, plan, app_config)
    assert halo.counter == 112
    assert reservation.identifiers[0] == "BL-0100"
    assert reservation.identifiers[-1] == "BL-0111"
    assert len(reservation.identifiers) == 12


def test_reserve_sends_all_three_updates_in_one_call(cable_type, app_config):
    """Spec 4.2. Two calls would open a window where the counter has moved
    but the trigger has not, or the reverse."""
    halo = FakeHalo(counter=100)
    plan = validate(cable_type, app_config)
    reserve(halo, cable_type, plan, app_config)

    assert len(halo.writes) == 1, "the reserve must be a single write"
    asset_id, updates = halo.writes[0]
    assert asset_id == 4711
    sent = {u["id"]: u["value"] for u in updates}
    assert sent[NEXTID_FIELD] == "112"
    assert sent[QTY_FIELD] == "0"
    assert sent[STATUS_FIELD].startswith("QUEUED BL-0100..BL-0111")


def test_reserve_status_line_carries_the_range_and_counts(cable_type, app_config):
    halo = FakeHalo(counter=100)
    plan = validate(cable_type, app_config)
    reserve(halo, cable_type, plan, app_config)
    line = {u["id"]: u["value"] for u in halo.writes[0][1]}[STATUS_FIELD]
    assert "12 cables, 24 labels" in line
    assert len(line) <= app_config.status_field_max_chars


def test_label_sequence_is_adjacent_pairs(cable_type, app_config):
    """Spec 5.4. The operator takes two in a row for the two ends of one
    cable; interleaving would force them to run the batch twice."""
    halo = FakeHalo(counter=100)
    plan = validate(replace(cable_type, qty=3), app_config)
    reservation = reserve(halo, cable_type, plan, app_config)
    assert reservation.label_sequence == [
        "BL-0100", "BL-0100", "BL-0101", "BL-0101", "BL-0102", "BL-0102",
    ]


def test_write_failure_consumes_nothing(cable_type, app_config):
    """Criterion 4's sibling: a failed write must leave the counter alone so
    the request is simply retried."""
    halo = FakeHalo(counter=100, write_error=RuntimeError("503 from Halo"))
    plan = validate(cable_type, app_config)
    with pytest.raises(ReserveError, match="No numbers consumed"):
        reserve(halo, cable_type, plan, app_config)
    assert halo.counter == 100
    assert halo.writes == []


def test_failed_read_back_aborts_without_rendering(cable_type, app_config):
    """The write may or may not have applied, so the only safe move is to
    stop and say so -- never to render on an unverified counter."""
    halo = FakeHalo(counter=100, read_error=RuntimeError("timeout"))
    plan = validate(cable_type, app_config)
    with pytest.raises(ReserveError, match="read-back failed"):
        reserve(halo, cable_type, plan, app_config)


def test_read_back_mismatch_aborts(cable_type, app_config):
    """Something else is writing the counter. Refuse rather than guess."""
    halo = FakeHalo(counter=100, counter_after=999)
    plan = validate(cable_type, app_config)
    with pytest.raises(ReserveError, match="read-back mismatch"):
        reserve(halo, cable_type, plan, app_config)


def test_reserve_reads_back_exactly_once(cable_type, app_config):
    halo = FakeHalo(counter=100)
    plan = validate(cable_type, app_config)
    reserve(halo, cable_type, plan, app_config)
    assert halo.reads == 1


def test_two_sequential_requests_never_overlap(cable_type, app_config):
    """The core guarantee, exercised end to end against one counter."""
    halo = FakeHalo(counter=100)
    first = reserve(halo, cable_type, validate(cable_type, app_config), app_config)
    second_asset = replace(cable_type, next_id=halo.counter, qty=5)
    second = reserve(halo, second_asset, validate(second_asset, app_config), app_config)
    assert set(first.identifiers).isdisjoint(second.identifiers)
    assert second.identifiers[0] == "BL-0112"


# --- guards ------------------------------------------------------------------

def test_duplicate_prefixes_reserve_nothing(caplog):
    """Criterion 6. Two counters issuing one prefix is the collision this
    project exists to prevent, so NEITHER asset is processed."""
    a = CableType(id=1, prefix="BL", next_id=100, qty=1)
    b = CableType(id=2, prefix="BL", next_id=500, qty=1)
    c = CableType(id=3, prefix="YE", next_id=1, qty=1)
    with caplog.at_level("ERROR"):
        survivors = veto_duplicate_prefixes([a, b, c])
    assert survivors == [c]
    assert "1, 2" in caplog.text


def test_duplicate_prefix_detection_ignores_case_and_padding():
    a = CableType(id=1, prefix="bl", next_id=1, qty=1)
    b = CableType(id=2, prefix=" BL ", next_id=1, qty=1)
    assert veto_duplicate_prefixes([a, b]) == []


def test_batch_cap_defers_rather_than_drops():
    assets = [CableType(id=i, prefix=f"P{i}", next_id=1, qty=1) for i in range(5)]
    capped = apply_batch_cap(assets, 2)
    assert len(capped) == 2
    assert capped == assets[:2]


# --- recovery ----------------------------------------------------------------

def test_recovery_hint_is_copy_pasteable(cable_type, app_config):
    """The log line IS the user interface for a burned block, so it carries
    the command rather than describing one."""
    halo = FakeHalo(counter=100)
    reservation = reserve(halo, cable_type, validate(cable_type, app_config), app_config)
    hint = recovery_hint(reservation, "share unreachable")
    assert "--prefix BL --from 100 --to 111 --asset-id 4711" in hint
    assert "will not be reissued" in hint


def test_the_remainder_of_a_big_request_is_written_back(cable_type, app_config, monkeypatch):
    """A request larger than one batch must not be thrown away: the trigger
    keeps what is left so the next poll continues it. This is the bug a
    user hit -- 1000 cables requested, 100 printed, 900 silently dropped."""
    monkeypatch.setattr(guards, "BATCH_SIZE", 100)
    asset = replace(cable_type, qty=500)
    halo = FakeHalo(counter=cable_type.next_id)

    reserve(halo, asset, validate(asset, app_config), app_config)

    _, updates = halo.writes[-1]
    written = {u["id"]: u["value"] for u in updates}
    assert written[QTY_FIELD] == "400", "the untaken cables stay on the asset"
    assert written[NEXTID_FIELD] == "200", "the counter advances by the batch only"
    assert "100 of 500 cables" in written[STATUS_FIELD]
    assert "400 still to print" in written[STATUS_FIELD]


def test_a_request_within_one_batch_clears_the_trigger(cable_type, app_config):
    halo = FakeHalo(counter=cable_type.next_id)
    reserve(halo, cable_type, validate(cable_type, app_config), app_config)
    _, updates = halo.writes[-1]
    assert {u["id"]: u["value"] for u in updates}[QTY_FIELD] == "0"
