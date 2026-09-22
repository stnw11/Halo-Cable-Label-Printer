#!/usr/bin/env python3
"""Verify the Halo side end to end. Prints nothing physical, writes nothing.

Run this before wiring any loop. It answers, in order:

  1. Do the credentials work at all?
  2. Can this application SEE assets, and does Halo honour the asset group
     scope? (Zero visible assets is a PERMISSIONS problem on the API
     application, not its login mode -- see src/halo_client.py.)
  3. Is each id in config/fields.yaml the field it is named as, and which
     of them are dropdowns?
  4. What is pending right now?

With --write-test <asset id> it also round-trips a write to the counter and
the status line, restoring the status afterwards. That is the check
that proves the API application's write permission before anything
depends on it. The prefix is never written: the service only reads it, and
on a dropdown it would need an option index, not the text.
"""
from __future__ import annotations

import argparse
import sys

import _bootstrap  # noqa: F401

from src.asset_source import build_cable_type, get_pending
from src.config import load_config
from src.errors import StartupError
from src.fields import field_by_id, field_entry
from src.halo_client import HaloClient


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write-test", type=int, metavar="ASSET_ID",
                        help="round-trip a write to the counter and status fields on this asset")
    parser.add_argument("--limit", type=int, default=5, help="how many assets to show")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except StartupError as exc:
        print(exc, file=sys.stderr)
        return 2

    with HaloClient(
        base_url=cfg.halo.base_url,
        auth_url=cfg.halo.auth_url,
        client_id=cfg.halo.client_id,
        client_secret=cfg.halo.client_secret,
        timeout=cfg.halo.timeout_seconds,
    ) as client:
        print("1. authenticating...")
        try:
            client.get_token()
        except Exception as exc:
            print(f"   FAILED: {exc}", file=sys.stderr)
            return 1
        print("   ok")

        print("2. listing assets...")
        group = cfg.halo.asset_group_id
        try:
            total = client.list_assets_page(1).get("record_count", 0)
            assets = list(client.iter_assets(asset_group_id=group))
        except Exception as exc:
            print(f"   FAILED: {exc}", file=sys.stderr)
            return 1
        print(f"   {total} asset(s) visible in total")
        if group is None:
            print("   no HALO_CABLE_ASSET_GROUP_ID set -- EVERY visible asset is in scope")
        else:
            print(f"   {len(assets)} in asset group {group}")
            if assets and len(assets) == total:
                print("   !! the group returned every visible asset. Either everything really")
                print("   is in the group, or Halo ignored the assetgroup_id parameter -- in")
                print("   which case the service is NOT scoped. Check a non-cable asset.")
        try:
            types = client._client.get(
                f"{cfg.halo.base_url}/api/AssetType", headers=client._headers()
            ).json()
            type_list = types if isinstance(types, list) else types.get("assettypes", [])
            print(f"   {len(type_list)} asset type(s) visible")
            if len(type_list) <= 2:
                print("   A very low asset-type count is the clearest sign that this")
                print("   application's permissions are narrower than they should be.")
        except Exception as exc:
            print(f"   (could not list asset types: {exc})")
        if not assets:
            print("   No assets in scope.")
            print("   This is almost always PERMISSIONS on the API application, not its")
            print("   login mode. Measured on this tenant: a working integration and a")
            print("   broken one were both on Application identity; the difference was")
            print("   that one could see 31 asset types and the other 1.")
            print("   Compare this application's permissions and asset-type visibility")
            print("   against an integration that already works.")
            return 1

        print("3. checking config/fields.yaml against Halo...")
        wanted = {
            "qty": (cfg.halo.qty_field_id, cfg.halo.qty_field_name),
            "prefix": (cfg.halo.prefix_field_id, cfg.halo.prefix_field_name),
            "next_id": (cfg.halo.nextid_field_id, cfg.halo.nextid_field_name),
            "status": (cfg.halo.status_field_id, cfg.halo.status_field_name),
        }
        problems = False
        single = None
        for role, (field_id, name) in wanted.items():
            entries = [e for e in (field_entry(a, field_id) for a in assets) if e]
            if not entries:
                # The list response leaves empty fields out entirely; a
                # single-asset GET includes them, so fall back to one.
                if single is None:
                    single = client.get_asset(assets[0]["id"])
                entry = field_entry(single, field_id)
                entries = [entry] if entry else []
            if not entries:
                problems = True
                print(f"   !! {role:<8} id={field_id} ({name!r}) is not on asset {assets[0]['id']} at all")
                continue
            halo_name = entries[0].get("name")
            kind = "dropdown" if entries[0].get("lookup") else "text"
            if halo_name != name:
                problems = True
                print(f"   !! {role:<8} id={field_id} is {halo_name!r} in Halo, but fields.yaml calls it {name!r}")
            else:
                print(f"   ok {role:<8} id={field_id} {name!r} ({kind}, on {len(entries)} asset(s))")
        if problems:
            print("   A name mismatch means the id points at a different field than intended.")
            print("   Fix the id (or the name, if the field was renamed) in config/fields.yaml.")

        print(f"4. pending cable types (group scope: {group or 'none'})...")
        pending = get_pending(client, cfg)
        if not pending:
            print("   none (nothing has Cables to Label set above zero)")
        for cable in pending[: args.limit]:
            print(f"   asset {cable.id}: prefix={cable.prefix!r} next={cable.next_id} qty={cable.qty}")

        if args.write_test:
            print(f"5. write round-trip on asset {args.write_test}...")
            asset = client.get_asset(args.write_test)
            before = build_cable_type(asset, cfg)
            original_status = field_by_id(asset, cfg.halo.status_field_id)
            try:
                client.update_fields(args.write_test, [
                    {"id": cfg.halo.nextid_field_id, "value": str(before.next_id)},
                    {"id": cfg.halo.status_field_id, "value": "check_halo.py write test"},
                ])
                after = client.read_counter(args.write_test, cfg.halo.nextid_field_id)
                print(f"   ok -- wrote and read back the counter ({after})")
            except Exception as exc:
                print(f"   FAILED: {exc}", file=sys.stderr)
                print("   The API application needs asset write permission. Halo's")
                print("   read-only flag is a form-layer control and does not block the API.")
                return 1
            finally:
                try:
                    client.update_fields(args.write_test, [
                        {"id": cfg.halo.status_field_id, "value": original_status or ""},
                    ])
                    print("   restored the original status field value")
                except Exception as exc:
                    print(f"   WARNING: could not restore the status field: {exc}", file=sys.stderr)

    print("\nall checks complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
