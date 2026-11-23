"""Write a reader-facing Markdown report from the shared summary records."""

import re
from datetime import UTC, datetime
from decimal import Decimal
from html import escape
from pathlib import Path

from bis_policy_rate_monitor.snapshot import STALE_AFTER_DAYS
from bis_policy_rate_monitor.summary import AreaSummary, ReportSummary, SummaryValue

REPORT_PATH = Path("out/report.md")
BIS_POLICY_RATES_URL = "https://data.bis.org/topics/CBPOL"
STATUS_LABELS = {
    "ok": "Available",
    "unavailable": "Unavailable in capture",
    "no_valid_latest": "No usable rate",
    "insufficient_history": "Insufficient history",
    "series_blocked": "Withheld for conflict",
    "incompatible_units": "Withheld for units",
}
WITHHELD = {"series_blocked", "incompatible_units"}
METADATA_LABELS = {
    "title": "series title",
    "source_ref": "publication source",
    "compilation": "compilation notes",
    "break_notes": "series break notes",
}


def _plain_text(value: SummaryValue) -> str:
    """Normalize and escape source text for Markdown prose and table cells."""
    # Source metadata is plain text and may contain characters that could alter Markdown.
    text = escape(" ".join(str(value).split()), quote=False)
    return re.sub(r"([\\`*_\[\]|~])", r"\\\1", text)


def _decimal_text(
    value: SummaryValue, decimals: SummaryValue = None, *, signed: bool = False,
) -> str:
    """Format an exact decimal for display using the source precision when available."""
    number = Decimal(str(value))
    if number.is_zero():
        number = number.copy_abs()

    whole, _, fraction = format(number, "f").partition(".")
    code = str(decimals).lstrip("0")
    # Display precision is a small numeric code. Keep exact digits if it is unusable.
    places = int(code or "0") if re.fullmatch(r"[0-9]{0,2}", code) else 0
    fraction = fraction.rstrip("0").ljust(places, "0")
    prefix = "+" if signed and number > 0 else ""
    return prefix + (f"{whole}.{fraction}" if fraction else whole)


def _rate_and_date(row: dict[str, SummaryValue], period: str) -> str:
    """Show the rate with the date actually used, or a dash for a missing rate."""
    rate = row[f"{period}_rate"]
    if rate is None:
        return "—"
    formatted = _decimal_text(rate, row[f"{period}_decimals"])
    return f"{formatted}% ({row[f'{period}_date']})"


def _area_name(row: dict[str, SummaryValue]) -> str:
    """Display the source label and the requested and canonical codes when they differ."""
    requested, bis_area = row["requested_area"], row["bis_area"]
    codes = str(requested) if requested == bis_area else f"{requested}/{bis_area}"
    label = _plain_text(row["area_label"])
    return codes if label == codes else f"{label} ({codes})"


def _reason(reason: SummaryValue, issues: SummaryValue) -> str:
    """Describe why a date could not supply an eligible observation."""
    if reason == "absent_date":
        return "has no daily observation in the source"
    if reason == "explicit_missing":
        return "has an observation marked missing by BIS"
    if "restricted_value" in str(issues).split("|"):
        return "has an observation with a restricted value"
    return "has an observation that could not be used"


def _days(value: SummaryValue) -> str:
    """Format the number of calendar days for an observation lookup note."""
    return f"{value} calendar day" + ("" if value == 1 else "s")


def _lookup_notes(row: dict[str, SummaryValue]) -> list[str]:
    """Explain fallback dates, observation age, and insufficient history."""
    messages = []
    if row["status"] in {"unavailable", "no_valid_latest"}:
        messages.append(_plain_text(row["status_detail"]))

    if row["latest_reason"]:
        reason = _reason(row["latest_reason"], row["latest_issues"])
        messages.append(f"The newest date, {row['newest_date']}, {reason}.")
        if row["latest_date"]:
            messages.append(f"The latest usable rate is from {row['latest_date']}.")

    anniversary = row["anniversary"]
    if row["historical_reason"]:
        reason = _reason(row["historical_reason"], row["historical_issues"])
        messages.append(f"The anniversary, {anniversary}, {reason}.")
        if row["historical_date"]:
            lag = _days(row["historical_lag_days"])
            messages.append(f"The comparison uses {row['historical_date']}, {lag} earlier.")
    if row["status"] == "insufficient_history":
        if anniversary:
            messages.append(f"The comparison needs an eligible rate on or before {anniversary}.")
        else:
            messages.append("The latest observation date has no representable prior year.")
    if row["latest_is_stale"]:
        age = _days(row["latest_age_days"])
        messages.append(f"The latest rate was {age} old when the ZIP was retrieved.")
    if row["historical_is_stale"]:
        messages.append(
            f"The comparison date is more than {STALE_AFTER_DAYS} days before the anniversary."
        )
    return messages


def _area_quality(area: AreaSummary, row: dict[str, SummaryValue]) -> list[str]:
    """Describe withheld rates, source disagreements, and metadata changes for one area."""
    messages = []
    if row["status"] in WITHHELD:
        messages.append(_plain_text(row["status_detail"]))
    elif area.code_check is not None and area.code_check.code_status == "codelist_mismatch":
        messages.append(_plain_text(area.code_check.message))
    if area.quality.get("cosmetic_keys"):
        messages.append(
            "Some duplicate rows differed in descriptive details such as labels or precision."
        )
    if row["metadata_changes"]:
        changed_fields = str(row["metadata_changes"]).split("|")
        fields = ", ".join(METADATA_LABELS[field] for field in changed_fields)
        messages.append(f"The selected rates have different series metadata ({fields}).")
        messages.append("Changes in the instrument or calculation method can affect comparability.")
    elif area.quality.get("metadata_changes"):
        messages.append("BIS series metadata changed during the available history.")
    return messages


def _count(value: int, noun: str) -> str:
    """Format a capture count with thousands separators and the matching noun."""
    return f"{value:,} {noun}" + ("" if value == 1 else "s")


def _capture_quality(summary: ReportSummary) -> list[str]:
    """Summarize the scan and resolution counts and include capture-wide notes."""
    counts = summary.quality_counts
    messages = []
    if counts:
        daily = _count(counts["emitted_rows"], "daily observation")
        monthly = _count(counts["monthly_rows"], "monthly row")
        missing = _count(counts["explicit_missing_rows"], "dated missing value")
        messages.append(
            f"Across all areas, the analysis retained {daily} and excluded {monthly}. "
            f"The daily observations include {missing}."
        )
        for key, description in (
            ("unsupported_frequency_rows", "with unsupported frequencies"),
            ("invalid_daily_keys", "with an invalid daily date or area"),
        ):
            if counts[key]:
                excluded = _count(counts[key], "row")
                messages.append(f"The analysis excluded {excluded} {description}.")
        if counts["exact_repeats"]:
            duplicates = _count(counts["exact_repeats"], "exact duplicate row")
            messages.append(f"The analysis collapsed {duplicates}.")
    messages.extend(
        note.replace("TIME_FORMAT", "`TIME_FORMAT`").replace(
            "reported frequency.", "reported frequency (`FREQ`)."
        )
        for note in summary.quality_notes
    )
    return messages


def render_report(summary: ReportSummary) -> str:
    """Render the standalone report with an image link relative to its output folder."""
    rows = summary.records()
    source = summary.source
    retrieved = datetime.fromisoformat(source["retrieved_at"]).astimezone(UTC)
    provenance = f"ZIP retrieved **{retrieved:%Y-%m-%d %H:%M UTC}**."
    if source["catalogue_release_date"]:
        release = _plain_text(source["catalogue_release_date"])
        provenance = f"BIS catalogue release **{release}**. {provenance}"

    lines = [
        "# BIS Policy Rate Monitor",
        "",
        provenance,
        "",
        "## Latest rates and 12-month changes",
        "",
        "Rates are in per cent per year. Each date is the actual observation date for that area.",
        "",
        "| Area | Latest rate (date) | 12-month rate (date) | Change (bp) | Status |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    # Keep selection and fallback explanations separate from observations about capture quality.
    date_notes, quality_notes = [], []
    for area, row in zip(summary.areas, rows, strict=True):
        name = _area_name(row)
        bp = row["change_bp"]
        change = "—" if bp is None else _decimal_text(bp, signed=True)
        lines.append(
            f"| {name} | {_rate_and_date(row, 'latest')} | {_rate_and_date(row, 'historical')} "
            f"| {change} | {STATUS_LABELS[row['status']]} |"
        )
        if messages := _lookup_notes(row):
            date_notes.append(f"- **{name}.** " + " ".join(messages))
        if messages := _area_quality(area, row):
            quality_notes.append(f"- **{name}.** " + " ".join(messages))

    if date_notes:
        lines.extend(["", "### Observation date notes", "", *date_notes])
    lines.extend([
        "", "## Daily policy rates", "",
        "![Daily central bank policy rates for the selected areas](policy_rates.png)", "",
        f"The chart starts on {summary.selection.start}. "
        "The comparison can use earlier observations.",
        "", "## How the comparison works", "",
        "For each area, we subtract one calendar year from its latest usable observation date. "
        "A 29 February date maps to 28 February when needed. We compare with the most recent "
        "usable observation on or before that anniversary.",
        "",
        "One basis point (bp) is 0.01 percentage points. We subtract the historical rate from "
        "the latest rate and multiply by 100.",
        "",
        f"We flag a latest rate more than {STALE_AFTER_DAYS} calendar days before ZIP retrieval "
        f"and a historical rate more than {STALE_AFTER_DAYS} calendar days before the anniversary.",
        "", "## Data quality", "",
        *(f"- {note}" for note in _capture_quality(summary)),
        *quality_notes,
        "", "## Source", "",
        "Bank for International Settlements, Central bank policy rates. "
        f"[BIS Data Portal]({BIS_POLICY_RATES_URL}) · [Flat CSV ZIP]({source['archive_url']})",
        "",
        f"Archive SHA-256 `{source['sha256']}`.",
        "",
    ])
    # Show separately retrieved XML provenance only when the metadata passed verification.
    if summary.sdmx is not None:
        metadata = summary.sdmx
        lines.extend([
            "Area validation uses the cached BIS SDMX structure and its referenced codelist "
            f"`{metadata.structure.area_codelist_urn}`.",
            "",
            f"Metadata retrieved `{metadata.retrieved_at.isoformat()}` · "
            f"XML SHA-256 `{metadata.sha256}`. [BIS SDMX structure]({metadata.source_url})",
            "",
        ])
    return "\n".join(lines)


def write_report(summary: ReportSummary, path: Path = REPORT_PATH) -> Path:
    """Write the UTF-8 Markdown report alongside the chart."""
    content = render_report(summary)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path
