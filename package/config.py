"""Runtime configuration and shared constants for the HFS shipment Lambda."""

import json
import os
from typing import Any, Dict

DRY_RUN: bool = os.environ.get("DRY_RUN", "true").lower() == "true"

# SQS queue URL for explicit re-queuing of retryable events (parent not ready)
QUEUE_URL: str = os.environ.get("QUEUE_URL", "")

# Delay (seconds) before a re-queued message becomes visible again.
REQUEUE_DELAY_SECONDS: int = int(os.environ.get("REQUEUE_DELAY_SECONDS", "300"))

# Routing guide policy ID to use for Asphalt orders with no preset SCAC.
ROUTING_GUIDE_POLICY_ID: str = os.environ.get("ROUTING_GUIDE_POLICY_ID", "b99fe034-cfef-46fa-b93e-c0c75bbd1574")
HAC_ASPHALT_LOCATIONS = {
    loc.strip() for loc in os.environ.get("HAC_ASPHALT_LOCATIONS", "4000,4001,4002").split(",") if loc.strip()
}
HAC_CAPACITY_INTEGRATION_ENABLED = os.environ.get("HAC_CAPACITY_INTEGRATION_ENABLED", "true").lower() == "true"
GOOGLE_SERVICE_ACCOUNT_SECRET_NAME: str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_SECRET_NAME", "hfs-create-shipment-from-order/google-service-account")
GOOGLE_SERVICE_ACCOUNT_SECRET_REGION: str = os.environ.get("GOOGLE_SERVICE_ACCOUNT_SECRET_REGION", os.environ.get("AWS_REGION", "us-west-2"))


def _load_json_env(name: str, default: Dict[str, Any]) -> Dict[str, Any]:
    raw_value = os.environ.get(name, "")
    if not raw_value:
        return default
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return default
    return parsed if isinstance(parsed, dict) else default


HAC_ASSIGNMENT_SHEET_IDS: Dict[str, str] = {
    str(location_id): str(sheet_id)
    for location_id, sheet_id in _load_json_env(
        "HAC_ASSIGNMENT_SHEET_IDS",
        {"4000": "16vMjdzV8JREK8Dznh_vRIht6-ImLgc9qjabi-TnwHks"},
    ).items()
    if sheet_id
}
HAC_ASPHALT_EQUIPMENT = [
    value.strip()
    for value in os.environ.get("HAC_ASPHALT_EQUIPMENT", "Blue Dot,Green Dot,General Emulsion,Asphalt").split(",")
    if value.strip()
]

EQUIPMENT_TYPE_MAP: Dict[str, Dict[str, Any]] = {
    # Note: 'id' is intentionally omitted — equipment type IDs differ between sandbox/dev/prod.
    # The Shipwell PUT API resolves the correct ID from 'machine_readable' server-side.
    # Always send only machine_readable + name when updating a shipment.
    "TANKER":            {"machine_readable": "TANKER",            "name": "Tanker"},
    "MULTI_COMPARTMENT":        {"machine_readable": "MULTICOMPART_TANK", "name": "Multi Compartment Tanker"},
    "MULTI_COMPARTMENT_TANKER": {"machine_readable": "MULTICOMPART_TANK", "name": "Multi Compartment Tanker"},
    "MULTICOMPARTMENT":         {"machine_readable": "MULTICOMPART_TANK", "name": "Multi Compartment Tanker"},
    "MULTICOMPART_TANK":        {"machine_readable": "MULTICOMPART_TANK", "name": "Multi Compartment Tanker"},
    "DRY_VAN":           {"machine_readable": "DRY_VAN",           "name": "Dry Van"},
    "REEFER":            {"machine_readable": "REEFER",            "name": "Reefer"},
    "FLATBED":           {"machine_readable": "FLATBED",           "name": "Flatbed"},
    "BULK":              {"machine_readable": "BULK",              "name": "Bulk"},
    "HOPPER":            {"machine_readable": "HOPPER",            "name": "Hopper"},
    "SUPER_BODY_TANKS":  {"machine_readable": "SUPER_BODY_TANKS",  "name": "Super Body Tanks"},
    "A_TRAINS":          {"machine_readable": "A_TRAINS",          "name": "A Trains"},
    # HFS single-letter petroleum/product codes → Tanker
    "P":                 {"machine_readable": "TANKER",            "name": "Tanker"},
    "PETROLEUM":         {"machine_readable": "TANKER",            "name": "Tanker"},
    # HFS product Equipment selection field values — map 1:1 to real Shipwell equipment types
    "ASPHALT":           {"machine_readable": "ASPHALT",           "name": "Asphalt"},
    "GREEN_DOT":         {"machine_readable": "GREEN_DOT",         "name": "Green Dot"},
    "BLUE_DOT":          {"machine_readable": "BLUE_DOT",          "name": "Blue Dot"},
    "EMULSION":          {"machine_readable": "EMULSION",          "name": "Emulsion"},
    "SEMI":              {"machine_readable": "SEMI",              "name": "Semi"},
    "BODY_TANKS":        {"machine_readable": "BODY_TANKS",        "name": "Body Tanks"},
}

DEFAULT_SCHEDULING_TIMEZONE = "America/Denver"
DEFAULT_LOAD_TIME = 0.75  # hours (45 min)
APPOINTMENT_DURATION_MINUTES = 45
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "SM-HFS-Shipwell@HFSinclair.com")
# Override recipient for testing (e.g. charles@shipwell.com). Takes precedence over SUPPORT_EMAIL.
SUPPORT_EMAIL_OVERRIDE = os.environ.get("SUPPORT_EMAIL_OVERRIDE", "")
SUPPORT_EMAIL_FROM = os.environ.get("SUPPORT_EMAIL_FROM", "charles@shipwell.com")
SEND_SUPPORT_EMAILS = os.environ.get("SEND_SUPPORT_EMAILS", "false").lower() == "true"
# Email provider: "ses" or "postmark" (default)
EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "postmark").lower()
POSTMARK_API_TOKEN = os.environ.get("POSTMARK_API_TOKEN", "")
DEFAULT_SERVICE_LEVEL = "STD"
DEFAULT_SHIPMENT_STATUS = "quoting"
AVG_SPEED_MPH = 50
FUEL_INTERVAL_MILES = 250
FUEL_STOP_DURATION_HOURS = 0.5
BREAK_RULE_HOURS = 8
BREAK_DURATION_HOURS = 0.5
MAX_DRIVE_HOURS_PER_DAY = 11
REST_DURATION_HOURS = 10
DEFAULT_EARLY_TIME = "00:01:00"
DEFAULT_LATE_TIME = "23:59:00"

# Optional fallback: map product_category values to Business Unit strings.
# Used when a product cannot be found in the catalog or has no BU custom field.
# Format (JSON): {"WAXES": "Lubes", "ASPHALTS": "Asphalt", "LUBRICANTS": "Lubes"}
# Keys are matched case-insensitively against the order item's product_category.
PRODUCT_CATEGORY_BU_MAP: Dict[str, str] = {
    k.upper(): v
    for k, v in _load_json_env("PRODUCT_CATEGORY_BU_MAP", {}).items()
}

# When true: raise RuntimeError (block shipment creation) if Business Unit cannot be resolved
# for any order item, even after PRODUCT_CATEGORY_BU_MAP fallback.
# When false: log WARNING + send support notification, then continue with empty BU.
# Default is now true — customer confirmed all products have BU values populated (2026-07-06).
# Set REQUIRE_BUSINESS_UNIT=false in Lambda env to temporarily disable strict mode.
REQUIRE_BUSINESS_UNIT: bool = os.environ.get("REQUIRE_BUSINESS_UNIT", "true").lower() == "true"
POINT_DELIVERY_APPT_WINDOW_HOURS = 3

# Minimum milliseconds between consecutive Shipwell API calls (token bucket rate limit).
# Set to 0 to disable rate limiting (default in prod).
# Recommended for sandbox: 200 (5 req/sec), for dev: 100 (10 req/sec).
API_RATE_LIMIT_MS: int = int(os.environ.get("API_RATE_LIMIT_MS", "0"))

class AlreadyNotifiedError(Exception):
    """Raised after a user-facing support email has already been sent for this failure.
    The lambda_handler catches this and skips the DLQ failure email to avoid duplicates.
    """
