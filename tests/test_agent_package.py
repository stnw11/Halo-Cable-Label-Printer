"""The Windows agent package is self-contained and runs on its own.

The package is copied to a print host that has no repo checkout, so the
proof that matters is running the packaged agent from the package folder,
with nothing else importable.
"""
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from protocol import write_job

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def package(tmp_path_factory):
    out = tmp_path_factory.mktemp("pkg")
    subprocess.run(
        [sys.executable, str(REPO / "tools" / "package_agent.py"), "--out", str(out)],
        check=True, capture_output=True, text=True,
    )
    return out / "HaloCableLabelAgent", out / "HaloCableLabelAgent.zip"


def test_package_has_the_installer_and_updater_where_the_scripts_expect(package):
    """update.ps1 resolves install.ps1 by relative path, so the published
    layout is part of the contract, not an accident of the builder."""
    folder, _ = package
    assert (folder / "install.ps1").is_file()
    assert (folder / "update.cmd").is_file()
    assert (folder / "winagent" / "update.ps1").is_file()
    # ..\install.ps1 from winagent\update.ps1 is what update.ps1 looks for first.
    assert ((folder / "winagent" / ".." / "install.ps1").resolve()).is_file()
    assert (folder / "INSTALL.txt").read_bytes().count(b"\r\n") > 5   # Notepad-friendly


def test_package_carries_no_live_config_or_build_debris(package):
    folder, _ = package
    names = {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}
    assert "winagent/config/agent.yaml" not in names
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    assert not [n for n in names if n.startswith(("src/", "tools/", "tests/", "config/"))]


def test_zip_matches_the_folder(package):
    folder, archive = package
    in_folder = {f"HaloCableLabelAgent/{p.relative_to(folder).as_posix()}" for p in folder.rglob("*") if p.is_file()}
    with zipfile.ZipFile(archive) as zf:
        assert set(zf.namelist()) == in_folder


def test_packaged_agent_processes_a_job_on_its_own(package, tmp_path, sidecar, payload):
    """Run the agent from the package folder with a clean environment and
    the package as the working directory, so an import that only resolves
    inside the repo fails here rather than on the print host."""
    folder, _ = package
    queue = tmp_path / "queue"
    config = tmp_path / "agent.yaml"
    config.write_text(f"queue_root: '{queue}'\nprinter_name: 'Test'\nbackend: 'null'\n")
    from protocol import ensure_queue
    ensure_queue(queue)
    write_job(queue, sidecar, payload)

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    run = subprocess.run(
        [sys.executable, "-I", str(folder / "winagent" / "src" / "agent.py"), "--config", str(config), "--once"],
        cwd=folder, env=env, capture_output=True, text=True, timeout=60,
    )
    assert run.returncode == 0, run.stderr
    results = list((queue / "results").glob("*.json"))
    assert len(results) == 1
    assert json.loads(results[0].read_text())["status"] == "success"
