"""Shipwell product, address-book, and equipment lookup helpers."""

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger()

from config import EQUIPMENT_TYPE_MAP
from hfs_utils import get_custom_field_value, get_item_product_ref, normalize_custom_field_text
from shipwell_client import _api_call
from requests.exceptions import HTTPError

# Module-level cache for address-book entry lookups.
# Persists across warm Lambda invocations (execution context reuse).
# Only successful lookups are cached — failures are not stored so a
# transient API error doesn't poison the cache for the container's lifetime.
# Keyed on location_id (ULID or external_id string) → full entry dict.
_address_book_cache: Dict[str, Dict] = {}


def fetch_product_by_id(product_id: str, base_url: str, headers: Dict) -> Optional[Dict]:
    """Fetch a Shipwell product by UUID/id. Returns None on 404 (product not in this environment)."""
    if not product_id:
        return None
    try:
        return _api_call(f"{base_url}/v2/products/{product_id}/", "GET", headers=headers)
    except HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def fetch_product_by_ref(product_ref: str, base_url: str, headers: Dict) -> Optional[Dict]:
    """Search for a product by HFS product reference."""
    if not product_ref:
        return None
    resp = _api_call(f"{base_url}/v2/products/?q={product_ref}", "GET", headers=headers)
    results = (resp or {}).get("results") or (resp or {}).get("data") or []
    for product in results:
        refs = product.get("references") or []
        if any(str(ref.get("value", "")).strip() == product_ref for ref in refs):
            return product
        if str(product.get("product_ref", "")).strip() == product_ref:
            return product
    return None  # no exact reference match found — do not fall back to results[0]


def get_product_for_item(item: Dict, base_url: str, headers: Dict) -> Optional[Dict]:
    """Resolve an order item to a Shipwell product by product_id first, then PRODUCT REF."""
    product_id = (item.get("shipping_requirements") or {}).get("product_id")
    if product_id:
        product = fetch_product_by_id(str(product_id), base_url, headers)
        if product:
            logger.debug(f"get_product_for_item: found by product_id={product_id} -> id={product.get('id')}")
            return product
        logger.debug(f"get_product_for_item: product_id={product_id} not found (404 or None) — falling back to product_ref")

    product_ref = get_item_product_ref(item)
    if product_ref:
        product = fetch_product_by_ref(product_ref, base_url, headers)
        if product:
            logger.debug(f"get_product_for_item: found by product_ref={product_ref!r} -> id={product.get('id')}")
        else:
            logger.warning(f"get_product_for_item: product_ref={product_ref!r} not found in catalog at {base_url}")
        return product
    logger.warning(f"get_product_for_item: item has no product_id or product_ref — cannot resolve product")
    return None


def get_product_custom_value(product: Dict, custom_fields: Dict, field_key: str) -> Optional[Any]:
    """Read a product custom field, with simple top-level fallbacks."""
    field_id = custom_fields.get(field_key, "")
    product_id = product.get("id", "?")
    if field_id:
        sw_custom = (product.get("custom_data") or {}).get("shipwell_custom_data") or {}
        for resource_type in ("product", "products"):
            fields = sw_custom.get(resource_type)
            if isinstance(fields, dict) and field_id in fields:
                return fields[field_id]
        # Log what we actually found so mismatches are diagnosable
        if sw_custom:
            logger.debug(
                f"get_product_custom_value: field_key={field_key!r} field_id={field_id!r} not in "
                f"custom_data for product {product_id}. Available resource_types: {list(sw_custom.keys())}"
            )
        else:
            logger.debug(
                f"get_product_custom_value: product {product_id} has no custom_data.shipwell_custom_data "
                f"(custom_data={product.get('custom_data')!r})"
            )

    for fallback_key in (field_key, field_key.replace("_", "-"), field_key.replace("_", " ")):
        if fallback_key in product:
            return product.get(fallback_key)
    return None


def get_address_book_custom_value(entry: Dict, custom_fields: Dict, field_key: str) -> Optional[Any]:
    """Read an address book custom field, with simple top-level fallbacks.

    Searches all known resource_type namespaces used by Shipwell custom fields
    on address book entries, including shipment_stop and purchase_order_stop
    (which is where HFS stores address-book-level stop custom fields such as
    additional_transit_time).
    """
    field_id = custom_fields.get(field_key, "")
    if field_id:
        sw_custom = (entry.get("custom_data") or {}).get("shipwell_custom_data") or {}
        for resource_type in (
            "address_book_entry",
            "address_book",
            "location",
            "stop",
            "shipment_stop",
            "purchase_order_stop",
        ):
            fields = sw_custom.get(resource_type)
            if isinstance(fields, dict) and field_id in fields:
                return fields[field_id]

    for fallback_key in (field_key, field_key.replace("_", "-"), field_key.replace("_", " ")):
        if fallback_key in entry:
            return entry.get(fallback_key)
    return None


def normalize_equipment_type_value(raw_equipment: Any) -> Optional[Dict[str, Any]]:
    """Normalize product/order equipment values into the shape Shipwell shipment PUTs expect."""
    if not raw_equipment:
        return None
    if isinstance(raw_equipment, dict):
        if raw_equipment.get("machine_readable") or raw_equipment.get("id") or raw_equipment.get("name"):
            return raw_equipment
        raw_equipment = raw_equipment.get("value")
    text = normalize_custom_field_text(raw_equipment)
    if not text:
        return None
    key = text.upper().replace("-", "_").replace("/", "_").replace(" ", "_")
    mapped = EQUIPMENT_TYPE_MAP.get(key)
    if mapped:
        return mapped
    return {"machine_readable": key, "name": text}


def equipment_identity(equipment_type: Dict[str, Any]) -> str:
    """Return a stable comparison key for equipment type dictionaries."""
    return normalize_custom_field_text(
        equipment_type.get("machine_readable") or equipment_type.get("id") or equipment_type.get("name")
    ).upper()


_UUID_RE = __import__('re').compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    __import__('re').IGNORECASE,
)


def _fetch_address_book_by_uuid(uuid: str, base_url: str, headers: Dict) -> Optional[Dict]:
    """Direct GET /v2/address-book/<uuid>/ — exact, fast, no full-text scan."""
    if uuid in _address_book_cache:
        return _address_book_cache[uuid]
    try:
        entry = _api_call(f"{base_url}/v2/address-book/{uuid}/", "GET", headers=headers)
        if entry and entry.get("id"):
            _address_book_cache[uuid] = entry
            return entry
    except Exception as exc:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            f"fetch_address_book_entry: UUID direct-fetch failed for {uuid!r}: {exc}"
        )
    return None


def fetch_address_book_entry(location_id: str, base_url: str, headers: Dict) -> Optional[Dict]:
    """Fetch an address book entry by HFS/Shipwell location id or UUID.

    If location_id looks like a UUID, use the direct GET /v2/address-book/<uuid>/
    endpoint (exact and fast). Otherwise fall back to the ?q= search.

    Results are cached in a module-level dict for the lifetime of the Lambda
    execution context (warm invocation reuse). Only successful lookups are
    cached — failures fall through so a transient API error doesn't poison
    the cache for the container's lifetime.
    """
    if not location_id:
        return None
    if location_id in _address_book_cache:
        import logging as _logging
        _logging.getLogger(__name__).debug(
            f"fetch_address_book_entry: cache hit location_id={location_id!r}"
        )
        return _address_book_cache[location_id]

    # UUID path: direct fetch — exact, no full-text scan
    if _UUID_RE.match(location_id):
        return _fetch_address_book_by_uuid(location_id, base_url, headers)

    # Numeric/string external reference path: search via ?q=
    resp = _api_call(f"{base_url}/v2/address-book/?q={location_id}", "GET", headers=headers)
    results = (resp or {}).get("results") or (resp or {}).get("data") or []
    entry = None
    for r in results:
        if str(r.get("id", "")) == location_id or str(r.get("external_id", "")) == location_id or str(r.get("external_reference", "")) == location_id:
            entry = r
            break
    if entry is None and results:
        entry = results[0]
    if entry is not None:
        _address_book_cache[location_id] = entry  # cache successes only
        import logging as _logging
        _logging.getLogger(__name__).debug(
            f"fetch_address_book_entry: cached location_id={location_id!r} "
            f"-> id={entry.get('id')} external_id={entry.get('external_id')}"
        )
    return entry
