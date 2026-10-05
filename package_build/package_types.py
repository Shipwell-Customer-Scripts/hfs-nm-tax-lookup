"""Packaging-type rules for HFS shipment line items (single source of truth).

Rule (confirmed by user 2026-09-30): a shipment line item's package_type mirrors the order
item's packaging ONLY when that value is a real Shipwell package type. If the order has no
packaging, or a value Shipwell does not know (e.g. TON), the shipment item stays blank (None).
"OTHER" is the corrogo/Shipwell default for "unset", so it is treated as blank and cleared.

Order-side source, in priority order:
  1. amount.unit                          (e.g. BARREL)
  2. shipping_requirements.packaging_type (e.g. BARREL)
Both are validated against VALID_PACKAGE_TYPES.
"""
from typing import Any, Callable, Dict, List, Optional, Tuple

# GET /v2/shipments/package-types/ (sandbox, 2026-09-30) — 32 codes.
VALID_PACKAGE_TYPES = frozenset({
    "BAG", "BALE", "BARREL", "BIN", "BOTTLE", "BOX", "BUCKET", "BUNDLE", "CAN", "CARTON",
    "CASE", "COIL", "CRATE", "CYLINDER", "DRUM", "FLOOR_LOADED", "JERRICAN", "OTHER",
    "PACKAGE", "PAIL", "PIECES", "PKG", "PLT", "REEL", "ROLL", "SKID", "TOTE_BIN",
    "TOTE_CAN", "TUBE", "UNIT", "VOLUME_GAL", "VOLUME_L",
})


def _norm(value: Any) -> Optional[str]:
    return str(value or "").strip().upper() or None


def desired_package_type(order_item: Optional[Dict]) -> Optional[str]:
    """Packaging the shipment line item should carry for this order item, or None (blank)."""
    item = order_item or {}
    candidates = (
        _norm((item.get("amount") or {}).get("unit")),
        _norm((item.get("shipping_requirements") or {}).get("packaging_type")),
    )
    for cand in candidates:
        if cand and cand != "OTHER" and cand in VALID_PACKAGE_TYPES:
            return cand
    return None


def _order_item_for(order: Dict, order_item_id: Optional[str]) -> Optional[Dict]:
    items = (order or {}).get("items") or []
    if order_item_id:
        for it in items:
            if it.get("id") == order_item_id:
                return it
    return items[0] if items else None


def sync_line_item_packaging(
    line_items: List[Dict],
    current_order: Optional[Dict],
    get_order: Callable[[str], Optional[Dict]],
) -> Tuple[List[Dict], List[str]]:
    """Return (line_items, changes) with package_type set per the rule above.

    Each shipment line item is matched to its own order via related_orders[0]
    (order_id / order_item_id). current_order is used when the ids match; other orders are
    fetched with get_order(order_id). Items whose order cannot be resolved are left untouched
    (never guess). The input list is not mutated.
    """
    import copy
    out = copy.deepcopy(line_items or [])
    changes: List[str] = []
    cache: Dict[str, Optional[Dict]] = {}
    cur_id = (current_order or {}).get("id")
    for idx, li in enumerate(out):
        rel = (li.get("related_orders") or [{}])[0] or {}
        order_id = rel.get("order_id")
        if order_id and order_id == cur_id:
            order = current_order
        elif order_id:
            if order_id not in cache:
                try:
                    cache[order_id] = get_order(order_id)
                except Exception:  # unresolved order -> leave the item alone
                    cache[order_id] = None
            order = cache[order_id]
        elif idx == 0 and current_order:
            order = current_order  # no related_orders info: single-order shipment
        else:
            order = None
        if not order:
            continue
        want = desired_package_type(_order_item_for(order, rel.get("order_item_id")))
        have = _norm(li.get("package_type"))
        # "OTHER" on the shipment counts as blank; want None + have OTHER -> clear it.
        if want != have and not (want is None and have is None):
            li["package_type"] = want
            changes.append(f"line_item[{idx}] {have!r}->{want!r}")
    return out, changes
