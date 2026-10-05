"""Generic shipment stop helpers."""

from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from config import DEFAULT_SCHEDULING_TIMEZONE
from hfs_utils import get_location_reference_id, get_reference_value, normalize_custom_field_text


def get_stop_timezone(stop: Dict) -> str:
    """
    Return the IANA timezone string for a shipment stop.

    Shipwell stores timezone in two places depending on how the stop was created:
      - stop.location.timezone          (set when location comes from address book)
      - stop.location.address.timezone  (set when location is built from a raw address)

    This function checks both, preferring location.timezone and falling back to
    location.address.timezone, then DEFAULT_SCHEDULING_TIMEZONE.
    """
    location = stop.get("location") or {}
    tz = location.get("timezone")
    if not tz:
        address = location.get("address") or {}
        tz = address.get("timezone")
    return tz or DEFAULT_SCHEDULING_TIMEZONE


def is_pickup_stop(stop: Dict) -> bool:
    """Return True if a shipment stop is pickup-like."""
    stop_type = (stop.get("stop_type") or "").upper()
    return bool(stop.get("is_pickup") or stop_type == "PICKUP" or (not stop_type and not stop.get("is_dropoff")))


def is_dropoff_stop(stop: Dict) -> bool:
    """Return True if a shipment stop is delivery-like."""
    return bool(stop.get("is_dropoff") or (stop.get("stop_type") or "").upper() in ("DROPOFF", "DELIVERY"))


def stop_location_matches_order_stop(shipment_stop: Dict, order_stop: Dict) -> bool:
    """Compare shipment stop and order stop location/address-book ids or postal address.

    Match priority:
    1. Location reference ID (address book entry ID) — most reliable.
    1b. created_using_address_book_entry_id on shipment stop matched against
        ADDRESS_BOOK_ENTRY_ID on the order stop — handles the common case where
        shipment stop location is a clone with no references[] but carries the
        originating address book entry UUID.
    2. Lat/long proximity — catches cases where address fields are null/sparse but
       the order's geolocation and the stop's location coordinates agree (within ~100m).
    3. Location name / company name — normalized case-insensitive match.
    4. postal_code + address_1 fallback.
    """
    shipment_loc = get_location_reference_id(shipment_stop)
    order_loc = get_location_reference_id(order_stop)
    if shipment_loc and order_loc and shipment_loc == order_loc:
        return True

    # Tier 1b: match via ADDRESS_BOOK_ENTRY_REFERENCE_ID (the external reference / TMW location code,
    # e.g. "L134"). TMW stamps this on the order's ship_to/ship_from references as
    # ADDRESS_BOOK_ENTRY_REFERENCE_ID. The shipment stop carries the same value in
    # location.location_name (the address book entry's external_reference, which becomes
    # the stop's location_name when Shipwell clones the address book entry).
    # This is the most reliable consolidation signal — use it first.
    s_loc_for_ab = (shipment_stop.get("location") or {})
    s_ext_ref = (s_loc_for_ab.get("external_reference") or "").strip()
    s_loc_name_for_ext = (s_loc_for_ab.get("location_name") or "").strip()
    o_ext_ref = (get_reference_value(order_stop, ("ADDRESS_BOOK_ENTRY_REFERENCE_ID",)) or "").strip()
    if o_ext_ref:
        if (s_ext_ref and s_ext_ref == o_ext_ref) or (s_loc_name_for_ext and s_loc_name_for_ext == o_ext_ref):
            return True

    # Tier 1c: match shipment stop's created_using_address_book_entry_id against
    # the order stop's ADDRESS_BOOK_ENTRY_ID reference. Shipment stops are clones
    # of address book entries — the clone's location.created_using_address_book_entry_id
    # is the stable UUID that matches what TMW stamps on the order's references.
    s_created_using = s_loc_for_ab.get("created_using_address_book_entry_id")
    o_ab_id = get_reference_value(order_stop, ("ADDRESS_BOOK_ENTRY_ID",))
    if s_created_using and o_ab_id and s_created_using == o_ab_id:
        return True

    # Tier 2: lat/long proximity match (~100m tolerance, ~0.001 degrees)
    # Shipment stop: location.latitude / location.longitude
    # Order stop (corrogo): geolocation.latitude / geolocation.longitude
    _LAT_LON_TOLERANCE = 0.001  # ~111m at equator, sufficient for same-facility match
    s_loc = shipment_stop.get("location") or {}
    s_lat = s_loc.get("latitude")
    s_lon = s_loc.get("longitude")
    o_geo = order_stop.get("geolocation") or {}
    o_lat = o_geo.get("latitude") or order_stop.get("latitude")
    o_lon = o_geo.get("longitude") or order_stop.get("longitude")
    if s_lat is not None and s_lon is not None and o_lat is not None and o_lon is not None:
        if abs(float(s_lat) - float(o_lat)) <= _LAT_LON_TOLERANCE and \
           abs(float(s_lon) - float(o_lon)) <= _LAT_LON_TOLERANCE:
            return True

    # Tier 3: location name / company name match (normalized, case-insensitive)
    # Handles cases where address and lat/long are both null but location_name is reliable.
    # Shipment stop: location.location_name
    # Order stop: location_name or company_name (corrogo ship_from/ship_to)
    def _norm(s):
        return (s or "").strip().upper()

    s_loc_name = _norm(s_loc.get("location_name") or s_loc.get("company_name"))
    o_loc_name = _norm(
        order_stop.get("location_name")
        or order_stop.get("company_name")
        or (order_stop.get("location") or {}).get("location_name")
    )
    if s_loc_name and o_loc_name and s_loc_name == o_loc_name:
        return True

    # Tier 4: postal_code + address_1 (normalized, case-insensitive)
    # Shipment stop uses location.address.{postal_code, address_1}
    # Order stop (corrogo ship_from) uses top-level {postal_code, line_1}

    s_addr = s_loc.get("address") or {}
    s_postal = _norm(s_addr.get("postal_code") or s_loc.get("postal_code"))
    s_line1 = _norm(s_addr.get("address_1") or s_loc.get("address_1") or s_loc.get("line_1"))

    o_addr = order_stop.get("address") or {}
    o_postal = _norm(order_stop.get("postal_code") or o_addr.get("postal_code"))
    o_line1 = _norm(order_stop.get("line_1") or o_addr.get("address_1") or o_addr.get("line_1"))

    if s_postal and o_postal and s_postal == o_postal:
        if not s_line1 or not o_line1 or s_line1 == o_line1:
            return True

    return False


def get_pickup_stop(stops: List[Dict]) -> Optional[Dict]:
    """Return first stop where is_pickup is True."""
    for stop in stops:
        if is_pickup_stop(stop):
            return stop
    return None


def get_dropoff_stop(stops: List[Dict]) -> Optional[Dict]:
    """Return first stop where is_dropoff is True."""
    for stop in stops:
        if is_dropoff_stop(stop):
            return stop
    return None


def parse_window_endpoint(raw_value: object, timezone_str: str) -> Optional[datetime]:
    """Parse an order plan_window endpoint into a timezone-aware datetime."""
    text = normalize_custom_field_text(raw_value)
    if not text:
        return None
    try:
        if "T" in text:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo(timezone_str))
            return parsed.astimezone(ZoneInfo(timezone_str))
        parsed = datetime.fromisoformat(f"{text}T00:00:00").replace(tzinfo=ZoneInfo(timezone_str))
        return parsed
    except Exception:
        return None


def set_stop_window(stop: Dict, start_dt: datetime, end_dt: datetime) -> None:
    """Write local planned window fields onto a shipment stop."""
    timezone_str = get_stop_timezone(stop)
    tz = ZoneInfo(timezone_str)
    local_start = start_dt.astimezone(tz)
    local_end = end_dt.astimezone(tz)
    stop["planned_date"] = local_start.strftime("%Y-%m-%d")
    stop["planned_time_window_start"] = local_start.strftime("%H:%M:%S")
    stop["planned_time_window_end"] = local_end.strftime("%H:%M:%S")
