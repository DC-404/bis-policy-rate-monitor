"""Check envelope, calendar, precision and missing-value rules at the CSV boundary."""

import csv
import io
from datetime import date
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

import pytest

from bis_policy_rate_monitor.flat_csv import FlatCsvError, parse_header, parse_observation

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic-cbpol-flat.zip"


@pytest.fixture(scope="module")
def source_rows() -> tuple[list[str], list[list[str]]]:
    """Load fixture records through the same ZIP and CSV interfaces as the pipeline."""
    with ZipFile(FIXTURE) as archive, archive.open("WS_CBPOL_csv_flat.csv") as member:
        with io.TextIOWrapper(member, encoding="utf-8", newline="") as text:
            columns, *rows = csv.reader(text, strict=True)
    return columns, rows


def test_parse_keeps_source_cells_labels_and_decimal_precision(source_rows) -> None:
    """Preserve source labels, record numbers and exact decimals for audit."""
    columns, records = source_rows
    header = parse_header(columns)
    # Include another colon inside the area label and a leading zero in DECIMALS.
    edited = records[6].copy()
    edited[header.positions["REF_AREA"]] = "US: United States: synthetic label"
    edited[header.positions["DECIMALS"]] = "04: Four"

    observation = parse_observation(header, edited, 7)

    assert observation.record_number == 7
    assert observation.raw_cells == tuple(edited)
    assert observation.flow_id == "BIS:WS_CBPOL(1.0)"
    assert observation.frequency.code == "D"
    assert observation.area.code == "US"
    assert observation.area.label == "United States: synthetic label"
    assert observation.period == date(2025, 11, 17)
    assert observation.raw_value == "3.25"
    assert observation.eligible_rate == Decimal("3.25")
    # Check scale as well as numeric equality to catch loss of source precision.
    assert observation.eligible_rate.as_tuple().exponent == -2
    assert observation.status.code == "A"
    assert observation.unit.code == "368"
    assert observation.multiplier.code == "0"
    assert observation.decimals.code == "04"
    assert observation.title == "Synthetic daily US rate"
    assert observation.compilation == "Instrument: legacy target, synthetic method"
    assert observation.issues == ()


def test_fixture_keeps_explicit_missing_monthly_and_negative_records(source_rows) -> None:
    """Distinguish explicit missingness, exclusion, duplicates and a valid negative rate."""
    columns, records = source_rows
    header = parse_header(columns)
    observations = [
        parse_observation(header, record, index) for index, record in enumerate(records, 1)
    ]
    # These fixture positions intentionally cover distinct observation types.
    first, repeat, missing = observations[:3]
    monthly, negative = observations[5], observations[-1]

    assert first.raw_cells == repeat.raw_cells and first.record_number != repeat.record_number
    assert missing.period == date(2025, 11, 16)
    assert missing.raw_value == "NaN" and missing.status.code == "M"
    assert missing.value_state == "missing" and missing.eligible_rate is None
    assert missing.issues == ()
    # The parser keeps the monthly period but does not make it a daily candidate.
    assert monthly.period == "2026-12" and monthly.value_state == "excluded"
    assert monthly.eligible_rate is None and monthly.issues == ()
    assert negative.eligible_rate == Decimal("-0.25")


@pytest.mark.parametrize(
    ("status", "value", "issue"),
    [
        ("A", "bad rate", "malformed_value"),
        ("A", "NaN", "normal_status_with_nan"),
        ("A", "Infinity", "non_finite_value"),
        ("M", "5.00", "missing_status_with_value"),
        ("M", "", "blank_value"),
        ("E", "5.00", "unsupported_status"),
    ],
)
def test_unusable_rates_have_distinct_diagnostics(source_rows, status, value, issue) -> None:
    """Keep separate reasons for status and value mismatches that affect eligibility."""
    columns, records = source_rows
    header = parse_header(columns)
    # Change only the status and value so each diagnostic isolates the intended case.
    edited = records[0].copy()
    edited[header.positions["OBS_STATUS"]] = status
    edited[header.positions["OBS_VALUE"]] = value

    observation = parse_observation(header, edited, 1)

    assert observation.value_state == "invalid"
    assert observation.eligible_rate is None
    assert observation.issues == (issue,)
    # Even an ineligible rate retains its raw value and date for diagnostics.
    assert observation.raw_value == value
    assert observation.period == date(2025, 11, 14)


def test_finite_value_still_needs_a_valid_key_unit_and_confidentiality(source_rows) -> None:
    """Suppress a finite value when its identity, units or publication status are unsafe."""
    columns, records = source_rows
    header = parse_header(columns)
    edited = records[0].copy()
    # Combine independent violations on an otherwise ordinary finite observation.
    for field, value in {
        "REF_AREA": "EURO",
        "TIME_PERIOD": "20251114",
        "UNIT_MEASURE": "999",
        "UNIT_MULT": "1",
        "OBS_CONF": "C",
    }.items():
        edited[header.positions[field]] = value

    observation = parse_observation(header, edited, 1)

    # A normal numeric token and a usable observation are separate checks.
    assert observation.value_state == "normal"
    assert observation.eligible_rate is None
    assert observation.period is None
    assert set(observation.issues) == {
        "invalid_area", "invalid_period", "unsupported_unit", "unsupported_multiplier",
        "restricted_value",
    }


def test_header_accepts_reordering_and_absent_optional_fields(source_rows) -> None:
    """Read fields by ID even when order and captions change or optional fields are absent."""
    columns, records = source_rows
    original_header = parse_header(columns)
    # Remove optional fields and reverse the rest with replacement captions.
    omitted = {original_header.positions[field] for field in ("TIME_FORMAT", "OBS_CONF", "TITLE")}
    order = [index for index in reversed(range(len(columns))) if index not in omitted]
    header = parse_header([columns[index].partition(":")[0] + ":new caption" for index in order])

    observation = parse_observation(header, [records[0][index] for index in order], 1)

    assert observation.period == date(2025, 11, 14)
    assert observation.eligible_rate == Decimal("4.00")
    assert observation.title is None
    assert header.missing_optional == ("OBS_CONF", "TIME_FORMAT", "TITLE")
    # Missing optional columns and frequency-based date validation stay visible.
    assert header.capture_notes(row_count=1, blank_time_format_rows=1) == (
        "Optional columns absent from the export: OBS_CONF, TITLE",
        "BIS omitted TIME_FORMAT. We validated dates using the reported frequency.",
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("missing", "Missing required flat CSV fields: OBS_VALUE"),
        ("extra", "Unexpected flat CSV fields: NEW_DIMENSION"),
        ("duplicate", "Duplicate flat CSV field IDs: FREQ"),
    ],
)
def test_bad_headers_fail_with_the_field_id(source_rows, change, message) -> None:
    """Identify schema drift through the missing, unexpected or duplicated field ID."""
    columns, _ = source_rows
    # Start with the missing-measure case, then substitute one other schema defect.
    edited = [column for column in columns if not column.startswith("OBS_VALUE:")]
    if change == "extra":
        edited = [*columns, "NEW_DIMENSION:New dimension"]
    elif change == "duplicate":
        edited = [*columns, "FREQ:Repeated with another label"]

    with pytest.raises(FlatCsvError, match=message):
        parse_header(edited)


def test_bad_width_and_envelope_reject_the_capture(source_rows) -> None:
    """Reject malformed records and unsupported actions before using a capture."""
    columns, records = source_rows
    header = parse_header(columns)

    # First remove a trailing cell while keeping the header intact.
    with pytest.raises(FlatCsvError, match="record 2 has 17 cells. Expected 18"):
        parse_observation(header, records[0][:-1], 2)

    # Then change the action on a well-shaped monthly record.
    edited = records[5].copy()
    edited[header.positions["ACTION"]] = "D"
    with pytest.raises(FlatCsvError, match="record 6 has an unexpected ACTION"):
        parse_observation(header, edited, 6)
