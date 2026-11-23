"""Local HTTP interface for the BIS Policy Rate Monitor."""

from os import environ
from pathlib import Path
from threading import Lock
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from bis_policy_rate_monitor.derived import transform_archive
from bis_policy_rate_monitor.fetch import DEFAULT_CACHE_DIR, FetchError, fetch_archive
from bis_policy_rate_monitor.snapshot import unavailable_rate_reason
from bis_policy_rate_monitor.summary import (
    DEFAULT_COUNTRIES,
    DEFAULT_START,
    WITHHELD_STATUSES,
    AreaSummary,
    SummaryValue,
    build_summary,
    chart_observations,
    list_daily_areas,
    parse_selection,
)
from bis_policy_rate_monitor.transform import TransformError

router = APIRouter()
SOURCE_FIELDS = (
    "flow_id", "frequency", "archive_url", "sha256", "retrieved_at", "catalogue_release_date",
)
_refresh_lock = Lock()


@router.get("/api/health")
def health() -> dict[str, str]:
    """Confirm that the API process can answer a request."""
    return {"status": "ok"}


@router.get("/api/areas")
def areas(request: Request) -> dict[str, list[dict[str, SummaryValue]]]:
    """Return daily areas backed by the verified cache."""
    try:
        return {"areas": list_daily_areas(cache_dir=request.app.state.cache_dir)}
    except TransformError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _chart_series(area: AreaSummary) -> dict[str, Any]:
    """Encode dated rates and gaps, leaving withheld series empty."""
    points = []
    # Return an empty chart series for conflicts or incompatible units. The snapshot still explains
    # why.
    if area.snapshot.status not in WITHHELD_STATUSES:
        for period, observation in chart_observations(area.history):
            rate = observation.eligible_rate if observation is not None else None
            points.append({
                "date": period.isoformat(),
                "rate": str(rate) if rate is not None else None,
                "gap": unavailable_rate_reason(observation) if rate is None else None,
            })
    return {"bis_area": area.request.bis_code, "points": points}


@router.get("/api/report")
def report(
    request: Request, countries: str = DEFAULT_COUNTRIES, start: str = DEFAULT_START,
) -> dict[str, Any]:
    """Return CLI-equivalent snapshots and chart histories from the verified cache."""
    try:
        selection = parse_selection(countries, start)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        result = build_summary(selection, cache_dir=request.app.state.cache_dir)
    except TransformError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # Keep requests and chart histories in the same order as the CLI report rows.
    return {
        "summary": result.records(),
        "series": [_chart_series(area) for area in result.areas],
        "source": {field: result.source[field] for field in SOURCE_FIELDS},
        "quality_notes": result.quality_notes,
    }


@router.post("/api/refresh")
def refresh(request: Request) -> dict[str, Any]:
    """Refresh the active flat capture and rebuild its verified transform."""
    # A cross-origin HTML form cannot submit application/json to the local service.
    media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(status_code=415, detail="Send the refresh request as application/json.")
    # The supported web launcher runs one worker, so this lock serializes refresh within that
    # process.
    if not _refresh_lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409, detail="A data refresh is already running. Try again shortly.",
        )
    try:
        cache_dir = request.app.state.cache_dir
        with httpx.Client(timeout=httpx.Timeout(30, connect=10)) as client:
            fetch_archive(client, cache_dir, refresh=True)
        # Fetch and transform publish separately. Readers check that their ZIP digests match.
        artifact = transform_archive(cache_dir)
    except FetchError as exc:
        raise HTTPException(status_code=502, detail=f"BIS download failed. {exc}") from exc
    except TransformError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"The BIS ZIP downloaded successfully. The transform failed. {exc} "
            "Retry the refresh or run bis-prates transform for this cache.",
        ) from exc
    finally:
        _refresh_lock.release()

    source = artifact.manifest["source"]
    return {"source": {field: source[field] for field in SOURCE_FIELDS}}


def create_app(cache_dir: Path, *, bundle_dir: Path | None = None) -> FastAPI:
    """Configure the verified-cache API and optionally serve a built React page."""
    application = FastAPI(title="BIS Policy Rate Monitor")
    application.state.cache_dir = cache_dir
    application.include_router(router)
    # The launcher serves built assets on the API origin. The Vite workflow supplies its own page.
    if bundle_dir is not None:
        index_path = bundle_dir / "index.html"

        @application.get("/", include_in_schema=False)
        def index() -> FileResponse:
            """Serve the entry point of the built frontend."""
            return FileResponse(index_path)

        application.mount("/assets", StaticFiles(directory=bundle_dir / "assets"), name="assets")
    return application


app = create_app(Path(environ.get("BIS_PRATES_CACHE_DIR", str(DEFAULT_CACHE_DIR))))
