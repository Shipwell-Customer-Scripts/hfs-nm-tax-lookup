"""Shipment creation helper functions."""

import copy
from typing import Any, Dict

from config import DEFAULT_SERVICE_LEVEL, DEFAULT_SHIPMENT_STATUS
from hfs_utils import (
    get_custom_field_value,
    get_reference_value,
    normalize_custom_field_text,
    notify_bodies_for_shipment,
)
from notifications import notify_support
from shipwell_client import _api_call, safe_update_shipment


def get_special_instructions_value(order_data: Dict, custom_fields: Dict) -> str:
    """Read Special Instructions from order custom fields using common config keys."""
    for key in ("special_instructions", "special_instruction", "carrier_instructions"):
        field_id = custom_fields.get(key, "")
        value = get_custom_field_value(order_data, field_id) if field_id else None
        if value:
            return normalize_custom_field_text(value)[:550]

    for ref_key in ("SPECIAL_INSTRUCTIONS", "Special Instructions"):
        value = get_reference_value(order_data, (ref_key,))
        if value:
            return normalize_custom_field_text(value)[:550]
    return ""


def append_carrier_notes(existing_notes: Any, additional_notes: str) -> str:
    """Append carrier notes without duplicating identical instructions on retries."""
    existing_text = normalize_custom_field_text(existing_notes)
    additional_text = normalize_custom_field_text(additional_notes)
    if not additional_text:
        return existing_text
    if not existing_text:
        return additional_text
    if additional_text in existing_text:
        return existing_text
    return f"{existing_text}\n\n{additional_text}"


def get_turn_order_note(order_data: Dict, custom_fields: Dict) -> str:
    """Return the carrier note that marks a shipment as part of a turn."""
    turn_order_fid = custom_fields.get("turn_order_number", "")
    turn_order_number = get_custom_field_value(order_data, turn_order_fid) if turn_order_fid else None
    if not turn_order_number:
        return ""
    order_number = order_data.get("order_number", "")
    if turn_order_number == order_number:
        return f"Turn Order: master turn {turn_order_number}."
    return f"Turn Order: sub-turn for master turn {turn_order_number}."


def build_shipment_name(order_data: Dict, business_unit: str, custom_fields: Dict) -> str:
    """Build the top-level shipment name field.

    Format: "<Product> | <Business Unit> | <Type Tag>"
    Examples:
      "Diesel | FUELS"
      "Asphalt | ASPHALTS | Split"
      "Diesel | FUELS | Turn (Parent)"
      "Diesel | FUELS | Turn (Child)"
      "Asphalt | ASPHALTS | Split | Turn (Parent)"

    Parts are only appended when the corresponding data is present.
    """
    parts = []

    # --- Product name ---
    items = order_data.get("items") or []
    if items:
        first_item = items[0]
        product_name = (
            first_item.get("name")
            or first_item.get("description")
            or first_item.get("product_ref")
            or ""
        ).strip()
        if product_name:
            parts.append(product_name)

    # --- Business unit ---
    bu = normalize_custom_field_text(business_unit).upper()
    if bu:
        parts.append(bu)

    # --- Split tag ---
    split_order_fid = custom_fields.get("split_order", "")
    split_order_id = get_custom_field_value(order_data, split_order_fid) if split_order_fid else None
    is_split = bool(normalize_custom_field_text(split_order_id))
    if is_split:
        parts.append("Split")

    # --- Turn order tag ---
    turn_order_fid = custom_fields.get("turn_order_number", "")
    turn_order_number = get_custom_field_value(order_data, turn_order_fid) if turn_order_fid else None
    turn_order_number = normalize_custom_field_text(turn_order_number)
    if turn_order_number:
        order_number = normalize_custom_field_text(order_data.get("order_number", ""))
        is_parent_turn = turn_order_number == order_number
        parts.append("Turn (Parent)" if is_parent_turn else "Turn (Child)")

    return " | ".join(parts)


def build_create_shipment_body(
    order_id: str, company_id: str, equipment_type: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the standard shipment creation request body.

    The shipment-assembly/load/create_shipment endpoint requires equipment_type
    as a plain string (machine_readable code), not the full dict object.
    """
    # Extract machine_readable string if a full equipment-type dict was passed
    if isinstance(equipment_type, dict):
        equipment_type_code = equipment_type.get("machine_readable") or equipment_type.get("code") or equipment_type
    else:
        equipment_type_code = equipment_type
    # shipment-assembly only accepts a subset of machine_readable codes.
    # MULTICOMPART_TANK is valid on direct shipment PUT but rejected by assembly.
    # Use MULTI_COMPARTMENT for creation; apply_shipment_creation_fields will
    # correct it to MULTICOMPART_TANK in the follow-up PUT.
    _ASSEMBLY_REMAP = {
        "MULTICOMPART_TANK": "MULTI_COMPARTMENT",
    }
    equipment_type_code = _ASSEMBLY_REMAP.get(equipment_type_code, equipment_type_code)
    return {
        "orders_simple": [{"id": order_id, "resource_type": "purchase_order"}],
        "mode": "FTL",
        "service_level": DEFAULT_SERVICE_LEVEL,
        "status": DEFAULT_SHIPMENT_STATUS,
        "equipment_type": equipment_type_code,
        "distribution_mode": False,
        "customer_id": company_id,
    }


def apply_shipment_creation_fields(
    shipment_data: Dict, order_data: Dict, equipment_type: Dict[str, Any],
    base_url: str, headers: Dict, custom_fields: Dict, sw: "ShipwellProgram",
    business_unit: str = "",
) -> Dict:
    """Set post-create shipment fields required by the HFS shipment creation doc."""
    updated = copy.deepcopy(shipment_data)
    order_number = order_data.get("order_number", "")
    special_instructions = get_special_instructions_value(order_data, custom_fields)
    turn_order_note = get_turn_order_note(order_data, custom_fields)

    if order_number:
        updated["bol_number"] = order_number

    shipment_name = build_shipment_name(order_data, business_unit, custom_fields)
    if shipment_name:
        updated["name"] = shipment_name
        sw.log("TRACE", f"Shipment name set: {shipment_name!r}", ["apply_shipment_creation_fields"])
    # Strip 'id' from equipment_type before PUT — equipment type IDs differ between
    # sandbox/dev/prod environments. The API resolves the correct ID from machine_readable.
    updated["equipment_type"] = {k: v for k, v in equipment_type.items() if k != "id"}
    if special_instructions:
        updated["notes_for_carrier"] = append_carrier_notes(updated.get("notes_for_carrier"), special_instructions)
    if turn_order_note:
        updated["notes_for_carrier"] = append_carrier_notes(updated.get("notes_for_carrier"), turn_order_note)

    # Keep these defaults explicit when Shipwell returns the fields in the shipment payload.
    # Some API versions reject unknown shipment PUT fields, so creation remains the primary
    # place where service_level/status are asserted.
    if "service_level" in updated and not updated.get("service_level"):
        updated["service_level"] = DEFAULT_SERVICE_LEVEL
    if "status" in updated and not updated.get("status"):
        updated["status"] = DEFAULT_SHIPMENT_STATUS

    # Write Business Unit directly to the shipment custom data (shipment section).
    # BU is derived from the product catalog — it is NOT stored on the order,
    # so shipment-assembly will not mirror it automatically. We own this write.
    # Must go in shipwell_custom_data.shipment (not purchase_order) so the UI renders it.
    if business_unit:
        bu_fid = custom_fields.get("order_business_unit") or custom_fields.get("business_unit", "")
        if bu_fid:
            ship_section = (
                (updated.get("custom_data") or {})
                .get("shipwell_custom_data", {})
                .get("shipment", {})
            )
            existing_bu = str(ship_section.get(bu_fid) or "").strip()
            if existing_bu != business_unit:
                updated.setdefault("custom_data", {})\
                       .setdefault("shipwell_custom_data", {})\
                       .setdefault("shipment", {})[bu_fid] = business_unit
                sw.log("TRACE", f"Business Unit written to shipment custom_data.shipment: {business_unit!r}",
                       ["apply_shipment_creation_fields"])

    # Packaging type at creation: mirror the order's packaging only when it is a real Shipwell
    # package type (package_types.py); otherwise the shipment item stays blank ("OTHER" cleared).
    _order_items = order_data.get("items") or []
    _ship_line_items = updated.get("line_items") or []
    # shipment-assembly may return the shipment before line_items are populated — retry GET.
    if _order_items and not _ship_line_items and updated.get("id"):
        import time as _t
        for _li_attempt in range(3):
            _t.sleep(2)
            _refreshed = _api_call(f"{base_url}/v2/shipments/{updated['id']}/", "GET", headers=headers) or {}
            _ship_line_items = _refreshed.get("line_items") or []
            if _ship_line_items:
                sw.log("TRACE", f"line_items appeared after {_li_attempt + 1} GET(s)",
                       ["apply_shipment_creation_fields"])
                break
        if not _ship_line_items:
            sw.log("WARNING", "line_items empty after retries — skipping packaging_type sync",
                   ["apply_shipment_creation_fields"])
    if _order_items and _ship_line_items:
        from package_types import sync_line_item_packaging
        _synced, _pkg_changes = sync_line_item_packaging(
            _ship_line_items, order_data,
            lambda _oid: _api_call(f"{base_url}/orders/{_oid}", "GET", headers=headers),
        )
        sw.log("TRACE", f"packaging_type sync at creation: {_pkg_changes or 'no change'}",
               ["apply_shipment_creation_fields"])
        if _pkg_changes:
            updated["line_items"] = _synced

    updated = safe_update_shipment(updated, base_url, headers, "Apply shipment creation fields")
    sw.log("TRACE", "Shipment creation fields applied", ["apply_shipment_creation_fields"])
    warn_if_mileage_missing(updated, order_data, custom_fields, sw)
    return updated


def warn_if_mileage_missing(
    shipment_data: Dict, order_data: Dict, custom_fields: Dict, sw: "ShipwellProgram",
) -> None:
    """Warn support when mileage was not calculated and this is not a customer pickup."""
    preset_scac_fid = custom_fields.get("preset_scac", "")
    preset_scac = get_custom_field_value(order_data, preset_scac_fid) if preset_scac_fid else None
    if normalize_custom_field_text(preset_scac).upper() == "CUST":
        return

    total_miles = shipment_data.get("total_miles")
    try:
        miles_value = float(total_miles or 0)
    except (TypeError, ValueError):
        miles_value = 0

    if miles_value <= 0:
        detail = "total_miles is 0 or missing. Mileage may not have been calculated by Shipwell at creation time. Manual review may be required for carrier rate calculation."
        subject = "Warning Shipment Mileage Not Calculated"
        plain, html = notify_bodies_for_shipment(shipment_data, subject, error_detail=detail)
        notify_support(subject, plain, sw, html_body=html)
