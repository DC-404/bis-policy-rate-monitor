"""Command line interface for the BIS Policy Rate Monitor."""

from pathlib import Path
from typing import Annotated

import httpx
import typer

from bis_policy_rate_monitor.derived import load_derived_artifact, transform_archive
from bis_policy_rate_monitor.fetch import (
    DEFAULT_CACHE_DIR,
    FetchError,
    fetch_archive,
    load_cached_archive,
)
from bis_policy_rate_monitor.metadata_cache import (
    MetadataCacheError,
    load_cached_metadata,
    refresh_metadata,
)
from bis_policy_rate_monitor.summary import (
    DEFAULT_COUNTRIES,
    DEFAULT_START,
    ReportSelection,
    build_summary,
    parse_selection,
)
from bis_policy_rate_monitor.transform import TransformError

app = typer.Typer(no_args_is_help=True, add_completion=False)
WEB_BUNDLE_DIR = Path(__file__).resolve().parents[2] / "web" / "dist"


@app.callback()
def main() -> None:
    """Retrieve and analyse BIS central bank policy rates."""


@app.command()
def fetch(
    cache_dir: Annotated[
        Path, typer.Option(help="Directory for the BIS archive.")
    ] = DEFAULT_CACHE_DIR,
    refresh: Annotated[bool, typer.Option("--refresh", help="Download a fresh archive.")] = False,
) -> None:
    """Download the BIS policy rate flat ZIP or reuse a fresh cached copy."""
    try:
        with httpx.Client(timeout=httpx.Timeout(30, connect=10)) as client:
            result = fetch_archive(client, cache_dir, refresh=refresh)
    except FetchError as exc:
        typer.echo(f"Fetch failed. {exc}", err=True)
        raise typer.Exit(code=1) from exc

    action = result.cache_action.capitalize()
    typer.echo(f"{action} {result.archive_path} ({result.byte_count:,} bytes)")
    typer.echo(f"Source {result.archive_url}")
    if result.catalogue_release_date:
        typer.echo(f"Catalogue release {result.catalogue_release_date}")
    typer.echo(f"Retrieved {result.retrieved_at.isoformat()}")
    typer.echo(f"SHA-256 {result.sha256}")


@app.command()
def metadata(
    cache_dir: Annotated[
        Path, typer.Option(help="Directory for the BIS cache.")
    ] = DEFAULT_CACHE_DIR,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Download and verify the CBPOL structure.")
    ] = False,
) -> None:
    """Inspect the verified SDMX metadata or explicitly refresh it from BIS."""
    try:
        # Structural XML refresh is independent of the policy observation ZIP.
        if refresh:
            with httpx.Client(timeout=httpx.Timeout(15, connect=10)) as client:
                result = refresh_metadata(client, cache_dir)
        else:
            result = load_cached_metadata(cache_dir)
    except MetadataCacheError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc

    action = "Refreshed" if refresh else "Cached"
    typer.echo(f"{action} {result.xml_path} ({result.byte_count:,} bytes)")
    typer.echo(f"Source {result.source_url}")
    typer.echo(f"Retrieved {result.retrieved_at.isoformat()}")
    typer.echo(f"SHA-256 {result.sha256}")
    typer.echo(result.structure.flow_urn)
    typer.echo(result.structure.dsd_urn)
    typer.echo(result.structure.frequency_codelist_urn)
    typer.echo(f"{result.structure.area_codelist_urn} ({len(result.structure.area_labels)} codes)")


@app.command()
def transform(
    cache_dir: Annotated[
        Path, typer.Option(help="Cache containing the active BIS archive.")
    ] = DEFAULT_CACHE_DIR,
) -> None:
    """Validate the cached BIS flat ZIP and write its daily observations."""
    try:
        artifact = transform_archive(cache_dir)
    except TransformError as exc:
        typer.echo(f"Transform failed. {exc}", err=True)
        raise typer.Exit(code=1) from exc

    counts = artifact.manifest["resolved"]
    typer.echo(
        f"Wrote {counts['emitted_rows']:,} daily rows and "
        f"{counts['metadata_rows']:,} metadata records"
    )
    typer.echo(f"Observations {artifact.observations_path}")
    typer.echo(f"Metadata {artifact.metadata_path}")
    typer.echo(f"Quality and provenance {artifact.manifest_path}")


@app.command()
def report(
    countries: Annotated[
        str, typer.Option(help="Comma-separated BIS area codes.")
    ] = DEFAULT_COUNTRIES,
    start: Annotated[
        str, typer.Option(help="First chart date, in YYYY-MM-DD format.")
    ] = DEFAULT_START,
    cache_dir: Annotated[
        Path, typer.Option(help="Directory for the verified BIS cache.")
    ] = DEFAULT_CACHE_DIR,
    out_dir: Annotated[
        Path, typer.Option(help="Directory for the four report files.")
    ] = Path("out"),
) -> None:
    """Generate CSV, JSON, a chart and a Markdown report from the verified cache."""
    _publish_report(_report_selection(countries, start), cache_dir, out_dir)


def _report_selection(countries: str, start: str) -> ReportSelection:
    """Validate report options and report invalid input to the CLI user."""
    try:
        return parse_selection(countries, start)
    except ValueError as exc:
        typer.echo(f"Report failed. {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _publish_report(selection: ReportSelection, cache_dir: Path, out_dir: Path) -> None:
    """Build and publish the selected report, then print its paths and source."""
    # Load Matplotlib only when a report is requested.
    from bis_policy_rate_monitor.output import ReportError, write_outputs

    try:
        summary = build_summary(selection, cache_dir=cache_dir)
        outputs = write_outputs(summary, out_dir)
    except (ValueError, ReportError) as exc:
        typer.echo(f"Report failed. {exc}", err=True)
        raise typer.Exit(code=1) from exc

    for path in outputs:
        typer.echo(f"Wrote {path}")
    typer.echo(f"Source {summary.source['archive_url']}")
    typer.echo(f"Retrieved {summary.source['retrieved_at']}")
    typer.echo(f"SHA-256 {summary.source['sha256']}")


@app.command()
def run(
    countries: Annotated[
        str, typer.Option(help="Comma-separated BIS area codes.")
    ] = DEFAULT_COUNTRIES,
    start: Annotated[
        str, typer.Option(help="First chart date, in YYYY-MM-DD format.")
    ] = DEFAULT_START,
    cache_dir: Annotated[
        Path, typer.Option(help="Directory for the BIS cache.")
    ] = DEFAULT_CACHE_DIR,
    out_dir: Annotated[
        Path, typer.Option(help="Directory for the four report files.")
    ] = Path("out"),
    refresh: Annotated[bool, typer.Option("--refresh", help="Download a fresh archive.")] = False,
) -> None:
    """Fetch, transform and generate the report in one command."""
    selection = _report_selection(countries, start)
    # Each stage exits on failure before the next stage can publish anything.
    fetch(cache_dir=cache_dir, refresh=refresh)
    typer.echo("Transforming daily observations from the verified ZIP.")
    transform(cache_dir=cache_dir)
    _publish_report(selection, cache_dir, out_dir)


@app.command()
def web(
    cache_dir: Annotated[
        Path, typer.Option(envvar="BIS_PRATES_CACHE_DIR", help="Directory for the BIS cache.")
    ] = DEFAULT_CACHE_DIR,
    port: Annotated[int, typer.Option(min=1, max=65535, help="Local TCP port.")] = 8000,
) -> None:
    """Open the built web monitor and API from one local process."""
    if not (WEB_BUNDLE_DIR / "index.html").is_file() or not (WEB_BUNDLE_DIR / "assets").is_dir():
        typer.echo(
            "The built web bundle is missing. From the repository root, "
            "run npm --prefix web ci and npm --prefix web run build.", err=True,
        )
        raise typer.Exit(code=1)

    # Keep the core CLI usable with a minimal installation.
    try:
        import uvicorn

        from bis_policy_rate_monitor.api import create_app
    except ModuleNotFoundError as exc:
        if exc.name not in {"fastapi", "uvicorn"}:
            raise
        typer.echo(
            'The web dependencies are missing. Run python -m pip install -e ".[web]".', err=True,
        )
        raise typer.Exit(code=1) from exc

    try:
        # A verified local capture can serve the monitor at any age until an explicit refresh.
        load_derived_artifact(cache_dir)
    except TransformError:
        try:
            load_cached_archive(cache_dir)
        except FetchError:
            fetch(cache_dir=cache_dir)
        typer.echo("Preparing daily observations for the web monitor.")
        transform(cache_dir=cache_dir)
    else:
        typer.echo(f"Using the verified BIS capture in {cache_dir}.")

    application = create_app(cache_dir, bundle_dir=WEB_BUNDLE_DIR)
    typer.echo(f"Open the web monitor at http://127.0.0.1:{port}/. Press Ctrl+C to stop.")
    # Bind to loopback and keep refresh serialization inside one worker.
    uvicorn.run(application, host="127.0.0.1", port=port, workers=1)
