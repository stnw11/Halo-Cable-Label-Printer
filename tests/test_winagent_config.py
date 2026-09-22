"""Windows agent configuration and startup checks.

Exercised on whatever platform the suite runs on. The parts that genuinely
need Windows (printer enumeration) degrade to a warning rather than a
failure, which is what lets the pipeline be proven on a machine where the
Brady driver is not installed yet.
"""
import sys

import pytest

from winagent.src.agent import Agent, AgentConfigError, load_agent_config
from winagent.src.backends import PrintError, build_backend

GOOD = """
queue_root: '{root}'
printer_name: 'Brady Wraptor A6200'
backend: 'null'
"""


def write_cfg(tmp_path, body):
    path = tmp_path / "agent.yaml"
    path.write_text(body)
    return path


def test_loads_a_good_config(tmp_path):
    cfg = load_agent_config(write_cfg(tmp_path, GOOD.format(root=tmp_path / "q")))
    assert cfg["backend"] == "null"
    assert cfg["ledger_path"].name == "ledger.sqlite"
    assert cfg["log_path"].name == "agent.log"


def test_missing_file_says_what_to_copy(tmp_path):
    with pytest.raises(AgentConfigError, match="agent.example.yaml"):
        load_agent_config(tmp_path / "absent.yaml")


def test_malformed_yaml_names_the_file(tmp_path):
    with pytest.raises(AgentConfigError, match="malformed YAML"):
        load_agent_config(write_cfg(tmp_path, "queue_root: [unclosed\n"))


@pytest.mark.parametrize("missing", ["queue_root", "printer_name"])
def test_required_keys(tmp_path, missing):
    body = "\n".join(
        line for line in GOOD.format(root=tmp_path / "q").splitlines()
        if not line.strip().startswith(missing)
    )
    with pytest.raises(AgentConfigError, match=missing):
        load_agent_config(write_cfg(tmp_path, body))


def test_unknown_backend_is_rejected(tmp_path):
    body = GOOD.format(root=tmp_path / "q").replace("'null'", "'laserjet'")
    with pytest.raises(AgentConfigError, match="not one of"):
        load_agent_config(write_cfg(tmp_path, body))


def test_null_backend_starts_without_a_printer(tmp_path, caplog):
    """The case that matters right now: the Brady driver cannot be installed
    until the printer is on the network, but the whole pipeline should still
    be verifiable before then."""
    cfg = load_agent_config(write_cfg(tmp_path, GOOD.format(root=tmp_path / "q")))
    agent = Agent(cfg)
    with caplog.at_level("INFO"):
        agent.preflight()
    assert "skipping the printer visibility check" in caplog.text
    agent.ledger.close()


def test_the_brady_backend_is_still_unimplemented():
    backend = build_backend("brady", "Wraptor A6200")
    with pytest.raises(PrintError, match="not implemented"):
        backend.preflight()


def test_the_gdi_backend_names_what_it_needs_when_windows_is_missing():
    """It is the intended backend, but it can only run on the print host:
    off Windows the failure should name pywin32, not raise ImportError."""
    backend = build_backend("gdi", "Wraptor A6200")
    if sys.platform == "win32":  # pragma: no cover - not where the tests run
        backend.preflight()
        return
    with pytest.raises(PrintError, match="pywin32"):
        backend.preflight()


def test_the_strip_is_split_into_one_box_per_label():
    from winagent.src.backends import page_boxes

    assert page_boxes((300, 1500), 4) == [
        (0, 0, 300, 375), (0, 375, 300, 750), (0, 750, 300, 1125), (0, 1125, 300, 1500),
    ]


def test_a_strip_that_does_not_divide_evenly_is_refused():
    """The sidecar and the image disagreeing means one of them is wrong;
    printing 'about right' labels is worse than refusing."""
    from winagent.src.backends import page_boxes

    with pytest.raises(PrintError, match="does not divide"):
        page_boxes((300, 1000), 3)


# --- the ledger is touched from the watcher thread and the sweep --------------

def test_the_ledger_works_from_another_thread(tmp_path):
    """Regression: the agent's folder watcher runs its own thread, and
    sqlite3 refuses cross-thread use of a connection by default. Jobs still
    printed, via the slower periodic sweep, but every watcher event died."""
    import threading

    from winagent.src.ledger import Ledger

    ledger = Ledger(tmp_path / "ledger.sqlite")
    failures = []

    def from_thread():
        try:
            ledger.claim("job-from-another-thread", "stem")
            ledger.seen("job-from-another-thread")
            ledger.mark("job-from-another-thread", "done")
        except Exception as exc:  # pragma: no cover - the bug being pinned
            failures.append(exc)

    worker = threading.Thread(target=from_thread)
    worker.start()
    worker.join(10)
    assert not failures, failures
    assert ledger.seen("job-from-another-thread") == "done"
    ledger.close()


def test_only_one_thread_can_claim_a_job(tmp_path):
    """Two threads racing on the same job id: exactly one prints it."""
    import threading

    from winagent.src.ledger import Ledger

    ledger = Ledger(tmp_path / "ledger.sqlite")
    start = threading.Barrier(2)
    won = []

    def claim():
        start.wait(5)
        if ledger.claim("contested", "stem"):
            won.append(threading.get_ident())

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(won) == 1, "a job must be claimed exactly once"
    ledger.close()


# --- pacing jobs so the printer keeps up --------------------------------------

def _agent_with_interval(tmp_path, seconds):
    from winagent.src.agent import Agent
    from protocol import ensure_queue

    queue = ensure_queue(tmp_path / "queue")
    return Agent({
        "queue_root": queue, "printer_name": "test", "backend": "null",
        "sidecar_grace_seconds": 1, "print_timeout_seconds": 10, "retain_done_days": 30,
        "poll_seconds": 1, "ledger_path": tmp_path / "ledger.sqlite",
        "log_path": tmp_path / "agent.log", "log_level": "INFO",
        "job_interval_seconds": seconds,
    })


def test_the_first_job_of_a_run_is_not_delayed(tmp_path, monkeypatch):
    agent = _agent_with_interval(tmp_path, 60)
    slept = []
    monkeypatch.setattr("winagent.src.agent.time.sleep", slept.append)
    agent._wait_for_the_printer()
    assert slept == [], "nothing has been printed yet, so there is nothing to wait for"


def test_a_later_job_waits_out_the_rest_of_the_interval(tmp_path, monkeypatch):
    """The printer drops a job that arrives while it is still ingesting the
    last one, and reports nothing. Measured on a Wraptor A6200."""
    import time as time_module

    agent = _agent_with_interval(tmp_path, 60)
    slept = []
    monkeypatch.setattr("winagent.src.agent.time.sleep", slept.append)
    agent._last_print_finished = time_module.monotonic() - 10   # 10s ago
    agent._wait_for_the_printer()
    assert len(slept) == 1 and 49 < slept[0] <= 50, slept


def test_no_waiting_once_the_interval_has_passed(tmp_path, monkeypatch):
    import time as time_module

    agent = _agent_with_interval(tmp_path, 60)
    slept = []
    monkeypatch.setattr("winagent.src.agent.time.sleep", slept.append)
    agent._last_print_finished = time_module.monotonic() - 120
    agent._wait_for_the_printer()
    assert slept == []


def test_the_pause_can_be_turned_off(tmp_path, monkeypatch):
    import time as time_module

    agent = _agent_with_interval(tmp_path, 0)
    slept = []
    monkeypatch.setattr("winagent.src.agent.time.sleep", slept.append)
    agent._last_print_finished = time_module.monotonic()
    agent._wait_for_the_printer()
    assert slept == []
