"""Shared startup for the tools/ scripts: import path plus .env.

Every tool loads the same `.env` the service does. Without this you would
have to export a dozen variables by hand before running a bring-up tool,
which is exactly when you least want friction -- and worse, a tool run with
half the environment set would read a DIFFERENT configuration from the
service it is meant to be diagnosing.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is a declared dependency
    pass
else:
    load_dotenv(REPO_ROOT / ".env")
