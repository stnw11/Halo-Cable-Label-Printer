"""Guard against site-specific values reaching a public repo.

Spec section 8 says no organization name, field id, hostname, share path,
printer name, or prefix may appear in any committed file. That was a manual
`grep` checklist item. This makes it automatic.

The trick that makes this work for ANY adopter, without this file itself
containing a single real value: it reads the LOCAL, gitignored configuration
and compares each value against the committed `.example` template. A value
that matches the template is a shipped default and is fine. A value that
DIFFERS is site-specific by definition, and must not appear in anything git
would commit.

On a fresh clone with no local config, these tests skip rather than fail.
"""
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent

# Values too short or too generic to be meaningful evidence of a leak.
# A three-digit field id is checked contextually instead (see below).
MIN_DISTINCTIVE_LENGTH = 6

# Keys whose value is allowed to differ from the example without being
# treated as a leak candidate: these hold no site-specific data.
SKIP_KEYS = {"LOG_LEVEL", "DISPLAY_TIMEZONE", "LOCK_PATH", "CONFIG_DIR"}

# Keys whose value IDENTIFIES a site: field ids, tenant URLs, hostnames,
# share names, printer names, paths. Only these get the aggressive
# short-number check, because only these are worth a false positive.
#
# Operational tuning (SHADOW_MODE, POLL_INTERVAL_SECONDS, retries, caps) is
# deliberately excluded. Those are small integers whose values collide with
# every literal in the codebase -- an early version of this test flagged
# `SHADOW_MODE=1` against every `= 1` in the tree, which is exactly the kind
# of noise that gets a guard disabled rather than fixed.
SENSITIVE_KEY = re.compile(
    r"(GROUP_ID|CLIENT_ID|CLIENT_SECRET|_URL|_HOST|_SHARE|_USER|_PASS"
    r"|QUEUE_ROOT|printer_name|queue_root|ledger_path|log_path|prefix"
    r"|^fields\.\w+\.id$)",
    re.IGNORECASE,
)


def committed_files() -> list[Path]:
    """Everything git would include: tracked files plus untracked files that
    are not ignored. Exactly the set that would land in a public repo."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.split("\n")
    paths = []
    for name in out:
        if not name:
            continue
        path = REPO / name
        if path.is_file() and path.suffix not in (".png", ".pdf", ".ico", ".zip"):
            paths.append(path)
    return paths


def read_env(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        # Strip an inline comment the way python-dotenv and compose do, or
        # `ID=42  # cable types` hides the 42 from the checks below.
        value = re.split(r"\s+#", value, maxsplit=1)[0]
        values[key.strip()] = value.strip().strip("'\"")
    return values


def flatten_yaml(data, prefix="") -> dict:
    out = {}
    if isinstance(data, dict):
        for key, value in data.items():
            out.update(flatten_yaml(value, f"{prefix}.{key}" if prefix else str(key)))
    elif data is not None:
        out[prefix] = str(data)
    return out


def read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return flatten_yaml(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    except yaml.YAMLError:
        return {}


def site_specific_values() -> dict[str, tuple[str, str]]:
    """{value: (source file, key)} for every local value that differs from
    its committed template."""
    candidates: dict[str, tuple[str, str]] = {}

    pairs = [(REPO / ".env", REPO / ".env.example")]
    for name in ("printers", "layout", "fields"):
        pairs.append((REPO / "config" / f"{name}.yaml", REPO / "config" / f"{name}.example.yaml"))
    pairs.append((
        REPO / "winagent" / "config" / "agent.yaml",
        REPO / "winagent" / "config" / "agent.example.yaml",
    ))

    for live, example in pairs:
        if not live.exists():
            continue
        reader = read_env if live.suffix == "" or live.name == ".env" else read_yaml
        live_values, example_values = reader(live), reader(example)
        for key, value in live_values.items():
            if key in SKIP_KEYS or not value:
                continue
            if example_values.get(key) == value:
                continue          # a shipped default, not site-specific
            candidates[value] = (live.relative_to(REPO).as_posix(), key)
    return candidates


@pytest.fixture(scope="module")
def local_values():
    values = site_specific_values()
    if not values:
        pytest.skip("no local configuration present (fresh clone) -- nothing to check")
    return values


def test_no_site_specific_value_appears_in_a_committed_file(local_values):
    """The headline check. If this fails, a real deployment value is about
    to be published."""
    files = committed_files()
    leaks = []

    for value, (source, key) in local_values.items():
        if len(value) < MIN_DISTINCTIVE_LENGTH:
            continue
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if value in text:
                leaks.append(f"{path.relative_to(REPO)} contains {source}'s {key}")

    assert not leaks, "site-specific values found in committed files:\n  " + "\n  ".join(leaks)


def test_short_numeric_settings_do_not_appear_as_assignments(local_values):
    """Halo field ids are short numbers, so a bare substring search would
    false-positive constantly (an id like 902 appears inside 1902, a byte
    count, a line number). Check for them in ASSIGNMENT position instead:
    `SOMETHING=<id>` or `something: <id>`.

    An earlier version also required the line to mention the setting's own
    key, which sounded careful and was in fact a hole: a planted
    `DEFAULT_STATUS_FIELD_ID = <id>` sailed through, because the key is
    `HALO_CABLE_STATUS_FIELD_ID` and the names did not match. Being strict
    beats being clever here.

    This file deliberately uses no real id, not even as an example. It used
    to, in this very docstring, and the exemption below -- "test files
    discuss the shape of the problem" -- is what let two real field ids
    reach a repo headed for publication. The exemption is gone: fixtures
    invent their own ids (conftest uses 811-814), so nothing needs it.
    """
    files = committed_files()
    leaks = []

    for value, (source, key) in local_values.items():
        if not value.isdigit() or len(value) >= MIN_DISTINCTIVE_LENGTH:
            continue
        if not SENSITIVE_KEY.search(key):
            continue
        pattern = re.compile(rf"(?:=|:)\s*['\"]?{re.escape(value)}\b")
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    leaks.append(
                        f"{path.relative_to(REPO)}:{line_no} assigns {value!r}, "
                        f"which is {source}'s {key}"
                    )

    assert not leaks, "site-specific ids found in committed files:\n  " + "\n  ".join(leaks)


def test_env_example_ships_every_secret_and_id_blank():
    """A template with a value filled in is how a real value gets committed
    by accident."""
    example = read_env(REPO / ".env.example")
    assert example, ".env.example is missing"
    must_be_blank = [
        key for key in example
        if key.endswith(("_ID", "_SECRET", "_PASS", "_USER", "_HOST", "_SHARE"))
    ]
    assert must_be_blank, "expected .env.example to define the secret/id keys"
    # The one exception: the share name install.ps1 creates by default. It
    # is the project's own name, not a site's, and shipping it documents it.
    shipped_defaults = {"SMB_SHARE": "CableLabelQueue"}
    filled = {k: example[k] for k in must_be_blank if example[k] and example[k] != shipped_defaults.get(k)}
    assert not filled, f".env.example ships non-blank values: {filled}"


def test_the_gitignore_actually_covers_the_local_config():
    """Belt and braces: the files this test reads must themselves be
    excluded, or the check above is moot."""
    should_be_ignored = [
        ".env",
        "config/printers.yaml",
        "config/layout.yaml",
        "config/fields.yaml",
        "winagent/config/agent.yaml",
        "halo-cable-label-print-spec.md",
    ]
    result = subprocess.run(
        ["git", "check-ignore"] + should_be_ignored,
        cwd=REPO, capture_output=True, text=True,
    )
    ignored = set(result.stdout.split("\n"))
    missing = [p for p in should_be_ignored if p not in ignored]
    assert not missing, f".gitignore does not cover: {missing}"


def test_example_configs_are_committed():
    """The flip side: an adopter must actually get the templates."""
    files = {p.relative_to(REPO).as_posix() for p in committed_files()}
    for required in (
        ".env.example",
        "config/printers.example.yaml",
        "config/layout.example.yaml",
        "config/fields.example.yaml",
        "winagent/config/agent.example.yaml",
    ):
        assert required in files, f"{required} must be committed"
