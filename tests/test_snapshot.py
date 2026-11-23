"""Exercise the selection and basis-point rules without CSV files or network calls."""

from datetime import date, timedelta
from decimal import Decimal, localcontext

import pytest

from bis_policy_rate_monitor.snapshot import DailyObservation, calculate_snapshot


def rate(day: str, value: str) -> DailyObservation:
    """Create an eligible daily fixture rate in the supported unit and multiplier."""
    return DailyObservation(date.fromisoformat(day), Decimal(value), "normal", "368", "0")


@pytest.mark.parametrize(
    ("cutoff_row", "historical_date", "reason", "lag", "change"),
    [
        (rate("2025-11-16", "4.25"), "2025-11-16", None, 0, "25"),
        (None, "2025-11-14", "absent_date", 2, "50"),
        (
            DailyObservation(date(2025, 11, 16), None, "missing", "368", "0"),
            "2025-11-14", "explicit_missing", 2, "50",
        ),
        (
            DailyObservation(date(2025, 11, 16), None, "normal", "368", "0", ("restricted_value",)),
            "2025-11-14", "ineligible", 2, "50",
        ),
    ],
    ids=["exact-anniversary", "sunday-absent", "published-missing", "restricted"],
)
def test_historical_lookup_uses_the_latest_eligible_row_on_or_before_cutoff(
    cutoff_row, historical_date, reason, lag, change,
) -> None:
    """Select the latest eligible rate at the anniversary and explain any fallback."""
    latest = rate("2026-11-16", "4.50")
    # Keep rows unsorted and include a plausible rate after the cutoff to rule it out.
    rows = [rate("2025-11-17", "4.40"), latest, rate("2025-11-14", "4.00")]
    if cutoff_row is not None:
        rows.append(cutoff_row)

    snapshot = calculate_snapshot(iter(rows), capture_date=date(2026, 11, 17))

    assert snapshot.status == "ok"
    assert snapshot.newest == snapshot.latest == latest
    assert snapshot.latest_reason is None
    assert snapshot.anniversary == date(2025, 11, 16)
    # Retain the dated cutoff row even when a different observation is selected.
    assert snapshot.at_anniversary == cutoff_row
    assert snapshot.historical is not None
    assert snapshot.historical.period == date.fromisoformat(historical_date)
    assert snapshot.historical_reason == reason
    assert snapshot.historical_lag_days == lag
    assert snapshot.change_bp == Decimal(change)


@pytest.mark.parametrize(
    ("state", "issues", "reason"),
    [("missing", (), "explicit_missing"), ("invalid", ("malformed_value",), "ineligible")],
)
def test_anniversary_uses_the_latest_valid_date_when_a_newer_record_is_unusable(
    state, issues, reason,
) -> None:
    """Anchor the anniversary on the latest usable rate rather than the last dated row."""
    # The newest dated row and the latest eligible row fall on different days.
    newest = DailyObservation(date(2026, 11, 17), None, state, "368", "0", issues)
    latest = rate("2026-11-16", "4.50")
    historical = rate("2025-11-14", "4.00")
    rows = [
        newest, latest, historical, rate("2025-11-17", "4.25"),
        DailyObservation(date(2025, 11, 16), None, "missing", "368", "0"),
    ]

    snapshot = calculate_snapshot(rows, capture_date=date(2026, 11, 18))

    assert snapshot.newest == newest and snapshot.latest == latest
    assert snapshot.latest_reason == reason
    assert snapshot.latest_age_days == 2
    # The newer date would incorrectly admit the historical rate on November 17.
    assert snapshot.anniversary == date(2025, 11, 16)
    assert snapshot.historical == historical
    assert snapshot.historical_reason == "explicit_missing"
    assert snapshot.historical_lag_days == 2
    assert snapshot.change_bp == Decimal("50")


def test_leap_day_uses_february_28_as_the_anniversary() -> None:
    """Use February 28 in a non-leap comparison year and support a zero latest rate."""
    latest = rate("2024-02-29", "0")
    historical = rate("2023-02-28", "-0.25")
    # A March 1 value is available, so selection proves the February boundary.
    rows = [rate("2023-03-01", "0.25"), historical, latest]

    snapshot = calculate_snapshot(rows, capture_date=latest.period)

    assert snapshot.anniversary == date(2023, 2, 28)
    assert snapshot.historical == historical
    assert snapshot.latest == latest
    assert snapshot.change_bp == Decimal("25")


def test_latest_rate_is_retained_when_there_is_no_eligible_history() -> None:
    """Keep the latest negative rate while suppressing an unsupported change."""
    latest = rate("2026-11-17", "-0.25")
    missing = DailyObservation(date(2025, 11, 17), None, "missing", "368", "0")

    snapshot = calculate_snapshot([latest, missing], capture_date=latest.period)

    assert snapshot.status == "insufficient_history"
    assert snapshot.latest == latest
    assert snapshot.anniversary == missing.period
    assert snapshot.historical_reason == "explicit_missing"
    # An explicit missing comparison produces no historical rate, lag or change.
    assert snapshot.historical is snapshot.historical_lag_days is snapshot.change_bp is None


def test_all_missing_rows_differ_from_an_unavailable_area() -> None:
    """Distinguish existing daily coverage with unreported rates from absent coverage."""
    newest = DailyObservation(date(2026, 11, 17), None, "missing", "368", "0")

    # Apply the same calculation date to one dated missing row and an empty series.
    missing = calculate_snapshot([newest], capture_date=newest.period)
    unavailable = calculate_snapshot([], capture_date=newest.period)

    assert missing.status == "no_valid_latest"
    assert missing.newest == newest and missing.latest_reason == "explicit_missing"
    assert missing.latest is missing.anniversary is missing.change_bp is None
    assert unavailable.status == "unavailable" and unavailable.newest is None


@pytest.mark.parametrize(
    ("unit", "multiplier"), [(None, "0"), ("368", "1")], ids=["missing-unit", "wrong-multiplier"],
)
def test_incompatible_units_block_the_whole_series_even_on_a_missing_row(unit, multiplier) -> None:
    """Enforce unit and multiplier compatibility at the series level."""
    latest = rate("2026-11-17", "4.50")
    # The two comparison rows are valid, but an older missing row has incompatible units.
    rows = [
        rate("2025-11-17", "4.00"), latest,
        DailyObservation(date(2024, 11, 17), None, "missing", unit, multiplier),
    ]

    snapshot = calculate_snapshot(rows, capture_date=latest.period)

    assert snapshot.status == "incompatible_units" and snapshot.newest == latest
    assert snapshot.latest is snapshot.historical is snapshot.change_bp is None


def test_series_block_withholds_even_eligible_rows() -> None:
    """Honor conflict decisions from the transform even when the input rows look usable."""
    rows = [rate("2025-11-17", "4.00"), rate("2026-11-17", "4.50")]
    # Pass the transform's series-wide block to two otherwise eligible observations.
    snapshot = calculate_snapshot(rows, capture_date=date(2026, 11, 18), series_blocked=True)

    assert snapshot.status == "series_blocked"
    assert snapshot.latest is snapshot.historical is snapshot.change_bp is None


@pytest.mark.parametrize(("age", "lag"), [(7, 8), (8, 7)])
def test_age_warnings_use_the_capture_date_and_anniversary_independently(age, lag) -> None:
    """Check independent age and lag warnings at both sides of the seven-day threshold."""
    latest = rate("2026-11-16", "4.50")
    anniversary = date(2025, 11, 16)
    historical = rate((anniversary - timedelta(days=lag)).isoformat(), "4.00")
    capture_date = latest.period + timedelta(days=age)

    snapshot = calculate_snapshot([latest, historical], capture_date=capture_date)

    assert snapshot.status == "ok" and snapshot.change_bp == Decimal("50")
    # Assert exact day counts before checking the two warning flags.
    assert snapshot.latest_age_days == age and snapshot.historical_lag_days == lag
    assert snapshot.latest_is_stale == (age > 7)
    assert snapshot.historical_is_stale == (lag > 7)


def test_basis_point_arithmetic_preserves_the_source_precision() -> None:
    """Retain a small exact change between rates that only differ at the tail."""
    latest = rate("2026-11-17", "2.123456789012345678901234567829")
    historical = rate("2025-11-17", "2.123456789012345678901234567891")

    # Lower the ambient decimal precision to exercise the calculation's own context.
    with localcontext() as context:
        context.prec = 8
        snapshot = calculate_snapshot([historical, latest], capture_date=latest.period)

    assert snapshot.change_bp == Decimal("-0.0000000000000000000000000062")


def test_unresolved_duplicate_dates_are_rejected_at_the_calculation_boundary() -> None:
    """Enforce the one-observation-per-date contract at the snapshot boundary."""
    # Send two different rates on one date directly to the calculation.
    rows = [rate("2026-11-17", "4.00"), rate("2026-11-17", "4.50")]

    with pytest.raises(ValueError, match="multiple rows for 2026-11-17"):
        calculate_snapshot(rows, capture_date=date(2026, 11, 17))
