"""Verify transfer limits, archive integrity and publication of the active capture."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import httpx
import pytest

import bis_policy_rate_monitor.fetch as fetch
from bis_policy_rate_monitor.fetch import FetchError, fetch_archive

CATALOGUE = (
    '<a href="/policy-rates.zip">Central bank policy rates (CSV, flat)'
    '<time datetime="Thu Nov 19 2026">19 Nov 2026</time></a>'
)
ARCHIVE_URL = "https://data.bis.org/policy-rates.zip"
FIXTURE = Path(__file__).parent / "fixtures" / "synthetic-cbpol-flat.zip"


def zip_response(body: bytes, headers: dict[str, str] | None = None) -> httpx.Response:
    """Return a streamed HTTP response so tests exercise raw byte accounting."""
    return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))


def archive_client(response: httpx.Response) -> httpx.Client:
    """Serve the catalogue separately from the supplied archive response."""
    def reply(request: httpx.Request) -> httpx.Response:
        if str(request.url) == fetch.BIS_CATALOGUE_URL:
            return httpx.Response(200, text=CATALOGUE)
        assert str(request.url) == ARCHIVE_URL
        return response

    return httpx.Client(transport=httpx.MockTransport(reply))


def zip_with_members(*names: str) -> bytes:
    """Build a minimal archive with the exact member names under test."""
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for name in names:
            archive.writestr(name, "example\n")
    return output.getvalue()


def fetch_bytes(cache_dir: Path, body: bytes, *, refresh: bool = False) -> fetch.FetchResult:
    """Publish synthetic ZIP bytes through the actual fetch path."""
    with archive_client(zip_response(body)) as client:
        return fetch_archive(client, cache_dir, refresh=refresh)


def test_fetch_commits_the_zip_and_returns_its_provenance(tmp_path: Path) -> None:
    """Verify the archive bytes and the provenance that identifies the active capture."""
    # Use the fixture archive so the expected digest is computed independently.
    body = FIXTURE.read_bytes()
    cache_dir = tmp_path / "cache"
    before = datetime.now(UTC)
    # The bytes and ZIP structure establish the format, even with this generic MIME type.
    headers = {
        "Content-Length": str(len(body)),
        "Content-Type": "application/octet-stream",
        "ETag": 'W/"test-v1"',
        "Last-Modified": "Thu, 19 Nov 2026 12:00:00 GMT",
    }
    with archive_client(zip_response(body, headers)) as client:
        result = fetch_archive(client, cache_dir)

    assert result.archive_path.name == "WS_CBPOL_csv_flat.zip"
    assert result.archive_path.parent.name == result.sha256
    assert result.archive_path.is_relative_to(cache_dir)
    assert result.archive_path.read_bytes() == body
    assert result.archive_url == ARCHIVE_URL
    assert result.catalogue_url == fetch.BIS_CATALOGUE_URL
    assert result.byte_count == len(body)
    assert result.sha256 == hashlib.sha256(body).hexdigest()
    assert before <= result.retrieved_at <= datetime.now(UTC)
    assert result.cache_action == "downloaded"
    manifest = json.loads((cache_dir / "fetch-manifest.json").read_text())
    assert cache_dir / manifest["archive_path"] == result.archive_path
    assert manifest["sha256"] == result.sha256
    assert manifest["byte_count"] == len(body)
    assert manifest["retrieved_at"] == result.retrieved_at.isoformat()
    # Keep the publisher's release date and HTTP validators alongside retrieval time.
    assert manifest["catalogue_release_date"] == "19 Nov 2026"
    assert manifest["etag"] == headers["ETag"]
    assert manifest["last_modified"] == headers["Last-Modified"]


@pytest.mark.parametrize(
    ("body", "headers", "reason"),
    [
        (b"<html>error page</html>", {}, "not a readable BIS ZIP"),
        (zip_with_members("wrong.csv"), {}, "regular BIS ZIP member"),
        (zip_with_members("WS_CBPOL_csv_flat.csv", "extra.csv"), {}, "exactly one BIS ZIP member"),
        (
            zip_with_members("WS_CBPOL_csv_flat.csv"),
            {"Content-Length": "4096"},
            "Incomplete BIS ZIP download",
        ),
    ],
    ids=["html", "wrong-member", "extra-member", "short-transfer"],
)
def test_failed_download_preserves_the_previous_cache(
    tmp_path: Path, body: bytes, headers: dict[str, str], reason: str,
) -> None:
    """Keep a previous verified capture available when a forced update fails."""
    # Record the original active bytes and manifest before testing the bad response.
    previous = FIXTURE.read_bytes()
    existing = fetch_bytes(tmp_path, previous)
    manifest_path = tmp_path / "fetch-manifest.json"
    original_manifest = manifest_path.read_bytes()

    with archive_client(zip_response(body, headers)) as client:
        with pytest.raises(FetchError, match=reason):
            fetch_archive(client, tmp_path, refresh=True)

    # Check that staging was cleaned and neither active file was changed.
    assert existing.archive_path.read_bytes() == previous
    assert manifest_path.read_bytes() == original_manifest
    assert list(tmp_path.glob(".fetch-*")) == []


@pytest.mark.parametrize("with_length", [True, False])
def test_download_cap_checks_declared_and_actual_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_length: bool
) -> None:
    """Enforce the compressed size limit both with and without a length header."""
    body = FIXTURE.read_bytes()
    monkeypatch.setattr(fetch, "MAX_ARCHIVE_BYTES", len(body) - 1)
    # Adding Content-Length isolates the early rejection from streamed byte counting.
    headers = {"Content-Length": str(len(body))} if with_length else {}

    with archive_client(zip_response(body, headers)) as client:
        with pytest.raises(FetchError, match="download limit"):
            fetch_archive(client, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_expanded_member_cap_prevents_cache_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a compressed archive whose member exceeds the expanded-size budget."""
    # Shrink the declared expansion cap so the small fixture exercises it.
    monkeypatch.setattr(fetch, "MAX_EXPANDED_BYTES", 1_024)

    with archive_client(zip_response(FIXTURE.read_bytes())) as client:
        with pytest.raises(FetchError, match="expanded size"):
            fetch_archive(client, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_archive_http_failure_leaves_no_local_zip(tmp_path: Path) -> None:
    """Report download HTTP failures without publishing a partial archive."""
    # Allow catalogue discovery to succeed so the 503 comes from the ZIP request.
    with archive_client(httpx.Response(503)) as client:
        with pytest.raises(FetchError, match="ZIP request returned HTTP 503"):
            fetch_archive(client, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_expired_transfer_budget_leaves_no_local_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Abandon an archive transfer that exceeds the deadline even if it responds."""
    # Advance an injected monotonic clock during the response instead of waiting.
    elapsed = 0.0
    monkeypatch.setattr(fetch, "monotonic", lambda: elapsed)
    body = FIXTURE.read_bytes()

    def reply(request: httpx.Request) -> httpx.Response:
        nonlocal elapsed
        if str(request.url) == fetch.BIS_CATALOGUE_URL:
            return httpx.Response(200, text=CATALOGUE)
        elapsed = fetch.ARCHIVE_TRANSFER_SECONDS + 1
        return zip_response(body)

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        with pytest.raises(FetchError, match="time budget"):
            fetch_archive(client, tmp_path)

    # A transfer timeout leaves no archive or manifest in the cache.
    assert list(tmp_path.iterdir()) == []


def test_second_fetch_reuses_the_archive_without_http_requests(tmp_path: Path) -> None:
    """Use a verified fresh cache without changing its recorded retrieval time."""
    first = fetch_bytes(tmp_path, FIXTURE.read_bytes())
    manifest_path = tmp_path / "fetch-manifest.json"
    original_manifest = manifest_path.read_bytes()

    # Fail the test immediately if reuse makes even a catalogue request.
    def unexpected_request(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"Fresh cache should avoid this HTTP request: {request.url}")

    with httpx.Client(transport=httpx.MockTransport(unexpected_request)) as client:
        second = fetch_archive(client, tmp_path)

    assert second.cache_action == "reused"
    assert second.archive_path == first.archive_path
    assert second.retrieved_at == first.retrieved_at
    assert second.sha256 == first.sha256
    assert manifest_path.read_bytes() == original_manifest


def test_expired_cache_downloads_a_new_archive(tmp_path: Path) -> None:
    """Replace a sound but expired capture rather than treating integrity as freshness."""
    first = fetch_bytes(tmp_path, FIXTURE.read_bytes())
    manifest_path = tmp_path / "fetch-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["retrieved_at"] = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    # Change the recorded retrieval time without corrupting the stored archive.
    manifest_path.write_text(json.dumps(manifest))

    second = fetch_bytes(tmp_path, zip_with_members("WS_CBPOL_csv_flat.csv"))

    assert second.cache_action == "downloaded"
    # The new digest distinguishes the replacement from a cache hit.
    assert second.sha256 != first.sha256
    assert json.loads(manifest_path.read_text())["sha256"] == second.sha256


def test_refresh_forces_download_and_switches_the_manifest(tmp_path: Path) -> None:
    """Honor explicit refresh even when the active capture is still fresh."""
    # Prime a current cache to distinguish explicit refresh from expiry.
    first = fetch_bytes(tmp_path, FIXTURE.read_bytes())
    body = zip_with_members("WS_CBPOL_csv_flat.csv")

    second = fetch_bytes(tmp_path, body, refresh=True)

    manifest = json.loads((tmp_path / "fetch-manifest.json").read_text())
    assert second.cache_action == "downloaded"
    assert second.archive_path != first.archive_path
    assert (tmp_path / manifest["archive_path"]).read_bytes() == body
    assert manifest["sha256"] == second.sha256


def test_changed_zip_bytes_are_repaired_even_when_size_matches(tmp_path: Path) -> None:
    """Detect same-length archive corruption through the recorded byte digest."""
    body = zip_with_members("WS_CBPOL_csv_flat.csv")
    first = fetch_bytes(tmp_path, body)
    changed = body.replace(b"example", b"remixed")
    assert changed != body and len(changed) == len(body)
    # Overwrite bytes in place so a size-only verification would pass.
    first.archive_path.write_bytes(changed)

    second = fetch_bytes(tmp_path, body)

    assert second.cache_action == "downloaded"
    assert second.archive_path == first.archive_path
    assert second.archive_path.read_bytes() == body


def test_malformed_manifest_is_rebuilt(tmp_path: Path) -> None:
    """Recover from invalid pointer JSON using a fresh verified download."""
    body = FIXTURE.read_bytes()
    first = fetch_bytes(tmp_path, body)
    manifest_path = tmp_path / "fetch-manifest.json"
    # Damage the manifest while leaving the archive bytes in place.
    manifest_path.write_text("{")

    second = fetch_bytes(tmp_path, body)

    assert second.cache_action == "downloaded"
    assert second.archive_path == first.archive_path
    assert json.loads(manifest_path.read_text())["sha256"] == second.sha256


def test_failed_manifest_publish_keeps_previous_entry_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep the previous manifest usable if activation of a downloaded archive fails."""
    first = fetch_bytes(tmp_path, FIXTURE.read_bytes())
    manifest_path = tmp_path / "fetch-manifest.json"
    original_manifest = manifest_path.read_bytes()
    original_replace = Path.replace

    # Allow the staged archive to move and inject failure only at the manifest swap.
    def replace(path: Path, target: Path) -> Path:
        if target == manifest_path:
            raise OSError("Simulated manifest write failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(FetchError, match="Simulated manifest write failure"):
        fetch_bytes(tmp_path, zip_with_members("WS_CBPOL_csv_flat.csv"), refresh=True)

    assert manifest_path.read_bytes() == original_manifest
    # A later normal fetch should reuse the preceding verified capture even if BIS is down.
    with archive_client(httpx.Response(503)) as client:
        result = fetch_archive(client, tmp_path)
    assert result.cache_action == "reused"
    assert result.archive_path == first.archive_path
    assert result.archive_path.read_bytes() == FIXTURE.read_bytes()
