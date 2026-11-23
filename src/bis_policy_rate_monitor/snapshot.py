"""Calculate a snapshot from one area's resolved daily observations."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Context, Decimal, localcontext
from typing import Literal

from bis_policy_rate_monitor.flat_csv import SUPPORTED_MULTIPLIER, SUPPORTED_UNIT, ValueState

STALE_AFTER_DAYS = 7
SnapshotStatus = Literal[
    "ok", "unavailable", "no_valid_latest", "insufficient_history",
    "series_blocked", "incompatible_units",
]
FallbackReason = Literal["explicit_missing", "ineligible", "absent_date"]


@dataclass(frozen=True, slots=True)
class DailyObservation:
    """The fields needed to select and explain a resolved daily rate."""

    period: date
    eligible_rate: Decimal | None
    value_state: ValueState
    unit: str | None
    multiplier: str | None
    issues: tuple[str, ...] = ()
    metadata_id: str | None = None
    pre_break: str | None = None


@dataclass(frozen=True, slots=True)
class Snapshot:
    status: SnapshotStatus
    newest: DailyObservation | None = None
    latest: DailyObservation | None = None
    anniversary: date | None = None
    at_anniversary: DailyObservation | None = None
    historical: DailyObservation | None = None
    latest_reason: FallbackReason | None = None
    historical_reason: FallbackReason | None = None
    latest_age_days: int | None = None
    change_bp: Decimal | None = None

    @property
    def historical_lag_days(self) -> int | None:
        """Return how far the selected historical rate precedes the anniversary."""
        if self.historical is None or self.anniversary is None:
            return None
        return (self.anniversary - self.historical.period).days

    @property
    def latest_is_stale(self) -> bool:
        """Check whether the latest rate exceeds the age threshold at retrieval."""
        return self.latest_age_days is not None and self.latest_age_days > STALE_AFTER_DAYS

    @property
    def historical_is_stale(self) -> bool:
        """Flag a historical observation whose lag exceeds the warning threshold."""
        lag = self.historical_lag_days
        return lag is not None and lag > STALE_AFTER_DAYS


def unavailable_rate_reason(row: DailyObservation | None) -> FallbackReason:
    """Explain an absent or ineligible daily rate."""
    if row is None:
        return "absent_date"
    if row.value_state == "missing":
        return "explicit_missing"
    return "ineligible"


def _basis_point_change(latest: Decimal, historical: Decimal) -> Decimal:
    """Compute the exact per-cent rate difference in basis points."""
    smallest_exponent = min(int(rate.as_tuple().exponent) for rate in (latest, historical))
    largest_place = max(latest.adjusted(), historical.adjusted())
    # Allow for all aligned places, a carry in subtraction and two places from multiplying by 100.
    with localcontext(Context(prec=largest_place - smallest_exponent + 4)):
        return (latest - historical) * 100


def calculate_snapshot(
    observations: Iterable[DailyObservation],
    *,
    capture_date: date,
    series_blocked: bool = False,
) -> Snapshot:
    """Select rates for one area given resolved daily rows in any order.

    Use the capture's UTC retrieval date to measure age. Pass the transform's
    series block so conflicts can be reported even when no rows survive resolution.
    """
    by_date: dict[date, DailyObservation] = {}
    for row in observations:
        if row.period in by_date:
            raise ValueError(f"Snapshot input has multiple rows for {row.period.isoformat()}.")
        by_date[row.period] = row

    # Remember the latest dated row even if a missing value makes an older rate the latest eligible
    # one.
    newest = by_date[max(by_date)] if by_date else None
    # Check units across the series, including dated missing rows, before comparing the rates.
    if any(
        (row.unit, row.multiplier) != (SUPPORTED_UNIT, SUPPORTED_MULTIPLIER)
        for row in by_date.values()
    ):
        return Snapshot("incompatible_units", newest=newest)
    if series_blocked:
        return Snapshot("series_blocked", newest=newest)
    if newest is None:
        return Snapshot("unavailable")

    eligible = [
        row for row in by_date.values()
        if row.value_state == "normal"
        and row.eligible_rate is not None
        and row.eligible_rate.is_finite()
    ]
    if not eligible:
        return Snapshot(
            "no_valid_latest", newest=newest, latest_reason=unavailable_rate_reason(newest),
        )

    latest = max(eligible, key=lambda row: row.period)
    latest_reason = None if latest is newest else unavailable_rate_reason(newest)
    latest_age_days = (capture_date - latest.period).days
    if latest.period.year == date.min.year:
        return Snapshot(
            "insufficient_history", newest=newest, latest=latest,
            latest_reason=latest_reason, latest_age_days=latest_age_days,
        )

    # Each area uses its own latest usable date. A leap-day anniversary falls on 28 February.
    day = 28 if (latest.period.month, latest.period.day) == (2, 29) else latest.period.day
    anniversary = latest.period.replace(year=latest.period.year - 1, day=day)
    at_anniversary = by_date.get(anniversary)
    # Select the most recent eligible observation at or before the anniversary to avoid using future
    # data.
    historical = max(
        (row for row in eligible if row.period <= anniversary),
        key=lambda row: row.period,
        default=None,
    )
    historical_reason = None
    if historical is None or historical is not at_anniversary:
        historical_reason = unavailable_rate_reason(at_anniversary)
    change_bp = None
    if historical is not None:
        assert latest.eligible_rate is not None and historical.eligible_rate is not None
        change_bp = _basis_point_change(latest.eligible_rate, historical.eligible_rate)

    return Snapshot(
        status="ok" if historical is not None else "insufficient_history",
        newest=newest,
        latest=latest,
        anniversary=anniversary,
        at_anniversary=at_anniversary,
        historical=historical,
        latest_reason=latest_reason,
        historical_reason=historical_reason,
        latest_age_days=latest_age_days,
        change_bp=change_bp,
    )
