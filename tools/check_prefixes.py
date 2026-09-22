#!/usr/bin/env python3
"""Audit the whole tenant for cable types that share a prefix.

Two cable types with the same prefix issue colliding identifiers from two
independent counters. The poll loop vetoes this when it sees both assets
pending in the same poll, but that only catches the collision at the moment
it would happen. Run this at setup and after adding any cable type.

Read-only. Writes nothing, to Halo or anywhere else.
"""
from __future__ import annotations

import sys
from collections import defaultdict

import _bootstrap  # noqa: F401

from src.asset_source import build_cable_type
from src.config import load_config
from src.errors import StartupError
from src.halo_client import HaloClient


def main(argv=None) -> int:
    try:
        cfg = load_config()
    except StartupError as exc:
        print(exc, file=sys.stderr)
        return 2

    by_prefix = defaultdict(list)
    unprefixed = []

    with HaloClient(
        base_url=cfg.halo.base_url,
        auth_url=cfg.halo.auth_url,
        client_id=cfg.halo.client_id,
        client_secret=cfg.halo.client_secret,
        timeout=cfg.halo.timeout_seconds,
    ) as client:
        for asset in client.iter_assets(asset_group_id=cfg.halo.asset_group_id):
            cable = build_cable_type(asset, cfg)
            if cable.prefix:
                by_prefix[cable.prefix.strip().upper()].append(cable)
            else:
                unprefixed.append(cable)

    collisions = {p: group for p, group in by_prefix.items() if len(group) > 1}

    print(f"{len(by_prefix)} distinct prefix(es) across {sum(len(g) for g in by_prefix.values())} asset(s)")
    for prefix in sorted(by_prefix):
        group = by_prefix[prefix]
        marker = "COLLISION" if len(group) > 1 else "ok"
        detail = ", ".join(f"asset {c.id} (next={c.next_id})" for c in group)
        print(f"  {marker:<10} {prefix:<10} {detail}")

    if unprefixed:
        print(f"\n{len(unprefixed)} asset(s) have no prefix set (they cannot be labelled):")
        for cable in unprefixed[:20]:
            print(f"  asset {cable.id} {cable.name}")

    if collisions:
        print(
            f"\n{len(collisions)} prefix collision(s). Each colliding pair keeps its OWN "
            f"counter, so both will eventually issue the same identifier. The service "
            f"refuses to reserve for any of these while they are both pending. Give each "
            f"cable type a unique prefix."
        )
        return 1

    print("\nno prefix collisions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
