"""Exercise CLI stages, browser API parity and recoverable report publication."""

import asyncio
import csv
import io
import json
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest
from PIL import Image
from typer.testing import CliRunner

from bis_policy_rate_monitor import cli, output
from bis_policy_rate_monitor.cli import app
from bis_policy_rate_monitor.fetch import BIS_CATALOGUE_URL, CSV_MEMBER_NAME, FetchError
from bis_policy_rate_monitor.metadata_cache import BIS_STRUCTURE_URL
from bis_policy_rate_monitor.transform import TransformError


@pytest.fixture
def runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    """Run real CLI commands in a temporary directory with an offline BIS transport."""
    fixtures = Path(__file__).parent / "fixtures"
    archive = (fixtures / "synthetic-cbpol-flat.zip").read_bytes()

    def reply(request: httpx.Request) -> httpx.Response:
        if str(request.url) == BIS_CATALOGUE_URL:
            link = '<a href="/policy-rates.zip">Central bank policy rates (CSV, flat)</a>'
            return httpx.Response(200, text=link)
        if str(request.url) == BIS_STRUCTURE_URL:
            structure = (fixtures / "synthetic-cbpol-structure.xml").read_bytes()
            return httpx.Response(200, stream=httpx.ByteStream(structure))
        assert str(request.url) == "https://data.bis.org/policy-rates.zip"
        return httpx.Response(200, stream=httpx.ByteStream(archive))

    client = httpx.Client

    def mock_client(**options) -> httpx.Client:
        return client(transport=httpx.MockTransport(reply), **options)

    monkeypatch.setattr("bis_policy_rate_monitor.cli.httpx.Client", mock_client)
    monkeypatch.chdir(tmp_path)
    return CliRunner()


def prepare_cache(runner: CliRunner) -> None:
    """Create a verified fixture capture and transform through the CLI."""
    for command in ("fetch", "transform"):
        result = runner.invoke(app, [command])
        assert result.exit_code == 0, result.output


def request_app(application, path: str, *, method: str = "GET", **params: str) -> httpx.Response:
    """Send a request through ASGI without binding a port or using external HTTP."""
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {} if method == "POST" else None
            return await client.request(method, path, params=params, json=body)

    return asyncio.run(request())


@pytest.fixture
def get_api(tmp_path: Path):
    """Expose the local API with the test cache and optional FastAPI dependency."""
    pytest.importorskip("fastapi")
    from bis_policy_rate_monitor.api import create_app

    application = create_app(tmp_path / "cache")

    def get(path: str, **options: str) -> httpx.Response:
        return request_app(application, path, **options)

    return get


def test_clean_cli_workflow_publishes_the_four_report_files(runner: CliRunner) -> None:
    """Follow fetch, transform and report from a clean cache to four coherent artifacts."""
    prepare_cache(runner)

    options = ["--countries", "US,EA,GB,JP,CH", "--start", "2015-01-01"]
    result = runner.invoke(app, ["report", *options])

    assert result.exit_code == 0, result.output
    directory = Path("out")
    assert {path.name for path in directory.iterdir()} == set(output.OUTPUT_NAMES)
    assert all(f"Wrote {directory / name}" in result.stdout for name in output.OUTPUT_NAMES)
    # Compare both serialized forms before inspecting Markdown and chart provenance.
    with (directory / "summary.csv").open(encoding="utf-8", newline="") as stream:
        csv_rows = list(csv.DictReader(stream))
    json_rows = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert [row["requested_area"] for row in csv_rows] == ["US", "EA", "GB", "JP", "CH"]
    assert [row["requested_area"] for row in json_rows] == ["US", "EA", "GB", "JP", "CH"]
    ea_csv, ea_json = csv_rows[1], json_rows[1]
    assert ea_csv["bis_area"] == ea_json["bis_area"] == "XM"
    assert ea_csv["latest_rate"] == ea_json["latest_rate"] == "4.50"
    assert ea_csv["change_bp"] == ea_json["change_bp"] == "50.00"
    assert ea_csv["latest_date"] == ea_json["latest_date"] == "2026-11-16"
    assert csv_rows[2]["change_bp"] == "" and json_rows[2]["change_bp"] is None
    assert csv_rows[3]["latest_rate"] == "" and json_rows[3]["latest_rate"] is None
    assert "area_code_status" not in ea_json
    report = (directory / "report.md").read_text(encoding="utf-8")
    assert "| Euro area (EA/XM) | 4.5000% (2026-11-16) | 4.0000% (2025-11-14) | +50 |" in report
    assert "](policy_rates.png)" in report
    assert ea_json["source_sha256"] in report and ea_json["source_sha256"] in result.stdout
    # Check the PNG's embedded source timestamp and digest as well as its existence.
    with Image.open(directory / "policy_rates.png") as chart:
        assert chart.format == "PNG"
        assert chart.info["ArchiveSHA256"] == ea_json["source_sha256"]
        assert chart.info["Source"] == ea_json["source_url"]
        assert chart.info["RetrievedAt"] == ea_json["source_retrieved_at"]
        assert "XM 2026-11-16" in chart.info["LatestObservationDates"]


def test_run_matches_separate_stages_and_supports_refresh(runner: CliRunner) -> None:
    """Keep the short command equivalent to the pipeline stages and honor forced refresh."""
    cache = ["--cache-dir", "review-cache"]
    invalid = runner.invoke(app, ["run", *cache, "--countries", "EA,XM"])
    assert invalid.exit_code == 1 and "Area requested more than once" in invalid.stderr
    assert not Path("review-cache").exists() and not Path("out").exists()

    selection = ["--countries", "EA,US", "--start", "2026-11-16"]
    single = ["--out-dir", "single-run"]
    result = runner.invoke(app, ["run", *cache, *selection, *single])
    assert result.exit_code == 0 and "Downloaded" in result.stdout, result.output
    assert {path.name for path in Path("single-run").iterdir()} == set(output.OUTPUT_NAMES)

    for command in [
        ["fetch", *cache],
        ["transform", *cache],
        ["report", *cache, *selection, "--out-dir", "separate-stages"],
    ]:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.output
        if command[0] == "fetch":
            assert "Reused" in result.stdout
    # Compare artifact bytes when both workflows use the same verified source capture.
    for name in output.OUTPUT_NAMES:
        reference = Path("separate-stages") / name
        assert (Path("single-run") / name).read_bytes() == reference.read_bytes(), name

    # Force the next invocation to run fetch again without touching the default report.
    refreshed = runner.invoke(app, ["run", *cache, *selection, *single, "--refresh"])
    assert refreshed.exit_code == 0 and "Downloaded" in refreshed.stdout, refreshed.output
    assert not Path("out").exists()


@pytest.mark.parametrize(
    ("stage", "error_type", "message"),
    [
        ("fetch_archive", FetchError, "Fetch failed. Simulated failure."),
        ("transform_archive", TransformError, "Transform failed. Simulated failure."),
    ],
)
def test_run_stops_before_publication_on_stage_failure(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
    stage: str, error_type: type[Exception], message: str,
) -> None:
    """Keep previously published reports intact if fetch or transform fails."""
    previous = Path("out/summary.json")
    previous.parent.mkdir()
    previous.write_text("previous report", encoding="utf-8")

    # Raise at the selected stage while leaving the rest of the CLI path intact.
    def fail(*args, **kwargs) -> None:
        raise error_type("Simulated failure.")

    monkeypatch.setattr(cli, stage, fail)
    result = runner.invoke(app, ["run"])

    assert result.exit_code == 1 and message in result.stderr
    assert previous.read_text(encoding="utf-8") == "previous report"
    assert list(previous.parent.iterdir()) == [previous]
    # Fetch can publish a verified ZIP before transform fails, but report files cannot change.
    assert Path("cache/fetch-manifest.json").exists() == (stage == "transform_archive")


def test_web_missing_bundle_explains_build_before_preparing_cache(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explain how to build missing web assets before doing any cache work."""
    # Use a non-existent bundle path so this test never depends on a local Vite build.
    monkeypatch.setattr(cli, "WEB_BUNDLE_DIR", tmp_path / "missing-dist")

    result = runner.invoke(app, ["web"])

    assert result.exit_code == 1 and "npm --prefix web run build" in result.stderr
    assert not Path("cache").exists()


def test_web_uses_one_cache_for_startup_and_api(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use one cache for startup and API reads, and avoid writes on warm startup."""
    pytest.importorskip("fastapi")
    uvicorn = pytest.importorskip("uvicorn")
    bundle = tmp_path / "dist"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "index.html").write_text('<script src="/assets/main.js"></script>')
    (bundle / "assets/main.js").write_text("console.log('test bundle');")
    monkeypatch.setattr(cli, "WEB_BUNDLE_DIR", bundle)
    monkeypatch.setenv("BIS_PRATES_CACHE_DIR", "environment-cache")
    launches = []

    # Capture the ASGI app and server options without opening a socket.
    def start(application, **options) -> None:
        launches.append((application, options))

    monkeypatch.setattr(uvicorn, "run", start)
    arguments = ["web", "--cache-dir", "web-cache", "--port", "8081"]
    cold = runner.invoke(app, arguments)
    assert cold.exit_code == 0, cold.output
    assert "Downloaded" in cold.stdout and "http://127.0.0.1:8081/" in cold.stdout
    application, options = launches[0]
    assert options == {"host": "127.0.0.1", "port": 8081, "workers": 1}
    assert Path("web-cache/transform-manifest.json").is_file()
    assert not Path("environment-cache").exists() and not Path("cache").exists()
    assert "/assets/main.js" in request_app(application, "/").text
    assert request_app(application, "/assets/main.js").text == "console.log('test bundle');"
    response = request_app(application, "/api/report", countries="EA")
    assert response.status_code == 200 and response.json()["summary"][0]["change_bp"] == "50.00"
    assert request_app(application, "/api/refresh").status_code == 405

    # Make fetch and transform fail on any warm call to establish that both are skipped.
    def fail(*args, **kwargs):
        pytest.fail("A verified cache must skip startup fetch and transform")

    monkeypatch.setattr(cli, "fetch_archive", fail)
    monkeypatch.setattr(cli, "transform_archive", fail)
    warm = runner.invoke(app, arguments)
    assert warm.exit_code == 0 and "Using the verified BIS capture" in warm.stdout, warm.output
    assert len(launches) == 2


def test_report_explains_area_requests_using_both_verified_sources(runner: CliRunner) -> None:
    """Distinguish missing coverage from invalid codes with optional SDMX context."""
    pytest.importorskip("pysdmx")
    pytest.importorskip("lxml")
    pytest.importorskip("sdmxschemas")
    pytest.importorskip("xmltodict")
    prepare_cache(runner)
    refreshed = runner.invoke(app, ["metadata", "--refresh"])
    assert refreshed.exit_code == 0, refreshed.output

    options = ["--countries", "EA,AF,ZZ,UX,CH,GB,US", "--start", "2026-11-19"]
    result = runner.invoke(app, ["report", *options])

    assert result.exit_code == 0, result.output
    records = json.loads(Path("out/summary.json").read_text(encoding="utf-8"))
    ea, af, zz, ux, ch, gb, us = records
    assert (ea["requested_area"], ea["bis_area"], ea["area_code_status"]) == ("EA", "XM", "defined")
    assert ea["latest_rate"] == "4.50" and ea["change_bp"] == "50.00"
    assert ea["has_daily_series"] is True
    assert af["status"] == "unavailable" and af["area_code_status"] == "defined_no_daily"
    assert af["has_daily_series"] is False and af["area_label"] == "Afghanistan"
    assert "defined in the cached BIS codelist" in af["status_detail"]
    assert zz["area_code_status"] == "unknown" and zz["area_suggestion"] is None
    assert ux["area_code_status"] == "unknown" and ux["area_suggestion"] == "US"
    assert "Try US" in ux["status_detail"]
    assert ch["area_code_status"] == "codelist_mismatch" and ch["latest_rate"] == "-0.25"
    assert ch["status"] == "insufficient_history" and ch["has_daily_series"] is True
    assert gb["status"] == "series_blocked" and gb["has_daily_series"] is True
    assert us["area_label"] == "United States"
    assert ea["sdmx_area_codelist"] == "Codelist=TEST:CL_AREA_TEST(2.5)"
    assert ea["sdmx_source_sha256"] in refreshed.stdout
    assert ea["sdmx_source_sha256"] != ea["source_sha256"]
    report = Path("out/report.md").read_text(encoding="utf-8")
    assert "The daily capture contains CH" in report
    assert "AF is defined in the cached BIS codelist" in report and "Try US" in report
    assert ea["sdmx_source_sha256"] in report and ea["source_sha256"] in report
    # Check that the CSV carries the same diagnostics and metadata provenance.
    with Path("out/summary.csv").open(encoding="utf-8", newline="") as stream:
        csv_records = list(csv.DictReader(stream))
    assert csv_records[1]["area_code_status"] == af["area_code_status"]
    assert csv_records[3]["area_suggestion"] == ux["area_suggestion"]

    manifest = Path("cache/metadata/metadata-manifest.json")
    # Break only the metadata pointer to exercise the flat-only fallback.
    manifest.write_text("{}", encoding="utf-8")
    fallback = runner.invoke(app, ["report", *options, "--out-dir", "flat-only"])
    assert fallback.exit_code == 0, fallback.output
    fallback_records = json.loads(Path("flat-only/summary.json").read_text(encoding="utf-8"))
    assert "sdmx_source_sha256" not in fallback_records[0]
    assert fallback_records[1]["status_detail"] == (
        "No daily observations for this area in this BIS capture."
    )
    assert fallback_records[0]["change_bp"] == ea["change_bp"]


@pytest.mark.parametrize("with_sdmx", [False, True])
def test_api_report_agrees_with_cli_and_marks_chart_gaps(
    runner: CliRunner, get_api, with_sdmx: bool,
) -> None:
    """Keep API calculations aligned with the CLI while supplying browser chart gaps."""
    if with_sdmx:
        pytest.importorskip("pysdmx")
        pytest.importorskip("lxml")
        pytest.importorskip("sdmxschemas")
        pytest.importorskip("xmltodict")
    # Both interfaces will read the same captured archive and derived tables.
    prepare_cache(runner)
    if with_sdmx:
        refresh = runner.invoke(app, ["metadata", "--refresh"])
        assert refresh.exit_code == 0, refresh.output

    countries, start = "EA,US,GB,AF", "2025-11-15"
    cli = runner.invoke(app, ["report", "--countries", countries, "--start", start])
    assert cli.exit_code == 0, cli.output
    response = get_api("/api/report", countries=countries, start=start)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["summary"] == json.loads(Path("out/summary.json").read_text())
    ea, _, gb, af = report["summary"]
    assert ea["change_bp"] == "50.00" and ea["historical_date"] == "2025-11-14"
    assert report["source"]["sha256"] == ea["source_sha256"]
    assert "archive_path" not in report["source"]
    assert "TIME_FORMAT" in report["quality_notes"][0]
    assert gb["status"] == "series_blocked" and gb["latest_rate"] is None
    assert af["status"] == "unavailable"
    if with_sdmx:
        assert af["area_code_status"] == "defined_no_daily"
        assert ea["sdmx_source_sha256"] != ea["source_sha256"]
    else:
        assert "area_code_status" not in af

    series = {item["bis_area"]: item["points"] for item in report["series"]}
    assert series["XM"] == [
        {"date": "2025-11-16", "rate": None, "gap": "explicit_missing"},
        {"date": "2025-11-17", "rate": None, "gap": "absent_date"},
        {"date": "2026-11-16", "rate": "4.50", "gap": None},
        {"date": "2026-11-17", "rate": None, "gap": "explicit_missing"},
    ]
    assert series["GB"] == series["AF"] == []

    response = get_api("/api/areas")
    assert response.status_code == 200, response.text
    areas = {item["bis_area"]: item for item in response.json()["areas"]}
    assert set(areas) == {"CH", "GB", "US", "XM"}
    assert areas["XM"] == {
        "requested_area": "EA", "bis_area": "XM", "area_label": "Euro area", "blocked": False,
    }
    # Withheld areas remain selectable so the UI can display the reason.
    assert areas["GB"]["blocked"] is True
    assert areas["US"]["area_label"] == ("United States" if with_sdmx else "US")


def test_api_explains_invalid_selection_and_unusable_cache(runner: CliRunner, get_api) -> None:
    """Distinguish bad user input from report data that needs to be prepared or repaired."""
    # Duplicate aliases should fail during validation even with an empty cache.
    response = get_api("/api/report", countries="EA,XM")
    assert response.status_code == 400 and "map to XM" in response.json()["detail"]
    response = get_api("/api/report", start="2025-02-29")
    assert response.status_code == 400 and "valid start date" in response.json()["detail"]

    for path in ("/api/areas", "/api/report"):
        response = get_api(path)
        assert response.status_code == 503 and "bis-prates fetch" in response.json()["detail"]

    prepare_cache(runner)
    manifest = Path("cache/transform-manifest.json")
    data = json.loads(manifest.read_text())
    # Simulate a transform that no longer matches the active archive digest.
    data["source"]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(data))
    for path in ("/api/areas", "/api/report"):
        response = get_api(path)
        assert response.status_code == 503 and "older archive" in response.json()["detail"]


def test_api_refresh_publishes_verified_data_and_recovers_after_failures(
    get_api, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise refresh serialization, two failure modes and recovery to a new report."""
    from bis_policy_rate_monitor import api

    csv_text = (Path(__file__).parent / "fixtures" / CSV_MEMBER_NAME).read_text(encoding="utf-8")
    active_csv: str | None = csv_text

    def reply(request: httpx.Request) -> httpx.Response:
        if active_csv is None:
            return httpx.Response(503)
        if str(request.url) == BIS_CATALOGUE_URL:
            return httpx.Response(200, text=(
                '<a href="/policy-rates.zip">Central bank policy rates (CSV, flat)</a>'
            ))
        assert str(request.url) == "https://data.bis.org/policy-rates.zip"
        output = io.BytesIO()
        with ZipFile(output, "w") as archive:
            archive.writestr(CSV_MEMBER_NAME, active_csv)
        return httpx.Response(200, stream=httpx.ByteStream(output.getvalue()))

    client = httpx.Client
    monkeypatch.setattr(api.httpx, "Client", lambda **kwargs: client(
        transport=httpx.MockTransport(reply), **kwargs,
    ))

    # Hold the lock to reproduce an in-progress refresh without racing threads.
    with api._refresh_lock:
        assert get_api("/api/refresh", method="POST").status_code == 409

    first = get_api("/api/refresh", method="POST")
    assert first.status_code == 200, first.text
    source = first.json()["source"]
    original = get_api("/api/report", countries="EA").json()
    assert original["source"] == source and original["summary"][0]["latest_rate"] == "4.50"

    active_csv = None
    response = get_api("/api/refresh", method="POST")
    assert response.status_code == 502 and "BIS download failed" in response.json()["detail"]
    assert get_api("/api/report", countries="EA").json() == original

    active_csv = "invalid header\n"
    response = get_api("/api/refresh", method="POST")
    assert response.status_code == 503 and "transform failed" in response.json()["detail"]
    stale = get_api("/api/report", countries="EA")
    assert stale.status_code == 503 and "older archive" in stale.json()["detail"]

    active_csv = csv_text.replace(",2026-11-16,4.50,", ",2026-11-16,4.75,", 1)
    response = get_api("/api/refresh", method="POST")
    assert response.status_code == 200, response.text
    updated = get_api("/api/report", countries="EA").json()
    # Check that the new summary reports the same provenance as the refresh response.
    assert updated["source"] == response.json()["source"]
    assert updated["source"]["sha256"] != source["sha256"]
    assert updated["summary"][0]["latest_rate"] == "4.75"


def test_report_guides_the_user_from_empty_cache_to_transform(runner: CliRunner) -> None:
    """Give the next command needed at each missing-cache stage."""
    # Start without an archive before checking the separate missing-transform state.
    missing_zip = runner.invoke(app, ["report"])
    assert missing_zip.exit_code == 1 and "Run bis-prates fetch first" in missing_zip.stderr
    assert runner.invoke(app, ["fetch"]).exit_code == 0

    missing_transform = runner.invoke(app, ["report"])

    assert missing_transform.exit_code == 1
    assert "Run bis-prates transform" in missing_transform.stderr
    assert not Path("out").exists()


def test_report_preserves_an_existing_staging_folder(runner: CliRunner) -> None:
    """Avoid interfering with an existing report writer or interrupted staging directory."""
    prepare_cache(runner)
    staging = Path("out/.report-staging")
    staging.mkdir(parents=True)
    pending = staging / "summary.csv"
    # A sentinel file represents another writer's in-progress output.
    pending.write_text("in progress", encoding="utf-8")

    result = runner.invoke(app, ["report"])

    assert result.exit_code == 1 and "Report staging already exists" in result.stderr
    assert list(staging.parent.iterdir()) == [staging]
    assert pending.read_text(encoding="utf-8") == "in progress"


def test_render_failure_leaves_previous_report_intact(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the previous report set when chart rendering fails before publication."""
    prepare_cache(runner)
    directory = Path("report-review")
    first = runner.invoke(app, ["report", "--countries", "EA", "--out-dir", str(directory)])
    assert first.exit_code == 0, first.output
    previous = {path.name: path.read_bytes() for path in directory.iterdir()}

    # Fail after summary rendering has begun to exercise cleanup of partial staging.
    def fail_chart(summary, path: Path) -> Path:
        assert (path.parent / "summary.csv").is_file()
        raise OSError("simulated chart write failure")

    monkeypatch.setattr(output, "write_chart", fail_chart)
    result = runner.invoke(app, ["report", "--countries", "US", "--out-dir", str(directory)])

    assert result.exit_code == 1 and "simulated chart write failure" in result.stderr
    assert "Wrote" not in result.stdout
    # Compare each published file and check that no staging artifact remains.
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == previous


@pytest.mark.parametrize("has_previous_report", [False, True])
def test_publish_failure_rolls_back_outputs(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, has_previous_report: bool,
) -> None:
    """Recover already replaced outputs on a first publication or on replacement."""
    prepare_cache(runner)
    directory = Path("out")
    directory.mkdir()
    if has_previous_report:
        previous_run = runner.invoke(app, ["report", "--countries", "EA"])
        assert previous_run.exit_code == 0, previous_run.output
    (directory / "notes.txt").write_text("keep this", encoding="utf-8")
    previous = {path.name: path.read_bytes() for path in directory.iterdir()}
    replace = Path.replace

    # Fail after the first replacement so the rollback must restore or remove it.
    def fail_summary_json(source: Path, target: Path) -> Path:
        if source == directory / ".report-staging" / "summary.json":
            raise OSError("simulated summary replacement failure")
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_summary_json)
    result = runner.invoke(app, ["report", "--countries", "US"])

    assert result.exit_code == 1 and "simulated summary replacement failure" in result.stderr
    assert "Wrote" not in result.stdout
    # Include the unrelated notes file in the exact folder comparison.
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == previous
