"""Order-focused business helpers."""

import logging
from typing import Any, Dict, List, Optional, Tuple

import requests

from accessorials import has_void_accessorial
from config import DRY_RUN, EQUIPMENT_TYPE_MAP, PRODUCT_CATEGORY_BU_MAP, REQUIRE_BUSINESS_UNIT
from hfs_utils import (
    build_order_email_body,
    build_order_email_html,
    notify_bodies_for_order,
    get_custom_field_value,
    get_item_product_ref,
    normalize_custom_field_text,
    set_custom_field_value,
)
from notifications import notify_support
from shipwell_client import _api_call, safe_update_order
from shipwell_resources import (
    equipment_identity,
    get_product_custom_value,
    get_product_for_item,
    normalize_equipment_type_value,
)

logger = logging.getLogger()


def cancel_order(order_data: Dict, base_url: str, headers: Dict) -> None:
    """Cancel an order using the Shipwell cancel endpoint."""
    order_id = order_data.get("id")
    if not order_id:
        raise ValueError("Cannot cancel order without id")

    # Prefer the corrogo /orders/ endpoint (all HFS orders are corrogo-native).
    # Fall back to the legacy /purchase-orders/ endpoint for any older orders
    # that may have been created via the legacy API.
    cancel_urls = [
        f"{base_url}/orders/{order_id}/cancel",
        f"{base_url}/orders/{order_id}/cancel/",
        f"{base_url}/purchase-orders/{order_id}/cancel",
        f"{base_url}/purchase-orders/{order_id}/cancel/",
    ]

    if DRY_RUN:
        logger.info(f"[DRY_RUN] Would cancel order {order_id} via {cancel_urls[0]}")
        return

    last_error: Optional[Exception] = None
    for url in cancel_urls:
        try:
            _api_call(url, "POST", headers=headers, body={})
            logger.info(f"Order cancelled: {order_id}")
            return
        except requests.exceptions.HTTPError as e:
            status_code = e.response.status_code if e.response is not None else None
            if status_code in (404, 405):
                last_error = e
                continue
            raise

    raise RuntimeError(f"Unable to cancel order {order_id}") from last_error


def check_for_voided_order(
    order_data: Dict, base_url: str, headers: Dict, custom_fields: Dict, sw: "ShipwellProgram",
) -> bool:
    """
    Handle a newly-created order that arrives with VOID in Preplanned Accessorials.
    Returns True when processing should stop before shipment creation.
    """
    if not has_void_accessorial(order_data, custom_fields):
        return False

    order_number = order_data.get("order_number", order_data.get("id", "unknown"))
    sw.log("TRACE", f"VOID found in Preplanned Accessorials for order {order_number}", ["check_for_voided_order"])
    cancel_order(order_data, base_url, headers)
    _void_subject = "New Order Voided"
    _void_plain = build_order_email_body(order_data)
    _void_html = build_order_email_html(order_data, _void_subject)
    notify_support(_void_subject, _void_plain, sw, html_body=_void_html)
    return True


def set_business_unit(
    order_data: Dict, base_url: str, headers: Dict, custom_fields: Dict, sw: "ShipwellProgram",
) -> Tuple[Dict, str]:
    """
    Determine Business Unit from all order item products and write it to the order custom field.
    Returns the updated order payload and the normalized business unit string.
    """
    business_unit_fid = custom_fields.get("business_unit", "")
    if not business_unit_fid:
        sw.log("WARNING", "Business Unit custom field id not configured", ["set_business_unit"])
        return order_data, ""

    # The order-level BU field may be a separate field from the product-level BU field.
    # "order_business_unit" key = field to write concatenated BU onto the order.
    # "business_unit" key = field to read BU from each product.
    # Falls back to "business_unit" for the order write if "order_business_unit" is not configured.
    order_bu_fid = custom_fields.get("order_business_unit") or business_unit_fid
    sw.log(
        "TRACE",
        f"set_business_unit: product field_id={business_unit_fid!r}, order field_id={order_bu_fid!r}",
        ["set_business_unit"],
    )

    # If BU is already set on the order itself, trust it and skip the product catalog lookup.
    # This handles the order.updated retry path: the user set BU directly on the order after
    # an order.created failure, and we should not re-derive it from products (which may still
    # be missing BU data in the product catalog).
    existing_bu = normalize_custom_field_text(get_custom_field_value(order_data, order_bu_fid))
    if existing_bu:
        sw.log("TRACE", f"set_business_unit: BU already set on order: {existing_bu!r} — skipping product lookup", ["set_business_unit"])
        return order_data, existing_bu

    items = order_data.get("items") or []
    if not items:
        raise RuntimeError("Error Retrieving Business Unit From Product(s): order has no items")

    business_units: List[str] = []
    unknown_refs: List[str] = []   # product ref not found in catalog at all
    no_bu_refs: List[str] = []     # product found but missing BU custom field
    for item in items:
        product = get_product_for_item(item, base_url, headers)
        product_ref = get_item_product_ref(item) or (item.get("shipping_requirements") or {}).get("product_id") or item.get("id", "")
        business_unit = ""
        product_not_found = False
        if product:
            # Try product_business_unit first (product-level BU field UUID),
            # fall back to business_unit for backward compatibility.
            business_unit = normalize_custom_field_text(
                get_product_custom_value(product, custom_fields, "product_business_unit")
                or get_product_custom_value(product, custom_fields, "business_unit")
            )
            if not business_unit:
                sw.log(
                    "WARNING",
                    f"Product {product_ref!r} found (id={product.get('id')}) but has no Business Unit custom field. "
                    f"custom_data.shipwell_custom_data: {(product.get('custom_data') or {}).get('shipwell_custom_data')!r}",
                    ["set_business_unit"],
                )
        else:
            product_not_found = True
            sw.log("WARNING", f"Product {product_ref!r} not found in catalog at {base_url}", ["set_business_unit"])

        # Fallback: use PRODUCT_CATEGORY_BU_MAP env var config when product catalog lookup fails.
        # This lets orders proceed even when sandbox/dev product data is incomplete.
        if not business_unit and PRODUCT_CATEGORY_BU_MAP:
            product_category = normalize_custom_field_text(item.get("product_category") or item.get("category")).upper()
            if product_category and product_category in PRODUCT_CATEGORY_BU_MAP:
                business_unit = PRODUCT_CATEGORY_BU_MAP[product_category]
                sw.log(
                    "WARNING",
                    f"Product {product_ref!r}: BU resolved via PRODUCT_CATEGORY_BU_MAP "
                    f"(product_category={product_category!r} -> BU={business_unit!r})",
                    ["set_business_unit"],
                )

        if not business_unit:
            if product_not_found:
                unknown_refs.append(str(product_ref))
            else:
                no_bu_refs.append(str(product_ref))
            continue
        business_units.append(business_unit)

    if unknown_refs:
        # Product reference(s) not found in Shipwell catalog — bad product number on the order.
        # Send a clear email identifying the unknown ref and instruct user to fix the order.
        subject = "Unknown Product Reference(s): " + ", ".join(unknown_refs)
        order_number = order_data.get("order_number", order_data.get("id", "unknown"))
        detail = (
            f"The following product reference(s) were not found in the Shipwell product catalog: "
            f"{', '.join(unknown_refs)}.\n\n"
            f"Shipment creation has been skipped for order {order_number}.\n\n"
            f"To correct this issue:\n"
            f"Correct the product number on the order. "
            f"The system will automatically retry shipment creation when the order is updated."
        )
        plain, html = notify_bodies_for_order(order_data, subject, error_detail=detail)
        notify_support(subject, plain, sw, html_body=html)
        msg = f"{subject}"
        sw.log("ERROR", msg, ["set_business_unit"])
        raise RuntimeError(msg)

    if no_bu_refs:
        # Product(s) exist in catalog but are missing the Business Unit custom field.
        # This is a catalog data quality issue — different remediation from unknown product.
        subject = "Error Retrieving Business Unit From Product(s)"
        order_number = order_data.get("order_number", order_data.get("id", "unknown"))
        detail = (
            f"Business Unit could not be resolved for product ref(s): {', '.join(no_bu_refs)} "
            f"on order {order_number}.\n\n"
            f"To correct this issue:\n"
            f"1. Open the order in Shipwell and set the Business Unit field directly on the order, then save. "
            f"The system will automatically retry shipment creation when the order is updated.\n"
            f"2. Alternatively, ensure the Business Unit custom field is populated on each product record "
            f"({', '.join(no_bu_refs)}) in the Shipwell product catalog."
        )
        plain, html = notify_bodies_for_order(order_data, subject, error_detail=detail)
        notify_support(subject, plain, sw, html_body=html)
        msg = f"{subject}: {', '.join(no_bu_refs)}"
        sw.log("ERROR", msg, ["set_business_unit"])
        raise RuntimeError(msg)

    business_unit_value = "/".join(sorted(set(business_units), key=str.upper))
    current_value = normalize_custom_field_text(get_custom_field_value(order_data, order_bu_fid))
    if current_value == business_unit_value:
        sw.log("TRACE", f"Business Unit already set: {business_unit_value}", ["set_business_unit"])
        return order_data, business_unit_value

    # BU is derived from products — do not write it back to the order.
    # The caller writes BU directly to the shipment custom data after shipment creation.
    sw.log("TRACE", f"Business Unit derived: {business_unit_value}", ["set_business_unit"])
    return order_data, business_unit_value


def maybe_set_preset_equipment_for_emulsion(
    order_data: Dict, equipment_type: Dict[str, Any], base_url: str, headers: Dict,
    custom_fields: Dict, sw: Optional["ShipwellProgram"] = None,
) -> None:
    """If product equipment resolves to Emulsion, write Preset Equipment on the order."""
    preset_equipment_fid = custom_fields.get("preset_equipment", "")
    if not preset_equipment_fid:
        return

    equipment_name = normalize_custom_field_text(
        equipment_type.get("name") or equipment_type.get("machine_readable")
    )
    if equipment_name.upper() != "EMULSION":
        return

    current_value = normalize_custom_field_text(get_custom_field_value(order_data, preset_equipment_fid))
    if current_value.upper() == "EMULSION":
        return

    updated_order = set_custom_field_value(order_data, "purchase_order", preset_equipment_fid, "Emulsion")
    safe_update_order(updated_order, base_url, headers, "Set Preset Equipment=Emulsion")
    if sw:
        sw.log("TRACE", "Preset Equipment set to Emulsion", ["determine_equipment_type"])


# Product reference for Water — excluded from equipment and multi-compartment checks.
WATER_PRODUCT_REF = "100197"


def determine_equipment_type(
    order_data: Dict, base_url: str, headers: Dict, custom_fields: Dict = None,
    business_unit: str = "", sw: Optional["ShipwellProgram"] = None,
) -> Dict:
    """
    Determine shipment equipment per HFS rules:

    1. Preset Equipment custom field → use it (any BU; CRUDE should always set this).
    2. Strip Water (product ref 100197) from all remaining checks — Water ships with
       any product and never specifies an equipment type.
    3. Collect product-level equipment for each remaining item that has one specified.
       If 2+ non-Water items each specify a *different* equipment → error.
    4. 2+ distinct non-Water product refs → Multi-Compartment Tanker (all BUs).
    5. Single non-Water item (or all same ref) with product equipment → use it.
    6. Default → Tanker.
    """
    custom_fields = custom_fields or {}

    # Step 1: Preset Equipment custom field.
    preset_equipment_fid = custom_fields.get("preset_equipment", "")
    preset_equipment = get_custom_field_value(order_data, preset_equipment_fid) if preset_equipment_fid else None
    preset_equipment_type = normalize_equipment_type_value(preset_equipment)
    if preset_equipment_type:
        logger.info(f"Preset Equipment={preset_equipment} -> {preset_equipment_type.get('name')}")
        return preset_equipment_type

    all_items = order_data.get("items") or []

    # Step 2: Exclude Water from all equipment/multi-compartment logic.
    items = [
        item for item in all_items
        if (get_item_product_ref(item) or "").strip() != WATER_PRODUCT_REF
    ]
    water_count = len(all_items) - len(items)
    if water_count:
        logger.info(f"determine_equipment_type: excluded {water_count} Water item(s) (ref={WATER_PRODUCT_REF}) from equipment check")

    # Step 3: Collect product-level equipment for non-Water items.
    equipment_by_key: Dict[str, Dict[str, Any]] = {}
    for item in items:
        product = get_product_for_item(item, base_url, headers)
        if not product:
            logger.warning(f"No product found while determining equipment for item {item.get('id', '')}")
            continue
        raw_equipment = (
            get_product_custom_value(product, custom_fields, "equipment")
            or product.get("equipment_type")
            or product.get("equipment")
        )
        equipment_type = normalize_equipment_type_value(raw_equipment)
        if not equipment_type:
            continue
        equipment_by_key[equipment_identity(equipment_type)] = equipment_type

    # Conflicting product-level equipment across non-Water items → error.
    if len(equipment_by_key) > 1:
        subject = "Error Determining Equipment Type \u2013 Multiple Equipment Defined"
        details = ", ".join(sorted(equipment_by_key.keys()))
        detail = (
            f"Multiple different equipment types found across order items: {details}. "
            f"All items must require the same equipment type."
        )
        plain, html = notify_bodies_for_order(order_data, subject, error_detail=detail)
        if sw:
            notify_support(subject, plain, sw, html_body=html)
        raise RuntimeError(f"{subject}: {details}")

    # Step 4: 2+ distinct non-Water product refs → Multi-Compartment Tanker (all BUs).
    non_empty_product_refs = [
        ref for ref in (get_item_product_ref(item) or "" for item in items) if ref
    ]
    has_multiple_product_refs = len(set(non_empty_product_refs)) > 1
    if has_multiple_product_refs:
        logger.info(
            f"determine_equipment_type: {len(items)} non-Water item(s) with "
            f"{len(set(non_empty_product_refs))} distinct product refs -> Multi-Compartment Tanker"
        )
        return EQUIPMENT_TYPE_MAP["MULTI_COMPARTMENT"]

    # Step 5: Single non-Water item (or all same ref) with product equipment → use it.
    if len(equipment_by_key) == 1:
        equipment_type = next(iter(equipment_by_key.values()))
        maybe_set_preset_equipment_for_emulsion(order_data, equipment_type, base_url, headers, custom_fields, sw)
        logger.info(f"Product equipment resolved: {equipment_type.get('name')}")
        return equipment_type

    # Step 6: Default → Tanker.
    logger.info("Defaulting to Tanker equipment type")
    return EQUIPMENT_TYPE_MAP["TANKER"]
