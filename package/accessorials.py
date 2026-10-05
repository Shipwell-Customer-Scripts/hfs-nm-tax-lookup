"""Preplanned accessorial charge handling."""

import copy
import logging
import re
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from hfs_utils import (
    get_custom_field_value,
    get_location_reference_id,
    normalize_charge_list,
    normalize_custom_field_text,
)
from shipwell_client import _api_call, safe_update_shipment
from shipwell_resources import (
    fetch_address_book_entry,
    get_address_book_custom_value,
    get_product_custom_value,
    get_product_for_item,
)

# ULID guard: same pattern as scheduling.py — skip facility ULIDs passed to address-book search.
_ULID_RE = re.compile(r'^[0-9A-Z]{26}$')

# Module-level cache for the Shipwell accessorial code set.
# Populated lazily on first call to _get_valid_accessorial_codes().
# Keyed by base_url so the cache works correctly in tests that switch environments.
_VALID_ACCESSORIAL_CODES: Dict[str, set] = {}


def _looks_like_ulid(value: str) -> bool:
    return bool(_ULID_RE.match(str(value or '').upper()))


def _get_valid_accessorial_codes(base_url: str, headers: Dict) -> set:
    """Return the set of valid Shipwell accessorial charge codes.

    Fetches /v2/shipments/accessorials/ on first call per base_url and caches
    the result for the lifetime of the Lambda container.  Only codes with
    is_charge_code=True are included.
    """
    if base_url not in _VALID_ACCESSORIAL_CODES:
        try:
            data = _api_call(f"{base_url}/v2/shipments/accessorials/", "GET", headers=headers)
            codes = {
                item["code"].upper()
                for item in (data if isinstance(data, list) else [])
                if item.get("is_charge_code") and item.get("code")
            }
            _VALID_ACCESSORIAL_CODES[base_url] = codes
            logger.info(f"_get_valid_accessorial_codes: loaded {len(codes)} valid codes from {base_url}")
        except Exception as exc:
            logger.warning(f"_get_valid_accessorial_codes: failed to fetch accessorial list: {exc} — skipping validation")
            # Return empty set; callers treat empty set as "skip validation" to avoid blocking all charges.
            return set()
    return _VALID_ACCESSORIAL_CODES[base_url]


def get_preplanned_accessorial_value(order_data: Dict, custom_fields: Dict) -> Optional[str]:
    """Read the order Preplanned Accessorials custom field using common config keys."""
    for key in (
        "preplanned_accessorials",
        "preplanned_accessorial",
        "preplanned_charges",
        "accessorials",
    ):
        field_id = custom_fields.get(key, "")
        value = get_custom_field_value(order_data, field_id) if field_id else None
        if value:
            return value
    return None


def has_void_accessorial(order_data: Dict, custom_fields: Dict) -> bool:
    """Return True when Preplanned Accessorials contains VOID."""
    raw_accessorials = get_preplanned_accessorial_value(order_data, custom_fields)
    if not raw_accessorials:
        return False
    return any(charge["code"] == "VOID" for charge in normalize_charge_list([raw_accessorials]))


def collect_preplanned_accessorials(
    shipment_data: Dict, order_data: Dict, base_url: str, headers: Dict, custom_fields: Dict,
) -> List[Dict[str, Any]]:
    """Build the accessorial charge list from order, product, and delivery-location sources."""
    raw_values: List[Any] = []
    order_accessorials = get_preplanned_accessorial_value(order_data, custom_fields)
    if order_accessorials:
        raw_values.append(order_accessorials)

    for item in order_data.get("items") or []:
        product = get_product_for_item(item, base_url, headers)
        if not product:
            continue
        product_charges = get_product_custom_value(product, custom_fields, "product_charges")
        if product_charges:
            raw_values.append(product_charges)

    for stop in shipment_data.get("stops") or []:
        if not stop.get("is_dropoff") and (stop.get("stop_type") or "").upper() not in ("DROPOFF", "DELIVERY"):
            continue
        location_id = get_location_reference_id(stop)
        if not location_id:
            continue
        if _looks_like_ulid(location_id):
            continue  # Tempus facility ULID — not a valid address-book external reference
        entry = fetch_address_book_entry(location_id, base_url, headers)
        if not entry:
            continue
        delivery_charges = get_address_book_custom_value(entry, custom_fields, "delivery_charges")
        if delivery_charges:
            raw_values.append(delivery_charges)

    return normalize_charge_list(raw_values)


def build_custom_charge_line_item(charge: Dict[str, Any], calculated_rates: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build a charge line item dict for the carrier assignment PUT body.

    If calculated_rates is provided (a dict mapping accessorial code → rate info
    from the contract's accessorial charge table), the unit_amount is taken from
    the pre-calculated rate rather than the charge dict's own amount (which is
    typically 0 when sourced from the order custom field).
    """
    code = (charge.get("code") or "").upper()
    # Prefer the contract-calculated rate amount when available.
    if calculated_rates and code in calculated_rates:
        rate_info = calculated_rates[code]
        unit_amount = float(rate_info.get("calculated_rate_amount") or 0)
        unit_name = rate_info.get("calculation_description") or charge.get("name") or code
        unit_amount_currency = rate_info.get("calculated_rate_currency") or "USD"
    else:
        unit_amount = float(charge.get("amount", 0))
        unit_name = charge.get("name") or code
        unit_amount_currency = "USD"
    return {
        "charge_code": code,
        "unit_name": unit_name,
        "unit_quantity": float(charge.get("quantity", 1)),
        "unit_amount": unit_amount,
        "unit_amount_currency": unit_amount_currency,
        "category": "ACCESSORIAL",
    }


def get_existing_custom_charge_line_items(shipment_data: Dict) -> List[Dict[str, Any]]:
    """Return existing custom charge lines from known shipment payload shapes."""
    for key in ("custom_charge_line_items", "vendor_charge_line_items"):
        value = shipment_data.get(key)
        if isinstance(value, list):
            return value
    financials = shipment_data.get("financials") or {}
    value = financials.get("custom_charge_line_items")
    return value if isinstance(value, list) else []


def set_custom_charge_line_items(shipment_data: Dict, charge_lines: List[Dict[str, Any]]) -> Dict:
    """Write custom charge lines to known shipment payload shapes."""
    updated = copy.deepcopy(shipment_data)
    if "custom_charge_line_items" in updated or "vendor_charge_line_items" not in updated:
        updated["custom_charge_line_items"] = charge_lines
    if "vendor_charge_line_items" in updated:
        updated["vendor_charge_line_items"] = charge_lines
    if isinstance(updated.get("financials"), dict):
        updated["financials"]["custom_charge_line_items"] = charge_lines
    return updated


def _fetch_contract_accessorial_rates(contract_id: str, shipment_id: str, base_url: str, headers: Dict) -> Dict[str, Any]:
    """Fetch calculated accessorial rates from the contract's accessorial charge table.

    Returns a dict mapping accessorial code (uppercase) → rate info dict, e.g.:
      {"TANKWASH": {"calculated_rate_amount": 75, "calculated_rate_currency": "USD",
                    "calculation_description": "Tank Wash"}, ...}

    Returns an empty dict on any error so callers degrade gracefully to unit_amount=0.
    """
    if not contract_id or not shipment_id:
        return {}
    try:
        contract = _api_call(f"{base_url}/v2/contracts/{contract_id}/", "GET", headers=headers) or {}
        charge_table_id = contract.get("accessorial_charge_table")
        if not charge_table_id:
            logger.info(f"_fetch_contract_accessorial_rates: contract {contract_id} has no accessorial_charge_table — skipping rate lookup")
            return {}
        resp = _api_call(
            f"{base_url}/v2/quoting/rate-tables/accessorial-charge-tables/{charge_table_id}/calculate-accessorial-rates/",
            "POST",
            headers=headers,
            body={"shipment_id": shipment_id},
        ) or {}
        rates = {}
        for item in resp.get("accessorials") or []:
            code = (item.get("accessorial") or "").upper()
            if code:
                rates[code] = item
        logger.info(f"_fetch_contract_accessorial_rates: loaded {len(rates)} rates from charge table {charge_table_id}")
        return rates
    except Exception as exc:
        logger.warning(f"_fetch_contract_accessorial_rates: failed for contract {contract_id}: {exc} — falling back to unit_amount=0")
        return {}


def apply_preplanned_accessorials(
    shipment_data: Dict, order_data: Dict, base_url: str, headers: Dict,
    custom_fields: Dict, sw: "ShipwellProgram",
) -> Dict:
    """Apply preplanned/product/delivery accessorial charges to the carrier assignment.

    Charges are written to the carrier assignment's customer_charge_line_items via
    PUT /v2/shipments/{id}/carrier-assignments/{vendor_rel_id}/ — NOT to the base
    shipment — because contract rating owns that field on the relationship record.

    When the carrier assignment has a contract_id, the contract's accessorial charge
    table is queried to get pre-calculated rates (e.g. TANKWASH=$75). These rates are
    used as unit_amount on the charge line items instead of the default 0.
    """
    charges = collect_preplanned_accessorials(shipment_data, order_data, base_url, headers, custom_fields)
    if not charges:
        sw.log("TRACE", "No preplanned accessorials to apply", ["apply_preplanned_accessorials"])
        return shipment_data

    # Resolve the shipment ID and current carrier assignment relationship ID.
    shipment_id = shipment_data.get("id")
    if not shipment_id:
        sw.log("WARNING", "apply_preplanned_accessorials: no shipment id — skipping", ["apply_preplanned_accessorials"])
        return shipment_data

    rv = shipment_data.get("relationship_to_vendor") or {}
    vendor_rel_id = rv.get("id")
    if not vendor_rel_id:
        sw.log("WARNING", "apply_preplanned_accessorials: no carrier assignment (relationship_to_vendor.id missing) — skipping", ["apply_preplanned_accessorials"])
        return shipment_data

    cancel_or_vor_codes = {"CAA", "VOR"}
    clear_existing = any(charge["code"] in cancel_or_vor_codes for charge in charges)

    # Re-fetch the carrier assignment directly to get the freshest charge list.
    # The shipment_data passed in was fetched right after assign_contract, but contract
    # rating charges (LHS, fuel) may not be written yet — a short retry loop ensures
    # we see them before appending preplanned charges.
    fresh_rv = rv  # fall back to what we have if fetch fails
    if not clear_existing:
        for attempt in range(3):
            try:
                fresh_assignment = _api_call(
                    f"{base_url}/v2/shipments/{shipment_id}/carrier-assignments/{vendor_rel_id}/",
                    "GET", headers=headers,
                )
                # Always update fresh_rv so we pick up contract_id as soon as Shipwell
                # writes it (which happens before charges settle). Previously fresh_rv was
                # only updated when charges were found, causing contract_id to be null on
                # the first few attempts and _fetch_contract_accessorial_rates to be skipped.
                fresh_rv = fresh_assignment
                existing_charges = fresh_assignment.get("customer_charge_line_items") or []
                if existing_charges:
                    logger.info(f"apply_preplanned_accessorials: fetched {len(existing_charges)} existing charges on attempt {attempt + 1}")
                    break
                if attempt < 2:
                    logger.info(f"apply_preplanned_accessorials: no existing charges yet (attempt {attempt + 1}), retrying in 3s")
                    time.sleep(3)
            except Exception as fetch_err:
                logger.warning(f"apply_preplanned_accessorials: GET assignment failed: {fetch_err}")
                break

    # HFS preplanned accessorials use HFS-internal charge codes (e.g. PUMP, HOSE, CAA, VOR)
    # that do not exist in Shipwell's standard accessorial list. Skip Shipwell code validation
    # entirely for this flow — the codes are written as custom charge line items on the carrier
    # assignment and HFS owns their meaning downstream.
    validated_charges = [c for c in charges if (c.get("code") or "").strip()]

    if not validated_charges:
        sw.log("TRACE", "No preplanned accessorials to apply", ["apply_preplanned_accessorials"])
        return shipment_data

    # CAA/VOR are destructive: clear all existing financials (including contract-rated charges)
    # and replace with only the preplanned codes.  All other accessorial codes are additive —
    # they append to whatever contract rating already wrote.
    existing_lines = [] if clear_existing else (fresh_rv.get("customer_charge_line_items") or [])
    existing_codes = {
        normalize_custom_field_text(line.get("charge_code") or line.get("code")).upper()
        for line in existing_lines
    }

    # Idempotency guard: if all preplanned codes are already present with a non-zero
    # unit_amount, another concurrent invocation already wrote them — skip entirely.
    # This prevents duplicate charge line items when multiple charge_line_item.created
    # events (LHS, FSC, TTS) each trigger apply_preplanned_accessorials concurrently.
    if not clear_existing and validated_charges:
        needed_codes = {(c.get("code") or "").upper() for c in validated_charges if (c.get("code") or "").strip()}
        already_applied = {
            normalize_custom_field_text(line.get("charge_code") or line.get("code")).upper()
            for line in existing_lines
            if float(line.get("unit_amount") or 0) > 0
        }
        if needed_codes and needed_codes.issubset(already_applied):
            sw.log("TRACE", f"apply_preplanned_accessorials: idempotency guard — {needed_codes} already present at non-zero rate, skipping", ["apply_preplanned_accessorials"])
            logger.info(f"apply_preplanned_accessorials: idempotency guard — {needed_codes} already present, skipping duplicate write")
            return shipment_data

    # Fetch contract-calculated accessorial rates so we can populate unit_amount
    # instead of leaving it at 0. The contract_id is on the carrier assignment.
    contract_id = fresh_rv.get("contract_id") or rv.get("contract_id") or ""
    calculated_rates: Dict[str, Any] = {}
    if contract_id and shipment_id:
        calculated_rates = _fetch_contract_accessorial_rates(contract_id, shipment_id, base_url, headers)
        if calculated_rates:
            sw.log("TRACE", f"Fetched {len(calculated_rates)} calculated accessorial rates from contract {contract_id}",
                   ["apply_preplanned_accessorials"])

    new_lines = existing_lines[:]
    for charge in validated_charges:
        code = (charge.get("code") or "").upper()
        if not code or code in existing_codes:
            continue
        new_lines.append(build_custom_charge_line_item(charge, calculated_rates=calculated_rates))
        existing_codes.add(code)

    # PUT the carrier assignment with the updated charge list.
    # The API requires vendor, vendor_charge_line_items, and customer_charge_line_items.
    vendor = (fresh_rv.get("vendor") or rv.get("vendor") or {})
    put_body = {
        "vendor": {
            "id": vendor.get("id"),
            "name": vendor.get("name") or vendor.get("dba_name") or "",
            "primary_email": vendor.get("primary_email") or "",
        },
        "vendor_charge_line_items": fresh_rv.get("vendor_charge_line_items") or rv.get("vendor_charge_line_items") or [],
        "customer_charge_line_items": new_lines,
    }
    charge_summary = ", ".join(f"{charge['code']}={charge.get('quantity', 1)}" for charge in validated_charges)
    applied = False
    for put_attempt in range(3):
        try:
            _api_call(
                f"{base_url}/v2/shipments/{shipment_id}/carrier-assignments/{vendor_rel_id}/",
                "PUT", headers=headers, body=put_body,
            )
        except Exception as exc:
            sw.log("WARNING", f"Preplanned accessorials PUT failed (attempt {put_attempt + 1}): {exc}", ["apply_preplanned_accessorials"])
            logger.warning(f"apply_preplanned_accessorials PUT failed (attempt {put_attempt + 1}): {exc}")
            if put_attempt < 2:
                time.sleep(2)
            continue

        # Verify the write landed — guard against overwrites from concurrent processes
        # (e.g. NM tax script racing with preplanned accessorials).
        try:
            time.sleep(1)
            verification = _api_call(
                f"{base_url}/v2/shipments/{shipment_id}/carrier-assignments/{vendor_rel_id}/",
                "GET", headers=headers,
            )
            verified_codes = {
                normalize_custom_field_text(c.get("charge_code") or c.get("code") or "").upper()
                for c in (verification.get("customer_charge_line_items") or [])
            }
            expected_applied = {(charge.get("code") or "").upper() for charge in validated_charges}
            missing_after_put = expected_applied - verified_codes
            if not missing_after_put:
                applied = True
                break
            # Write was overwritten — rebuild put_body from verified state and retry
            logger.warning(
                f"apply_preplanned_accessorials: verification failed on attempt {put_attempt + 1} — "
                f"missing={sorted(missing_after_put)}, retrying PUT"
            )
            sw.log("WARNING",
                   f"Preplanned accessorials verification failed (attempt {put_attempt + 1}) — "
                   f"missing={sorted(missing_after_put)}, retrying",
                   ["apply_preplanned_accessorials"])
            # Re-read the freshest state and re-merge our charges on top
            verified_lines = verification.get("customer_charge_line_items") or []
            verified_existing_codes = {
                normalize_custom_field_text(c.get("charge_code") or c.get("code") or "").upper()
                for c in verified_lines
            }
            retry_new_lines = verified_lines[:]
            for charge in validated_charges:
                code = (charge.get("code") or "").upper()
                if code and code not in verified_existing_codes:
                    retry_new_lines.append(build_custom_charge_line_item(charge, calculated_rates=calculated_rates))
                    verified_existing_codes.add(code)
            put_body["customer_charge_line_items"] = retry_new_lines
            if put_attempt < 2:
                time.sleep(2)
        except Exception as verify_exc:
            logger.warning(f"apply_preplanned_accessorials: verification GET failed: {verify_exc}")
            applied = True  # assume success if we can't verify
            break

    if applied:
        sw.log("TRACE", f"Preplanned accessorials applied: {charge_summary}", ["apply_preplanned_accessorials"])
    else:
        sw.log("WARNING", f"Preplanned accessorials may not have persisted after 3 attempts: {charge_summary}", ["apply_preplanned_accessorials"])
        logger.warning(f"apply_preplanned_accessorials: gave up after 3 attempts — {charge_summary}")
    return shipment_data
