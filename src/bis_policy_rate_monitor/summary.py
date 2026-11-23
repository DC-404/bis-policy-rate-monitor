"""Build the selected-area summary and chart inputs from the verified cache."""

import csv
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, cast

from bis_policy_rate_monitor.area_diagnostics import AreaDiagnostic, diagnose_area
from bis_policy_rate_monitor.derived import (
    METADATA_COLUMNS,
    OBSERVATION_COLUMNS,
    DerivedArtifact,
    load_derived_artifact,
)
from bis_policy_rate_monitor.fetch import DEFAULT_CACHE_DIR
from bis_policy_rate_monitor.flat_csv import AREA_CODE, ValueState
from bis_policy_rate_monitor.metadata_cache import (
    MetadataCacheError,
    MetadataCapture,
    load_cached_metadata,
)
from bis_policy_rate_monitor.snapshot import (
    DailyObservation,
    Snapshot,
    SnapshotStatus,
    calculate_snapshot,
)
from bis_policy_rate_monitor.transform import TransformError

AREA_ALIASES = {"EA": "XM"}
DEFAULT_COUNTRIES = "US,EA,GB,JP,CH"
DEFAULT_START = "2015-01-01"
STATUS_MESSAGES: dict[SnapshotStatus, str] = {
    "ok": "Latest and historical rates available.",
    "unavailable": "No daily observations for this area in this BIS capture.",
    "no_valid_latest": "This area has daily observations but no eligible rate.",
    "insufficient_history": "No eligible observation on or before the anniversary.",
    "series_blocked": "Calculations withheld due to a data quality conflict.",
    "incompatible_units": "Calculations withheld due to an unsupported unit or multiplier.",
}
COMPARISON_METADATA = ("title", "source_ref", "compilation", "break_notes")
WITHHELD_STATUSES = {"series_blocked", "incompatible_units"}
CONFLICT_FIELDS = {
    "OBS_VALUE": "observation value",
    "OBS_STATUS": "status",
    "OBS_CONF": "confidentiality code",
    "UNIT_MEASURE": "unit",
    "UNIT_MULT": "unit multiplier",
    "TITLE": "series title",
    "SOURCE_REF": "publication source",
    "COMPILATION": "compilation notes",
    "SUPP_INFO_BREAKS": "series break notes",
    "OBS_PRE_BREAK": "pre-break value",
}
CONFLICT_STATUS_LABELS = {"A": "normal value", "M": "missing value"}
SummaryValue = str | int | bool | None


@dataclass(frozen=True, slots=True)
class AreaRequest:
    requested_code: str
    bis_code: str


@dataclass(frozen=True, slots=True)
class ReportSelection:
    areas: tuple[AreaRequest, ...]
    start: date


@dataclass(frozen=True, slots=True)
class AreaSummary:
    request: AreaRequest
    label: str
    snapshot: Snapshot
    history: tuple[DailyObservation, ...]
    quality: dict[str, Any]
    code_check: AreaDiagnostic | None = None


@dataclass(frozen=True, slots=True)
class ReportSummary:
    selection: ReportSelection
    source: dict[str, Any]
    quality_notes: tuple[str, ...]
    areas: tuple[AreaSummary, ...]
    metadata: dict[str, dict[str, str]]
    quality_counts: dict[str, int] = field(default_factory=dict)
    sdmx: MetadataCapture | None = None

    def records(self) -> list[dict[str, SummaryValue]]:
        """Return flat rows in request order for both CSV and JSON output."""
        return [self._record(area) for area in self.areas]

    def _record(self, area: AreaSummary) -> dict[str, SummaryValue]:
        """Serialize one snapshot with lookup details, metadata, and source provenance."""
        snapshot = area.snapshot
        latest, historical = snapshot.latest, snapshot.historical
        latest_metadata = self.metadata.get(latest.metadata_id or "", {}) if latest else {}
        historical_metadata = (
            self.metadata.get(historical.metadata_id or "", {}) if historical else {}
        )
        differing_fields = [
            field for field in COMPARISON_METADATA
            if historical and latest_metadata.get(field) != historical_metadata.get(field)
        ]
        withheld = snapshot.status in WITHHELD_STATUSES
        # Keep exact decimal strings in the records shared by CSV, JSON, and the API.
        record: dict[str, SummaryValue] = {
            "requested_area": area.request.requested_code,
            "bis_area": area.request.bis_code,
            "area_label": area.label,
            "status": snapshot.status,
            "status_detail": _status_detail(area),
            "flow_id": self.source["flow_id"],
            "frequency": self.source["frequency"],
            "source_url": self.source["archive_url"],
            "source_sha256": self.source["sha256"],
            "source_retrieved_at": self.source["retrieved_at"],
            "source_release_date": self.source["catalogue_release_date"],
            "unit_measure": latest.unit if latest else None,
            "unit_label": latest_metadata.get("unit_label") or None,
            "unit_mult": latest.multiplier if latest else None,
            "newest_date": _period(snapshot.newest) if not withheld else None,
            "latest_date": _period(latest),
            "latest_rate": str(latest.eligible_rate) if latest else None,
            "latest_decimals": latest_metadata.get("decimals") or None,
            "latest_title": latest_metadata.get("title") or None,
            "latest_source_ref": latest_metadata.get("source_ref") or None,
            "latest_reason": snapshot.latest_reason,
            "latest_issues": _issues(snapshot.newest) if snapshot.latest_reason else None,
            "latest_age_days": snapshot.latest_age_days,
            "latest_is_stale": snapshot.latest_is_stale,
            "anniversary": snapshot.anniversary.isoformat() if snapshot.anniversary else None,
            "historical_date": _period(historical),
            "historical_rate": str(historical.eligible_rate) if historical else None,
            "historical_decimals": historical_metadata.get("decimals") or None,
            "historical_title": historical_metadata.get("title") or None,
            "historical_source_ref": historical_metadata.get("source_ref") or None,
            "historical_reason": snapshot.historical_reason,
            "historical_issues": _issues(snapshot.at_anniversary),
            "historical_lag_days": snapshot.historical_lag_days,
            "historical_is_stale": snapshot.historical_is_stale,
            "change_bp": str(snapshot.change_bp) if snapshot.change_bp is not None else None,
            "metadata_changes": "|".join(differing_fields) or None,
        }
        if self.sdmx is not None and area.code_check is not None:
            record.update({
                "area_code_status": area.code_check.code_status,
                "has_daily_series": area.code_check.has_daily_series,
                "area_suggestion": area.code_check.suggestion,
                "sdmx_flow_urn": self.sdmx.structure.flow_urn,
                "sdmx_dsd_urn": self.sdmx.structure.dsd_urn,
                "sdmx_area_codelist": self.sdmx.structure.area_codelist_urn,
                "sdmx_source_url": self.sdmx.source_url,
                "sdmx_source_sha256": self.sdmx.sha256,
                "sdmx_source_retrieved_at": self.sdmx.retrieved_at.isoformat(),
            })
        return record


def _period(row: DailyObservation | None) -> str | None:
    """Return an observation's ISO date or None when the observation is absent."""
    return row.period.isoformat() if row else None


def _issues(row: DailyObservation | None) -> str | None:
    """Join the diagnostic codes for an observation when present."""
    return "|".join(row.issues) if row and row.issues else None


def _conflict_detail(example: dict[str, Any]) -> str:
    """Display a disputed daily key and the evidence retained in the manifest."""
    introduction = f"Duplicate rows disagree for the same daily observation on {example['date']}."
    # Earlier transform manifests carry the disputed fields and source records without per-row
    # values.
    if not example.get("evidence"):
        fields = ", ".join(CONFLICT_FIELDS.get(field, field) for field in example["fields"])
        records = ", ".join(str(record) for record in example["records"])
        return (
            f"{introduction} Source records {records} differ in {fields}. "
            "Run bis-prates transform to show the conflicting values."
        )

    notes = [introduction]
    for record in example["evidence"]:
        entries = []
        # Put the rate before the status so that the two values read together.
        values = sorted(record["values"].items(), key=lambda item: item[0] != "OBS_VALUE")
        for field_id, value in values:
            if value is None:
                text = "(restricted)"
            elif not value.strip():
                text = "(blank)"
            elif field_id == "OBS_STATUS" and value in CONFLICT_STATUS_LABELS:
                text = f"{value} ({CONFLICT_STATUS_LABELS[value]})"
            else:
                text = " ".join(value.split())
                if field_id not in {"OBS_VALUE", "OBS_STATUS"}:
                    text = f"“{text}”"
            entries.append(f"{CONFLICT_FIELDS.get(field_id, field_id)} {text}")
        details = (" and " if len(entries) == 2 else ", ").join(entries)
        notes.append(f"Source record {record['record_number']} reports {details}.")
    return " ".join(notes)


def _status_detail(area: AreaSummary) -> str:
    """Combine the rate outcome with area diagnostics or conflict evidence."""
    status = area.snapshot.status
    message = STATUS_MESSAGES[status]
    if area.code_check is not None and area.code_check.message:
        if status == "unavailable":
            return area.code_check.message
        message = f"{message} {area.code_check.message}"
    if status in WITHHELD_STATUSES:
        examples = area.quality.get("examples", ())
        if status == "series_blocked":
            for example in examples:
                if example["kind"] == "duplicate_conflict":
                    return f"{message} {_conflict_detail(example)}"
        for example in examples:
            if example["kind"] == "unsupported_unit":
                fields = ", ".join(example["fields"])
                records = ", ".join(str(record) for record in example["records"])
                return (
                    f"{message} Example on {example['date']} "
                    f"({fields}, source records {records})."
                )
    return message


def parse_selection(
    countries: str = DEFAULT_COUNTRIES, start: str = DEFAULT_START,
) -> ReportSelection:
    """Validate the report options before reading any files."""
    areas: dict[str, AreaRequest] = {}
    for token in countries.split(","):
        requested = token.strip().upper()
        if AREA_CODE.fullmatch(requested) is None:
            raise ValueError("Provide comma-separated two-character area codes, such as US,EA.")
        # Look up the canonical BIS code and retain the request for reporting, including EA to XM.
        bis_code = AREA_ALIASES.get(requested, requested)
        if bis_code in areas:
            first = areas[bis_code].requested_code
            raise ValueError(
                f"Area requested more than once ({first}, {requested} map to {bis_code})."
            )
        areas[bis_code] = AreaRequest(requested, bis_code)

    message = "Provide a valid start date in YYYY-MM-DD format."
    try:
        start_date = date.fromisoformat(start)
    except ValueError as exc:
        raise ValueError(message) from exc
    if start_date.isoformat() != start:
        raise ValueError(message)
    return ReportSelection(tuple(areas.values()), start_date)


def _optional_metadata(cache_dir: Path) -> MetadataCapture | None:
    """Use the SDMX extension only when the local XML verifies."""
    try:
        return load_cached_metadata(cache_dir)
    # The flat archive still establishes daily coverage when optional SDMX metadata is unavailable.
    except MetadataCacheError:
        return None


@contextmanager
def _read_daily_tables(
    artifact: DerivedArtifact,
) -> Iterator[tuple[dict[str, dict[str, str]], csv.DictReader]]:
    """Read the metadata index and stream observations from the same verified artifact."""
    with artifact.metadata_path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != list(METADATA_COLUMNS):
            raise ValueError("Unexpected derived metadata columns.")
        metadata = {row["metadata_id"]: row for row in reader}

    with artifact.observations_path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        if reader.fieldnames != list(OBSERVATION_COLUMNS):
            raise ValueError("Unexpected derived daily observation columns.")
        yield metadata, reader


def list_daily_areas(*, cache_dir: Path = DEFAULT_CACHE_DIR) -> list[dict[str, SummaryValue]]:
    """List captured daily areas, including withheld series, with their display labels."""
    artifact = load_derived_artifact(cache_dir)
    sdmx = _optional_metadata(cache_dir)
    codelist = sdmx.structure.area_labels if sdmx is not None else {}
    requested_by_bis = {bis: requested for requested, bis in AREA_ALIASES.items()}
    metadata_ids: dict[str, str] = {}
    eligible_ids: dict[str, str] = {}
    try:
        with _read_daily_tables(artifact) as (metadata, observations):
            for row in observations:
                area = row["ref_area"]
                # The transform publishes each area's rows in ascending date order.
                metadata_ids[area] = row["metadata_id"]
                if row["eligible_rate"]:
                    eligible_ids[area] = row["metadata_id"]

        areas: list[dict[str, SummaryValue]] = []
        for area, quality in sorted(artifact.manifest["series_quality"].items()):
            metadata_id = eligible_ids.get(area, metadata_ids.get(area))
            label = metadata[metadata_id]["area_label"] if metadata_id is not None else ""
            areas.append({
                "requested_area": requested_by_bis.get(area, area),
                "bis_area": area,
                "area_label": label or codelist.get(area) or area,
                "blocked": quality["blocked"],
            })
        return areas
    except (OSError, ValueError, KeyError, TypeError, csv.Error) as exc:
        raise TransformError(
            f"Could not read the derived BIS data. Run bis-prates transform. {exc}"
        ) from exc


def chart_observations(
    history: tuple[DailyObservation, ...],
) -> Iterator[tuple[date, DailyObservation | None]]:
    """Yield dated rows and one marker per run of absent dates in ascending history."""
    previous = None
    for row in history:
        # One absent-date marker breaks the line across the gap without padding every day.
        if previous is not None and (row.period - previous).days > 1:
            yield previous + timedelta(days=1), None
        yield row.period, row
        previous = row.period


def build_summary(
    selection: ReportSelection, *, cache_dir: Path = DEFAULT_CACHE_DIR,
) -> ReportSummary:
    """Load the selected daily histories and calculate one snapshot per request."""
    artifact = load_derived_artifact(cache_dir)
    sdmx = _optional_metadata(cache_dir)
    observations: dict[str, list[DailyObservation]] = {
        area.bis_code: [] for area in selection.areas
    }
    usable_areas: set[str] = set()
    try:
        source = artifact.manifest["source"]
        retrieved_at = datetime.fromisoformat(source["retrieved_at"])
        if retrieved_at.utcoffset() is None:
            raise ValueError("Capture retrieval time has no time zone.")
        capture_date = retrieved_at.astimezone(UTC).date()
        quality = artifact.manifest["series_quality"]
        scan, resolved = artifact.manifest["scan"], artifact.manifest["resolved"]
        notes = tuple(scan["notes"])
        scan_keys = ("monthly_rows", "unsupported_frequency_rows", "invalid_daily_keys")
        resolved_keys = ("emitted_rows", "explicit_missing_rows", "exact_repeats")
        counts = {key: scan[key] for key in scan_keys}
        counts.update({key: resolved[key] for key in resolved_keys})

        with _read_daily_tables(artifact) as (metadata, daily_rows):
            for row in daily_rows:
                area = row["ref_area"]
                if sdmx is not None and row["eligible_rate"]:
                    # Collect coverage for suggestions even when the report requests a different
                    # subset.
                    usable_areas.add(area)
                if area not in observations:
                    continue
                attributes = metadata[row["metadata_id"]]
                observations[area].append(DailyObservation(
                    period=date.fromisoformat(row["time_period"]),
                    eligible_rate=Decimal(row["eligible_rate"]) if row["eligible_rate"] else None,
                    value_state=cast(ValueState, row["value_state"]),
                    unit=attributes["unit_measure"],
                    multiplier=attributes["unit_mult"],
                    issues=tuple(row["issues"].split("|")) if row["issues"] else (),
                    metadata_id=row["metadata_id"],
                    pre_break=row["obs_pre_break"] or None,
                ))

        areas = []
        for request in selection.areas:
            rows = observations[request.bis_code]
            series_quality = quality.get(request.bis_code, {})
            # The comparison uses the full history. Apply the chart start only to display rows.
            snapshot = calculate_snapshot(
                rows,
                capture_date=capture_date,
                series_blocked=series_quality.get("blocked", False),
            )
            # Use metadata attached to the row that represents the displayed series state.
            dated_row = snapshot.latest or snapshot.newest
            label = metadata[dated_row.metadata_id or ""]["area_label"] if dated_row else ""
            code_check = None
            if sdmx is not None:
                # Manifest coverage includes series whose conflicting rows were omitted.
                code_check = diagnose_area(
                    request.bis_code,
                    area_labels=sdmx.structure.area_labels,
                    daily_areas=quality.keys(),
                    usable_areas=usable_areas,
                )
                label = label or sdmx.structure.area_labels.get(request.bis_code, "")
            areas.append(AreaSummary(
                request=request,
                label=label or request.bis_code,
                snapshot=snapshot,
                history=tuple(row for row in rows if row.period >= selection.start),
                quality=series_quality,
                code_check=code_check,
            ))
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation, csv.Error) as exc:
        raise TransformError(
            f"Could not read the derived BIS data. Run bis-prates transform. {exc}"
        ) from exc

    return ReportSummary(selection, source, notes, tuple(areas), metadata, counts, sdmx=sdmx)
