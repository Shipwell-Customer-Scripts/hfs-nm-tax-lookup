"""Shipwell HTTP client helpers shared by the Lambda flows."""

import json
import logging
import random
import threading
import time
from typing import Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import API_RATE_LIMIT_MS, DRY_RUN

logger = logging.getLogger()


class NonRetryableAPIError(RuntimeError):
    """Raised when a 400 response contains a data error that will never succeed on retry."""
    pass


# Substrings in a 400 response body that indicate a permanent data error.
# These short-circuit all SQS retry logic.
_NON_RETRYABLE_400_MARKERS = [
    "stop is scheduled before",
    "PLAN_CREATION_FAILED",
    "Invocation is already marked as complete",
]

_http_session: requests.Session | None = None

# ---------------------------------------------------------------------------
# Token-bucket rate limiter
# ---------------------------------------------------------------------------
_rate_lock = threading.Lock()
_last_call_time: float = 0.0


def _rate_limit() -> None:
    """Enforce a minimum inter-call gap of API_RATE_LIMIT_MS milliseconds.

    No-op when API_RATE_LIMIT_MS == 0 (default in prod).
    Thread-safe via _rate_lock — safe for concurrent Lambda invocations sharing
    the same execution environment.
    """
    if API_RATE_LIMIT_MS <= 0:
        return
    global _last_call_time
    min_gap = API_RATE_LIMIT_MS / 1000.0
    with _rate_lock:
        now = time.monotonic()
        wait = min_gap - (now - _last_call_time)
        if wait > 0:
            time.sleep(wait)
        _last_call_time = time.monotonic()


def get_http_session() -> requests.Session:
    """Return a cached requests.Session with retry adapter and connection pooling."""
    global _http_session
    if _http_session is None:
        retry = Retry(
            total=3,
            backoff_factor=1,
            backoff_jitter=0.5,
            # Only retry network-level connection errors at the urllib3 layer.
            # 5xx status retries are handled by _api_call's manual loop so we
            # don't double-retry and blow the Lambda timeout budget.
            # IMPORTANT: POST/PATCH/DELETE are NOT in allowed_methods because
            # they are non-idempotent — a ReadTimeout on POST /v2/shipments/
            # could mean the server received the request and created the record.
            # Retrying at the transport layer would create a duplicate.
            # SQS provides the retry boundary for non-idempotent operations.
            status_forcelist=[429],
            allowed_methods=["GET", "PUT"],
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
        logger.info("Initialized HTTP session with connection pool")
    return _http_session


def _api_call(
    url: str, verb: str, headers: Optional[Dict] = None,
    body: Optional[Dict] = None, max_retries: int = 3,
) -> Optional[Dict]:
    """
    Shipwell API caller with exponential backoff for 429/5xx.
    Mirrors callShipwellAPI() in the GAS scripts.
    """
    method = verb.upper()
    session = get_http_session()
    _rate_limit()
    for attempt in range(max_retries):
        try:
            resp = session.request(
                method, url,
                headers=headers or {},
                json=body,
                timeout=60,
            )
            if resp.status_code == 429 or (500 <= resp.status_code < 600):
                wait = (2 ** attempt) + random.random()
                logger.warning(f"HTTP {resp.status_code} on {method} {url} - retrying in {wait:.1f}s (attempt {attempt+1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(wait)
                    continue
            if resp.status_code >= 400:
                try:
                    body_text = resp.text[:2000]
                    logger.error(f"HTTP {resp.status_code} response body: {body_text}")
                    # Detect permanent data errors that will never succeed on retry.
                    # Raise NonRetryableAPIError immediately to skip all SQS retries.
                    if resp.status_code == 400:
                        for marker in _NON_RETRYABLE_400_MARKERS:
                            if marker in body_text:
                                raise NonRetryableAPIError(
                                    f"Non-retryable 400 on {method} {url}: {marker!r} — "
                                    f"body: {body_text[:500]}"
                                )
                except NonRetryableAPIError:
                    raise
                except Exception:
                    pass
            resp.raise_for_status()
            if method != "DELETE" and resp.status_code != 204 and resp.content:
                return resp.json()
            return None
        except requests.exceptions.HTTPError as e:
            # 404 is deterministic — retrying will not help. Raise immediately.
            if hasattr(e, 'response') and e.response is not None and e.response.status_code == 404:
                raise
            # Capture response body for logging and terminal-error detection.
            err_body = ""
            if hasattr(e, 'response') and e.response is not None:
                try:
                    err_body = e.response.text or ""
                except Exception:
                    pass
            if err_body:
                status_code = e.response.status_code if hasattr(e, 'response') and e.response is not None else '?'
                logger.error(f"HTTP {status_code} response body: {err_body[:500]}")
            # "Invocation is already marked as complete" is a terminal 400 — never retry.
            if "Invocation is already marked as complete" in err_body:
                raise
            if attempt < max_retries - 1:
                wait = (2 ** attempt) + random.random()
                logger.warning(f"HTTPError on {method} {url}: {e} - retrying in {wait:.1f}s")
                time.sleep(wait)
            else:
                raise
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                wait = (2 ** attempt) + random.random()
                logger.warning(f"RequestException on {method} {url}: {e} - retrying in {wait:.1f}s")
                time.sleep(wait)
            else:
                raise
    return None


def safe_update_order(order_data: Dict, base_url: str, headers: Dict, reason: str = "") -> Dict:
    """PUT a full order payload, honoring DRY_RUN."""
    order_id = order_data.get("id")
    if not order_id:
        raise ValueError("Cannot update order without id")
    if DRY_RUN:
        logger.info(f"[DRY_RUN] Would PUT order {order_id}: {reason}")
        return order_data
    return _api_call(f"{base_url}/orders/{order_id}", "PUT", headers=headers, body=order_data) or order_data


def safe_update_shipment(shipment_data: Dict, base_url: str, headers: Dict, reason: str = "") -> Dict:
    """PUT a full shipment payload, honoring DRY_RUN."""
    shipment_id = shipment_data.get("id")
    if not shipment_id:
        raise ValueError("Cannot update shipment without id")
    if DRY_RUN:
        logger.info(f"[DRY_RUN] Would PUT shipment {shipment_id}: {reason}")
        return shipment_data
    return _api_call(f"{base_url}/v2/shipments/{shipment_id}/", "PUT", headers=headers, body=shipment_data) or shipment_data


def safe_add_shipment_stop(
    shipment_id: str, stop_data: Dict, base_url: str, headers: Dict, reason: str = "",
) -> Dict:
    """POST a stop onto a shipment, honoring DRY_RUN."""
    if not shipment_id:
        raise ValueError("Cannot add stop without shipment id")
    if DRY_RUN:
        logger.info(f"[DRY_RUN] Would POST stop to shipment {shipment_id}: {reason}")
        return stop_data
    logger.info(f"safe_add_shipment_stop body: {json.dumps(stop_data, default=str)[:1000]}")
    return _api_call(f"{base_url}/v2/shipments/{shipment_id}/stops/", "POST", headers=headers, body=stop_data) or stop_data


def safe_update_shipment_stop(
    shipment_id: str, stop_id: str, stop_data: Dict, base_url: str, headers: Dict, reason: str = "",
) -> Dict:
    """PUT an existing shipment stop, honoring DRY_RUN."""
    if not shipment_id or not stop_id:
        raise ValueError("Cannot update stop without shipment id and stop id")
    if DRY_RUN:
        logger.info(f"[DRY_RUN] Would PUT stop {stop_id} on shipment {shipment_id}: {reason}")
        return stop_data
    return _api_call(f"{base_url}/v2/shipments/{shipment_id}/stops/{stop_id}/", "PUT", headers=headers, body=stop_data) or stop_data
