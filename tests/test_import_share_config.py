"""Loading install.ps1's queue-share.env into .env."""
import pytest

from tools.import_share_config import (
    ShareFileError,
    merge,
    parse_share_file,
    password_needed,
    quote_password,
    read_env_values,
)

# As install.ps1 writes it: Windows line endings, a comment header.
SHARE_FILE = (
    "# Written by install.ps1 on PRINTHOST, 2026-09-18 21:00.\r\n"
    "# Copy this file to the Docker host and run:\r\n"
    "SMB_HOST=printhost.example.lan\r\n"
    "SMB_SHARE=CableLabelQueue\r\n"
    "SMB_USER=svc-cablelabel\r\n"
    "SMB_DOMAIN=EXAMPLE\r\n"
)

ENV = """# --- The print queue ---
QUEUE_ROOT=/mnt/cable-queue
# The queue share on the Windows print host.
SMB_HOST=
SMB_SHARE=
SMB_USER=
SMB_PASS=
SMB_DOMAIN=
LOG_LEVEL=INFO
"""


def test_share_file_is_parsed_ignoring_comments_and_crlf():
    assert parse_share_file(SHARE_FILE) == {
        "SMB_HOST": "printhost.example.lan",
        "SMB_SHARE": "CableLabelQueue",
        "SMB_USER": "svc-cablelabel",
        "SMB_DOMAIN": "EXAMPLE",
    }


def test_a_local_account_has_an_empty_domain():
    text = SHARE_FILE.replace("SMB_DOMAIN=EXAMPLE", "SMB_DOMAIN=")
    assert parse_share_file(text)["SMB_DOMAIN"] == ""


def test_a_share_file_carrying_a_password_is_refused():
    with pytest.raises(ShareFileError, match="Passwords do not belong"):
        parse_share_file(SHARE_FILE + "SMB_PASS=secret\r\n")


def test_a_file_that_is_not_a_share_file_is_refused():
    with pytest.raises(ShareFileError, match="missing SMB_HOST"):
        parse_share_file("LOG_LEVEL=INFO\n")


def test_merge_updates_lines_in_place_and_keeps_everything_else():
    new, changed = merge(ENV, parse_share_file(SHARE_FILE), "pw")
    lines = new.split("\n")
    assert lines[:3] == ENV.split("\n")[:3]                 # untouched lines and comments
    assert "SMB_HOST=printhost.example.lan" in lines
    assert lines.index("SMB_HOST=printhost.example.lan") == ENV.split("\n").index("SMB_HOST=")
    assert "LOG_LEVEL=INFO" in lines
    assert read_env_values(new)["SMB_PASS"] == "'pw'"
    assert set(changed) == {"SMB_HOST", "SMB_SHARE", "SMB_USER", "SMB_DOMAIN", "SMB_PASS"}


def test_merge_without_a_password_leaves_the_stored_one():
    env = ENV.replace("SMB_PASS=", "SMB_PASS='kept'")
    new, changed = merge(env, parse_share_file(SHARE_FILE), None)
    assert read_env_values(new)["SMB_PASS"] == "'kept'"
    assert "SMB_PASS" not in changed


def test_missing_keys_are_appended():
    new, _ = merge("LOG_LEVEL=INFO\n", parse_share_file(SHARE_FILE), "pw")
    assert new.startswith("LOG_LEVEL=INFO\n")
    assert new.endswith("SMB_DOMAIN=EXAMPLE\nSMB_PASS='pw'\n")


def test_running_it_twice_changes_nothing_the_second_time():
    once, _ = merge(ENV, parse_share_file(SHARE_FILE), "pw")
    twice, changed = merge(once, parse_share_file(SHARE_FILE), None)
    assert twice == once
    assert changed == []


def test_windows_line_endings_in_env_are_kept():
    new, _ = merge(ENV.replace("\n", "\r\n"), parse_share_file(SHARE_FILE), "pw")
    assert "\r\n" in new and "\n" not in new.replace("\r\n", "")


def test_a_password_is_needed_when_none_is_stored_or_the_account_changed():
    share = parse_share_file(SHARE_FILE)
    assert password_needed(ENV, share)
    stored, _ = merge(ENV, share, "pw")
    assert not password_needed(stored, share)
    other = dict(share, SMB_USER="someone-else")
    assert password_needed(stored, other)


@pytest.mark.parametrize("password,expected", [
    ("plain", "'plain'"),
    ("has$dollar#hash", "'has$dollar#hash'"),        # single quotes are literal in Compose
    ("it's", '"it\'s"'),
    ('mix\'"$\\', '"mix\'\\"$$\\\\"'),                   # double quotes: escape \ " and $
])
def test_passwords_are_quoted_so_compose_reads_them_back_exactly(password, expected):
    assert quote_password(password) == expected


def test_a_password_with_a_comma_is_refused():
    with pytest.raises(ShareFileError, match="comma"):
        quote_password("a,b")


def test_a_name_that_will_not_resolve_is_detected(monkeypatch):
    """Docker resolves SMB_HOST when it mounts the share, so a name this
    machine cannot look up fails the mount, not the import."""
    from tools import import_share_config as mod

    assert mod.host_resolves("localhost")
    monkeypatch.setattr(mod.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert not mod.host_resolves("printhost.example.lan")
