"""Verify the streamed scan, quality policy, derived files and report explanations."""

import csv
import io
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest
from typer.testing import CliRunner

import bis_policy_rate_monitor.transform as transform
from bis_policy_rate_monitor.cli import app
from bis_policy_rate_monitor.derived import load_derived_artifact, transform_archive
from bis_policy_rate_monitor.fetch import BIS_CATALOGUE_URL, CSV_MEMBER_NAME, fetch_archive
from bis_policy_rate_monitor.flat_csv import FlatHeader, FlatObservation
from bis_policy_rate_monitor.quality import qualify_daily_archive
from bis_policy_rate_monitor.report import write_report
from bis_policy_rate_monitor.summary import build_summary, parse_selection
from bis_policy_rate_monitor.transform import TransformError, scan_daily_archive

FIXTURES = Path(__file__).parent / "fixtures"
CATALOGUE = '<a href="/policy-rates.zip">Central bank policy rates (CSV, flat)</a>'


def seed_cache(cache_dir: Path, body: bytes, *, refresh: bool = False) -> None:
    """Prepare the active fixture ZIP through the same validation as a real fetch."""
    def reply(request: httpx.Request) -> httpx.Response:
        if str(request.url) == BIS_CATALOGUE_URL:
            return httpx.Response(200, text=CATALOGUE)
        assert str(request.url) == "https://data.bis.org/policy-rates.zip"
        return httpx.Response(200, stream=httpx.ByteStream(body))

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        fetch_archive(client, cache_dir, refresh=refresh)


def csv_archive(text: str) -> bytes:
    """Wrap custom CSV text in the exact expected ZIP member."""
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(CSV_MEMBER_NAME, text)
    return output.getvalue()


def test_scan_emits_daily_rows_and_preserves_the_capture_identity(tmp_path: Path) -> None:
    """Keep source positions, release metadata and daily-scan diagnostics intact."""
    seed_cache(tmp_path, (FIXTURES / "synthetic-cbpol-flat.zip").read_bytes())
    manifest_path = tmp_path / "fetch-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["retrieved_at"] = "2000-01-01T00:00:00+00:00"
    # Set stable retrieval provenance so the assertions do not depend on the clock.
    manifest_path.write_text(json.dumps(manifest))
    expanded_size = (FIXTURES / CSV_MEMBER_NAME).stat().st_size
    emitted: list[FlatObservation] = []

    # Collect emitted rows to compare their source positions and original cell values.
    def consume(header: FlatHeader, row: FlatObservation) -> None:
        assert len(header.field_ids) == 18
        emitted.append(row)

    result = scan_daily_archive(consume, tmp_path, expanded_limit=expanded_size)

    assert result.source.sha256 == manifest["sha256"]
    assert result.source.retrieved_at == datetime(2000, 1, 1, tzinfo=UTC)
    assert result.expanded_bytes == expanded_size
    assert (result.total_rows, result.daily_rows, result.monthly_rows) == (13, 12, 1)
    assert len(emitted) == 12 and all(row.frequency.code == "D" for row in emitted)
    assert 6 not in {row.record_number for row in emitted}
    assert emitted[0].raw_cells == emitted[1].raw_cells
    assert (result.eligible_daily_rates, result.explicit_missing_daily_rows) == (9, 3)
    assert result.notes == (
        "BIS left TIME_FORMAT blank on 13 of 13 rows. "
        "We validated dates using the reported frequency.",
    )


def test_scan_counts_bad_keys_values_and_frequencies(tmp_path: Path) -> None:
    """Count malformed daily keys and unusable rates separately from excluded frequencies."""
    with (FIXTURES / CSV_MEMBER_NAME).open(newline="", encoding="utf-8") as source:
        header, daily, *_ = csv.reader(source)
    positions = {name.partition(":")[0]: index for index, name in enumerate(header)}
    rows = [daily]
    # Make distinct changes to the period, rate and frequency on copies of a source row.
    for field, value in (("TIME_PERIOD", "2025-02-30"), ("OBS_VALUE", "NaN"), ("FREQ", "Q")):
        edited = daily.copy()
        edited[positions[field]] = value
        rows.append(edited)
    output = io.StringIO(newline="")
    csv.writer(output).writerows([header, *rows])
    seed_cache(tmp_path, csv_archive(output.getvalue()))
    emitted = []

    result = scan_daily_archive(lambda header, row: emitted.append(row.record_number), tmp_path)

    assert (result.total_rows, result.daily_rows, result.unsupported_frequency_rows) == (4, 3, 1)
    assert result.invalid_daily_keys == result.eligible_daily_rates == 1
    assert result.other_ineligible_daily_rows == 1
    assert result.issue_counts == {
        "invalid_period": 1, "normal_status_with_nan": 1, "unsupported_frequency": 1,
    }
    # Only daily rows with usable keys are emitted, including an ineligible rate.
    assert emitted == [1, 3]


def test_scan_rejects_expanded_data_over_the_byte_limit(tmp_path: Path) -> None:
    """Enforce actual bytes read while streaming even after fetch validation passed."""
    seed_cache(tmp_path, (FIXTURES / "synthetic-cbpol-flat.zip").read_bytes())

    # Choose a separate scan limit below the verified fixture's expanded size.
    with pytest.raises(TransformError, match="expanded BIS CSV exceeds the 1,024-byte read limit"):
        scan_daily_archive(lambda header, row: None, tmp_path, expanded_limit=1024)


def test_bad_crc_blocks_the_scan_even_after_fetch_checks_pass(tmp_path: Path) -> None:
    """Verify end-of-stream CRC rather than relying on member metadata alone."""
    text = (FIXTURES / CSV_MEMBER_NAME).read_text()
    original = csv_archive(text)
    # Replace equal-length payload bytes in the stored member without changing its CRC.
    corrupt = original.replace(b"Synthetic source", b"Different source", 1)
    assert len(corrupt) == len(original) and corrupt != original
    seed_cache(tmp_path, corrupt)

    with pytest.raises(TransformError, match="Bad CRC-32"):
        scan_daily_archive(lambda header, row: None, tmp_path)


def test_scan_rejects_a_row_width_change(tmp_path: Path) -> None:
    """Report the offending source record when the member shape changes midway."""
    text = (FIXTURES / CSV_MEMBER_NAME).read_text()
    # Append a one-cell record to a structurally valid fixture ZIP.
    seed_cache(tmp_path, csv_archive(text + "dataflow\n"))

    with pytest.raises(TransformError, match="CSV record 14 has 1 cells. Expected 18"):
        scan_daily_archive(lambda header, row: None, tmp_path)


def test_csv_record_limit_counts_across_quoted_newlines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound a logical CSV record even when it contains many short physical lines."""
    with (FIXTURES / CSV_MEMBER_NAME).open(newline="", encoding="utf-8") as source:
        header, daily, *_ = csv.reader(source)
    compilation = next(i for i, name in enumerate(header) if name.startswith("COMPILATION:"))
    # Build a valid quoted field whose combined lines exceed the lowered record limit.
    daily[compilation] = "Synthetic line\n" * 100
    output = io.StringIO(newline="")
    csv.writer(output).writerows([header, daily])
    seed_cache(tmp_path, csv_archive(output.getvalue()))
    monkeypatch.setattr(transform, "MAX_CSV_RECORD_CHARS", 1024)

    with pytest.raises(TransformError, match="record exceeds the decoded character limit"):
        scan_daily_archive(lambda header, row: None, tmp_path)


def test_quality_resolves_fixture_repeats_and_contains_conflicts(tmp_path: Path) -> None:
    """Collapse exact repeats and distinguish blocking evidence from cosmetic changes."""
    seed_cache(tmp_path, (FIXTURES / "synthetic-cbpol-flat.zip").read_bytes())

    capture = qualify_daily_archive(tmp_path)
    rows = {(row.area.code, row.period): row for row in capture.observations()}

    assert (capture.exact_repeats, capture.unique_keys, capture.emitted_rows) == (1, 9, 8)
    outcomes = capture.eligible_rates, capture.explicit_missing_rows, capture.other_ineligible_rows
    assert outcomes == (5, 2, 1)
    # The conflicting GB key is omitted and the rest of the series is withheld.
    gb = capture.series_quality["GB"]
    assert gb.blocked and gb.conflicting_keys == 1
    conflict, = gb.examples
    assert conflict.date == date(2026, 11, 17)
    assert conflict.fields == ("OBS_STATUS", "OBS_VALUE") and conflict.records == (11, 12)
    assert ("GB", date(2026, 11, 17)) not in rows
    assert rows["GB", date(2025, 11, 17)].eligible_rate is None

    # A same-key label difference stays cosmetic while a dated compilation change remains visible.
    us = capture.series_quality["US"]
    assert not us.blocked and us.cosmetic_keys == us.metadata_changes == 1
    warning, change = us.examples
    assert warning.fields == ("REF_AREA",) and warning.records == (8, 9)
    assert change.fields == ("COMPILATION",) and change.records == (7, 8)
    assert change.previous_date == date(2025, 11, 17)
    earlier_us = rows["US", date(2025, 11, 17)]
    assert earlier_us.compilation == "Instrument: legacy target, synthetic method"
    latest_us = rows["US", date(2026, 11, 18)]
    assert latest_us.compilation == "Instrument: current target, synthetic method"
    assert latest_us.eligible_rate == Decimal("3.00") and latest_us.area.label is None
    assert rows["XM", date(2026, 11, 16)].eligible_rate == Decimal("4.50")


def test_quality_groups_nonadjacent_keys_and_detects_later_exact_repeats(tmp_path: Path) -> None:
    """Resolve duplicates and metadata changes independently of input ordering."""
    text = (FIXTURES / CSV_MEMBER_NAME).read_text()
    header, *rows = csv.reader(io.StringIO(text))
    output = io.StringIO(newline="")
    # Reverse the fixture and repeat a cosmetic variant after all other keys.
    csv.writer(output).writerows([header, *reversed(rows), rows[7]])
    seed_cache(tmp_path, csv_archive(output.getvalue()))

    capture = qualify_daily_archive(tmp_path)

    assert (capture.exact_repeats, capture.unique_keys, capture.eligible_rates) == (2, 9, 5)
    assert capture.series_quality["GB"].blocked
    # Verify the change points follow chronological order even though the fixture was reversed.
    change, = [e for e in capture.series_quality["US"].examples if e.kind == "metadata_change"]
    assert (change.previous_date, change.date) == (date(2025, 11, 17), date(2026, 11, 18))


def test_quality_uses_neutral_precision_for_equivalent_rates(tmp_path: Path) -> None:
    """Keep equivalent finite values and discard a disputed display precision hint."""
    header, daily, *_ = csv.reader(io.StringIO((FIXTURES / CSV_MEMBER_NAME).read_text()))
    alternative = daily.copy()
    # Use differently formatted tokens for the same number and change only the precision attribute.
    alternative[6] = "4.0"
    alternative[11] = "2: Two"
    output = io.StringIO(newline="")
    csv.writer(output).writerows([header, daily, alternative])
    seed_cache(tmp_path, csv_archive(output.getvalue()))

    capture = qualify_daily_archive(tmp_path)

    row, = capture.observations()
    event, = capture.series_quality["XM"].examples
    assert capture.exact_repeats == 0 and event.fields == ("DECIMALS", "OBS_VALUE")
    assert row.eligible_rate == Decimal("4.00") and row.decimals.code is None


def test_unsupported_multiplier_blocks_the_series_even_on_a_missing_row(tmp_path: Path) -> None:
    """Catch an incompatible multiplier even if that dated row has no usable rate."""
    columns, *rows = csv.reader(io.StringIO((FIXTURES / CSV_MEMBER_NAME).read_text()))
    # Alter the multiplier on the explicit missing row, leaving the valid rows alone.
    rows[2][8] = "1: Tens"
    output = io.StringIO(newline="")
    csv.writer(output).writerows([columns, *rows])
    seed_cache(tmp_path, csv_archive(output.getvalue()))

    capture = qualify_daily_archive(tmp_path)

    xm = capture.series_quality["XM"]
    assert xm.blocked and xm.unsupported_unit_rows == 1 and xm.conflicting_keys == 0
    event, = xm.examples
    assert event.fields == ("UNIT_MULT",) and event.records == (3,)
    assert all(row.eligible_rate is None for row in capture.observations() if row.area.code == "XM")
    # An unrelated series stays eligible.
    assert not capture.series_quality["US"].blocked


def test_disputed_compilation_blocks_an_otherwise_matching_rate(tmp_path: Path) -> None:
    """Treat conflicting instrument descriptions as material even at equal rates."""
    header, daily, *_ = csv.reader(io.StringIO((FIXTURES / CSV_MEMBER_NAME).read_text()))
    alternative = daily.copy()
    # Change only the source compilation note at the same daily observation key.
    alternative[10] = "Different synthetic policy instrument"
    output = io.StringIO(newline="")
    csv.writer(output).writerows([header, daily, alternative])
    seed_cache(tmp_path, csv_archive(output.getvalue()))

    capture = qualify_daily_archive(tmp_path)

    series = capture.series_quality["XM"]
    event, = series.examples
    assert series.blocked and event.fields == ("COMPILATION",)
    assert event.records == (1, 2)
    assert list(capture.observations()) == []


def test_transform_cli_writes_observations_metadata_and_quality(tmp_path: Path) -> None:
    """Verify the output schema, metadata references and source provenance together."""
    seed_cache(tmp_path, (FIXTURES / "synthetic-cbpol-flat.zip").read_bytes())

    result = CliRunner().invoke(app, ["transform", "--cache-dir", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "Wrote 8 daily rows" in result.stdout
    artifact = load_derived_artifact(tmp_path)
    # Read both published CSVs independently and rebuild the metadata lookup.
    with artifact.observations_path.open(newline="", encoding="utf-8") as observations:
        rows = list(csv.DictReader(observations))
    with artifact.metadata_path.open(newline="", encoding="utf-8") as metadata:
        attributes = {row["metadata_id"]: row for row in csv.DictReader(metadata)}
    keys = [(row["ref_area"], row["time_period"]) for row in rows]
    assert len(keys) == len(set(keys)) == 8 and keys == sorted(keys)
    assert ("GB", "2026-11-17") not in keys
    gb, = [row for row in rows if row["ref_area"] == "GB"]
    assert (gb["eligible_rate"], gb["issues"]) == ("", "series_blocked")
    xm = {row["time_period"]: row for row in rows if row["ref_area"] == "XM"}
    assert xm["2026-11-16"]["eligible_rate"] == "4.50"
    assert xm["2026-11-16"]["source_record"] == "4"
    # Two observations with the same attributes should share one stored metadata row.
    assert xm["2026-11-16"]["metadata_id"] == xm["2025-11-14"]["metadata_id"]
    missing = xm["2026-11-17"]
    assert (missing["eligible_rate"], missing["raw_value"], missing["value_state"]) == (
        "", "NaN", "missing",
    )
    latest_us = next(row for row in rows if row["source_record"] == "8")
    assert attributes[latest_us["metadata_id"]]["area_label"] == ""
    assert attributes[latest_us["metadata_id"]]["compilation"] == (
        "Instrument: current target, synthetic method"
    )
    # The transform manifest must identify the exact cache capture used for both outputs.
    fetch = json.loads((tmp_path / "fetch-manifest.json").read_text())
    manifest = artifact.manifest
    assert manifest["source"]["sha256"] == fetch["sha256"]
    assert manifest["source"]["retrieved_at"] == fetch["retrieved_at"]
    assert manifest["resolved"]["exact_repeats"] == manifest["resolved"]["blocked_series"] == 1
    assert manifest["resolved"]["metadata_rows"] == len(attributes) == 5
    event, = manifest["series_quality"]["GB"]["examples"]
    assert event["date"] == "2026-11-17" and event["records"] == [11, 12]


def test_summary_preserves_independent_dates_and_statuses(tmp_path: Path) -> None:
    """Preserve per-area rates, dates and reasons across a mixed report selection."""
    seed_cache(tmp_path, (FIXTURES / "synthetic-cbpol-flat.zip").read_bytes())
    artifact = transform_archive(tmp_path)
    selection = parse_selection(" us , ea, GB, JP, CH", "2026-11-17")

    report = build_summary(selection, cache_dir=tmp_path)

    # Include available, withheld, absent and insufficient-history areas in one result.
    us, xm, gb, jp, ch = records = report.records()
    assert [row["requested_area"] for row in records] == ["US", "EA", "GB", "JP", "CH"]
    assert all(row.keys() == us.keys() for row in records)
    assert all(row["source_sha256"] == artifact.manifest["source"]["sha256"] for row in records)
    assert report.quality_notes == tuple(artifact.manifest["scan"]["notes"])
    assert (us["status"], us["latest_date"], us["latest_rate"], us["change_bp"]) == (
        "ok", "2026-11-18", "3.00", "-25.00",
    )
    assert us["area_label"] == "US"
    assert us["historical_reason"] == "absent_date" and us["metadata_changes"] == "compilation"
    assert (xm["bis_area"], xm["area_label"], xm["latest_date"], xm["historical_date"]) == (
        "XM", "Euro area", "2026-11-16", "2025-11-14",
    )
    assert (xm["latest_reason"], xm["historical_reason"], xm["historical_lag_days"]) == (
        "explicit_missing", "explicit_missing", 2,
    )
    assert (xm["latest_rate"], xm["historical_rate"], xm["change_bp"]) == ("4.50", "4.00", "50.00")
    assert (xm["unit_measure"], xm["unit_mult"], xm["latest_decimals"], xm["historical_title"]) == (
        "368", "0", "4", "Synthetic daily euro area rate",
    )
    # The chart window can contain only a missing row while the snapshot still uses older rates.
    missing, = report.areas[1].history
    assert (missing.period, missing.value_state, missing.eligible_rate) == (
        date(2026, 11, 17), "missing", None,
    )
    assert gb["status"] == "series_blocked" and gb["change_bp"] is None
    assert jp["status"] == "unavailable" and jp["latest_rate"] is None
    assert jp["status_detail"] == "No daily observations for this area in this BIS capture."
    assert (ch["status"], ch["latest_rate"], ch["historical_rate"], ch["change_bp"]) == (
        "insufficient_history", "-0.25", None, None,
    )


def test_summary_reports_conflict_when_no_observations_for_the_area_survive(tmp_path: Path) -> None:
    """Keep conflict explanations visible when the derived table has no usable area rows."""
    with (FIXTURES / CSV_MEMBER_NAME).open(encoding="utf-8", newline="") as source:
        header, *rows = csv.reader(source)
    output = io.StringIO(newline="")
    # Keep only the two conflicting GB rows to leave no surviving observation key.
    csv.writer(output).writerows([header, rows[10], rows[11]])
    seed_cache(tmp_path, csv_archive(output.getvalue()))
    artifact = transform_archive(tmp_path)

    report = build_summary(parse_selection("GB"), cache_dir=tmp_path)

    row, = report.records()
    assert row["status"] == "series_blocked" and row["newest_date"] is None
    assert row["latest_rate"] is None and report.areas[0].history == ()
    assert "same daily observation on 2026-11-17" in row["status_detail"]
    assert "Source record 1 reports observation value 5.00" in row["status_detail"]
    assert "Source record 2 reports observation value NaN" in row["status_detail"]

    # Manifests from earlier transforms can still explain a dispute by record number.
    del artifact.manifest["series_quality"]["GB"]["examples"][0]["evidence"]
    artifact.manifest_path.write_text(json.dumps(artifact.manifest), encoding="utf-8")
    legacy, = build_summary(parse_selection("GB"), cache_dir=tmp_path).records()
    assert "Source records 1, 2 differ in status, observation value." in legacy["status_detail"]
    assert "Run bis-prates transform" in legacy["status_detail"]


def test_report_explains_fallbacks_and_capture_quality(tmp_path: Path) -> None:
    """Render the exact rates, fallback explanations and provenance for real user review."""
    fixture = (FIXTURES / CSV_MEMBER_NAME).read_text(encoding="utf-8")
    seed_cache(tmp_path, csv_archive(fixture.replace(",-0.25,", ",-0.2500123,")))
    manifest_path = tmp_path / "fetch-manifest.json"
    fetch = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Fix retrieval time well after the dated fixture to test the age warning.
    fetch["retrieved_at"] = "2026-12-01T03:15:00+00:00"
    manifest_path.write_text(json.dumps(fetch), encoding="utf-8")
    transform_archive(tmp_path)
    selection = parse_selection("EA,US,GB,JP,CH", "2026-11-17")
    summary = build_summary(selection, cache_dir=tmp_path)

    report_path = write_report(summary, tmp_path / "out" / "report.md")
    report = report_path.read_text(encoding="utf-8")

    assert "| Euro area (EA/XM) | 4.5000% (2026-11-16) | 4.0000% (2025-11-14) | +50 |" in report
    assert "| US | 3.0000% (2026-11-18) | 3.2500% (2025-11-17) | -25 |" in report
    assert "| United Kingdom (GB) | — | — | — | Withheld for conflict |" in report
    assert "| JP | — | — | — | Unavailable in capture |" in report
    swiss_row = "| Switzerland (CH) | -0.2500123% (2026-11-17) | — | — | Insufficient history |"
    assert swiss_row in report
    # Check the prose distinguishes an explicitly missing date from an absent row.
    assert "The newest date, 2026-11-17, has an observation marked missing by BIS." in report
    assert "The anniversary, 2025-11-16, has an observation marked missing by BIS." in report
    assert "The comparison uses 2025-11-14, 2 calendar days earlier." in report
    assert "The anniversary, 2025-11-18, has no daily observation in the source." in report
    assert "The latest rate was 13 calendar days old when the ZIP was retrieved." in report
    assert "different series metadata (compilation notes)" in report
    assert "same daily observation on 2026-11-17" in report
    assert "Source record 11 reports observation value 5.00 and status A (normal value)." in report
    assert "Source record 12 reports observation value NaN and status M (missing value)." in report
    # The report should account for skipped rows, collapsed repeats and blank time metadata.
    assert "excluded 1 monthly row" in report
    assert "include 2 dated missing values" in report
    assert "collapsed 1 exact duplicate row" in report
    assert "BIS left `TIME_FORMAT` blank on 13 of 13 rows" in report
    assert "using the reported frequency (`FREQ`)" in report
    assert "ZIP retrieved **2026-12-01 03:15 UTC**" in report
    assert f"Archive SHA-256 `{fetch['sha256']}`" in report
    assert "](policy_rates.png)" in report


def test_second_transform_produces_the_same_files_and_manifest(tmp_path: Path) -> None:
    """Reuse byte-identical derived outputs for an unchanged active ZIP."""
    seed_cache(tmp_path, (FIXTURES / "synthetic-cbpol-flat.zip").read_bytes())
    first = transform_archive(tmp_path)
    # Capture all three published files before rerunning the transform.
    original = [path.read_bytes() for path in (
        first.manifest_path, first.observations_path, first.metadata_path,
    )]

    second = transform_archive(tmp_path)

    assert second == first
    assert [path.read_bytes() for path in (
        second.manifest_path, second.observations_path, second.metadata_path,
    )] == original
    assert len(list((tmp_path / "derived").iterdir())) == 1


def test_transform_withholds_restricted_values_from_the_derived_csv(tmp_path: Path) -> None:
    """Strip restricted rates and pre-break values from the derived data boundary."""
    with (FIXTURES / CSV_MEMBER_NAME).open(newline="", encoding="utf-8") as source:
        header, daily, *_ = csv.reader(source)
    positions = {name.partition(":")[0]: index for index, name in enumerate(header)}
    # Mark an otherwise valid row confidential and add a pre-break value to protect.
    daily[positions["OBS_CONF"]] = "C"
    daily[positions["OBS_PRE_BREAK"]] = "4.25"
    output = io.StringIO(newline="")
    csv.writer(output).writerows([header, daily])
    seed_cache(tmp_path, csv_archive(output.getvalue()))

    artifact = transform_archive(tmp_path)

    with artifact.observations_path.open(newline="", encoding="utf-8") as observations:
        row, = csv.DictReader(observations)
    assert (row["eligible_rate"], row["raw_value"], row["obs_pre_break"]) == ("", "", "")
    assert (row["obs_conf"], row["issues"], row["source_record"]) == ("C", "restricted_value", "1")


def test_duplicate_evidence_redacts_restricted_rates(tmp_path: Path) -> None:
    """Keep conflict evidence useful without disclosing restricted numeric values."""
    header, daily, *_ = csv.reader(io.StringIO((FIXTURES / CSV_MEMBER_NAME).read_text()))
    positions = {name.partition(":")[0]: index for index, name in enumerate(header)}
    restricted = daily.copy()
    # Use distinctive confidential values that can be checked for absence in the manifest.
    private_cells = {"OBS_CONF": "C", "OBS_VALUE": "990.125", "OBS_PRE_BREAK": "991.25"}
    for field, value in private_cells.items():
        restricted[positions[field]] = value
    output = io.StringIO(newline="")
    csv.writer(output).writerows([header, daily, restricted])
    seed_cache(tmp_path, csv_archive(output.getvalue()))

    artifact = transform_archive(tmp_path)
    row, = build_summary(parse_selection("EA"), cache_dir=tmp_path).records()
    # The evidence should retain the confidentiality code while redacting both values.
    evidence = artifact.manifest["series_quality"]["XM"]["examples"][0]["evidence"]
    assert evidence[1]["values"] == {"OBS_CONF": "C", "OBS_VALUE": None, "OBS_PRE_BREAK": None}
    assert "pre-break value (restricted)" in row["status_detail"]
    assert "observation value (restricted)" in row["status_detail"]
    assert all(value not in artifact.manifest_path.read_text() for value in ("990.125", "991.25"))


def test_reader_rejects_transform_after_the_source_zip_changes(tmp_path: Path) -> None:
    """Refuse to combine a newly active archive with tables derived from its predecessor."""
    seed_cache(tmp_path, (FIXTURES / "synthetic-cbpol-flat.zip").read_bytes())
    first = transform_archive(tmp_path)
    changed_csv = (FIXTURES / CSV_MEMBER_NAME).read_text().replace(
        ",2026-11-16,4.50,", ",2026-11-16,4.75,", 1,
    )
    # Publish the changed ZIP and deliberately defer its transform.
    seed_cache(tmp_path, csv_archive(changed_csv), refresh=True)

    with pytest.raises(TransformError, match="belongs to an older archive"):
        load_derived_artifact(tmp_path)
    with pytest.raises(TransformError, match="belongs to an older archive"):
        build_summary(parse_selection("EA"), cache_dir=tmp_path)

    # A verified rebuild should restore reporting using the new rate and digest.
    second = transform_archive(tmp_path)
    assert load_derived_artifact(tmp_path) == second
    assert second.manifest["source"]["sha256"] != first.manifest["source"]["sha256"]
    assert "4.75" in second.observations_path.read_text()


def test_reader_rejects_damaged_csv_and_transform_repairs_it(tmp_path: Path) -> None:
    """Detect a damaged derived table and recover by publishing a verified rebuild."""
    seed_cache(tmp_path, (FIXTURES / "synthetic-cbpol-flat.zip").read_bytes())
    first = transform_archive(tmp_path)
    # Corrupt only the metadata CSV while keeping its manifest and source intact.
    first.metadata_path.write_text("broken\n")

    with pytest.raises(TransformError, match=r"Invalid derived file metadata\.csv"):
        load_derived_artifact(tmp_path)

    repaired = transform_archive(tmp_path)
    assert repaired.metadata_path != first.metadata_path
    assert load_derived_artifact(tmp_path) == repaired


def test_failed_manifest_switch_preserves_previous_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve the previous derived set when a new pointer cannot be activated."""
    seed_cache(tmp_path, (FIXTURES / "synthetic-cbpol-flat.zip").read_bytes())
    first = transform_archive(tmp_path)
    published = (first.manifest_path, first.observations_path, first.metadata_path)
    original = [path.read_bytes() for path in published]
    changed_csv = (FIXTURES / CSV_MEMBER_NAME).read_text().replace(
        ",2026-11-16,4.50,", ",2026-11-16,4.75,", 1,
    )
    seed_cache(tmp_path, csv_archive(changed_csv), refresh=True)

    # Fail at publication after preparing new tables for the changed ZIP.
    def fail_replace(source: Path, destination: Path) -> None:
        assert destination == first.manifest_path
        raise OSError("simulated manifest switch failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(TransformError, match="simulated manifest switch failure"):
        transform_archive(tmp_path)

    # Preserved outputs must remain byte-identical even though the active ZIP is newer.
    assert [path.read_bytes() for path in published] == original
    with pytest.raises(TransformError, match="belongs to an older archive"):
        load_derived_artifact(tmp_path)
