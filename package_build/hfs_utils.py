"""Pure data helpers for HFS order and shipment payloads."""

import copy
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger()


def get_custom_field_value(data: Dict, field_id: str) -> Optional[str]:
    """
    Extract a custom field value from order or shipment data.
    Checks purchase_order path first, then shipment path.
    """
    if not field_id:
        return None
    sw_custom = (data.get("custom_data") or {}).get("shipwell_custom_data") or {}
    for key in ("purchase_order", "shipment"):
        fields = sw_custom.get(key)
        if isinstance(fields, dict):
            val = fields.get(field_id)
            if val is not None:
                return val
    return None


def set_custom_field_value(data: Dict, resource_type: str, field_id: str, value: Any) -> Dict:
    """
    Return a copy of data with a Shipwell custom field set.
    resource_type is usually "purchase_order" or "shipment".
    """
    if not field_id:
        return copy.deepcopy(data)

    updated = copy.deepcopy(data)
    custom_data = updated.setdefault("custom_data", {})
    sw_custom = custom_data.setdefault("shipwell_custom_data", {})
    fields = sw_custom.setdefault(resource_type, {})
    fields[field_id] = value
    return updated


def normalize_custom_field_text(value: Any) -> str:
    """Normalize a custom field value to a trimmed string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Y" if value else "N"
    return str(value).strip()


def custom_field_is_truthy(value: Any, true_values: Tuple[str, ...] = ("Y", "X", "TRUE", "1")) -> bool:
    """Return True when a Shipwell/SAP-style flag value is enabled."""
    return normalize_custom_field_text(value).upper() in true_values


def get_reference_value(entity: Dict, qualifiers: Tuple[str, ...]) -> Optional[str]:
    """Find the first matching reference value on an order, item, or stop object."""
    wanted = {q.upper() for q in qualifiers}
    for ref in entity.get("references") or []:
        qualifier = str(ref.get("qualifier", "")).upper()
        if qualifier in wanted:
            value = ref.get("value")
            return str(value).strip() if value is not None else None
    return None


def get_item_product_ref(item: Dict) -> Optional[str]:
    """Extract the PRODUCT REF value from an order item using common Shipwell shapes."""
    ref = get_reference_value(item, ("PRODUCT REF", "PRODUCT_REF", "PRODUCT_REFERENCE", "Product Ref"))
    if ref:
        return ref

    shipping_requirements = item.get("shipping_requirements") or {}
    for key in ("product_ref", "product_reference", "product_id"):
        value = shipping_requirements.get(key)
        if value:
            return str(value).strip()
    return None


def get_order_product_refs(order_data: Dict) -> List[str]:
    """Return product refs from all order items, preserving order and removing duplicates."""
    refs: List[str] = []
    seen = set()
    for item in order_data.get("items") or []:
        product_ref = get_item_product_ref(item)
        if product_ref and product_ref not in seen:
            refs.append(product_ref)
            seen.add(product_ref)
    return refs


# Module-level cache for address-book entry external_reference lookups.
# Persists across warm Lambda invocations (execution context reuse).
# Only successful lookups are cached — failures are not stored so a
# transient API error doesn't poison the cache for the container's lifetime.
_ab_entry_cache: Dict[str, str] = {}


def get_location_reference_id(
    stop: Dict,
    base_url: Optional[str] = None,
    headers: Optional[Dict] = None,
) -> Optional[str]:
    """
    Extract the business location/address-book reference used by HFS stop custom fields.

    Check order:
    1. Explicit references on the stop (ADDRESS_BOOK_ENTRY_ID, LOCATION_ID, STOP LOCATION ID)
    2. Stable fields on location: address_book_entry_id, addressbook_entry_id, external_id
    3. If location has created_using_address_book_entry_id, look up that address book entry
       via the Shipwell API and return its external_reference (e.g. "4000").
    4. Fall back to location.id (raw UUID — last resort).
    """
    ref = get_reference_value(stop, ("ADDRESS_BOOK_ENTRY_ID", "LOCATION_ID", "STOP LOCATION ID"))
    if ref:
        return ref
    # address_book_entry_reference_id may appear at top-level stop or on
    # stop.location (dashboard API returns it on location; full GET may vary).
    # This is the stable HFS location number (e.g. "2500" for Tulsa West Refinery).
    top_level_ref = stop.get("address_book_entry_reference_id")
    if top_level_ref:
        return str(top_level_ref).strip()
    location = stop.get("location") or {}
    location_ref = location.get("address_book_entry_reference_id")
    if location_ref:
        return str(location_ref).strip()
    for key in ("address_book_entry_id", "addressbook_entry_id", "external_id"):
        value = location.get(key)
        if value:
            return str(value).strip()

    # Check created_using_address_book_entry_id — the stop location is a per-shipment
    # clone whose `id` is a random UUID. The original address book entry carries the
    # stable external_reference (e.g. "4000") that HAC_ASPHALT_LOCATIONS checks against.
    ab_entry_id = location.get("created_using_address_book_entry_id")
    if ab_entry_id and base_url and headers:
        # Return cached result if available (successes only — failures are not
        # cached so a transient API error doesn't poison the warm container).
        if ab_entry_id in _ab_entry_cache:
            logger.debug(
                f"get_location_reference_id: cache hit ab_entry_id={ab_entry_id!r} "
                f"-> {_ab_entry_cache[ab_entry_id]!r}"
            )
            return _ab_entry_cache[ab_entry_id]
        try:
            import requests as _requests
            url = f"{base_url}/v2/address-book/{ab_entry_id}/"
            resp = _requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            external_ref = resp.json().get("external_reference")
            if external_ref:
                result = str(external_ref).strip()
                _ab_entry_cache[ab_entry_id] = result  # cache successes only
                logger.debug(
                    f"get_location_reference_id: resolved ab_entry_id={ab_entry_id!r} "
                    f"-> external_reference={result!r} (cached)"
                )
                return result
        except Exception as exc:
            logger.warning(
                f"get_location_reference_id: failed to look up address book entry "
                f"{ab_entry_id!r}: {exc} — not caching, will retry on next call"
            )

    # Last resort: raw location UUID (not a stable business key)
    raw_id = location.get("id")
    if raw_id:
        return str(raw_id).strip()
    return None


def parse_charge_code(raw_charge: Any) -> Optional[Dict[str, Any]]:
    """
    Parse accessorial syntax like HOSE or HOSE=20.
    Returns {'code': 'HOSE', 'quantity': 20} or None for empty values.
    """
    text = normalize_custom_field_text(raw_charge)
    if not text:
        return None

    if "=" in text:
        code, qty_text = text.split("=", 1)
        code = code.strip().upper()
        qty_text = qty_text.strip()
        try:
            quantity: Any = float(qty_text)
            if quantity.is_integer():
                quantity = int(quantity)
        except ValueError:
            quantity = qty_text or 1
    else:
        code = text.upper()
        quantity = 1

    if not code:
        return None
    return {"code": code, "quantity": quantity}


def split_charge_list(raw_value: Any) -> List[str]:
    """Split comma, semicolon, newline, or pipe separated accessorial values."""
    text = normalize_custom_field_text(raw_value)
    if not text:
        return []
    return [part.strip() for part in re.split(r"[,;\n|]+", text) if part.strip()]


def normalize_charge_list(raw_values: List[Any]) -> List[Dict[str, Any]]:
    """Parse and dedupe charge values by code while preserving first quantity seen."""
    charges: List[Dict[str, Any]] = []
    seen = set()
    for raw in raw_values:
        for part in split_charge_list(raw):
            parsed = parse_charge_code(part)
            if not parsed:
                continue
            code = parsed["code"]
            if code in seen:
                continue
            charges.append(parsed)
            seen.add(code)
    return charges


def format_stop_address(stop: Dict) -> str:
    """Return a compact human-readable stop address."""
    location = stop.get("location") or stop
    address = location.get("address") or {}
    if isinstance(address, str):
        return address
    parts = [
        address.get("address_1") or location.get("address_1"),
        address.get("city") or location.get("city"),
        address.get("state_province") or location.get("state_province"),
        address.get("postal_code") or location.get("postal_code"),
    ]
    return ", ".join(str(part) for part in parts if part)


def get_location_display_name(stop: Dict) -> str:
    """Return the best human-readable location name for a stop.

    For shipment stops: prefers location_name, then company_name.
    For order stops (ship_from/ship_to): prefers location_name, then company_name.
    Falls back to external_reference / location id only as a last resort.
    """
    location = stop.get("location") or stop
    # location_name == external_reference for HFS, so this is the canonical display value
    loc_name = location.get("location_name") or ""
    if loc_name:
        return loc_name
    company = location.get("company_name") or ""
    if company:
        return company
    # Last resort: try the stable external reference id (not raw UUID if avoidable)
    ext_ref = get_location_reference_id(stop)
    return ext_ref or ""


def format_stop_lines(stop: Dict) -> List[str]:
    """Format stop details for support messages."""
    location = stop.get("location") or {}
    loc_name = get_location_display_name(stop)
    company_name = location.get("company_name") or ""
    return [
        f"  Stop Number: {stop.get('ordinal_index', stop.get('sequence_number', ''))}",
        f"  Location Name: {loc_name}",
        f"  Company Name: {company_name}",
        f"  Address: {format_stop_address(stop)}",
        (
            "  Planned Windows: "
            f"{stop.get('planned_date', '')} "
            f"{stop.get('planned_time_window_start', '')}-"
            f"{stop.get('planned_time_window_end', '')} "
            f"{location.get('timezone', '')}"
        ),
    ]


def format_item_lines(items: List[Dict]) -> List[str]:
    """Format order or shipment item details."""
    if not items:
        return ["  None"]

    lines: List[str] = []
    for index, item in enumerate(items, start=1):
        weight = item.get("weight") or item.get("total_weight") or {}
        if isinstance(weight, dict):
            weight_text = f"{weight.get('value', '')} {weight.get('unit', '')}".strip()
        else:
            weight_text = str(weight)
        lines.extend([
            f"  Item Number: {item.get('item_number', item.get('id', index))}",
            f"  Product Category: {item.get('product_category', item.get('category', ''))}",
            f"  Product Reference: {get_item_product_ref(item) or ''}",
            f"  Product Name: {item.get('description', item.get('product_name', ''))}",
            f"  Weight: {weight_text}",
            f"  Quantity Handling Unit: {item.get('quantity', item.get('packaging_quantity', ''))}",
        ])
    return lines


def get_shipment_product_category(shipment_data: Dict) -> Optional[str]:
    """Return the product_category of the first line item on the shipment."""
    line_items = shipment_data.get("line_items") or []
    for item in line_items:
        cat = item.get("product_category")
        if cat:
            return str(cat).strip()
    return None


# ---------------------------------------------------------------------------
# HTML email helpers
# ---------------------------------------------------------------------------

_HTML_STYLE = """
<style>
  body { font-family: Arial, sans-serif; font-size: 14px; color: #222; margin: 0; padding: 20px; background: #f5f5f5; }
  .card { background: #fff; border-radius: 6px; padding: 24px; max-width: 700px; margin: 0 auto; box-shadow: 0 1px 4px rgba(0,0,0,0.1); }
  .error-banner { background: #fdecea; border-left: 4px solid #d32f2f; border-radius: 4px; padding: 12px 16px; margin-bottom: 20px; }
  .error-banner p { margin: 0; color: #b71c1c; font-weight: bold; }
  .error-banner .detail { font-weight: normal; color: #333; margin-top: 6px; }
  h2 { font-size: 16px; color: #444; margin: 20px 0 8px; border-bottom: 1px solid #e0e0e0; padding-bottom: 4px; }
  table { width: 100%; border-collapse: collapse; margin-bottom: 16px; }
  th { text-align: left; background: #f0f0f0; padding: 8px 10px; font-size: 13px; color: #555; border: 1px solid #ddd; }
  td { padding: 7px 10px; border: 1px solid #ddd; font-size: 13px; vertical-align: top; }
  td.label { width: 38%; color: #555; font-weight: bold; background: #fafafa; }
  tr:nth-child(even) td { background: #f9f9f9; }
  tr:nth-child(even) td.label { background: #f3f3f3; }
  .section-header { background: #1a73e8; color: #fff; font-weight: bold; text-align: center; padding: 6px 10px; font-size: 13px; }
  .hac-banner { background: #e8f0fe; border-left: 4px solid #1a73e8; border-radius: 4px; padding: 12px 16px; margin-bottom: 20px; }
  .hac-banner p { margin: 0; font-weight: bold; color: #1a237e; }
  .footer { margin-top: 20px; font-size: 12px; color: #999; text-align: center; }
</style>
"""


def _h(value: Any) -> str:
    """HTML-escape a value for safe insertion."""
    import html as _html
    return _html.escape(str(value) if value is not None else "")


def _kv_row(label: str, value: Any) -> str:
    return f'<tr><td class="label">{_h(label)}</td><td>{_h(value)}</td></tr>'


def _section(title: str) -> str:
    return f'<tr><td colspan="2" class="section-header">{_h(title)}</td></tr>'


def _stop_rows(stop: Dict) -> str:
    location = stop.get("location") or {}
    loc_name = get_location_display_name(stop)
    company_name = location.get("company_name") or ""
    stop_num = stop.get("ordinal_index", stop.get("sequence_number", ""))
    stop_type = "Pickup" if stop.get("is_pickup") else "Delivery"
    rows = [
        _section(f"Stop {stop_num} — {stop_type}" + (f": {loc_name}" if loc_name else "")),
        _kv_row("Location Name", loc_name),
        _kv_row("Company Name", company_name),
        _kv_row("Address", format_stop_address(stop)),
        _kv_row("Planned Windows",
            f"{stop.get('planned_date', '')} "
            f"{stop.get('planned_time_window_start', '')}–"
            f"{stop.get('planned_time_window_end', '')} "
            f"{location.get('timezone', '')}"
        ),
    ]
    appt = stop.get("appointment_window") or {}
    if appt.get("start"):
        rows.append(_kv_row("Appointment Window", f"{appt.get('start')} – {appt.get('end', '')}"))
    return "".join(rows)


def _item_rows(items: List[Dict]) -> str:
    if not items:
        return _section("Items") + '<tr><td colspan="2">None</td></tr>'
    rows = [_section("Items")]
    for i, item in enumerate(items, 1):
        weight = item.get("weight") or item.get("total_weight") or {}
        weight_text = f"{weight.get('value', '')} {weight.get('unit', '')}".strip() if isinstance(weight, dict) else str(weight)
        if i > 1:
            rows.append('<tr><td colspan="2" style="background:#e8eaf6;font-size:12px;padding:4px 10px;">Item {}</td></tr>'.format(i))
        rows += [
            _kv_row("Product Category", item.get("product_category", item.get("category", ""))),
            _kv_row("Product Reference", get_item_product_ref(item) or ""),
            _kv_row("Product Name", item.get("description", item.get("product_name", ""))),
            _kv_row("Weight", weight_text),
            _kv_row("Quantity / Handling Unit", item.get("quantity", item.get("packaging_quantity", ""))),
        ]
    return "".join(rows)


def _wrap_html(title: str, table_rows: str, error_detail: str = "", hac_context: str = "") -> str:
    error_block = ""
    if error_detail:
        # Convert newlines to <br> so multi-line detail/resolution text renders correctly in HTML.
        error_detail_html = _h(error_detail).replace("\n", "<br>")
        error_block = f"""
        <div class="error-banner">
          <p>⚠️ Error Detail</p>
          <p class="detail">{error_detail_html}</p>
        </div>"""
    hac_block = ""
    if hac_context:
        hac_block = f"""
        <div class="hac-banner">
          <p>🗂 HAC Carrier Capacity Context</p>
          <p style="margin:8px 0 0;font-weight:normal;font-size:13px;color:#1a237e;">{hac_context}</p>
        </div>"""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">{_HTML_STYLE}</head>
<body><div class="card">
  <h1 style="font-size:18px;color:#d32f2f;margin:0 0 16px;">{_h(title)}</h1>
  {hac_block}
  {error_block}
  <table>{table_rows}</table>
  <div class="footer">HFS Shipwell Lambda &mdash; automated notification</div>
</div></body></html>"""


def build_order_email_body(order_data: Dict, error_detail: str = "", issue_type: str = "Order Issue") -> str:
    """Build plain-text order email body (used as fallback)."""
    ship_from = order_data.get("ship_from") or {}
    ship_to = order_data.get("ship_to") or {}
    custom_data = (order_data.get("custom_data") or {}).get("shipwell_custom_data") or {}
    po_fields = custom_data.get("purchase_order") or {}
    origin_name = get_location_display_name(ship_from)
    dest_name = get_location_display_name(ship_to)
    lines = [
        f"Issue Type: {issue_type}",
        f"Order Number: {order_data.get('order_number', '')}",
        f"Business Unit: {po_fields.get('business_unit', '')}",
        f"Origin: {origin_name}",
        f"Origin Address: {format_stop_address(ship_from)}",
        f"Destination: {dest_name}",
        f"Destination Address: {format_stop_address(ship_to)}",
        "",
        f"Split Order Id: {get_reference_value(order_data, ('SPLIT_ORDER_ID', 'Split Order Id')) or po_fields.get('split_order_id', '')}",
        f"Turn Order Number: {po_fields.get('turn_order_number', '')}",
        f"Order Source: {po_fields.get('order_source', '')}",
        f"Preset SCAC: {po_fields.get('preset_scac', '')}",
        f"Preset Equipment: {po_fields.get('preset_equipment', '')}",
        f"Auto Schedule: {po_fields.get('auto_schedule', '')}",
        "",
        "Items:",
    ]
    lines.extend(format_item_lines(order_data.get("items") or []))
    if error_detail:
        lines += ["", f"Error Detail: {error_detail}"]
    return "\n".join(lines)


def build_order_email_html(order_data: Dict, subject: str, error_detail: str = "", issue_type: str = "Order Issue") -> str:
    """Build HTML order email body."""
    ship_from = order_data.get("ship_from") or {}
    ship_to = order_data.get("ship_to") or {}
    custom_data = (order_data.get("custom_data") or {}).get("shipwell_custom_data") or {}
    po_fields = custom_data.get("purchase_order") or {}
    origin_name = get_location_display_name(ship_from)
    dest_name = get_location_display_name(ship_to)
    rows = [
        _section("Order Details"),
        _kv_row("Issue Type", issue_type),
        _kv_row("Order Number", order_data.get("order_number", "")),
        _kv_row("Business Unit", po_fields.get("business_unit", "")),
        _kv_row("Split Order Id", get_reference_value(order_data, ("SPLIT_ORDER_ID", "Split Order Id")) or po_fields.get("split_order_id", "")),
        _kv_row("Turn Order Number", po_fields.get("turn_order_number", "")),
        _kv_row("Order Source", po_fields.get("order_source", "")),
        _kv_row("Preset SCAC", po_fields.get("preset_scac", "")),
        _kv_row("Preset Equipment", po_fields.get("preset_equipment", "")),
        _kv_row("Auto Schedule", po_fields.get("auto_schedule", "")),
        _section("Origin"),
        _kv_row("Location Name", origin_name),
        _kv_row("Address", format_stop_address(ship_from)),
        _section("Destination"),
        _kv_row("Location Name", dest_name),
        _kv_row("Address", format_stop_address(ship_to)),
        _item_rows(order_data.get("items") or []),
    ]
    return _wrap_html(subject, "".join(rows), error_detail=error_detail)


def _get_shipment_origin_dest_names(shipment_data: Dict) -> tuple:
    """Return (origin_name, dest_name) from the shipment's stops list."""
    stops = shipment_data.get("stops") or []
    if not stops:
        return "", ""
    # Sort by ordinal_index / sequence_number to be safe
    def _stop_order(s):
        return s.get("ordinal_index") or s.get("sequence_number") or 0
    sorted_stops = sorted(stops, key=_stop_order)
    origin_name = get_location_display_name(sorted_stops[0]) if sorted_stops else ""
    dest_name = get_location_display_name(sorted_stops[-1]) if len(sorted_stops) > 1 else ""
    return origin_name, dest_name


def build_shipment_email_body(shipment_data: Dict, error_detail: str = "", issue_type: str = "Shipment Issue") -> str:
    """Build plain-text shipment email body (used as fallback)."""
    vendor = ((shipment_data.get("relationship_to_vendor") or {}).get("vendor") or {})
    carrier_name = vendor.get("name", "")
    carrier_scac = ""
    for code in vendor.get("identifying_codes") or []:
        if (code.get("type") or "").upper() == "SCAC":
            carrier_scac = code.get("value", "")
            break
    equipment = (shipment_data.get("equipment_type") or {}).get("machine_readable", "")
    custom_data = (shipment_data.get("custom_data") or {}).get("shipwell_custom_data") or {}
    ship_fields = custom_data.get("shipment") or {}
    origin_name, dest_name = _get_shipment_origin_dest_names(shipment_data)
    lines = [
        f"Issue Type: {issue_type}",
        f"Shipment Number: {shipment_data.get('reference_id', shipment_data.get('id', ''))}",
        f"Business Unit: {ship_fields.get('business_unit', '')}",
        f"Origin: {origin_name}",
        f"Destination: {dest_name}",
        "",
        f"BOL Number: {shipment_data.get('bol_number', '')}",
        f"Equipment Type: {equipment}",
        f"Carrier: {carrier_name}" + (f" ({carrier_scac})" if carrier_scac else ""),
        f"Preset SCAC: {ship_fields.get('preset_scac', '')}",
        f"Turn Order Number: {ship_fields.get('turn_order_number', '')}",
        f"Split Order Id: {ship_fields.get('split_order_id', '')}",
        "", "Stops:",
    ]
    for stop in shipment_data.get("stops") or []:
        lines.extend(format_stop_lines(stop))
    lines += ["", "Items:"]
    lines.extend(format_item_lines(shipment_data.get("line_items") or shipment_data.get("items") or []))
    if error_detail:
        lines += ["", f"Error Detail: {error_detail}"]
    return "\n".join(lines)


def build_shipment_email_html(shipment_data: Dict, subject: str, error_detail: str = "", hac_context: str = "", issue_type: str = "Shipment Issue") -> str:
    """Build HTML shipment email body."""
    vendor = ((shipment_data.get("relationship_to_vendor") or {}).get("vendor") or {})
    carrier_name = vendor.get("name", "")
    carrier_scac = ""
    for code in vendor.get("identifying_codes") or []:
        if (code.get("type") or "").upper() == "SCAC":
            carrier_scac = code.get("value", "")
            break
    equipment = (shipment_data.get("equipment_type") or {}).get("machine_readable", "")
    custom_data = (shipment_data.get("custom_data") or {}).get("shipwell_custom_data") or {}
    ship_fields = custom_data.get("shipment") or {}
    origin_name, dest_name = _get_shipment_origin_dest_names(shipment_data)
    rows = [
        _section("Shipment Details"),
        _kv_row("Issue Type", issue_type),
        _kv_row("Shipment Number", shipment_data.get("reference_id", shipment_data.get("id", ""))),
        _kv_row("Business Unit", ship_fields.get("business_unit", "")),
        _kv_row("Origin", origin_name),
        _kv_row("Destination", dest_name),
        _kv_row("BOL Number", shipment_data.get("bol_number", "")),
        _kv_row("Equipment Type", equipment),
        _kv_row("Carrier", carrier_name + (f" ({carrier_scac})" if carrier_scac else "")),
        _kv_row("Preset SCAC", ship_fields.get("preset_scac", "")),
        _kv_row("Turn Order Number", ship_fields.get("turn_order_number", "")),
        _kv_row("Split Order Id", ship_fields.get("split_order_id", "")),
    ]
    for stop in shipment_data.get("stops") or []:
        rows.append(_stop_rows(stop))
    rows.append(_item_rows(shipment_data.get("line_items") or shipment_data.get("items") or []))
    return _wrap_html(subject, "".join(rows), error_detail=error_detail, hac_context=hac_context)


def build_hac_email_body(
    shipment_data: Dict,
    scac: str = "",
    appointment_date: str = "",
    location_id: str = "",
    product_category: str = "",
    error_detail: str = "",
) -> str:
    """Plain-text HAC email body (fallback)."""
    lines = [
        "--- HAC Carrier Capacity Context ---",
        f"SCAC: {scac}",
        f"Stop 1 Location Name: {location_id}",
        f"Appointment Date: {appointment_date}",
        f"Product Category: {product_category}",
        "",
        "--- Shipment ---",
    ]
    lines.append(build_shipment_email_body(shipment_data, error_detail=error_detail, issue_type="Shipment Issue (HAC Capacity)"))
    return "\n".join(lines)


def notify_bodies_for_shipment(
    shipment_data: Dict, subject: str, error_detail: str = "", issue_type: str = "Shipment Issue"
) -> tuple:
    """Return (plain_body, html_body) for a shipment-focused notification."""
    return (
        build_shipment_email_body(shipment_data, error_detail=error_detail, issue_type=issue_type),
        build_shipment_email_html(shipment_data, subject, error_detail=error_detail, issue_type=issue_type),
    )


def notify_bodies_for_order(
    order_data: Dict, subject: str, error_detail: str = "", issue_type: str = "Order Issue"
) -> tuple:
    """Return (plain_body, html_body) for an order-focused notification."""
    return (
        build_order_email_body(order_data, error_detail=error_detail, issue_type=issue_type),
        build_order_email_html(order_data, subject, error_detail=error_detail, issue_type=issue_type),
    )


def notify_bodies_for_hac(
    shipment_data: Dict, subject: str,
    scac: str = "", appointment_date: str = "",
    location_id: str = "", product_category: str = "",
    error_detail: str = "",
) -> tuple:
    """Return (plain_body, html_body) for a HAC-capacity-focused notification."""
    return (
        build_hac_email_body(shipment_data, scac=scac, appointment_date=appointment_date,
                             location_id=location_id, product_category=product_category,
                             error_detail=error_detail),
        build_hac_email_html(shipment_data, subject, scac=scac, appointment_date=appointment_date,
                             location_id=location_id, product_category=product_category,
                             error_detail=error_detail),
    )


def build_hac_email_html(
    shipment_data: Dict,
    subject: str,
    scac: str = "",
    appointment_date: str = "",
    location_id: str = "",
    product_category: str = "",
    error_detail: str = "",
) -> str:
    """HTML HAC email body with capacity context banner."""
    hac_context = " &nbsp;|&nbsp; ".join(filter(None, [
        f"SCAC: {_h(scac)}" if scac else "",
        f"Location Name: {_h(location_id)}" if location_id else "",
        f"Appointment Date: {_h(appointment_date)}" if appointment_date else "",
        f"Product Category: {_h(product_category)}" if product_category else "",
    ]))
    return build_shipment_email_html(shipment_data, subject, error_detail=error_detail,
                                     hac_context=hac_context, issue_type="Shipment Issue (HAC Capacity)")
