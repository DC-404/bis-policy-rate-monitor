"""Check the plotted observations and reasons shown when a line cannot be drawn."""

from datetime import date
from decimal import Decimal
from math import isnan
from pathlib import Path

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.dates import date2num

from bis_policy_rate_monitor.chart import build_chart, write_chart
from bis_policy_rate_monitor.snapshot import DailyObservation, Snapshot
from bis_policy_rate_monitor.summary import AreaRequest, AreaSummary, ReportSelection, ReportSummary


def report_for(start: date, *areas: AreaSummary) -> ReportSummary:
    """Create a chart input with a fixed source timestamp and provided area order."""
    selection = ReportSelection(tuple(area.request for area in areas), start)
    source = {"retrieved_at": "2026-11-21T18:14:14+00:00"}
    return ReportSummary(selection, source, (), areas, {})


def test_chart_breaks_the_line_at_unavailable_values_and_absent_dates() -> None:
    """Keep missingness and source breaks visible without inventing rates."""
    start = date(2026, 11, 1)
    # Day 2 is explicitly missing, day 4 is absent and day 7 has an invalid value.
    history = (
        DailyObservation(start, Decimal("3"), "normal", "368", "0"),
        DailyObservation(date(2026, 11, 2), None, "missing", "368", "0"),
        DailyObservation(date(2026, 11, 3), Decimal("2.75"), "normal", "368", "0", pre_break="3"),
        DailyObservation(date(2026, 11, 5), Decimal("2.75"), "normal", "368", "0"),
        DailyObservation(date(2026, 11, 6), Decimal("2.5"), "normal", "368", "0"),
        DailyObservation(date(2026, 11, 7), None, "invalid", "368", "0"),
        DailyObservation(date(2026, 11, 8), Decimal("2.5"), "normal", "368", "0"),
    )
    area = AreaSummary(AreaRequest("EA", "XM"), "Euro area", Snapshot("ok"), history, {})

    figure = build_chart(report_for(start, area))

    header, axes, footer = figure.axes
    line, break_marker = axes.lines
    assert list(line.get_xdata()) == [date(2026, 11, day) for day in range(1, 9)]
    # Check the plotted values directly so the test does not depend on image pixels.
    values = line.get_ydata()
    assert [i for i, value in enumerate(values) if isnan(value)] == [1, 3, 6]
    assert [value for value in values if not isnan(value)] == [3, 2.75, 2.75, 2.5, 2.5]
    assert line.get_markevery() == [0, 2, 7]
    assert list(break_marker.get_xdata()) == [date(2026, 11, 3)]
    assert list(break_marker.get_ydata()) == [2.75] and break_marker.get_marker() == "D"
    assert axes.get_ylabel() == "Policy rate (% per year)"
    assert "2026-11-01 to 2026-11-08" in axes.get_title(loc="left")
    assert [text.get_text() for text in header.get_legend().texts] == ["Euro area (EA/XM)"]
    assert header.get_legend().legend_handles[0].get_marker() in {None, "None"}
    assert "can look dotted" in footer.texts[0].get_text()
    # The footer explains how isolated points and source break markers should be read.
    markers = footer.texts[1].get_text()
    assert "Filled circles mark usable rates with no adjacent usable day" in markers
    assert "Open diamonds mark dated rows with a pre-break value" in markers


def test_chart_explains_why_requested_series_are_unplottable() -> None:
    """Give a specific legend entry for each of four unplottable outcomes."""
    start = date(2026, 11, 22)
    # Include an otherwise usable rate to verify that a withheld series is still hidden.
    blocked = AreaSummary(
        AreaRequest("GB", "GB"), "United Kingdom", Snapshot("series_blocked"),
        (DailyObservation(start, Decimal("4"), "normal", "368", "0"),), {},
    )
    unavailable = AreaSummary(AreaRequest("JP", "JP"), "JP", Snapshot("unavailable"), (), {})
    outside = AreaSummary(AreaRequest("US", "US"), "United States", Snapshot("ok"), (), {})
    missing = AreaSummary(
        AreaRequest("CH", "CH"), "Switzerland", Snapshot("no_valid_latest"),
        (DailyObservation(start, None, "missing", "368", "0"),), {},
    )
    summary = report_for(start, blocked, unavailable, outside, missing)

    figure = build_chart(summary)

    header, axes, _ = figure.axes
    labels = [text.get_text() for text in header.get_legend().texts]
    assert labels == [
        "United Kingdom (GB) · withheld for conflict",
        "JP · unavailable in capture",
        "United States (US) · no rows in range",
        "Switzerland (CH) · no eligible rates in range",
    ]
    assert not axes.lines
    assert axes.texts[0].get_text() == "No eligible daily rates in this date range."
    assert axes.get_xlim() == (date2num(start) - 0.5, date2num(start) + 0.5)

    canvas = FigureCanvasAgg(figure)
    # Measure text after rendering to confirm the title and legend do not overlap.
    canvas.draw()
    renderer = canvas.get_renderer()
    title_bounds = header.texts[0].get_window_extent(renderer)
    legend_bounds = header.get_legend().get_window_extent(renderer)
    assert title_bounds.y0 > legend_bounds.y1


def test_chart_supports_the_earliest_iso_start_date(tmp_path: Path) -> None:
    """Check that a legal lower date boundary can still produce an empty chart."""
    area = AreaSummary(AreaRequest("JP", "JP"), "JP", Snapshot("unavailable"), (), {})
    summary = report_for(date.min, area)

    # Exercise PNG rendering because plotting libraries can fail during axis formatting.
    assert write_chart(summary, tmp_path / "policy_rates.png").is_file()
