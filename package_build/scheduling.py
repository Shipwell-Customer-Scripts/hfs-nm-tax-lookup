"""Scheduling calculation, facility-hours, and appointment helpers."""

import copy
import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from config import (
    APPOINTMENT_DURATION_MINUTES,
    AVG_SPEED_MPH,
    BREAK_DURATION_HOURS,
    BREAK_RULE_HOURS,
    DEFAULT_EARLY_TIME,
    DEFAULT_LATE_TIME,
    DEFAULT_LOAD_TIME,
    DEFAULT_SCHEDULING_TIMEZONE,
    DRY_RUN,
    FUEL_INTERVAL_MILES,
    FUEL_STOP_DURATION_HOURS,
    MAX_DRIVE_HOURS_PER_DAY,
    POINT_DELIVERY_APPT_WINDOW_HOURS,
    REST_DURATION_HOURS,
)
from hfs_utils import (
    notify_bodies_for_shipment,
    notify_bodies_for_order,
    custom_field_is_truthy,
    get_custom_field_value,
    get_location_reference_id,
    normalize_custom_field_text,
)
from notifications import notify_support
from shipwell_client import _api_call, safe_update_shipment, safe_update_shipment_stop
from shipwell_resources import fetch_address_book_entry, get_address_book_custom_value
from stops import get_dropoff_stop, get_pickup_stop, get_stop_timezone, parse_window_endpoint, set_stop_window

logger = logging.getLogger()


def parse_iso8601_duration(duration_str: str) -> Optional[float]:
    """
    Parse ISO 8601 duration like 'P0DT0H45M0.000000S' into decimal hours.
    Returns None if unparseable.
    """
    match = re.match(r"P(\d+)DT(\d+)H(\d+)M([\d.]+)S", duration_str)
    if not match:
        logger.warning(f"Could not parse duration: {duration_str}")
        return None
    days, hours, minutes, seconds = int(match[1]), int(match[2]), int(match[3]), float(match[4])
    return (days * 24) + hours + (minutes / 60) + (seconds / 3600)


def calc_hours_of_service(transit_hours: float) -> Tuple[float, float]:
    """
    Calculate HOS rest requirements.
    Per 11-hour driving block: 10 hrs HOS rest; first block adds 5 hrs additional transit.
    Returns (hos, additional_transit) in hours.
    """
    blocks = math.floor(transit_hours / 11)
    hos = blocks * 10
    additional_transit = 5.0 if blocks >= 1 else 0.0
    return hos, additional_transit


def detect_delivery_window_case(time_start: Optional[str], time_end: Optional[str]) -> int:
    """
    Classify delivery window:
    0 = unspecified/invalid, 1 = full-day (00:00-23:59), 2 = point-in-time, 3 = specific range.
    """
    if not time_start or not time_end:
        return 0

    def normalize(t: str) -> str:
        t = t.strip()
        if re.match(r"^\d{2}:\d{2}$", t):
            return t + ":00"
        return t

    ns = normalize(time_start)
    ne = normalize(time_end)
    try:
        s = datetime.strptime(ns[:8], "%H:%M:%S")
        e = datetime.strptime(ne[:8], "%H:%M:%S")
    except ValueError:
        return 0

    if s.hour == 0 and s.minute == 0 and e.hour == 23 and e.minute == 59:
        return 1
    if ns == ne:
        return 2
    return 3


def parse_local_datetime(date_str: str, time_str: str, tz_str: str) -> Optional[datetime]:
    """
    Parse a local date + time string into a timezone-aware datetime, using the given IANA timezone.
    Returns None on parse failure.
    """
    if not date_str or not time_str:
        return None
    time_str = time_str.strip()
    if re.match(r"^\d{2}:\d{2}$", time_str):
        time_str += ":00"
    try:
        tz = ZoneInfo(tz_str)
        iso = f"{date_str}T{time_str}"
        if re.search(r"[+-]\d{2}:\d{2}$", time_str) or time_str.endswith("Z"):
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return dt.astimezone(tz)
        dt = datetime.fromisoformat(iso).replace(tzinfo=tz)
        return dt
    except Exception as e:
        logger.warning(f"parse_local_datetime failed for {date_str}T{time_str} ({tz_str}): {e}")
        return None


def calc_load_travel_time(
    shipment_data: Dict, planned_date: str, time_part: str,
    timezone_str: str, load_time: float, appointment_window: float = 0.0,
) -> str:
    """
    Backward time calculation: given a delivery datetime, calculate pickup start.
    delivery_dt - ceil(load_time + travel_time + hos + additional_transit + appointment_window) hours.
    Mirrors calcLoadTravelTime() in SchedulingCalculations.gs.
    """
    total_miles = shipment_data.get("total_miles") or 0
    travel_time = total_miles / 50.0
    hos, additional_transit = calc_hours_of_service(travel_time)
    total_hours = math.ceil(load_time + travel_time + hos + additional_transit + appointment_window)

    delivery_dt = parse_local_datetime(planned_date, time_part, timezone_str)
    if not delivery_dt:
        try:
            delivery_dt = datetime.fromisoformat(f"{planned_date}T{time_part}".replace("Z", "+00:00"))
        except Exception:
            delivery_dt = datetime.now(timezone.utc)

    pickup_dt = delivery_dt - timedelta(hours=total_hours)
    return pickup_dt.isoformat()


def calc_delivery_from_pickup(
    shipment_data: Dict, planned_date: str, time_part: str, timezone_str: str, load_time: float,
) -> str:
    """
    Forward time calculation: given a pickup datetime, calculate delivery time.
    pickup_dt + ceil(load_time + travel_time + hos + additional_transit) hours.
    Mirrors calcDeliveryFromPickup() in SchedulingCalculations.gs.
    """
    total_miles = shipment_data.get("total_miles") or 0
    travel_time = total_miles / 50.0
    hos, additional_transit = calc_hours_of_service(travel_time)
    total_hours = math.ceil(load_time + travel_time + hos + additional_transit)

    pickup_dt = parse_local_datetime(planned_date, time_part, timezone_str)
    if not pickup_dt:
        try:
            pickup_dt = datetime.fromisoformat(f"{planned_date}T{time_part}".replace("Z", "+00:00"))
        except Exception:
            pickup_dt = datetime.now(timezone.utc)

    delivery_dt = pickup_dt + timedelta(hours=total_hours)
    return delivery_dt.isoformat()


def get_facility_hours(
    facility_id: str, date_str: str, timezone_str: str, base_url: str, headers: Dict,
) -> Optional[Dict]:
    """
    Fetch facility hours-of-operation for a given date.
    Returns {'open': datetime, 'close': datetime} (timezone-aware) or None if closed/unavailable.
    Mirrors getFacilityHours() in SchedulingCalculations.gs.
    """
    day_names = ["SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY"]
    try:
        resp = _api_call(f"{base_url}/facilities/{facility_id}/hours-of-operation", "GET", headers=headers)
        data = resp.get("data") or []
        if not data:
            return None
        date_obj = datetime.fromisoformat(f"{date_str}T00:00:00+00:00")
        js_day = (date_obj.weekday() + 1) % 7
        day_name = day_names[js_day]

        day_entry = next((d for d in data if d.get("day") == day_name), None)
        if not day_entry or not day_entry.get("is_facility_open"):
            return None

        open_dt = parse_local_datetime(date_str, day_entry["open_time"], timezone_str)
        close_dt = parse_local_datetime(date_str, day_entry["close_time"], timezone_str)
        if not open_dt or not close_dt:
            return None
        # Tempus encodes overnight hours as close < open (e.g. open=06:00, close=02:00 next day).
        # Advance close by 1 day so the window is contiguous and clamp logic works correctly.
        if close_dt < open_dt:
            close_dt += timedelta(days=1)
        return {"open": open_dt, "close": close_dt}
    except Exception as e:
        logger.warning(f"get_facility_hours error ({facility_id}): {e}")
        return None


def clamp_to_facility_hours(dt: datetime, facility_hours: Optional[Dict]) -> datetime:
    """Clamp a datetime to facility open/close bounds."""
    if not facility_hours:
        return dt
    open_dt = facility_hours["open"]
    close_dt = facility_hours["close"]
    if dt < open_dt:
        return open_dt
    if dt > close_dt:
        return close_dt
    return dt


def default_business_hours(date_str: str, timezone_str: str) -> Dict[str, datetime]:
    """Return the document's default all-day business hours for a local date."""
    open_dt = parse_local_datetime(date_str, DEFAULT_EARLY_TIME, timezone_str)
    close_dt = parse_local_datetime(date_str, DEFAULT_LATE_TIME, timezone_str)
    if not open_dt or not close_dt:
        tz = ZoneInfo(timezone_str)
        open_dt = datetime.fromisoformat(f"{date_str}T00:01:00").replace(tzinfo=tz)
        close_dt = datetime.fromisoformat(f"{date_str}T23:59:00").replace(tzinfo=tz)
    return {"open": open_dt, "close": close_dt}


def business_hours_for_stop(
    stop: Dict, dt: datetime, base_url: str, headers: Dict,
) -> Dict[str, datetime]:
    """Fetch facility hours for a stop/date or fall back to default 00:01-23:59."""
    location = stop.get("location") or {}
    timezone_str = location.get("timezone") or (location.get("address") or {}).get("timezone") or DEFAULT_SCHEDULING_TIMEZONE
    date_str = dt.astimezone(ZoneInfo(timezone_str)).date().isoformat()
    facility_id = location.get("facility_id")
    if facility_id:
        hours = get_facility_hours(facility_id, date_str, timezone_str, base_url, headers)
        if hours:
            return hours
    return default_business_hours(date_str, timezone_str)


def is_default_early(dt: datetime) -> bool:
    """Return True if the time looks like an all-day placeholder 'start' (00:00–00:01 local)."""
    return dt.hour == 0 and dt.minute in (0, 1)


def is_default_late(dt: datetime) -> bool:
    """Return True if the time looks like an all-day placeholder 'end' (23:58–23:59 local)."""
    return dt.hour == 23 and dt.minute >= 58


def is_full_day_window(start_dt: datetime, end_dt: datetime) -> bool:
    """Return True when start/end span a ~24h window — signals a date-only order plan_window.

    The corrogo orders API stores plan_window times as UTC midnight (00:01 / 23:59) and
    returns them as-is.  After conversion to a local timezone (e.g. MDT = UTC-6), those
    midnight boundaries shift to the previous evening (18:01 / 17:59), so is_default_early
    / is_default_late won't fire.  We catch this by checking whether the window duration
    is close to 24 hours regardless of the absolute hour values.

    IMPORTANT: Do NOT flag a window as full-day when the local start and end are both
    clean quarter-hour-aligned times that aren't the canonical 00:01/23:59 defaults.
    For example, 00:15–23:45 is a 23.5h intentional wide window, not a placeholder.
    We only apply the span heuristic when the local endpoints look like defaults OR
    when the UTC timestamps both sit at/near midnight (indicating corrogo's verbatim
    UTC storage), not when the user explicitly specified precise local times.
    """
    if start_dt is None or end_dt is None:
        return False
    # Normalise: if end < start (e.g. UTC boundary crosses midnight in local tz) use abs span
    span = abs((end_dt - start_dt).total_seconds())
    if not (20 * 3600 <= span <= 28 * 3600):
        return False
    # At this point the span is ~24h. Only treat as full-day placeholder when at least
    # one endpoint looks like a default (00:00/00:01 or 23:58/23:59 local), OR when
    # both UTC timestamps are near midnight (corrogo's verbatim 00:01/23:59 UTC storage).
    # An explicit wide window like 00:15–23:45 local has neither property.
    local_start_is_default = is_default_early(start_dt) or is_default_late(start_dt)
    local_end_is_default = is_default_early(end_dt) or is_default_late(end_dt)
    if local_start_is_default or local_end_is_default:
        return True
    # Also catch the timezone-shifted case: corrogo stores 00:01/23:59 UTC verbatim;
    # after localising to e.g. CDT (UTC-5) those become 19:01/18:59 the prior evening.
    # In that case neither local endpoint is a default, but both UTC times are near midnight.
    utc_start = start_dt.astimezone(timezone.utc)
    utc_end   = end_dt.astimezone(timezone.utc)
    utc_start_near_midnight = utc_start.hour == 0 and utc_start.minute <= 5
    utc_end_near_midnight   = utc_end.hour == 23 and utc_end.minute >= 55
    return utc_start_near_midnight and utc_end_near_midnight


def window_from_order_or_stop(order_stop: Dict, shipment_stop: Dict) -> Optional[Dict[str, Any]]:
    """Read a stop window from the order plan_window, falling back to shipment planned fields."""
    location = shipment_stop.get("location") or order_stop.get("location") or {}
    timezone_str = location.get("timezone") or (location.get("address") or {}).get("timezone") or DEFAULT_SCHEDULING_TIMEZONE
    plan_window = (
        (order_stop.get("shipping_requirements") or {}).get("plan_window")
        or order_stop.get("plan_window")
        or {}
    )

    start_dt = parse_window_endpoint(plan_window.get("start"), timezone_str)
    end_dt = parse_window_endpoint(plan_window.get("end"), timezone_str)

    if not start_dt and shipment_stop.get("planned_date"):
        start_dt = parse_local_datetime(
            shipment_stop.get("planned_date"),
            shipment_stop.get("planned_time_window_start") or DEFAULT_EARLY_TIME,
            timezone_str,
        )
    if not end_dt and shipment_stop.get("planned_date"):
        end_dt = parse_local_datetime(
            shipment_stop.get("planned_date"),
            shipment_stop.get("planned_time_window_end") or DEFAULT_LATE_TIME,
            timezone_str,
        )

    if not start_dt and not end_dt:
        return None
    if start_dt and not end_dt:
        end_dt = start_dt
    if end_dt and not start_dt:
        start_dt = end_dt

    # Detect full-day windows (either via classic 00:01/23:59 local times OR by
    # ~24h span, which happens when UTC midnight timestamps shift into the local
    # previous-evening after timezone conversion).
    full_day = is_full_day_window(start_dt, end_dt)
    start_is_default = is_default_early(start_dt) or full_day
    end_is_default = is_default_late(end_dt) or full_day

    if full_day:
        logger.info(
            f"window_from_order_or_stop: detected full-day span "
            f"({start_dt.isoformat()} → {end_dt.isoformat()}); "
            f"marking both endpoints as default (will convert to facility hours)"
        )

    return {
        "start": start_dt,
        "end": end_dt,
        "start_is_default": start_is_default,
        "end_is_default": end_is_default,
    }


def apply_default_business_times(
    window: Dict[str, Any], stop: Dict, base_url: str, headers: Dict,
) -> Dict[str, datetime]:
    """Substitute business open/close for default early/late window times."""
    start_dt = window["start"]
    end_dt = window["end"]
    
    logger.info(
        f"apply_default_business_times: input window {start_dt.isoformat()} → {end_dt.isoformat()} "
        f"(start_is_default={window.get('start_is_default')}, end_is_default={window.get('end_is_default')})"
    )
    
    if window.get("start_is_default"):
        biz_hours = business_hours_for_stop(stop, start_dt, base_url, headers)
        logger.info(f"Converting start from {start_dt.isoformat()} to facility open {biz_hours['open'].isoformat()}")
        start_dt = biz_hours["open"]
    if window.get("end_is_default"):
        biz_hours = business_hours_for_stop(stop, end_dt, base_url, headers)
        logger.info(f"Converting end from {end_dt.isoformat()} to facility close {biz_hours['close'].isoformat()}")
        end_dt = biz_hours["close"]
    
    logger.info(f"apply_default_business_times: output window {start_dt.isoformat()} → {end_dt.isoformat()}")
    return {"start": start_dt, "end": end_dt}


def clamp_forward_to_business_hours(
    dt: datetime, stop: Dict, base_url: str, headers: Dict,
) -> datetime:
    """Move a delivery time forward into business hours."""
    hours = business_hours_for_stop(stop, dt, base_url, headers)
    if dt < hours["open"]:
        return hours["open"]
    if dt <= hours["close"]:
        return dt
    next_day = dt + timedelta(days=1)
    return business_hours_for_stop(stop, next_day, base_url, headers)["open"]


def clamp_end_to_business_hours(
    dt: datetime, stop: Dict, base_url: str, headers: Dict,
) -> datetime:
    """Clamp a delivery window END to the facility's closing time.

    Unlike clamp_forward_to_business_hours (which rolls past-close times to
    next-day open), this caps the time at today's close so the window end
    never extends beyond operating hours.
    """
    hours = business_hours_for_stop(stop, dt, base_url, headers)
    if dt < hours["open"]:
        return hours["open"]
    if dt <= hours["close"]:
        return dt
    return hours["close"]


def clamp_backward_to_business_hours(
    dt: datetime, stop: Dict, base_url: str, headers: Dict,
) -> datetime:
    """Move a pickup time backward into business hours.

    When the facility is closed on dt's date (get_facility_hours returns None),
    the fallback default_business_hours returns 00:01-23:59 which would make
    any time appear within hours. Instead, walk backward day by day to find
    the most recent open day and return its close time.
    """
    location = stop.get("location") or {}
    timezone_str = location.get("timezone") or (location.get("address") or {}).get("timezone") or DEFAULT_SCHEDULING_TIMEZONE
    facility_id = location.get("facility_id")

    def _real_hours_for_dt(candidate: datetime) -> Optional[Dict]:
        """Return real facility hours for candidate's date, or None if closed/unknown."""
        date_str = candidate.astimezone(ZoneInfo(timezone_str)).date().isoformat()
        if facility_id:
            return get_facility_hours(facility_id, date_str, timezone_str, base_url, headers)
        return None  # no facility → caller uses default

    real = _real_hours_for_dt(dt)
    if real is None and facility_id:
        # Facility is closed on dt's date. Walk backward up to 7 days to find the
        # most recent open day and return its close time.
        for days_back in range(1, 8):
            prev = dt - timedelta(days=days_back)
            prev_real = _real_hours_for_dt(prev)
            if prev_real is not None:
                return prev_real["close"]
        # No open day found in 7 days — fall back to default hours on dt's date
        hours = business_hours_for_stop(stop, dt, base_url, headers)
    else:
        hours = real if real is not None else business_hours_for_stop(stop, dt, base_url, headers)

    if hours["open"] <= dt <= hours["close"]:
        return dt
    if dt < hours["open"]:
        # Before open on this day — go to previous day's close (walking back for closed days)
        for days_back in range(1, 8):
            prev = dt - timedelta(days=days_back)
            prev_real = _real_hours_for_dt(prev)
            if prev_real is not None:
                return prev_real["close"]
        previous_day = dt - timedelta(days=1)
        return business_hours_for_stop(stop, previous_day, base_url, headers)["close"]
    return hours["close"]


def overlap_with_hours(
    window_start: datetime,
    window_end: datetime,
    biz: Dict[str, datetime],
) -> timedelta:
    """Return the duration of [window_start, window_end] that falls within biz open/close.

    Returns timedelta(0) if there is no overlap.
    """
    overlap_start = max(window_start, biz["open"])
    overlap_end   = min(window_end,   biz["close"])
    if overlap_end <= overlap_start:
        return timedelta(0)
    return overlap_end - overlap_start


def _warn_if_window_moved(
    original_start: datetime,
    adjusted_start: datetime,
    threshold_hours: float,
    label: str,
    direction: str,
    shipment_data: Dict,
    sw: "ShipwellProgram",
) -> None:
    """Send a warning email when a planning window was moved more than threshold_hours.

    Called when fit_window_to_facility_hours has to move an entire window (rather than
    just clip a boundary) because the overlap with facility hours is less than the
    load/unload time.  label is 'Pickup' or 'Delivery'; direction is 'backward' or 'forward'.
    """
    moved_h = abs((adjusted_start - original_start).total_seconds()) / 3600
    if moved_h > threshold_hours:
        subject = f"{label} Window Adjusted \u2014 Carrier May Exceed Free Time"
        detail = (
            f"The calculated {label.lower()} window was moved {moved_h:.1f}h {direction} "
            f"to fit within facility operating hours "
            f"(original start: {original_start.isoformat()}, "
            f"adjusted start: {adjusted_start.isoformat()}). "
            f"This may cause the carrier to wait beyond the free time allowance."
        )
        try:
            plain, html = notify_bodies_for_shipment(shipment_data, subject, error_detail=detail)
            notify_support(subject, plain, sw, html_body=html)
        except Exception as _e:
            logger.warning(f"_warn_if_window_moved: email failed: {_e}")
        logger.warning(f"_warn_if_window_moved: {detail}")


def _warn_if_pickup_imminent(
    pickup_start: datetime,
    shipment_data: Dict,
    sw: "ShipwellProgram",
    lead_hours: float = 4.0,
) -> None:
    """Send a warning email if the pickup window start is within lead_hours of now.

    Implements the customer spec requirement: if planning_window.start <= now + 4 hours,
    warn that the pickup window may be in the past or may not allow the carrier enough
    lead time to arrive.  Fires for every pickup case (before open, crosses open,
    after close, crosses close).
    """
    now_utc = datetime.now(ZoneInfo("UTC"))
    threshold = now_utc + timedelta(hours=lead_hours)
    if pickup_start <= threshold:
        subject = "Pickup Window May Be Imminent or in the Past"
        detail = (
            f"The calculated pickup window starts at {pickup_start.isoformat()}, "
            f"which is within {lead_hours:.0f} hours of now ({now_utc.isoformat()}). "
            f"The carrier may not have enough lead time to arrive, or the window "
            f"may already be in the past."
        )
        try:
            plain, html = notify_bodies_for_shipment(shipment_data, subject, error_detail=detail)
            notify_support(subject, plain, sw, html_body=html)
        except Exception as _e:
            logger.warning(f"_warn_if_pickup_imminent: email failed: {_e}")
        logger.warning(f"_warn_if_pickup_imminent: {detail}")


def fit_window_to_facility_hours(
    window_start: datetime,
    window_end: datetime,
    load_time_h: float,
    stop: Dict,
    direction: str,
    base_url: str,
    headers: Dict,
    shipment_data: Dict,
    sw: "ShipwellProgram",
    warn_threshold_hours: float = 1.0,
    label: str = "Pickup",
) -> Dict[str, datetime]:
    """Fit a planning window to facility business hours, respecting load/unload time.

    Implements the customer's four-case spec for both pickup (direction='backward')
    and delivery (direction='forward').

    Cases handled:
      Before open   -- move entire window in direction to first available slot
      Crosses open  -- if overlap >= load time: clip boundary; else move entire window
      After close   -- move entire window in direction to first available slot
      Crosses close -- if overlap >= load time: clip boundary; else move entire window

    When the window is moved more than warn_threshold_hours, a warning email is sent
    via _warn_if_window_moved.

    If load_time_h <= 0 (delivery facility with no unload time configured), any overlap
    satisfies the threshold and the window is always clipped rather than moved.

    Returns {"start": ..., "end": ...} with a guaranteed non-inverted window.
    """
    # Guard: zero/missing load time -- always clip, never move
    if load_time_h <= 0:
        load_td = timedelta(0)
        always_clip = True
    else:
        load_td = timedelta(hours=load_time_h)
        always_clip = False

    original_start = window_start

    # Fetch business hours for the reference day:
    # pickup uses window_end as anchor (backward scheduling), delivery uses window_start
    ref_dt = window_end if direction == "backward" else window_start
    biz = business_hours_for_stop(stop, ref_dt, base_url, headers)

    overlap = overlap_with_hours(window_start, window_end, biz)

    if always_clip or overlap >= load_td:
        # Enough overlap (or no load time requirement) -- clip the out-of-hours boundary
        new_start = max(window_start, biz["open"])
        new_end   = min(window_end,   biz["close"])
        logger.info(
            f"fit_window_to_facility_hours ({label}, {direction}): "
            f"overlap={overlap} >= load={load_td} -- clipping. "
            f"Result: {new_start.isoformat()} \u2013 {new_end.isoformat()}"
        )
    else:
        # Insufficient overlap -- move the entire window
        if direction == "backward":
            # Pickup: anchor end at facility close, derive start backwards
            if window_end < biz["open"]:
                # Entire window is before open -- use previous day's close
                prev_biz = business_hours_for_stop(stop, window_end - timedelta(days=1), base_url, headers)
                anchor_close = prev_biz["close"]
                anchor_open  = prev_biz["open"]
            else:
                anchor_close = biz["close"]
                anchor_open  = biz["open"]
            new_end   = anchor_close
            new_start = max(anchor_open, anchor_close - load_td)
        else:
            # Delivery: insufficient overlap -- move the entire window forward.
            # Anchor selection:
            #   Before open or crosses open (window_end <= biz close): today's open
            #     still has the full operating day available to accommodate load_td.
            #   Crosses close or entirely after close (window_end > biz close): the
            #     window is already past the point where today can absorb a full
            #     load_td block, so move to next day's open.
            if window_end <= biz["close"]:
                # Before open or crosses open -- today's open is the anchor
                anchor_open  = biz["open"]
                anchor_close = biz["close"]
            else:
                # Crosses close or entirely after close -- use next day's open
                next_biz = business_hours_for_stop(stop, window_start + timedelta(days=1), base_url, headers)
                anchor_open  = next_biz["open"]
                anchor_close = next_biz["close"]
            new_start = anchor_open
            new_end   = min(anchor_open + load_td, anchor_close)

        logger.warning(
            f"fit_window_to_facility_hours ({label}, {direction}): "
            f"overlap={overlap} < load={load_td} -- moving entire window. "
            f"Result: {new_start.isoformat()} \u2013 {new_end.isoformat()}"
        )
        _warn_if_window_moved(
            original_start, new_start, warn_threshold_hours,
            label, direction, shipment_data, sw,
        )

    # Safety: ensure window is never inverted
    if new_end < new_start:
        new_end = new_start

    return {"start": new_start, "end": new_end}


def parse_float_field(value: Any, default: float = 0.0) -> float:
    try:
        return float(normalize_custom_field_text(value) or default)
    except (TypeError, ValueError):
        return default


def parse_selection_transit_value(raw_value: Any, custom_field_def: Optional[Dict] = None) -> float:
    """Parse an additional_transit_time SELECTION field value to a signed float.

    Shipwell SELECTION fields store values with underscores instead of decimal
    points (e.g. "2_00" = 2.00, "0_50" = 0.50). The sign is carried only by
    the label ("-1.00"), not by the value itself (which is also "1_00" for both
    +1 and -1). When the custom field definition is provided, look up the label
    for the given value to determine the correct sign.
    """
    text = normalize_custom_field_text(raw_value)
    if not text:
        return 0.0

    # New format: n-prefix = negative, p-prefix = positive (e.g. "n1_25" = -1.25, "p0_50" = +0.50)
    if text.startswith("n") or text.startswith("p"):
        sign = -1.0 if text.startswith("n") else 1.0
        normalized = text[1:].replace("_", ".")
        try:
            return sign * float(normalized)
        except (TypeError, ValueError):
            pass

    # Legacy format: bare underscored decimal with sign carried only in label (e.g. "1_25")
    normalized = text.replace("_", ".")
    try:
        magnitude = float(normalized)
    except (TypeError, ValueError):
        return 0.0

    # Resolve sign from label when field definition is available
    if custom_field_def and magnitude != 0.0:
        for option in (custom_field_def.get("allowed_values") or []):
            if str(option.get("value", "")) == text:
                label_text = normalize_custom_field_text(option.get("label", ""))
                try:
                    return float(label_text)
                except (TypeError, ValueError):
                    pass
                break

    return magnitude


# ULID pattern: 26 uppercase alphanumeric chars (Crockford base-32).
# Tempus facility IDs are ULIDs — they are NOT valid address-book external
# references (which are short numeric strings like "4000"). Passing a ULID
# to /v2/address-book/?q=<ulid> triggers a slow full-text scan (~30-60s);
# we guard against that here by skipping ULIDs entirely.
_ULID_RE = re.compile(r'^[0-9A-Z]{26}$')


def _looks_like_ulid(value: str) -> bool:
    """Return True if value matches the ULID format (26 Crockford base-32 chars)."""
    return bool(_ULID_RE.match(str(value or '').upper()))


# Module-level cache for custom field definitions (keyed by field_id).
# Persists across warm Lambda invocations. Only successful lookups are cached.
_custom_field_def_cache: Dict[str, Dict] = {}


def _fetch_custom_field_def(
    field_id: str, base_url: str, headers: Dict
) -> Optional[Dict]:
    """Fetch a custom field definition by UUID, using a module-level cache."""
    if not field_id:
        return None
    if field_id in _custom_field_def_cache:
        return _custom_field_def_cache[field_id]
    # Try fetching from the company endpoint; use /v2/auth/me/ to discover company_id
    try:
        me = _api_call(f"{base_url}/v2/auth/me/", "GET", headers=headers)
        company_id = (me.get("company") or {}).get("id", "")
        if company_id:
            defn = _api_call(
                f"{base_url}/v2/companies/{company_id}/custom-fields/{field_id}/",
                "GET", headers=headers,
            )
            if defn and defn.get("id"):
                _custom_field_def_cache[field_id] = defn
                return defn
    except Exception as exc:
        logger.debug(f"_fetch_custom_field_def: failed for {field_id!r}: {exc}")
    return None


def _get_address_book_entry_uuid(order_stop: Dict) -> Optional[str]:
    """Extract the ADDRESS_BOOK_ENTRY_ID UUID from an order ship_from/ship_to stop.

    Corrogo orders carry two reference qualifiers on each stop:
      - ADDRESS_BOOK_ENTRY_REFERENCE_ID → numeric external ref (e.g. "2104292")
      - ADDRESS_BOOK_ENTRY_ID           → Shipwell address-book UUID

    We prefer the UUID for a direct /v2/address-book/<uuid>/ fetch rather than
    the slower full-text ?q= search.
    """
    for ref in (order_stop.get("references") or []):
        if str(ref.get("qualifier", "")).upper() == "ADDRESS_BOOK_ENTRY_ID":
            value = ref.get("value")
            if value:
                return str(value).strip()
    return None


def get_additional_transit_time(
    order_data: Dict, base_url: str, headers: Dict, custom_fields: Dict,
    pickup_stop: Optional[Dict] = None,
    dropoff_stop: Optional[Dict] = None,
) -> float:
    """Sum Additional Transit Time from shipment stop custom_data.

    Reads additional_transit_time from each shipment stop's
    custom_data.shipwell_custom_data.shipment_stop — populated when the order's
    purchase_order_stop custom_data flows through to the shipment stop via corrogo.

    If the field is absent on a stop, that stop contributes 0h (no fallback).
    """
    total = 0.0
    field_key = "additional_transit_time"
    field_id = custom_fields.get(field_key, "")

    if not field_id:
        return 0.0

    # Lazily fetch field definition for sign resolution (cached after first call)
    field_def: Optional[Dict] = _fetch_custom_field_def(field_id, base_url, headers)

    stop_pairs = [
        ("ship_from", pickup_stop),
        ("ship_to",   dropoff_stop),
    ]

    for stop_label, shipment_stop in stop_pairs:
        if not shipment_stop:
            logger.debug(f"get_additional_transit_time: {stop_label} — no shipment stop, skipping (0h)")
            continue

        _stop_cfs = (
            (shipment_stop.get("custom_data") or {})
            .get("shipwell_custom_data", {})
            .get("shipment_stop", {})
        )
        raw = _stop_cfs.get(field_id)

        if not raw:
            logger.debug(f"get_additional_transit_time: {stop_label} — field not set on shipment stop, using 0h")
            continue

        hours = parse_selection_transit_value(raw, field_def)
        logger.info(
            f"get_additional_transit_time: {stop_label} "
            f"source=shipment_stop.custom_data raw={raw!r} -> {hours}h"
        )
        total += hours

    return total


# ---------------------------------------------------------------------------
# Tulsa West Refinery (facility 2500) — Lubes multi-compartment load time
# ---------------------------------------------------------------------------
# Facility ID for Tulsa West Refinery (Lubes). Multi-rack repositioning logic
# applies only to this facility.
_LUBES_FACILITY_ID = "01KC4CNNNR3MQK96FRV9X6N8ZC"

# Dock group classification by product_category (case-insensitive).
# Both WFO variants map to the same group; repositioning between them = 30 min.
_LUBES_DOCK_GROUP: Dict[str, str] = {
    "extracts": "EXTRACT",
    "extracts-htw": "EXTRACT",
    "waxes": "WAX",
    "wax free oils": "WFO",
    "wax free oils-htw": "WFO",
}

# Repositioning times (hours) between dock group pairs.
# Key is a frozenset of the two group names.
_LUBES_REPOSITIONING_H: Dict[frozenset, float] = {
    frozenset({"WFO", "WFO"}): 30 / 60,      # two distinct WFO products → 30 min
    frozenset({"EXTRACT", "WFO"}): 60 / 60,   # Extract + WFO → 60 min
}


def _lubes_dock_group_for_load_type(lt: Dict) -> Optional[str]:
    """Return the dock group (EXTRACT / WAX / WFO) for a matched load type, or None."""
    for cat in (lt.get("product_category") or []):
        group = _LUBES_DOCK_GROUP.get(cat.lower().strip())
        if group:
            return group
    return None


def _lubes_repositioning_hours(groups: List[str]) -> float:
    """
    Given a list of dock groups (one per distinct product category), return
    the multi-rack repositioning time in hours.

    Rules:
    - All products in the same dock group → 0 min.
    - Two distinct WFO products (both map to WFO group but different
      product_category, e.g. WAX FREE OILS + WAX FREE OILS-HTW) → 30 min.
    - Extract + WFO → 60 min.
    - Wax never co-loads with anything (spec constraint; guarded upstream).

    NOTE: groups may contain duplicate group names (e.g. ['WFO', 'WFO']) when
    two different product categories both map to WFO. We must NOT deduplicate
    before checking — we count group occurrences to detect the WFO+WFO case.
    """
    if not groups:
        return 0.0
    unique_groups = set(groups)
    if len(unique_groups) == 1:
        sole_group = next(iter(unique_groups))
        # Two distinct product categories both in WFO group → 30 min.
        if sole_group == "WFO" and len(groups) >= 2:
            return 30 / 60
        return 0.0
    # Multiple distinct dock groups → look up repositioning table.
    if len(unique_groups) == 2:
        key = frozenset(unique_groups)
        return _LUBES_REPOSITIONING_H.get(key, 0.0)
    # 3+ distinct dock groups: not expected per spec; log and return 0.
    logger.warning(f"_lubes_repositioning_hours: unexpected {len(unique_groups)} dock groups {groups} — using 0")
    return 0.0


def _get_product_category_for_item(
    item: Dict, base_url: str, headers: Dict
) -> Optional[str]:
    """
    Resolve product_category for a single order item using the standard
    priority chain: item field → product catalog → description fallback.
    """
    if item.get("product_category"):
        return str(item["product_category"]).strip()
    product_id = (item.get("shipping_requirements") or {}).get("product_id")
    if product_id:
        try:
            product_data = _api_call(f"{base_url}/v2/products/{product_id}/", "GET", headers=headers)
            cat = product_data.get("category")
            if cat:
                return cat
        except Exception as e:
            logger.warning(f"Product lookup failed for product_id={product_id}: {e}")
    if item.get("description"):
        return item["description"].split()[0]
    return None


def _match_load_type(product_category: str, load_types: List[Dict], shipment_data: Optional[Dict] = None) -> Optional[Dict]:
    """
    Match a product_category string against a list of facility load types.
    Returns the best-matched load type dict, or None.
    """
    cat_lower = product_category.lower()

    def _cat_matches(lt: dict) -> bool:
        lt_name = (lt.get("name") or "").lower()
        lt_cats = [c.lower() for c in (lt.get("product_category") or [])]
        if lt_cats:
            return any(c == cat_lower or c in cat_lower or cat_lower in c for c in lt_cats)
        return bool(lt_name and (cat_lower in lt_name or lt_name in cat_lower))

    matched = next((lt for lt in load_types if _cat_matches(lt)), None)
    if not matched and shipment_data:
        equipment = (shipment_data.get("equipment_type") or {}).get("machine_readable", "")
        if equipment:
            eq_lower = equipment.lower()
            matched = next(
                (lt for lt in load_types
                 if eq_lower in (lt.get("name") or "").lower()
                 or eq_lower in (lt.get("machine_readable") or "").lower()),
                None,
            )
    if not matched:
        matched = next((lt for lt in load_types if lt.get("delivery_type") == "SHIPPING"), None)
    if not matched and load_types:
        matched = load_types[0]
    return matched


def get_facility_load_time(
    facility_id: str, order_data: Dict, base_url: str, headers: Dict,
    shipment_data: Optional[Dict] = None,
) -> float:
    """
    Fetch load time from facility load-types API matched by product category,
    with equipment type as a fallback (mirrors _get_matched_load_type_id priority).
    Returns load time in decimal hours. Default fallback: 0.75 hours (45 min).
    Mirrors getFacilityLoadTime() in SchedulingCalculations.gs.

    For Tulsa West Refinery (Lubes / facility 2500), applies multi-compartment
    load time logic:
    - Single product (or same product across multiple compartments): dock load time only.
    - Multiple distinct products: sum of each dock's load time + multi-rack
      repositioning time based on the dock group pair.
    """
    items = order_data.get("items") or []
    if not items:
        return DEFAULT_LOAD_TIME

    try:
        resp = _api_call(f"{base_url}/facilities/{facility_id}/load-types", "GET", headers=headers)
        load_types = resp.get("data") or []
        if not load_types:
            return DEFAULT_LOAD_TIME

        # --- Lubes multi-compartment path (Tulsa West Refinery only) ---
        if facility_id == _LUBES_FACILITY_ID:
            return _get_lubes_load_time(items, load_types, base_url, headers, shipment_data)

        # --- Standard single-product path ---
        first_item = items[0]
        product_category = _get_product_category_for_item(first_item, base_url, headers)
        if not product_category:
            return DEFAULT_LOAD_TIME

        matched = _match_load_type(product_category, load_types, shipment_data)
        if not matched or not matched.get("load_unload_duration"):
            return DEFAULT_LOAD_TIME

        parsed = parse_iso8601_duration(matched["load_unload_duration"])
        if parsed and parsed > 0:
            return parsed

    except Exception as e:
        logger.error(f"get_facility_load_time error: {e}", exc_info=True)

    return DEFAULT_LOAD_TIME


def _get_lubes_load_time(
    items: List[Dict],
    load_types: List[Dict],
    base_url: str,
    headers: Dict,
    shipment_data: Optional[Dict] = None,
) -> float:
    """
    Lubes (Tulsa West Refinery) multi-compartment loading time calculation.

    Algorithm:
    1. Resolve product_category for each order item (cache load type per category).
    2. Sum load times for ALL items — including duplicate categories (same product
       loaded into multiple compartments each counts as its own load time).
    3. Repositioning is determined by the SET of DISTINCT dock groups only:
       - All items in the same dock group → 0 min reposition
       - Two distinct WFO dock-group items (WAX FREE OILS + WAX FREE OILS-HTW) → 30 min
       - Extract + WFO → 60 min
       - Wax never co-loads (spec constraint)
    Same product in multiple compartments → load times summed, 0 repositioning.
    """
    # Step 1: resolve product_category for every item and match load type.
    # Always sum load times across all items (including duplicate categories).
    # Repositioning is determined by the SET of distinct dock groups only.
    resolved: List[tuple] = []  # list of (cat_key, matched_load_type)
    category_load_type_cache: Dict[str, Dict] = {}  # category_lower → matched load type (avoid repeat API calls)
    for item in items:
        cat = _get_product_category_for_item(item, base_url, headers)
        if not cat:
            continue
        cat_key = cat.lower().strip()
        if cat_key not in category_load_type_cache:
            category_load_type_cache[cat_key] = _match_load_type(cat, load_types, shipment_data)
        resolved.append((cat_key, category_load_type_cache[cat_key]))

    if not resolved:
        logger.warning("_get_lubes_load_time: no product categories resolved — using default load time")
        return DEFAULT_LOAD_TIME

    # Step 2: sum load times for every item; collect distinct dock groups for repositioning.
    total_load_time = 0.0
    seen_dock_groups: Dict[str, bool] = {}  # ordered-set of distinct dock groups
    for cat_key, lt in resolved:
        if not lt or not lt.get("load_unload_duration"):
            logger.warning(f"_get_lubes_load_time: no load type matched for category={cat_key!r} — skipping")
            continue
        lt_hours = parse_iso8601_duration(lt["load_unload_duration"]) or 0.0
        total_load_time += lt_hours
        group = _lubes_dock_group_for_load_type(lt)
        if group:
            seen_dock_groups[group] = True
        logger.info(
            f"_get_lubes_load_time: category={cat_key!r} → load_type={lt.get('name')!r} "
            f"dock_group={group} load_time={lt_hours}h"
        )

    if total_load_time == 0.0:
        return DEFAULT_LOAD_TIME

    # Step 3: repositioning uses only distinct dock groups (same product = same group = 0 reposition).
    distinct_dock_groups = list(seen_dock_groups.keys())
    repositioning_h = _lubes_repositioning_hours(distinct_dock_groups)
    total = total_load_time + repositioning_h
    logger.info(
        f"_get_lubes_load_time: items={len(resolved)} distinct_dock_groups={distinct_dock_groups} "
        f"sum_load_time={total_load_time}h repositioning={repositioning_h}h total={total}h"
    )
    return total


def calculate_transit_hours_for_windows(
    shipment_data: Dict, order_data: Dict, pickup_stop: Dict, dropoff_stop: Dict,
    base_url: str, headers: Dict, custom_fields: Dict,
) -> Tuple[float, Dict]:
    """Calculate transit time using the constants from the HFS document.

    Returns a tuple of (total_hours, breakdown_dict) where breakdown_dict contains
    each component for logging and audit purposes.
    """
    distance = float(shipment_data.get("total_miles") or 0)
    drive_time = distance / AVG_SPEED_MPH if distance > 0 else 0.0
    fuel_stops = int(distance / FUEL_INTERVAL_MILES)
    fuel_time = fuel_stops * FUEL_STOP_DURATION_HOURS
    hos_breaks = int(drive_time / BREAK_RULE_HOURS)
    break_time = hos_breaks * BREAK_DURATION_HOURS
    rest_periods = max(0, math.ceil(drive_time / MAX_DRIVE_HOURS_PER_DAY) - 1)
    rest_time = rest_periods * REST_DURATION_HOURS

    pickup_fid = (pickup_stop.get("location") or {}).get("facility_id")
    dropoff_fid = (dropoff_stop.get("location") or {}).get("facility_id")
    if pickup_fid:
        load_time = get_facility_load_time(pickup_fid, order_data, base_url, headers, shipment_data=shipment_data)
        load_time_source = "pickup facility dock"
    elif dropoff_fid:
        load_time = get_facility_load_time(dropoff_fid, order_data, base_url, headers, shipment_data=shipment_data)
        load_time_source = "dropoff facility dock"
    else:
        load_time = 0.5
        load_time_source = "default (0.5h — no facility dock config)"

    additional_transit = get_additional_transit_time(
        order_data, base_url, headers, custom_fields,
        pickup_stop=pickup_stop,
        dropoff_stop=dropoff_stop,
    )
    total = drive_time + load_time + fuel_time + break_time + rest_time + additional_transit

    breakdown = {
        "distance_miles": round(distance, 1),
        "drive_time_h": round(drive_time, 2),
        "load_time_h": round(load_time, 2),
        "load_time_source": load_time_source,
        "fuel_stops": fuel_stops,
        "fuel_time_h": round(fuel_time, 2),
        "hos_breaks": hos_breaks,
        "break_time_h": round(break_time, 2),
        "rest_periods": rest_periods,
        "rest_time_h": round(rest_time, 2),
        "additional_transit_h": round(additional_transit, 2),
        "total_h": round(total, 2),
    }
    return total, breakdown


_parse_local_datetime = parse_local_datetime
_window_from_order_or_stop = window_from_order_or_stop
_apply_default_business_times = apply_default_business_times
_clamp_forward_to_business_hours = clamp_forward_to_business_hours
_clamp_backward_to_business_hours = clamp_backward_to_business_hours
_clamp_to_facility_hours = clamp_to_facility_hours


# Known sandbox field UUID for Carrier Managed Dispatch (461637e7-82a0-4926-a608-4d43b10c3804).
# Updated 2026-07-09: confirmed via GET /v2/companies/<hfs-id>/custom-fields/ (field name: pptas_flag).
# Also configurable via the CARRIER_MANAGED_DISPATCH_FIELD_ID env var or the
# webhook custom_data.custom_fields mapping (keys: carrier_managed_dispatch / carrier_managed).
import os as _os
_CARRIER_MANAGED_FIELD_FALLBACK = _os.environ.get(
    "CARRIER_MANAGED_DISPATCH_FIELD_ID",
    "461637e7-82a0-4926-a608-4d43b10c3804",  # sandbox: pptas_flag field
)


def is_carrier_managed_dispatch(order_data: Dict, custom_fields: Dict) -> bool:
    """Return True when Carrier Managed Dispatch is X/Y/true.

    Looks up the field UUID via (in priority order):
    1. webhook custom_fields mapping keys 'carrier_managed_dispatch' / 'carrier_managed'
    2. CARRIER_MANAGED_DISPATCH_FIELD_ID env var (default: sandbox UUID 461637e7...)
    """
    # Priority 1: webhook-configured mapping
    for key in ("carrier_managed_dispatch", "carrier_managed"):
        field_id = custom_fields.get(key, "")
        if field_id:
            value = get_custom_field_value(order_data, field_id)
            if custom_field_is_truthy(value):
                logger.info(f"is_carrier_managed_dispatch: True via webhook mapping '{key}' -> {field_id}")
                return True

    # Priority 2: fallback hardcoded/env UUID
    if _CARRIER_MANAGED_FIELD_FALLBACK:
        value = get_custom_field_value(order_data, _CARRIER_MANAGED_FIELD_FALLBACK)
        if custom_field_is_truthy(value):
            logger.info(
                f"is_carrier_managed_dispatch: True via fallback field UUID {_CARRIER_MANAGED_FIELD_FALLBACK} "
                f"(value={value!r}). Consider adding 'carrier_managed_dispatch' to webhook custom_fields."
            )
            return True

    return False


def set_pickup_delivery_windows(
    shipment_data: Dict, order_data: Dict, base_url: str, headers: Dict,
    custom_fields: Dict, sw: "ShipwellProgram",
    is_auto_scheduled: bool = True,
) -> Optional[Dict[str, Any]]:
    """Set both pickup and delivery windows according to the HFS shipment creation rules.

    ``is_auto_scheduled`` controls whether Tempus appointment scheduling will run
    after this call.  When False (e.g. CUST/customer-pickup orders), point-in-time
    pickup windows are written as-is (start == end, rounded down to the nearest
    15-minute boundary) instead of being expanded by POINT_DELIVERY_APPT_WINDOW_HOURS
    for an availability query that will never happen.
    """
    logger.info("=== set_pickup_delivery_windows START ===")
    if DRY_RUN and shipment_data.get("id") == "dry-run-id":
        latest = copy.deepcopy(shipment_data)
    else:
        latest = _api_call(f"{base_url}/v2/shipments/{shipment_data['id']}/", "GET", headers=headers) or shipment_data
    pickup_stop = get_pickup_stop(latest.get("stops") or [])
    dropoff_stop = get_dropoff_stop(latest.get("stops") or [])
    if not pickup_stop or not dropoff_stop:
        sw.log("WARNING", "Missing pickup or delivery stop; shipment windows not updated", ["set_pickup_delivery_windows"])
        return None

    pickup_window = _window_from_order_or_stop(order_data.get("ship_from") or {}, pickup_stop)
    delivery_window = _window_from_order_or_stop(order_data.get("ship_to") or {}, dropoff_stop)
    
    logger.info(
        f"Window extraction: pickup_window={pickup_window is not None}, delivery_window={delivery_window is not None}"
    )
    if pickup_window:
        logger.info(
            f"Pickup window: {pickup_window['start'].isoformat()} → {pickup_window['end'].isoformat()} "
            f"(start_is_default={pickup_window.get('start_is_default')}, end_is_default={pickup_window.get('end_is_default')})"
        )
    if delivery_window:
        logger.info(
            f"Delivery window: {delivery_window['start'].isoformat()} → {delivery_window['end'].isoformat()} "
            f"(start_is_default={delivery_window.get('start_is_default')}, end_is_default={delivery_window.get('end_is_default')})"
        )

    carrier_managed = is_carrier_managed_dispatch(order_data, custom_fields)
    logger.info(f"Carrier managed dispatch: {carrier_managed}")

    # Warn when the planned pickup is in the past — this usually means the
    # ordering system sent stale order data (e.g. a re-submitted old row).
    now_utc = datetime.now(ZoneInfo("UTC"))
    if pickup_window and pickup_window["start"] < now_utc - timedelta(hours=12):
        logger.warning(
            f"Pickup window start {pickup_window['start'].isoformat()} is in the past "
            f"(now={now_utc.isoformat()}). The order may contain a stale ship date. "
            f"Windows will be set as received — verify order data is correct."
        )
    if delivery_window and delivery_window["start"] < now_utc - timedelta(hours=12):
        logger.warning(
            f"Delivery window start {delivery_window['start'].isoformat()} is in the past "
            f"(now={now_utc.isoformat()}). The order may contain a stale ship date."
        )

    transit_hours, transit_breakdown = calculate_transit_hours_for_windows(
        latest, order_data, pickup_stop, dropoff_stop, base_url, headers, custom_fields,
    )

    # Log transit calculation breakdown to both Python logger and Scriptly workflow logs
    _bd = transit_breakdown
    _transit_log = (
        f"Transit calculation: "
        f"{_bd['distance_miles']} mi ÷ {AVG_SPEED_MPH} mph = {_bd['drive_time_h']}h drive | "
        f"load time: {_bd['load_time_h']}h ({_bd['load_time_source']}) | "
        f"fuel stops: {_bd['fuel_stops']} × {FUEL_STOP_DURATION_HOURS}h = {_bd['fuel_time_h']}h | "
        f"HOS 30-min breaks: {_bd['hos_breaks']} = {_bd['break_time_h']}h | "
        f"rest periods: {_bd['rest_periods']} = {_bd['rest_time_h']}h | "
        f"additional transit: {_bd['additional_transit_h']}h | "
        f"TOTAL: {_bd['total_h']}h"
    )
    logger.info(_transit_log)
    sw.log("TRACE", _transit_log, ["set_pickup_delivery_windows", "transit_calculation"])

    # Resolve delivery (unload) facility load time for facility-hours fitting.
    delivery_fid_for_lt = (dropoff_stop.get("location") or {}).get("facility_id")
    delivery_load_time_h = (
        get_facility_load_time(delivery_fid_for_lt, order_data, base_url, headers, shipment_data=latest)
        if delivery_fid_for_lt else DEFAULT_LOAD_TIME
    )
    # Resolve pickup facility ID and load time (also in transit_breakdown but resolved here for fitting).
    pickup_fid_for_lt = (pickup_stop.get("location") or {}).get("facility_id")
    pickup_load_time_h = transit_breakdown.get("load_time_h") or DEFAULT_LOAD_TIME

    _is_point_in_time = False  # set True below only for point-in-time pickup orders
    _is_point_in_time_delivery = False  # set True when delivery is PIT and pickup window is a 3h derived window (latest-first scheduling)
    _preserve_pickup_window = False  # set True when pickup window is a derived range that must not be overwritten by appointment sync
    _only_pickup_window = False  # set True when the order has a pickup window but no delivery window — delivery stop planned window must be left blank

    if not pickup_window and not delivery_window:
        logger.info("Branch: Neither pickup nor delivery window — building 3-hour default window")
        # Neither the order plan_window nor the shipment stop has a date/time set.
        # Build a 3-hour pickup window anchored to now (or today's facility open time
        # if we're before business hours), then project delivery forward by transit_hours.
        pickup_tz_str = get_stop_timezone(pickup_stop)
        pickup_tz = ZoneInfo(pickup_tz_str)
        now_local = datetime.now(pickup_tz)
        today_str = now_local.date().isoformat()
        pickup_biz = business_hours_for_stop(pickup_stop, now_local, base_url, headers)
        # Anchor to the later of now and business open so we never set a past window
        anchor = max(now_local, pickup_biz["open"])
        window_end_candidate = anchor + timedelta(hours=POINT_DELIVERY_APPT_WINDOW_HOURS)
        # Clamp end to business close
        window_end_clamped = min(window_end_candidate, pickup_biz["close"])
        # If there isn't room for the full 3-hour window before close, start from close-3h
        if window_end_clamped < anchor + timedelta(hours=POINT_DELIVERY_APPT_WINDOW_HOURS):
            anchor = max(pickup_biz["open"], pickup_biz["close"] - timedelta(hours=POINT_DELIVERY_APPT_WINDOW_HOURS))
            window_end_clamped = pickup_biz["close"]
        pickup_result = {"start": anchor, "end": window_end_clamped}
        _raw_del_start = pickup_result["start"] + timedelta(hours=transit_hours)
        _raw_del_end   = pickup_result["end"]   + timedelta(hours=transit_hours)
        if delivery_fid_for_lt:
            _del_fitted = fit_window_to_facility_hours(
                _raw_del_start, _raw_del_end, delivery_load_time_h, dropoff_stop,
                direction="forward", base_url=base_url, headers=headers,
                shipment_data=shipment_data, sw=sw, label="Delivery",
            )
            delivery_start = _del_fitted["start"]
            delivery_end   = _del_fitted["end"]
        else:
            delivery_start = _clamp_forward_to_business_hours(_raw_del_start, dropoff_stop, base_url, headers)
            delivery_end   = _clamp_forward_to_business_hours(_raw_del_end,   dropoff_stop, base_url, headers)
        if delivery_end < delivery_start:
            delivery_end = delivery_start
        delivery_result = {"start": delivery_start, "end": delivery_end}
        sw.log(
            "TRACE",
            f"No order plan_window found — built 3-hour default pickup window "
            f"{pickup_result['start'].isoformat()} → {pickup_result['end'].isoformat()}",
            ["set_pickup_delivery_windows"],
        )
    elif pickup_window and delivery_window:
        logger.info("Branch: Both pickup and delivery windows exist — applying default business times conversion")
        if carrier_managed:
            logger.info("Carrier managed: merging pickup start with delivery end")
            pickup_adjusted = _apply_default_business_times(pickup_window, pickup_stop, base_url, headers)
            delivery_adjusted = _apply_default_business_times(delivery_window, dropoff_stop, base_url, headers)
            # For carrier-managed, both stops share the full window (pickup_start → delivery_end).
            # Guard: if delivery_adjusted end is before pickup_adjusted start (e.g. delivery date
            # earlier than pickup date due to bad order data), fall back to pickup window only.
            merged_start = pickup_adjusted["start"]
            merged_end = delivery_adjusted["end"]
            if merged_end < merged_start:
                logger.warning(
                    f"Carrier managed window inversion detected: pickup_start={merged_start.isoformat()} "
                    f"delivery_end={merged_end.isoformat()} — using pickup window end instead"
                )
                merged_end = pickup_adjusted["end"]
            pickup_result = {"start": merged_start, "end": merged_end}
            delivery_result = {"start": merged_start, "end": merged_end}
        else:
            logger.info("Non-carrier managed: applying business times to both windows independently")
            pickup_result = _apply_default_business_times(pickup_window, pickup_stop, base_url, headers)
            delivery_result = _apply_default_business_times(delivery_window, dropoff_stop, base_url, headers)

            # Validate that the gap between pickup end and delivery END is >= transit time.
            # Using delivery_end (not delivery_start) so that wide/full-day delivery windows
            # are not falsely flagged — the shipment can still be delivered toward the end
            # of the window even if transit time exceeds the gap to window start.
            # True impossibility = truck can't arrive before the delivery window closes.
            _gap_hours = (delivery_result["end"] - pickup_result["end"]).total_seconds() / 3600
            if _gap_hours < transit_hours:
                _order_id = (order_data or {}).get("order_number") or (order_data or {}).get("id", "?")
                _shortfall = transit_hours - _gap_hours
                _subject = f"HFS Window Gap Error — Order {_order_id}: delivery window too tight for transit"
                _error_detail = (
                    f"Pickup window ends {pickup_result['end'].isoformat()}, "
                    f"delivery window ends {delivery_result['end'].isoformat()}. "
                    f"Available time (pickup end → delivery end): {_gap_hours:.2f}h. "
                    f"Required transit: {transit_hours:.2f}h "
                    f"({transit_breakdown.get('distance_miles', '?')} mi). "
                    f"Gap is {_shortfall:.2f}h too short."
                )
                logger.warning(f"set_pickup_delivery_windows: {_error_detail}")
                sw.log("WARNING", _error_detail, ["set_pickup_delivery_windows", "transit_gap_check"])
                try:
                    _plain, _html = notify_bodies_for_order(
                        order_data, _subject,
                        error_detail=_error_detail,
                        issue_type="Window Gap Error",
                    )
                    notify_support(_subject, _plain, sw, html_body=_html)
                except Exception as _notify_err:
                    logger.warning(f"set_pickup_delivery_windows: transit gap email failed: {_notify_err}")

            # If delivery is point-in-time (start == end), flag for latest-first scheduling.
            # This happens when _backfill_order_plan_window pre-fills ship_from before
            # shipment-assembly, so both windows are present at set_pickup_delivery_windows time.
            _delivery_pit = delivery_result["start"] == delivery_result["end"]
            if _delivery_pit:
                _is_point_in_time_delivery = True
                _preserve_pickup_window = True
                logger.info(
                    "Both-windows branch: delivery is PIT (start==end after business-times conversion) — "
                    "setting is_point_in_time_delivery=True, force_prefer_latest for scheduling"
                )
    elif pickup_window:
        logger.info("Branch: Only pickup window exists — calculating delivery from pickup + transit")
        _only_pickup_window = True
        pickup_result = _apply_default_business_times(pickup_window, pickup_stop, base_url, headers)
        # If the order specified a single point-in-time (start == end), track it so the
        # appointment scheduler can enforce exact-time-only booking (no fallback to other slots).
        # Expand end by POINT_DELIVERY_APPT_WINDOW_HOURS so Tempus availability query has a valid
        # range and the planned window displays the scheduling window to dispatchers.
        _is_point_in_time = pickup_result["start"] >= pickup_result["end"]
        if _is_point_in_time:
            # Store the original point-in-time end so the stop's planned window
            # displays exactly 12:00–12:00 (not the expanded scheduling range).
            _pit_end = pickup_result["start"]  # start == end for point-in-time
            pickup_result["pit_end"] = _pit_end
            if is_auto_scheduled:
                # Expand end for the Tempus availability query (requires start < end).
                # Use the facility's actual load time (appointment duration) for the expansion
                # so Tempus can find a slot of exactly the right length starting at the PIT.
                # Fall back to POINT_DELIVERY_APPT_WINDOW_HOURS if load time is unavailable.
                _pit_query_hours = transit_breakdown.get("load_time_h") or POINT_DELIVERY_APPT_WINDOW_HOURS
                pickup_result["end"] = pickup_result["start"] + timedelta(hours=_pit_query_hours)
                logger.info(
                    f"Only-pickup branch: point-in-time window — expanded end by {_pit_query_hours}h (load_time) "
                    f"for availability query; planned window will show {pickup_result['start'].isoformat()} – {_pit_end.isoformat()}"
                )
            else:
                # Not auto-scheduled (e.g. CUST carrier) — no Tempus query needed.
                # Write the exact point-in-time as-is; end stays equal to start.
                logger.info(
                    f"Only-pickup branch: point-in-time window, not auto-scheduled — "
                    f"writing exact time {pickup_result['start'].isoformat()} (no expansion)"
                )
        # Delivery window derivation from pickup-only order:
        #   PIT pickup  → delivery_start = PIT + transit
        #                  delivery_end   = PIT + transit + 3h  (3h arrival window)
        #   Range pickup → delivery_start = order_pickup_start + transit
        #                  delivery_end   = order_pickup_end   + transit
        #   For range pickup, derive delivery from the CLAMPED pickup window (pickup_result)
        #   rather than the raw order plan_window. When the order has a full-day default
        #   pickup window (e.g. 00:01–23:59), pickup_result is already clamped to the
        #   facility's operating hours (e.g. 05:15–13:30). Using the raw order endpoints
        #   would anchor delivery to 00:01, producing a wrong delivery start and a
        #   23:58-wide delivery window instead of the correct facility-hours span.
        _pit_anchor = pickup_result["start"]  # for PIT: start == end before any expansion
        _order_pickup_start = pickup_result["start"]  # clamped-to-facility-hours pickup start
        _order_pickup_end   = pickup_result["end"]    # clamped-to-facility-hours pickup end
        _raw_del_start_op = _order_pickup_start + timedelta(hours=transit_hours)
        if _is_point_in_time:
            # PIT pickup: delivery window opens at transit offset, closes 3h later.
            _raw_del_end_op = _pit_anchor + timedelta(hours=transit_hours + POINT_DELIVERY_APPT_WINDOW_HOURS)
            if delivery_fid_for_lt:
                _del_fitted_op = fit_window_to_facility_hours(
                    _raw_del_start_op, _raw_del_end_op, delivery_load_time_h, dropoff_stop,
                    direction="forward", base_url=base_url, headers=headers,
                    shipment_data=shipment_data, sw=sw, label="Delivery",
                )
                delivery_start = _del_fitted_op["start"]
                delivery_end   = _del_fitted_op["end"]
            else:
                delivery_start = _clamp_forward_to_business_hours(_raw_del_start_op, dropoff_stop, base_url, headers)
                delivery_end   = clamp_end_to_business_hours(_raw_del_end_op, dropoff_stop, base_url, headers)
            if delivery_end < delivery_start:
                delivery_end = delivery_start
            logger.info(
                f"PIT pickup delivery derivation: PIT={_pit_anchor.isoformat()} + "
                f"{transit_hours:.2f}h transit + {POINT_DELIVERY_APPT_WINDOW_HOURS}h window = "
                f"{delivery_start.isoformat()} – {delivery_end.isoformat()} (clamped to facility close)"
            )
        else:
            # Range pickup: delivery start/end = order plan_window start/end + transit.
            _raw_del_end_range = _order_pickup_end + timedelta(hours=transit_hours)
            if delivery_fid_for_lt:
                _del_fitted_range = fit_window_to_facility_hours(
                    _raw_del_start_op, _raw_del_end_range, delivery_load_time_h, dropoff_stop,
                    direction="forward", base_url=base_url, headers=headers,
                    shipment_data=shipment_data, sw=sw, label="Delivery",
                )
                delivery_start = _del_fitted_range["start"]
                delivery_end   = _del_fitted_range["end"]
            else:
                delivery_start = _clamp_forward_to_business_hours(_raw_del_start_op, dropoff_stop, base_url, headers)
                delivery_end   = clamp_end_to_business_hours(_raw_del_end_range, dropoff_stop, base_url, headers)
            logger.info(
                f"Range pickup delivery derivation: "
                f"pickup (clamped) [{_order_pickup_start.isoformat()} – {_order_pickup_end.isoformat()}] + "
                f"{transit_hours:.2f}h transit = "
                f"{delivery_start.isoformat()} – {delivery_end.isoformat()} (clamped to facility close)"
            )
        if delivery_end < delivery_start:
            delivery_end = delivery_start
        delivery_result = {"start": delivery_start, "end": delivery_end}
    else:
        logger.info("Branch: Only delivery window exists — calculating pickup backward from delivery - transit")
        # Apply default business-hour times for any delivery window (full-day or partial-day).
        # The full-day special case has been removed — all delivery windows are treated uniformly.
        delivery_result = _apply_default_business_times(delivery_window, dropoff_stop, base_url, headers)
        _is_point_in_time_delivery = delivery_result["start"] == delivery_result["end"]
        if _is_point_in_time_delivery:
            _preserve_pickup_window = True  # 3-hour pickup window derived from delivery point — must not be stomped by appointment sync
            # For point-in-time delivery orders with no pickup window, derive the pickup
            # window by working backwards from the delivery point:
            #   pickup_end   = delivery_point - transit_hours  (latest the truck can leave)
            #   pickup_start = pickup_end - POINT_DELIVERY_APPT_WINDOW_HOURS  (3-hour window)
            # This window is both the displayed planned window on the stop AND the
            # availability search window. Slot selection is latest-first so the truck
            # departs as close to pickup_end as possible and still makes the delivery.
            raw_pickup_end = delivery_result["start"] - timedelta(hours=transit_hours)
            # pickup_end: clamp backward (if after close, pull to close; if before open, go to prev-day close)
            pickup_end = _clamp_backward_to_business_hours(raw_pickup_end, pickup_stop, base_url, headers)
            # pickup_start: derive from the *clamped* pickup_end (not raw_pickup_end) so that
            # when raw_pickup_end is far enough past close that raw_pickup_end - 3h is also
            # past close, both don't independently clamp to close and produce a 0h window.
            # Example: raw_pickup_end=19:00, close=13:30 → pickup_end clamped to 13:30;
            #   raw_pickup_start derived from 13:30 - 3h = 10:30 (within hours) → 3h window.
            raw_pickup_start = pickup_end - timedelta(hours=POINT_DELIVERY_APPT_WINDOW_HOURS)
            # pickup_start: clamp FORWARD — if before open, advance to open time.
            # Do NOT clamp backward here: raw_pickup_start may be before the facility's open
            # time (e.g. 05:15 on a 06:00-open facility) and backward-clamping would jump
            # it to the previous-night close time (e.g. 02:00), producing a nonsensical
            # window that starts before the facility is open.
            pickup_biz = business_hours_for_stop(pickup_stop, raw_pickup_start, base_url, headers)
            if raw_pickup_start < pickup_biz["open"]:
                pickup_start = pickup_biz["open"]
            elif raw_pickup_start > pickup_biz["close"]:
                pickup_start = pickup_biz["close"]
            else:
                pickup_start = raw_pickup_start
            if pickup_end < pickup_start:
                pickup_end = pickup_start
            # Load-time guard: if the clamped window is shorter than the load time
            # (e.g. raw_pickup_end just barely inside facility open so pickup_start was
            # forced forward, squashing the window), there isn't enough dock time on this
            # day. Roll pickup_end back to the PREVIOUS day's close and rebuild the 3h
            # window from there, giving a full slot on the prior operating day.
            _pit_load_time_h = transit_breakdown.get("load_time_h") or 0.0
            if _pit_load_time_h > 0 and (pickup_end - pickup_start).total_seconds() / 3600 < _pit_load_time_h:
                logger.info(
                    f"PIT pickup: window {pickup_start.isoformat()} – {pickup_end.isoformat()} "
                    f"({(pickup_end - pickup_start).total_seconds()/3600:.2f}h) is shorter than "
                    f"load_time ({_pit_load_time_h}h) — rolling to previous day close"
                )
                _prev_day_biz = business_hours_for_stop(pickup_stop, pickup_end - timedelta(days=1), base_url, headers)
                if _prev_day_biz:
                    pickup_end = _prev_day_biz["close"]
                    _raw_ps_prev = pickup_end - timedelta(hours=POINT_DELIVERY_APPT_WINDOW_HOURS)
                    if _raw_ps_prev < _prev_day_biz["open"]:
                        pickup_start = _prev_day_biz["open"]
                    elif _raw_ps_prev > _prev_day_biz["close"]:
                        pickup_start = _prev_day_biz["close"]
                    else:
                        pickup_start = _raw_ps_prev
                    if pickup_end < pickup_start:
                        pickup_end = pickup_start
                    logger.info(
                        f"PIT pickup: rolled to prev day "
                        f"{pickup_start.isoformat()} – {pickup_end.isoformat()}"
                    )
            _pit_window_log = (
                f"Point-in-time delivery window derivation: "
                f"delivery={delivery_result['start'].isoformat()} − "
                f"{transit_hours:.2f}h total transit "
                f"({transit_breakdown['drive_time_h']}h drive + "
                f"{transit_breakdown['load_time_h']}h load + "
                f"{transit_breakdown['fuel_time_h']}h fuel + "
                f"{transit_breakdown['break_time_h']}h HOS breaks + "
                f"{transit_breakdown['rest_time_h']}h rest + "
                f"{transit_breakdown['additional_transit_h']}h additional) = "
                f"pickup window {pickup_start.isoformat()} – {pickup_end.isoformat()} "
                f"(3h window ending at latest viable pickup)"
            )
            logger.info(_pit_window_log)
            sw.log("TRACE", _pit_window_log, ["set_pickup_delivery_windows", "point_in_time_delivery"])
        else:
            _preserve_pickup_window = True  # derived range from delivery window — must not be stomped by appointment sync
            # Mirror the delivery window width: both start and end are shifted back by
            # transit_total. This preserves the full span of the delivery window as the
            # pickup window, so a 4-hour delivery window produces a 4-hour pickup window.
            #   pickup_start = delivery_start − transit_total
            #   pickup_end   = delivery_end   − transit_total
            # Both are then clipped to the pickup facility's operating hours.
            raw_pickup_start = delivery_result["start"] - timedelta(hours=transit_hours)
            raw_pickup_end   = delivery_result["end"]   - timedelta(hours=transit_hours)
            _outside_hours_flag = False  # lifted to delivery-range scope for return value
            # Off-hours guard: if the entire derived pickup window falls outside the
            # pickup facility's operating hours (raw_pickup_end is before open on its day
            # AND raw_pickup_start doesn't reach the previous day's close), there is no
            # valid slot and continuing would produce a degenerate zero-width window.
            # Send an error email and abort window calculation.
            _guard_biz = business_hours_for_stop(pickup_stop, raw_pickup_end, base_url, headers)
            _outside_hours = False
            if raw_pickup_end < _guard_biz["open"]:
                # Window end is before facility open — check if start is also before open
                # (i.e. entire window falls in overnight gap before today's open).
                _guard_prev_biz = business_hours_for_stop(
                    pickup_stop, raw_pickup_end - timedelta(days=1), base_url, headers
                )
                if raw_pickup_start > _guard_prev_biz["close"]:
                    _outside_hours = True
            elif raw_pickup_start > _guard_biz["close"]:
                # Window start is after facility close — entire window falls after today's close.
                _outside_hours = True
            if _outside_hours:
                _outside_hours_flag = True
                _loc_name = (pickup_stop.get("location") or {}).get("location_name") or ""
                _subject = "Pickup Window Outside Facility Hours — Manual Scheduling Required"
                _detail = (
                    f"The derived pickup window "
                    f"{raw_pickup_start.isoformat()} \u2013 {raw_pickup_end.isoformat()} "
                    f"falls entirely outside the operating hours of the pickup facility"
                    + (f" ({_loc_name!r})" if _loc_name else "") + ". "
                    f"Delivery window: {delivery_result['start'].isoformat()} \u2013 "
                    f"{delivery_result['end'].isoformat()}; "
                    f"transit: {transit_hours:.2f}h. "
                    f"Facility hours on {raw_pickup_end.date()}: "
                    f"{_guard_biz['open'].isoformat()} \u2013 {_guard_biz['close'].isoformat()}. "
                    f"A manual pickup appointment must be scheduled."
                )
                _plain, _html = notify_bodies_for_shipment(latest, _subject, error_detail=_detail)
                notify_support(_subject, _plain, sw, html_body=_html)
                sw.log("WARNING", _subject + " — " + _detail, ["set_pickup_delivery_windows"])
                # Write the raw derived pickup window to the shipment stop even though it
                # falls outside facility hours, so the planner can see the calculated window
                # and manually schedule. Do NOT clamp — use the raw values as-is.
                pickup_start = round_down_to_quarter_hour(raw_pickup_start)
                pickup_end   = round_down_to_quarter_hour(raw_pickup_end)
                logger.info(
                    f"Delivery-range derived pickup window (outside facility hours — written as-is): "
                    f"delivery [{delivery_result['start'].isoformat()} – {delivery_result['end'].isoformat()}] "
                    f"− {transit_hours:.2f}h transit → "
                    f"raw [{raw_pickup_start.isoformat()} – {raw_pickup_end.isoformat()}] "
                    f"(outside facility hours {_guard_biz['open'].isoformat()} – {_guard_biz['close'].isoformat()})"
                )
            else:
                # Step 1: clamp end to facility hours on the day of raw_pickup_end (authoritative anchor).
                # - After close  → pull back to close.
                # - Within hours → use as-is.
                # - Before open  → clamp to open (do NOT jump to previous day's close; the
                #   derived window only covers raw_pickup_start → raw_pickup_end and we only
                #   go to the previous day's close if raw_pickup_start itself extends there).
                _end_biz = business_hours_for_stop(pickup_stop, raw_pickup_end, base_url, headers)
                if raw_pickup_end > _end_biz["close"]:
                    pickup_end = _end_biz["close"]
                elif raw_pickup_end < _end_biz["open"]:
                    # raw_pickup_end is before today's open; go to previous day's close only
                    # if the window actually extends there (raw_pickup_start is on the prev day).
                    _prev_day = raw_pickup_end - timedelta(days=1)
                    _prev_biz = business_hours_for_stop(pickup_stop, _prev_day, base_url, headers)
                    if raw_pickup_start <= _prev_biz["close"]:
                        pickup_end = _prev_biz["close"]
                    else:
                        pickup_end = _end_biz["open"]
                else:
                    pickup_end = raw_pickup_end
                # Step 2: clamp start forward to facility hours on the same day as pickup_end.
                # Using pickup_end's date as the reference prevents start from landing on a
                # different day than end when raw_pickup_start crosses a midnight boundary.
                pickup_biz = business_hours_for_stop(pickup_stop, pickup_end, base_url, headers)
                if raw_pickup_start < pickup_biz["open"]:
                    pickup_start = pickup_biz["open"]
                elif raw_pickup_start > pickup_biz["close"]:
                    pickup_start = pickup_biz["close"]
                else:
                    pickup_start = raw_pickup_start
                if pickup_end < pickup_start:
                    pickup_end = pickup_start
                logger.info(
                    f"Delivery-range derived pickup window (width-preserved, clipped to facility hours): "
                    f"delivery [{delivery_result['start'].isoformat()} – {delivery_result['end'].isoformat()}] "
                    f"− {transit_hours:.2f}h transit → "
                    f"raw [{raw_pickup_start.isoformat()} – {raw_pickup_end.isoformat()}] → "
                    f"clipped [{pickup_start.isoformat()} – {pickup_end.isoformat()}]"
                )
        pickup_result = {"start": pickup_start, "end": pickup_end}

    # Gap 3: warn if pickup window start is within 4 hours of now
    if pickup_result:
        _warn_if_pickup_imminent(pickup_result["start"], latest, sw)

    # Round pickup window DOWN to the nearest 15-minute boundary so it aligns with
    # quarter-hour appointment slots. Both start and end are floored so the displayed
    # planned window is always conservative (never overshoots the calculated bounds).
    pickup_start_rounded = round_down_to_quarter_hour(pickup_result["start"])
    pickup_end_rounded = round_down_to_quarter_hour(pickup_result["end"])

    # Enforce a minimum 3-hour pickup planning window on the stop —
    # but NOT for point-in-time pickup orders. PIT orders must display
    # start == end on the stop (the exact requested time). The 3h expansion
    # was already applied to the availability query window above; restoring
    # pit_end here ensures the stop shows the dispatcher-visible PIT time.
    if _is_point_in_time and "pit_end" in pickup_result:
        _pit_end_rounded = round_down_to_quarter_hour(pickup_result["pit_end"])
        pickup_end_rounded = _pit_end_rounded
        pickup_start_rounded = _pit_end_rounded  # start == end for PIT
        logger.info(
            f"set_pickup_delivery_windows: PIT pickup — writing start==end={_pit_end_rounded.isoformat()} to stop "
            f"(3h minimum not enforced for point-in-time)"
        )
    else:
        # Only enforce the 3-hour minimum when the order did NOT provide an explicit
        # pickup window range AND the pickup window was not derived from a delivery
        # range (which already width-preserves the delivery span minus transit).
        # In both cases the order's intent is clear and should be honoured as-is.
        _pickup_window_explicit = (
            pickup_window
            and not pickup_window.get("start_is_default", True)
            and not pickup_window.get("end_is_default", True)
        )
        _skip_3h_minimum = _pickup_window_explicit or _preserve_pickup_window
        _min_pickup_window = timedelta(hours=POINT_DELIVERY_APPT_WINDOW_HOURS)
        if not _skip_3h_minimum and pickup_end_rounded - pickup_start_rounded < _min_pickup_window:
            pickup_end_rounded = pickup_start_rounded + _min_pickup_window
            logger.info(
                f"set_pickup_delivery_windows: enforcing 3h minimum pickup planning window "
                f"(was {(pickup_result['end'] - pickup_result['start']).total_seconds()/3600:.2f}h) "
                f"→ {pickup_start_rounded.isoformat()} – {pickup_end_rounded.isoformat()}"
            )
        elif _skip_3h_minimum:
            logger.info(
                f"set_pickup_delivery_windows: pickup window {'explicitly set on order' if _pickup_window_explicit else 'derived from delivery range'} "
                f"{pickup_start_rounded.isoformat()} – {pickup_end_rounded.isoformat()} "
                f"— 3h minimum not enforced"
            )

    logger.info(
        f"Final calculated windows BEFORE writing to shipment: "
        f"pickup {pickup_result['start'].isoformat()} → {pickup_result['end'].isoformat()} "
        f"(rounded: {pickup_start_rounded.isoformat()} → {pickup_end_rounded.isoformat()}), "
        f"delivery {delivery_result['start'].isoformat()} → {delivery_result['end'].isoformat()}"
    )

    set_stop_window(pickup_stop, pickup_start_rounded, pickup_end_rounded)

    # For carrier-managed dispatch, delivery_result shares the merged window
    # (pickup_start → delivery_end). set_stop_window uses start for planned_date,
    # which would set the delivery stop to the pickup date (e.g. Jul 1) instead of
    # the delivery date (e.g. Jul 30). Fix: anchor the delivery stop's planned_date
    # on the END of the merged window.
    if carrier_managed:
        tz_str = get_stop_timezone(dropoff_stop)
        tz = ZoneInfo(tz_str)
        delivery_end_local = delivery_result["end"].astimezone(tz)
        delivery_start_local = delivery_result["start"].astimezone(tz)
        delivery_start_local = round_up_to_quarter_hour(delivery_start_local)
        delivery_end_local = round_up_to_quarter_hour(delivery_end_local)
        dropoff_stop["planned_date"] = delivery_end_local.strftime("%Y-%m-%d")
        dropoff_stop["planned_time_window_start"] = delivery_start_local.strftime("%H:%M:%S")
        dropoff_stop["planned_time_window_end"] = delivery_end_local.strftime("%H:%M:%S")
        logger.info(
            f"Carrier managed: delivery stop planned_date set to end date "
            f"{delivery_end_local.strftime('%Y-%m-%d')} "
            f"(window {delivery_start_local.strftime('%H:%M')}–{delivery_end_local.strftime('%H:%M')} {tz_str}) [quarter-hour rounded up]"
        )
    elif _only_pickup_window:
        # Order had a pickup window but no delivery window — write the derived delivery
        # window (calculated from pickup + transit) to the delivery stop so dispatchers
        # can see the expected arrival window. Round both endpoints down to the nearest
        # 15-minute boundary to match the pickup stop rounding convention.
        _del_start_rounded = round_up_to_quarter_hour(delivery_result["start"])
        _del_end_rounded   = round_up_to_quarter_hour(delivery_result["end"])
        set_stop_window(dropoff_stop, _del_start_rounded, _del_end_rounded)
        logger.info(
            f"Only-pickup branch: writing derived delivery window "
            f"{_del_start_rounded.isoformat()} – {_del_end_rounded.isoformat()} "
            f"to delivery stop (derived from pickup + transit, rounded up to 15-min)"
        )
    else:
        # When the delivery window came explicitly from the order (not derived/calculated),
        # write it exactly as specified — do not round. Rounding is only appropriate when
        # the Lambda calculated the window itself (derived from pickup + transit, or defaulted
        # to facility hours). An order-provided window of e.g. 00:15–23:45 must be written
        # as 00:15–23:45, not rounded up to 00:15–00:00 (midnight overflow) or any other value.
        _delivery_window_from_order = (
            delivery_window is not None
            and not delivery_window.get("start_is_default")
            and not delivery_window.get("end_is_default")
        )
        if _delivery_window_from_order:
            # Preserve the order-specified delivery window exactly as-is.
            set_stop_window(
                dropoff_stop,
                delivery_result["start"],
                delivery_result["end"],
            )
            logger.info(
                f"Order-specified delivery window written as-is (no rounding): "
                f"{delivery_result['start'].isoformat()} – {delivery_result['end'].isoformat()}"
            )
        else:
            # PIT delivery or facility-hours-substituted window — round to quarter-hour.
            set_stop_window(
                dropoff_stop,
                round_up_to_quarter_hour(delivery_result["start"]),
                round_up_to_quarter_hour(delivery_result["end"]),
            )

    logger.info(
        f"Stop windows set in memory: "
        f"pickup planned_date={pickup_stop.get('planned_date')}, "
        f"planned_time_window_start={pickup_stop.get('planned_time_window_start')}, "
        f"planned_time_window_end={pickup_stop.get('planned_time_window_end')}"
    )
    logger.info(
        f"Stop windows set in memory: "
        f"delivery planned_date={dropoff_stop.get('planned_date')}, "
        f"planned_time_window_start={dropoff_stop.get('planned_time_window_start')}, "
        f"planned_time_window_end={dropoff_stop.get('planned_time_window_end')}"
    )
    
    # Use individual stop PUTs instead of a full shipment PUT to avoid clobbering
    # concurrently-added stops (e.g. split child delivery stops added while this
    # invocation is still running). The stop PUT only touches the single stop
    # and does not require the full shipment body.
    _stop_read_only_keys = [
        "carrier_specified_eta", "predictive_model_eta", "trip_management_eta",
        "display_eta_window", "display_planned_window", "display_schedule",
        "auto_ordinal_index", "alerts", "on_time",
    ]
    if not DRY_RUN:
        for _stop in [pickup_stop, dropoff_stop]:
            _stop_id = _stop.get("id")
            if not _stop_id:
                continue
            # Fetch the canonical stop body (avoids sending stale fields that cause 500s).
            _stop_body = _api_call(
                f"{base_url}/v2/shipments/{latest['id']}/stops/{_stop_id}/",
                "GET", headers=headers,
            ) or dict(_stop)
            for _k in _stop_read_only_keys:
                _stop_body.pop(_k, None)
            # Apply the computed window fields from our in-memory stop dict.
            for _wk in ["planned_date", "planned_time_window_start", "planned_time_window_end"]:
                if _stop.get(_wk) is not None:
                    _stop_body[_wk] = _stop[_wk]
            # For facility stops, also set appointment_type=BY_APPOINTMENT_ONLY.
            # The Tempus SyncUnscheduledAndFCFSAppointmentConsumer watches
            # appointment.type (not planned_date) in the stop.updated event to decide
            # whether to create an UNSCHEDULED record. Without this field change the
            # consumer's early-return fires and no record is created.
            _stop_facility_id = (_stop_body.get("location") or {}).get("facility_id")
            if _stop_facility_id and not _stop_body.get("appointment_type"):
                _stop_body["appointment_type"] = "BY_APPOINTMENT_ONLY"
            safe_update_shipment_stop(
                latest["id"], _stop_id, _stop_body, base_url, headers,
                reason="Set pickup/delivery window (stop PUT, not full shipment PUT)",
            )
        logger.info(
            f"[TRACE] Shipment windows set: "
            f"pickup {pickup_stop.get('planned_date')} {pickup_stop.get('planned_time_window_start')}–{pickup_stop.get('planned_time_window_end')}, "
            f"delivery {dropoff_stop.get('planned_date')} {dropoff_stop.get('planned_time_window_start')}–{dropoff_stop.get('planned_time_window_end')}"
        )
    else:
        logger.info(
            f"[DRY_RUN] Would PUT stop windows: "
            f"pickup {pickup_stop.get('planned_date')} {pickup_stop.get('planned_time_window_start')}–{pickup_stop.get('planned_time_window_end')}, "
            f"delivery {dropoff_stop.get('planned_date')} {dropoff_stop.get('planned_time_window_start')}–{dropoff_stop.get('planned_time_window_end')}"
        )
    # Re-fetch to pick up latest stop state (including any concurrently-added stops).
    latest = _api_call(f"{base_url}/v2/shipments/{latest['id']}/", "GET", headers=headers) or latest

    # For carrier-managed dispatch, set appointment_window on both stops to the full
    # merged ISO range (pickup_start → delivery_end). This is what drives the
    # multi-day "Planned" and "Appointment" display in the Shipwell UI
    # (e.g. "Mon Jun 29, 00:00 CDT - Sat Jul 4, 02:00 CDT").
    # appointment_window must be written via the stop PUT endpoint separately —
    # it is not propagated through the full shipment PUT.
    # carrier_managed: planning window only — no appointment_window or appointment_type set.
    # Tempus scheduling is suppressed at the handler level (auto_schedule_enabled=False when carrier_managed).

    sw.log(
        "TRACE",
        f"Shipment windows set: pickup {pickup_result['start'].isoformat()} -> {pickup_result['end'].isoformat()}, "
        f"delivery {delivery_result['start'].isoformat()} -> {delivery_result['end'].isoformat()}",
        ["set_pickup_delivery_windows"],
    )
    logger.info("=== set_pickup_delivery_windows COMPLETED ===")
    return {
        "start_datetime": pickup_result["start"].isoformat(),
        "end_datetime": pickup_result["end"].isoformat(),
        "pickup_stop": pickup_stop,
        "delivery_stop": dropoff_stop,
        "transit_hours": transit_hours,
        "is_point_in_time": _is_point_in_time,
        "is_point_in_time_delivery": _is_point_in_time_delivery,
        "preserve_pickup_window": _preserve_pickup_window,
        "outside_facility_hours": locals().get("_outside_hours_flag", False),

    }


def calculate_pickup_window(
    shipment_data: Dict, order_data: Dict, base_url: str, headers: Dict,
) -> Optional[Dict]:
    """
    Calculate pickup window using Path A (backward from delivery) or Path B (forward from pickup).
    Returns {'start_datetime', 'end_datetime', 'pickup_stop', 'load_time'} or None.
    Mirrors calculatePickupWindow() in AppointmentScheduler.gs.
    """
    logger.info("=== calculate_pickup_window START ===")
    stops = shipment_data.get("stops") or []
    dropoff_stop = get_dropoff_stop(stops)
    pickup_stop = get_pickup_stop(stops)

    if not dropoff_stop or not pickup_stop:
        logger.error("Missing pickup or dropoff stop - cannot calculate pickup window")
        return None

    delivery_tz = get_stop_timezone(dropoff_stop)
    pickup_tz = get_stop_timezone(pickup_stop)
    delivery_date = dropoff_stop.get("planned_date")
    delivery_time_start = dropoff_stop.get("planned_time_window_start")
    delivery_time_end = dropoff_stop.get("planned_time_window_end")
    pickup_date = pickup_stop.get("planned_date")
    pickup_time_start = pickup_stop.get("planned_time_window_start")
    pickup_time_end = pickup_stop.get("planned_time_window_end")

    pickup_fid = (pickup_stop.get("location") or {}).get("facility_id")
    delivery_fid = (dropoff_stop.get("location") or {}).get("facility_id")

    load_time = get_facility_load_time(pickup_fid, order_data, base_url, headers) if pickup_fid else DEFAULT_LOAD_TIME
    logger.info(f"Load time: {load_time} hours")

    delivery_case = detect_delivery_window_case(delivery_time_start, delivery_time_end)
    appt_window = 3.0 if delivery_time_start and delivery_time_start == delivery_time_end else 0.0

    use_path_b = delivery_case in (0, 1) or not (pickup_date and pickup_time_start and pickup_time_end)
    logger.info(f"Delivery case: {delivery_case}, appointment_window: {appt_window}, path: {'B' if use_path_b else 'A'}")

    safe_pickup_date = pickup_date or delivery_date or datetime.now().strftime("%Y-%m-%d")
    pickup_hours = get_facility_hours(pickup_fid, safe_pickup_date, pickup_tz, base_url, headers) if pickup_fid else None
    delivery_hours = get_facility_hours(delivery_fid, delivery_date, delivery_tz, base_url, headers) if delivery_fid and delivery_date else None

    if not use_path_b and delivery_date and delivery_time_start and delivery_time_end:
        logger.info("Path A: backward from delivery")
        start_str = calc_load_travel_time(shipment_data, delivery_date, delivery_time_start, delivery_tz, load_time, appt_window)
        # end_str uses 0.0 (no extra appt_window) so pickup_end stays at latest-load-start,
        # giving a [pickup_start, pickup_end] window of width appt_window (typically 3 h).
        end_str = calc_load_travel_time(shipment_data, delivery_date, delivery_time_end, delivery_tz, load_time, 0.0)
        try:
            _raw_start = datetime.fromisoformat(start_str)
            _raw_end   = datetime.fromisoformat(end_str)
            if pickup_fid:
                _fitted = fit_window_to_facility_hours(
                    _raw_start, _raw_end, load_time, pickup_stop,
                    direction="backward", base_url=base_url, headers=headers,
                    shipment_data=shipment_data, sw=sw, label="Pickup",
                )
                start_dt, end_dt = _fitted["start"], _fitted["end"]
                _warn_if_pickup_imminent(start_dt, shipment_data, sw)
            else:
                start_dt = _clamp_to_facility_hours(_raw_start, pickup_hours)
                end_dt   = _clamp_to_facility_hours(_raw_end,   pickup_hours)
            if start_dt > end_dt:
                end_dt = start_dt
            start_str = start_dt.isoformat()
            end_str = end_dt.isoformat()
        except Exception as e:
            logger.warning(f"Clamping failed: {e}")
    else:
        logger.info("Path B: forward from pickup")
        if pickup_time_start and pickup_time_end and safe_pickup_date:
            start_str = (
                _parse_local_datetime(safe_pickup_date, pickup_time_start, pickup_tz) or
                datetime.fromisoformat(f"{safe_pickup_date}T00:00:00+00:00")
            ).isoformat()
            end_str = (
                _parse_local_datetime(safe_pickup_date, pickup_time_end, pickup_tz) or
                datetime.fromisoformat(f"{safe_pickup_date}T23:59:00+00:00")
            ).isoformat()
        else:
            start_str = f"{safe_pickup_date}T00:00:00+00:00"
            end_str = f"{safe_pickup_date}T23:59:00+00:00"
        try:
            _raw_start = datetime.fromisoformat(start_str)
            _raw_end   = datetime.fromisoformat(end_str)
            if pickup_fid:
                _fitted = fit_window_to_facility_hours(
                    _raw_start, _raw_end, load_time, pickup_stop,
                    direction="backward", base_url=base_url, headers=headers,
                    shipment_data=shipment_data, sw=sw, label="Pickup",
                )
                start_dt, end_dt = _fitted["start"], _fitted["end"]
                _warn_if_pickup_imminent(start_dt, shipment_data, sw)
            else:
                start_dt = _clamp_to_facility_hours(_raw_start, pickup_hours)
                end_dt   = _clamp_to_facility_hours(_raw_end,   pickup_hours)
            if start_dt > end_dt:
                end_dt = start_dt
            start_str = start_dt.isoformat()
            end_str = end_dt.isoformat()
        except Exception as e:
            logger.warning(f"Path B clamping failed: {e}")

    logger.info(f"Pickup window: {start_str} -> {end_str}")
    logger.info("=== calculate_pickup_window COMPLETED ===")
    return {"start_datetime": start_str, "end_datetime": end_str, "pickup_stop": pickup_stop, "load_time": load_time}


def update_pickup_stop_planned_window(
    shipment_data: Dict, pickup_stop: Dict, start_dt_str: str, end_dt_str: str,
    base_url: str, headers: Dict,
) -> None:
    """
    Update the pickup stop's planned_date and planned_time_window in local timezone via full shipment PUT.
    Mirrors updatePickupStopPlannedWindow() in AppointmentScheduler.gs.
    """
    logger.info("=== update_pickup_stop_planned_window START ===")
    tz_str = get_stop_timezone(pickup_stop)
    tz = ZoneInfo(tz_str)

    start_dt = round_down_to_quarter_hour(datetime.fromisoformat(start_dt_str).astimezone(tz))
    end_dt = round_down_to_quarter_hour(datetime.fromisoformat(end_dt_str).astimezone(tz))

    latest = _api_call(f"{base_url}/v2/shipments/{shipment_data['id']}/", "GET", headers=headers)
    stop_idx = next((i for i, s in enumerate(latest.get("stops") or []) if s.get("id") == pickup_stop.get("id")), None)
    if stop_idx is None:
        raise ValueError(f"Pickup stop {pickup_stop.get('id')} not found on shipment")

    latest["stops"][stop_idx]["planned_date"] = start_dt.strftime("%Y-%m-%d")
    latest["stops"][stop_idx]["planned_time_window_start"] = start_dt.strftime("%H:%M:%S")
    latest["stops"][stop_idx]["planned_time_window_end"] = end_dt.strftime("%H:%M:%S")

    if DRY_RUN:
        logger.info(f"[DRY_RUN] Would PUT shipment {shipment_data['id']} with pickup window {start_dt} -> {end_dt}")
        return

    _api_call(f"{base_url}/v2/shipments/{shipment_data['id']}/", "PUT", headers=headers, body=latest)
    logger.info("Pickup stop planned window updated")
    logger.info("=== update_pickup_stop_planned_window COMPLETED ===")


def find_available_slot(
    availability_data: Dict, requested_start: str, original_requested_date: str,
    appointment_duration_minutes: Optional[int] = None,
) -> Optional[Dict]:
    """
    Find the best available appointment slot at or before the requested pickup start time.

    Rather than stepping by a fixed increment, scans all available windows and picks
    the one whose start time is closest to (but not after) requested_start.
    Falls back up to 3 hours before requested_start.

    Prefers BY_APPOINTMENT windows (two-pass).
    Returns schedule body dict, {'needs_new_availability': True, ...} on midnight boundary, or None.
    """
    available_windows = availability_data.get("available_windows") or []
    logger.info(f"find_available_slot: {len(available_windows)} windows, requested_start={requested_start}")

    duration = timedelta(minutes=appointment_duration_minutes or APPOINTMENT_DURATION_MINUTES)
    max_lookback = timedelta(hours=3)

    try:
        requested_dt = datetime.fromisoformat(requested_start.replace("Z", "+00:00"))
        original_dt = datetime.fromisoformat(original_requested_date.replace("Z", "+00:00"))
    except Exception as e:
        logger.error(f"find_available_slot: bad datetime: {e}")
        return None

    original_date_only = original_dt.date()
    earliest_allowed = requested_dt - max_lookback

    for pass_windows in [
        [w for w in available_windows if w.get("appointment_type") == "BY_APPOINTMENT"],
        available_windows,
    ]:
        best_slot = None
        best_start = None

        for w in pass_windows:
            try:
                w_start = datetime.fromisoformat(w["start"]["timestamp"].replace("Z", "+00:00"))
                w_end = datetime.fromisoformat(w["end"]["timestamp"].replace("Z", "+00:00"))
            except Exception:
                continue

            if w_end - w_start < duration:
                continue

            if w_start <= requested_dt and requested_dt + duration <= w_end:
                candidate_start = requested_dt
            else:
                candidate_start = w_start

            candidate_end = candidate_start + duration

            if candidate_start < earliest_allowed or candidate_end > w_end:
                continue

            if candidate_start > requested_dt:
                continue

            # Compare dates in UTC to avoid false midnight-boundary detection when
            # Tempus returns slot timestamps in a local timezone (e.g. MDT = UTC-6)
            # that crosses a calendar date boundary vs. the UTC query window.
            candidate_start_utc_date = candidate_start.astimezone(timezone.utc).date()
            if candidate_start_utc_date < original_date_only:
                logger.warning("Midnight boundary crossed - need new availability for previous day")
                return {
                    "needs_new_availability": True,
                    "new_start_datetime": candidate_start.isoformat(),
                    "new_date": candidate_start_utc_date.isoformat(),
                }

            if best_start is None or candidate_start > best_start:
                best_start = candidate_start
                best_slot = {
                    "start": {"timestamp": candidate_start.isoformat(), "timezone": w["start"].get("timezone", "UTC")},
                    "end": {"timestamp": candidate_end.isoformat(), "timezone": w["end"].get("timezone", "UTC")},
                    "dock_id": w.get("dock_id"),
                }

        if best_slot:
            logger.info(f"Found best slot: {best_start.isoformat()} -> {(best_start + duration).isoformat()}")
            return best_slot

    logger.error("No available slot found within 3-hour lookback window")
    return None


def round_down_to_quarter_hour(dt: datetime) -> datetime:
    """Round a datetime down to the nearest 15-minute interval.

    Examples:
        10:42 -> 10:30
        10:47 -> 10:45
        10:00 -> 10:00 (already on quarter hour)
        10:14 -> 10:00
    """
    minutes_since_midnight = dt.hour * 60 + dt.minute
    rounded_minutes = (minutes_since_midnight // 15) * 15
    return dt.replace(hour=rounded_minutes // 60, minute=rounded_minutes % 60, second=0, microsecond=0)


def round_up_to_quarter_hour(dt: datetime) -> datetime:
    """Round a datetime up to the nearest 15-minute interval.

    Examples:
        10:42 -> 10:45
        10:47 -> 11:00
        10:00 -> 10:00 (already on quarter hour)
        10:01 -> 10:15
        23:59 -> 23:45 (capped at last quarter-hour of day to avoid midnight overflow)
    """
    minutes_since_midnight = dt.hour * 60 + dt.minute
    if dt.second > 0 or dt.microsecond > 0:
        minutes_since_midnight += 1  # treat any partial minute as a full minute
    rounded_minutes = math.ceil(minutes_since_midnight / 15) * 15
    # Cap at 23:45 to prevent overflow past midnight (e.g. 23:59 -> 24:00 -> 00:00).
    # Any time in [23:46, 23:59] rounds down to 23:45 rather than wrapping to next day.
    max_minutes = 23 * 60 + 45  # 23:45
    if rounded_minutes >= 24 * 60:
        rounded_minutes = max_minutes
    total_minutes = rounded_minutes
    return dt.replace(hour=total_minutes // 60, minute=total_minutes % 60, second=0, microsecond=0)


def round_to_nearest_quarter_hour(dt: datetime) -> datetime:
    """Round a datetime to the nearest 15-minute interval (up or down).

    Used for planning window END so we don't unnecessarily shrink the
    latest valid appointment start by always flooring.

    Examples:
        10:52 -> 11:00
        10:42 -> 10:45
        10:07 -> 10:00
        10:50 -> 10:45  (equidistant rounds down)
    """
    minutes_since_midnight = dt.hour * 60 + dt.minute + dt.second / 60
    rounded_minutes = round(minutes_since_midnight / 15) * 15
    # Clamp to valid day range
    rounded_minutes = max(0, min(rounded_minutes, 23 * 60 + 59))
    return dt.replace(hour=int(rounded_minutes) // 60, minute=int(rounded_minutes) % 60, second=0, microsecond=0)



def is_slot_at_capacity(
    facility_id: str, dock_id: str, start_dt: datetime, end_dt: datetime,
    matched_load_type_id: Optional[str], base_url: str, headers: Dict,
) -> bool:
    """
    Check if an appointment slot at a specific dock is already at capacity.

    Counts all overlapping SCHEDULED appointments at the dock (regardless of
    load type stored on the record — Tempus does not persist matched_load_type_id
    on the appointment object, so filtering by it always returns zero and defeats
    the capacity check).  The dock rule's concurrent_loads_allowed is looked up
    for the given matched_load_type_id to determine the limit.

    Returns True if slot is full, False if slot has capacity.
    """
    try:
        if not matched_load_type_id:
            # Can't look up dock rule without knowing load type — assume available
            logger.debug("is_slot_at_capacity: no matched_load_type_id provided — assuming available")
            return False

        start_str = start_dt.isoformat()
        end_str = end_dt.isoformat()

        # Fetch all SCHEDULED appointments at this facility.
        # NOTE: The /facilities/appointments endpoint does NOT support dock_id as a
        # query parameter (returns 400). Filter dock_id client-side instead.
        appt_url = (
            f"{base_url}/facilities/appointments"
            f"?facility_id={facility_id}"
            f"&status=SCHEDULED"
            f"&page=1&limit=100"
        )
        existing_resp = _api_call(appt_url, "GET", headers=headers)
        all_appointments = existing_resp.get("data") or []

        # Count SCHEDULED appointments on our specific dock whose time window overlaps
        # [start_dt, end_dt). Filter dock_id client-side since the API param is unsupported.
        # NOTE: do NOT filter by matched_load_type_id on the appointment record —
        # Tempus does not persist that field on the stored appointment object, so
        # the filter would always produce zero matches and silently disable this check.
        overlapping_count = 0
        for appt in all_appointments:
            if appt.get("dock_id") != dock_id:
                continue
            if appt.get("status") != "SCHEDULED":
                continue
            try:
                appt_start = datetime.fromisoformat(appt["start"]["timestamp"].replace("Z", "+00:00"))
                appt_end = datetime.fromisoformat(appt["end"]["timestamp"].replace("Z", "+00:00"))
                # Standard interval overlap: appt_start < our_end AND appt_end > our_start
                if appt_start < end_dt and appt_end > start_dt:
                    overlapping_count += 1
            except Exception as e:
                logger.debug(f"is_slot_at_capacity: failed to parse appointment times: {e}")
                continue

        # Look up max_concurrent_appointments from the dock rules endpoint for this load type.
        # NOTE: the dock detail endpoint (/docks/{id}) does NOT include rules — use /docks/{id}/rules.
        # The field on each rule is max_concurrent_appointments (not concurrent_loads_allowed).
        rules_url = f"{base_url}/facilities/{facility_id}/docks/{dock_id}/rules"
        rules_resp = _api_call(rules_url, "GET", headers=headers)
        rules = (rules_resp.get("data") or []) if isinstance(rules_resp, dict) else []
        matching_rule = next(
            (r for r in rules if r.get("load_type_id") == matched_load_type_id), None
        )

        if not matching_rule:
            logger.warning(
                f"is_slot_at_capacity: no dock rule for load_type_id={matched_load_type_id} "
                f"on dock {dock_id} at facility {facility_id} — assuming capacity=1"
            )
            concurrent_loads_allowed = 1
        else:
            concurrent_loads_allowed = matching_rule.get("max_concurrent_appointments", 1)

        is_full = overlapping_count >= concurrent_loads_allowed
        logger.info(
            f"is_slot_at_capacity: dock={dock_id} overlapping={overlapping_count} "
            f"limit={concurrent_loads_allowed} full={is_full} "
            f"({start_str} – {end_str})"
        )
        if is_full:
            logger.warning(
                f"is_slot_at_capacity: FULL — {overlapping_count}/{concurrent_loads_allowed} "
                f"appointments at dock {dock_id} overlap {start_str} – {end_str}"
            )
        return is_full

    except Exception as e:
        logger.warning(f"is_slot_at_capacity: capacity check failed (non-fatal): {e}")
        # Fail open — don't block a booking if the check itself errors
        return False


def find_first_available_slot(
    availability_data: Dict,
    base_url: str = None,
    headers: Dict = None,
    facility_id: str = None,
    planning_window_end: Optional[datetime] = None,
    planning_window_start: Optional[datetime] = None,
    prefer_latest: bool = False,
    excluded_slot: Optional[Dict] = None,
    matched_load_type_id: Optional[str] = None,
    exact_time: Optional[datetime] = None,
    rule_windows: Optional[List[Tuple[datetime, datetime]]] = None,
    appointment_duration_minutes: Optional[int] = None,
) -> Optional[Dict]:
    """
    excluded_slot: optional {"dock_id": str, "start": datetime, "end": datetime}.
    When provided, any candidate that falls on this exact dock+time is skipped,
    even if the capacity check says it is available.  Used in the race-guard retry
    to avoid re-booking the same slot that was just cancelled.
    """
    """Return the best available appointment slot within the pickup planning window.

    ``prefer_latest=True`` should be set ONLY when the 3-hour pickup planning
    window was derived from a point-in-time delivery (delivery start == end).
    In that case we prefer the LATEST slot first — closest to pickup_end (the
    latest-load-start time) — and walk backwards toward pickup_start if earlier
    slots are full.  This mirrors the Google Sheets logic:
    "start with pickup_end, go backwards to pickup_start."

    When ``prefer_latest=False`` (the default) the function returns the earliest
    available slot, which is correct for all other scheduling paths.

    ``planning_window_start`` / ``planning_window_end`` bound the search window
    regardless of ``prefer_latest``.  Slots outside those bounds are excluded.

    Algorithm:
      For each available_window wide enough to hold APPOINTMENT_DURATION_MINUTES:
        - If prefer_latest: anchor slot at round_down(min(w_end-duration, planning_window_end)).
        - Otherwise: anchor slot at round_down(w_start) (earliest).
      Candidates outside [planning_window_start, planning_window_end] are excluded.
      Full slots (capacity check) are skipped — other docks at the same time are
      still considered before moving to an earlier time.
      Returns the latest candidate when prefer_latest, otherwise the earliest.

    Appointment times are rounded down to the nearest 15-minute interval.

    If base_url and headers are provided, checks capacity to prevent double-booking.
    Skips slots that are already at their concurrent_loads_allowed limit.
    """
    duration = timedelta(minutes=appointment_duration_minutes or APPOINTMENT_DURATION_MINUTES)
    # facility_id: prefer explicit param, fall back to response body
    if not facility_id:
        facility_id = (availability_data.get("load_type_dock_rule_match_results") or {}).get("facility_id")
    # matched_load_type_id can be passed explicitly (e.g. retry path) or extracted from the response
    if not matched_load_type_id:
        if "matched_load_type_id" in availability_data:
            matched_load_type_id = availability_data["matched_load_type_id"]
        elif "load_type_dock_rule_match_results" in availability_data:
            results = availability_data["load_type_dock_rule_match_results"]
            if isinstance(results, dict):
                matched_load_type_id = results.get("matched_load_type_id")

    if not facility_id:
        logger.warning("find_first_available_slot: facility_id not available — capacity check will be skipped")

    # Build the set of docks confirmed by Tempus to support this load type.
    # The availability response includes load_type_dock_rule_match_results.matched_docks
    # which lists every dock that has a rule matching the requested load type.
    # Any available_window whose dock_id is NOT in this set is for an incompatible
    # dock and must be skipped — attempting to book it would fail at the Tempus level.
    # If matched_docks is absent (older API versions), fall back to allowing all docks.
    match_results = availability_data.get("load_type_dock_rule_match_results") or {}
    raw_matched_docks = match_results.get("matched_docks") or []
    if raw_matched_docks:
        supported_dock_ids = {d["dock_id"] for d in raw_matched_docks if d.get("dock_id")}
        logger.info(
            f"find_first_available_slot: load-type-supported docks: {supported_dock_ids}"
        )
    else:
        supported_dock_ids = None  # no filter — allow all docks
        logger.info("find_first_available_slot: no matched_docks in response — skipping dock load-type filter")

    # Build a flat list of all valid 15-minute slot starts across every available
    # dock window, bounded by [planning_window_start, planning_window_end].
    # Each entry is (candidate_start, window_dict).
    # Walking all slots (rather than one anchor per window) ensures we don't miss
    # an open slot on a different dock or at an earlier time in the same window.
    candidates = []
    step = timedelta(minutes=15)
    for window in availability_data.get("available_windows") or []:
        dock_id = window.get("dock_id")
        # Skip docks that Tempus has not matched to this load type.
        if supported_dock_ids is not None and dock_id not in supported_dock_ids:
            logger.info(
                f"find_first_available_slot: skipping dock {dock_id} — not in load-type-supported dock set"
            )
            continue
        try:
            w_start = datetime.fromisoformat(window["start"]["timestamp"].replace("Z", "+00:00"))
            w_end = datetime.fromisoformat(window["end"]["timestamp"].replace("Z", "+00:00"))
        except Exception:
            continue
        if w_end - w_start < duration:
            continue

        # First valid slot start: round down w_start to the nearest 15-min boundary,
        # but ensure we don't go before w_start.
        first_slot = round_down_to_quarter_hour(w_start)
        if first_slot < w_start:
            first_slot = first_slot + step

        # Clamp to planning window
        if planning_window_start and first_slot < planning_window_start:
            first_slot = round_down_to_quarter_hour(planning_window_start)
            if first_slot < planning_window_start:
                first_slot += step

        # Last valid slot start: must leave room for appointment duration, and must
        # not exceed planning_window_end when set.
        # Special case: if planning_window_end falls inside the availability window
        # (w_start <= planning_window_end <= w_end), it is a valid slot start even
        # if w_end - duration < planning_window_end (Tempus clips window end to
        # facility close, so e.g. a 08:30 close returns w_end=08:30 and
        # w_end - 45min = 07:45, incorrectly excluding the 08:30 start slot).
        last_slot = w_end - duration
        if planning_window_end:
            # planning_window_end is the latest time a pickup appointment may START —
            # load time is already baked into the transit calculation, so a slot that
            # starts at planning_window_end is viable (truck loads, departs, still makes
            # the delivery). A slot may NOT start after planning_window_end.
            if last_slot > planning_window_end:
                last_slot = planning_window_end
        last_slot = round_down_to_quarter_hour(last_slot)

        if first_slot > last_slot:
            continue

        slot = first_slot
        while slot <= last_slot:
            candidates.append((slot, window))
            slot += step

    # De-duplicate: same start+dock may appear from overlapping windows; keep unique pairs.
    seen = set()
    deduped = []
    for (cs, w) in candidates:
        key = (cs, w.get("dock_id"))
        if key not in seen:
            seen.add(key)
            deduped.append((cs, w))
    candidates = deduped

    # Sort: latest-first when prefer_latest, earliest-first otherwise.
    # We then walk the sorted list and return the first slot that passes all checks.
    candidates.sort(key=lambda item: item[0], reverse=prefer_latest)

    filtered = []
    skipped_full: list = []  # (candidate_start, dock_id) pairs skipped due to capacity
    for candidate_start, window in candidates:
        dock_id = window.get("dock_id")

        # NOTE: Client-side capacity check removed — Tempus availability response is
        # the authoritative source of truth for slot availability. If Tempus says a
        # slot is open, we trust it. Our client-side check overcounted by fetching
        # all SCHEDULED appointments (including from other tests/load types) and
        # applying max_concurrent=1, incorrectly rejecting slots Tempus considered open.

        # Excluded-slot guard: hard-skip the race-guard cancelled slot.
        if excluded_slot:
            ex_dock = excluded_slot.get("dock_id")
            ex_start = excluded_slot.get("start")
            if ex_dock and ex_start and dock_id == ex_dock and candidate_start == ex_start:
                logger.info(
                    f"find_first_available_slot: skipping excluded slot "
                    f"{candidate_start.isoformat()} on dock {dock_id} (race-guard retry)"
                )
                continue

        # Exact-time filter.
        if exact_time is not None:
            slot_diff = abs((candidate_start - exact_time).total_seconds())
            if slot_diff > 60:
                logger.info(
                    f"find_first_available_slot: exact_time filter — skipping slot "
                    f"{candidate_start.isoformat()} (requested {exact_time.isoformat()})"
                )
                continue

        # Skip slots whose appointment span [start, start+duration] crosses a rule gap.
        if rule_windows:
            slot_end = candidate_start + duration
            in_window = any(
                rw_start <= candidate_start and slot_end <= rw_end
                for rw_start, rw_end in rule_windows
            )
            if not in_window:
                logger.debug(
                    f"find_first_available_slot: skipping {candidate_start.isoformat()} "
                    f"— slot end {slot_end.isoformat()} crosses rule gap"
                )
                continue

        filtered.append((candidate_start, window))

    candidates = filtered

    if not candidates:
        if exact_time is not None:
            logger.info(
                f"find_first_available_slot: no slot available at exact time {exact_time.isoformat()} "
                "— shipment will remain unscheduled"
            )
        return None, skipped_full

    # candidates is already sorted (latest-first or earliest-first) and capacity-filtered.
    # The first entry is the best slot.
    start_dt, window = candidates[0]
    end_dt = start_dt + duration
    logger.info(
        f"find_first_available_slot: selected slot {start_dt.isoformat()} -> {end_dt.isoformat()} "
        f"dock={window.get('dock_id')} ({'latest-first' if prefer_latest else 'earliest-first'})"
    )
    return {
        "start": {"timestamp": start_dt.isoformat(), "timezone": window["start"].get("timezone", "UTC")},
        "end": {"timestamp": end_dt.isoformat(), "timezone": window["end"].get("timezone", "UTC")},
        "dock_id": window.get("dock_id"),
    }, skipped_full


def get_stop_window_datetimes(stop: Dict) -> Optional[Tuple[str, str]]:
    """Return ISO start/end datetimes for a shipment stop's planned window."""
    location = stop.get("location") or {}
    timezone_str = location.get("timezone") or (location.get("address") or {}).get("timezone") or DEFAULT_SCHEDULING_TIMEZONE
    planned_date = stop.get("planned_date")
    start_time = stop.get("planned_time_window_start")
    end_time = stop.get("planned_time_window_end")
    start_dt = _parse_local_datetime(planned_date, start_time, timezone_str)
    end_dt = _parse_local_datetime(planned_date, end_time, timezone_str)
    if not start_dt or not end_dt:
        return None
    if end_dt < start_dt:
        end_dt = start_dt
    return start_dt.isoformat(), end_dt.isoformat()


def is_facility_stop(stop: Dict) -> bool:
    """Return True when a shipment stop has a facility id and can be dock scheduled."""
    return bool(get_stop_facility_id(stop))


def get_stop_facility_id(stop: Dict) -> Optional[str]:
    """Extract the Tempus facility_id from a shipment stop, checking all known paths.

    Checks in order:
      1. stop.location.facility_id
      2. stop.location.address.facility_id
      3. stop.address_book_entry.facility_id
    Returns None if not found.
    """
    loc = stop.get("location") or {}
    facility_id = loc.get("facility_id") or (loc.get("address") or {}).get("facility_id")
    if not facility_id:
        abe = stop.get("address_book_entry") or {}
        facility_id = abe.get("facility_id")
    return facility_id or None


def _get_matched_load_type_id(
    facility_id: str,
    shipment_data: Dict,
    base_url: str,
    headers: Dict,
    product_category: Optional[str] = None,
) -> Optional[str]:
    """Look up the matched_load_type_id for a shipment at a facility.

    Tempus uses this to enforce per-load-type dock rules (including concurrent_loads_allowed).
    Without it, double-booking can occur because Tempus won't know which dock rule to apply.

    Match priority:
      1. product_category (e.g. "Asphalt", "Blue Dot") -- HFS shipments use this, not equipment_type
      2. equipment_type.machine_readable from the shipment
      3. First load type in the list (last-resort fallback)

    Returns the best-matching load type id string, or None if unavailable.
    """
    try:
        lt_resp = _api_call(f"{base_url}/facilities/{facility_id}/load-types", "GET", headers=headers)
        lt_data = lt_resp.get("data") or []
        if not lt_data:
            return None

        # Priority 1: match on product_category (Asphalt, Blue Dot, etc.)
        # Match bidirectionally: load type name contains category word OR category contains load type name.
        # Example: product_category="ASPHALTS" should match load type named "Asphalt" because
        # "asphalt" is in "asphalts" (category contains name), even though "asphalts" is not in "asphalt".
        # Also check product_category list on the load type itself for exact/substring matches.
        if product_category:
            cat_lower = product_category.lower()
            def _category_matches_lt(lt: dict) -> bool:
                lt_name = (lt.get("name") or "").lower()
                lt_cats = [c.lower() for c in (lt.get("product_category") or [])]
                # Priority 1: structured product_category list — checked first so that
                # a specific category like "WAX FREE OILS" doesn't spuriously match
                # a shorter-named load type like "Wax" via name substring.
                if lt_cats:
                    return any(c == cat_lower or c in cat_lower or cat_lower in c for c in lt_cats)
                # Priority 2: name substring only when no product_category list populated
                return bool(lt_name and (cat_lower in lt_name or lt_name in cat_lower))
            matched = next((lt for lt in lt_data if _category_matches_lt(lt)), None)
            if matched:
                logger.debug(f"_get_matched_load_type_id: matched by product_category={product_category!r} -> {matched.get('id')}")
                return matched.get("id")

        # Priority 2: match on equipment_type machine_readable
        equipment = (shipment_data.get("equipment_type") or {}).get("machine_readable", "")
        if equipment:
            equipment_lower = equipment.lower()
            matched = next(
                (lt for lt in lt_data if equipment_lower in (lt.get("name") or "").lower()
                 or equipment_lower in (lt.get("machine_readable") or "").lower()),
                None,
            )
            if matched:
                logger.debug(f"_get_matched_load_type_id: matched by equipment_type={equipment!r} -> {matched.get('id')}")
                return matched.get("id")

        # Priority 3: first load type as last-resort fallback
        logger.debug(f"_get_matched_load_type_id: no match for product_category={product_category!r} equipment={equipment!r} — using first load type")
        return lt_data[0].get("id")
    except Exception as e:
        logger.debug(f"_get_matched_load_type_id: could not resolve load type for facility {facility_id}: {e}")
        return None


# Public alias — for use by order-update and other consumers.
get_matched_load_type_id = _get_matched_load_type_id


def _get_dock_rule_windows(
    facility_id: str,
    matched_load_type_id: Optional[str],
    base_url: str,
    headers: Dict,
    target_date: datetime,
    tz_str: str = "America/Denver",
) -> Optional[List[Tuple[datetime, datetime]]]:
    """
    Fetch dock rules for the facility and return sorted (window_start, window_end)
    timezone-aware datetime pairs for target_date.

    Rules are filtered by matched_load_type_id when provided.
    Gaps between returned windows are "down" periods — appointments must not span them.
    Returns None on any error (caller falls back to no clipping).
    """
    try:
        tz = ZoneInfo(tz_str)
        docks_resp = _api_call(f"{base_url}/facilities/{facility_id}/docks?limit=20", "GET", headers=headers)
        docks = (docks_resp or {}).get("data") or []
        windows = []
        for dock in docks:
            dock_id = dock.get("id")
            if not dock_id:
                continue
            rules_resp = _api_call(f"{base_url}/facilities/{facility_id}/docks/{dock_id}/rules?limit=20", "GET", headers=headers)
            rules = (rules_resp or {}).get("data") or []
            for rule in rules:
                if matched_load_type_id and rule.get("load_type_id") != matched_load_type_id:
                    continue
                start_str = rule.get("first_appointment_start_time") or "00:00:00"
                end_str = rule.get("last_appointment_end_time") or "23:59:00"
                try:
                    h, m, s = (int(x) for x in start_str.split(":"))
                    rule_start = datetime(target_date.year, target_date.month, target_date.day, h, m, s, tzinfo=tz)
                    h, m, s = (int(x) for x in end_str.split(":"))
                    rule_end = datetime(target_date.year, target_date.month, target_date.day, h, m, s, tzinfo=tz)
                    if rule_end > rule_start:
                        windows.append((rule_start, rule_end))
                except Exception:
                    continue
        # Merge overlapping windows, sort by start
        windows.sort(key=lambda x: x[0])
        merged: List[Tuple[datetime, datetime]] = []
        for ws, we in windows:
            if merged and ws <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], we))
            else:
                merged.append((ws, we))
        return merged
    except Exception as e:
        logger.debug(f"_get_dock_rule_windows: failed to fetch rules for {facility_id}: {e}")
        return None


def schedule_appointment_for_stop(
    shipment_data: Dict, stop_data: Dict, start_dt: str, end_dt: str,
    base_url: str, headers: Dict, sw: "ShipwellProgram",
    product_category: Optional[str] = None,
    is_point_in_time: bool = False,
    preserve_pickup_window: bool = False,
    force_prefer_latest: bool = False,
) -> bool:
    """Schedule the latest available appointment for a specific facility stop.

    - Sends delivery_type (SHIPPING for pickup, RECEIVING for delivery) so Tempus
      can match the correct dock rule.
    - Passes matched_load_type_id to enforce per-load-type concurrent_loads_allowed.
      If the API rejects that field (400), retries without it for compatibility.
    - product_category (e.g. "Asphalt") used to resolve matched_load_type_id for HFS
      shipments which don't set equipment_type.
    """
    facility_id = (stop_data.get("location") or {}).get("facility_id")
    if not facility_id:
        return True

    # Determine delivery_type: SHIPPING = outbound/pickup, RECEIVING = inbound/delivery
    is_pickup = bool(stop_data.get("is_pickup") or (stop_data.get("stop_type") or "").upper() == "PICKUP")
    delivery_type = "SHIPPING" if is_pickup else "RECEIVING"

    appt_check_url = (
        f"{base_url}/facilities/appointments?page=1&limit=10"
        f"&facility_id={facility_id}&scheduled_resource_id={shipment_data['id']}&stop_id={stop_data['id']}"
    )
    existing = _api_call(appt_check_url, "GET", headers=headers)
    existing_data = existing.get("data") or []
    if existing_data and existing_data[0].get("status") == "SCHEDULED":
        sw.log("TRACE", f"Appointment already scheduled for stop {stop_data.get('id')}", ["schedule_appointment_for_stop"])
        return True

    # Include matched_load_type_id so Tempus enforces dock concurrent_loads_allowed.
    # If the API rejects the field (400), retry without it — some Tempus versions may
    # not accept matched_load_type_id on the availability endpoint.
    matched_load_type_id = _get_matched_load_type_id(facility_id, shipment_data, base_url, headers, product_category=product_category)

    # Resolve appointment duration from matched load type (minutes).
    # Used in slot-finding so the schedule body end = start + load_type.appointment_duration.
    _appt_duration_minutes: Optional[int] = None
    if matched_load_type_id and facility_id:
        try:
            _lt_resp = _api_call(f"{base_url}/facilities/{facility_id}/load-types", "GET", headers=headers)
            _lt_match = next((lt for lt in (_lt_resp.get("data") or []) if lt.get("id") == matched_load_type_id), None)
            if _lt_match and _lt_match.get("appointment_duration"):
                _parsed_dur = parse_iso8601_duration(_lt_match["appointment_duration"])
                if _parsed_dur and _parsed_dur > 0:
                    _appt_duration_minutes = int(round(_parsed_dur * 60))
                    logger.info(
                        f"schedule_appointment_for_stop: load type {matched_load_type_id} "
                        f"appointment_duration={_lt_match['appointment_duration']} → {_appt_duration_minutes}min"
                    )
        except Exception as _dur_err:
            logger.warning(f"schedule_appointment_for_stop: could not resolve load type duration: {_dur_err}")

    # Tempus requires start < end strictly. If the order has a single point-in-time
    # window (start == end), expand end by 1 hour so the availability call succeeds.
    _start_obj = datetime.fromisoformat(start_dt.replace("Z", "+00:00")) if isinstance(start_dt, str) else start_dt
    _end_obj = datetime.fromisoformat(end_dt.replace("Z", "+00:00")) if isinstance(end_dt, str) else end_dt
    # Round start DOWN to the nearest 15-min boundary for slot selection clamping,
    # but KEEP the original unrounded start_dt for the Tempus availability request.
    # Tempus creates the unscheduled appointment record as a side effect of the
    # availability call and uses the exact start_datetime as a key — a clean
    # quarter-hour timestamp (e.g. 03:30:00) does NOT trigger record creation,
    # whereas the raw microsecond value (e.g. 03:42:30.672) does.
    _tz_for_round = ZoneInfo((stop_data.get("location") or {}).get("timezone") or DEFAULT_SCHEDULING_TIMEZONE)
    _start_obj_rounded = round_down_to_quarter_hour(_start_obj.astimezone(_tz_for_round))
    # start_dt (unrounded) is passed to Tempus in the availability body.
    # _start_obj_rounded is used as planning_window_start so find_first_available_slot
    # returns slots starting at the rounded boundary (e.g. 03:30) not 03:45.
    # Capture the original planning window bounds BEFORE any Tempus-required expansion.
    # _pw_start/_pw_end are used after booking to write back the correct planned window
    # and must reflect the order's intent, not the expanded availability query range.
    _original_end_obj = _end_obj
    if _start_obj >= _end_obj:
        _end_obj = _start_obj + timedelta(hours=1)
        end_dt = _end_obj.isoformat()
        logger.info(f"schedule_appointment_for_stop: point-in-time window detected — expanded end by 1h to {end_dt}")

    # Fetch dock rule windows so we can avoid booking slots that span rule gaps.
    _tz_str = (stop_data.get("location") or {}).get("timezone") or DEFAULT_SCHEDULING_TIMEZONE
    _rule_windows = _get_dock_rule_windows(
        facility_id, matched_load_type_id, base_url, headers,
        target_date=_start_obj, tz_str=_tz_str,
    ) if facility_id else None
    # If the planning window crosses midnight (end on a different calendar date than start),
    # also fetch rule windows for the next day and append them so the anchor logic and
    # cross-break overflow can find slots in the early-morning part of the window.
    if _rule_windows is not None and _end_obj.date() > _start_obj.date():
        _next_day_windows = _get_dock_rule_windows(
            facility_id, matched_load_type_id, base_url, headers,
            target_date=_end_obj, tz_str=_tz_str,
        )
        if _next_day_windows:
            _rule_windows = sorted(_rule_windows + _next_day_windows, key=lambda x: x[0])
            logger.info(
                f"schedule_appointment_for_stop: window crosses midnight — merged next-day rule windows: "
                f"{[(s.isoformat(), e.isoformat()) for s, e in _rule_windows]}"
            )
    if _rule_windows is not None:
        logger.info(f"schedule_appointment_for_stop: dock rule windows for {facility_id}: {[(s.isoformat(), e.isoformat()) for s, e in _rule_windows]}")
    else:
        logger.info("schedule_appointment_for_stop: dock rule windows unavailable — proceeding without rule-gap filtering")

    # The planned window end (e.g. 08:30) is the latest time a slot may *start*, not
    # the latest time it may *finish*. Extend the availability query end by the
    # appointment duration so Tempus returns windows that include slots starting up to
    # end_dt. find_first_available_slot still clamps slot starts to planning_window_end,
    # so no slot will be booked that starts after the planned window end.
    _avail_end_obj = _end_obj + timedelta(minutes=_appt_duration_minutes or APPOINTMENT_DURATION_MINUTES)

    # For PIT-delivery orders (force_prefer_latest=True), slot search runs latest-first
    # starting from _end_obj and working backward. The availability query must be
    # anchored to the rule window containing _end_obj (e.g. 07:30–15:00 when
    # _end_obj=08:42), NOT the rule window containing _start_obj (e.g. 00:00–07:00).
    # For forward (earliest-first) orders, anchor to _start_obj as before.
    _rule_anchor = _end_obj if force_prefer_latest else _start_obj

    # If we have rule windows, clip the availability query to avoid crossing
    # a rule boundary — Tempus returns 0 slots if the query spans a gap.
    # For PIT-delivery (latest-first): clip avail_start UP to the containing rule
    # window's start (so the query falls entirely inside the window around _end_obj).
    # For forward (earliest-first): clip avail_end DOWN to the containing rule
    # window's end (so the query falls entirely inside the window around _start_obj).
    _anchor_rule_start: Optional[datetime] = None
    _anchor_rule_end: Optional[datetime] = None
    if _rule_windows:
        for rw_start, rw_end in _rule_windows:
            if rw_start <= _rule_anchor <= rw_end:
                _anchor_rule_start = rw_start
                _anchor_rule_end = rw_end
                break
        # If _rule_anchor falls in a gap between rule windows (no containing window found),
        # set _anchor_rule_end to the start of the next rule window after the anchor.
        # This prevents the forward overflow from including rule windows that are entirely
        # *before* the gap — which would book appointments at times earlier than _start_obj.
        if _anchor_rule_end is None:
            for rw_start, rw_end in _rule_windows:
                if rw_start > _rule_anchor:
                    _anchor_rule_end = rw_start
                    logger.info(
                        f"schedule_appointment_for_stop: _rule_anchor {_rule_anchor.isoformat()} "
                        f"falls in a gap between rule windows — setting _anchor_rule_end to "
                        f"next rule window start {_anchor_rule_end.isoformat()} to prevent "
                        f"backward overflow into pre-gap slots"
                    )
                    break

    if force_prefer_latest and _anchor_rule_start is not None:
        # PIT-delivery: query from the rule window start (or _start_obj if later)
        # through _avail_end_obj clipped to rule window end.
        _avail_start_obj = max(_start_obj, _anchor_rule_start)
        if _anchor_rule_end and _avail_end_obj > _anchor_rule_end:
            logger.info(
                f"schedule_appointment_for_stop: PIT-delivery — clipping avail_end from "
                f"{_avail_end_obj.isoformat()} to rule boundary {_anchor_rule_end.isoformat()}"
            )
            _avail_end_obj = _anchor_rule_end
        if _avail_start_obj != _start_obj:
            logger.info(
                f"schedule_appointment_for_stop: PIT-delivery — raising avail_start from "
                f"{start_dt} to rule window start {_avail_start_obj.isoformat()}"
            )
        avail_start_dt = _avail_start_obj.isoformat()
    else:
        # Forward (earliest-first): clip end to containing rule window as before.
        if _anchor_rule_end and _avail_end_obj > _anchor_rule_end:
            logger.info(
                f"schedule_appointment_for_stop: clipping avail_end from {_avail_end_obj.isoformat()} "
                f"to rule boundary {_anchor_rule_end.isoformat()}"
            )
            _avail_end_obj = _anchor_rule_end
        # Use the rounded start for the availability query so Tempus receives a clean
        # quarter-hour boundary (e.g. 10:45:00) rather than a raw sub-second timestamp
        # (e.g. 10:45:23.456). Tempus rounds up to the next slot boundary on non-aligned
        # timestamps, causing it to return w_start=11:00 instead of 10:45 and the first
        # bookable slot to land 15 minutes later than intended.
        avail_start_dt = _start_obj_rounded.isoformat()

    avail_end_dt = _avail_end_obj.isoformat()
    logger.info(
        f"schedule_appointment_for_stop: extended availability query end by {APPOINTMENT_DURATION_MINUTES}m "
        f"({end_dt} → {avail_end_dt}) so slots starting at window end are included"
    )

    def _make_avail_body(q_start: str, q_end: str) -> Dict:
        body: Dict = {
            "request_criteria_type": "SHIPMENT",
            "shipment_id": shipment_data["id"],
            "stop_id": stop_data["id"],
            "start_datetime": q_start,
            "end_datetime": q_end,
            "delivery_type": delivery_type,
        }
        if matched_load_type_id:
            body["matched_load_type_id"] = matched_load_type_id
        return body

    avail_body: Dict = _make_avail_body(avail_start_dt, avail_end_dt)

    logger.info(f"schedule_appointment_for_stop: availability request body: {json.dumps(avail_body)}")
    try:
        availability_data = _api_call(
            f"{base_url}/facilities/{facility_id}/availability",
            "POST", headers=headers,
            body=avail_body,
        )
    except Exception as avail_err:
        err_str = str(avail_err)
        if "400" in err_str or "Bad Request" in err_str:
            # Retry progressively stripping optional fields to isolate which one Tempus rejects
            logger.warning(f"Availability 400 — retrying without optional fields: {avail_err}")
            for strip_keys in (
                ["matched_load_type_id"],
                ["matched_load_type_id", "delivery_type"],
            ):
                stripped_body = {k: v for k, v in avail_body.items() if k not in strip_keys}
                logger.info(f"schedule_appointment_for_stop: availability retry (stripped {strip_keys}): {json.dumps(stripped_body)}")
                try:
                    availability_data = _api_call(
                        f"{base_url}/facilities/{facility_id}/availability",
                        "POST", headers=headers,
                        body=stripped_body,
                    )
                    logger.info(f"Availability call succeeded after stripping {strip_keys}")
                    break
                except Exception as retry_err:
                    if "400" in str(retry_err) or "Bad Request" in str(retry_err):
                        logger.warning(f"Still 400 after stripping {strip_keys}: {retry_err}")
                        continue
                    raise
            else:
                raise avail_err  # all retries failed
        else:
            raise
    # Resolve planning window bounds from the stop's planned window.
    _pw_end: Optional[datetime] = None
    _pw_start: Optional[datetime] = None
    # Prefer start_dt/end_dt (the raw calculated datetimes passed in) as planning window bounds.
    # The stop's planned_time_window_start/end are lossy for multi-day windows — they only store
    # a single date + time pair, so a 24h span (e.g. Aug 14 22:30 → Aug 15 22:30) collapses to
    # the same time on the start date (22:30–22:30), making find_first_available_slot see a
    # zero-width window and find no slots. Always use the raw datetimes when available.
    try:
        _pw_start = _start_obj_rounded  # use rounded start so slot selection begins at quarter-hour boundary
        _pw_end = _original_end_obj  # use original end BEFORE any 1h Tempus expansion
    except Exception:
        # Fallback to stop's planned window if start_dt/end_dt are unusable.
        _pw_times = get_stop_window_datetimes(stop_data)
        if _pw_times:
            try:
                _pw_start = datetime.fromisoformat(_pw_times[0].replace("Z", "+00:00"))
                _pw_end = datetime.fromisoformat(_pw_times[1].replace("Z", "+00:00"))
            except Exception:
                pass

    # For point-in-time orders the appointment must be booked at the exact requested time.
    # No fallback to other slots — if the exact time is unavailable, leave unscheduled.
    _is_point_in_time = is_point_in_time
    if _is_point_in_time:
        _exact_time = datetime.fromisoformat(start_dt.replace("Z", "+00:00")) if isinstance(start_dt, str) else start_dt
        logger.info(
            f"schedule_appointment_for_stop: point-in-time order — only booking at exact time {_exact_time.isoformat()}"
        )
        schedule_body, _skipped = find_first_available_slot(
            availability_data, base_url, headers, facility_id=facility_id,
            planning_window_end=_pw_end,
            planning_window_start=_pw_start,
            prefer_latest=False,
            exact_time=_exact_time,
            rule_windows=_rule_windows,
            appointment_duration_minutes=_appt_duration_minutes,
        )
    else:
        # Non-PIT orders: use earliest-first unless force_prefer_latest is set
        # (e.g. 3h pickup window derived from a point-in-time delivery).
        _prefer_latest = force_prefer_latest
        if force_prefer_latest:
            logger.info("schedule_appointment_for_stop: PIT-delivery derived window — using latest-first slot selection (force_prefer_latest)")
        else:
            logger.info("schedule_appointment_for_stop: non-PIT order — using earliest-first slot selection")
        schedule_body, _skipped = find_first_available_slot(
            availability_data, base_url, headers, facility_id=facility_id,
            planning_window_end=_pw_end,
            planning_window_start=_pw_start,
            prefer_latest=_prefer_latest,
            rule_windows=_rule_windows,
            appointment_duration_minutes=_appt_duration_minutes,
        )
    if _skipped:
        _tz_str = get_stop_timezone(stop_data)
        _tz = ZoneInfo(_tz_str)
        _skipped_strs = ", ".join(
            datetime.fromisoformat(s.isoformat()).astimezone(_tz).strftime("%H:%M")
            if hasattr(s, "isoformat") else str(s)
            for (s, _d) in _skipped
        )
        _skip_msg = (
            f"Slot search: {len(_skipped)} slot(s) full — backed up through: {_skipped_strs}. "
            f"{'Booked earlier slot.' if schedule_body else 'No open slot found in planning window.'}"
        )
        logger.info(_skip_msg)
        sw.log("TRACE", _skip_msg, ["schedule_appointment_for_stop", "slot_search"])
    # Cross-break overflow: if the primary availability query found no open slot,
    # walk through the remaining rule windows that still overlap the planning window
    # and retry availability + slot search in each.
    #
    # Direction logic:
    #   PIT-delivery (force_prefer_latest=True): primary query covered the rule window
    #   containing _end_obj (latest end). Overflow walks BACKWARD through earlier rule
    #   windows (those whose end <= _anchor_rule_start) so the truck can still make
    #   the delivery — skipping the 07:00–07:30 break automatically.
    #
    #   Forward (earliest-first): primary query covered the rule window containing
    #   _start_obj. Overflow walks FORWARD through later rule windows (those whose
    #   start >= _anchor_rule_end) to pick up the next available slot after the break.
    #
    # In both cases we stay within [_start_obj, _end_obj] (the original planning window).
    if not schedule_body and not _is_point_in_time and _rule_windows and not DRY_RUN:
        if force_prefer_latest:
            # Overflow: earlier rule windows, latest-first within each
            overflow_windows = [
                (rw_start, rw_end) for rw_start, rw_end in reversed(_rule_windows)
                if _anchor_rule_start is None or rw_end <= _anchor_rule_start
                if rw_end > _start_obj  # must overlap the planning window
            ]
        else:
            # Overflow: later rule windows, earliest-first within each
            overflow_windows = [
                (rw_start, rw_end) for rw_start, rw_end in _rule_windows
                if _anchor_rule_end is None or rw_start >= _anchor_rule_end
                if rw_start < _end_obj  # must overlap the planning window
            ]

        for ow_start, ow_end in overflow_windows:
            if schedule_body:
                break
            # Clamp query to planning window
            q_start_obj = max(ow_start, _start_obj)
            q_end_obj = min(ow_end, _end_obj + timedelta(minutes=_appt_duration_minutes or APPOINTMENT_DURATION_MINUTES))
            if q_end_obj <= q_start_obj:
                continue
            q_start = q_start_obj.isoformat()
            q_end = q_end_obj.isoformat()
            overflow_body = _make_avail_body(q_start, q_end)
            logger.info(
                f"schedule_appointment_for_stop: cross-break overflow — "
                f"trying {'earlier' if force_prefer_latest else 'later'} rule window "
                f"{ow_start.isoformat()}–{ow_end.isoformat()} "
                f"(query {q_start}–{q_end})"
            )
            try:
                overflow_avail = _api_call(
                    f"{base_url}/facilities/{facility_id}/availability",
                    "POST", headers=headers,
                    body=overflow_body,
                )
            except Exception as _ov_err:
                logger.warning(f"schedule_appointment_for_stop: cross-break overflow query failed: {_ov_err}")
                continue
            # Determine rule windows for this overflow window (same list, just filter)
            ov_rule_windows = [(rw_s, rw_e) for rw_s, rw_e in (_rule_windows or []) if rw_s <= ow_end and rw_e >= ow_start]
            schedule_body, _ov_skipped = find_first_available_slot(
                overflow_avail, base_url, headers, facility_id=facility_id,
                planning_window_end=_pw_end,
                planning_window_start=_pw_start,
                prefer_latest=force_prefer_latest,
                rule_windows=ov_rule_windows,
            )
            if schedule_body:
                logger.info(
                    f"schedule_appointment_for_stop: cross-break overflow — found slot in "
                    f"{'earlier' if force_prefer_latest else 'later'} rule window "
                    f"{ow_start.isoformat()}–{ow_end.isoformat()}"
                )
                sw.log(
                    "TRACE",
                    f"Cross-break overflow: booked slot in "
                    f"{'earlier' if force_prefer_latest else 'later'} rule window after primary was full",
                    ["schedule_appointment_for_stop", "cross_break_overflow"],
                )
            if _ov_skipped:
                _tz_ov = ZoneInfo(get_stop_timezone(stop_data))
                _ov_skipped_strs = ", ".join(
                    datetime.fromisoformat(s.isoformat()).astimezone(_tz_ov).strftime("%H:%M")
                    if hasattr(s, "isoformat") else str(s)
                    for (s, _d) in _ov_skipped
                )
                logger.info(f"schedule_appointment_for_stop: cross-break overflow skipped full slots: {_ov_skipped_strs}")

    if not schedule_body:
        stop_type = "Pickup" if stop_data.get("is_pickup") else "Delivery"
        subject = f"No Available {stop_type} Appointment Found for Shipment"
        loc_name = (stop_data.get("location") or {}).get("location_name") or ""
        window_start = stop_data.get("planned_time_window_start") or stop_data.get("planned_date") or ""
        window_end = stop_data.get("planned_time_window_end") or ""
        detail = (
            f"No open dock slots found for {stop_type.lower()} stop"
            + (f" at {loc_name!r}" if loc_name else "")
            + (f" in window {window_start}\u2013{window_end}" if window_start else "") + "."
            + " A manual appointment must be scheduled."
        )
        plain, html = notify_bodies_for_shipment(shipment_data, subject, error_detail=detail)
        notify_support(subject, plain, sw, html_body=html)
        # Dock is full — no slot available, but we still need an UNSCHEDULED
        # appointment record in Tempus so the shipment appears in the
        # Unscheduled panel for manual scheduling.
        #
        # Strategy: fire availability calls that are guaranteed to return zero
        # slots — Tempus seeds the UNSCHEDULED record (with created_by=null and
        # scheduled_resource_metadata.planned_date set) as a side effect of any
        # zero-result availability call on a fresh stop.
        #
        # Step 1: query the inter-rule gap window (e.g. 07:00–07:30 MDT at
        # Artesia). The gap has no dock capacity by definition, so this always
        # returns zero regardless of how full the dock is — making it a reliable
        # seed trigger even in sandbox where the dock has spare capacity.
        #
        # Step 2 (fallback): if the gap seed didn't work, query the rule window
        # that precedes the planning window's anchor rule (e.g. 04:45–07:00).
        # This returns zero when the dock is genuinely full.
        #
        # IMPORTANT: Do NOT use an explicit POST to /appointments — that sets
        # created_by to the API token user and leaves planned_date null on the
        # Tempus record, making the shipment invisible in the date-grouped panel.
        _existing_check = _api_call(appt_check_url, "GET", headers=headers)
        _existing_data = _existing_check.get("data") or []
        _need_system_record = (
            not _existing_data
            or _existing_data[0].get("created_by") is not None
        )
        if _need_system_record:
            sw.log("TRACE",
                f"schedule_appointment_for_stop: dock full — seeding system UNSCHEDULED "
                f"record via gap-window availability call for stop {stop_data.get('id')}",
                ["schedule_appointment_for_stop"])

            def _seed_via_avail(q_start: datetime, q_end: datetime, label: str) -> bool:
                """Fire one availability call; return True if Tempus seeded the record."""
                _body: Dict[str, Any] = {
                    "request_criteria_type": "SHIPMENT",
                    "shipment_id": shipment_data["id"],
                    "stop_id": stop_data["id"],
                    "start_datetime": q_start.isoformat(),
                    "end_datetime": q_end.isoformat(),
                    "delivery_type": delivery_type,
                }
                if matched_load_type_id:
                    _body["matched_load_type_id"] = matched_load_type_id
                sw.log("TRACE",
                    f"schedule_appointment_for_stop: seed attempt [{label}] "
                    f"{q_start.isoformat()} → {q_end.isoformat()}",
                    ["schedule_appointment_for_stop"])
                try:
                    _resp = _api_call(
                        f"{base_url}/facilities/{facility_id}/availability",
                        "POST", headers=headers, body=_body,
                    )
                    _windows = _resp.get("available_windows") or []
                    sw.log("TRACE",
                        f"schedule_appointment_for_stop: seed attempt [{label}] — "
                        f"{len(_windows)} windows returned",
                        ["schedule_appointment_for_stop"])
                    _rc = _api_call(appt_check_url, "GET", headers=headers)
                    _rc_data = _rc.get("data") or []
                    if _rc_data:
                        sw.log("TRACE",
                            f"schedule_appointment_for_stop: seeded {_rc_data[0].get('id')} "
                            f"(created_by={_rc_data[0].get('created_by')}) via [{label}]",
                            ["schedule_appointment_for_stop"])
                        return True
                except Exception as _err:
                    sw.log("WARNING",
                        f"schedule_appointment_for_stop: seed attempt [{label}] failed: {_err}",
                        ["schedule_appointment_for_stop"])
                return False

            _seeded = False
            try:
                # Step 1 — gap window between rule windows (guaranteed zero slots).
                # Identify the gap: find consecutive rule windows and use the
                # interval between them that contains or immediately follows the
                # anchor rule window (the one containing _start_obj or _end_obj).
                _gap_seeded = False
                if _rule_windows and len(_rule_windows) >= 2:
                    for _gi in range(len(_rule_windows) - 1):
                        _gap_start_dt = _rule_windows[_gi][1]   # end of rule window i
                        _gap_end_dt = _rule_windows[_gi + 1][0]  # start of rule window i+1
                        if _gap_end_dt > _gap_start_dt:          # real gap (not zero-width)
                            # Use the first real gap that falls within or after the
                            # planning window date (any gap on the correct day works).
                            if _gap_start_dt.date() == _start_obj.date():
                                _gap_seeded = _seed_via_avail(_gap_start_dt, _gap_end_dt, "gap")
                                if _gap_seeded:
                                    _seeded = True
                                    break

                if not _seeded:
                    # Step 2 — prior rule window (before the anchor rule window).
                    # For a planning window anchored in rule window N, query
                    # rule window N-1 (e.g. 04:45–07:00 when anchor is 07:30–15:00).
                    #
                    # When _start_obj falls in a gap (so _anchor_rule_start is None but
                    # _anchor_rule_end was set to the next window's start by the fix above),
                    # use the window immediately preceding _anchor_rule_end as the prior window.
                    # This ensures seeding still works when the planning window falls in a break gap.
                    _step2_anchor = _anchor_rule_start if _anchor_rule_start is not None else _anchor_rule_end
                    if _rule_windows and _step2_anchor is not None:
                        for _ri, (rw_s, rw_e) in enumerate(_rule_windows):
                            # Match: find the rule window whose start equals the step2 anchor.
                            # For gap case: _anchor_rule_end is the start of the post-gap window;
                            # the prior window is the one just before it (index _ri - 1).
                            # For normal case: _anchor_rule_start matches the anchor window start;
                            # the prior window is one before it as before.
                            _anchor_matches = (
                                (rw_s == _anchor_rule_start) if _anchor_rule_start is not None
                                else (rw_s == _anchor_rule_end)
                            )
                            if _anchor_matches and _ri > 0:
                                _prev_rw_s, _prev_rw_e = _rule_windows[_ri - 1]
                                # Clip prior window start to planning window start
                                # so we don't query times before the order window.
                                _prior_q_start = max(_prev_rw_s, _start_obj)
                                _prior_q_end = _prev_rw_e
                                if _prior_q_end > _prior_q_start:
                                    _seeded = _seed_via_avail(
                                        _prior_q_start, _prior_q_end, "prior-rule-window"
                                    )
                                break

                if not _seeded:
                    sw.log("WARNING",
                        f"schedule_appointment_for_stop: gap+prior-window seed attempts did not "
                        f"produce a system UNSCHEDULED record for stop {stop_data.get('id')} — "
                        f"falling back to explicit POST /appointments",
                        ["schedule_appointment_for_stop"])
                    # Fallback: explicit POST to create the UNSCHEDULED record.
                    # Tempus availability-call seeding is unreliable when the prior rule
                    # window still has open capacity (returns >0 windows, no side-effect record).
                    # A direct POST guarantees the shipment appears in the Unscheduled panel.
                    # Note: this sets created_by to the API token user (not null), but that
                    # is acceptable — the shipment will still appear in the panel.
                    try:
                        is_pickup_fb = bool(stop_data.get("is_pickup") or (stop_data.get("stop_type") or "").upper() == "PICKUP")
                        # Derive planned_date from the stop for scheduled_resource_metadata.
                        # Tempus uses this to group the UNSCHEDULED record by date in the
                        # panel — without it the record has metadata=null and is invisible.
                        _fb_planned_date = stop_data.get("planned_date") or _start_obj.strftime("%Y-%m-%d")
                        _fb_body: Dict[str, Any] = {
                            "facility_id": facility_id,
                            "scheduled_resource_type": "SHIPMENT",
                            "scheduled_resource_id": shipment_data["id"],
                            "stop_id": stop_data["id"],
                            "delivery_type": "SHIPPING" if is_pickup_fb else "RECEIVING",
                            "appointment_type": "BY_APPOINTMENT_ONLY",
                            "scheduled_resource_metadata": {
                                "resource_type": "SHIPMENT",
                                "shipment_id": shipment_data["id"],
                                "planned_date": _fb_planned_date,
                            },
                        }
                        if matched_load_type_id:
                            _fb_body["matched_load_type_id"] = matched_load_type_id
                        _fb_resp = _api_call(
                            f"{base_url}/facilities/{facility_id}/appointments",
                            "POST", headers=headers, body=_fb_body,
                        )
                        sw.log("TRACE",
                            f"schedule_appointment_for_stop: explicit POST fallback created "
                            f"UNSCHEDULED record {_fb_resp.get('id')} for stop {stop_data.get('id')}",
                            ["schedule_appointment_for_stop"])
                    except Exception as _fb_err:
                        _fb_err_str = str(_fb_err)
                        import re as _re_fb
                        _fb_existing_match = _re_fb.search(r'already exists with ID ([A-Z0-9]{26})', _fb_err_str)
                        if _fb_existing_match:
                            sw.log("TRACE",
                                f"schedule_appointment_for_stop: explicit POST fallback — "
                                f"record already exists ({_fb_existing_match.group(1)}), OK",
                                ["schedule_appointment_for_stop"])
                        else:
                            sw.log("WARNING",
                                f"schedule_appointment_for_stop: explicit POST fallback failed for "
                                f"stop {stop_data.get('id')}: {_fb_err} — shipment will not appear in panel",
                                ["schedule_appointment_for_stop"])
            except Exception as _seed_err:
                sw.log("WARNING",
                    f"schedule_appointment_for_stop: seed attempt failed for stop "
                    f"{stop_data.get('id')}: {_seed_err}",
                    ["schedule_appointment_for_stop"])
        else:
            sw.log("TRACE",
                f"schedule_appointment_for_stop: dock full — system UNSCHEDULED record already exists "
                f"for stop {stop_data.get('id')} (status={_existing_data[0].get('status')})",
                ["schedule_appointment_for_stop"])
        return False

    existing = _api_call(appt_check_url, "GET", headers=headers)
    existing_data = existing.get("data") or []
    if not existing_data:
        # No unscheduled appointment record exists — create one explicitly.
        # The availability call was supposed to create this as a side effect but
        # consistently doesn't for Turn Parent orders on near-term dates.
        # POST /facilities/{facility_id}/appointments creates an UNSCHEDULED record
        # that we can then call /schedule on.
        is_pickup = bool(stop_data.get("is_pickup") or (stop_data.get("stop_type") or "").upper() == "PICKUP")
        _create_body: Dict[str, Any] = {
            "facility_id": facility_id,
            "scheduled_resource_type": "SHIPMENT",
            "scheduled_resource_id": shipment_data["id"],
            "stop_id": stop_data["id"],
            "delivery_type": "SHIPPING" if is_pickup else "RECEIVING",
            "appointment_type": "BY_APPOINTMENT_ONLY",
        }
        if matched_load_type_id:
            _create_body["matched_load_type_id"] = matched_load_type_id
        logger.info(
            f"schedule_appointment_for_stop: no appointment record found — "
            f"creating unscheduled record for stop {stop_data.get('id')}"
        )
        try:
            _created = _api_call(
                f"{base_url}/facilities/{facility_id}/appointments",
                "POST", headers=headers,
                body=_create_body,
            )
            logger.info(
                f"schedule_appointment_for_stop: created appointment record "
                f"{_created.get('id')} status={_created.get('status')}"
            )
        except Exception as _create_err:
            _create_err_str = str(_create_err)
            # Tempus returns 400 "A similar appointment already exists with ID <ulid>"
            # when a concurrent invocation already created the UNSCHEDULED record.
            # Treat this as a recoverable case: re-fetch the existing record and continue.
            import re as _re
            _existing_id_match = _re.search(r'already exists with ID ([A-Z0-9]{26})', _create_err_str)
            if _existing_id_match:
                sw.log("TRACE",
                    f"schedule_appointment_for_stop: UNSCHEDULED record already exists "
                    f"(ID {_existing_id_match.group(1)}) for stop {stop_data.get('id')} — "
                    f"using existing record instead of failing",
                    ["schedule_appointment_for_stop"])
            else:
                sw.log("WARNING", f"No appointment record found and failed to create one for stop {stop_data.get('id')}: {_create_err}", ["schedule_appointment_for_stop"])
                return False
        existing = _api_call(appt_check_url, "GET", headers=headers)
        existing_data = existing.get("data") or []
        if not existing_data:
            sw.log("WARNING", f"No appointment record found for stop {stop_data.get('id')} even after explicit create", ["schedule_appointment_for_stop"])
            return False

    # Re-check SCHEDULED status here — a concurrent Lambda instance may have scheduled
    # the appointment between our availability call above and this point.
    # Re-check SCHEDULED status here — a concurrent Lambda instance may have scheduled
    # the appointment between our availability call above and this point.
    # IMPORTANT: Tempus can return multiple appointment records for the same stop when two
    # concurrent Lambdas both fire availability calls (the second call creates a second
    # UNSCHEDULED record on top of the first one that the winner already scheduled).
    # We must select only the UNSCHEDULED record to schedule against — never schedule on
    # a record that is already SCHEDULED, CANCELLED, or otherwise terminal.
    # If all records are SCHEDULED, the concurrent winner handled it and we bow out.
    _unscheduled_record = next(
        (r for r in existing_data if r.get("status") not in ("SCHEDULED", "CANCELLED")),
        None,
    )
    if _unscheduled_record is None:
        # All records are SCHEDULED (or CANCELLED) — concurrent winner already booked.
        if any(r.get("status") == "SCHEDULED" for r in existing_data):
            sw.log("TRACE", f"Appointment already scheduled by concurrent execution for stop {stop_data.get('id')} — skipping", ["schedule_appointment_for_stop"])
            return True
        # All records are CANCELLED (edge case) — fall through to let the explicit-create path handle it.
        sw.log("WARNING", f"All appointment records for stop {stop_data.get('id')} are CANCELLED — cannot schedule; manual intervention required", ["schedule_appointment_for_stop"])
        return False

    # Add matched_load_type_id to the schedule body so Tempus enforces the
    # concurrent_loads_allowed dock rule at schedule time — matches frontend behaviour
    # (frontend sends matched_load_type_id on both create and schedule calls).
    if matched_load_type_id and "matched_load_type_id" not in schedule_body:
        schedule_body["matched_load_type_id"] = matched_load_type_id

    schedule_url = f"{base_url}/facilities/appointments/{_unscheduled_record['id']}/schedule"
    if DRY_RUN:
        logger.info(f"[DRY_RUN] Would POST schedule for stop {stop_data.get('id')}: {json.dumps(schedule_body)}")
        logger.info(f"[DRY_RUN] Would update stop {stop_data.get('id')} planned window to match appointment")
        return True

    try:
        _api_call(schedule_url, "POST", headers=headers, body=schedule_body)
    except Exception as _schedule_err:
        _schedule_err_str = str(_schedule_err)
        _is_capacity_400 = (
            ("400" in _schedule_err_str or "Bad Request" in _schedule_err_str)
            and _unscheduled_record is not None
        )
        if _is_capacity_400:
            # Tempus returned 400 — a concurrent Lambda already booked this slot.
            # Loop: cancel UNSCHEDULED record, jitter, re-query availability, pick next
            # slot (excluding all previously-contested slots), retry.  Repeat up to
            # MAX_400_RETRIES times so cascading concurrent 400s are all handled.
            import time as _time_400
            import random as _random_400
            MAX_400_RETRIES = 10  # enough for 8 slots across 2 docks
            _excluded_slots_400 = []  # accumulate all slots that returned 400
            _current_body = schedule_body   # body that just got a 400
            _current_record = _unscheduled_record  # UNSCHEDULED record to cancel
            _retry_success = False

            for _attempt_400 in range(MAX_400_RETRIES):
                logger.warning(
                    f"schedule_appointment_for_stop: Tempus 400 on POST /schedule for "
                    f"stop {stop_data.get('id')} (attempt {_attempt_400 + 1}/{MAX_400_RETRIES}) — "
                    f"concurrent booking detected; cancelling and retrying next slot"
                )
                # Cancel the current UNSCHEDULED record.
                try:
                    _api_call(
                        f"{base_url}/facilities/appointments/{_current_record['id']}/cancel",
                        "POST", headers=headers,
                        body={"reason": f"Concurrent 400 attempt {_attempt_400 + 1}: slot taken — retrying"},
                    )
                    _cancelled_for_retry = True
                    logger.info(f"schedule_appointment_for_stop: cancelled UNSCHEDULED {_current_record['id']} after 400")
                except Exception as _ce:
                    logger.warning(f"schedule_appointment_for_stop: failed to cancel after 400: {_ce}")

                # Extract dock+slot from the body that just got a 400.
                _400_dock_id   = _current_body.get("dock_id")
                _400_start_str = ((_current_body.get("start") or {}).get("timestamp") or "") or _current_body.get("start_datetime", "")
                _400_end_str   = ((_current_body.get("end") or {}).get("timestamp") or "")   or _current_body.get("end_datetime", "")
                try:
                    _400_start_dt = datetime.fromisoformat(_400_start_str.replace("Z", "+00:00")) if _400_start_str else None
                except Exception:
                    _400_start_dt = None

                # Add this slot to the exclusion list.
                if _400_dock_id and _400_start_dt:
                    _excluded_slots_400.append({"dock_id": _400_dock_id, "start": _400_start_dt})
                logger.info(
                    f"schedule_appointment_for_stop: 400-triggered retry attempt {_attempt_400 + 1} — "
                    f"always excluding dock={_400_dock_id} slot={_400_start_str} "
                    f"(total excluded: {len(_excluded_slots_400)})"
                )

                # Jitter to reduce thundering-herd on the next slot.
                _jitter = _random_400.uniform(1.0, 3.0)
                logger.info(f"schedule_appointment_for_stop: jitter sleep {_jitter:.1f}s before retry {_attempt_400 + 1}")
                _time_400.sleep(_jitter)

                # Reuse the original availability response — do not re-query Tempus.
                # Re-querying causes Tempus to omit same-time slots on other docks
                # because it treats the facility-level slot as taken once one dock
                # books it.  The original response already shows all docks open;
                # find_first_available_slot + _excl_set handles skipping the
                # contested dock+time, so the next order lands on the other dock
                # at the same start time rather than advancing 15 minutes.
                _retry_avail_400 = availability_data

                # Find next slot, excluding ALL previously-contested slots.
                # find_first_available_slot only accepts one excluded_slot; pass the
                # most-recently-contested one and pre-filter the rest ourselves.
                # Build a set of (dock_id, slot_start_isoformat) for fast lookup.
                _excl_set = {(ex["dock_id"], ex["start"].isoformat()) for ex in _excluded_slots_400 if ex.get("dock_id") and ex.get("start")}

                _retry_body_400 = None
                if _retry_avail_400:
                    # Pass the most-recent exclusion to find_first_available_slot;
                    # we'll post-filter any others.
                    _last_excl = _excluded_slots_400[-1] if _excluded_slots_400 else None
                    _retry_body_400, _ = find_first_available_slot(
                        _retry_avail_400,
                        base_url=base_url, headers=headers,
                        facility_id=facility_id,
                        planning_window_start=_pw_start,
                        planning_window_end=_pw_end,
                        prefer_latest=_prefer_latest,
                        excluded_slot=_last_excl,
                        matched_load_type_id=matched_load_type_id,
                    )
                    # Post-filter: if the returned slot is still in our exclusion set, skip it.
                    if _retry_body_400:
                        _rb_dock  = _retry_body_400.get("dock_id", "")
                        _rb_start = ((_retry_body_400.get("start") or {}).get("timestamp") or "") or _retry_body_400.get("start_datetime", "")
                        try:
                            _rb_start_dt = datetime.fromisoformat(_rb_start.replace("Z", "+00:00")) if _rb_start else None
                        except Exception:
                            _rb_start_dt = None
                        if _rb_start_dt and (_rb_dock, _rb_start_dt.isoformat()) in _excl_set:
                            logger.info(f"schedule_appointment_for_stop: post-filter skipped already-excluded slot {_rb_start} on {_rb_dock}")
                            _retry_body_400 = None

                if not _retry_body_400:
                    sw.log("TRACE",
                        f"schedule_appointment_for_stop: no slot available after 400-triggered retry "
                        f"(attempt {_attempt_400 + 1}) — stop left unscheduled",
                        ["schedule_appointment_for_stop"]
                    )
                    break  # no more slots to try

                # Create a fresh UNSCHEDULED record for the new slot.
                _new_appt_id = None
                try:
                    _new_rec = _api_call(
                        f"{base_url}/facilities/{facility_id}/appointments",
                        "POST", headers=headers,
                        body={
                            "stop_id": stop_data.get("id"),
                            "scheduled_resource_type": "SHIPMENT",
                            "scheduled_resource_id": shipment_data.get("id"),
                            "delivery_type": "SHIPPING",
                            "matched_load_type_id": matched_load_type_id,
                        },
                    ) or {}
                    _new_appt_id = _new_rec.get("id")
                except Exception as _new_create_err:
                    logger.warning(f"schedule_appointment_for_stop: 400-retry create new UNSCHEDULED failed: {_new_create_err} — re-fetching")
                    _existing_after_cancel = _api_call(appt_check_url, "GET", headers=headers) or {}
                    _new_unscheduled_rec = next(
                        (a for a in (_existing_after_cancel.get("data") or []) if a.get("status") not in ("SCHEDULED", "CANCELLED")),
                        None,
                    )
                    _new_appt_id = _new_unscheduled_rec["id"] if _new_unscheduled_rec else None

                if not _new_appt_id:
                    sw.log("WARNING",
                        f"schedule_appointment_for_stop: 400-retry could not get a new UNSCHEDULED record "
                        f"(attempt {_attempt_400 + 1}) — stop left unscheduled",
                        ["schedule_appointment_for_stop"]
                    )
                    break

                # Attempt to schedule the new slot.
                try:
                    _api_call(
                        f"{base_url}/facilities/appointments/{_new_appt_id}/schedule",
                        "POST", headers=headers, body=_retry_body_400,
                    )
                    # SUCCESS — update bookkeeping and fall through to post-booking checks.
                    schedule_body = _retry_body_400
                    _retry_start_str = ((_retry_body_400.get("start") or {}).get("timestamp") or "") or _retry_body_400.get("start_datetime", "")
                    _retry_end_str   = ((_retry_body_400.get("end") or {}).get("timestamp") or "") or _retry_body_400.get("end_datetime", "")
                    logger.info(
                        f"schedule_appointment_for_stop: 400-triggered retry succeeded on attempt "
                        f"{_attempt_400 + 1} — rescheduled stop {stop_data.get('id')} on slot {_retry_start_str}"
                    )
                    booked_dock_id  = _retry_body_400.get("dock_id")
                    _bss = _retry_start_str
                    _bes = _retry_end_str
                    booked_start_dt = datetime.fromisoformat(_bss.replace("Z", "+00:00")) if _bss else None
                    booked_end_dt   = datetime.fromisoformat(_bes.replace("Z", "+00:00")) if _bes else None
                    _unscheduled_record = {"id": _new_appt_id}
                    _retry_success = True
                    break
                except Exception as _retry_err:
                    _retry_err_str = str(_retry_err)
                    if "400" in _retry_err_str or "Bad Request" in _retry_err_str:
                        # Another 400 — loop again with this slot now excluded.
                        logger.warning(
                            f"schedule_appointment_for_stop: 400-triggered retry attempt {_attempt_400 + 1} "
                            f"also got 400 — cascading; will try next slot"
                        )
                        _current_body   = _retry_body_400
                        _current_record = {"id": _new_appt_id}
                        continue  # loop
                    else:
                        sw.log("WARNING",
                            f"schedule_appointment_for_stop: 400-triggered retry schedule call failed "
                            f"(attempt {_attempt_400 + 1}): {_retry_err}",
                            ["schedule_appointment_for_stop"]
                        )
                        break  # non-400 error — give up

            if not _retry_success:
                return False
        else:
            # Non-400 error — re-raise so the caller can handle it.
            raise
    sw.log("TRACE", f"Appointment scheduled for stop {stop_data.get('id')}", ["schedule_appointment_for_stop"])

    # Fix 1 — unconditionally re-assert the rounded planned window on the stop.
    # Tempus may sync the appointment start time (raw, non-quarter-hour-aligned) back
    # to the stop's planned_time_window_start after scheduling, overwriting the rounded
    # value we wrote earlier.  Re-writing here ensures the dispatcher always sees a
    # clean 15-minute-aligned window regardless of what Tempus wrote back.
    #
    # Special case: if the pre-scheduling planned window was a full-day default
    # (00:00–23:45), it means the stop never had a real window (e.g. PIT delivery
    # where ship_from had no plan_window).  In that case, mirror the booked
    # appointment time onto the stop so the dispatcher sees the actual slot instead
    # of the default — matching Tempus's own behavior of syncing the appointment
    # window back to the stop.
    _effective_pw_start = _pw_start
    _effective_pw_end = _pw_end
    if _pw_start and _pw_end and schedule_body and not DRY_RUN:
        try:
            _pw_tz_str = get_stop_timezone(stop_data)
            _pw_tz = ZoneInfo(_pw_tz_str)
            _pw_start_local_check = _pw_start.astimezone(_pw_tz)
            _pw_end_local_check = _pw_end.astimezone(_pw_tz)
            if is_full_day_window(_pw_start_local_check, _pw_end_local_check):
                _booked_start_str = (schedule_body.get("start") or {}).get("timestamp")
                _booked_end_str = (schedule_body.get("end") or {}).get("timestamp")
                if _booked_start_str and _booked_end_str:
                    _effective_pw_start = datetime.fromisoformat(_booked_start_str.replace("Z", "+00:00"))
                    _effective_pw_end = datetime.fromisoformat(_booked_end_str.replace("Z", "+00:00"))
                    logger.info(
                        f"schedule_appointment_for_stop: full-day default window detected — "
                        f"will mirror booked slot {_booked_start_str}–{_booked_end_str} onto stop planned window"
                    )
        except Exception as _fday_err:
            logger.warning(f"schedule_appointment_for_stop: full-day default window check failed (non-fatal): {_fday_err}")

    if preserve_pickup_window and _effective_pw_start and _effective_pw_end and not DRY_RUN:
        try:
            _refreshed_for_window = _api_call(f"{base_url}/v2/shipments/{shipment_data['id']}/", "GET", headers=headers) or shipment_data
            _s_idx = next((i for i, s in enumerate(_refreshed_for_window.get("stops") or []) if s.get("id") == stop_data.get("id")), None)
            if _s_idx is not None:
                _stop_tz_str = get_stop_timezone(_refreshed_for_window["stops"][_s_idx])
                _stop_tz = ZoneInfo(_stop_tz_str)
                _pw_start_local = round_down_to_quarter_hour(_effective_pw_start.astimezone(_stop_tz))
                _pw_end_local = round_down_to_quarter_hour(_effective_pw_end.astimezone(_stop_tz))
                _expected_start = _pw_start_local.strftime("%H:%M:%S")
                _expected_end = _pw_end_local.strftime("%H:%M:%S")
                _current_start = _refreshed_for_window["stops"][_s_idx].get("planned_time_window_start")
                _current_end = _refreshed_for_window["stops"][_s_idx].get("planned_time_window_end")
                if _current_start != _expected_start or _current_end != _expected_end:
                    _refreshed_for_window["stops"][_s_idx]["planned_date"] = _pw_start_local.strftime("%Y-%m-%d")
                    _refreshed_for_window["stops"][_s_idx]["planned_time_window_start"] = _expected_start
                    _refreshed_for_window["stops"][_s_idx]["planned_time_window_end"] = _expected_end
                    _api_call(f"{base_url}/v2/shipments/{shipment_data['id']}/", "PUT", headers=headers, body=_refreshed_for_window)
                    sw.log("TRACE",
                        f"Post-schedule window assertion: restored rounded pickup window "
                        f"{_expected_start}–{_expected_end} "
                        f"(Tempus had written {_current_start}–{_current_end})",
                        ["schedule_appointment_for_stop"])
                    logger.info(
                        f"schedule_appointment_for_stop: post-schedule window assertion — "
                        f"restored {_expected_start}–{_expected_end} for stop {stop_data.get('id')} "
                        f"(was {_current_start}–{_current_end})"
                    )
                else:
                    logger.info(
                        f"schedule_appointment_for_stop: post-schedule window assertion — "
                        f"window already correct ({_current_start}–{_current_end}), no update needed"
                    )
        except Exception as _window_assert_err:
            logger.warning(f"schedule_appointment_for_stop: post-schedule window assertion failed (non-fatal): {_window_assert_err}")

    # Post-booking race condition check: verify we didn't double-book the dock.
    # Two Lambda instances can both pass the pre-booking capacity check simultaneously
    # if they query availability before either has committed.  After booking we
    # re-count overlapping appointments on the same dock — if the count exceeds
    # concurrent_loads_allowed, cancel our appointment and recurse once to find
    # the next available slot on a different dock or time.
    _cancelled_for_retry = False  # True once we cancel our appointment and attempt a retry
    try:
        # schedule_body from find_first_available_slot has dock_id as a top-level key.
        booked_dock_id = schedule_body.get("dock_id")
        booked_start_str = (schedule_body.get("start") or {}).get("timestamp")
        booked_end_str = (schedule_body.get("end") or {}).get("timestamp")
        # Use the matched_load_type_id already resolved earlier in this function.
        # Fall back to scanning available_windows if somehow still None.
        matched_lt_id = matched_load_type_id or next(
            (w.get("load_type_id") for w in (availability_data.get("available_windows") or []) if w.get("load_type_id")),
            None,
        )
        if booked_dock_id and booked_start_str and booked_end_str and matched_lt_id:
            booked_start_dt = datetime.fromisoformat(booked_start_str.replace("Z", "+00:00"))
            booked_end_dt = datetime.fromisoformat(booked_end_str.replace("Z", "+00:00"))
            logger.info(
                f"schedule_appointment_for_stop: post-booking capacity check — "
                f"dock={booked_dock_id} slot={booked_start_str}→{booked_end_str} "
                f"load_type={matched_lt_id}"
            )
            # Poll up to 3 times (at 0s, 3s, 6s) to catch concurrent bookings that
            # commit slightly after ours. A single read is not sufficient because
            # Tempus's appointment list may not reflect a concurrent schedule immediately.
            import time as _time
            over_capacity = False
            for _attempt in range(3):
                if _attempt > 0:
                    _time.sleep(3)
                over_capacity = is_slot_at_capacity(
                    facility_id, booked_dock_id, booked_start_dt, booked_end_dt,
                    matched_lt_id, base_url, headers,
                )
                logger.info(
                    f"schedule_appointment_for_stop: post-booking over_capacity={over_capacity} "
                    f"(attempt {_attempt + 1}/3)"
                )
                if over_capacity:
                    break
            if over_capacity:
                logger.warning(
                    f"schedule_appointment_for_stop: post-booking capacity check detected double-book "
                    f"on dock {booked_dock_id} at {booked_start_str} — determining if we are the loser"
                )
                # Determine whether we are the "loser" (the later-booked appointment).
                # Strategy: fetch all SCHEDULED appointments on this dock+slot, sorted by
                # appointment ID (ULIDs are lexicographically time-ordered).  The appointment
                # with the HIGHEST ID was booked last — that is the loser that should cancel.
                # If our appointment ID is the highest, we cancel and reschedule.  If we are
                # the first-booked (lower ID), we keep our slot and let the other Lambda handle
                # its own cancellation when its own post-booking check fires.
                #
                # This prevents the mutual-cancellation loop where both sides cancel each other
                # and then both retry onto the same (now-empty) slot.
                import time as _time2  # noqa: PLC0415 (local import is intentional)
                # Use _unscheduled_record (not existing_data[0]) — in the concurrent case
                # existing_data may contain both a SCHEDULED (winner) and UNSCHEDULED record;
                # existing_data[0] could be either depending on Tempus sort order.
                our_appt_id = _unscheduled_record["id"]
                try:
                    all_appts_resp = _api_call(
                        f"{base_url}/facilities/appointments"
                        f"?facility_id={facility_id}&status=SCHEDULED&page=1&limit=100",
                        "GET", headers=headers,
                    )
                    all_appts = all_appts_resp.get("data") or []
                    overlapping_appt_ids = []
                    for _a in all_appts:
                        if _a.get("dock_id") != booked_dock_id or _a.get("status") != "SCHEDULED":
                            continue
                        try:
                            _as = datetime.fromisoformat(_a["start"]["timestamp"].replace("Z", "+00:00"))
                            _ae = datetime.fromisoformat(_a["end"]["timestamp"].replace("Z", "+00:00"))
                            if _as < booked_end_dt and _ae > booked_start_dt:
                                overlapping_appt_ids.append(_a["id"])
                        except Exception:
                            continue
                except Exception as fetch_err:
                    logger.warning(f"schedule_appointment_for_stop: could not fetch overlapping appts for loser-detection: {fetch_err}")
                    overlapping_appt_ids = [our_appt_id]  # assume we are the only one / loser by default

                # ULIDs are lexicographically ordered by creation time — lowest = first-booked = winner.
                # Fix 2: with N concurrent Lambdas all landing on the same slot, only the SINGLE
                # lowest ULID is the winner.  Every other Lambda (N-1) must cancel and retry.
                # Previous logic used `our_id >= max(others)` which only eliminated one loser per
                # round; with 6 concurrent invocations, 5 would incorrectly keep their slots.
                # New logic: we_are_loser = (our_id != min(all overlapping ids)).
                # If overlapping_appt_ids contains ONLY our own ID, there is no actual conflict —
                # we are the sole occupant of this slot. Treat as not-a-loser so we keep the slot.
                other_appt_ids = [aid for aid in overlapping_appt_ids if aid != our_appt_id]
                if not other_appt_ids:
                    we_are_loser = False
                    logger.info(
                        f"schedule_appointment_for_stop: only our own appointment found at slot — "
                        f"no real conflict, keeping slot (our_id={our_appt_id})"
                    )
                else:
                    # Winner = single lowest ULID across ALL overlapping appointments (including ours).
                    we_are_loser = our_appt_id != min(overlapping_appt_ids)
                logger.info(
                    f"schedule_appointment_for_stop: overlapping_appts={overlapping_appt_ids} "
                    f"our_id={our_appt_id} winner_id={min(overlapping_appt_ids) if overlapping_appt_ids else 'n/a'} "
                    f"we_are_loser={we_are_loser}"
                )

                if not we_are_loser:
                    logger.info(
                        f"schedule_appointment_for_stop: we are NOT the loser (our ID is lowest) — "
                        f"keeping our slot, letting the other Lambda cancel its own duplicate"
                    )
                    # Our appointment stays. The other concurrent Lambda's post-booking check will
                    # fire, see that it is the highest-ID appointment, cancel itself, and reschedule.
                    #
                    # However, Tempus may unschedule our appointment as a side-effect of the other
                    # Lambda re-scheduling its own appointment on the same slot (conflict resolution).
                    # We verify our slot is still SCHEDULED after a short wait; if it has been
                    # knocked out, we reschedule ourselves to a fresh non-conflicting slot.
                    import time as _time3
                    _time3.sleep(5.0)  # give the loser time to cancel + reschedule (~5-6s jitter+retry)
                    verify_resp = _api_call(appt_check_url, "GET", headers=headers)
                    verify_data = (verify_resp.get("data") or [])
                    our_appt_current = next((a for a in verify_data if a["id"] == our_appt_id), None)
                    if not our_appt_current or our_appt_current.get("status") != "SCHEDULED":
                        logger.warning(
                            f"schedule_appointment_for_stop: winner appointment {our_appt_id} was "
                            f"unscheduled by Tempus conflict resolution — rescheduling to a new slot"
                        )
                        # Re-fetch availability and find a non-conflicting slot, excluding the
                        # slot that was just taken by the loser (the one we originally booked).
                        winner_retry_avail = _api_call(
                            f"{base_url}/facilities/{facility_id}/availability",
                            "POST", headers=headers,
                            body={
                                "request_criteria_type": "SHIPMENT",
                                "shipment_id": shipment_data["id"],
                                "stop_id": stop_data["id"],
                                "start_datetime": (_pw_start or booked_start_dt).isoformat(),
                                "end_datetime": (_pw_end or booked_end_dt).isoformat(),
                                "delivery_type": "SHIPPING",
                            },
                        )
                        winner_retry_body, _winner_skipped = find_first_available_slot(
                            winner_retry_avail,
                            base_url=base_url, headers=headers,
                            facility_id=facility_id,
                            planning_window_start=_pw_start,
                            planning_window_end=_pw_end,
                            prefer_latest=_prefer_latest,
                            excluded_slot={"dock_id": booked_dock_id, "start": booked_start_dt},
                            matched_load_type_id=matched_load_type_id,  # carry through from initial booking
                        )
                        if winner_retry_body and our_appt_id:
                            try:
                                _api_call(
                                    f"{base_url}/facilities/appointments/{our_appt_id}/schedule",
                                    "POST", headers=headers, body=winner_retry_body,
                                )
                                sw.log("TRACE", f"Winner recovery: rescheduled stop {stop_data.get('id')} on new slot after Tempus conflict eviction", ["schedule_appointment_for_stop"])
                                schedule_body = winner_retry_body
                            except Exception as winner_retry_err:
                                logger.warning(f"schedule_appointment_for_stop: winner recovery reschedule failed: {winner_retry_err}")
                        else:
                            sw.log("WARNING", "Winner recovery: no alternative slot available", ["schedule_appointment_for_stop"])
                    else:
                        logger.info(
                            f"schedule_appointment_for_stop: winner verification OK — "
                            f"appointment {our_appt_id} is still SCHEDULED"
                        )
                        # Tempus may have reset the stop's planned window to 00:00–23:59
                        # when the concurrent loser cancelled its appointment on the
                        # same dock+slot. Re-write the correct window now as a
                        # defensive guard even though we didn't cancel anything ourselves.
                        if preserve_pickup_window and _pw_start and _pw_end:
                            try:
                                refreshed = _api_call(f"{base_url}/v2/shipments/{shipment_data['id']}/", "GET", headers=headers) or shipment_data
                                s_idx = next((i for i, s in enumerate(refreshed.get("stops") or []) if s.get("id") == stop_data.get("id")), None)
                                if s_idx is not None:
                                    stop_tz_str = get_stop_timezone(refreshed["stops"][s_idx])
                                    stop_tz = ZoneInfo(stop_tz_str)
                                    pw_start_local = round_down_to_quarter_hour(_pw_start.astimezone(stop_tz))
                                    pw_end_local = round_down_to_quarter_hour(_pw_end.astimezone(stop_tz))
                                    current_start = refreshed["stops"][s_idx].get("planned_time_window_start")
                                    current_end = refreshed["stops"][s_idx].get("planned_time_window_end")
                                    expected_start = pw_start_local.strftime("%H:%M:%S")
                                    expected_end = pw_end_local.strftime("%H:%M:%S")
                                    if current_start != expected_start or current_end != expected_end:
                                        refreshed["stops"][s_idx]["planned_date"] = pw_start_local.strftime("%Y-%m-%d")
                                        refreshed["stops"][s_idx]["planned_time_window_start"] = expected_start
                                        refreshed["stops"][s_idx]["planned_time_window_end"] = expected_end
                                        _api_call(f"{base_url}/v2/shipments/{shipment_data['id']}/", "PUT", headers=headers, body=refreshed)
                                        sw.log("TRACE",
                                            f"Winner: restored pickup window after concurrent loser cancel: "
                                            f"{pw_start_local.strftime('%H:%M')} – {pw_end_local.strftime('%H:%M')} "
                                            f"(was {current_start}–{current_end})",
                                            ["schedule_appointment_for_stop"])
                                        logger.info(
                                            f"schedule_appointment_for_stop: winner restored pickup window "
                                            f"{expected_start}–{expected_end} for stop {stop_data.get('id')} "
                                            f"(Tempus had reset it to {current_start}–{current_end})"
                                        )
                                    else:
                                        logger.info(
                                            f"schedule_appointment_for_stop: winner — pickup window already correct "
                                            f"({current_start}–{current_end}), no restore needed"
                                        )
                            except Exception as winner_restore_err:
                                logger.warning(f"schedule_appointment_for_stop: winner pickup window restore failed (non-fatal): {winner_restore_err}")
                else:
                    logger.warning(
                        f"schedule_appointment_for_stop: we ARE the loser (highest ID) — "
                        f"cancelling and rescheduling on a different dock/slot"
                    )
                    # Cancel our appointment using /cancel (not /unschedule)
                    cancel_url = f"{base_url}/facilities/appointments/{our_appt_id}/cancel"
                    try:
                        _api_call(cancel_url, "POST", headers=headers, body={"reason": "Double-booking detected by post-schedule race guard — rescheduling on different slot"})
                        _cancelled_for_retry = True
                        logger.info(f"schedule_appointment_for_stop: cancelled double-booked appointment {our_appt_id}")
                    except Exception as cancel_err:
                        logger.warning(f"schedule_appointment_for_stop: failed to cancel double-booked appointment: {cancel_err}")

                    # Wait a short random jitter so the winner's appointment is visible to
                    # Tempus before we query availability — the capacity check in
                    # find_first_available_slot will then see the winner's slot as full
                    # and steer us to an earlier or different-dock slot.
                    import random as _random
                    jitter_secs = _random.uniform(2.0, 5.0)
                    logger.info(f"schedule_appointment_for_stop: jitter sleep {jitter_secs:.1f}s before retry")
                    _time2.sleep(jitter_secs)

                    # Re-fetch availability and find an alternative slot.
                    # Only hard-exclude the cancelled dock+slot if the winner's appointment
                    # is still SCHEDULED there — i.e. there really is a concurrent conflict.
                    # If the slot is now free (we were the only booking), allow the retry to
                    # pick it again rather than being pushed to a suboptimal dock.
                    facility_id_retry = (availability_data.get("load_type_dock_rule_match_results") or {}).get("facility_id") or facility_id
                    # For point-in-time orders _pw_start == _pw_end, which gives
                    # start_datetime == end_datetime in the availability request —
                    # Tempus rejects that with a 400.  Expand the retry window by the
                    # appointment duration so the query is valid.  find_first_available_slot
                    # will still apply the exact_time filter and only pick the right slot.
                    _retry_pw_start = _pw_start or booked_start_dt
                    _retry_pw_end = _pw_end or booked_end_dt
                    if _is_point_in_time and _retry_pw_start == _retry_pw_end:
                        _retry_pw_end = _retry_pw_start + timedelta(minutes=_appt_duration_minutes or APPOINTMENT_DURATION_MINUTES)
                        logger.info(
                            f"schedule_appointment_for_stop: point-in-time retry — expanded end by "
                            f"{_appt_duration_minutes or APPOINTMENT_DURATION_MINUTES}min for availability query "
                            f"({_retry_pw_start.isoformat()} – {_retry_pw_end.isoformat()})"
                        )
                    retry_start = _retry_pw_start.isoformat()
                    retry_end = _retry_pw_end.isoformat()

                    slot_still_contested = is_slot_at_capacity(
                        facility_id_retry, booked_dock_id,
                        booked_start_dt, booked_start_dt + (booked_end_dt - booked_start_dt),
                        matched_load_type_id, base_url, headers,
                    )
                    retry_excluded = (
                        {"dock_id": booked_dock_id, "start": booked_start_dt}
                        if slot_still_contested else None
                    )
                    logger.info(
                        f"schedule_appointment_for_stop: slot_still_contested={slot_still_contested} "
                        f"— {'excluding' if retry_excluded else 'allowing'} dock {booked_dock_id} at {booked_start_dt.isoformat()} in retry"
                    )

                    retry_avail = _api_call(
                        f"{base_url}/facilities/{facility_id_retry}/availability",
                        "POST", headers=headers,
                        body={
                            "request_criteria_type": "SHIPMENT",
                            "shipment_id": shipment_data["id"],
                            "stop_id": stop_data["id"],
                            "start_datetime": retry_start,
                            "end_datetime": retry_end,
                            "delivery_type": "SHIPPING",
                        },
                    )
                    retry_schedule_body, _retry_skipped = find_first_available_slot(
                        retry_avail,
                        base_url=base_url, headers=headers,
                        facility_id=facility_id_retry,
                        planning_window_start=_pw_start,
                        planning_window_end=_pw_end,
                        prefer_latest=_prefer_latest,
                        excluded_slot=retry_excluded,
                        matched_load_type_id=matched_load_type_id,  # carry through from initial booking
                    )
                    if retry_schedule_body:
                        # Schedule directly on our own appointment ID (the one we just cancelled).
                        # Avoid re-fetching via appt_check_url — that query can return another
                        # shipment's appointment record when Tempus results race, causing the wrong
                        # appointment to be scheduled on an alien stop.
                        try:
                            _api_call(
                                f"{base_url}/facilities/appointments/{our_appt_id}/schedule",
                                "POST", headers=headers, body=retry_schedule_body,
                            )
                            sw.log("TRACE", f"Post-double-book retry: rescheduled stop {stop_data.get('id')} on new dock/slot", ["schedule_appointment_for_stop"])
                            schedule_body = retry_schedule_body
                            # Tempus reset the stop's planned window to 00:00–23:59 when we
                            # cancelled the double-booked appointment above.  If we're
                            # preserving the original 3-hour pickup window, re-write it now
                            # so the operator sees the correct window rather than a full day.
                            if preserve_pickup_window and _pw_start and _pw_end:
                                try:
                                    refreshed = _api_call(f"{base_url}/v2/shipments/{shipment_data['id']}/", "GET", headers=headers) or shipment_data
                                    s_idx = next((i for i, s in enumerate(refreshed.get("stops") or []) if s.get("id") == stop_data.get("id")), None)
                                    if s_idx is not None:
                                        stop_tz_str = get_stop_timezone(refreshed["stops"][s_idx])
                                        stop_tz = ZoneInfo(stop_tz_str)
                                        pw_start_local = round_down_to_quarter_hour(_pw_start.astimezone(stop_tz))
                                        pw_end_local = round_down_to_quarter_hour(_pw_end.astimezone(stop_tz))
                                        refreshed["stops"][s_idx]["planned_date"] = pw_start_local.strftime("%Y-%m-%d")
                                        refreshed["stops"][s_idx]["planned_time_window_start"] = pw_start_local.strftime("%H:%M:%S")
                                        refreshed["stops"][s_idx]["planned_time_window_end"] = pw_end_local.strftime("%H:%M:%S")
                                        _api_call(f"{base_url}/v2/shipments/{shipment_data['id']}/", "PUT", headers=headers, body=refreshed)
                                        sw.log("TRACE",
                                            f"Restored pickup window after race-guard retry: "
                                            f"{pw_start_local.strftime('%H:%M')} – {pw_end_local.strftime('%H:%M')} "
                                            f"(Tempus had reset to 00:00–23:59)",
                                            ["schedule_appointment_for_stop"])
                                        logger.info(
                                            f"schedule_appointment_for_stop: restored planned window "
                                            f"{pw_start_local.strftime('%H:%M')} – {pw_end_local.strftime('%H:%M')} "
                                            f"for stop {stop_data.get('id')} after Tempus reset"
                                        )
                                except Exception as restore_err:
                                    logger.warning(f"schedule_appointment_for_stop: failed to restore pickup window after retry: {restore_err}")
                        except Exception as retry_sched_err:
                            logger.warning(f"schedule_appointment_for_stop: retry schedule on {our_appt_id} failed: {retry_sched_err}")
                            sw.log("WARNING", f"Post-double-book retry: schedule call failed: {retry_sched_err}", ["schedule_appointment_for_stop"])
                    else:
                        sw.log("WARNING", "Post-double-book retry: no alternative slot found — appointment left unscheduled", ["schedule_appointment_for_stop"])
                        return False
    except Exception as race_err:
        if _cancelled_for_retry:
            # We already cancelled our appointment and then hit an error in the retry path.
            # The stop has no appointment. Return False so the caller can handle it.
            sw.log("WARNING",
                f"Race-guard retry failed after cancelling appointment: {race_err} — stop left unscheduled",
                ["schedule_appointment_for_stop"])
            logger.warning(f"schedule_appointment_for_stop: retry failed after cancel (returning False): {race_err}")
            return False
        logger.warning(f"schedule_appointment_for_stop: post-booking race check failed (non-fatal): {race_err}")

    # Sync planned_time_window_start/end to reflect the confirmed appointment.
    # For point-in-time orders: planned window = appt_start → appt_start (exact time only).
    # The appointment duration (e.g. 12:00–12:45) is a Tempus internal concept and
    # must not bleed into the planned window — 12:00–12:45 would imply 12:15 or 12:30
    # are valid pickup times, which they are not.
    # EXCEPTION: when preserve_pickup_window=True the pickup window was deliberately set
    # to a 3-hour derived range (point-in-time delivery orders) and must not be overwritten
    # by the appointment start time. The operator needs to see the full 3-hour window.
    # schedule_body holds the final confirmed slot (post any race-guard retry).
    if schedule_body and not preserve_pickup_window:
        try:
            appt_start_str = schedule_body["start"]["timestamp"]
            appt_end_str = schedule_body["end"]["timestamp"]
            stop_tz_str = get_stop_timezone(stop_data)
            stop_tz = ZoneInfo(stop_tz_str)
            appt_start_dt = datetime.fromisoformat(appt_start_str.replace("Z", "+00:00")).astimezone(stop_tz)
            appt_end_dt = datetime.fromisoformat(appt_end_str.replace("Z", "+00:00")).astimezone(stop_tz)
            # For range orders: preserve the original planning window (pw_start–pw_end)
            # so the UI shows the dispatcher-visible window, not just the booked slot.
            # The appointment field already shows the booked slot separately.
            # For point-in-time orders: show appt_start–appt_start (exact moment).
            # NOTE: use _is_point_in_time flag directly — _pw_end was expanded for the
            # Tempus availability query so (_pw_end - _pw_start) > 60s even for PIT orders.
            if _is_point_in_time:
                # Point-in-time order — collapse planned window to exact appointment start
                planned_start_dt = appt_start_dt
                planned_end_dt = appt_start_dt
            elif _pw_start and _pw_end:
                # Range order — restore original planning window
                planned_start_dt = _pw_start.astimezone(stop_tz)
                planned_end_dt = _pw_end.astimezone(stop_tz)
            else:
                # Fallback — use appointment start
                planned_start_dt = appt_start_dt
                planned_end_dt = appt_start_dt

            latest = _api_call(f"{base_url}/v2/shipments/{shipment_data['id']}/", "GET", headers=headers)
            stop_idx = next((i for i, s in enumerate(latest.get("stops") or []) if s.get("id") == stop_data.get("id")), None)
            if stop_idx is not None:
                latest["stops"][stop_idx]["planned_date"] = planned_start_dt.strftime("%Y-%m-%d")
                latest["stops"][stop_idx]["planned_time_window_start"] = planned_start_dt.strftime("%H:%M:%S")
                latest["stops"][stop_idx]["planned_time_window_end"] = planned_end_dt.strftime("%H:%M:%S")
                _api_call(f"{base_url}/v2/shipments/{shipment_data['id']}/", "PUT", headers=headers, body=latest)
                sw.log("TRACE",
                    f"Synced stop {stop_data.get('id')} planned window: "
                    f"{planned_start_dt.strftime('%H:%M')} – {planned_end_dt.strftime('%H:%M')} "
                    f"(appointment {appt_start_dt.strftime('%H:%M')} – {appt_end_dt.strftime('%H:%M')})",
                    ["schedule_appointment_for_stop"])
            else:
                sw.log("WARNING",
                    f"Could not find stop {stop_data.get('id')} in shipment — planned window not synced",
                    ["schedule_appointment_for_stop"])
        except Exception as sync_err:
            sw.log("WARNING", f"Failed to sync planned window to appointment: {sync_err}", ["schedule_appointment_for_stop"])
            logger.warning(f"Planned window sync failed (non-fatal): {sync_err}", exc_info=True)
    elif schedule_body and preserve_pickup_window:
        # Unconditionally reassert the derived planning window after a successful booking.
        # Tempus syncs the appointment slot time (e.g. 0.75h) back onto the stop's
        # planned_time_window after scheduling, overwriting the 3-hour derived window we
        # set earlier. Always re-write _effective_pw_start/_pw_end here to restore it,
        # regardless of what the stop currently shows.
        #
        # IMPORTANT: use a stop-level PUT (not a full shipment PUT) so we only touch the
        # planning window fields. A full shipment PUT races with Tempus's own post-booking
        # stop update and can wipe appointment_type/appointment_window if the GET happens
        # before Tempus has synced those fields back onto the stop.
        try:
            _preserve_tz_str = get_stop_timezone(stop_data)
            _preserve_tz = ZoneInfo(_preserve_tz_str)
            if _effective_pw_start and _effective_pw_end:
                _stop_id = stop_data.get("id")
                _latest_stop = _api_call(
                    f"{base_url}/v2/shipments/{shipment_data['id']}/stops/{_stop_id}/",
                    "GET", headers=headers
                ) or {}
                if _latest_stop:
                    _p_start_local = round_down_to_quarter_hour(_effective_pw_start.astimezone(_preserve_tz))
                    _p_end_local = round_down_to_quarter_hour(_effective_pw_end.astimezone(_preserve_tz))
                    _latest_stop["planned_date"] = _p_start_local.strftime("%Y-%m-%d")
                    _latest_stop["planned_time_window_start"] = _p_start_local.strftime("%H:%M:%S")
                    _latest_stop["planned_time_window_end"] = _p_end_local.strftime("%H:%M:%S")
                    _api_call(
                        f"{base_url}/v2/shipments/{shipment_data['id']}/stops/{_stop_id}/",
                        "PUT", headers=headers, body=_latest_stop
                    )
                    logger.info(
                        f"schedule_appointment_for_stop: preserve_pickup_window — reasserted derived window "
                        f"{_p_start_local.strftime('%H:%M')}–{_p_end_local.strftime('%H:%M')} "
                        f"to stop {_stop_id} (stop-level PUT, unconditional post-schedule reassert)"
                    )
                    sw.log("TRACE",
                        f"preserve_pickup_window: reasserted derived window {_p_start_local.strftime('%H:%M')}–{_p_end_local.strftime('%H:%M')} "
                        f"(unconditional post-schedule reassert)",
                        ["schedule_appointment_for_stop"])
        except Exception as _preserve_err:
            logger.warning(f"schedule_appointment_for_stop: preserve_pickup_window reassert failed (non-fatal): {_preserve_err}")

    return True


def restore_pickup_window(
    shipment_data: Dict, pickup_window_calc: Dict,
    base_url: str, headers: Dict, sw: "ShipwellProgram",
) -> None:
    """Re-write the pickup stop's planned window after a scheduling failure.

    Tempus resets the shipment stop's planned_time_window to 00:00-23:59 when it
    cancels a double-booked appointment during the race-guard retry.  This leaves
    the UI showing a full-day placeholder instead of the calculated 3-hour window.
    Call this after auto_schedule_appointments returns False to restore the correct
    window so operators can see the intended pickup time at a glance.
    """
    pickup_stop = pickup_window_calc.get("pickup_stop")
    start_iso = pickup_window_calc.get("start_datetime")
    end_iso = pickup_window_calc.get("end_datetime")
    if not pickup_stop or not start_iso or not end_iso:
        logger.warning("restore_pickup_window: missing pickup_stop or window datetimes — skipping")
        return

    try:
        start_dt = datetime.fromisoformat(start_iso)
        end_dt = datetime.fromisoformat(end_iso)
        stop_tz_str = get_stop_timezone(pickup_stop)
        stop_tz = ZoneInfo(stop_tz_str)
        start_local = round_down_to_quarter_hour(start_dt.astimezone(stop_tz))
        end_local = round_down_to_quarter_hour(end_dt.astimezone(stop_tz))

        latest = _api_call(f"{base_url}/v2/shipments/{shipment_data['id']}/", "GET", headers=headers)
        if not latest:
            logger.warning("restore_pickup_window: could not fetch shipment — skipping")
            return

        stop_id = pickup_stop.get("id")
        stop_idx = next(
            (i for i, s in enumerate(latest.get("stops") or []) if s.get("id") == stop_id),
            None,
        )
        if stop_idx is None:
            logger.warning(f"restore_pickup_window: stop {stop_id} not found in shipment — skipping")
            return

        # For point-in-time orders the end was expanded for the availability query;
        # the planned window display should show start → start (exact time).
        display_end = start_local if pickup_window_calc.get("is_point_in_time") else end_local

        latest["stops"][stop_idx]["planned_date"] = start_local.strftime("%Y-%m-%d")
        latest["stops"][stop_idx]["planned_time_window_start"] = start_local.strftime("%H:%M:%S")
        latest["stops"][stop_idx]["planned_time_window_end"] = display_end.strftime("%H:%M:%S")
        _api_call(f"{base_url}/v2/shipments/{shipment_data['id']}/", "PUT", headers=headers, body=latest)
        sw.log(
            "TRACE",
            f"Restored pickup stop {stop_id} planned window to "
            f"{start_local.strftime('%H:%M')} – {display_end.strftime('%H:%M')} "
            f"after Tempus appointment cancellation reset",
            ["restore_pickup_window"],
        )
        logger.info(
            f"restore_pickup_window: restored stop {stop_id} planned window "
            f"{start_local.strftime('%H:%M')} – {display_end.strftime('%H:%M')} ({stop_tz_str})"
        )
    except Exception as e:
        logger.warning(f"restore_pickup_window: failed ({e})", exc_info=True)


def auto_schedule_appointments(
    shipment_data: Dict, pre_calculated: Optional[Dict],
    base_url: str, headers: Dict, sw: "ShipwellProgram",
    product_category: Optional[str] = None,
) -> bool:
    """Auto schedule pickup and delivery facility stops. Returns False if any required slot fails."""
    if DRY_RUN and shipment_data.get("id") == "dry-run-id":
        latest = shipment_data
    else:
        latest = _api_call(f"{base_url}/v2/shipments/{shipment_data['id']}/", "GET", headers=headers) or shipment_data

    facility_stops = [stop for stop in latest.get("stops") or [] if is_facility_stop(stop)]
    if not facility_stops:
        sw.log("TRACE", "No facility stops found for AutoSchedule", ["auto_schedule_appointments"])
        return True

    appt_error = False
    for stop in facility_stops:
        if pre_calculated and stop.get("id") == (pre_calculated.get("pickup_stop") or {}).get("id"):
            window = (pre_calculated["start_datetime"], pre_calculated["end_datetime"])
        else:
            window = get_stop_window_datetimes(stop)

        if not window:
            sw.log("WARNING", f"No planned window for facility stop {stop.get('id')}", ["auto_schedule_appointments"])
            appt_error = True
            continue

        _pit = pre_calculated.get("is_point_in_time", False) if pre_calculated else False
        _preserve = pre_calculated.get("preserve_pickup_window", False) if pre_calculated else False
        # Point-in-time delivery (3h derived pickup window): use latest-first so the
        # truck departs as close to the delivery constraint as possible.
        # All other window types (range, previously full-day) use earliest-first.
        _force_latest = bool(
            pre_calculated.get("is_point_in_time_delivery", False)
        ) if pre_calculated else False
        if not schedule_appointment_for_stop(latest, stop, window[0], window[1], base_url, headers, sw, product_category=product_category, is_point_in_time=_pit, preserve_pickup_window=_preserve, force_prefer_latest=_force_latest):
            appt_error = True

    return not appt_error


def create_appointment(
    stop_data: Dict, availability_data: Dict, shipment_data: Dict,
    start_dt: str, end_dt: str, base_url: str, headers: Dict, sw: "ShipwellProgram",
) -> bool:
    """
    Find an available slot and schedule the appointment.
    Handles midnight boundary crossing by fetching new availability.
    Mirrors createAppointment() in AppointmentScheduler.gs.
    """
    logger.info("=== create_appointment START ===")
    facility_id = (availability_data.get("load_type_dock_rule_match_results") or {}).get("facility_id")

    shipment_data = _api_call(f"{base_url}/v2/shipments/{shipment_data['id']}/", "GET", headers=headers)

    appt_url = (
        f"{base_url}/facilities/appointments?page=1&limit=10"
        f"&facility_id={facility_id}&scheduled_resource_id={shipment_data['id']}&stop_id={stop_data['id']}"
    )
    existing = _api_call(appt_url, "GET", headers=headers)
    existing_data = existing.get("data") or []

    if existing_data and existing_data[0].get("status") == "SCHEDULED":
        logger.info("Appointment already scheduled - skipping")
        return True

    schedule_body = find_available_slot(availability_data, start_dt, start_dt, appointment_duration_minutes=_ca_appt_duration_minutes)

    if schedule_body and schedule_body.get("needs_new_availability"):
        logger.info("Midnight boundary: fetching previous day availability...")
        new_start_str = schedule_body["new_start_datetime"]
        new_start_dt = datetime.fromisoformat(new_start_str.replace("Z", "+00:00"))

        tz_match = re.search(r"([+-]\d{2}:\d{2})$", start_dt)
        tz_offset = tz_match.group(1) if tz_match else "+00:00"

        day_start = new_start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = new_start_dt.replace(hour=23, minute=59, second=0, microsecond=0)
        new_start_str_fmt = day_start.strftime("%Y-%m-%dT%H:%M:%S") + tz_offset
        new_end_str_fmt = day_end.strftime("%Y-%m-%dT%H:%M:%S") + tz_offset

        new_avail = _api_call(
            f"{base_url}/facilities/{facility_id}/availability",
            "POST", headers=headers,
            body={
                "request_criteria_type": "SHIPMENT",
                "shipment_id": shipment_data["id"],
                "stop_id": stop_data["id"],
                "start_datetime": new_start_str_fmt,
                "end_datetime": new_end_str_fmt,
            },
        )
        if (new_avail.get("available_windows") or []):
            schedule_body = find_available_slot(new_avail, new_start_dt.isoformat(), start_dt, appointment_duration_minutes=_ca_appt_duration_minutes)
        else:
            schedule_body = None

    if not schedule_body or schedule_body.get("needs_new_availability"):
        logger.error("No appointment slot found - shipment remains unscheduled")
        sw.log("WARNING", "No appointment slot available within search window", ["create_appointment"])
        return False

    if not existing_data:
        logger.error("No appointment record found to schedule against")
        return False

    appt_id = existing_data[0]["id"]
    schedule_url = f"{base_url}/facilities/appointments/{appt_id}/schedule"

    if DRY_RUN:
        logger.info(f"[DRY_RUN] Would POST schedule: {json.dumps(schedule_body)}")
        return True

    _api_call(schedule_url, "POST", headers=headers, body=schedule_body)
    logger.info(f"Appointment scheduled: {schedule_body}")
    logger.info("=== create_appointment COMPLETED ===")
    return True


def get_availability(
    shipment_data: Dict, order_data: Dict, pre_calculated: Optional[Dict],
    base_url: str, headers: Dict, sw: "ShipwellProgram",
) -> bool:
    """
    Fetch facility availability and create appointment.
    Uses pre_calculated window if provided; otherwise calculates dynamically.
    Retries 1 hour earlier if no windows found.
    Mirrors getAvailibility() in AppointmentScheduler.gs.
    """
    logger.info("=== get_availability START ===")
    stops = shipment_data.get("stops") or []
    dropoff_stop = get_dropoff_stop(stops)
    if not dropoff_stop:
        logger.error("No dropoff stop found")
        return False

    if pre_calculated:
        start_dt = pre_calculated["start_datetime"]
        end_dt = pre_calculated["end_datetime"]
        pickup_stop = pre_calculated["pickup_stop"]
    else:
        pickup_stop = get_pickup_stop(stops)
        if not pickup_stop:
            logger.error("No pickup stop found")
            return False
        pickup_fid = (pickup_stop.get("location") or {}).get("facility_id")
        load_time = get_facility_load_time(pickup_fid, order_data, base_url, headers) if pickup_fid else DEFAULT_LOAD_TIME
        dropoff_tz = get_stop_timezone(dropoff_stop)
        start_dt = calc_load_travel_time(shipment_data, dropoff_stop.get("planned_date", ""), dropoff_stop.get("planned_time_window_start", ""), dropoff_tz, load_time)
        end_dt = calc_load_travel_time(shipment_data, dropoff_stop.get("planned_date", ""), dropoff_stop.get("planned_time_window_end", ""), dropoff_tz, load_time)

    facility_id = (pickup_stop.get("location") or {}).get("facility_id")
    if not facility_id:
        logger.error("No facility_id on pickup stop")
        return False

    try:
        early_start = datetime.fromisoformat(start_dt.replace("Z", "+00:00")) - timedelta(hours=3)
        avail_query_start = early_start.isoformat()
    except Exception:
        avail_query_start = start_dt

    # Resolve appointment duration from the facility's matched load type.
    # Used for availability query padding and slot end calculation.
    _ca_appt_duration_minutes: Optional[int] = None
    try:
        _ca_lt_resp = _api_call(f"{base_url}/facilities/{facility_id}/load-types", "GET", headers=headers)
        _ca_lt_list = _ca_lt_resp.get("data") or []
        _ca_order_items = (order_data or {}).get("items") or [] if order_data else []
        _ca_first_item = _ca_order_items[0] if _ca_order_items else {}
        _ca_product_id = (_ca_first_item.get("shipping_requirements") or {}).get("product_id")
        _ca_product_category = None
        if _ca_product_id:
            try:
                _ca_pd = _api_call(f"{base_url}/v2/products/{_ca_product_id}/", "GET", headers=headers)
                _ca_product_category = _ca_pd.get("category")
            except Exception:
                pass
        if _ca_lt_list:
            _ca_matched_lt = None
            if _ca_product_category:
                _cat_lower = _ca_product_category.lower()
                def _ca_cat_matches(lt: dict) -> bool:
                    lt_name = (lt.get("name") or "").lower()
                    lt_cats = [c.lower() for c in (lt.get("product_category") or [])]
                    # Priority 1: structured product_category list — checked first so that
                    # a specific category like "WAX FREE OILS" doesn't spuriously match
                    # a shorter-named load type like "Wax" via name substring.
                    if lt_cats:
                        return any(c == _cat_lower or c in _cat_lower or _cat_lower in c for c in lt_cats)
                    # Priority 2: name substring only when no product_category list populated
                    return bool(lt_name and (_cat_lower in lt_name or lt_name in _cat_lower))
                _ca_matched_lt = next(
                    (lt for lt in _ca_lt_list if _ca_cat_matches(lt)),
                    None,
                )
            if not _ca_matched_lt:
                _ca_matched_lt = _ca_lt_list[0]
            if _ca_matched_lt and _ca_matched_lt.get("appointment_duration"):
                _ca_parsed = parse_iso8601_duration(_ca_matched_lt["appointment_duration"])
                if _ca_parsed and _ca_parsed > 0:
                    _ca_appt_duration_minutes = int(round(_ca_parsed * 60))
                    logger.info(f"create_appointment/get_availability: load type {_ca_matched_lt.get('name')!r} appointment_duration → {_ca_appt_duration_minutes}min")
    except Exception as _ca_dur_err:
        logger.warning(f"create_appointment/get_availability: could not resolve load type duration: {_ca_dur_err}")

    # Extend end_datetime by appointment duration so Tempus returns windows that
    # include slots starting at the planned window end (end_dt is latest *start*, not
    # latest *finish*). Mirrors the fix in schedule_appointment_for_stop.
    try:
        avail_end_obj = datetime.fromisoformat(end_dt.replace("Z", "+00:00")) + timedelta(minutes=_ca_appt_duration_minutes or APPOINTMENT_DURATION_MINUTES)
        avail_end_dt = avail_end_obj.isoformat()
    except Exception:
        avail_end_dt = end_dt

    avail_body = {
        "request_criteria_type": "SHIPMENT",
        "shipment_id": shipment_data["id"],
        "stop_id": pickup_stop["id"],
        "start_datetime": avail_query_start,
        "end_datetime": avail_end_dt,
    }

    availability_data = _api_call(f"{base_url}/facilities/{facility_id}/availability", "POST", headers=headers, body=avail_body)

    retry = 0
    while not (availability_data.get("available_windows") or []) and retry < 12:
        retry += 1
        logger.warning(f"No availability windows - shifting 1 hour earlier (attempt {retry})")
        try:
            start_obj = datetime.fromisoformat(avail_body["start_datetime"].replace("Z", "+00:00"))
            new_start = start_obj - timedelta(hours=1)
            avail_body["end_datetime"] = avail_body["start_datetime"]
            avail_body["start_datetime"] = new_start.isoformat()
            start_dt = avail_body["start_datetime"]
            end_dt = avail_body["end_datetime"]
        except Exception as e:
            logger.error(f"Availability retry shift failed: {e}")
            break
        availability_data = _api_call(f"{base_url}/facilities/{facility_id}/availability", "POST", headers=headers, body=avail_body)

    created = create_appointment(pickup_stop, availability_data, shipment_data, start_dt, end_dt, base_url, headers, sw)
    logger.info("=== get_availability COMPLETED ===")
    return created


def set_specific_appointment(
    shipment_data: Dict, appt_start: str, appt_end: str, dock_id: Optional[str],
    load_type: Optional[str], base_url: str, headers: Dict, sw: "ShipwellProgram",
) -> Optional[Dict]:
    """
    Schedule a specific appointment time (turn orders bypass availability search).
    Handles point-in-time (start==end) by adding 1hr buffer to end.
    Mirrors setSpecificAppointment() in AppointmentScheduler.gs.
    """
    logger.info("=== set_specific_appointment START ===")

    if appt_start == appt_end:
        logger.info("Point-in-time appointment - adding 1hr buffer to end")
        try:
            end_dt = datetime.fromisoformat(appt_end.replace("Z", "+00:00"))
            end_dt = end_dt + timedelta(hours=1)
            tz_match = re.search(r"([+-]\d{2}:\d{2})$", appt_end)
            tz_offset = tz_match.group(1) if tz_match else "+00:00"
            appt_end = end_dt.strftime("%Y-%m-%dT%H:%M:%S") + tz_offset
        except Exception as e:
            logger.warning(f"Point-in-time buffer failed: {e}")

    try:
        pickup_stop = get_pickup_stop(shipment_data.get("stops") or [])
        if not pickup_stop:
            raise ValueError("No pickup stop on shipment")

        facility_id = (pickup_stop.get("location") or {}).get("facility_id")
        if not facility_id:
            raise ValueError("No facility_id on pickup stop")

        matched_load_type_id = None
        if load_type:
            try:
                lt_resp = _api_call(f"{base_url}/facilities/{facility_id}/load-types", "GET", headers=headers)
                lt_data = lt_resp.get("data") or []
                matched_lt = next((lt for lt in lt_data if (lt.get("name") or "").lower() == load_type.lower()), None)
                if matched_lt:
                    matched_load_type_id = matched_lt.get("id")
            except Exception as e:
                logger.warning(f"Failed to fetch load types: {e}")

        shipment_data = _api_call(f"{base_url}/v2/shipments/{shipment_data['id']}/", "GET", headers=headers)

        appt_check_url = (
            f"{base_url}/facilities/appointments?page=1&limit=10"
            f"&facility_id={facility_id}&scheduled_resource_id={shipment_data['id']}&stop_id={pickup_stop['id']}"
        )
        existing = _api_call(appt_check_url, "GET", headers=headers)
        existing_data = existing.get("data") or []

        selected_dock_id = dock_id

        if not existing_data:
            avail_body = {
                "request_criteria_type": "SHIPMENT",
                "shipment_id": shipment_data["id"],
                "stop_id": pickup_stop["id"],
                "start_datetime": appt_start,
                "end_datetime": appt_end,
            }
            avail_data = _api_call(f"{base_url}/facilities/{facility_id}/availability", "POST", headers=headers, body=avail_body)

            if not selected_dock_id:
                windows = avail_data.get("available_windows") or []
                by_appt = next((w for w in windows if w.get("appointment_type") == "BY_APPOINTMENT"), None)
                selected_dock_id = by_appt["dock_id"] if by_appt else (windows[0]["dock_id"] if windows else None)

            existing = _api_call(appt_check_url, "GET", headers=headers)
            existing_data = existing.get("data") or []
            if not existing_data:
                raise RuntimeError("Failed to create appointment record via availability")

        if existing_data[0].get("status") == "SCHEDULED":
            logger.info("Appointment already scheduled - skipping")
            return existing_data[0]

        appt_id = existing_data[0]["id"]

        if not selected_dock_id:
            avail_data2 = _api_call(
                f"{base_url}/facilities/{facility_id}/availability", "POST", headers=headers,
                body={"request_criteria_type": "SHIPMENT", "shipment_id": shipment_data["id"],
                      "stop_id": pickup_stop["id"], "start_datetime": appt_start, "end_datetime": appt_end},
            )
            windows = avail_data2.get("available_windows") or []
            if windows:
                by_appt = next((w for w in windows if w.get("appointment_type") == "BY_APPOINTMENT"), None)
                selected_dock_id = by_appt["dock_id"] if by_appt else windows[0]["dock_id"]
            else:
                raise RuntimeError("No dock_id available and no availability windows")

        tz_str = get_stop_timezone(pickup_stop)
        schedule_body: Dict[str, Any] = {
            "start": {"timestamp": appt_start, "timezone": tz_str},
            "end": {"timestamp": appt_end, "timezone": tz_str},
            "dock_id": selected_dock_id,
        }
        if matched_load_type_id:
            schedule_body["matched_load_type_id"] = matched_load_type_id

        schedule_url = f"{base_url}/facilities/appointments/{appt_id}/schedule"

        if DRY_RUN:
            logger.info(f"[DRY_RUN] Would POST schedule: {json.dumps(schedule_body)}")
            return None

        result = _api_call(schedule_url, "POST", headers=headers, body=schedule_body)
        logger.info(f"Appointment scheduled successfully: {appt_id}")
        logger.info("=== set_specific_appointment COMPLETED ===")
        return result

    except Exception as e:
        logger.error(f"set_specific_appointment error: {e}", exc_info=True)
        sw.log("WARNING", f"Appointment scheduling failed: {e}", ["set_specific_appointment"])
        return None


def _restore_planned_windows_from_appointments_UNUSED(
    shipment_data: Dict, base_url: str, headers: Dict, sw: "ShipwellProgram",
) -> bool:
    """UNUSED — kept for reference. Backed out 2026-07-24: Tempus intentionally resets
    planned_time_window when rescheduling; this is not a bug to fix on our side.
    Original intent: Detect and repair planned windows reset to 00:00:00 by Tempus after a manual reschedule.

    When a user drags an appointment to a new time in the Tempus calendar, Tempus syncs
    the change back to Shipwell which sets the stop's planned_time_window_start/end to
    00:00:00.  This function detects that pattern and restores the planned window from
    the actual SCHEDULED Tempus appointment for each affected facility stop.

    Returns True if at least one stop was repaired, False otherwise.
    """
    stops = shipment_data.get("stops") or []
    facility_stops = [s for s in stops if (s.get("location") or {}).get("facility_id")]
    if not facility_stops:
        logger.info("restore_planned_windows_from_appointments: no facility stops — nothing to do")
        return False

    repaired_any = False
    # We do at most one full-shipment PUT; accumulate all stop patches first.
    try:
        latest = _api_call(f"{base_url}/v2/shipments/{shipment_data['id']}/", "GET", headers=headers) or shipment_data
    except Exception as e:
        logger.warning(f"restore_planned_windows_from_appointments: could not refresh shipment: {e}")
        latest = shipment_data

    needs_put = False
    for stop in facility_stops:
        stop_id = stop.get("id")
        facility_id = (stop.get("location") or {}).get("facility_id")
        window_start = stop.get("planned_time_window_start") or ""
        window_end   = stop.get("planned_time_window_end") or ""

        # Detect the Tempus reset pattern: time is exactly 00:00:00 (or close).
        # We only repair when BOTH start and end are zeroed — a deliberate midnight
        # appointment would be unusual but we don't want to over-correct.
        start_is_zeroed = window_start.startswith("00:00")
        end_is_zeroed   = window_end.startswith("00:00") or window_end.startswith("23:59")
        if not (start_is_zeroed and end_is_zeroed):
            logger.info(
                f"restore_planned_windows_from_appointments: stop {stop_id} window "
                f"{window_start}–{window_end} not zeroed — skipping"
            )
            continue

        # Look up the actual Tempus appointment for this stop.
        try:
            appt_resp = _api_call(
                f"{base_url}/facilities/appointments?page=1&limit=5"
                f"&facility_id={facility_id}"
                f"&scheduled_resource_id={shipment_data['id']}"
                f"&stop_id={stop_id}",
                "GET", headers=headers,
            )
            appts = (appt_resp or {}).get("data") or []
        except Exception as e:
            logger.warning(f"restore_planned_windows_from_appointments: appointment lookup failed for stop {stop_id}: {e}")
            continue

        scheduled = next((a for a in appts if (a.get("status") or "").upper() == "SCHEDULED"), None)
        if not scheduled:
            logger.info(
                f"restore_planned_windows_from_appointments: stop {stop_id} has zeroed window "
                f"but no SCHEDULED appointment — leaving as-is (may need manual fix)"
            )
            continue

        # Extract appointment start/end times.
        sched_start = (scheduled.get("start") or {}).get("timestamp") or ""
        sched_end   = (scheduled.get("end") or {}).get("timestamp") or ""
        if not sched_start:
            logger.warning(f"restore_planned_windows_from_appointments: SCHEDULED appointment {scheduled.get('id')} has no start timestamp — skipping")
            continue

        try:
            stop_tz_str = (stop.get("location") or {}).get("timezone") or DEFAULT_SCHEDULING_TIMEZONE
            stop_tz = ZoneInfo(stop_tz_str)
            appt_start_dt = datetime.fromisoformat(sched_start.replace("Z", "+00:00")).astimezone(stop_tz)
            appt_end_dt   = datetime.fromisoformat(sched_end.replace("Z", "+00:00")).astimezone(stop_tz) if sched_end else appt_start_dt
        except Exception as e:
            logger.warning(f"restore_planned_windows_from_appointments: could not parse appointment times for stop {stop_id}: {e}")
            continue

        # Restore: planned window = appointment start–end (or start–start for PIT).
        # Round to nearest quarter-hour for display consistency.
        planned_start_local = round_down_to_quarter_hour(appt_start_dt)
        planned_end_local   = round_down_to_quarter_hour(appt_end_dt)

        stop_idx = next(
            (i for i, s in enumerate(latest.get("stops") or []) if s.get("id") == stop_id),
            None,
        )
        if stop_idx is None:
            logger.warning(f"restore_planned_windows_from_appointments: stop {stop_id} not found in latest shipment")
            continue

        latest["stops"][stop_idx]["planned_date"] = planned_start_local.strftime("%Y-%m-%d")
        latest["stops"][stop_idx]["planned_time_window_start"] = planned_start_local.strftime("%H:%M:%S")
        latest["stops"][stop_idx]["planned_time_window_end"]   = planned_end_local.strftime("%H:%M:%S")
        needs_put = True
        repaired_any = True

        sw.log(
            "INFO",
            f"restore_planned_windows_from_appointments: repaired stop {stop_id} "
            f"(was 00:00 → restored to {planned_start_local.strftime('%H:%M')}–{planned_end_local.strftime('%H:%M')} "
            f"from appointment {scheduled.get('id')})",
            ["restore_planned_windows_from_appointments"],
        )
        logger.info(
            f"restore_planned_windows_from_appointments: stop {stop_id} restored "
            f"{planned_start_local.strftime('%H:%M')}–{planned_end_local.strftime('%H:%M')} "
            f"from appointment {scheduled.get('id')}"
        )

    if needs_put and not DRY_RUN:
        try:
            from shipwell_client import safe_update_shipment
            safe_update_shipment(latest, base_url, headers, "Restore planned windows after Tempus reschedule")
        except Exception as e:
            logger.warning(f"restore_planned_windows_from_appointments: PUT failed: {e}")
            sw.log("WARNING", f"Failed to write repaired planned windows: {e}", ["restore_planned_windows_from_appointments"])
    elif needs_put and DRY_RUN:
        logger.info("[DRY_RUN] Would PUT repaired planned windows to shipment")

    return repaired_any


# ---------------------------------------------------------------------------
# Appointment cancel / reschedule helpers
# (used by order-update; available to create-shipment if needed)
# ---------------------------------------------------------------------------

def get_scheduled_appointment_for_stop(
    shipment_id: str,
    stop_id: str,
    facility_id: str,
    base_url: str,
    headers: Dict,
) -> Optional[Dict]:
    """Return the first SCHEDULED Tempus appointment for the given stop, or None."""
    try:
        resp = _api_call(
            f"{base_url}/facilities/appointments"
            f"?page=1&limit=5"
            f"&facility_id={facility_id}"
            f"&scheduled_resource_id={shipment_id}"
            f"&stop_id={stop_id}",
            "GET", headers=headers,
        )
        for appt in (resp or {}).get("data") or []:
            if (appt.get("status") or "").upper() == "SCHEDULED":
                return appt
    except Exception as e:
        logger.warning(f"get_scheduled_appointment_for_stop: lookup failed stop={stop_id}: {e}")
    return None


def cancel_stop_appointments(
    shipment_id: str,
    stop_id: str,
    facility_id: str,
    label: str,
    ref_id: str,
    base_url: str,
    headers: Dict,
    sw: "ShipwellProgram",
) -> bool:
    """Find any SCHEDULED/UNSCHEDULED appointments for this stop and cancel them.

    Returns True if at least one appointment was cancelled.
    Cancels both SCHEDULED and stale UNSCHEDULED stubs so a fresh rebook
    gets a clean Tempus dock assignment.
    """
    appt_url = (
        f"{base_url}/facilities/appointments"
        f"?page=1&limit=10"
        f"&facility_id={facility_id}"
        f"&scheduled_resource_id={shipment_id}"
        f"&stop_id={stop_id}"
    )

    try:
        resp = _api_call(appt_url, "GET", headers=headers)
    except Exception as e:
        logger.warning(f"  {ref_id} {label}: appointment lookup failed: {e} - skipping cancellation")
        sw.log("WARNING", f"{label} appointment lookup failed: {e}", ["cancel_stop_appointments"])
        return False

    appt_list = (resp or {}).get("data") or []
    scheduled = [a for a in appt_list if (a.get("status") or "").upper() == "SCHEDULED"]
    unscheduled = [
        a for a in appt_list
        if (a.get("status") or "").upper() not in ("SCHEDULED", "CANCELLED")
    ]

    if not scheduled and not unscheduled:
        logger.info(f"  {ref_id} {label}: no appointments to cancel")
        return False

    any_cancelled = False
    for appt in scheduled + unscheduled:
        appt_id = appt.get("id", "")
        appt_status = (appt.get("status") or "").upper()
        logger.info(f"  {ref_id} {label}: cancelling appointment {appt_id} (status={appt_status})")
        sw.log("TRACE", f"Cancelling {label} appointment {appt_id} (status={appt_status}) for {ref_id}", ["cancel_stop_appointments"])

        if DRY_RUN:
            logger.info(f"[DRY_RUN] Would POST /facilities/appointments/{appt_id}/cancel")
            any_cancelled = True
            continue

        try:
            _api_call(
                f"{base_url}/facilities/appointments/{appt_id}/cancel",
                "POST", headers=headers,
                body={"reason": "Planning window updated via order update"},
            )
            logger.info(f"  {ref_id} {label}: appointment {appt_id} cancelled")
            sw.log("INFO", f"{label} appointment {appt_id} cancelled for {ref_id}", ["cancel_stop_appointments"])
            any_cancelled = True
        except Exception as e:
            logger.warning(f"  {ref_id} {label}: failed to cancel appointment {appt_id}: {e}")
            sw.log("WARNING", f"Failed to cancel {label} appointment {appt_id}: {e}", ["cancel_stop_appointments"])

    return any_cancelled


def reschedule_appointment(
    appointment_id: str,
    start_utc: str,
    end_utc: str,
    tz_str: str,
    label: str,
    ref_id: str,
    base_url: str,
    headers: Dict,
    sw: "ShipwellProgram",
    dock_id: Optional[str] = None,
) -> bool:
    """Reschedule an existing Tempus appointment to a new time slot.

    Uses POST /facilities/appointments/{appointment_id}/reschedule.
    Preferred over cancel+rebook when we want to preserve dock/metadata.
    Returns True on success, False on failure.
    """
    if DRY_RUN:
        logger.info(
            f"[DRY_RUN] Would POST /facilities/appointments/{appointment_id}/reschedule "
            f"start={start_utc} end={end_utc} tz={tz_str}"
        )
        return True

    body: Dict[str, Any] = {
        "start": {"timestamp": start_utc, "timezone": tz_str},
        "end":   {"timestamp": end_utc,   "timezone": tz_str},
    }
    if dock_id:
        body["dock_id"] = dock_id

    try:
        _api_call(
            f"{base_url}/facilities/appointments/{appointment_id}/reschedule",
            "POST", headers=headers, body=body,
        )
        logger.info(
            f"  {ref_id} {label}: rescheduled appointment {appointment_id} "
            f"to {start_utc}-{end_utc} (tz={tz_str})"
        )
        sw.log(
            "INFO",
            f"{label} appointment {appointment_id} rescheduled to {start_utc}-{end_utc} for {ref_id}",
            ["reschedule_appointment"],
        )
        return True
    except Exception as e:
        logger.warning(f"  {ref_id} {label}: failed to reschedule appointment {appointment_id}: {e}")
        sw.log("WARNING", f"Failed to reschedule {label} appointment {appointment_id}: {e}", ["reschedule_appointment"])
        return False
