#!/usr/bin/env python3
"""Load the queue share settings written by install.ps1 into .env.

install.ps1 on the print host writes queue-share.env: the SMB_HOST,
SMB_SHARE, SMB_USER and SMB_DOMAIN the Docker side needs, but never the
password. Copy that file here and run:

    python tools/import_share_config.py queue-share.env

It updates those four lines in .env in place, keeps every other line, and
asks for the share account's password when .env has none yet or the account
changed. It never shows or writes the password anywhere but .env.
"""
from __future__ import annotations

import argparse
import getpass
import re
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SHARE_KEYS = ("SMB_HOST", "SMB_SHARE", "SMB_USER", "SMB_DOMAIN")
VOLUME = "halo-cable-label-printer_cable-queue"
_LINE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)\s*=(.*)$")


class ShareFileError(ValueError):
    """A problem the person running the tool has to fix."""


def parse_share_file(text: str) -> dict[str, str]:
    """The SMB_* values from a queue-share.env. Comments, blank lines and any
    other key are ignored; a password, if someone added one, is refused."""
    values = {}
    for line in text.replace("\r\n", "\n").split("\n"):
        match = _LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if key == "SMB_PASS":
            raise ShareFileError(
                "the share file contains SMB_PASS. Passwords do not belong in that file; "
                "remove the line and let this tool ask for it."
            )
        if key in SHARE_KEYS:
            values[key] = value
    missing = [k for k in ("SMB_HOST", "SMB_SHARE", "SMB_USER") if not values.get(k)]
    if missing:
        raise ShareFileError(f"the share file is missing {', '.join(missing)} -- is it the file install.ps1 wrote?")
    values.setdefault("SMB_DOMAIN", "")
    return values


def read_env_values(text: str) -> dict[str, str]:
    values = {}
    for line in text.split("\n"):
        match = _LINE.match(line)
        if match:
            values[match.group(1)] = match.group(2).strip()
    return values


def quote_password(password: str) -> str:
    """The password as a .env value that Docker Compose reads back exactly.

    Single quotes are literal in a Compose .env, so `$` and `#` need no
    escaping there. A password containing a single quote goes in double
    quotes instead, where `$` must be doubled and `\\` and `"` escaped.
    A comma can never work: the password is part of a comma-separated
    list of mount options.
    """
    if not password:
        raise ShareFileError("the password is empty")
    if "," in password:
        raise ShareFileError(
            "the password contains a comma, which cannot be passed to the share mount "
            "(its options are comma-separated). Change the share account's password."
        )
    if any(c in password for c in "\r\n"):
        raise ShareFileError("the password contains a line break")
    if "'" not in password:
        return f"'{password}'"
    escaped = password.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
    return f'"{escaped}"'


def merge(env_text: str, share: dict[str, str], password: str | None) -> tuple[str, list[str]]:
    """Return .env with the share values (and the password, if given) set.

    Existing lines are replaced where they are, so comments and ordering
    survive; keys .env does not have yet are appended. Returns the new text
    and the keys whose value changed.
    """
    wanted = dict(share)
    if password is not None:
        wanted["SMB_PASS"] = quote_password(password)
    current = read_env_values(env_text)
    changed = [k for k, v in wanted.items() if current.get(k) != v]

    newline = "\r\n" if "\r\n" in env_text else "\n"
    lines = env_text.replace("\r\n", "\n").split("\n")
    seen = set()
    for i, line in enumerate(lines):
        match = _LINE.match(line)
        if match and match.group(1) in wanted and match.group(1) not in seen:
            key = match.group(1)
            lines[i] = f"{key}={wanted[key]}"
            seen.add(key)
    missing = [k for k in wanted if k not in seen]
    if missing:
        if lines and lines[-1] == "":
            lines.pop()
        lines += [f"{k}={wanted[k]}" for k in missing] + [""]
    return newline.join(lines), changed


def password_needed(env_text: str, share: dict[str, str]) -> bool:
    """A password is needed when .env has none, or the account changed and
    the stored password therefore belongs to a different account."""
    current = read_env_values(env_text)
    if not current.get("SMB_PASS"):
        return True
    return any(current.get(k, "") != share[k] for k in ("SMB_USER", "SMB_DOMAIN"))


def _ask_password(account: str) -> str:
    if not sys.stdin.isatty():
        raise ShareFileError("a password is needed but this is not an interactive terminal -- run it in one")
    first = getpass.getpass(f"Password for {account}: ")
    if getpass.getpass("Again, to confirm: ") != first:
        raise ShareFileError("the two passwords did not match; nothing was changed")
    return first


def host_resolves(host: str) -> bool:
    """Docker resolves the share's host itself when it mounts the volume, so
    a name this machine cannot look up fails the mount with a DNS error
    rather than anything about the share. An IP address always works."""
    try:
        socket.getaddrinfo(host, None)
        return True
    except OSError:
        return False


def _volume_exists() -> bool:
    try:
        return subprocess.run(
            ["docker", "volume", "inspect", VOLUME], capture_output=True, timeout=15
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("share_file", help="the queue-share.env written by install.ps1")
    parser.add_argument("--env", default=str(REPO_ROOT / ".env"), help="the .env to update (default: the project's)")
    parser.add_argument("--new-password", action="store_true", help="ask for the password even if .env has one")
    args = parser.parse_args(argv)

    env_path = Path(args.env)
    try:
        share = parse_share_file(Path(args.share_file).read_text(encoding="utf-8-sig"))
        if not env_path.exists():
            raise ShareFileError(f"{env_path} not found -- copy .env.example to .env first")
        env_text = env_path.read_text(encoding="utf-8")
        account = f"{share['SMB_DOMAIN']}\\{share['SMB_USER']}" if share["SMB_DOMAIN"] else share["SMB_USER"]
        password = None
        if args.new_password or password_needed(env_text, share):
            password = _ask_password(account)
        new_text, changed = merge(env_text, share, password)
    except (ShareFileError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not changed:
        print(f"{env_path} already has these share settings; nothing changed.")
        return 0
    env_path.write_text(new_text, encoding="utf-8")
    what = [k for k in changed if k != "SMB_PASS"] + (["the password"] if "SMB_PASS" in changed else [])
    print(f"updated {env_path}: {', '.join(what)}")
    print(f"  share: //{share['SMB_HOST']}/{share['SMB_SHARE']} as {account}")
    if not host_resolves(share["SMB_HOST"]):
        print(f"  warning: this machine cannot resolve {share['SMB_HOST']}. Docker looks the name up "
              f"itself when it mounts the share, so the mount will fail. Put the print host's IP "
              f"address in SMB_HOST instead, or add a DNS entry.")
    if _volume_exists():
        print("The Docker volume still holds the old share settings. Recreate it:")
        print(f"  docker compose down && docker volume rm {VOLUME} && docker compose up -d")
    else:
        print("Next: docker compose up -d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
