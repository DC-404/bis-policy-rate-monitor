"""Publish resolved daily observations and their provenance for reporting."""

import csv
import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from bis_policy_rate_monitor.fetch import DEFAULT_CACHE_DIR, FetchError, load_cached_archive
from bis_policy_rate_monitor.flat_csv import FLOW_ID
from bis_policy_rate_monitor.quality import QualifiedCapture, SeriesQuality, qualify_daily_archive
from bis_policy_rate_monitor.transform import TransformError

MANIFEST_NAME = "transform-manifest.json"
OBSERVATIONS_NAME = "daily-observations.csv"
METADATA_NAME = "metadata.csv"
SCHEMA_VERSION = 1
OBSERVATION_COLUMNS = (
    "ref_area", "time_period", "eligible_rate", "raw_value", "value_state", "obs_status",
    "obs_conf", "obs_pre_break", "metadata_id", "source_record", "issues",
)
METADATA_COLUMNS = (
    "metadata_id", "area_label", "unit_measure", "unit_label", "unit_mult", "multiplier_label",
    "decimals", "decimals_label", "time_format", "time_format_label", "title", "source_ref",
    "compilation", "break_notes",
)


@dataclass(frozen=True, slots=True)
class DerivedArtifact:
    manifest_path: Path
    observations_path: Path
    metadata_path: Path
    manifest: dict[str, Any]


def _sha256(path: Path) -> str:
    """Hash a complete file without reading it into one buffer."""
    with path.open("rb") as content:
        return hashlib.file_digest(content, "sha256").hexdigest()


def _write_tables(directory: Path, capture: QualifiedCapture) -> int:
    """Write observations and deduplicated metadata, redacting restricted values."""
    metadata_ids: dict[tuple[str | None, ...], int] = {}
    with (
        (directory / OBSERVATIONS_NAME).open("w", encoding="utf-8", newline="") as observations,
        (directory / METADATA_NAME).open("w", encoding="utf-8", newline="") as metadata,
    ):
        observation_writer = csv.writer(observations)
        metadata_writer = csv.writer(metadata)
        observation_writer.writerow(OBSERVATION_COLUMNS)
        metadata_writer.writerow(METADATA_COLUMNS)

        for row in capture.observations():
            area = row.area.code
            assert area is not None
            # Join each dated observation to its own metadata, sharing identical tuples.
            attributes = (
                row.area.label, row.unit.code, row.unit.label, row.multiplier.code,
                row.multiplier.label, row.decimals.code, row.decimals.label, row.time_format.code,
                row.time_format.label, row.title, row.source_ref, row.compilation, row.break_notes,
            )
            metadata_id = metadata_ids.get(attributes)
            if metadata_id is None:
                metadata_id = len(metadata_ids) + 1
                metadata_ids[attributes] = metadata_id
                metadata_writer.writerow((metadata_id, *attributes))

            restricted = "restricted_value" in row.issues
            issues = row.issues
            if capture.series_quality[area].blocked:
                issues = (*issues, "series_blocked")
            observation_writer.writerow((
                area, row.period, row.eligible_rate, None if restricted else row.raw_value,
                row.value_state, row.status.code, row.confidentiality.code,
                None if restricted else row.pre_break, metadata_id, row.record_number,
                "|".join(issues),
            ))

    return len(metadata_ids)


def _series_summary(series: SeriesQuality) -> dict[str, Any]:
    """Serialize the quality counters and bounded examples for one area."""
    summary = asdict(series)
    summary["examples"] = [
        {
            "kind": event.kind,
            "date": event.date.isoformat(),
            "previous_date": event.previous_date.isoformat() if event.previous_date else None,
            "fields": list(event.fields),
            "records": list(event.records),
            **(
                {"evidence": [asdict(record) for record in event.evidence]}
                if event.evidence else {}
            ),
        }
        for event in series.examples
    ]
    return summary


def _manifest(
    capture: QualifiedCapture, cache_dir: Path, location: Path,
    hashes: dict[str, str], metadata_count: int,
) -> dict[str, Any]:
    """Bind the derived files and quality results to their verified source capture."""
    source = capture.scan.source
    scan = asdict(capture.scan)
    # Store archive provenance in the dedicated source section rather than duplicating it inside
    # scan counts.
    scan.pop("source")
    scan["notes"] = list(capture.scan.notes)
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "catalogue_url": source.catalogue_url,
            "archive_url": source.archive_url,
            "archive_path": source.archive_path.relative_to(cache_dir).as_posix(),
            "sha256": source.sha256,
            "byte_count": source.byte_count,
            "retrieved_at": source.retrieved_at.isoformat(),
            "catalogue_release_date": source.catalogue_release_date,
            "etag": source.etag,
            "last_modified": source.last_modified,
            "flow_id": FLOW_ID,
            "frequency": "D",
        },
        "artifact_dir": location.relative_to(cache_dir).as_posix(),
        "file_sha256": hashes,
        "scan": scan,
        "resolved": {
            "exact_repeats": capture.exact_repeats,
            "unique_keys": capture.unique_keys,
            "emitted_rows": capture.emitted_rows,
            "eligible_rates": capture.eligible_rates,
            "explicit_missing_rows": capture.explicit_missing_rows,
            "other_ineligible_rows": capture.other_ineligible_rows,
            "blocked_series": sum(series.blocked for series in capture.series_quality.values()),
            "metadata_rows": metadata_count,
        },
        "series_quality": {
            area: _series_summary(series) for area, series in capture.series_quality.items()
        },
    }


def _active_sha256(cache_dir: Path) -> str:
    """Return the verified active ZIP digest or raise a transform prerequisite error."""
    try:
        return load_cached_archive(cache_dir).sha256
    except FetchError as exc:
        raise TransformError(str(exc)) from exc


def _read_derived(cache_dir: Path, active_sha256: str) -> DerivedArtifact:
    """Open the derived generation only if its source and file digests verify."""
    manifest_path = cache_dir / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["schema_version"] != SCHEMA_VERSION:
            raise TransformError(
                "Unsupported transform manifest version. Run bis-prates transform."
            )
        # A perfectly intact transform can still describe a previously active archive.
        if manifest["source"]["sha256"] != active_sha256:
            raise TransformError(
                "The transform belongs to an older archive. Run bis-prates transform."
            )
        location = manifest["artifact_dir"]
        if not re.fullmatch(r"derived/[0-9a-f]{32}", location):
            raise TransformError("Invalid transform artifact path. Run bis-prates transform.")
        directory = cache_dir / location
        hashes = manifest["file_sha256"]
        for name in (OBSERVATIONS_NAME, METADATA_NAME):
            if not (directory / name).is_file() or _sha256(directory / name) != hashes[name]:
                raise TransformError(f"Invalid derived file {name}. Run bis-prates transform.")
    except TransformError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise TransformError("No readable transform manifest. Run bis-prates transform.") from exc

    return DerivedArtifact(
        manifest_path, directory / OBSERVATIONS_NAME, directory / METADATA_NAME, manifest
    )


def load_derived_artifact(cache_dir: Path = DEFAULT_CACHE_DIR) -> DerivedArtifact:
    """Open outputs that match the active source archive and their recorded checksums."""
    return _read_derived(cache_dir, _active_sha256(cache_dir))


def transform_archive(cache_dir: Path = DEFAULT_CACHE_DIR) -> DerivedArtifact:
    """Qualify the active capture and atomically publish the derived manifest."""
    capture = qualify_daily_archive(cache_dir)
    source_sha256 = capture.scan.source.sha256
    manifest_path = cache_dir / MANIFEST_NAME
    try:
        with TemporaryDirectory(dir=cache_dir, prefix=".transform-") as temporary:
            staging = Path(temporary)
            tables = staging / "tables"
            tables.mkdir()
            metadata_count = _write_tables(tables, capture)
            hashes = {name: _sha256(tables / name) for name in (OBSERVATIONS_NAME, METADATA_NAME)}

            try:
                previous = _read_derived(cache_dir, source_sha256)
            except TransformError:
                previous = None
            # Reuse the validated table directory when both generated CSVs are byte-identical.
            if previous and previous.manifest["file_sha256"] == hashes:
                location = previous.observations_path.parent
            else:
                location = cache_dir / "derived" / uuid4().hex
                location.parent.mkdir(parents=True, exist_ok=True)
                tables.rename(location)

            manifest = _manifest(capture, cache_dir, location, hashes, metadata_count)
            staged_manifest = staging / MANIFEST_NAME
            staged_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            # A concurrent fetch may have replaced the active ZIP during the scan.
            if _active_sha256(cache_dir) != source_sha256:
                raise TransformError(
                    "The active BIS archive changed. Run bis-prates transform again."
                )
            # The published manifest points to a complete pair of files.
            staged_manifest.replace(manifest_path)
    except OSError as exc:
        raise TransformError(
            f"Could not publish the derived BIS data in {cache_dir}: {exc}"
        ) from exc

    return DerivedArtifact(
        manifest_path, location / OBSERVATIONS_NAME, location / METADATA_NAME, manifest
    )
