"""Interpret BIS portal flat CSV cells according to the monitor's source contract."""

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

FLOW_ID = "BIS:WS_CBPOL(1.0)"
SUPPORTED_UNIT = "368"
SUPPORTED_MULTIPLIER = "0"
REQUIRED_FIELDS = frozenset({
    "STRUCTURE", "STRUCTURE_ID", "ACTION", "FREQ", "REF_AREA", "TIME_PERIOD",
    "OBS_VALUE", "OBS_STATUS", "UNIT_MEASURE", "UNIT_MULT",
})
OPTIONAL_FIELDS = frozenset({
    "TIME_FORMAT", "DECIMALS", "TITLE", "SOURCE_REF", "COMPILATION",
    "SUPP_INFO_BREAKS", "OBS_CONF", "OBS_PRE_BREAK",
})
AREA_CODE = re.compile(r"[A-Z0-9]{2}")
DECIMAL_TOKEN = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?")
ValueState = Literal["normal", "missing", "invalid", "excluded"]


class FlatCsvError(ValueError):
    """The header, row width or envelope is incompatible with the source contract."""


@dataclass(frozen=True, slots=True)
class FlatHeader:
    field_ids: tuple[str, ...]
    positions: dict[str, int]
    missing_optional: tuple[str, ...]

    def capture_notes(self, *, row_count: int, blank_time_format_rows: int) -> tuple[str, ...]:
        """Describe optional columns and blank time format once for the capture."""
        notes = []
        missing = [field for field in self.missing_optional if field != "TIME_FORMAT"]
        if missing:
            notes.append("Optional columns absent from the export: " + ", ".join(missing))
        if "TIME_FORMAT" in self.missing_optional:
            notes.append(
                "BIS omitted TIME_FORMAT. We validated dates using the reported frequency."
            )
        elif blank_time_format_rows:
            notes.append(
                f"BIS left TIME_FORMAT blank on {blank_time_format_rows:,} of {row_count:,} rows. "
                "We validated dates using the reported frequency."
            )
        return tuple(notes)


@dataclass(frozen=True, slots=True)
class CodedCell:
    code: str | None
    label: str | None


@dataclass(frozen=True, slots=True)
class FlatObservation:
    record_number: int
    raw_cells: tuple[str, ...]
    flow_id: str
    frequency: CodedCell
    area: CodedCell
    raw_period: str
    period: date | str | None
    raw_value: str
    value_state: ValueState
    eligible_rate: Decimal | None
    status: CodedCell
    unit: CodedCell
    multiplier: CodedCell
    time_format: CodedCell
    decimals: CodedCell
    confidentiality: CodedCell
    title: str | None
    source_ref: str | None
    compilation: str | None
    break_notes: str | None
    pre_break: str | None
    issues: tuple[str, ...]


def parse_header(columns: Sequence[str]) -> FlatHeader:
    """Validate field IDs and record their positions and absent optional fields."""
    # The column ID precedes the human-readable caption and survives caption changes.
    field_ids = tuple(column.partition(":")[0].strip() for column in columns)
    if "" in field_ids:
        raise FlatCsvError("The flat CSV header contains an empty field ID.")
    repeated = sorted(field_id for field_id, count in Counter(field_ids).items() if count > 1)
    if repeated:
        raise FlatCsvError("Duplicate flat CSV field IDs: " + ", ".join(repeated))
    present = set(field_ids)
    missing = REQUIRED_FIELDS - present
    unexpected = present - REQUIRED_FIELDS - OPTIONAL_FIELDS
    if missing or unexpected:
        problems = []
        if missing:
            problems.append("Missing required flat CSV fields: " + ", ".join(sorted(missing)))
        if unexpected:
            problems.append("Unexpected flat CSV fields: " + ", ".join(sorted(unexpected)))
        raise FlatCsvError(". ".join(problems) + ".")

    return FlatHeader(
        field_ids=field_ids,
        positions={field: index for index, field in enumerate(field_ids)},
        missing_optional=tuple(sorted(OPTIONAL_FIELDS - present)),
    )


def _coded(cell: str | None) -> CodedCell:
    """Split a labelled cell at its first separator and keep the rest of its label."""
    code, _, label = (cell or "").partition(": ")
    return CodedCell(code.strip() or None, label or None)


def _parse_period(raw_period: str, frequency: str | None) -> date | str | None:
    """Validate a calendar period using the frequency and exact export format."""
    # Use FREQ for date validation because the BIS flat export can omit TIME_FORMAT.
    if frequency in {"D", "M"}:
        try:
            parsed = date.fromisoformat(raw_period if frequency == "D" else f"{raw_period}-01")
        except ValueError:
            return None
        # Round-trip to require the export's exact YYYY-MM-DD or YYYY-MM spelling.
        if frequency == "D" and parsed.isoformat() == raw_period:
            return parsed
        if frequency == "M" and parsed.isoformat()[:7] == raw_period:
            return raw_period
    return None


def _rate_value(
    raw_value: str, status: str | None
) -> tuple[ValueState, Decimal | None, str | None]:
    """Interpret a value and its BIS status, preserving exact decimal precision."""
    token = raw_value.strip()
    if not token:
        return "invalid", None, "blank_value"
    # An explicit missing observation needs both the published status and rate token.
    if status == "M" and raw_value == "NaN":
        return "missing", None, None
    if status not in {"A", "M"}:
        return "invalid", None, "unsupported_status"
    if status == "A" and raw_value == "NaN":
        return "invalid", None, "normal_status_with_nan"
    try:
        rate = Decimal(token)
    except InvalidOperation:
        return "invalid", None, "malformed_value"
    if not rate.is_finite():
        return "invalid", None, "non_finite_value"
    if not DECIMAL_TOKEN.fullmatch(token):
        return "invalid", None, "malformed_value"
    if status == "M":
        return "invalid", None, "missing_status_with_value"
    return "normal", rate, None


def parse_observation(
    header: FlatHeader, cells: Sequence[str], record_number: int
) -> FlatObservation:
    """Parse a row and record eligibility evidence without dropping source cells."""
    if len(cells) != len(header.field_ids):
        raise FlatCsvError(
            f"CSV record {record_number} has {len(cells)} cells. Expected {len(header.field_ids)}."
        )
    # Retain the untouched cells so duplicate resolution can compare source evidence.
    raw_cells = tuple(cells)

    def cell(field: str) -> str | None:
        """Read a source cell by field ID and return None for an omitted column."""
        position = header.positions.get(field)
        return raw_cells[position] if position is not None else None

    flow = _coded(cell("STRUCTURE_ID"))
    # Apply the flat export envelope checks even for rows outside the daily frequency.
    for field, observed, expected in (
        ("STRUCTURE", cell("STRUCTURE"), "dataflow"),
        ("STRUCTURE_ID", flow.code, FLOW_ID),
        ("ACTION", cell("ACTION"), "I"),
    ):
        if observed != expected:
            raise FlatCsvError(
                f"CSV record {record_number} has an unexpected {field}. Expected {expected}."
            )

    frequency = _coded(cell("FREQ"))
    area = _coded(cell("REF_AREA"))
    raw_period = cell("TIME_PERIOD") or ""
    period = _parse_period(raw_period, frequency.code)
    raw_value = cell("OBS_VALUE") or ""
    status = _coded(cell("OBS_STATUS"))
    unit = _coded(cell("UNIT_MEASURE"))
    multiplier = _coded(cell("UNIT_MULT"))
    confidentiality = _coded(cell("OBS_CONF"))

    issues = []
    value_state: ValueState = "excluded"
    rate = None
    if frequency.code not in {"D", "M"}:
        issues.append("unsupported_frequency")
    elif period is None:
        issues.append("invalid_period")
    # Assign rate eligibility only to daily rows while keeping foreign frequencies visible to scan
    # diagnostics.
    if frequency.code == "D":
        if area.code is None or not AREA_CODE.fullmatch(area.code):
            issues.append("invalid_area")
        if unit.code != SUPPORTED_UNIT:
            issues.append("unsupported_unit")
        if multiplier.code != SUPPORTED_MULTIPLIER:
            issues.append("unsupported_multiplier")
        if confidentiality.code not in {None, "F"}:
            issues.append("restricted_value")
        value_state, rate, value_issue = _rate_value(raw_value, status.code)
        if value_issue:
            issues.append(value_issue)

    return FlatObservation(
        record_number=record_number,
        raw_cells=raw_cells,
        flow_id=FLOW_ID,
        frequency=frequency,
        area=area,
        raw_period=raw_period,
        period=period,
        raw_value=raw_value,
        value_state=value_state,
        eligible_rate=None if issues else rate,
        status=status,
        unit=unit,
        multiplier=multiplier,
        time_format=_coded(cell("TIME_FORMAT")),
        decimals=_coded(cell("DECIMALS")),
        confidentiality=confidentiality,
        title=cell("TITLE"),
        source_ref=cell("SOURCE_REF"),
        compilation=cell("COMPILATION"),
        break_notes=cell("SUPP_INFO_BREAKS"),
        pre_break=cell("OBS_PRE_BREAK"),
        issues=tuple(issues),
    )
