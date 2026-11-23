"""Render the selected daily policy rate histories as a standalone PNG."""

from datetime import UTC, date, datetime
from itertools import cycle
from math import ceil, isfinite, nan
from pathlib import Path

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.dates import AutoDateLocator, ConciseDateFormatter, DayLocator, date2num
from matplotlib.figure import Figure
from matplotlib.lines import Line2D

from bis_policy_rate_monitor.snapshot import DailyObservation
from bis_policy_rate_monitor.summary import ReportSummary, chart_observations

CHART_PATH = Path("out/policy_rates.png")
SERIES_COLORS = ("#0072B2", "#D55E00", "#007B63", "#933E8B", "#795426", "#6B647A")
UNPLOTTED_COLOR = "#64748B"


def _plot_points(history: tuple[DailyObservation, ...]) -> tuple[list[date], list[float]]:
    """Convert daily observations and gap markers to Matplotlib coordinates."""
    dates: list[date] = []
    rates: list[float] = []
    for period, observation in chart_observations(history):
        dates.append(period)
        rate = observation.eligible_rate if observation is not None else None
        # Matplotlib breaks at NaN. Float conversion is limited to the plot coordinates.
        rates.append(float(rate) if rate is not None else nan)
    return dates, rates


def build_chart(summary: ReportSummary) -> Figure:
    """Build the chart from start-filtered histories in the verified report summary."""
    start = summary.selection.start
    capture_date = datetime.fromisoformat(summary.source["retrieved_at"]).astimezone(UTC).date()
    end = max(
        (row.period for area in summary.areas for row in area.history),
        default=max(start, capture_date),
    )
    # Reserve more vertical room when the user requests enough areas to wrap the legend.
    extra_legend_rows = max(0, ceil(len(summary.areas) / 3) - 2)
    figure = Figure(
        figsize=(12, 7.1 + 0.28 * extra_legend_rows),
        dpi=150,
        layout="constrained",
        facecolor="white",
    )
    figure.get_layout_engine().set(w_pad=0.17, h_pad=0.075)
    header, axes, footer = figure.subplots(3, height_ratios=(3 + 0.85 * extra_legend_rows, 16, 2))
    header.set_axis_off()
    header.text(
        0, 1, "Central bank policy rates", va="top",
        fontsize=21, fontweight="bold", color="#18283B",
    )
    axes.set_title(f"Daily series · {start} to {end}", loc="left", fontsize=11)

    legend_handles = []
    plotted = marked_break = False
    for area, color in zip(summary.areas, cycle(SERIES_COLORS), strict=False):
        requested, bis_code = area.request.requested_code, area.request.bis_code
        codes = requested if requested == bis_code else f"{requested}/{bis_code}"
        label = area.label if area.label == codes else f"{area.label} ({codes})"
        status = area.snapshot.status

        if status == "series_blocked":
            note = "withheld for conflict"
        elif status == "incompatible_units":
            note = "withheld for units"
        elif status == "unavailable":
            note = "unavailable in capture"
        elif not area.history:
            note = "no rows in range"
        else:
            dates, rates = _plot_points(area.history)
            note = None if any(isfinite(rate) for rate in rates) else "no eligible rates in range"

        if note is not None:
            legend_handles.append(Line2D(
                [], [], color=UNPLOTTED_COLOR, marker="x", linestyle="none",
                label=f"{label} · {note}",
            ))
            continue

        isolated = [
            index for index, rate in enumerate(rates)
            if isfinite(rate)
            and (index == 0 or not isfinite(rates[index - 1]))
            and (index == len(rates) - 1 or not isfinite(rates[index + 1]))
        ]
        axes.plot(
            dates, rates, label=label, color=color, linewidth=1.6,
            linestyle="-", solid_capstyle="butt", marker="o", markevery=isolated, markersize=4,
        )
        legend_handles.append(Line2D([], [], color=color, linewidth=1.6, label=label))
        plotted = True

        break_points = [
            (row.period, float(row.eligible_rate)) for row in area.history
            if row.pre_break and row.eligible_rate is not None
            and isfinite(float(row.eligible_rate))
        ]
        if break_points:
            break_dates, break_rates = zip(*break_points, strict=True)
            axes.plot(
                break_dates, break_rates, color=color, linestyle="none", marker="D",
                markerfacecolor="white", markersize=6, zorder=4,
            )
            marked_break = True

    if legend_handles:
        header.legend(
            handles=legend_handles, loc="lower left", ncols=min(3, len(legend_handles)),
            borderaxespad=0, frameon=False, fontsize=10, columnspacing=1.7,
        )
    if not plotted:
        axes.text(
            0.5, 0.52, "No eligible daily rates in this date range.",
            transform=axes.transAxes, ha="center", color="#4B5968",
        )
        axes.set_yticks([])

    axes.set_ylabel("Policy rate (% per year)")
    # Give the chart a visible range for one day and keep axis formatting within Python dates.
    left_edge = max(date2num(start) - 0.5, date2num(date.min))
    axes.set_xlim(left_edge, date2num(end) + 0.5)
    locator = (
        DayLocator(tz=UTC) if (end - start).days < 4
        else AutoDateLocator(tz=UTC, minticks=4, maxticks=9)
    )
    axes.xaxis.set_major_locator(locator)
    axes.xaxis.set_major_formatter(ConciseDateFormatter(locator, tz=UTC))
    axes.grid(axis="y", color="#D7DEE6", linewidth=0.8)
    axes.set_axisbelow(True)
    for side in ("top", "right"):
        axes.spines[side].set_visible(False)
    for side in ("bottom", "left"):
        axes.spines[side].set_color("#AAB7C5")
    axes.tick_params(colors="#405268")

    footer.set_axis_off()
    explanation = "Filled circles mark usable rates with no adjacent usable day in the chart."
    if marked_break:
        explanation += " Open diamonds mark dated rows with a pre-break value."
    footer.text(
        0, 0.85, "Lines join consecutive usable days. Absent or unusable rates leave gaps, "
        "which can look dotted when repeated.", fontsize=9, color="#405268",
    )
    footer.text(0, 0.52, explanation, fontsize=9, color="#405268")
    footer.text(
        0, 0.19, f"Source  Bank for International Settlements · ZIP retrieved {capture_date} UTC",
        fontsize=9, color="#405268",
    )
    return figure


def write_chart(summary: ReportSummary, path: Path = CHART_PATH) -> Path:
    """Write the PNG using the noninteractive Agg backend."""
    figure = build_chart(summary)
    path.parent.mkdir(parents=True, exist_ok=True)
    latest_dates = ", ".join(
        f"{area.request.bis_code} {area.snapshot.latest.period}"
        for area in summary.areas if area.snapshot.latest is not None
    )
    # Embed the capture and observation dates so the PNG remains traceable when shared on its own.
    FigureCanvasAgg(figure).print_png(path, metadata={
        "Title": "Central bank policy rates",
        "Source": summary.source.get("archive_url"),
        "ArchiveSHA256": summary.source.get("sha256"),
        "RetrievedAt": summary.source["retrieved_at"],
        "LatestObservationDates": latest_dates,
    })
    return path
