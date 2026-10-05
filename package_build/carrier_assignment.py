"""Carrier assignment and routing-guide helpers."""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from config import DRY_RUN, HAC_ASPHALT_LOCATIONS, HAC_ASSIGNMENT_SHEET_IDS, HAC_CAPACITY_INTEGRATION_ENABLED, ROUTING_GUIDE_POLICY_ID
from google_sheets_client import build_google_sheets_client
from hac_capacity import CapacityError, check_sheet_capacity, consume_capacity_row, detect_capacity_columns, find_date_row, get_assignment_sheet_id, normalize_date_value, row_update_range
from hfs_utils import (
    get_custom_field_value,
    get_location_reference_id,
    normalize_custom_field_text,
    notify_bodies_for_order,
    notify_bodies_for_hac,
    notify_bodies_for_shipment,
)
from notifications import notify_support
from shipwell_client import _api_call
from stops import get_dropoff_stop, get_pickup_stop

logger = logging.getLogger()


def _select_preferred_poc(pocs: list) -> Optional[Dict]:
    """
    Select the preferred vendor_point_of_contact from a carrier relationship's POC list.

    Priority order (to ensure SLU carrier dashboard users can see their shipments):
      1. POC with job_title="BILLING" and access_all_customer_shipments=True
      2. POC with job_title="BILLING" (any access_all value)
      3. Any POC with access_all_customer_shipments=True and a user set
      4. First POC in list (fallback)

    Rationale: the carrier-relationships API returns POCs in non-deterministic order
    depending on the query path (paginated scan vs q= search). Using job_title="BILLING"
    as the primary key gives a stable, intent-based selection that matches the
    confirmed-working manual fix (POC 4dc0681e, job_title=BILLING, access_all=True).
    """
    if not pocs:
        return None
    # Priority 1: BILLING + access_all
    for p in pocs:
        if (p.get("job_title", "").upper() == "BILLING"
                and p.get("access_all_customer_shipments")
                and p.get("user")):
            return p
    # Priority 2: BILLING alone (no access_all requirement)
    for p in pocs:
        if p.get("job_title", "").upper() == "BILLING" and p.get("user"):
            return p
    # Priority 3: any access_all=True with a user
    for p in pocs:
        if p.get("access_all_customer_shipments") and p.get("user"):
            return p
    # Fallback: first POC
    return pocs[0]


def should_omit_point_of_contact(order_data: Dict, suppress_notifications: bool = False) -> bool:
    """
    Determines whether vendor_point_of_contact should be omitted from carrier-config POST.
    IMPORTANT: vendor_point_of_contact must be omitted entirely (not set to null) or API returns 500.

    Previously this returned suppress_notifications, which also suppressed the POC for PPTAS/direct-assign
    shipments — preventing SLU carrier users from seeing those shipments on their load board.
    POC is now always included when available so carrier portal visibility is not broken.
    suppress_notifications only controls email/tender notification behavior, not POC assignment.
    """
    return False


def get_order_source(order_data: Dict, custom_fields: Dict, shipment_data: Optional[Dict] = None) -> str:
    """Read Order Source from configured custom fields.

    Reads from order_data first, then falls back to shipment_data when provided.
    SAP sometimes writes order_source to the shipment custom_data before the order
    custom_data is updated, so the fallback ensures we catch PPTAS/CRUDE correctly.
    """
    for key in ("order_source", "source_order", "source"):
        field_id = custom_fields.get(key, "")
        if not field_id:
            continue
        value = get_custom_field_value(order_data, field_id)
        if not value and shipment_data:
            value = get_custom_field_value(shipment_data, field_id)
        if value:
            return normalize_custom_field_text(value).upper()
    return ""


def get_business_unit_value(data: Dict, custom_fields: Dict) -> str:
    """Read Business Unit from order or shipment custom data.

    Checks order_business_unit field first (the order-level concatenated BU field),
    then falls back to business_unit (used for product-level BU lookup).
    """
    for key in ("order_business_unit", "business_unit"):
        field_id = custom_fields.get(key, "")
        if field_id:
            value = normalize_custom_field_text(get_custom_field_value(data, field_id))
            if value:
                return value
    return ""


def is_crude_business_unit(business_unit: str) -> bool:
    """Return True when the slash-delimited Business Unit value contains CRUDE."""
    bu_parts = {part.strip().upper() for part in business_unit.split("/") if part.strip()}
    return any(p.startswith("CRUDE") for p in bu_parts)


def should_direct_assign_preset_carrier(order_data: Dict, shipment_data: Dict, custom_fields: Dict) -> bool:
    """Preset SCAC direct-assignment rule from the HFS doc."""
    order_source = get_order_source(order_data, custom_fields, shipment_data=shipment_data)
    business_unit = get_business_unit_value(order_data, custom_fields) or get_business_unit_value(shipment_data, custom_fields)
    return order_source == "PPTAS" or is_crude_business_unit(business_unit)


def get_first_pickup_location_id(
    shipment_data: Dict,
    base_url: Optional[str] = None,
    headers: Optional[Dict] = None,
) -> Optional[str]:
    """Return Pickup Stop 1 Location Id / address book id."""
    pickup_stop = get_pickup_stop(shipment_data.get("stops") or [])
    return get_location_reference_id(pickup_stop or {}, base_url=base_url, headers=headers) if pickup_stop else None


def is_hac_asphalt_location(
    shipment_data: Dict,
    base_url: Optional[str] = None,
    headers: Optional[Dict] = None,
) -> bool:
    """Return True when Pickup Stop 1 is one of the HAC Asphalt capacity locations."""
    location_id = get_first_pickup_location_id(shipment_data, base_url=base_url, headers=headers)
    return bool(location_id and location_id in HAC_ASPHALT_LOCATIONS)


def get_shipment_product_category(shipment_data: Dict, order_data: Dict) -> str:
    """Best-effort product category for route-guide matching."""
    for item in shipment_data.get("line_items") or []:
        value = item.get("product_category") or item.get("category")
        if value:
            return normalize_custom_field_text(value)
    for item in order_data.get("items") or []:
        value = item.get("product_category") or item.get("category")
        if value:
            return normalize_custom_field_text(value)
    return ""


def filter_contract_matches(contracts: List[Dict], carrier_relationship_id: str) -> List[Dict]:
    """Return applicable contracts for a carrier relationship.

    Filters to contracts that:
      1. Belong to the given carrier_relationship_id
      2. Have status == 'ACTIVE'  (excludes PAUSED, EXPIRED, UPCOMING, etc.)
      3. Have valid dates: start_date <= today <= end_date (or end_date is None)

    The /v2/contracts/applicable-contracts/ API does NOT filter by contract
    status or date validity — it returns all matching contracts regardless of
    whether they are paused or date-expired. We enforce those constraints here
    so that a paused or expired contract never causes a false ambiguity error.
    """
    import datetime as _dt
    today = _dt.date.today()
    result = []
    for contract_wrapper in contracts:
        contract = contract_wrapper.get("contract") or {}
        if contract.get("carrier_relationship") != carrier_relationship_id:
            continue
        # Status must be ACTIVE
        status = (contract.get("status") or "").upper()
        if status and status != "ACTIVE":
            logger.info(
                f"filter_contract_matches: skipping contract {contract.get('id','?')!r} "
                f"name={contract.get('name','?')!r} — status={status} (not ACTIVE)"
            )
            continue
        # Date range: start_date <= today <= end_date (end_date may be None = open-ended)
        start_str = contract.get("start_date")
        end_str   = contract.get("end_date")
        if start_str:
            try:
                if _dt.date.fromisoformat(start_str) > today:
                    logger.info(
                        f"filter_contract_matches: skipping contract {contract.get('id','?')!r} "
                        f"name={contract.get('name','?')!r} — start_date {start_str} is in the future"
                    )
                    continue
            except ValueError:
                pass
        if end_str:
            try:
                if _dt.date.fromisoformat(end_str) < today:
                    logger.info(
                        f"filter_contract_matches: skipping contract {contract.get('id','?')!r} "
                        f"name={contract.get('name','?')!r} — end_date {end_str} is in the past"
                    )
                    continue
            except ValueError:
                pass
        result.append(contract_wrapper)
    return result


def require_exactly_one_contract(
    matches: List[Dict], shipment_data: Dict, preset_scac: str, sw: "ShipwellProgram",
) -> Dict:
    """Enforce the doc's exactly-one applicable-contract rule."""
    if len(matches) != 1:
        subject = f"Error Assigning Preset SCAC - {len(matches)} Available Contract(s) Found"
        detail = (
            f"Preset SCAC: {preset_scac}. "
            f"Expected exactly 1 matching contract but found {len(matches)}. "
            "Verify that exactly one active contract exists for this carrier, lane, and equipment type."
        )
        plain, html = notify_bodies_for_shipment(shipment_data, subject, error_detail=detail)
        notify_support(subject, plain, sw, html_body=html)
        raise RuntimeError(f"{subject} for {preset_scac}")
    return matches[0]


def _flatten_text(value: Any) -> str:
    """Flatten nested API objects into uppercase searchable text."""
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return str(value).upper()
    if isinstance(value, list):
        return " ".join(_flatten_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_flatten_text(item) for item in value.values())
    return str(value).upper()


def get_equipment_match_terms(shipment_data: Dict) -> List[str]:
    """Return equipment identifiers for route-guide matching."""
    equipment = shipment_data.get("equipment_type") or {}
    terms = []
    if isinstance(equipment, dict):
        for key in ("id", "machine_readable", "name"):
            if equipment.get(key) is not None:
                terms.append(str(equipment[key]).upper())
    elif equipment:
        terms.append(str(equipment).upper())
    return terms


def _is_stable_location_id(stop: Dict) -> bool:
    """
    Return True when the stop has a stable business-key location ID
    (address_book_entry_id, external_id, or a reference qualifier like
    ADDRESS_BOOK_ENTRY_ID / LOCATION_ID / STOP LOCATION ID).

    A raw `location.id` UUID is NOT a stable ID — it is a Shipwell-internal
    stop UUID that never appears in routing-guide lane address objects.
    """
    from hfs_utils import get_reference_value
    # Check references first (most explicit)
    ref = get_reference_value(stop, ("ADDRESS_BOOK_ENTRY_ID", "LOCATION_ID", "STOP LOCATION ID"))
    if ref:
        return True
    # address_book_entry_reference_id may appear at top-level stop or on
    # stop.location (dashboard API returns it on location; full GET may vary).
    if stop.get("address_book_entry_reference_id"):
        return True
    location = stop.get("location") or {}
    if location.get("address_book_entry_reference_id"):
        return True
    # address_book_entry_id or external_id are stable business keys
    for key in ("address_book_entry_id", "addressbook_entry_id", "external_id"):
        if location.get(key):
            return True
    return False


def _get_stop_postal_code(stop: Dict) -> Optional[str]:
    """Extract the postal code from a shipment stop location."""
    location = stop.get("location") or {}
    address = location.get("address") or {}
    return (
        address.get("postal_code")
        or location.get("postal_code")
        or None
    )


def _policy_postal_codes(policy: Dict, direction: str) -> List[str]:
    """
    Extract postal codes from a routing guide policy's origins or destinations array.
    direction must be 'origins' or 'destinations'.
    """
    return [
        entry["postal_code"]
        for entry in (policy.get(direction) or [])
        if entry.get("postal_code")
    ]


def route_guide_policy_matches(policy: Dict, shipment_data: Dict, order_data: Dict) -> bool:
    """
    Best-effort route-guide match for lane / equipment / product category.

    Strategy:
    1. If stable address-book IDs are present on both stops, use the existing
       ID-in-policy-text approach (handles policies that encode location IDs).
    2. Otherwise (no stable IDs — only raw Shipwell location.id UUIDs which
       never appear in routing-guide address objects), fall back to postal-code
       matching against the policy's `origins` and `destinations` arrays,
       combined with equipment-type and mode checks.
    """
    stops = shipment_data.get("stops") or []
    pickup_stop = get_pickup_stop(stops)
    dropoff_stop = get_dropoff_stop(stops)

    pickup_has_stable_id = bool(pickup_stop and _is_stable_location_id(pickup_stop))
    dropoff_has_stable_id = bool(dropoff_stop and _is_stable_location_id(dropoff_stop))

    if pickup_has_stable_id or dropoff_has_stable_id:
        # --- ID-based match (original behaviour) ---
        policy_text = _flatten_text(policy)
        pickup_id = get_first_pickup_location_id(shipment_data)
        dropoff_id = get_location_reference_id(dropoff_stop or {})
        product_category = get_shipment_product_category(shipment_data, order_data)
        equipment_terms = get_equipment_match_terms(shipment_data)

        logger.info(
            f"route_guide_policy_matches [ID-based]: checking policy '{policy.get('name')}' (id={policy.get('id')}) "
            f"against shipment terms: pickup_id={pickup_id!r}, dropoff_id={dropoff_id!r}, "
            f"product_category={product_category!r}, equipment_terms={equipment_terms}"
        )

        required_terms = [term for term in (pickup_id, dropoff_id, product_category) if term]
        missing_terms = [term for term in required_terms if str(term).upper() not in policy_text]
        if missing_terms:
            logger.info(
                f"route_guide_policy_matches [ID-based]: policy '{policy.get('name')}' — "
                f"missing required terms: {missing_terms} in policy text; falling through to postal-code match"
            )
            # Fall through to postal-code matching below — the policy may use addresses
            # rather than address-book IDs (e.g. policies created via the Shipwell UI).
        else:
            if equipment_terms and not any(term in policy_text for term in equipment_terms):
                logger.info(
                    f"route_guide_policy_matches [ID-based]: policy '{policy.get('name')}' REJECTED — "
                    f"no equipment match (shipment={equipment_terms}, none found in policy text)"
                )
                return False

            logger.info(
                f"route_guide_policy_matches [ID-based]: policy '{policy.get('name')}' MATCHED "
                f"(all terms found: {required_terms}, equipment: {equipment_terms})"
            )
            return True

    # --- Postal-code fallback ---
    # Reached when: (a) stops have no stable IDs, OR
    #               (b) stable IDs were present but not found in policy text
    #                   (policy was built with addresses, not address-book IDs).
    logger.debug(
        "route_guide_policy_matches: using postal-code / equipment / mode matching "
        f"for policy '{policy.get('name')}'"
    )

    origin_postal = _get_stop_postal_code(pickup_stop) if pickup_stop else None
    dest_postal = _get_stop_postal_code(dropoff_stop) if dropoff_stop else None

    if not origin_postal or not dest_postal:
        logger.debug(
            "route_guide_policy_matches: postal-code fallback skipped — "
            f"origin_postal={origin_postal!r} dest_postal={dest_postal!r}"
        )
        return False

    policy_origin_postals = _policy_postal_codes(policy, "origins")
    policy_dest_postals = _policy_postal_codes(policy, "destinations")

    # Match origin postal: policy origins must contain the shipment's origin postal,
    # OR the policy has no origins defined (wildcard — match any origin).
    if policy_origin_postals and origin_postal not in policy_origin_postals:
        logger.debug(
            f"route_guide_policy_matches: origin postal {origin_postal!r} "
            f"not in policy origins {policy_origin_postals} for '{policy.get('name')}'"
        )
        return False
    if not policy_origin_postals:
        logger.debug(
            f"route_guide_policy_matches: policy '{policy.get('name')}' has no origin postal codes — "
            "treating as wildcard origin"
        )

    # Tiebreaker: if the policy origin has an address_1, check it matches the shipment's
    # pickup address_1 (loose substring, uppercase). This disambiguates two policies that
    # share the same postal code but are at different physical addresses (e.g. two Tulsa
    # refineries at 74107).
    if policy_origin_postals and origin_postal in policy_origin_postals:
        shipment_origin_address = ""
        if pickup_stop:
            loc = pickup_stop.get("location") or {}
            addr = loc.get("address") or {}
            shipment_origin_address = (addr.get("address_1") or "").strip().upper()
        matching_policy_origins = [
            entry for entry in (policy.get("origins") or [])
            if entry.get("postal_code") == origin_postal
        ]
        for entry in matching_policy_origins:
            policy_addr = (entry.get("address_1") or "").strip().upper()
            if policy_addr and shipment_origin_address:
                if policy_addr not in shipment_origin_address and shipment_origin_address not in policy_addr:
                    logger.debug(
                        f"route_guide_policy_matches: origin address mismatch for '{policy.get('name')}' — "
                        f"policy='{policy_addr}' shipment='{shipment_origin_address}' (same postal {origin_postal!r})"
                    )
                    return False

    # Match destination postal: policy destinations must contain the shipment's dest postal,
    # OR the policy has no destinations defined (wildcard — match any destination).
    if policy_dest_postals and dest_postal not in policy_dest_postals:
        logger.debug(
            f"route_guide_policy_matches: dest postal {dest_postal!r} "
            f"not in policy destinations {policy_dest_postals} for '{policy.get('name')}'"
        )
        return False
    if not policy_dest_postals:
        logger.debug(
            f"route_guide_policy_matches: policy '{policy.get('name')}' has no destination postal codes — "
            "treating as wildcard destination"
        )

    # If both origin and destination are wildcards, we need at least the origin
    # postal to have matched something meaningful — skip fully-wildcard policies
    # that have neither origin nor destination postals (can't validate at all).
    if not policy_origin_postals and not policy_dest_postals:
        logger.debug(
            f"route_guide_policy_matches: postal fallback skipped for policy "
            f"'{policy.get('name')}' — no postal codes on either side (fully wildcard)"
        )
        return False

    # Equipment check
    equipment_terms = get_equipment_match_terms(shipment_data)
    policy_equipment = [
        str(eq.get("machine_readable") or eq.get("name") or eq.get("id") or "").upper()
        for eq in (policy.get("equipment_types") or [])
        if eq
    ]
    if equipment_terms and policy_equipment:
        if not any(term in policy_equipment for term in equipment_terms):
            logger.debug(
                f"route_guide_policy_matches: equipment mismatch "
                f"shipment={equipment_terms} policy={policy_equipment}"
            )
            return False

    # Mode check
    shipment_mode = shipment_data.get("mode") or {}
    shipment_mode_code = str(
        (shipment_mode.get("code") or shipment_mode.get("id") or shipment_mode)
        if shipment_mode else ""
    ).upper()
    policy_modes = [
        str(m.get("code") or m.get("id") or "").upper()
        for m in (policy.get("modes") or [])
        if m
    ]
    if shipment_mode_code and policy_modes:
        if shipment_mode_code not in policy_modes:
            logger.debug(
                f"route_guide_policy_matches: mode mismatch "
                f"shipment={shipment_mode_code!r} policy={policy_modes}"
            )
            return False

    logger.info(
        f"route_guide_policy_matches: MATCHED via postal codes "
        f"origin={origin_postal!r} dest={dest_postal!r} policy='{policy.get('name')}'"
    )
    return True


def _fetch_applicable_policies(shipment_id: str, base_url: str, headers: Dict) -> Optional[List[Dict]]:
    """
    Call GET /v2/shipments/{id}/applicable-policies/ and return the list of
    routing-guide policies (policy_type=ROUTING_GUIDE, workflow_id present).

    Returns None on any API error so the caller can fall back gracefully.
    """
    try:
        resp = _api_call(
            f"{base_url}/v2/shipments/{shipment_id}/applicable-policies/",
            "GET",
            headers=headers,
        )
        if resp is None:
            logger.warning("_fetch_applicable_policies: API returned None")
            return None
        # Response is a plain list (not paginated)
        if not isinstance(resp, list):
            logger.warning(f"_fetch_applicable_policies: unexpected response type {type(resp)} — {str(resp)[:200]}")
            return None
        rg_policies = [
            entry["policy"]
            for entry in resp
            if isinstance(entry, dict)
            and isinstance(entry.get("policy"), dict)
            and entry["policy"].get("policy_type") == "ROUTING_GUIDE"
            and entry["policy"].get("workflow_id")  # must have a workflow to initiate
            and entry["policy"].get("status") == "ACTIVE"
        ]
        logger.info(
            f"_fetch_applicable_policies: {len(resp)} total entries, "
            f"{len(rg_policies)} ROUTING_GUIDE with workflow: "
            + ", ".join(f"'{p.get('name')}' (id={p.get('id')})" for p in rg_policies)
        )
        return rg_policies
    except Exception as exc:
        logger.warning(f"_fetch_applicable_policies: error calling applicable-policies endpoint: {exc}")
        return None


def _find_matching_policy_fallback(
    shipment_data: Dict, order_data: Dict, base_url: str, headers: Dict,
) -> List[Dict]:
    """
    Fallback client-side policy matching using GET /v2/routing-guide/policies/.
    Used when applicable-policies returns 0 results or errors.
    Returns the list of matched policies (may be 0 or >1).
    """
    policies: List[Dict] = []
    page = 1
    while True:
        resp = _api_call(f"{base_url}/v2/routing-guide/policies/?page={page}&status=ACTIVE", "GET", headers=headers)
        results = (resp or {}).get("results") or (resp or {}).get("data") or []
        policies.extend(results)
        total_pages = (resp or {}).get("total_pages") or 1
        if page >= total_pages:
            break
        page += 1

    # Filter by start_date/end_date
    today = datetime.utcnow().date()
    date_filtered = []
    for p in policies:
        start = p.get("start_date")
        end = p.get("end_date")
        try:
            if start and datetime.strptime(start[:10], "%Y-%m-%d").date() > today:
                logger.info(f"_find_matching_policy_fallback: skipping '{p.get('name')}' — start_date {start} is in the future")
                continue
            if end and datetime.strptime(end[:10], "%Y-%m-%d").date() < today:
                logger.info(f"_find_matching_policy_fallback: skipping '{p.get('name')}' — end_date {end} is in the past")
                continue
        except Exception:
            pass
        date_filtered.append(p)
    policies = date_filtered

    logger.info(
        f"_find_matching_policy_fallback: {len(policies)} active policies after date filtering: "
        + ", ".join(f"'{p.get('name')}' (id={p.get('id')})" for p in policies)
    )
    matches = [p for p in policies if route_guide_policy_matches(p, shipment_data, order_data)]
    logger.info(
        f"_find_matching_policy_fallback: {len(matches)} matched: "
        + ", ".join(f"'{p.get('name')}' (id={p.get('id')})" for p in matches)
    )
    return matches


def find_matching_route_guide_policy(
    shipment_data: Dict, order_data: Dict, base_url: str, headers: Dict, sw: "ShipwellProgram",
) -> Dict:
    """
    Find exactly one active routing-guide policy that matches this shipment.

    Strategy:
    1. PRIMARY — GET /v2/shipments/{id}/applicable-policies/  (server-side match,
       same logic the Shipwell UI uses for the "Push to Routing Guide" dropdown).
       Filter to ROUTING_GUIDE entries with a workflow_id.
    2. FALLBACK — if the primary returns 0 results or errors, fall back to
       client-side matching against GET /v2/routing-guide/policies/?status=ACTIVE
       using route_guide_policy_matches().

    Raises RuntimeError (and sends a support notification) if not exactly 1 match.
    """
    shipment_id = shipment_data.get("id") or ""

    # --- PRIMARY: applicable-policies endpoint ---
    primary_matches: Optional[List[Dict]] = None
    if shipment_id:
        primary_matches = _fetch_applicable_policies(shipment_id, base_url, headers)
    else:
        logger.warning("find_matching_route_guide_policy: no shipment_id in shipment_data — skipping applicable-policies")

    if primary_matches is not None and len(primary_matches) == 1:
        logger.info(
            f"find_matching_route_guide_policy: PRIMARY match — '{primary_matches[0].get('name')}' "
            f"(id={primary_matches[0].get('id')}) via applicable-policies"
        )
        return primary_matches[0]

    if primary_matches is not None and len(primary_matches) > 1:
        # Multiple matches from the authoritative endpoint — error immediately.
        policy_list = "\n".join(
            f"  - {p.get('name')} (id: {p.get('id')})"
            for p in primary_matches
        )
        subject = f"Error Assigning Route Guide - {len(primary_matches)} Available Route Guide(s) Found"
        detail = (
            f"Expected exactly 1 matching route guide policy but the applicable-policies API returned {len(primary_matches)}.\n\n"
            f"Matched policies:\n{policy_list}\n\n"
            "To resolve: update or deactivate routing guide policies so that only one matches this shipment's lane. "
            "Multiple active policies for the same lane create ambiguity and prevent automatic carrier assignment."
        )
        plain, html = notify_bodies_for_order(order_data, subject, error_detail=detail)
        notify_support(subject, plain, sw, html_body=html)
        raise RuntimeError(subject)

    if primary_matches is not None and len(primary_matches) == 0:
        # The API returned a definitive answer: no routing guides match this shipment.
        # Do not fall back to client-side matching — surface the error directly.
        logger.info(
            "find_matching_route_guide_policy: applicable-policies returned 0 results — no routing guide available"
        )
        subject = "Error Assigning Route Guide - 0 Available Route Guide(s) Found"
        detail = (
            "The applicable-policies API returned 0 routing guide matches for this shipment. "
            "Verify that an active routing guide exists for this origin/destination/equipment combination."
        )
        plain, html = notify_bodies_for_order(order_data, subject, error_detail=detail)
        notify_support(subject, plain, sw, html_body=html)
        raise RuntimeError(subject)

    # primary_matches is None — the API call itself failed. Fall back to client-side matching.
    logger.warning(
        "find_matching_route_guide_policy: applicable-policies call failed — running client-side fallback"
    )

    # --- FALLBACK: client-side lane matching (only on API error) ---
    matches = _find_matching_policy_fallback(shipment_data, order_data, base_url, headers)

    if len(matches) != 1:
        subject = f"Error Assigning Route Guide - {len(matches)} Available Route Guide(s) Found"
        detail = (
            f"Expected exactly 1 matching route guide policy but client-side fallback found {len(matches)} "
            "(applicable-policies API was unavailable). "
            "Verify that exactly one active route guide covers this shipment's lane, equipment type, and product category."
        )
        if len(matches) == 0:
            detail += (
                "\n\nCheck that an active routing guide exists for this origin/destination/equipment combination."
            )
        else:
            policy_list = "\n".join(
                f"  - {p.get('name')} (id: {p.get('id')})"
                for p in matches
            )
            detail += (
                f"\n\nFallback matched policies:\n{policy_list}\n\n"
                "To resolve: update or deactivate routing guide policies so that only one matches this lane."
            )
        plain, html = notify_bodies_for_order(order_data, subject, error_detail=detail)
        notify_support(subject, plain, sw, html_body=html)
        raise RuntimeError(subject)

    logger.info(
        f"find_matching_route_guide_policy: FALLBACK match — '{matches[0].get('name')}' "
        f"(id={matches[0].get('id')}) via client-side matching (applicable-policies was unavailable)"
    )
    return matches[0]


def _get_routing_guide_steps(
    shipment_data: Dict,
    order_data: Dict,
    base_url: str,
    headers: Dict,
    sw: "ShipwellProgram",
) -> List[Dict]:
    """
    Return the ordered carrier sequence from the Shipwell routing guide workflow
    that matches this shipment's lane, including step duration from the workflow.

    Each entry:
        {
            "step_num":        int,   # 1-based position in the waterfall
            "step_id":         str,   # e.g. "STEP_1"
            "company_id":      str,   # tender_to_company UUID
            "scac":            str,   # resolved SCAC (empty string if unresolvable)
            "duration_seconds": int,  # from expires_after_seconds param; 0 if unset
        }

    Returns [] if:
    - No matching routing guide policy is found
    - The policy has no workflow_id
    - The workflow has no TENDER action steps
    - Any error occurs (logged as WARNING; caller falls back gracefully)
    """
    shipment_id = shipment_data.get("id", "")
    logger.info(f"_get_routing_guide_steps: resolving steps for shipment {shipment_id}")

    # ── 1. Find the matching routing guide policy ────────────────────────────
    try:
        policy = find_matching_route_guide_policy(shipment_data, order_data, base_url, headers, sw)
    except Exception as e:
        logger.warning(f"_get_routing_guide_steps: could not find routing guide policy: {e}")
        sw.log("WARNING", f"_get_routing_guide_steps: no routing guide policy found for shipment {shipment_id}: {e}", ["_get_routing_guide_steps"])
        return []

    workflow_id = policy.get("workflow_id")
    if not workflow_id:
        logger.warning(
            f"_get_routing_guide_steps: policy '{policy.get('name')}' has no workflow_id — cannot read steps"
        )
        sw.log("WARNING", f"_get_routing_guide_steps: policy '{policy.get('name')}' has no workflow_id", ["_get_routing_guide_steps"])
        return []

    # ── 2. Fetch the workflow object ─────────────────────────────────────────
    try:
        workflow = _api_call(f"{base_url}/workflows/{workflow_id}/", "GET", headers=headers)
    except Exception as e:
        logger.warning(f"_get_routing_guide_steps: failed to fetch workflow {workflow_id}: {e}")
        sw.log("WARNING", f"_get_routing_guide_steps: failed to fetch workflow {workflow_id}: {e}", ["_get_routing_guide_steps"])
        return []

    if not workflow:
        logger.warning(f"_get_routing_guide_steps: empty response for workflow {workflow_id}")
        return []

    # ── 3. Extract TENDER action steps in order ──────────────────────────────
    # get_tender_steps() (from workflow_splice) returns actions where action_id == 'TENDER'
    # in the order they appear in the workflow's actions list.
    sw.log(
        "TRACE",
        f"_get_routing_guide_steps: matched policy '{policy.get('name')}' (id={policy.get('id')}) "
        f"workflow '{workflow.get('name')}' (id={workflow_id}) for shipment {shipment_id}",
        ["_get_routing_guide_steps"],
    )
    from workflow_splice import get_tender_steps, get_company_id_from_step, resolve_scac
    tender_steps = get_tender_steps(workflow)

    if not tender_steps:
        logger.warning(
            f"_get_routing_guide_steps: workflow '{workflow.get('name')}' ({workflow_id}) "
            f"has no TENDER action steps"
        )
        sw.log("WARNING", f"_get_routing_guide_steps: workflow '{workflow.get('name')}' ({workflow_id}) has no TENDER action steps", ["_get_routing_guide_steps"])
        return []

    # ── 4. Build the step list ───────────────────────────────────────────────
    steps: List[Dict] = []
    for step_num, action in enumerate(tender_steps, start=1):
        step_id    = action.get("step_id", f"STEP_{step_num}")
        company_id = get_company_id_from_step(action) or ""

        # expires_after_seconds is a named param on the TENDER action
        params = {p["name"]: p["value"] for p in (action.get("params") or [])}
        duration_seconds = int(params.get("expires_after_seconds") or 0)

        # Resolve SCAC — best-effort; step is still included even if SCAC is unknown
        scac = ""
        if company_id:
            try:
                scac = resolve_scac(company_id, base_url, headers) or ""
            except Exception as e:
                logger.warning(
                    f"_get_routing_guide_steps: could not resolve SCAC for company {company_id} "
                    f"(step {step_id}): {e}"
                )

        steps.append({
            "step_num":         step_num,
            "step_id":          step_id,
            "company_id":       company_id,
            "scac":             scac.upper().strip(),
            "duration_seconds": duration_seconds,
        })

        logger.info(
            f"_get_routing_guide_steps: step {step_num} — {step_id} — "
            f"SCAC={scac!r} company={company_id} duration={duration_seconds}s"
        )

    sw.log(
        "TRACE",
        f"Routing guide steps for shipment {shipment_id}: "
        + ", ".join(
            f"{s['step_id']}={s['scac']}({s['duration_seconds']}s)"
            for s in steps
        ),
        ["_get_routing_guide_steps"],
    )
    return steps


def build_carrier_config_body(
    carrier_relationship: Dict, equipment_type: Dict, mode: Dict,
    contract_id: Optional[str], order_data: Dict, suppress_notifications: bool = False,
) -> Dict:
    """Build POST carrier-config payload, conditionally omitting vendor_point_of_contact."""
    body: Dict[str, Any] = {
        "carrier_relationship_id": None,
        "carrier_status": None,
        "vendor": carrier_relationship.get("shipwell_vendor"),
        "equipment_type": equipment_type,
        "mode": mode,
        "service_level": carrier_relationship.get("service_level"),
        "contract_id": contract_id,
    }
    if not should_omit_point_of_contact(order_data, suppress_notifications):
        pocs = carrier_relationship.get("point_of_contacts") or []
        if pocs:
            preferred = _select_preferred_poc(pocs)
            if preferred:
                body["vendor_point_of_contact"] = preferred
    return body


def build_carrier_config_body_from_shipment(
    carrier_rel: Dict, shipment_data: Dict,
    contract_id: Optional[str], order_data: Dict, suppress_notifications: bool = False,
) -> Dict:
    """Convenience wrapper: extracts equipment_type and mode from shipment_data."""
    equipment_type = shipment_data.get("equipment_type") or {}
    mode = shipment_data.get("mode") or {}
    return build_carrier_config_body(
        carrier_rel, equipment_type, mode, contract_id, order_data, suppress_notifications,
    )


def get_carrier_by_scac(scac_code: str, base_url: str, headers: Dict) -> Optional[Dict]:
    """
    Find an active carrier relationship by SCAC code.
    Returns None if not found (non-fatal).

    Tries three strategies in order:
      1. Paginated scan of /v2/carrier-relationships/?carrier_status=ACTIVE
      2. q= search (handles known Shipwell API bug where results=[] despite total_count>0)
      3. Company identifying_codes lookup via vendor_id (last resort)
    """
    normalized = scac_code.upper().strip()
    page = 1
    while True:
        try:
            resp = _api_call(
                f"{base_url}/v2/carrier-relationships/?carrier_status=ACTIVE&page={page}",
                "GET", headers=headers,
            )
        except Exception as e:
            logger.warning(f"get_carrier_by_scac: lookup failed on page {page}: {e}")
            return None

        results = resp.get("results") or []
        total_pages = resp.get("total_pages") or 1

        for carrier in results:
            for code in (carrier.get("identifying_codes") or []):
                if code.get("type") == "SCAC" and code.get("value", "").upper() == normalized:
                    logger.info(f"Found carrier for SCAC {normalized}: {carrier.get('name')}")
                    return carrier

        if page >= total_pages:
            break
        page += 1

    # Fallback 1: q= search (Shipwell API known issue — results may be empty despite hits)
    logger.info(f"get_carrier_by_scac: paginated scan missed SCAC {normalized!r}, trying q= fallback")
    try:
        resp = _api_call(
            f"{base_url}/v2/carrier-relationships/?q={normalized}",
            "GET", headers=headers,
        )
        for carrier in resp.get("results") or []:
            for code in (carrier.get("identifying_codes") or []):
                if code.get("type") == "SCAC" and code.get("value", "").upper() == normalized:
                    logger.info(f"Found carrier for SCAC {normalized} via q= fallback: {carrier.get('name')}")
                    return carrier
    except Exception as e:
        logger.warning(f"get_carrier_by_scac: q= fallback failed: {e}")

    # Fallback 2: company SCAC lookup → vendor_id → carrier-relationship
    logger.info(f"get_carrier_by_scac: trying vendor_id lookup fallback for SCAC {normalized!r}")
    try:
        comp_page = 1
        vendor_id = None
        while True:
            comp_resp = _api_call(
                f"{base_url}/v2/companies/?page={comp_page}&page_size=200",
                "GET", headers=headers,
            )
            for company in comp_resp.get("results") or []:
                for code in (company.get("identifying_codes") or []):
                    if code.get("type") == "SCAC" and code.get("value", "").upper() == normalized:
                        vendor_id = company.get("id")
                        logger.info(f"Found company for SCAC {normalized}: {company.get('name')} vendor_id={vendor_id}")
                        break
                if vendor_id:
                    break
            comp_total = comp_resp.get("total_pages") or 1
            if vendor_id or comp_page >= comp_total:
                break
            comp_page += 1

        if vendor_id:
            rel_resp = _api_call(
                f"{base_url}/v2/carrier-relationships/?vendor_id={vendor_id}",
                "GET", headers=headers,
            )
            rel_results = rel_resp.get("results") or []
            if rel_results:
                carrier = rel_results[0]
                if not carrier.get("identifying_codes"):
                    carrier["identifying_codes"] = [{"type": "SCAC", "value": normalized}]
                logger.info(f"Found carrier for SCAC {normalized} via vendor_id fallback: {carrier.get('name')}")
                return carrier
    except Exception as e:
        logger.warning(f"get_carrier_by_scac: vendor_id fallback failed: {e}")

    logger.warning(f"get_carrier_by_scac: no active carrier found with SCAC {normalized!r}")
    return None


def _initiate_routing_guide(
    shipment_id: str, base_url: str, headers: Dict, sw: "ShipwellProgram",
    policy: Optional[Dict] = None,
    shipment_data: Optional[Dict] = None,
) -> None:
    """
    Fetch the routing guide policy, check all TENDER step carriers against the
    HAC sheet, splice out any that have exhausted capacity, initiate the workflow,
    then immediately restore the original workflow.

    Fail open on any error — if capacity check or splice fails, the routing guide
    is still initiated with the original workflow.
    """
    logger.info("=== _initiate_routing_guide START ===")

    if policy is None:
        policy = _api_call(
            f"{base_url}/v2/routing-guide/policies/{ROUTING_GUIDE_POLICY_ID}",
            "GET", headers=headers,
        )
    if not policy or not policy.get("workflow_id"):
        raise RuntimeError(f"Routing guide policy {ROUTING_GUIDE_POLICY_ID} not found or has no workflow_id")

    workflow_id = policy["workflow_id"]
    sw.log("TRACE", f"Initiating routing guide: policy='{policy.get('name')}' workflow={workflow_id}", ["assign_contract"])
    logger.info(f"Initiating routing guide workflow {workflow_id} for shipment {shipment_id}")

    # HAC capacity check + splice if enabled and shipment data is available
    use_capacity_check = (
        HAC_CAPACITY_INTEGRATION_ENABLED
        and shipment_data is not None
    )

    if use_capacity_check:
        location_id = get_first_pickup_location_id(shipment_data, base_url=base_url, headers=headers)
        is_hac = location_id and location_id in HAC_ASPHALT_LOCATIONS
    else:
        is_hac = False

    if is_hac:
        try:
            from handler import _get_pickup_appointment_date, build_google_sheets_client
            from workflow_splice import initiate_with_capacity_check

            from hac_capacity import _EQUIPMENT_TYPE_TO_SHEET_BUCKET

            appointment_date = _get_pickup_appointment_date(shipment_data)

            # Prefer equipment_type for capacity bucketing — Blue Dot and Green Dot
            # shipments carry their product identity in equipment_type, not the line
            # item description (which may say "EMULSION" for a Blue Dot load).
            equipment_type = (
                (shipment_data.get("equipment_type") or {}).get("name", "").upper().replace(" ", "_")
                if isinstance(shipment_data.get("equipment_type"), dict)
                else str(shipment_data.get("equipment_type") or "").upper()
            )
            equipment_bucket = _EQUIPMENT_TYPE_TO_SHEET_BUCKET.get(equipment_type)
            if equipment_bucket:
                product_category = equipment_bucket
            else:
                product_category = get_shipment_product_category(shipment_data, {})
            if not product_category:
                product_category = "Asphalt"

            sw.log("TRACE", f"_initiate_routing_guide: equipment_type={equipment_type!r} → product_category={product_category!r} for capacity check", ["_initiate_routing_guide"])

            sheets = build_google_sheets_client()

            splice_result = initiate_with_capacity_check(
                shipment_id=shipment_id,
                workflow_id=workflow_id,
                appointment_date=appointment_date,
                product_category=product_category,
                location_id=location_id,
                sheets_client=sheets,
                base_url=base_url,
                headers=headers,
                sw=sw,
                dry_run=DRY_RUN,
            )

            # Increment capacity for the first kept carrier (STEP_1 of the spliced workflow).
            # The routing guide tenders async via Opus so we must increment here at send-time,
            # not in process_carrier_assigned_event (which fires after acceptance).
            if not DRY_RUN and HAC_CAPACITY_INTEGRATION_ENABLED:
                try:
                    from handler import _hac_capacity_increment
                    # Increment for the FIRST kept step (the carrier that receives STEP_1 tender).
                    # If that carrier has no SCAC or no sheet tab, skip — do NOT jump to the next
                    # carrier with a SCAC (that would increment the wrong carrier).
                    first_kept = splice_result.get("kept", [{}])[0] if splice_result.get("kept") else {}
                    first_kept_scac = first_kept.get("scac") or ""
                    if first_kept_scac:
                        _shipment_for_cap = _api_call(f"{base_url}/v2/shipments/{shipment_id}/", "GET", headers=headers) or shipment_data
                        _hac_capacity_increment(
                            _shipment_for_cap, first_kept_scac, sw,
                            base_url=base_url, headers=headers,
                            caller="_initiate_routing_guide:routing_guide_tender",
                        )
                    else:
                        sw.log("TRACE", f"_initiate_routing_guide: first kept carrier has no SCAC — skipping capacity increment (carrier will manage their own sheet)", ["_initiate_routing_guide"])
                except Exception as _cap_err:
                    sw.log("WARNING", f"_initiate_routing_guide: capacity increment failed ({_cap_err}) — continuing", ["_initiate_routing_guide"])

        except Exception as e:
            logger.warning(f"_initiate_routing_guide: HAC capacity check failed ({e}) — initiating without splice")
            sw.log("WARNING", f"HAC capacity check failed: {e} — initiating workflow without splice", ["assign_contract"])
            if not DRY_RUN:
                _api_call(
                    f"{base_url}/v2/shipments/{shipment_id}/routing-guide/initiate/",
                    "POST", headers=headers,
                    body={"workflow": workflow_id},
                )
    else:
        if DRY_RUN:
            logger.info(f"[DRY_RUN] Would POST routing-guide/initiate workflow={workflow_id}")
        else:
            _api_call(
                f"{base_url}/v2/shipments/{shipment_id}/routing-guide/initiate/",
                "POST", headers=headers,
                body={"workflow": workflow_id},
            )

    sw.log("TRACE", "Routing guide initiated — tendering workflow running async", ["assign_contract"])
    logger.info("=== _initiate_routing_guide COMPLETED ===")


# Categories and description keywords that are customer-side only and must
# never be copied into vendor_charge_line_items.
_TAX_CATEGORIES = frozenset({"TTS"})
_TAX_DESCRIPTION_KEYWORDS = ("tax", "gross receipts")


def filter_vendor_charge_lines(charge_lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return charge lines with tax-only items removed.

    Tax charges (category TTS, or description containing 'Tax'/'Gross Receipts')
    are customer-facing only and must not appear in vendor_charge_line_items.
    """
    result = []
    for line in charge_lines:
        category = (line.get("category") or "").upper().strip()
        if category in _TAX_CATEGORIES:
            continue
        description = (line.get("unit_name") or line.get("description") or "").lower()
        if any(kw in description for kw in _TAX_DESCRIPTION_KEYWORDS):
            continue
        result.append(line)
    return result


def _mirror_read_back(
    shipment_id: str, vendor_rel_id: str, base_url: str, headers: Dict,
    sw: Optional[Any], expected: int,
) -> Optional[int]:
    """GET the assignment after the mirror PUT and log the persisted vendor line count.

    A successful PUT does not prove the lines persisted. Logs ERROR (and sw ERROR) if
    vendor_charge_line_items is empty while customer non-tax lines exist. Never raises.
    """
    try:
        back = _api_call(
            f"{base_url}/v2/shipments/{shipment_id}/carrier-assignments/{vendor_rel_id}/",
            "GET", headers=headers,
        ) or {}
        v_count = len(back.get("vendor_charge_line_items") or [])
        c_nontax = len(filter_vendor_charge_lines(back.get("customer_charge_line_items") or []))
        msg = (
            f"mirror_vendor_charges read-back: assignment {vendor_rel_id} shipment {shipment_id} "
            f"vendor_charge_line_items={v_count} (expected {expected}) customer_non_tax_lines={c_nontax}"
        )
        if v_count == 0 and c_nontax > 0:
            logger.error(msg + " — VENDOR LINES EMPTY AFTER MIRROR PUT")
            if sw:
                sw.log("ERROR", msg + " — vendor lines empty after mirror PUT", ["mirror_vendor_charges"])
        else:
            logger.info(msg)
            if sw:
                sw.log("TRACE", msg, ["mirror_vendor_charges"])
        return v_count
    except Exception as exc:
        logger.warning(f"mirror_vendor_charges read-back failed for shipment {shipment_id}: {exc}")
        if sw:
            sw.log("WARNING", f"mirror_vendor_charges read-back failed (non-fatal): {exc}", ["mirror_vendor_charges"])
        return None


def mirror_vendor_charges(
    shipment_id: str,
    vendor_rel_id: str,
    base_url: str,
    headers: Dict,
    sw: Optional[Any] = None,
    existing_assignment: Optional[Dict] = None,
) -> None:
    """
    Read the current customer_charge_line_items from the carrier assignment and
    mirror non-tax items into vendor_charge_line_items so the carrier can see
    their financials.

    Tax line items (category TTS / descriptions containing 'Tax'/'Gross Receipts')
    are excluded — those are customer-side only.

    Called after any carrier-assignment PUT that uses tender=True (which lets Shipwell
    calculate customer_charge_line_items from the contract) or after a tender creates
    charges server-side.

    Uses existing_assignment if provided (avoids a redundant GET), otherwise fetches fresh.
    """
    try:
        if existing_assignment is not None:
            va = existing_assignment
        else:
            va = _api_call(
                f"{base_url}/v2/shipments/{shipment_id}/carrier-assignments/{vendor_rel_id}/",
                "GET", headers=headers,
            ) or {}

        cli = va.get("customer_charge_line_items") or []
        if not cli:
            logger.info(
                f"mirror_vendor_charges: no customer_charge_line_items yet on "
                f"assignment {vendor_rel_id} for shipment {shipment_id} — skipping mirror"
            )
            return

        already = va.get("vendor_charge_line_items") or []
        if already:
            # Already populated — don't clobber existing vendor lines
            logger.info(
                f"mirror_vendor_charges: vendor_charge_line_items already set ({len(already)} items) — skipping"
            )
            return

        put_body = dict(va)  # shallow copy of full assignment body
        vendor_lines = filter_vendor_charge_lines(cli)
        put_body["vendor_charge_line_items"] = vendor_lines
        # Remove read-only / nested fields that cause 400s on PUT
        for _drop in ("id", "created_at", "updated_at", "carrier_status", "is_assigned_carrier"):
            put_body.pop(_drop, None)

        _api_call(
            f"{base_url}/v2/shipments/{shipment_id}/carrier-assignments/{vendor_rel_id}/",
            "PUT", headers=headers, body=put_body,
        )
        total = sum(float(c.get("unit_amount", 0)) for c in vendor_lines)
        skipped_tax = len(cli) - len(vendor_lines)
        _mirror_read_back(shipment_id, vendor_rel_id, base_url, headers, sw, len(vendor_lines))
        logger.info(
            f"mirror_vendor_charges: mirrored {len(vendor_lines)} charge items (${total:.2f}) to "
            f"vendor_charge_line_items on assignment {vendor_rel_id} for shipment {shipment_id}"
            + (f" (skipped {skipped_tax} tax line(s))" if skipped_tax else "")
        )
        if sw:
            sw.log(
                "TRACE",
                f"mirror_vendor_charges: {len(vendor_lines)} items mirrored to vendor side for shipment {shipment_id}"
                + (f" ({skipped_tax} tax line(s) excluded)" if skipped_tax else ""),
                ["mirror_vendor_charges"],
            )
    except Exception as exc:
        # Non-fatal — log and continue; customer charges are already correct
        logger.warning(f"mirror_vendor_charges: failed for shipment {shipment_id}: {exc}")
        if sw:
            sw.log("WARNING", f"mirror_vendor_charges failed (non-fatal): {exc}", ["mirror_vendor_charges"])


def build_carrier_assignment_body(
    carrier_rel: Dict, contract_id: Optional[str], tender: bool = False,
) -> Dict[str, Any]:
    """Build the carrier-assignment payload."""
    body: Dict[str, Any] = {
        "vendor": carrier_rel.get("shipwell_vendor"),
        "contract_id": contract_id,
        "vendor_charge_line_items": [],
        "customer_charge_line_items": [],
    }
    if tender:
        body["tender"] = True
    # Include vendor_point_of_contact so SLU carrier users retain load board visibility
    # after the carrier-assignment PUT (which otherwise resets the POC set by carrier-config).
    # Use _select_preferred_poc for stable, billing-first selection.
    pocs = carrier_rel.get("point_of_contacts") or []
    preferred = _select_preferred_poc(pocs)
    if preferred:
        body["vendor_point_of_contact"] = preferred.get("id")
    return body


def _calculate_contract_charge_items(
    contract_id: str,
    shipment_id: str,
    base_url: str,
    headers: Dict,
) -> List[Dict[str, Any]]:
    """
    Call POST /v2/contracts/{id}/calculate-charge-items/ with request_type=BY_SHIPMENT
    and return a single consolidated LINE_HAUL charge item for POST /v2/tenders/.

    The Shipwell tenders API rejects multiple charge_line_items with the same
    category bucket (LH) — e.g. LINE_HAUL + FUEL_SURCHARGE both map to LH internally
    and cause a "Duplicate category found: LH" 400 error. Confirmed from real tender
    records: accepted tenders carry exactly one LINE_HAUL item with the all-in amount.

    So we sum all returned charge items into a single LINE_HAUL total.

    Returns a single-item list, or empty list on any error so callers can fail open.
    """
    try:
        resp = _api_call(
            f"{base_url}/v2/contracts/{contract_id}/calculate-charge-items/",
            "POST",
            headers=headers,
            body={"request_type": "BY_SHIPMENT", "shipment_id": shipment_id},
        ) or {}
        raw_items = resp.get("charge_items") or []
        if not raw_items:
            logger.warning(f"_calculate_contract_charge_items: no items returned for contract {contract_id}")
            return []

        total = sum(float(item.get("unit_amount") or item.get("amount") or 0) for item in raw_items)
        currency = (raw_items[0].get("unit_amount_currency") or "USD") if raw_items else "USD"

        logger.info(
            f"_calculate_contract_charge_items: {len(raw_items)} raw items summed to ${total:.2f} "
            f"for contract {contract_id} (consolidated to single LINE_HAUL)"
        )
        return [{
            "category": "LINE_HAUL",
            "charge_code": "LHS",
            "unit_amount": str(round(total, 2)),
            "unit_amount_currency": currency,
            "unit_name": "Linehaul Service",
            "unit_quantity": "1",
        }]
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(f"_calculate_contract_charge_items: error for contract {contract_id}: {exc}")
        return []


def _establish_carrier_config_relationship(
    shipment_id: str,
    shipment_data: Dict,
    carrier_rel: Dict,
    contract_id: Optional[str],
    order_data: Dict,
    base_url: str,
    headers: Dict,
    sw: "ShipwellProgram",
) -> None:
    """
    POST carrier-config to establish relationship_to_vendor before tendering.

    When post_tender_direct is used (POST /v2/tenders/ only), Shipwell creates
    the carrier-config relationship internally when the carrier accepts — which
    triggers bare $0 UNSPECIFIED side-effect tenders and duplicate carrier emails.

    By establishing the relationship here (before the tender is sent), acceptance
    finds relationship_to_vendor already present and no internal carrier-config
    flow runs — no side-effect tenders, no duplicate emails.

    The carrier-config POST itself also creates a bare $0 side-effect tender.
    We immediately revoke it so the carrier only ever sees the real charged tender
    from post_tender_direct.
    """
    equipment_type = shipment_data.get("equipment_type") or {}
    mode = shipment_data.get("mode") or {}

    # Use contract mode if available — but do NOT override equipment_type from the contract.
    # The shipment's equipment_type is derived from the product at creation time (e.g. Green Dot,
    # Blue Dot) and must be preserved. Contract equipment_types lists all applicable types with
    # Asphalt first, which would incorrectly overwrite the product-derived equipment type.
    if contract_id:
        contracts_resp = _api_call(
            f"{base_url}/v2/contracts/applicable-contracts/",
            "POST", headers=headers,
            body={"shipment_id": shipment_id, "request_type": "BY_SHIPMENT"},
        ) or {}
        for c in (contracts_resp.get("data") or []):
            if (c.get("contract") or {}).get("id") == contract_id:
                mo_list = c["contract"].get("modes") or []
                if mo_list:
                    mode = mo_list[0]
                break

    config_body = build_carrier_config_body(
        carrier_rel, equipment_type, mode, contract_id, order_data,
        suppress_notifications=True,
    )

    if DRY_RUN:
        sw.log("TRACE", f"[DRY_RUN] Would POST carrier-config to establish relationship_to_vendor for shipment {shipment_id}", ["_establish_carrier_config_relationship"])
        return

    sw.log("TRACE", f"Establishing carrier-config relationship for shipment {shipment_id} before tendering", ["_establish_carrier_config_relationship"])
    _api_call(f"{base_url}/v2/shipments/{shipment_id}/carrier-config/", "POST", headers=headers, body=config_body)

    # Immediately revoke any bare $0 UNSPECIFIED side-effect tenders created by carrier-config
    try:
        resp = _api_call(
            f"{base_url}/v2/tenders/?shipment_id={shipment_id}&page_size=20",
            "GET", headers=headers,
        ) or {}
        for tender in (resp.get("results") or []):
            status = (tender.get("status") or "").lower()
            lane_type = (tender.get("lane_type") or "").upper()
            cli = tender.get("charge_line_items") or []
            total = sum(float(c.get("unit_amount", 0)) for c in cli)
            if status in ("open", "pending") and lane_type == "UNSPECIFIED" and total == 0.0:
                tender_id = tender.get("id", "")
                _api_call(f"{base_url}/v2/tenders/{tender_id}/revoke/", "POST", headers=headers)
                sw.log("TRACE", f"Revoked bare $0 carrier-config side-effect tender {tender_id}", ["_establish_carrier_config_relationship"])
    except Exception as e:
        # Non-fatal — log and continue; the real tender will still be sent
        logger.warning(f"_establish_carrier_config_relationship: side-effect tender cleanup failed: {e}")
        sw.log("WARNING", f"Could not revoke carrier-config side-effect tender: {e}", ["_establish_carrier_config_relationship"])


def post_tender_direct(
    shipment_id: str,
    shipment_data: Dict,
    carrier_rel: Dict,
    contract: Dict,
    base_url: str,
    headers: Dict,
    sw: "ShipwellProgram",
    order_data: Optional[Dict] = None,
    custom_fields: Optional[Dict] = None,
) -> Dict:
    """
    Tender a shipment directly via POST /v2/tenders/ with charge line items
    calculated by Shipwell's calculate-charge-items API.

    Replaces the carrier-assignment PUT tender flow for preset SCAC orders,
    giving explicit financial visibility on the tender (LH + FSC + accessorials).
    """
    contract_id = contract.get("id")
    carrier_company_id = (carrier_rel.get("shipwell_vendor") or {}).get("id")
    if not carrier_company_id:
        raise RuntimeError("post_tender_direct: carrier company UUID not found on carrier_rel")

    # When contract is provided, Shipwell auto-injects charge line items server-side
    # (LH + FSC from the contract rate/fuel-surcharge tables). Do NOT pass charge_line_items
    # — sending any items alongside a contract causes "Duplicate category: LH" 400 error.
    sw.log("TRACE", f"post_tender_direct: contract {contract_id} provided — Shipwell will auto-calculate charges", ["post_tender_direct"])

    # --- involved_tender_to_company_users: populate from carrier POCs ---
    # This is what drives the "Tender request" email and load board visibility.
    # Without it the tender stays open and no email is sent.
    pocs = carrier_rel.get("point_of_contacts") or []
    # Pass as flat list of UUID strings — API accepts this format and stores them correctly
    # (dict format {"id": "..."} causes 400; omitting entirely causes involved_users=null)
    # Only send to DISPATCHER POC to avoid "one of multiple carriers" warning in email.
    # Fall back to all should_send_email POCs if no DISPATCHER is found.
    dispatcher_users = [
        poc["user"]
        for poc in pocs
        if poc.get("user")
        and poc.get("should_send_email", True)
        and (poc.get("job_title") or "").upper() == "DISPATCHER"
    ]
    involved_users = dispatcher_users or [
        poc["user"]
        for poc in pocs
        if poc.get("user") and poc.get("should_send_email", True)
    ]
    sw.log(
        "TRACE",
        f"post_tender_direct: {len(involved_users)} involved users from carrier POCs",
        ["post_tender_direct"],
    )

    # --- Build pickup/delivery dates from stops ---
    stops = shipment_data.get("stops") or []
    pickup_stop = stops[0] if stops else {}
    delivery_stop = stops[-1] if stops else {}
    for s in stops:
        stype = (s.get("stop_type") or "").upper()
        if stype in ("PICKUP", "ORIGIN"):
            pickup_stop = s
        elif stype in ("DELIVERY", "DESTINATION"):
            delivery_stop = s

    earliest_pickup_date = pickup_stop.get("planned_date")
    latest_pickup_date = pickup_stop.get("planned_date")
    earliest_pickup_time = pickup_stop.get("planned_time_window_start")
    latest_pickup_time = pickup_stop.get("planned_time_window_end")
    delivery_date = delivery_stop.get("planned_date")

    equipment_type = (shipment_data.get("equipment_type") or {}).get("machine_readable")
    mode_id = (shipment_data.get("mode") or {}).get("id")

    body: Dict[str, Any] = {
        "shipment": shipment_id,
        "tender_to_company": carrier_company_id,
        "contract": contract_id,
        "equipment_type": equipment_type,
        "mode": mode_id,
        "earliest_pickup_date": earliest_pickup_date,
        "latest_pickup_date": latest_pickup_date,
        "earliest_pickup_time": earliest_pickup_time,
        "latest_pickup_time": latest_pickup_time,
        "delivery_date": delivery_date,
        "lane_type": "DIRECT",
    }
    if involved_users:
        body["involved_tender_to_company_users"] = involved_users
    # Remove None values — API rejects explicit nulls on some fields
    body = {k: v for k, v in body.items() if v is not None}

    logger.info(f"post_tender_direct: POST /v2/tenders/ body={json.dumps(body, default=str)}")

    if DRY_RUN:
        sw.log("TRACE", f"[DRY_RUN] Would POST /v2/tenders/ to {carrier_company_id} — contract {contract_id} (Shipwell auto-calculates charges)", ["post_tender_direct"])
        return body

    # Dedup: check for existing open/pending tender for this shipment+carrier before posting
    existing_tenders = (_api_call(
        f"{base_url}/v2/tenders/?shipment_id={shipment_id}&page_size=10",
        "GET", headers=headers,
    ) or {}).get("results") or []
    # carrier_rel uses shipwell_vendor.id for the company UUID (not company.id)
    carrier_company_id_check = (
        (carrier_rel.get("shipwell_vendor") or {}).get("id")
        or (carrier_rel.get("company") or {}).get("id")
        or carrier_rel.get("id", "")
    )
    for et in existing_tenders:
        ttc = et.get("tender_to_company")
        ttc_id = ttc.get("id") if isinstance(ttc, dict) else ttc
        if et.get("status") in ("open", "pending") and ttc_id == carrier_company_id_check:
            sw.log("TRACE", f"post_tender_direct: existing {et['status']} tender {et['id']} found — skipping duplicate POST", ["post_tender_direct"])
            logger.info(f"post_tender_direct: skipping — tender {et['id']} already exists for this shipment+carrier")
            return et

    result = _api_call(f"{base_url}/v2/tenders/", "POST", headers=headers, body=body)
    tender_id = (result or {}).get("id")
    auto_items = result.get("charge_line_items") or [] if result else []
    auto_total = sum(float(li.get("unit_amount", 0)) for li in auto_items)
    sw.log(
        "TRACE",
        f"post_tender_direct: tender {tender_id} created — {len(auto_items)} auto-calculated charge items total ~${auto_total:.2f}",
        ["post_tender_direct"],
    )
    logger.info(f"post_tender_direct: tender created: {tender_id}")

    # POST /v2/tenders/ alone transitions the shipment to `tendered` state — no carrier-config needed.
    # Do NOT call assign_or_tender_carrier here: POST carrier-config creates a bare $0 tender as a
    # side-effect that cannot be revoked (DELETE/PATCH/PUT → 405). The real tender above is sufficient.
    sw.log("TRACE", f"post_tender_direct: tender {tender_id} created — shipment should now be in tendered state (no carrier-config needed)", ["post_tender_direct"])

    # Apply preplanned accessorials (e.g. TANKWASH, CAA, VOR) to the carrier assignment
    # so they appear in the financials alongside the auto-calculated LH + FSC.
    #
    # NM-origin shipments: skip here and let the TTS charge_line_item.created hook
    # handle it instead. Shipwell writes LHS, FSC, and TTS as separate charge events
    # after contract rating — each fires charge_line_item.created concurrently. If we
    # write TANKWASH here, those concurrent events overwrite the carrier assignment
    # before our verification GET can confirm it, causing the write to fail all 3
    # retry attempts. The TTS hook in process_charge_line_item_created_event fires
    # exactly once (only on TTS), so it races with nothing and lands cleanly.
    if order_data:
        try:
            from accessorials import apply_preplanned_accessorials as _apply_pa
            _shipment_post_tender = _api_call(f"{base_url}/v2/shipments/{shipment_id}/", "GET", headers=headers) or shipment_data
            _pickup_stops_ptd = [s for s in (_shipment_post_tender or {}).get("stops", []) if s.get("is_pickup")]
            _origin_state_ptd = ((_pickup_stops_ptd[0].get("location") or {}).get("address") or {}).get("state_province", "").upper() if _pickup_stops_ptd else ""
            if _origin_state_ptd == "NM":
                sw.log("TRACE", "post_tender_direct: NM-origin shipment — skipping apply_preplanned_accessorials here; TTS charge_line_item.created hook will apply after contract rating settles", ["post_tender_direct"])
            else:
                _apply_pa(_shipment_post_tender, order_data, base_url, headers, custom_fields or {}, sw)
        except Exception as _pa_err:
            sw.log("WARNING", f"post_tender_direct: apply_preplanned_accessorials failed (non-fatal): {_pa_err}", ["post_tender_direct"])

    return result or body


def assign_or_tender_carrier(
    shipment_id: str, shipment_data: Dict, order_data: Dict, carrier_rel: Dict,
    contract: Optional[Dict], base_url: str, headers: Dict, direct_assign: bool,
    suppress_notifications: bool, sw: "ShipwellProgram",
) -> Dict:
    """Configure a carrier and either directly assign or tender based on the doc branch."""
    contract_id = contract.get("id") if contract else None
    equipment_type = (
        (contract.get("equipment_types") or [None])[0] if contract else None
    ) or shipment_data.get("equipment_type") or {}
    mode = ((contract.get("modes") or [None])[0] if contract else None) or shipment_data.get("mode") or {}

    config_body = build_carrier_config_body(
        carrier_rel, equipment_type, mode, contract_id, order_data,
        suppress_notifications=suppress_notifications,
    )
    logger.info(f"carrier-config payload: {json.dumps(config_body, default=str)}")

    effective_contract_id = contract_id  # tracks if we fell back to no-contract
    if DRY_RUN:
        logger.info(f"[DRY_RUN] Would POST carrier-config: {json.dumps(config_body, default=str)}")
    else:
        try:
            _api_call(f"{base_url}/v2/shipments/{shipment_id}/carrier-config/", "POST", headers=headers, body=config_body)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 400 and contract_id:
                err_text = e.response.text or ""
                if "No matching lane" in err_text or "Unable to calculate contract charges" in err_text:
                    subject = f"Error Assigning Carrier - Contract Lane Not Matched"
                    detail = (
                        f"Contract ID: {contract_id}. "
                        f"carrier-config returned 400: {err_text[:300]}. "
                        "Carrier was NOT assigned. Please assign the carrier manually and verify the contract lane covers this shipment's lane/equipment."
                    )
                    plain, html = notify_bodies_for_shipment(shipment_data, subject, error_detail=detail)
                    notify_support(subject, plain, sw, html_body=html)
                    raise RuntimeError(
                        f"carrier-config 400 - contract {contract_id} lane not matched for shipment {shipment_id}: {err_text[:200]}"
                    ) from e
                else:
                    raise
            else:
                raise

    latest = shipment_data
    if not (DRY_RUN and shipment_id == "dry-run-id"):
        latest = _api_call(f"{base_url}/v2/shipments/{shipment_id}/", "GET", headers=headers) or shipment_data
    vendor_rel_id = (latest.get("relationship_to_vendor") or {}).get("id")

    assignment_body = build_carrier_assignment_body(
        carrier_rel,
        effective_contract_id,
        tender=not direct_assign,
    )

    if DRY_RUN:
        action = "assign" if direct_assign else "tender"
        logger.info(f"[DRY_RUN] Would {action} carrier: vendor_rel_id={vendor_rel_id}, body={json.dumps(assignment_body, default=str)}")
        return assignment_body

    result = _api_call(
        f"{base_url}/v2/shipments/{shipment_id}/carrier-assignments/{vendor_rel_id}/",
        "PUT", headers=headers, body=assignment_body,
    )
    logger.info(f"Carrier {'assigned' if direct_assign else 'tendered'}: {(result or {}).get('id')}")

    # Mirror customer_charge_line_items → vendor_charge_line_items so the carrier
    # can see their financials. When tender=True, Shipwell calculates customer charges
    # synchronously in the PUT response; mirror them to the vendor side immediately.
    if vendor_rel_id:
        mirror_vendor_charges(
            shipment_id, vendor_rel_id, base_url, headers,
            sw=None,  # sw not available in this function signature
            existing_assignment=result,
        )

    return result or assignment_body


def _check_hac_sheet_capacity_preflight(
    scac: str,
    shipment_data: Dict,
    sw: "ShipwellProgram",
    base_url: Optional[str] = None,
    headers: Optional[Dict] = None,
    order_data: Optional[Dict] = None,
) -> None:
    """
    Pre-flight capacity check against the HAC Google Sheet for the given carrier SCAC.
    Raises RuntimeError (with error email) if the carrier has no driver or equipment
    capacity for the shipment's pickup date. Does NOT consume capacity.

    Called for all HAC asphalt locations before a tender or direct assignment is issued.
    Fails open (logs warning, does not block) if the sheet integration is disabled or
    if the sheet lookup itself errors — to avoid blocking real shipments on infra issues.
    """
    if not HAC_CAPACITY_INTEGRATION_ENABLED:
        sw.log("WARNING", f"HAC sheet pre-flight skipped — HAC_CAPACITY_INTEGRATION_ENABLED=false", ["_check_hac_sheet_capacity_preflight"])
        return

    location_id = get_first_pickup_location_id(shipment_data, base_url=base_url, headers=headers)
    appointment_date = _get_pickup_appointment_date_from_shipment(shipment_data)
    product_category = get_shipment_product_category(shipment_data, order_data or {})
    if not product_category:
        equipment_type = (shipment_data.get("equipment_type") or {}).get("machine_readable", "TANKER")
        product_category = equipment_type

    if not location_id or not appointment_date:
        subject = "Error Via Pre-Tender Script"
        plain, html = notify_bodies_for_shipment(
            shipment_data, subject,
            error_detail=f"HAC sheet pre-flight: missing location_id ({location_id!r}) or appointment_date ({appointment_date!r}).",
        )
        notify_support(subject, plain, sw, html_body=html)
        raise RuntimeError(subject)

    sw.log("TRACE", f"HAC sheet pre-flight: checking {scac} capacity for {appointment_date} at {location_id} ({product_category})", ["_check_hac_sheet_capacity_preflight"])
    try:
        sheets = build_google_sheets_client()
        has_capacity = check_sheet_capacity(scac, location_id, appointment_date, product_category, sheets)
    except CapacityError as e:
        subject = f"Error In Tender Script \u2013 Capacity Undefined for Today for {scac}"
        plain, html = notify_bodies_for_hac(
            shipment_data, subject, scac=scac, location_id=location_id,
            appointment_date=appointment_date, product_category=product_category,
            error_detail=str(e),
        )
        notify_support(subject, plain, sw, html_body=html)
        raise RuntimeError(subject) from e
    except Exception as e:
        # Fail open on unexpected errors (sheet infra issue) — log warning but do not block
        sw.log("WARNING", f"HAC sheet pre-flight: unexpected error checking {scac} capacity — failing open: {e}", ["_check_hac_sheet_capacity_preflight"])
        return

    if not has_capacity:
        subject = f"Error In Tender Script \u2013 No Capacity Available for {scac}"
        plain, html = notify_bodies_for_hac(
            shipment_data, subject, scac=scac, location_id=location_id,
            appointment_date=appointment_date, product_category=product_category,
            error_detail=f"Carrier {scac} has no {product_category} capacity for {appointment_date}. Tender was not sent.",
        )
        notify_support(subject, plain, sw, html_body=html)
        raise RuntimeError(subject)

    sw.log("TRACE", f"HAC sheet pre-flight PASSED: {scac} has capacity for {appointment_date} ({product_category})", ["_check_hac_sheet_capacity_preflight"])


def _is_hac_managed_scac(scac: str, location_id: str) -> bool:
    """
    Return True if this SCAC has a tab in the HAC assignment sheet — i.e. it is a
    HAC-managed carrier whose tender must be issued by the HAC sheet assign-carrier
    function, not automatically at shipment creation.

    Probes tab existence by attempting to read cell A1 from the SCAC-named tab.
    A missing tab raises an exception → not HAC-managed → returns False.
    """
    sheet_id = get_assignment_sheet_id(location_id)
    if not sheet_id:
        return False
    try:
        sheets = build_google_sheets_client()
        sheets.get_values(sheet_id, f"'{scac}'!A1")
        return True
    except Exception:
        return False


def _get_pickup_appointment_date_from_shipment(shipment_data: Dict) -> Optional[str]:
    """Return the YYYY-MM-DD appointment/planned date for the first pickup stop."""
    pickup_stop = get_pickup_stop(shipment_data.get("stops") or [])
    if not pickup_stop:
        return None
    appt_window = pickup_stop.get("appointment_window") or {}
    start = appt_window.get("start")
    if start:
        return normalize_date_value(start)
    planned = pickup_stop.get("planned_date")
    return normalize_date_value(planned) if planned else None


def _is_shipment_pickup_scheduled(
    shipment_id: str,
    shipment_data: Dict,
    base_url: str,
    headers: Dict,
) -> bool:
    """
    Returns True iff the first pickup stop has a confirmed SCHEDULED Tempus appointment.

    Fast path: appointment_window.start is populated on the stop → Tempus scheduled it.
    Slow path: query GET /facilities/appointments filtered by facility_id + stop_id
               and check status == "SCHEDULED".

    Returns False if:
    - No pickup stop found
    - appointment_window.start is missing AND Tempus has no SCHEDULED record
    - Tempus record exists but status is UNSCHEDULED
    """
    stops = shipment_data.get("stops") or []
    pickup_stop = get_pickup_stop(stops)
    if not pickup_stop:
        return False

    # Fast path: appointment_window.start set → confirmed scheduled
    appt_window = pickup_stop.get("appointment_window") or {}
    if appt_window.get("start"):
        return True

    # Slow path: query Tempus directly
    stop_id = pickup_stop.get("id") or ""
    location = pickup_stop.get("location") or {}
    facility_id = (
        pickup_stop.get("facility_id")
        or location.get("facility_id")
        or ""
    )
    if not stop_id or not facility_id:
        return False

    try:
        appt_url = (
            f"{base_url}/facilities/appointments?page=1&limit=5"
            f"&facility_id={facility_id}&scheduled_resource_id={shipment_id}&stop_id={stop_id}"
        )
        appt_data = (_api_call(appt_url, "GET", headers=headers) or {}).get("data") or []
        if not appt_data:
            return False
        return (appt_data[0].get("status") or "").upper() == "SCHEDULED"
    except Exception as e:
        logger.warning(f"_is_shipment_pickup_scheduled: Tempus query failed for {shipment_id}: {e}")
        return False


def assign_contract(
    shipment_id: str, order_data: Dict, base_url: str,
    headers: Dict, custom_fields: Dict, sw: "ShipwellProgram",
    bypass_hac_guard: bool = False,
) -> Dict:
    """
    Assign carrier to shipment via applicable contracts or preset SCAC.
    Mirrors assignContract() in assignCarrier.gs.

    bypass_hac_guard: when True, skip the HAC location early-return so the
    routing guide is initiated (with capacity splice) from the HAC assignment
    spreadsheet button flow.  Always False for automatic shipment-creation triggers.
    """
    logger.info("=== assign_contract START ===")
    preset_scac_fid = custom_fields.get("preset_scac", "")
    customer_pickup_carrier_id = custom_fields.get("customer_pickup_carrier_id", "")

    shipment_data = _api_call(f"{base_url}/v2/shipments/{shipment_id}/", "GET", headers=headers)
    preset_scac = (
        get_custom_field_value(shipment_data, preset_scac_fid)
        or get_custom_field_value(order_data, preset_scac_fid)
        if preset_scac_fid else None
    )

    turn_order_fid = custom_fields.get("turn_order_number", "")
    turn_order_number = get_custom_field_value(order_data, turn_order_fid) if turn_order_fid else None
    order_number = order_data.get("order_number", "")
    is_child_turn = bool(turn_order_number and turn_order_number != order_number)

    if not preset_scac or not preset_scac.strip():
        if is_child_turn:
            # Child turn orders must inherit carrier from the parent via handle_turn_order;
            # they must never trigger the routing guide independently.
            sw.log(
                "TRACE",
                f"Child turn order (turn_order_number={turn_order_number}) has no preset SCAC — skipping routing guide; carrier inherited from parent",
                ["assign_contract"],
            )
            logger.info(f"Child turn order {order_number}: skipping routing guide — carrier should come from parent turn")
            return {"skipped": "CHILD_TURN_ORDER"}

        if bool(turn_order_number and turn_order_number == order_number):
            # Parent turn orders with no preset SCAC skip carrier assignment entirely.
            # The carrier will be assigned later via the Google Sheet button, which will
            # trigger the carrier_assigned event and cascade to all child turn orders.
            sw.log(
                "TRACE",
                f"Parent turn order (turn_order_number={turn_order_number}) has no preset SCAC — skipping carrier assignment; will be assigned manually via Google Sheet",
                ["assign_contract"],
            )
            logger.info(f"Parent turn order {order_number}: skipping carrier assignment — awaiting manual assignment via Google Sheet")
            return {"skipped": "PARENT_TURN_ORDER_NO_PRESET"}

        # Orders originating from a HAC Asphalt location must NOT trigger the routing
        # guide automatically. A button in the HAC assignment spreadsheet is used to
        # manually initiate the routing guide for these orders once capacity is confirmed.
        if is_hac_asphalt_location(shipment_data, base_url=base_url, headers=headers) and not bypass_hac_guard:
            location_id = get_first_pickup_location_id(shipment_data, base_url=base_url, headers=headers)
            sw.log(
                "TRACE",
                f"Pickup location '{location_id}' is a HAC Asphalt location — skipping automatic routing guide. "
                "Carrier assignment will be initiated manually via the HAC assignment spreadsheet.",
                ["assign_contract"],
            )
            logger.info(
                f"Shipment {shipment_id}: skipping routing guide for HAC location '{location_id}' "
                "— awaiting manual initiation from spreadsheet button."
            )
            return {"skipped": "HAC_MANUAL_ROUTING", "location_id": location_id}

        policy = find_matching_route_guide_policy(shipment_data, order_data, base_url, headers, sw)
        _initiate_routing_guide(shipment_id, base_url, headers, sw, policy=policy, shipment_data=shipment_data)
        return {"routing_guide_policy_id": policy.get("id"), "workflow_id": policy.get("workflow_id")}

    normalized_scac = preset_scac.upper().strip()
    logger.info(f"Preset SCAC: {normalized_scac}")

    # --- Guard: skip if shipment already tendered or has an open tender ---
    # Shipwell fires order.created twice (BU write-back) and routing guide workflows can
    # also trigger carrier-config on a freshly tendered shipment. Check both the shipment
    # state and existing tenders before doing anything — avoids duplicate $0 bare tenders.
    _current_state = shipment_data.get("state", "")
    if _current_state in ("tendered", "carrier_confirmed", "dispatched", "in_transit", "at_pickup", "at_delivery", "delivered"):
        sw.log("TRACE", f"assign_contract: shipment already in state '{_current_state}' — skipping carrier assignment", ["assign_contract"])
        logger.info("=== assign_contract SKIPPED (shipment already tendered/assigned) ===")
        return {"skipped": "ALREADY_TENDERED", "state": _current_state}

    _existing_tenders = (_api_call(
        f"{base_url}/v2/tenders/?shipment_id={shipment_id}&page_size=10",
        "GET", headers=headers,
    ) or {}).get("results") or []
    _open_tender = next(
        (t for t in _existing_tenders if t.get("status") in ("open", "pending", "accepted")),
        None,
    )
    if _open_tender:
        sw.log("TRACE", f"assign_contract: open tender {_open_tender.get('id')} already exists (status={_open_tender.get('status')}) — skipping", ["assign_contract"])
        logger.info("=== assign_contract SKIPPED (tender already exists) ===")
        return {"skipped": "TENDER_EXISTS", "tender_id": _open_tender.get("id")}

    # TENDER CREATION LOCK: DynamoDB conditional write
    # ─────────────────────────────────────────────────────────────────────────
    # Two concurrent Lambda invocations for the same order can both pass the
    # state + tender checks above (neither tender exists yet for either of them).
    # Use the same DynamoDB distributed mutex as shipment creation: only the first
    # invocation can write the lock key; all others get ConditionalCheckFailedException
    # and bail out immediately (the winner's tender will be visible on any retry).
    # Key: "tender_creation:{shipment_id}"  TTL: 5 minutes
    import boto3 as _dynamo_boto3
    import time as _dynamo_time
    import os as _dynamo_os
    import random as _dynamo_random
    _tender_lock_key = f"tender_creation:{shipment_id}"
    _dynamo_client = None
    try:
        _dynamo_client = _dynamo_boto3.client("dynamodb", region_name=_dynamo_os.environ.get("DYNAMO_REGION", "us-west-2"))
        _dynamo_client.put_item(
            TableName=_dynamo_os.environ.get("EMAIL_DEDUP_TABLE", "hfs-email-dedup"),
            Item={
                "dedup_key": {"S": _tender_lock_key},
                "shipment_id": {"S": shipment_id},
                "expires_at": {"N": str(int(_dynamo_time.time()) + 300)},
            },
            ConditionExpression="attribute_not_exists(dedup_key)",
        )
        logger.info(f"Tender creation lock acquired for shipment {shipment_id}")
    except Exception as _tlock_err:
        _tlock_err_str = str(_tlock_err)
        if "ConditionalCheckFailedException" in _tlock_err_str:
            # Another Lambda is already tendering this shipment — bail out.
            sw.log("TRACE", f"assign_contract: tender creation lock contention for {shipment_id} — another invocation is already tendering, skipping", ["assign_contract"])
            logger.info("=== assign_contract SKIPPED (tender lock contention — another invocation is tendering) ===")
            return {"skipped": "TENDER_LOCK_CONTENTION", "shipment_id": shipment_id}
        else:
            # DynamoDB unavailable — fail open, log and continue (rare)
            logger.warning(f"Tender creation lock DynamoDB write failed (fail open): {_tlock_err}")

    is_customer_pickup = normalized_scac == "CUST"

    if is_customer_pickup:
        if not customer_pickup_carrier_id:
            raise ValueError("customer_pickup_carrier_id not in custom_fields")
        carrier_rel = _api_call(f"{base_url}/v2/carrier-relationships/{customer_pickup_carrier_id}/", "GET", headers=headers)
        result = assign_or_tender_carrier(
            shipment_id, shipment_data, order_data, carrier_rel, None, base_url, headers,
            direct_assign=True, suppress_notifications=True, sw=sw,
        )
        sw.log("TRACE", f"Customer pickup carrier assigned to shipment {shipment_id}", ["assign_contract"])
        logger.info("=== assign_contract COMPLETED ===")
        return result

    preset_carrier_rel = get_carrier_by_scac(normalized_scac, base_url, headers)

    # PPTAS (and CRUDE) orders are pre-arranged — the carrier is already committed.
    # Skip capacity checks and tendering; go straight to direct assign.
    # Still attempt a contract lookup for the preset carrier so that financials
    # (rate/charge line items) are populated on the shipment. If no matching contract
    # is found, fall back to contract=None (no financials, existing behaviour).
    direct_assign = should_direct_assign_preset_carrier(order_data, shipment_data, custom_fields)
    if direct_assign:
        sw.log("TRACE",
               f"assign_contract: PPTAS/CRUDE direct-assign for {normalized_scac} — looking up contract for financials",
               ["assign_contract"])
        logger.info(f"assign_contract: PPTAS/CRUDE direct-assign {normalized_scac} — attempting contract lookup")
        _pptas_contract = None
        try:
            _pptas_contracts_resp = _api_call(
                f"{base_url}/v2/contracts/applicable-contracts/",
                "POST", headers=headers,
                body={"shipment_id": shipment_id, "request_type": "BY_SHIPMENT"},
            )
            _pptas_contracts = _pptas_contracts_resp.get("data") or []
            _pptas_matches = filter_contract_matches(_pptas_contracts, preset_carrier_rel["id"]) if preset_carrier_rel else []
            if _pptas_matches:
                _pptas_contract = _pptas_matches[0].get("contract")
                sw.log("TRACE",
                       f"assign_contract: PPTAS/CRUDE — found contract {(_pptas_contract or {}).get('id')} for {normalized_scac}",
                       ["assign_contract"])
                logger.info(f"assign_contract: PPTAS/CRUDE — using contract {(_pptas_contract or {}).get('id')} for {normalized_scac}")
            else:
                sw.log("TRACE",
                       f"assign_contract: PPTAS/CRUDE — no contract found for {normalized_scac}, proceeding without contract",
                       ["assign_contract"])
                logger.warning(f"assign_contract: PPTAS/CRUDE — no applicable contract for {normalized_scac}; financials will not be set")
        except Exception as _pptas_contract_err:
            logger.warning(f"assign_contract: PPTAS/CRUDE contract lookup failed ({_pptas_contract_err}); proceeding without contract")
        result = assign_or_tender_carrier(
            shipment_id, shipment_data, order_data, preset_carrier_rel, _pptas_contract, base_url, headers,
            direct_assign=True, suppress_notifications=True, sw=sw,
        )
        sw.log("TRACE", f"Preset SCAC {normalized_scac} assigned to shipment {shipment_id} (PPTAS/CRUDE, contract={'set' if _pptas_contract else 'none'})", ["assign_contract"])
        logger.info("=== assign_contract COMPLETED (PPTAS/CRUDE direct-assign) ===")
        return result

    contracts_resp = _api_call(
        f"{base_url}/v2/contracts/applicable-contracts/",
        "POST", headers=headers,
        body={"shipment_id": shipment_id, "request_type": "BY_SHIPMENT"},
    )
    contracts = contracts_resp.get("data") or []
    if not contracts:
        raise RuntimeError("No applicable contracts found")

    selected = require_exactly_one_contract(
        filter_contract_matches(contracts, preset_carrier_rel["id"]),
        shipment_data,
        normalized_scac,
        sw,
    )
    contract = selected["contract"]
    contract_id = contract["id"]

    cap = _api_call(
        f"{base_url}/v2/carrier-capacity/carrier-capacity/available/?shipment_id={shipment_id}&contract_id={contract_id}",
        "GET", headers=headers,
    )
    if not cap.get("capacity_available"):
        raise RuntimeError(f"Carrier capacity not available: {cap.get('reason')}")

    # direct_assign is already False here (PPTAS/CRUDE exited above)

    # --- HAC carrier gate + capacity pre-flight ---
    # For HAC asphalt locations, check whether this SCAC is managed by the HAC
    # assignment sheet. If it is, skip the tender entirely — the HAC sheet
    # assign-carrier function will issue the tender when the dispatcher selects
    # this shipment. Non-HAC carriers at the same location tender immediately
    # (after a capacity pre-flight to confirm availability).
    if is_hac_asphalt_location(shipment_data, base_url=base_url, headers=headers):
        _hac_location_id = get_first_pickup_location_id(shipment_data, base_url=base_url, headers=headers)
        if _hac_location_id and _is_hac_managed_scac(normalized_scac, _hac_location_id) and not bypass_hac_guard:
            sw.log(
                "TRACE",
                f"assign_contract: {normalized_scac} is a HAC-managed carrier at location "
                f"{_hac_location_id} — skipping tender; HAC sheet will assign.",
                ["assign_contract"],
            )
            logger.info(f"=== assign_contract SKIPPED (HAC-managed SCAC {normalized_scac}) ===")
            return {"skipped": "HAC_MANAGED_SCAC", "scac": normalized_scac}
        # Non-HAC carrier at a HAC location — capacity pre-flight then tender normally.
        _check_hac_sheet_capacity_preflight(normalized_scac, shipment_data, sw, base_url, headers, order_data=order_data)

    if direct_assign:
        # Direct assign path (customer pickup, carrier-managed dispatch, PPTAS, etc.)
        result = assign_or_tender_carrier(
            shipment_id, shipment_data, order_data, preset_carrier_rel, contract, base_url, headers,
            direct_assign=True, suppress_notifications=True, sw=sw,
        )
        sw.log("TRACE", f"Preset SCAC {normalized_scac} assigned to shipment {shipment_id}", ["assign_contract"])

        # Re-apply vendor_point_of_contact after direct assign.
        # build_carrier_assignment_body includes the POC id, but the carrier-assignments PUT may
        # not persist it (API sometimes ignores it on direct-assign). Re-assert via a follow-up PUT.
        if not DRY_RUN:
            pocs = preset_carrier_rel.get("point_of_contacts") or []
            logger.info(f"[POC-reapply] pocs count={len(pocs)} shipment={shipment_id}")
            preferred_poc = _select_preferred_poc(pocs)
            logger.info(f"[POC-reapply] preferred_poc={preferred_poc.get('id') if preferred_poc else None}")
            if preferred_poc:
                _fresh = _api_call(f"{base_url}/v2/shipments/{shipment_id}/", "GET", headers=headers) or {}
                vendor_rel_id = (_fresh.get("relationship_to_vendor") or {}).get("id")
                logger.info(f"[POC-reapply] vendor_rel_id={vendor_rel_id}")
                if vendor_rel_id:
                    va_body = _api_call(f"{base_url}/v2/shipments/{shipment_id}/carrier-assignments/{vendor_rel_id}/", "GET", headers=headers) or {}
                    va_body["vendor_point_of_contact"] = preferred_poc.get("id")
                    poc_result = _api_call(f"{base_url}/v2/shipments/{shipment_id}/carrier-assignments/{vendor_rel_id}/", "PUT", headers=headers, body=va_body)
                    logger.info(f"[POC-reapply] PUT result poc={( poc_result or {}).get('vendor_point_of_contact')}")
                    sw.log("TRACE", f"Re-applied vendor_point_of_contact={preferred_poc.get('id')!r} on VA {vendor_rel_id} after direct assign", ["assign_contract"])
    else:
        # Tender path — establish carrier-config relationship first, then send real tender.
        # Without a prior carrier-config POST, Shipwell creates relationship_to_vendor
        # internally when the carrier accepts the tender, which generates bare $0
        # UNSPECIFIED side-effect tenders and duplicate carrier notification emails.
        # Establishing the relationship here prevents that entirely.
        _establish_carrier_config_relationship(
            shipment_id, shipment_data, preset_carrier_rel, contract_id,
            order_data, base_url, headers, sw,
        )
        # Refresh shipment_data after carrier-config POST (state/rtv may have changed)
        shipment_data = _api_call(f"{base_url}/v2/shipments/{shipment_id}/", "GET", headers=headers) or shipment_data
        result = post_tender_direct(
                shipment_id, shipment_data, preset_carrier_rel, contract,
                base_url, headers, sw, order_data=order_data,
                custom_fields=custom_fields,
            )
        sw.log("TRACE", f"Preset SCAC {normalized_scac} tendered to shipment {shipment_id}", ["assign_contract"])

        # Re-apply vendor_point_of_contact after post_tender_direct.
        # The tender POST causes Shipwell to create/refresh the VA internally, clearing the POC
        # that was set by the prior carrier-config POST. We re-assert it here so SLU carrier
        # users can see the shipment on their load board.
        if not DRY_RUN:
            pocs = preset_carrier_rel.get("point_of_contacts") or []
            preferred_poc = _select_preferred_poc(pocs)
            if preferred_poc:
                _fresh = _api_call(f"{base_url}/v2/shipments/{shipment_id}/", "GET", headers=headers) or {}
                vendor_rel_id = (_fresh.get("relationship_to_vendor") or {}).get("id")
                if vendor_rel_id:
                    va_body = _api_call(f"{base_url}/v2/shipments/{shipment_id}/carrier-assignments/{vendor_rel_id}/", "GET", headers=headers) or {}
                    va_body["vendor_point_of_contact"] = preferred_poc.get("id")
                    _api_call(f"{base_url}/v2/shipments/{shipment_id}/carrier-assignments/{vendor_rel_id}/", "PUT", headers=headers, body=va_body)
                    sw.log("TRACE", f"Re-applied vendor_point_of_contact={preferred_poc.get('id')!r} on VA {vendor_rel_id} after post_tender_direct", ["assign_contract"])

    logger.info("=== assign_contract COMPLETED ===")
    return result


def assign_specific_contract(
    shipment_id: str, vendor_id: str, contract_id: Optional[str],
    order_data: Dict, base_url: str, headers: Dict, custom_fields: Dict, sw: "ShipwellProgram",
) -> Dict:
    """
    Assign a specific carrier (by vendor_id) and contract to a shipment (turn order flow).
    Mirrors assignSpecificContract() in assignCarrier.gs.
    """
    logger.info("=== assign_specific_contract START ===")
    rel_resp = _api_call(f"{base_url}/v2/carrier-relationships/?vendor_id={vendor_id}", "GET", headers=headers)
    results = rel_resp.get("results") or []
    if not results:
        raise RuntimeError(f"No carrier relationship found for vendor_id={vendor_id}")

    carrier_rel = results[0]
    shipment_data = _api_call(f"{base_url}/v2/shipments/{shipment_id}/", "GET", headers=headers)

    equipment_type = shipment_data.get("equipment_type") or {}
    mode = shipment_data.get("mode") or {}

    if contract_id:
        contracts_resp = _api_call(
            f"{base_url}/v2/contracts/applicable-contracts/",
            "POST", headers=headers,
            body={"shipment_id": shipment_id, "request_type": "BY_SHIPMENT"},
        )
        contracts = contracts_resp.get("data") or []
        matching = next((c for c in contracts if c.get("contract", {}).get("id") == contract_id), None)
        if matching:
            equipment_type = matching["contract"]["equipment_types"][0]
            mode = matching["contract"]["modes"][0]
            cap = _api_call(
                f"{base_url}/v2/carrier-capacity/carrier-capacity/available/?shipment_id={shipment_id}&contract_id={contract_id}",
                "GET", headers=headers,
            )
            if not cap.get("capacity_available"):
                logger.warning(f"Capacity not available: {cap.get('reason')} - proceeding anyway (parent had this carrier)")
        else:
            logger.warning(f"Contract {contract_id} not in applicable contracts - using shipment equipment/mode")

    config_body = build_carrier_config_body(carrier_rel, equipment_type, mode, contract_id, order_data)

    if DRY_RUN:
        logger.info(f"[DRY_RUN] Would POST carrier-config (specific): {json.dumps(config_body, default=str)}")
    else:
        _api_call(f"{base_url}/v2/shipments/{shipment_id}/carrier-config/", "POST", headers=headers, body=config_body)

    shipment_data = _api_call(f"{base_url}/v2/shipments/{shipment_id}/", "GET", headers=headers)
    vendor_rel_id = (shipment_data.get("relationship_to_vendor") or {}).get("id")

    assignment_body = {
        "vendor": carrier_rel.get("shipwell_vendor"),
        "contract_id": contract_id,
        "vendor_charge_line_items": [],
        "customer_charge_line_items": [],
    }

    if DRY_RUN:
        logger.info(f"[DRY_RUN] Would PUT carrier-assignment (specific): vendor_rel_id={vendor_rel_id}")
        logger.info(f"[DRY_RUN] Would POST /v2/tenders/ to notify carrier company {carrier_rel.get('shipwell_vendor', {}).get('id')}")
        return assignment_body

    result = _api_call(
        f"{base_url}/v2/shipments/{shipment_id}/carrier-assignments/{vendor_rel_id}/",
        "PUT", headers=headers, body=assignment_body,
    )
    logger.info(f"Specific carrier assigned: {result.get('id')}")
    sw.log("TRACE", f"Turn order carrier assigned to shipment {shipment_id}", ["assign_specific_contract"])

    # Create a tender to trigger the carrier email notification.
    # The routing guide handles this for parent shipments; for turn order children
    # we bypass the routing guide so we POST the tender directly.
    vendor_company_id = (carrier_rel.get("shipwell_vendor") or {}).get("id")
    if vendor_company_id:
        try:
            tender_result = _api_call(
                f"{base_url}/v2/tenders/",
                "POST", headers=headers,
                body={"shipment": shipment_id, "tender_to_company": vendor_company_id},
            )
            tender_id = (tender_result or {}).get("id")
            sw.log("TRACE", f"Turn order child tender created: {tender_id} for shipment {shipment_id}", ["assign_specific_contract"])
        except Exception as e:
            sw.log("WARNING", f"Turn order child tender creation failed (non-fatal): {e}", ["assign_specific_contract"])
    else:
        sw.log("WARNING", f"Could not create tender — no vendor_company_id for shipment {shipment_id}", ["assign_specific_contract"])

    # Mirror customer_charge_line_items → vendor_charge_line_items so the carrier
    # can see their financials. Shipwell populates customer_charge_line_items via the
    # tender POST above; we read the settled state and copy to the vendor side.
    import time as _time
    _time.sleep(2)  # brief wait for Shipwell to settle charge calculation from tender
    mirror_vendor_charges(shipment_id, vendor_rel_id, base_url, headers, sw=sw)

    logger.info("=== assign_specific_contract COMPLETED ===")
    return result


# ---------------------------------------------------------------------------
# HAC Carrier Waterfall
# ---------------------------------------------------------------------------

def tender_hac_waterfall_next(
    shipment_id: str,
    shipment_data: Dict,
    order_data: Dict,
    rg_steps: List[Dict],
    base_url: str,
    headers: Dict,
    sw: "ShipwellProgram",
    carrier_constraints: Optional[Dict] = None,
    spreadsheet_id: str = "",
    custom_fields: Optional[Dict] = None,
) -> Dict:
    """
    Advance the HAC carrier waterfall for a shipment using routing guide steps.

    ``rg_steps`` is the ordered list returned by ``_get_routing_guide_steps()``:
        [ { step_num, step_id, company_id, scac, duration_seconds }, ... ]

    ``carrier_constraints`` is accepted for backward compatibility with the Apps
    Script payload but is no longer used for gating. The constraint check is now
    performed live by reading the Carrier Constraints tab directly (see Gate 2).

    ``spreadsheet_id`` is the Google Sheet ID used to read the Carrier Constraints
    tab (Available vs. Constraint floor comparison) at Gate 2.

    Gate logic per step (in order):
      1. SCAC already rejected/expired this shipment → skip
      2. Carrier Constraints floor check: reads Available (cols B–E) and Constraint
         (cols G–J) from the Carrier Constraints tab live. If available_drivers ≤
         constraint_drivers OR available_{equipment_bucket} ≤ constraint_{bucket} → skip.
         Blank constraint on a dimension = no restriction on that dimension.
      3. HAC sheet capacity check → skip if no capacity
      4. If eligible: resolve carrier relationship + contract, assign, tender
         with expires_at = now + duration_seconds

    Returns:
      { "tendered": True,  "scac": str,  "exhausted": False }
      { "tendered": False, "exhausted": True,  "skipped": "ALL_EXHAUSTED" }
      { "tendered": False, "exhausted": False, "skipped": reason }
    """
    from datetime import datetime, timezone, timedelta

    logger.info(f"=== tender_hac_waterfall_next START shipment={shipment_id} steps={[s['scac'] for s in rg_steps]} ===")

    if not rg_steps:
        return {"tendered": False, "exhausted": True, "skipped": "NO_RG_STEPS"}

    if carrier_constraints is None:
        carrier_constraints = {}

    # ── Build skip set from tender history ────────────────────────────────────
    tenders_resp = _api_call(
        f"{base_url}/v2/tenders/?shipment_id={shipment_id}&page_size=50",
        "GET", headers=headers,
    ) or {}
    all_tenders = tenders_resp.get("results") or []

    rejected_scacs: set = set()
    for t in all_tenders:
        if t.get("status") not in ("rejected", "expired"):
            continue
        t_company = t.get("company") or {}
        for code in (t_company.get("identifying_codes") or []):
            if code.get("type") == "SCAC":
                scac_val = (code.get("value") or "").upper().strip()
                if scac_val:
                    rejected_scacs.add(scac_val)
                break

    logger.info(f"tender_hac_waterfall_next: rejected/expired SCACs: {sorted(rejected_scacs)}")
    sw.log("TRACE", f"Waterfall: {len(all_tenders)} tenders, rejected SCACs: {sorted(rejected_scacs)}", ["tender_hac_waterfall_next"])

    # ── Shared context needed for capacity + bucket ───────────────────────────
    location_id      = get_first_pickup_location_id(shipment_data, base_url=base_url, headers=headers)
    appointment_date = _get_pickup_appointment_date_from_shipment(shipment_data) or ""
    product_category = get_shipment_product_category(shipment_data, order_data)
    if not product_category:
        product_category = (shipment_data.get("equipment_type") or {}).get("machine_readable", "TANKER")

    # Map product_category to constraint bucket name
    _bucket_map = {
        "BLUE_DOT":  "blue_dot",
        "GREEN_DOT": "green_dot",
        "ASPHALT":   "asphalt",
        "EMULSION":  "blue_dot",
        "EMULSIONS": "blue_dot",
    }
    constraint_bucket = _bucket_map.get((product_category or "").upper(), "asphalt")

    tried_all = True

    for step in rg_steps:
        scac_upper       = (step.get("scac") or "").upper().strip()
        company_id       = step.get("company_id") or ""
        duration_seconds = int(step.get("duration_seconds") or 0)
        step_id          = step.get("step_id", "")

        if not scac_upper and not company_id:
            sw.log("WARNING", f"Waterfall: step {step_id} has no SCAC or company_id — skipping", ["tender_hac_waterfall_next"])
            continue

        # ── Gate 1: already rejected/expired ─────────────────────────────────
        if scac_upper and scac_upper in rejected_scacs:
            logger.info(f"tender_hac_waterfall_next: skipping {scac_upper} ({step_id}) — already rejected/expired")
            continue

        # ── Gate 2: carrier constraints (reserve floor check) ─────────────────
        # The constraint values are floors: if Available ≤ Constraint for drivers
        # OR for the shipment's equipment bucket, skip this carrier.
        # A blank constraint (None) means no restriction on that dimension.
        if scac_upper and spreadsheet_id:
            try:
                blocked, gate_reason = _check_carrier_constraint_gate(
                    spreadsheet_id, scac_upper, constraint_bucket, sw,
                )
                if blocked:
                    sw.log("TRACE", f"Waterfall: skipping {scac_upper} ({step_id}) — constraint gate: {gate_reason}", ["tender_hac_waterfall_next"])
                    logger.info(f"tender_hac_waterfall_next: skipping {scac_upper} — {gate_reason}")
                    continue
            except Exception as cg_err:
                sw.log("WARNING", f"Waterfall: constraint gate check failed for {scac_upper}: {cg_err} — allowing through", ["tender_hac_waterfall_next"])

        # ── Gate 3: HAC sheet capacity ────────────────────────────────────────
        if location_id and HAC_CAPACITY_INTEGRATION_ENABLED:
            try:
                from google_sheets_client import build_google_sheets_client as _build_gsc
                _sheets = _build_gsc()
                has_cap = check_sheet_capacity(
                    scac_upper, location_id, appointment_date, product_category, _sheets,
                )
                if not has_cap:
                    sw.log("TRACE", f"Waterfall: skipping {scac_upper} ({step_id}) — no HAC sheet capacity for {appointment_date}/{product_category}", ["tender_hac_waterfall_next"])
                    logger.info(f"tender_hac_waterfall_next: skipping {scac_upper} — no sheet capacity")
                    continue
            except CapacityError as ce:
                sw.log("WARNING", f"Waterfall: capacity check failed for {scac_upper}: {ce} — skipping", ["tender_hac_waterfall_next"])
                continue

        # ── Eligible — resolve carrier relationship + contract ────────────────
        tried_all  = False
        carrier_rel = get_carrier_by_scac(scac_upper, base_url, headers) if scac_upper else None

        # Fall back to company_id lookup if SCAC resolution failed
        if not carrier_rel and company_id:
            try:
                company_resp = _api_call(f"{base_url}/v2/companies/{company_id}/", "GET", headers=headers) or {}
                if company_resp.get("id"):
                    carrier_rel = {"shipwell_vendor": company_resp, "id": company_resp.get("id")}
                    logger.info(f"tender_hac_waterfall_next: resolved carrier via company_id {company_id} for step {step_id}")
            except Exception as e:
                sw.log("WARNING", f"Waterfall: company lookup failed for {company_id}: {e}", ["tender_hac_waterfall_next"])

        if not carrier_rel:
            sw.log("WARNING", f"Waterfall: carrier not found for SCAC={scac_upper!r} company_id={company_id!r} ({step_id}) — skipping", ["tender_hac_waterfall_next"])
            tried_all = True
            continue

        contracts_resp = _api_call(
            f"{base_url}/v2/contracts/applicable-contracts/",
            "POST", headers=headers,
            body={"shipment_id": shipment_id, "request_type": "BY_SHIPMENT"},
        )
        contracts = (contracts_resp or {}).get("data") or []
        if not contracts:
            sw.log("WARNING", f"Waterfall: no applicable contracts for shipment {shipment_id} — cannot tender {scac_upper}", ["tender_hac_waterfall_next"])
            return {"tendered": False, "exhausted": False, "skipped": "NO_CONTRACTS"}

        matching = filter_contract_matches(contracts, carrier_rel["id"])
        if not matching:
            sw.log("WARNING", f"Waterfall: no matching contract for {scac_upper} on shipment {shipment_id} ({step_id}) — skipping", ["tender_hac_waterfall_next"])
            tried_all = True
            continue

        contract = matching[0]["contract"]

        # Establish carrier-config relationship
        _establish_carrier_config_relationship(
            shipment_id, shipment_data, carrier_rel, contract["id"],
            order_data, base_url, headers, sw,
        )
        # Refresh shipment after carrier-config POST
        shipment_data = _api_call(
            f"{base_url}/v2/shipments/{shipment_id}/", "GET", headers=headers,
        ) or shipment_data
        vendor_rel_id = (shipment_data.get("relationship_to_vendor") or {}).get("id")

        assignment_body = {
            "vendor": carrier_rel.get("shipwell_vendor"),
            "contract_id": contract["id"],
            "vendor_charge_line_items": [],
            "customer_charge_line_items": [],
        }

        if DRY_RUN:
            logger.info(f"[DRY_RUN] Would PUT carrier-assignment: {scac_upper} ({step_id}) vendor_rel_id={vendor_rel_id}")
            return {"tendered": True, "scac": scac_upper, "exhausted": False}

        waterfall_result = _api_call(
            f"{base_url}/v2/shipments/{shipment_id}/carrier-assignments/{vendor_rel_id}/",
            "PUT", headers=headers, body=assignment_body,
        )
        # Mirror customer_charge_line_items → vendor_charge_line_items so the carrier
        # can see their financials in the waterfall tender.
        if vendor_rel_id:
            mirror_vendor_charges(
                shipment_id, vendor_rel_id, base_url, headers,
                sw=sw, existing_assignment=waterfall_result,
            )

        # Apply preplanned accessorials (e.g. TANKWASH) to the carrier assignment
        # so they appear in the tender financials. Must run after the carrier-assignment
        # PUT so the relationship_to_vendor and contract_id are in place.
        if order_data:
            try:
                from accessorials import apply_preplanned_accessorials as _apply_pa
                _shipment_for_pa = _api_call(f"{base_url}/v2/shipments/{shipment_id}/", "GET", headers=headers) or shipment_data
                _apply_pa(_shipment_for_pa, order_data, base_url, headers, custom_fields or {}, sw)
            except Exception as _pa_err:
                sw.log("WARNING", f"Waterfall: apply_preplanned_accessorials failed for {scac_upper}: {_pa_err} — non-fatal", ["tender_hac_waterfall_next"])

        # Consume HAC capacity sheet (increment consumed column)
        if location_id and HAC_CAPACITY_INTEGRATION_ENABLED:
            try:
                from google_sheets_client import build_google_sheets_client as _build_gsc
                _sheets = _build_gsc()
                sheet_id = get_assignment_sheet_id(location_id)
                if sheet_id:
                    range_notation = f"'{scac_upper}'!A:Z"
                    rows = _sheets.get_values(sheet_id, range_notation)
                    if len(rows) >= 4:
                        section_row = rows[1]
                        header_row  = rows[2]
                        columns     = detect_capacity_columns(header_row, section_row)
                        row_index   = find_date_row(rows[3:], appointment_date)
                        row         = rows[3 + row_index]
                        abs_row_index = 3 + row_index
                        consumed_col, new_consumed_value, _ = consume_capacity_row(row, columns, product_category)
                        cell_range = row_update_range(scac_upper, abs_row_index, start_col=consumed_col, end_col=consumed_col)
                        _sheets.update_values(sheet_id, cell_range, [[new_consumed_value]])
                        sw.log("TRACE", f"Waterfall: consumed HAC capacity for {scac_upper} on {appointment_date} (col={consumed_col} val={new_consumed_value})", ["tender_hac_waterfall_next"])
            except Exception as cap_err:
                sw.log("WARNING", f"Waterfall: HAC capacity consume failed for {scac_upper}: {cap_err}", ["tender_hac_waterfall_next"])

        # Compute expires_at from routing guide step duration
        expires_at: Optional[str] = None
        if duration_seconds and duration_seconds > 0:
            expires_at = (datetime.now(timezone.utc) + timedelta(seconds=duration_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Send tender with expiry
        vendor_company_id = (carrier_rel.get("shipwell_vendor") or {}).get("id")
        tender_id: Optional[str] = None
        if vendor_company_id:
            try:
                tender_body: Dict = {"shipment": shipment_id, "tender_to_company": vendor_company_id}
                if expires_at:
                    tender_body["expires_at"] = expires_at
                tender_result = _api_call(
                    f"{base_url}/v2/tenders/", "POST", headers=headers, body=tender_body,
                )
                tender_id = (tender_result or {}).get("id")
                sw.log(
                    "TRACE",
                    f"Waterfall tender created: {tender_id} for {scac_upper} ({step_id}) expires_at={expires_at!r}",
                    ["tender_hac_waterfall_next"],
                )
                logger.info(f"tender_hac_waterfall_next: tendered to {scac_upper} ({step_id}), tender_id={tender_id}, expires_at={expires_at}")
            except Exception as e:
                sw.log("WARNING", f"Waterfall tender creation failed for {scac_upper} (non-fatal): {e}", ["tender_hac_waterfall_next"])

        logger.info(f"=== tender_hac_waterfall_next COMPLETE: tendered to {scac_upper} ({step_id}) ===")
        return {"tendered": True, "scac": scac_upper, "exhausted": False}

    logger.info(f"tender_hac_waterfall_next: all steps exhausted for shipment {shipment_id}")
    sw.log("TRACE", f"Waterfall: all routing guide steps exhausted for shipment {shipment_id}", ["tender_hac_waterfall_next"])
    return {"tendered": False, "exhausted": tried_all, "skipped": "ALL_EXHAUSTED"}


def _read_constraint_tab_for_scac(
    spreadsheet_id: str,
    scac: str,
    sheets,
) -> Optional[Dict]:
    """
    Reads a single SCAC row from the Carrier Constraints tab and returns
    both the Available values (cols B–E) and Constraint values (cols G–J).

    Tab layout (1-based columns, rows):
      Row 5:  section headers  — "Available" (merged B–E), "Carrier" (F), "Constraint" (merged G–J)
      Row 6:  column headers   — # Drivers, Blue Dot, Green Dot, Asphalt  (repeated in each section)
      Col B–E = Available      Col F = SCAC label      Col G–J = Constraint
      Data rows start at row 7

    Returns a dict:
      {
        'available': { 'drivers': int|None, 'blue_dot': int|None, 'green_dot': int|None, 'asphalt': int|None },
        'constraint': { 'drivers': int|None, 'blue_dot': int|None, 'green_dot': int|None, 'asphalt': int|None },
      }
    or None if the SCAC is not found in the tab.
    """
    CONSTRAINTS_TAB = "Carrier Constraints"
    HEADER_ROW      = 6
    DATA_START_ROW  = 7
    SCAC_COL        = 6   # col F (1-based)
    AVAIL_COL_START = 2   # col B (1-based)
    AVAIL_NUM_COLS  = 4   # B–E: # Drivers, Blue Dot, Green Dot, Asphalt
    CONSTR_COL_START = 7  # col G (1-based)
    CONSTR_NUM_COLS  = 4  # G–J: # Drivers, Blue Dot, Green Dot, Asphalt

    # Read column headers from Available section (B6:E6) and Constraint section (G6:J6)
    avail_header_range  = f"'{CONSTRAINTS_TAB}'!B{HEADER_ROW}:E{HEADER_ROW}"
    constr_header_range = f"'{CONSTRAINTS_TAB}'!G{HEADER_ROW}:J{HEADER_ROW}"
    avail_headers  = (sheets.get_values(spreadsheet_id, avail_header_range) or [[]])[0]
    constr_headers = (sheets.get_values(spreadsheet_id, constr_header_range) or [[]])[0]

    # Normalize header name → payload key
    _hdr_to_key = {
        "# drivers": "drivers",
        "blue dot":  "blue_dot",
        "green dot": "green_dot",
        "asphalt":   "asphalt",
    }
    avail_keys  = [_hdr_to_key.get(str(h).strip().lower()) for h in avail_headers]
    constr_keys = [_hdr_to_key.get(str(h).strip().lower()) for h in constr_headers]

    # Find the SCAC row
    scac_range = f"'{CONSTRAINTS_TAB}'!F{DATA_START_ROW}:F50"
    scac_rows  = sheets.get_values(spreadsheet_id, scac_range) or []
    target_row = None
    for i, row in enumerate(scac_rows):
        cell_scac = str(row[0]).strip().upper() if row else ""
        if cell_scac == scac.upper():
            target_row = DATA_START_ROW + i
            break

    if target_row is None:
        return None

    # Read Available (B–E) and Constraint (G–J) for this row
    avail_range  = f"'{CONSTRAINTS_TAB}'!B{target_row}:E{target_row}"
    constr_range = f"'{CONSTRAINTS_TAB}'!G{target_row}:J{target_row}"
    avail_row  = (sheets.get_values(spreadsheet_id, avail_range)  or [[]])[0]
    constr_row = (sheets.get_values(spreadsheet_id, constr_range) or [[]])[0]

    def _parse_cell(val) -> Optional[int]:
        """Blank → None (no data); numeric → int."""
        if val is None or str(val).strip() == "":
            return None
        try:
            return int(float(str(val)))
        except (ValueError, TypeError):
            return None

    def _row_to_dict(row, keys):
        result = {}
        for i, key in enumerate(keys):
            if key:
                result[key] = _parse_cell(row[i] if i < len(row) else None)
        return result

    return {
        "available":  _row_to_dict(avail_row,  avail_keys),
        "constraint": _row_to_dict(constr_row, constr_keys),
    }


def _check_carrier_constraint_gate(
    spreadsheet_id: str,
    scac: str,
    equipment_bucket: str,
    sw: "ShipwellProgram",
) -> tuple:
    """
    Gate check: read Available and Constraint values from the Carrier Constraints
    tab and return (blocked: bool, reason: str).

    Logic (per column with a non-None constraint value):
      If available_value <= constraint_value → blocked (carrier is at or below reserve floor)

    Checks two dimensions for each shipment:
      1. # Drivers  — always checked regardless of equipment type
      2. equipment_bucket  — the shipment-specific product column
                             ('blue_dot', 'green_dot', or 'asphalt')

    A blank constraint (None) on a dimension = no restriction on that dimension.
    If SCAC is not found in the tab, the gate passes (no constraint configured).
    """
    from google_sheets_client import build_google_sheets_client as _build_gsc
    sheets = _build_gsc()

    row_data = _read_constraint_tab_for_scac(spreadsheet_id, scac, sheets)
    if row_data is None:
        # SCAC not in constraints tab — no restriction
        return False, ""

    available  = row_data["available"]
    constraint = row_data["constraint"]

    # Check # Drivers
    avail_drivers  = available.get("drivers")
    constr_drivers = constraint.get("drivers")
    if constr_drivers is not None and avail_drivers is not None:
        if avail_drivers <= constr_drivers:
            return True, f"available drivers ({avail_drivers}) <= constraint ({constr_drivers})"

    # Check shipment equipment bucket
    avail_bucket  = available.get(equipment_bucket)
    constr_bucket = constraint.get(equipment_bucket)
    if constr_bucket is not None and avail_bucket is not None:
        if avail_bucket <= constr_bucket:
            return True, f"available {equipment_bucket} ({avail_bucket}) <= constraint ({constr_bucket})"

    return False, ""
