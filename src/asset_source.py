"""get_pending() -- the ONLY place the trigger query lives.

Kept as its own module so that when Halo grows a server-side filter for
custom field values, or the group's asset count makes listing everything
expensive, this is the one function to change.
"""
from __future__ import annotations

import logging

from .config import AppConfig
from .fields import as_int, as_text, field_by_id, field_text_by_id
from .models import CableType

logger = logging.getLogger(__name__)


def build_cable_type(asset: dict, cfg: AppConfig) -> CableType:
    """Map one raw Halo asset onto a CableType.

    Fields are addressed by numeric id rather than by name: ids do not
    drift, and a mis-cased name fails silently by matching nothing. The
    counter and quantity are passed through as_int with a default that
    PRESERVES the original value when it will not coerce, so the allocator
    can refuse it with a message naming what was actually found.
    """
    raw_next = field_by_id(asset, cfg.halo.nextid_field_id)
    raw_qty = field_by_id(asset, cfg.halo.qty_field_id)
    return CableType(
        id=asset.get("id"),
        prefix=field_text_by_id(asset, cfg.halo.prefix_field_id),
        next_id=as_int(raw_next, default=raw_next),
        qty=as_int(raw_qty, default=0) or 0,
        name=as_text(asset.get("inventory_number") or asset.get("name")),
        color=field_text_by_id(asset, cfg.halo.color_field_id) if cfg.halo.color_field_id else "",
        type_name=as_text(asset.get("assettype_name")),
        raw=asset,
    )


def get_pending(client, cfg: AppConfig) -> list[CableType]:
    """Every cable type with a positive Cables to Label value.

    Scoping is the asset group (HALO_CABLE_ASSET_GROUP_ID), applied by Halo
    through the assetgroup_id query parameter: only records someone
    deliberately put in the label-automation group can ever trigger a
    print. There is no client-side re-check because there is nothing to
    check against -- the live tenant returns no group key on an asset at
    all. That the parameter is honoured was measured (the group returned
    only its own members, a fraction of the visible assets), and
    tools/check_halo.py re-measures it by comparing the scoped and unscoped
    counts.
    """
    pending: list[CableType] = []
    scanned = 0
    for asset in client.iter_assets(asset_group_id=cfg.halo.asset_group_id):
        scanned += 1
        cable = build_cable_type(asset, cfg)
        if isinstance(cable.qty, int) and cable.qty > 0:
            pending.append(cable)

    logger.debug(
        "scanned %d asset(s) in group %s, %d pending",
        scanned, cfg.halo.asset_group_id, len(pending),
    )
    if scanned == 0:
        logger.warning(
            "the asset list came back empty for group %s. If that is unexpected, this "
            "is almost always the API application's PERMISSIONS rather than its login "
            "mode -- compare what it can see against an integration that already works "
            "(tools/check_halo.py reports what is visible with and without the group).",
            cfg.halo.asset_group_id,
        )
    return pending
