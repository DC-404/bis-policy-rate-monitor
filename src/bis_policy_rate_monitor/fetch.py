"""Discover, download and validate the BIS policy rate archive."""

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Literal
from urllib.parse import urljoin, urlsplit
from zipfile import BadZipFile, ZipFile

import httpx

BIS_CATALOGUE_URL = "https://data.bis.org/bulkdownload"
FLAT_DOWNLOAD_TITLE = "Central bank policy rates (CSV, flat)"
MAX_CATALOGUE_BYTES = 4 * 1024 * 1024
DEFAULT_CACHE_DIR = Path("cache")
ARCHIVE_NAME = "WS_CBPOL_csv_flat.zip"
MANIFEST_NAME = "fetch-manifest.json"
CACHE_MAX_AGE = timedelta(hours=24)
MANIFEST_VERSION = 1
CSV_MEMBER_NAME = "WS_CBPOL_csv_flat.csv"
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_BYTES = 640 * 1024 * 1024
ARCHIVE_TRANSFER_SECONDS = 60


class FetchError(Exception):
    """A fetch failure the CLI can show directly to the user."""


@dataclass(frozen=True)
class FetchResult:
    """Provenance for an archive placed in the local cache."""

    catalogue_url: str
    archive_url: str
    archive_path: Path
    retrieved_at: datetime
    byte_count: int
    sha256: str
    cache_action: Literal["downloaded", "reused"]
    catalogue_release_date: str | None = None
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class _CatalogueArchive:
    url: str
    release_date: str | None


class _CatalogueLinks(HTMLParser):
    def __init__(self) -> None:
        """Initialize the per-anchor state and collect matching downloads."""
        super().__init__()
        self.matches: list[_CatalogueArchive] = []
        self._href = ""
        self._text: list[str] | None = None
        self._inside_time = False
        self._time_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Begin collecting an anchor or its nested release date."""
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []
            self._inside_time = False
            self._time_text = []
        elif tag == "time" and self._text is not None:
            self._inside_time = True

    def handle_data(self, data: str) -> None:
        """Collect title and release-date text in separate buffers."""
        if self._text is not None:
            if self._inside_time:
                self._time_text.append(data)
            else:
                self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Keep matching anchors with their displayed release date."""
        if tag == "time":
            self._inside_time = False
        elif tag == "a" and self._text is not None:
            title = " ".join("".join(self._text).split())
            if title.casefold() == FLAT_DOWNLOAD_TITLE.casefold():
                release_date = " ".join("".join(self._time_text).split()) or None
                self.matches.append(_CatalogueArchive(self._href, release_date))
            self._text = None


def select_flat_zip_url(html: str, catalogue_url: str = BIS_CATALOGUE_URL) -> str:
    """Resolve the uniquely named flat download from catalogue HTML."""
    return _select_flat_archive(html, catalogue_url).url


def _is_catalogue_zip_url(url: str, catalogue_url: str) -> bool:
    """Check for an HTTPS ZIP on the catalogue's network location."""
    target = urlsplit(url)
    return (
        target.scheme == "https"
        and target.netloc.casefold() == urlsplit(catalogue_url).netloc.casefold()
        and target.path.lower().endswith(".zip")
    )


def _select_flat_archive(html: str, catalogue_url: str) -> _CatalogueArchive:
    """Resolve the single distinct flat ZIP target and its release date."""
    parser = _CatalogueLinks()
    parser.feed(html)
    parser.close()
    if not parser.matches:
        raise FetchError(f"The BIS catalogue has no '{FLAT_DOWNLOAD_TITLE}' link.")

    # Resolve the published links first so repeated markup for one archive can be collapsed.
    targets: dict[str, set[str]] = {}
    for entry in parser.matches:
        try:
            url = urljoin(catalogue_url, entry.url.strip())
            valid = _is_catalogue_zip_url(url, catalogue_url)
        except ValueError as exc:
            raise FetchError("The BIS flat download link contains a malformed URL.") from exc
        if not valid:
            raise FetchError("The BIS flat download link must point to an HTTPS ZIP on BIS.")
        dates = targets.setdefault(url, set())
        if entry.release_date:
            dates.add(entry.release_date)

    if len(targets) != 1:
        raise FetchError(f"The BIS catalogue links to multiple '{FLAT_DOWNLOAD_TITLE}' ZIPs.")
    (url, dates), = targets.items()
    # Repeated links can refer to one ZIP. Keep a release date only when they agree on it.
    return _CatalogueArchive(url, next(iter(dates)) if len(dates) == 1 else None)


def discover_archive(client: httpx.Client, catalogue_url: str = BIS_CATALOGUE_URL) -> str:
    """Read the catalogue with the caller's HTTP client and return the ZIP URL."""
    return _discover_archive(client, catalogue_url).url


def _discover_archive(client: httpx.Client, catalogue_url: str) -> _CatalogueArchive:
    """Read bounded catalogue HTML and resolve the selected archive."""
    body = bytearray()
    try:
        with client.stream("GET", catalogue_url, headers={"Accept": "text/html"}) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                body.extend(chunk)
                if len(body) > MAX_CATALOGUE_BYTES:
                    raise FetchError("The BIS catalogue exceeds the 4 MiB size limit.")
            html = body.decode(response.encoding or "utf-8", errors="replace")
    except httpx.HTTPStatusError as exc:
        raise FetchError(f"The BIS catalogue returned HTTP {exc.response.status_code}.") from exc
    except httpx.RequestError as exc:
        raise FetchError(f"Could not read the BIS catalogue: {exc}") from exc

    return _select_flat_archive(html, catalogue_url)


def _declared_archive_size(response: httpx.Response) -> int | None:
    """Check an advertised download size before writing the response."""
    length = response.headers.get("Content-Length")
    if length is None:
        return None
    try:
        size = int(length)
    except ValueError as exc:
        raise FetchError("The BIS ZIP response has an invalid Content-Length.") from exc
    if size < 0:
        raise FetchError("The BIS ZIP response has an invalid Content-Length.")
    if size > MAX_ARCHIVE_BYTES:
        raise FetchError(f"The BIS ZIP exceeds the {MAX_ARCHIVE_BYTES:,}-byte download limit.")
    return size


def _download_zip(
    client: httpx.Client, archive_url: str, temporary_path: Path
) -> tuple[int, str, httpx.Headers]:
    """Stream a bounded ZIP to staging and return its byte count, digest, and headers."""
    digest = hashlib.sha256()
    byte_count = 0
    deadline = monotonic() + ARCHIVE_TRANSFER_SECONDS
    # The byte count and digest describe the ZIP itself, so request identity encoding.
    headers = {"Accept": "application/zip", "Accept-Encoding": "identity"}
    try:
        with client.stream("GET", archive_url, headers=headers, follow_redirects=False) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise FetchError("The BIS ZIP request requires a complete HTTP 200 response.")
            if response.headers.get("Content-Encoding", "identity").casefold() != "identity":
                raise FetchError("The BIS ZIP response uses an unsupported HTTP content encoding.")
            expected_size = _declared_archive_size(response)
            with temporary_path.open("wb") as output:
                # Check the actual bytes even when HTTP omits Content-Length.
                for chunk in response.iter_raw():
                    if monotonic() >= deadline:
                        raise FetchError("The BIS ZIP download exceeded its transfer time budget.")
                    byte_count += len(chunk)
                    if byte_count > MAX_ARCHIVE_BYTES:
                        raise FetchError(
                            f"The BIS ZIP exceeds the {MAX_ARCHIVE_BYTES:,}-byte download limit."
                        )
                    output.write(chunk)
                    digest.update(chunk)
    except httpx.HTTPStatusError as exc:
        raise FetchError(f"The BIS ZIP request returned HTTP {exc.response.status_code}.") from exc
    except httpx.RequestError as exc:
        raise FetchError(f"Could not download the BIS ZIP: {exc}") from exc

    if monotonic() >= deadline:
        raise FetchError("The BIS ZIP download exceeded its transfer time budget.")
    if expected_size is not None and byte_count != expected_size:
        raise FetchError(
            f"Incomplete BIS ZIP download: expected {expected_size:,} bytes, "
            f"received {byte_count:,}."
        )
    return byte_count, digest.hexdigest(), response.headers


def _validate_zip(path: Path) -> None:
    """Check the structure and member header before committing the archive."""
    try:
        with ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) != 1:
                raise FetchError(f"Expected exactly one BIS ZIP member named {CSV_MEMBER_NAME}.")
            member = members[0]
            unix_type = stat.S_IFMT(member.external_attr >> 16) if member.create_system == 3 else 0
            if (
                member.filename != CSV_MEMBER_NAME
                or member.is_dir()
                or member.external_attr & 0x10
                or unix_type not in (0, stat.S_IFREG)
            ):
                raise FetchError(f"Expected a regular BIS ZIP member named {CSV_MEMBER_NAME}.")
            if not 0 < member.file_size <= MAX_EXPANDED_BYTES:
                raise FetchError(
                    "The BIS CSV member must declare an expanded size between 1 and "
                    f"{MAX_EXPANDED_BYTES:,} bytes."
                )
            if member.flag_bits & 1:
                raise FetchError("The BIS CSV member is encrypted.")
            with archive.open(member):
                # Opening checks the local member header. Transform will read the payload to EOF.
                pass
    except (BadZipFile, NotImplementedError) as exc:
        raise FetchError(f"The download is not a readable BIS ZIP archive: {exc}") from exc


def _archive_location(sha256: str) -> Path:
    """Address an archive within the cache by the digest of its ZIP bytes."""
    return Path("archives") / sha256 / ARCHIVE_NAME


def _read_cached_archive(cache_dir: Path, *, require_fresh: bool) -> FetchResult | None:
    """Verify the manifest and ZIP, applying the reuse window when requested."""
    try:
        manifest = json.loads((cache_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        if (
            manifest["schema_version"] != MANIFEST_VERSION
            or manifest["catalogue_url"] != BIS_CATALOGUE_URL
        ):
            return None
        retrieved_at = datetime.fromisoformat(manifest["retrieved_at"])
        if retrieved_at.utcoffset() is None:
            return None
        # Fetch freshness is separate from the integrity checks needed for saved-capture replay.
        if require_fresh and not timedelta(0) <= datetime.now(UTC) - retrieved_at < CACHE_MAX_AGE:
            return None

        sha256 = manifest["sha256"]
        byte_count = manifest["byte_count"]
        archive_url = manifest["archive_url"]
        if (
            not re.fullmatch(r"[0-9a-f]{64}", sha256)
            or type(byte_count) is not int
            or not 0 < byte_count <= MAX_ARCHIVE_BYTES
            or not isinstance(archive_url, str)
            or not _is_catalogue_zip_url(archive_url, BIS_CATALOGUE_URL)
        ):
            return None
        location = _archive_location(sha256)
        if manifest["archive_path"] != location.as_posix():
            return None
        archive_path = cache_dir / location
        if not archive_path.is_file() or archive_path.stat().st_size != byte_count:
            return None
        # Rehash the cached file because a modified archive can retain the expected size.
        with archive_path.open("rb") as cached:
            if hashlib.file_digest(cached, "sha256").hexdigest() != sha256:
                return None
        _validate_zip(archive_path)

        release_date, etag, last_modified = (
            manifest.get(field) for field in ("catalogue_release_date", "etag", "last_modified")
        )
        if any(
            value is not None and not isinstance(value, str)
            for value in (release_date, etag, last_modified)
        ):
            return None
    except (OSError, KeyError, TypeError, ValueError, FetchError):
        return None

    return FetchResult(
        catalogue_url=BIS_CATALOGUE_URL,
        archive_url=archive_url,
        archive_path=archive_path,
        retrieved_at=retrieved_at.astimezone(UTC),
        byte_count=byte_count,
        sha256=sha256,
        cache_action="reused",
        catalogue_release_date=release_date,
        etag=etag,
        last_modified=last_modified,
    )


def load_cached_archive(cache_dir: Path = DEFAULT_CACHE_DIR) -> FetchResult:
    """Verify the active local capture independently of the fetch reuse window."""
    result = _read_cached_archive(cache_dir, require_fresh=False)
    if result is None:
        raise FetchError(f"No verified BIS archive in {cache_dir}. Run bis-prates fetch first.")
    return result


def _write_manifest(path: Path, result: FetchResult) -> None:
    """Write the archive's provenance and relative cache path to a staged manifest."""
    manifest = {
        "schema_version": MANIFEST_VERSION,
        "catalogue_url": result.catalogue_url,
        "catalogue_release_date": result.catalogue_release_date,
        "archive_url": result.archive_url,
        "archive_path": _archive_location(result.sha256).as_posix(),
        "retrieved_at": result.retrieved_at.isoformat(),
        "byte_count": result.byte_count,
        "sha256": result.sha256,
        "etag": result.etag,
        "last_modified": result.last_modified,
    }
    path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def fetch_archive(
    client: httpx.Client, cache_dir: Path = DEFAULT_CACHE_DIR, *, refresh: bool = False
) -> FetchResult:
    """Return a fresh cached archive or download a replacement."""
    if not refresh and (cached := _read_cached_archive(cache_dir, require_fresh=True)) is not None:
        return cached

    entry = _discover_archive(client, BIS_CATALOGUE_URL)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=cache_dir, prefix=".fetch-") as directory:
            staging = Path(directory)
            temporary_path = staging / ARCHIVE_NAME
            byte_count, sha256, headers = _download_zip(client, entry.url, temporary_path)
            retrieved_at = datetime.now(UTC)
            _validate_zip(temporary_path)
            archive_path = cache_dir / _archive_location(sha256)
            result = FetchResult(
                catalogue_url=BIS_CATALOGUE_URL,
                archive_url=entry.url,
                archive_path=archive_path,
                retrieved_at=retrieved_at,
                byte_count=byte_count,
                sha256=sha256,
                cache_action="downloaded",
                catalogue_release_date=entry.release_date,
                etag=headers.get("ETag"),
                last_modified=headers.get("Last-Modified"),
            )
            staged_manifest = staging / MANIFEST_NAME
            _write_manifest(staged_manifest, result)
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.replace(archive_path)
            # The manifest selects the active archive. Publish that pointer last.
            staged_manifest.replace(cache_dir / MANIFEST_NAME)
    except OSError as exc:
        raise FetchError(f"Could not update the BIS cache at {cache_dir}: {exc}") from exc

    return result
