"""
HFS NM Tax Lookup — AWS Lambda

Triggered by: SQS FIFO queue (hfs-nm-tax-lookup.fifo), fed by the shared
              shipwell-webhook fan-out when custom_data.name == "HFS NM Tax Lookup".

Business logic:
  On shipment.carrier_assigned, shipment.charge_line_item.created,
     shipment.charge_line_item.updated:
    1. Fetch full shipment via details.self_link
    2. Validate carrier, customer, charges, pickup stop, and origin address
    3. Look up NM Gross Receipts Tax rate from Google Sheets (nm_tax_rates tab)
       - Primary:  match UPPER(city) + county_code (combined key)
       - Fallback: match county_code against col A (county header rows only — unambiguous)
    5. Remove any existing NM Tax charge (charge_code == "TTS") to avoid duplicates
    6. Calculate tax_amount = round(sum(non-tax carrier_charges) * rate, 2)
    7. Build NM Tax charge item and PUT to carrier assignment

Environment variables:
  DRY_RUN                           "true" to log writes without executing (default: true)
  GOOGLE_SERVICE_ACCOUNT_SECRET_NAME  Secrets Manager secret name for Google service account JSON
  GOOGLE_SERVICE_ACCOUNT_SECRET_REGION  AWS region for that secret (default: us-west-2)
  NM_TAX_SHEET_ID                   Google Sheets spreadsheet ID
  NM_TAX_SHEET_RANGE                Sheet range to read (default: Sheet1!A:E)
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import boto3
import requests
from importlib import import_module
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _filter_vendor_charge_lines(charge_lines):
    """Return charge lines with tax-only items removed.

    Tax charges (category TTS, or description containing 'tax'/'gross receipts')
    are customer-facing only and must not appear in vendor_charge_line_items.
    Fails safe: returns input unchanged on any error.
    """
    try:
        _TAX_CATS = frozenset({"TTS", "TAX"})
        _TAX_KWS = ("tax", "gross receipts")
        result = []
        for line in (charge_lines or []):
            cat = (line.get("category") or "").upper().strip()
            if cat in _TAX_CATS:
                continue
            desc = (line.get("unit_name") or line.get("description") or "").lower()
            if any(kw in desc for kw in _TAX_KWS):
                continue
            result.append(line)
        return result
    except Exception as _e:
        logger.warning(f"_filter_vendor_charge_lines failed (returning input unchanged): {_e}")
        return list(charge_lines or [])


# ─────────────────────────────────────────────────────────────────────────────
# Constants / env
# ─────────────────────────────────────────────────────────────────────────────

DRY_RUN   = os.environ.get("DRY_RUN", "true").lower() == "true"
API_BASE  = os.environ.get("SHIPWELL_API_ROOT", "https://dev-api.shipwell.com").rstrip("/")

GOOGLE_SERVICE_ACCOUNT_SECRET_NAME   = os.environ.get("GOOGLE_SERVICE_ACCOUNT_SECRET_NAME", "hfs-create-shipment-from-order/google-service-account")
GOOGLE_SERVICE_ACCOUNT_SECRET_REGION = os.environ.get("GOOGLE_SERVICE_ACCOUNT_SECRET_REGION", os.environ.get("AWS_REGION", "us-west-2"))
NM_TAX_SHEET_ID    = os.environ.get("NM_TAX_SHEET_ID", "1Z6aOdmCri_q9UfVxPmg5lZYxl4pjg3I2kJ-uT9rhzmk")
NM_TAX_SHEET_RANGE = os.environ.get("NM_TAX_SHEET_RANGE", "NM Tax Rates!A:E")

# Preplanned accessorials — custom field UUID for the preplanned_accessorials field
# and the charge table UUID used to look up per-code rates (e.g. TANKWASH=$75).
# These are passed via webhook custom_data.custom_fields when available;
# the constants below are fallbacks for the sandbox environment.
PREPLANNED_ACCESSORIALS_CF_ID_DEFAULT  = os.environ.get("PREPLANNED_ACCESSORIALS_CF_ID",  "d2ab8c89-70cb-40c5-9604-81ca69eed15b")
ACCESSORIAL_CHARGE_TABLE_ID_DEFAULT    = os.environ.get("ACCESSORIAL_CHARGE_TABLE_ID",    "80bef106-d327-4b26-9cf3-dac771efcd69")

SCRIPT_NAME = "HFS NM Tax Lookup"  # must match QUEUE_ROUTING key in shipwell-webhook

# ─────────────────────────────────────────────────────────────────────────────
# AWS clients (module-level for warm reuse)
# ─────────────────────────────────────────────────────────────────────────────

_secrets_client = boto3.client("secretsmanager", region_name="us-west-2")

# ─────────────────────────────────────────────────────────────────────────────
# Google Sheets — tax rate table (cached in-memory for warm Lambda reuse)
# ─────────────────────────────────────────────────────────────────────────────
# Structure: two dicts built from the sheet on first call.
#   _tax_by_city_county : { ("ALBUQUERQUE", "001"): 0.0763, ... }  combined key (preferred)
#   _tax_by_county_code : { "001": 0.0619, ... }                   county-code-only fallback
#                                                                    (first match per code wins)
# ─────────────────────────────────────────────────────────────────────────────

_tax_by_city_county: dict[tuple[str, str], float] | None = None
_tax_by_county_code: dict[str, float]             | None = None

# Events that should trigger a tax charge update
# carrier_assigned is the primary trigger. Charges are added by Shipwell a few
# seconds after assignment, so we poll briefly before giving up.
# charge_line_item events handle cases where charges are updated after assignment.
# The self-loop guard prevents infinite loops from our own TTS writes.
TRIGGER_EVENTS = frozenset([
    "shipment.carrier_assigned",
    "shipment.charge_line_item.created",
    "shipment.charge_line_item.updated",
    "shipment.charge_line_item.carrier_added",
    "shipment.charge_line_item.carrier_updated",
])

NM_TAX_CHARGE_CODE = "TTS"


def _load_service_account_info() -> dict:
    """Fetch Google service account JSON from Secrets Manager."""
    resp = _secrets_client.get_secret_value(SecretId=GOOGLE_SERVICE_ACCOUNT_SECRET_NAME)
    return json.loads(resp["SecretString"])


def _build_google_credentials():
    """Return a refreshed google.oauth2.service_account.Credentials object."""
    sa_module   = import_module("google.oauth2.service_account")
    auth_module = import_module("google.auth.transport.requests")
    creds = sa_module.Credentials.from_service_account_info(
        _load_service_account_info(),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    creds.refresh(auth_module.Request())
    return creds


def _fetch_sheet_rows() -> list[list[str]]:
    """Read all rows from the NM tax rate Google Sheet."""
    creds = _build_google_credentials()
    url   = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{NM_TAX_SHEET_ID}"
        f"/values/{NM_TAX_SHEET_RANGE}"
    )
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {creds.token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("values") or []


def _ensure_tax_table_loaded() -> None:
    """
    Populate module-level dicts from the Google Sheet on first invocation
    (or after a cold start). Skips header row and section-header rows
    (those with an empty city AND empty county code).
    """
    global _tax_by_city_county, _tax_by_county_code
    if _tax_by_city_county is not None:
        return  # already loaded in this Lambda container

    by_city_county: dict[tuple[str, str], float] = {}
    by_code: dict[str, float] = {}

    rows = _fetch_sheet_rows()
    logger.info(f"Loaded {len(rows)} rows from NM tax Google Sheet")

    for row in rows:
        # Pad to at least 5 columns
        while len(row) < 5:
            row.append("")
        city_raw, county_code_raw, _muni, _loc, rate_raw = row[0], row[1], row[2], row[3], row[4]

        # Skip header row
        if city_raw.strip().upper() == "CITY":
            continue
        # Skip section-header rows (no city, no county code, no rate)
        if not city_raw.strip() and not county_code_raw.strip():
            continue
        # Parse rate: "7.63%" -> 0.0763
        rate_str = rate_raw.strip().rstrip("%")
        if not rate_str:
            continue
        try:
            rate = float(rate_str) / 100.0
        except ValueError:
            logger.warning(f"Could not parse rate {rate_raw!r} -- skipping row")
            continue

        city = city_raw.strip().upper()
        code = county_code_raw.strip()

        # Primary key: (UPPER city, county_code) combined -- avoids ambiguity when
        # the same city name appears in multiple counties
        if city:
            combined_key = (city, code)  # code may be "" if sheet row has no county
            if combined_key not in by_city_county:
                by_city_county[combined_key] = rate

        # County-code-only fallback: use col A (city column) for county code lookup.
        # County codes appear only once in col A (as the "Remainder of County" row),
        # but appear in col B for every row in that county — so col A is unambiguous.
        if code and city == code and code not in by_code:
            # This is a "county header" row: col A contains the county code itself
            by_code[code] = rate

    _tax_by_city_county = by_city_county
    _tax_by_county_code = by_code
    logger.info(
        f"NM tax table ready: {len(by_city_county)} city+county entries, {len(by_code)} county-code entries"
    )


# ─────────────────────────────────────────────────────────────────────────────
# HTTP session (module-level, with retry adapter — same as Dexter)
# ─────────────────────────────────────────────────────────────────────────────

_http_session: requests.Session | None = None


def get_http_session() -> requests.Session:
    """Return a cached requests.Session with retry adapter."""
    global _http_session
    if _http_session is None:
        retry = Retry(
            total=3,
            connect=0,          # no retries on connection timeouts — avoids blowing lambda budget (30s × retries > 60s limit)
            backoff_factor=1,
            backoff_jitter=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "PUT", "POST"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retry,
            pool_connections=2,
            pool_maxsize=5,
        )
        _http_session = requests.Session()
        _http_session.mount("https://", adapter)
        _http_session.headers.update({"Content-Type": "application/json"})
        logger.info("Initialized HTTP session with retry adapter")
    return _http_session


def shipwell_request(
    token: str,
    url: str,
    method: str = "GET",
    body: dict | None = None,
) -> dict | None:
    """HTTP request against Shipwell API using persistent session."""
    session = get_http_session()
    session.headers["Authorization"] = token

    resp = session.request(method=method.upper(), url=url, json=body, timeout=30)

    if method.upper() == "DELETE" or resp.status_code == 204:
        return None
    if not resp.ok:
        if resp.status_code in (429,) or 500 <= resp.status_code < 600:
            raise RuntimeError(
                f"HTTP {resp.status_code} after retries for {url}: {resp.text[:200]}"
            )
        logger.error(f"Non-retryable HTTP {resp.status_code} for {url}: {resp.text[:200]}")
        resp.raise_for_status()
    return resp.json()


# ─────────────────────────────────────────────────────────────────────────────
# DryRunLedger — mirrors carrier-charge-totaler pattern
# ─────────────────────────────────────────────────────────────────────────────

class DryRunLedger:
    def __init__(self, shipment_id: str):
        self.shipment_id = shipment_id
        self.entries: list[dict] = []

    def record(self, operation: str, **kwargs) -> None:
        entry = {"operation": operation, **kwargs}
        self.entries.append(entry)
        logger.info(f"[DRY_RUN] Would perform: {json.dumps(entry)}")

    def summarize(self, outcome: str) -> None:
        logger.info(json.dumps({
            "dry_run_summary": True,
            "shipment_id": self.shipment_id,
            "outcome": outcome,
            "suppressed_write_count": len(self.entries),
            "suppressed_writes": self.entries,
        }))


# ─────────────────────────────────────────────────────────────────────────────
# ShipwellProgram — Scriptlab invocation tracker (same as carrier-charge-totaler)
# ─────────────────────────────────────────────────────────────────────────────

class ShipwellProgram:
    def __init__(
        self,
        script_name: str,
        webhook_event_id: str,
        webhook_event_name: str,
        webhook_resource_id: str,
        base_url: str,
        token: str,
        ledger: DryRunLedger | None = None,
    ):
        self.base_url       = base_url
        self.token          = token
        self.ledger         = ledger
        self.invocation_id: str | None = None
        self.logs: list[dict] = []
        self.start_body = {
            "script_name":           script_name,
            "webhook_event_id":      webhook_event_id,
            "webhook_event_name":    webhook_event_name,
            "webhook_resource_id":   webhook_resource_id,
            "webhook_resource_type": "shipment",
        }

    def start(self) -> None:
        if DRY_RUN:
            if self.ledger:
                self.ledger.record("script_invocation_start",
                                   script_name=self.start_body["script_name"])
            return
        try:
            resp = shipwell_request(
                self.token,
                f"{self.base_url}/scripts/invocations",
                "POST", self.start_body,
            )
            self.invocation_id = (resp or {}).get("id")
            logger.info(f"Script invocation started: {self.invocation_id}")
        except Exception as exc:
            logger.warning(f"Could not start script invocation: {exc}")

    def log(self, level: str, message: str) -> None:
        entry = {
            "level":       level,
            "timestamp":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "message":     message,
            "stack_trace": [],
        }
        self.logs.append(entry)
        logger.info(f"[{level}] {message}")

    def finalize(self, did_succeed: bool, outcome_message: str = "Script Complete") -> None:
        if DRY_RUN:
            if self.ledger:
                self.ledger.record("script_invocation_finalize",
                                   did_succeed=did_succeed,
                                   outcome_message=outcome_message)
            return
        if not self.invocation_id:
            logger.warning("No invocation_id — skipping finalize")
            return
        try:
            body = {
                "did_succeed":     did_succeed,
                "completed_at":    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "outcome_message": outcome_message,
                "logs":            self.logs,
            }
            shipwell_request(
                self.token,
                f"{self.base_url}/scripts/invocations/{self.invocation_id}/finalize/",
                "POST", body,
            )
            logger.info(f"Script invocation finalized: success={did_succeed}")
        except Exception as exc:
            logger.warning(f"Could not finalize script invocation: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Base URL helper
# ─────────────────────────────────────────────────────────────────────────────

def _get_base_url(env: str) -> str:
    if env in ("dev", "sandbox"):
        return f"https://{env}-api.shipwell.com"
    override = os.environ.get("SHIPWELL_API_ROOT", "").rstrip("/")
    return override or API_BASE


# ─────────────────────────────────────────────────────────────────────────────
# Google Sheets tax rate lookup
# ─────────────────────────────────────────────────────────────────────────────

def lookup_tax_rate(city: str | None, county_code: str | None) -> float | None:
    """
    Look up NM Gross Receipts Tax rate from the cached Google Sheet table.

    Step 1: match (UPPER(city), county_code) combined key against _tax_by_city_county.
            This avoids returning the wrong rate when two counties share a city name.
            If county_code is not available, tries (UPPER(city), "") as a last-resort
            city-only match for rows with no county code in the sheet.
    Step 2: if no result and county_code provided -> match county_code alone against
            _tax_by_county_code, built from col A (county code appears once there as
            a "Remainder of County" row — unambiguous). Col B is NOT used.
    Returns rate as float (e.g. 0.0763) or None if not found.
    """
    _ensure_tax_table_loaded()

    city_upper = city.strip().upper() if city else None
    # Normalize county code to 3-digit zero-padded string (e.g. "15" → "015")
    code = county_code.strip().zfill(3) if county_code and county_code.strip() else None

    # Step 1: combined city + county_code lookup (most precise)
    if city_upper is not None:
        # Try exact combined match first
        combined_key = (city_upper, code or "")
        rate = _tax_by_city_county.get(combined_key)  # type: ignore[union-attr]
        if rate is not None:
            logger.info(f"Tax rate found by city+county={combined_key!r}: {rate}")
            return rate
        # Step 1b: city-only scan — when county_code is absent, find any row matching
        # this city. If exactly one match exists it is unambiguous; use it.
        if code is None:
            city_matches = [
                v for (c, _), v in _tax_by_city_county.items()  # type: ignore[union-attr]
                if c == city_upper
            ]
            if len(city_matches) == 1:
                logger.info(f"Tax rate found by city-only scan for {city_upper!r}: {city_matches[0]}")
                return city_matches[0]
            elif len(city_matches) > 1:
                logger.warning(f"Ambiguous city-only match for {city_upper!r}: {len(city_matches)} entries — county_code needed")

    # Step 2: county_code-only fallback (uses col A county-header rows, not col B)
    if code:
        rate = _tax_by_county_code.get(code)  # type: ignore[union-attr]
        if rate is not None:
            logger.info(f"Tax rate found by county_code={code!r} (fallback): {rate}")
            return rate

    logger.warning(
        f"No NM tax rate found for city={city!r} county_code={county_code!r}"
    )
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Core processing
# ─────────────────────────────────────────────────────────────────────────────

def process_webhook(payload: dict) -> None:
    """Main processing logic for a single webhook event."""

    event_name  = payload.get("event_name", "unknown")
    webhook_id  = payload.get("id", "unknown")
    custom_data = payload.get("custom_data") or {}

    logger.info(f"Processing webhook id={webhook_id} event={event_name}")

    # ── Pre-flight: event filter + loop guard (before any API calls or script invocations) ──
    if event_name not in TRIGGER_EVENTS:
        logger.info(f"Skipping - event {event_name!r} not in trigger set")
        return

    # Loop guard: skip charge_line_item events triggered by the API user (i.e. us).
    # carrier_assigned is always processed regardless of source user.
    api_user_id = custom_data.get("api_user_id", "")
    source_user_id = (payload.get("source") or {}).get("user_id", "")
    if api_user_id and source_user_id == api_user_id and "charge_line_item" in event_name:
        logger.info("Skipping - charge_line_item event triggered by API user (self-loop guard)")
        return

    # Pre-invocation de-dupe: if this is a charge_line_item event for our own NM tax
    # charge (TTS), skip before creating a Script Lab invocation record. This prevents
    # the Workflows tab from being cluttered with entries for the redundant updated events
    # Shipwell fires on previously-added line items when a new charge is added.
    if "charge_line_item" in event_name:
        _early_details = payload.get("details") or {}
        # details.charge_code is present directly on charge_line_item events
        _early_charge_code = (
            _early_details.get("charge_code")
            or _early_details.get("code")
            or ""
        ).upper()
        if _early_charge_code == NM_TAX_CHARGE_CODE:
            logger.info(f"Skipping - charge_line_item event is for our own NM tax charge ({NM_TAX_CHARGE_CODE}) — no invocation created")
            return


    # ── Auth ──────────────────────────────────────────────────────────────────
    token = custom_data.get("token", "")
    if not token:
        logger.error("No token in webhook custom_data — cannot process")
        return

    script_name = custom_data.get("name", SCRIPT_NAME)

    # Custom field UUID for NM Tax Code (county code), injected via webhook custom_data
    nm_tax_code_field_id = (custom_data.get("custom_fields") or {}).get(
        "nm_tax_code_field_id", ""
    )

    env      = (payload.get("source") or {}).get("environment", "")
    base_url = _get_base_url(env)

    # Extract shipment ID for script invocation resource tracking.
    # carrier_assigned: details.id is the shipment ID.
    # charge_line_item.*: details.id is the charge line item ID; shipment is under details.shipment.id.
    _details = payload.get("details") or {}
    _shipment_ref = _details.get("shipment") or {}
    shipment_id = (
        _shipment_ref.get("id")
        or _details.get("id")
        or "unknown"
    )
    ledger = DryRunLedger(shipment_id) if DRY_RUN else None


    sw = ShipwellProgram(
        script_name=script_name,
        webhook_event_id=webhook_id,
        webhook_event_name=event_name,
        webhook_resource_id=shipment_id,
        base_url=base_url,
        token=token,
        ledger=ledger,
    )
    # sw.start() is deferred until just before the actual PUT — this prevents
    # a Workflows tab entry being created for runs that are skipped early
    # (de-dupe guard, missing carrier, etc.).

    try:

        details  = payload.get("details") or {}
        logger.debug(f"details keys={list(details.keys())!r} details={json.dumps(details)[:500]}")


        # ── Fetch full shipment ───────────────────────────────────────────────
        # For carrier_assigned: details.self_link is the shipment link directly.
        # For charge_line_item.created/updated: details is a dict keyed by index
        # ("1", "2", ...) and the shipment link is nested under details[key]["shipment"].
        self_link = details.get("self_link")
        if not self_link:
            # charge_line_item events: shipment is nested under details.shipment
            shipment_ref = details.get("shipment") or {}
            self_link = shipment_ref.get("self_link")
            if not self_link:
                sid_val = shipment_ref.get("id") or details.get("id", "")
                self_link = f"/v2/shipments/{sid_val}/" if sid_val else None
        if not self_link:
            self_link = f"/v2/shipments/unknown/"

        logger.info(f"Fetching shipment via {self_link}")
        # Build full URL: if self_link is already absolute (starts with https://), use as-is.
        # This handles synthetic test webhooks that pass a full URL in details.self_link.
        _ship_url = self_link if self_link.startswith("http") else f"{base_url}{self_link}"
        shipment = shipwell_request(token, _ship_url)
        if not shipment:
            raise ValueError(f"Empty shipment response for {self_link}")

        sid = shipment.get("id", shipment_id)
        logger.info(f"Fetched shipment {sid}")

        # ── Validate carrier assignment ───────────────────────────────────────
        rel_vendor = shipment.get("relationship_to_vendor")
        if not rel_vendor:
            msg = f"Shipment {sid} has no carrier assignment — skipping"
            logger.warning(msg)
            return

        assignment_id = rel_vendor.get("id")
        if not assignment_id:
            msg = f"Shipment {sid} carrier assignment has no id — skipping"
            logger.warning(msg)
            return

        # ── Validate customer relationship ────────────────────────────────────
        rel_customer = shipment.get("relationship_to_customer")
        if not rel_customer:
            msg = f"Shipment {sid} has no customer relationship — skipping"
            logger.warning(msg)
            return

        # ── Validate carrier charges (poll briefly on carrier_assigned to allow charges to settle) ──
        carrier_charges = []
        charges_settled = (payload.get("custom_data") or {}).get("charges_settled", False)
        # charges_settled means a rerate PUT already ran — charges regenerate async.
        # Shipwell takes >14s after the PUT returns to regenerate contract charges,
        # so use the same 5 × 5s = 25s window as a cold carrier-assign.
        if charges_settled or event_name == "shipment.carrier_assigned":
            max_attempts = 5
            poll_sleep = 5
        else:
            max_attempts = 1
            poll_sleep = 5
        for attempt in range(max_attempts):
            if attempt > 0:
                time.sleep(poll_sleep)
                # Re-fetch shipment so charges are fresh
                _ship_url_retry = self_link if self_link.startswith("http") else f"{base_url}{self_link}"
                shipment = shipwell_request(token, _ship_url_retry)
                rel_vendor = shipment.get("relationship_to_vendor") or {}
            all_charges = rel_vendor.get("customer_charge_line_items") or []
            carrier_charges = [
                c for c in all_charges
                if (c.get("charge_code") or "").upper() != NM_TAX_CHARGE_CODE
            ]
            if carrier_charges:
                break
            logger.info(f"No carrier charges yet (attempt {attempt + 1}/{max_attempts}) — waiting...")
        if not carrier_charges:
            msg = f"Shipment {sid} has no carrier charge line items after {max_attempts} attempts — skipping"
            logger.warning(msg)
            return

        # ── Find pickup stop ──────────────────────────────────────────────────
        stops = shipment.get("stops") or []
        pickup_stop = next((s for s in stops if s.get("is_pickup")), None)
        if not pickup_stop:
            msg = f"Shipment {sid} has no pickup stop — skipping"
            logger.warning(msg)
            return

        # ── Validate origin address ───────────────────────────────────────────
        origin_address = (pickup_stop.get("location") or {}).get("address") or {}
        if not origin_address:
            msg = f"Shipment {sid} pickup stop has no address — skipping"
            logger.warning(msg)
            return

        # ── Extract city and county_code ──────────────────────────────────────
        city = (origin_address.get("city") or "").strip().upper() or None

        county_code = None
        if nm_tax_code_field_id:
            stop_custom = (
                (pickup_stop.get("custom_data") or {})
                .get("shipwell_custom_data") or {}
            ).get("shipment_stop") or {}
            county_code = (stop_custom.get(nm_tax_code_field_id) or "").strip() or None

        if not city and not county_code:
            msg = f"Shipment {sid} — no city and no county_code available — skipping"
            logger.warning(msg)
            return

        logger.info(f"Origin city={city!r} county_code={county_code!r}")

        # ── Google Sheets tax rate lookup ──────────────────────────────────────
        tax_rate = lookup_tax_rate(city, county_code)
        if tax_rate is None:
            msg = f"Shipment {sid} — no NM tax rate found for city={city!r} county_code={county_code!r} — skipping"
            logger.warning(msg)
            return

        # ── Calculate tax amount ──────────────────────────────────────────────
        total_charges = sum(
            float(item.get("amount") or 0) for item in carrier_charges
        )
        tax_amount = round(total_charges * tax_rate, 2)
        logger.info(
            f"Shipment {sid}: total_charges={total_charges:.2f} rate={tax_rate} "
            f"tax_amount={tax_amount:.2f}"
        )

        # ── De-dupe guard: only applies to charge_line_item events ──────────────
        # On carrier_assigned, always calculate and write — no de-dupe.
        # On charge_line_item.* events, Shipwell fires redundant updated events for
        # previously-added line items when a new charge is added (e.g. the NM tax
        # charge itself triggering updated events for the original carrier charges).
        # In that case, skip if the NM tax charge already exists with the correct amount.
        if "charge_line_item" in event_name:
            existing_nm = next(
                (c for c in all_charges if (c.get("charge_code") or "").upper() == NM_TAX_CHARGE_CODE),
                None,
            )
            if existing_nm is not None and round(float(existing_nm.get("amount") or 0), 2) == tax_amount:
                msg = (
                    f"Shipment {sid}: NM tax charge already exists with correct amount "
                    f"({tax_amount:.2f}) — skipping redundant write (charge_line_item event de-dupe)"
                )
                logger.info(msg)
                return

        # ── Build NM Tax charge line item ─────────────────────────────────────
        nm_tax_charge = {
            "unit_name":     "New Mexico Gross Receipts Tax",
            "charge_code":   NM_TAX_CHARGE_CODE,
            "category":      "TAX",
            "amount":        tax_amount,
            "unit_amount":   tax_amount,
            "unit_quantity": 1.0,
        }

        # ── Build carrier assignment PUT payload ──────────────────────────────
        # Re-fetch the carrier assignment immediately before writing so we pick up
        # any preplanned accessorial charges (e.g. TANKWASH) that were written
        # concurrently by hfs-create-shipment-from-order after we snapshotted
        # carrier_charges above.  Without this, a tight race window causes TTS to
        # overwrite TANKWASH: NM tax reads charges at t=0 (no TANKWASH yet),
        # hfs-create writes TANKWASH at t+1, NM tax PUTs [LHS, 405, TTS] at t+2.
        try:
            _fresh_assignment = shipwell_request(
                token,
                f"{base_url}/v2/shipments/{sid}/carrier-assignments/{assignment_id}/",
            )
            _fresh_charges = _fresh_assignment.get("customer_charge_line_items") or []
            # Rebuild base list: all current charges except any stale TTS (we'll re-add ours)
            _fresh_base = [
                c for c in _fresh_charges
                if (c.get("charge_code") or "").upper() != NM_TAX_CHARGE_CODE
            ]
            logger.info(
                f"Pre-PUT re-fetch: {len(_fresh_charges)} charges on assignment "
                f"({len(_fresh_base)} non-TTS) — using fresh list to preserve preplanned charges"
            )
            final_charge_list = _fresh_base + [nm_tax_charge]
        except Exception as _refetch_err:
            logger.warning(
                f"Pre-PUT re-fetch failed ({_refetch_err}) — falling back to snapshot charge list"
            )
            final_charge_list = carrier_charges + [nm_tax_charge]

        # ── Apply preplanned accessorials (e.g. TANKWASH) ────────────────────
        # The NM tax lambda is the last writer on the carrier assignment for NM-origin
        # shipments. hfs-create-shipment-from-order intentionally skips writing preplanned
        # accessorials before this lambda runs (to avoid the concurrent-overwrite race).
        # We include them here so TANKWASH and other preplanned charges land alongside TTS
        # in a single atomic PUT — no race possible.
        #
        # Custom field UUID for preplanned_accessorials and the charge table are passed
        # via webhook custom_data (same mechanism as nm_tax_code_field_id).
        _preplanned_cf_id = (
            (custom_data.get("custom_fields") or {}).get("preplanned_accessorials", "")
            or PREPLANNED_ACCESSORIALS_CF_ID_DEFAULT
        )
        _charge_table_id = (
            (custom_data.get("custom_fields") or {}).get("accessorial_charge_table_id", "")
            or ACCESSORIAL_CHARGE_TABLE_ID_DEFAULT
        )
        if _preplanned_cf_id:
            try:
                # Resolve preplanned_accessorials from shipment custom_data first (fastest path).
                # The create-shipment lambda mirrors order custom fields onto the shipment.
                # If not found there, fall back to iterating shipment.orders.
                _ship_cd = (shipment.get("custom_data") or {}).get("shipwell_custom_data", {}).get("shipment", {})
                _pa_raw = (_ship_cd.get(_preplanned_cf_id) or "").strip()
                _order_id_for_pa = ""
                if _pa_raw:
                    logger.info(f"preplanned_accessorials={_pa_raw!r} read from shipment custom_data")
                else:
                    # Fallback: look up via shipment.orders (may be None in some environments)
                    _orders_on_ship = shipment.get("orders") or []
                    for _oref in _orders_on_ship:
                        _oid = _oref.get("id") if isinstance(_oref, dict) else _oref
                        if not _oid:
                            continue
                        _order_resp = shipwell_request(token, f"{base_url}/purchase-orders/{_oid}")
                        if not _order_resp:
                            continue
                        _order_id_for_pa = _oid
                        _cd = (_order_resp.get("custom_data") or {}).get("shipwell_custom_data", {}).get("purchase_order", {})
                        _pa_raw = (_cd.get(_preplanned_cf_id) or "").strip()
                        if _pa_raw:
                            break

                if _pa_raw:
                    # Parse comma-separated codes (e.g. "TANKWASH" or "PUMP,HOSE")
                    _pa_codes = [c.strip().upper() for c in _pa_raw.split(",") if c.strip()]
                    # Remove stale preplanned charges from final_charge_list (e.g. $0 CLN/TANKWASH
                    # from a prior run before rates were available). We'll re-add at correct amount.
                    # Match by charge_code OR unit_name against each pa_code.
                    _pa_codes_set = set(_pa_codes)
                    final_charge_list = [
                        c for c in final_charge_list
                        if (c.get("charge_code") or "").upper() not in _pa_codes_set
                        and (c.get("unit_name") or "").upper() not in _pa_codes_set
                    ]
                    # Already-present codes: check both charge_code and unit_name
                    _already_present = {
                        (c.get("charge_code") or "").upper()
                        for c in final_charge_list
                    } | {
                        (c.get("unit_name") or "").upper()
                        for c in final_charge_list
                    }
                    # Fetch contract accessorial rate for each code if charge table is known
                    _contract_id = (_fresh_assignment or {}).get("contract_id") or ""
                    _pa_rates: dict = {}
                    if _contract_id and _charge_table_id:
                        try:
                            _rate_resp = shipwell_request(
                                token,
                                f"{base_url}/v2/quoting/rate-tables/accessorial-charge-tables/{_charge_table_id}/calculate-accessorial-rates/",
                                "POST",
                                {"shipment_id": sid},
                            )
                            # API returns {"accessorials": [{"accessorial": "TANKWASH", "calculated_rate_amount": 75.0, ...}]}
                            for _r in (_rate_resp or {}).get("accessorials", []):
                                _rc = (_r.get("accessorial") or _r.get("charge_code") or "").upper()
                                _rv = float(_r.get("calculated_rate_amount") or _r.get("unit_amount") or 0)
                                if _rc and _rv > 0:
                                    _pa_rates[_rc] = _rv
                            logger.info(f"Fetched {len(_pa_rates)} preplanned accessorial rates from charge table")
                        except Exception as _rate_err:
                            logger.warning(f"Could not fetch accessorial rates: {_rate_err}")

                    for _code in _pa_codes:
                        if _code in _already_present:
                            logger.info(f"Preplanned accessorial {_code} already in charge list — skipping")
                            continue
                        _unit_amount = _pa_rates.get(_code, 0.0)
                        final_charge_list.append({
                            "charge_code": "CLN",
                            "unit_name": _code,
                            "category": "ACCESSORIAL",
                            "unit_amount": _unit_amount,
                            "amount": _unit_amount,
                            "unit_quantity": 1.0,
                        })
                        logger.info(f"Adding preplanned accessorial {_code} at unit_amount={_unit_amount} to TTS PUT")
                else:
                    logger.info("No preplanned accessorials on order — skipping")
            except Exception as _pa_err:
                logger.warning(f"Preplanned accessorial lookup failed (non-fatal, continuing with TTS write): {_pa_err}")

        vendor_info = rel_vendor.get("customer") or {}
        customer_info = (rel_customer.get("customer") or {})

        updated_assignment = {
            "vendor": {
                "name":          vendor_info.get("name", ""),
                "primary_email": vendor_info.get("primary_email", ""),
            },
            "customer": {
                "name":          customer_info.get("name", ""),
                "primary_email": customer_info.get("primary_email", ""),
            },
            "customer_charge_line_items": final_charge_list,
            "vendor_charge_line_items":   _filter_vendor_charge_lines(final_charge_list),
        }

        put_url = (
            f"{base_url}/v2/shipments/{sid}/carrier-assignments/{assignment_id}/"
        )

        # Start Script Lab invocation only now — we are committed to writing
        sw.start()

        if DRY_RUN:
            ledger.record(
                "put_carrier_assignment",
                shipment_id=sid,
                assignment_id=assignment_id,
                url=put_url,
                nm_tax_charge=nm_tax_charge,
                total_charges=total_charges,
                tax_rate=tax_rate,
            )
        else:
            shipwell_request(token, put_url, "PUT", updated_assignment)
            logger.info(
                f"PUT carrier assignment {assignment_id} on shipment {sid} "
                f"with NM Tax charge {tax_amount}"
            )

        outcome = (
            f"Applied NM Tax charge ${tax_amount:.2f} "
            f"(rate={tax_rate}, total_charges={total_charges:.2f}) "
            f"to shipment {sid}"
        )
        sw.log("INFO", outcome)
        sw.finalize(True, outcome)
        if ledger:
            ledger.summarize(outcome)

    except Exception as exc:
        logger.error(f"Unhandled error: {exc}", exc_info=True)
        sw.log("ERROR", str(exc))
        sw.finalize(False, f"Error: {exc}")
        if ledger:
            ledger.summarize(f"ERROR: {exc}")
        raise  # re-raise so SQS retries via DLQ policy


# ─────────────────────────────────────────────────────────────────────────────
# SQS entry point
# ─────────────────────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> None:
    """
    Triggered by SQS FIFO. Each record is an independent webhook payload.
    Re-raises on unhandled failure so SQS retries (up to 3x) before DLQ.
    """
    for record in event.get("Records", []):
        try:
            payload = json.loads(record["body"])
            process_webhook(payload)
        except Exception as exc:
            logger.error(f"Failed to process SQS record: {exc}", exc_info=True)
            raise
