#!/usr/bin/env python3
"""Build the Windows print agent package: one folder, plus a zip of it.

The package is everything the print host needs and nothing else: the agent,
the queue protocol it shares with the Docker side, the install scripts, and
INSTALL.txt. Copy it to the print host and run install.ps1.

    tools/package_agent.py                 # -> out/HaloCableLabelAgent/ and .zip

No live config goes in: agent.yaml is created by install.ps1 on the print
host, from agent.example.yaml.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

import _bootstrap  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "HaloCableLabelAgent"

# (source in the repo, path in the package)
FILES = [
    ("winagent/install.ps1", "install.ps1"),
    ("winagent/update.cmd", "update.cmd"),
    ("winagent/update.ps1", "winagent/update.ps1"),
    ("winagent/INSTALL.txt", "INSTALL.txt"),
    ("protocol/__init__.py", "protocol/__init__.py"),
    ("protocol/queue.py", "protocol/queue.py"),
    ("protocol/job_schema.json", "protocol/job_schema.json"),
    ("winagent/README.md", "winagent/README.md"),
    ("winagent/requirements.txt", "winagent/requirements.txt"),
    ("winagent/install-service.ps1", "winagent/install-service.ps1"),
    ("winagent/check-windows-host.ps1", "winagent/check-windows-host.ps1"),
    ("winagent/config/agent.example.yaml", "winagent/config/agent.example.yaml"),
    ("winagent/src/__init__.py", "winagent/src/__init__.py"),
    ("winagent/src/agent.py", "winagent/src/agent.py"),
    ("winagent/src/backends.py", "winagent/src/backends.py"),
    ("winagent/src/ledger.py", "winagent/src/ledger.py"),
]

# Opened in Notepad on the print host, so given Windows line endings.
CRLF_SUFFIXES = (".txt",)


def build(out_dir: Path) -> tuple[Path, Path]:
    """Build the package under out_dir. Returns (folder, zip)."""
    folder = out_dir / PACKAGE_NAME
    if folder.exists():
        shutil.rmtree(folder)
    for source, target in FILES:
        src, dst = REPO_ROOT / source, folder / target
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.suffix in CRLF_SUFFIXES:
            text = src.read_text(encoding="utf-8").replace("\r\n", "\n")
            dst.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
        else:
            shutil.copyfile(src, dst)

    archive = out_dir / f"{PACKAGE_NAME}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                zf.write(path, Path(PACKAGE_NAME) / path.relative_to(folder))
    return folder, archive


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(REPO_ROOT / "out"), help="output directory (default: out/)")
    args = parser.parse_args(argv)
    folder, archive = build(Path(args.out))
    print(f"package: {folder}")
    print(f"zip:     {archive}")
    print("Copy either to the print host and run install.ps1 as administrator (see INSTALL.txt).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
