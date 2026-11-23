"""Check that suggestions use both SDMX code membership and usable flat observations."""

from bis_policy_rate_monitor.area_diagnostics import diagnose_area


def test_suggestions_use_defined_usable_series_and_prefer_transposition() -> None:
    """Prefer a usable, defined transposition over several edit-distance matches."""
    labels = dict.fromkeys(["US", "SA", "SB", "SC", "SD", "ST", "AU"], "Synthetic area")
    # SX lacks a codelist entry and ST lacks a usable rate, so neither qualifies.
    result = diagnose_area(
        "SU",
        area_labels=labels,
        daily_areas={"US", "SA", "SB", "SC", "SD", "ST", "SX"},
        usable_areas={"US", "SA", "SB", "SC", "SD", "SX"},
    )

    assert result.suggestion == "US"


def test_unknown_area_without_close_usable_match_has_no_suggestions() -> None:
    """Keep an unknown code visible when no close candidate can supply a rate."""
    # ZA has daily rows but is unusable, and the usable XZ is undefined.
    result = diagnose_area(
        "ZZ",
        area_labels={"ZA": "South Africa", "AF": "Afghanistan", "US": "United States"},
        daily_areas={"ZA", "US", "XZ"},
        usable_areas={"US", "XZ"},
    )

    assert result.code_status == "unknown"
    assert result.suggestion is None


def test_unknown_area_with_equally_close_candidates_has_no_suggestions() -> None:
    """Avoid guessing which area the caller meant when usable matches tie."""
    # Each of the three defined and usable codes differs from ZZ by one character.
    labels = {"CZ": "Czechia", "NZ": "New Zealand", "ZA": "South Africa"}
    result = diagnose_area(
        "ZZ", area_labels=labels, daily_areas=labels.keys(), usable_areas=labels.keys()
    )

    assert result.code_status == "unknown"
    assert result.suggestion is None
