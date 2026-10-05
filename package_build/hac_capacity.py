"""HAC Asphalt Google Sheet capacity helpers."""

import logging
import re
from copy import deepcopy
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from config import HAC_ASSIGNMENT_SHEET_IDS, HAC_ASPHALT_EQUIPMENT
from hfs_utils import normalize_custom_field_text

logger = logging.getLogger()


class CapacityError(RuntimeError):
    """Raised when HAC capacity cannot be evaluated from the sheet data."""


def get_assignment_sheet_id(location_id: str, sheet_ids: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Return the configured carrier assignment sheet id for a HAC location."""
    mapping = sheet_ids if sheet_ids is not None else HAC_ASSIGNMENT_SHEET_IDS
    return mapping.get(str(location_id).strip())


def normalize_header(value: Any) -> str:
    """Normalize a sheet header for loose matching."""
    return re.sub(r"[^A-Z0-9]+", "", normalize_custom_field_text(value).upper())


def parse_number(value: Any, default: float = 0.0) -> float:
    """Parse a spreadsheet numeric cell."""
    text = normalize_custom_field_text(value).replace(",", "")
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def format_number(value: float) -> Any:
    """Return an int when possible so sheet updates stay tidy."""
    return int(value) if float(value).is_integer() else value


# Google Sheets date serial epoch: Dec 30, 1899 (accounts for the Lotus 1900 leap-year bug)
_SHEETS_EPOCH = date(1899, 12, 30)


def normalize_date_value(value: Any) -> str:
    """Normalize common sheet/API date values to YYYY-MM-DD where possible."""
    # Handle Google Sheets serial numbers (returned when valueRenderOption=UNFORMATTED_VALUE)
    # Sheets encodes dates as integer days since Dec 30, 1899.
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        serial = int(value)
        if 1 <= serial <= 2958465:  # sane range: 1900-01-01 to 9999-12-31
            try:
                from datetime import timedelta
                return (_SHEETS_EPOCH + timedelta(days=serial)).isoformat()
            except (ValueError, OverflowError):
                pass

    text = normalize_custom_field_text(value)
    if not text:
        return ""
    # Also handle numeric strings that are serial numbers
    try:
        serial = int(text)
        if 1 <= serial <= 2958465:
            from datetime import timedelta
            return (_SHEETS_EPOCH + timedelta(days=serial)).isoformat()
    except (ValueError, TypeError):
        pass
    # Standard formats
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%-m/%-d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    # Sheet row date format: "Fri 05/29", "Mon 05/27" — no year, assume current year
    # Strip leading day-of-week label if present (e.g. "Fri 05/29" -> "05/29")
    dow_stripped = re.sub(r'^[A-Za-z]{3}\s+', '', text)
    if re.match(r'^\d{1,2}/\d{1,2}$', dow_stripped):
        current_year = datetime.now().year
        try:
            return datetime.strptime(f"{dow_stripped}/{current_year}", "%m/%d/%Y").date().isoformat()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return text


def requested_date_key(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return normalize_date_value(value)


def find_date_row(rows: List[List[Any]], requested_date: Any, date_col: int = 0) -> int:
    """Find the zero-based row index matching the requested assignment date."""
    wanted = requested_date_key(requested_date)
    for index, row in enumerate(rows):
        if date_col < len(row) and normalize_date_value(row[date_col]) == wanted:
            return index
    raise CapacityError(f"Capacity row not found for date {wanted}")


def find_column(headers: List[Any], candidates: Tuple[str, ...]) -> int:
    """Find the first header column matching any normalized candidate."""
    normalized_candidates = tuple(normalize_header(candidate) for candidate in candidates)
    for index, header in enumerate(headers):
        normalized = normalize_header(header)
        if normalized in normalized_candidates:
            return index
    for index, header in enumerate(headers):
        normalized = normalize_header(header)
        if any(candidate in normalized or normalized in candidate for candidate in normalized_candidates):
            return index
    raise CapacityError(f"Column not found for candidates: {', '.join(candidates)}")


def detect_capacity_columns(header_row: List[Any], section_row: List[Any], equipment_names: Optional[List[str]] = None) -> Dict[str, int]:
    """
    Detect equipment available and consumed column indices from the SCAC tab.

    The sheet has a fixed 3-row header structure:
      row 0: location label (ignored)
      row 1: section groupings — 'Capacity', 'Consumed', 'Available' (merged across cols)
      row 2: column names — 'Day Of Week', '# Drivers', 'Blue Dot', 'Green Dot', 'Asphalt' (repeated per section)

    Column layout (0-indexed):
      0: Date
      1-4:  Capacity  — # Drivers, Blue Dot, Green Dot, Asphalt  (read-only, set by humans)
      5-8:  Consumed  — # Drivers, Blue Dot, Green Dot, Asphalt  (Lambda writes equipment cols 6-8)
      9-12: Available — # Drivers, Blue Dot, Green Dot, Asphalt  (formula; Lambda reads cols 9-12)

    Because column names repeat across sections, we resolve positions by finding the
    section boundaries in section_row first, then locating equipment within each section.
    """
    equipment_names = equipment_names or HAC_ASPHALT_EQUIPMENT

    # Find the column index where each section starts by scanning section_row
    section_starts: Dict[str, int] = {}
    for col_idx, cell in enumerate(section_row):
        label = normalize_header(cell)
        if label and label not in section_starts:
            section_starts[label] = col_idx

    consumed_start = section_starts.get("CONSUMED")
    available_start = section_starts.get("AVAILABLE")
    if consumed_start is None or available_start is None:
        raise CapacityError(
            f"Could not find Consumed/Available sections in section row: {section_row}"
        )

    # Within each section the sub-columns are: # Drivers, then one col per equipment type.
    # We detect column positions by scanning the header_row within each section boundary,
    # rather than assuming positional offsets — this handles mismatches where Consumed and
    # Available may have different numbers of equipment columns (e.g. no General Emulsion
    # column in Available).
    columns: Dict[str, int] = {
        "driver_available": available_start,  # Available # Drivers — checked but not written
    }

    # Build a name→col_index map for each section from the actual header row
    def section_col_map(section_start: int, next_section_start: int) -> Dict[str, int]:
        result: Dict[str, int] = {}
        for col_idx in range(section_start, min(next_section_start, len(header_row))):
            name = normalize_equipment_key(header_row[col_idx])
            if name and name not in result:
                result[name] = col_idx
        return result

    # Determine section boundaries: consumed → available → end of row
    capacity_start = section_starts.get("CAPACITY", 1)
    consumed_cols = section_col_map(consumed_start, available_start)
    available_cols = section_col_map(available_start, len(header_row) + 1)

    for equipment_name in (equipment_names or HAC_ASPHALT_EQUIPMENT):
        key = normalize_equipment_key(equipment_name)
        if key in consumed_cols:
            columns[f"{key}_consumed"] = consumed_cols[key]
        if key in available_cols:
            columns[f"{key}_available"] = available_cols[key]

    return columns


def normalize_equipment_key(equipment_name: str) -> str:
    return normalize_header(equipment_name).lower()


# Map Shipwell equipment types that don't directly match sheet column names to the
# correct HAC sheet bucket.  All HFS tanker loads are asphalt products.
_EQUIPMENT_TYPE_TO_SHEET_BUCKET: Dict[str, str] = {
    "TANKER": "Asphalt",
    "MULTI_COMPARTMENT": "Asphalt",
    "MULTI_COMPARTMENT_TANKER": "Asphalt",
    "MULTICOMPARTMENT": "Asphalt",
    # Shipwell equipment types that map directly to sheet columns (title-case names
    # normalized to UPPER_UNDERSCORE by the caller before lookup)
    "ASPHALT": "Asphalt",
    "BLUE_DOT": "Blue Dot",
    "GREEN_DOT": "Green Dot",
    "EMULSION": "Emulsion",
    # Product category plural/variant forms (from Shipwell line items / custom fields)
    "ASPHALTS": "Asphalt",
    "BLUE DOTS": "Blue Dot",
    "BLUEDOTS": "Blue Dot",
    "GREEN DOTS": "Green Dot",
    "GREENDOTS": "Green Dot",
    "EMULSIONS": "Emulsion",
    "GENERAL_EMULSION": "Emulsion",
    "GENERAL EMULSION": "Emulsion",
}


def choose_equipment_for_capacity(
    row: List[Any], columns: Dict[str, int], requested_equipment: str,
    equipment_order: Optional[List[str]] = None,
) -> Optional[str]:
    """Return the equipment bucket to consume, allowing Emulsion Blue/Green fallback."""
    requested = normalize_custom_field_text(requested_equipment)
    # Remap equipment types that don't match sheet column names directly.
    bucket = _EQUIPMENT_TYPE_TO_SHEET_BUCKET.get(requested.upper())
    if bucket is not None:
        requested = bucket
    if requested.upper() == "EMULSION":
        candidates = [
            name for name in (equipment_order or HAC_ASPHALT_EQUIPMENT)
            if normalize_equipment_key(name) in ("bluedot", "greendot", "generalemulsion")
        ]
    else:
        candidates = [requested]

    for candidate in candidates:
        available_col = columns.get(f"{normalize_equipment_key(candidate)}_available")
        if available_col is None:
            continue
        if available_col < len(row) and parse_number(row[available_col]) > 0:
            return candidate
    return None


def has_driver_capacity(row: List[Any], columns: Dict[str, int]) -> bool:
    driver_available_col = columns["driver_available"]
    return driver_available_col < len(row) and parse_number(row[driver_available_col]) > 0


def ensure_row_width(row: List[Any], column_index: int) -> List[Any]:
    widened = list(row)
    while len(widened) <= column_index:
        widened.append("")
    return widened


def increment_cell(row: List[Any], column_index: int, amount: float = 1.0) -> List[Any]:
    updated = ensure_row_width(row, column_index)
    updated[column_index] = format_number(parse_number(updated[column_index]) + amount)
    return updated


def decrement_cell(row: List[Any], column_index: int, amount: float = 1.0) -> List[Any]:
    updated = ensure_row_width(row, column_index)
    updated[column_index] = format_number(max(0.0, parse_number(updated[column_index]) - amount))
    return updated


def consume_capacity_row(
    row: List[Any], columns: Dict[str, int], requested_equipment: str,
    equipment_order: Optional[List[str]] = None,
) -> Tuple[List[Any], str]:
    """
    Increment the Consumed column for the selected equipment type.
    Only the single consumed cell is returned for writing — formulas handle
    Available and the Consumed # Drivers total, so we must not overwrite them.
    Returns (consumed_col_index, new_value, selected_equipment).
    """
    if not has_driver_capacity(row, columns):
        raise CapacityError("Driver capacity unavailable")
    selected_equipment = choose_equipment_for_capacity(row, columns, requested_equipment, equipment_order)
    if not selected_equipment:
        raise CapacityError(f"Equipment capacity unavailable for {requested_equipment}")

    equipment_key = normalize_equipment_key(selected_equipment)
    consumed_col = columns[f"{equipment_key}_consumed"]
    new_value = format_number(parse_number(row[consumed_col] if consumed_col < len(row) else 0) + 1.0)
    return consumed_col, new_value, selected_equipment


def return_capacity_row(row: List[Any], columns: Dict[str, int], consumed_equipment: str) -> List[Any]:
    """
    Decrement the Consumed column for the equipment type on a rejected/removed tender.

    Only the equipment Consumed column is written — mirrors consume_capacity_row.
    Applies the same bucket remapping as choose_equipment_for_capacity so that
    plural product category names (e.g. ASPHALTS) resolve to the correct sheet
    column (e.g. Asphalt).
    """
    # Apply bucket remapping (same as choose_equipment_for_capacity) before key lookup
    from hfs_utils import normalize_custom_field_text
    remapped = _EQUIPMENT_TYPE_TO_SHEET_BUCKET.get(normalize_custom_field_text(consumed_equipment).upper())
    resolved_equipment = remapped if remapped is not None else consumed_equipment
    equipment_key = normalize_equipment_key(resolved_equipment)
    consumed_col = columns.get(f"{equipment_key}_consumed")
    if consumed_col is None:
        raise CapacityError(f"Consumed column not found for {consumed_equipment}")
    # Only decrement the consumed cell — Available is formula-driven
    new_value = format_number(max(0.0, parse_number(row[consumed_col] if consumed_col < len(row) else 0) - 1.0))
    return consumed_col, new_value


def column_letter(column_index: int) -> str:
    """Convert a zero-based column index to an A1 notation column letter."""
    if column_index < 0:
        raise ValueError("column_index must be >= 0")
    result = ""
    current = column_index
    while True:
        current, remainder = divmod(current, 26)
        result = chr(ord("A") + remainder) + result
        if current == 0:
            return result
        current -= 1


def row_update_range(tab_name: str, row_index: int, start_col: int = 0, end_col: Optional[int] = None) -> str:
    """Build an A1 notation range for writing a full row back."""
    row_number = row_index + 1
    start_letter = column_letter(start_col)
    if end_col is None:
        return f"'{tab_name}'!{start_letter}{row_number}:{row_number}"
    return f"'{tab_name}'!{start_letter}{row_number}:{column_letter(end_col)}{row_number}"


def check_sheet_capacity(
    scac: str,
    location_id: str,
    appointment_date: str,
    product_category: str,
    sheets_client: Any,
) -> bool:
    """
    Check whether a carrier (by SCAC) has remaining capacity on the HAC sheet
    for the given location, date, and product category.

    Returns True if capacity is available, False if exhausted.
    Raises CapacityError if the sheet cannot be read or is malformed.

    This is a read-only check — does not consume any capacity.
    """
    sheet_id = get_assignment_sheet_id(location_id)
    if not sheet_id:
        raise CapacityError(f"No assignment sheet configured for location {location_id}")

    range_notation = f"'{scac}'!A:Z"
    try:
        rows = sheets_client.get_values(sheet_id, range_notation)
    except Exception as e:
        raise CapacityError(f"Failed to read sheet for carrier {scac}: {e}") from e

    if len(rows) < 4:
        raise CapacityError(f"Sheet for {scac} has too few rows (expected >=4, got {len(rows)})")

    section_row = rows[1]
    header_row = rows[2]
    columns = detect_capacity_columns(header_row, section_row)
    row_index = find_date_row(rows[3:], appointment_date)
    row = rows[3 + row_index]

    # Driver capacity check
    if not has_driver_capacity(row, columns):
        logger.info(f"check_sheet_capacity: {scac} has no driver capacity for {appointment_date}")
        return False

    # Equipment/product capacity check
    equipment_key = normalize_equipment_key(product_category)
    avail_col = columns.get(f"{equipment_key}_available")
    if avail_col is None:
        avail_col = columns.get("asphalt_available")
    if avail_col is None:
        raise CapacityError(f"Sheet for {scac} has no available column for product_category {product_category!r}")

    available = parse_number(row[avail_col] if avail_col < len(row) else 0)
    logger.info(f"check_sheet_capacity: {scac} {product_category} available={available} for {appointment_date}")
    return available >= 1
