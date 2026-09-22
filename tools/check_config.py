#!/usr/bin/env python3
"""Compare this deployment's config against the templates an update brought.

Updates never touch `.env` or `config/*.yaml` -- they are gitignored, so a
pull leaves them alone and a rebuild mounts them unchanged. That is the
behaviour you want, but it has one blind spot: a new setting added upstream
appears only in the `.example` template, and the deployment silently keeps
running on the code's built-in default. Nobody is told a choice now exists.

This tool is the telling. It compares key names only -- never values, which
are site-specific and in `.env` are secret -- and it writes nothing. What it
reports:

  * settings the template has and this deployment does not (new upstream, or
    never copied across), with the template's default
  * settings this deployment has that the template dropped (renamed or
    retired upstream; harmless, but they now do nothing)
  * a live file missing entirely, which is what a fresh install looks like

Read-only by design: it suggests, and a human edits. Merging someone's
`.env` automatically is how a deployment ends up with a setting nobody chose.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
_ENV_LINE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)\s*=(.*)$")


def env_keys(text: str) -> dict[str, str]:
    """Key -> value for a .env file. Comments and blank lines are skipped."""
    found = {}
    for line in text.replace("\r\n", "\n").split("\n"):
        match = _ENV_LINE.match(line)
        if match:
            found[match.group(1)] = match.group(2).strip()
    return found


def yaml_keys(text: str) -> dict[str, str]:
    """Dotted leaf paths -> value for a YAML file.

    Paths rather than a nested diff so that `fields.color.id` reads the same
    way as an env var does, and so both halves of this tool report alike.
    """
    data = yaml.safe_load(text) or {}
    found: dict[str, str] = {}

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        else:
            found[path] = "" if node is None else str(node)

    walk(data, "")
    return found


def compare(template: Path, live: Path) -> tuple[list[str], dict[str, str], list[str]]:
    """Return (notes, missing_here, retired). `missing_here` maps key -> the
    template's value, which is the default an operator would be accepting."""
    if not template.is_file():
        return [f"no template at {template.name}; nothing to compare"], {}, []
    if not live.is_file():
        return (
            [f"{live.name} does not exist yet -- copy {template.name} to it and fill it in"],
            {},
            [],
        )

    reader = env_keys if live.suffix != ".yaml" else yaml_keys
    try:
        template_keys = reader(template.read_text())
        live_keys = reader(live.read_text())
    except yaml.YAMLError as exc:
        return [f"{live.name} is not valid YAML: {exc}"], {}, []

    missing = {k: v for k, v in template_keys.items() if k not in live_keys}
    retired = [k for k in live_keys if k not in template_keys]
    return [], missing, sorted(retired)


def pairs(repo_root: Path) -> list[tuple[Path, Path]]:
    """Every (template, live) pair this deployment has, .env first."""
    found = [(repo_root / ".env.example", repo_root / ".env")]
    for template in sorted((repo_root / "config").glob("*.example.yaml")):
        found.append((template, template.with_name(template.name.replace(".example", ""))))
    return found


def report(repo_root: Path, out=None) -> bool:
    """Print the comparison. True if everything matches."""
    out = sys.stdout if out is None else out   # bound per call, not at import
    clean = True
    for template, live in pairs(repo_root):
        notes, missing, retired = compare(template, live)
        for note in notes:
            clean = False
            print(f"  {live.name}: {note}", file=out)
        if missing:
            clean = False
            print(f"  {live.name}: {len(missing)} setting(s) in {template.name} are not here:", file=out)
            for key, value in missing.items():
                shown = f" (template: {value})" if value else ""
                print(f"      + {key}{shown}", file=out)
        if retired:
            clean = False
            print(f"  {live.name}: no longer in {template.name}, so now ignored:", file=out)
            for key in retired:
                print(f"      - {key}", file=out)
    if clean:
        print("  config matches the templates; nothing to carry across", file=out)
    return clean


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero when anything differs (for CI; an update script "
        "should report and carry on, since every new setting has a default)",
    )
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    print("Comparing this deployment's config against the templates:")
    clean = report(args.root)
    if not clean:
        print(
            "\nNothing was changed. New settings fall back to the code's default "
            "until you add them by hand, which is the point: the file is yours."
        )
    return 1 if (args.strict and not clean) else 0


if __name__ == "__main__":
    raise SystemExit(main())
