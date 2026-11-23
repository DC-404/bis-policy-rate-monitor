"""Exercise argument normalization shared by CLI and browser reports."""

from datetime import date

import pytest

from bis_policy_rate_monitor.summary import AreaRequest, parse_selection


def test_selection_keeps_request_order_and_resolves_the_euro_area_alias() -> None:
    """Keep caller order and requested codes while resolving the BIS euro area key."""
    selection = parse_selection(" jp , ea, us ", "2025-01-01")

    # Keep the original EA alongside the canonical XM for traceable results.
    assert selection.areas == (
        AreaRequest("JP", "JP"), AreaRequest("EA", "XM"), AreaRequest("US", "US"),
    )
    assert selection.start == date(2025, 1, 1)


def test_selection_normalizes_codes_before_checking_for_duplicates() -> None:
    """Reject aliases and repeated spellings that point to the same output series."""
    # The cases become duplicates once case, whitespace and aliases are applied.
    with pytest.raises(ValueError, match="Area requested more than once"):
        parse_selection("US, us ")


@pytest.mark.parametrize("countries", ["US,", "USA"], ids=["empty-token", "wrong-length"])
def test_selection_rejects_malformed_area_tokens(countries: str) -> None:
    """Reject malformed tokens before starting file or network work."""
    # Each input violates the contract for comma-separated two-character codes.
    with pytest.raises(ValueError, match="comma-separated two-character area codes"):
        parse_selection(countries)


@pytest.mark.parametrize("start", ["20250101", "2025-02-29"])
def test_selection_rejects_invalid_start_dates(start: str) -> None:
    """Require a real date in the canonical chart-start format."""
    # Non-canonical representations and impossible dates must both fail.
    with pytest.raises(ValueError, match="valid start date in YYYY-MM-DD format"):
        parse_selection(start=start)
