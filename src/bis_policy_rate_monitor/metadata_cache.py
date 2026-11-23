"""Fetch and cache the verified BIS CBPOL SDMX structure."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

import httpx

from bis_policy_rate_monitor.fetch import DEFAULT_CACHE_DIR
from bis_policy_rate_monitor.flat_csv import FLOW_ID
from bis_policy_rate_monitor.sdmx_metadata import (
    CbpolStructure,
    SdmxStructureError,
    read_cbpol_structure,
)

METADATA_DIR = "metadata"
MANIFEST_NAME = "metadata-manifest.json"
MANIFEST_VERSION = 1
FLOW_AGENCY, _, FLOW_RESOURCE = FLOW_ID.partition(":")
FLOW_NAME, _, FLOW_VERSION = FLOW_RESOURCE.removesuffix(")").partition("(")
BIS_STRUCTURE_URL = (
    f"https://stats.bis.org/api/v2/structure/dataflow/{FLOW_AGENCY}/{FLOW_NAME}/{FLOW_VERSION}"
    "?references=descendants"
)
STRUCTURE_ACCEPT = "application/vnd.sdmx.structure+xml;version=2.1"
MAX_METADATA_BYTES = 8 * 1024 * 1024
METADATA_TRANSFER_SECONDS = 45


class MetadataCacheError(ValueError):
    """A metadata read or refresh failure with a message for the user."""


@dataclass(frozen=True, slots=True)
class MetadataCapture:
    """The verified structure and provenance of its saved XML."""

    source_url: str
    xml_path: Path
    sha256: str
    byte_count: int
    retrieved_at: datetime
    structure: CbpolStructure


def _structure_references(structure: CbpolStructure) -> dict[str, str | list[str]]:
    """Record the versioned artefacts and dimension order verified by the parser."""
    return {
        "flow_urn": structure.flow_urn,
        "dsd_urn": structure.dsd_urn,
        "dimensions": list(structure.dimensions),
        "frequency_codelist_urn": structure.frequency_codelist_urn,
        "area_codelist_urn": structure.area_codelist_urn,
    }


def _read_structure_xml(path: Path) -> CbpolStructure:
    """Check the staged XML format before resolving structural metadata."""
    # Keep the download limit meaningful when the SDMX reader can decompress gzip.
    if not path.read_bytes().removeprefix(b"\xef\xbb\xbf").lstrip().startswith(b"<"):
        raise MetadataCacheError("The BIS SDMX response must contain an XML structure message.")
    return read_cbpol_structure(path)


def _download_metadata(client: httpx.Client, destination: Path) -> tuple[int, str]:
    """Stream and hash a complete BIS structure response within the transfer limits."""
    headers = {"Accept": STRUCTURE_ACCEPT, "Accept-Encoding": "identity"}
    digest = hashlib.sha256()
    received = 0
    deadline = monotonic() + METADATA_TRANSFER_SECONDS
    try:
        with client.stream(
            "GET", BIS_STRUCTURE_URL, headers=headers, follow_redirects=False,
        ) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise MetadataCacheError("Expected a complete HTTP 200 BIS SDMX response.")
            if response.headers.get("Content-Encoding", "identity").casefold() != "identity":
                raise MetadataCacheError("Unsupported content encoding in the BIS SDMX response.")
            length = response.headers.get("Content-Length")
            invalid_length = "The BIS SDMX response has an invalid Content-Length."
            try:
                expected = int(length) if length is not None else None
            except ValueError as exc:
                raise MetadataCacheError(invalid_length) from exc
            if expected is not None and expected < 0:
                raise MetadataCacheError(invalid_length)
            limit_error = f"The BIS SDMX response exceeds the {MAX_METADATA_BYTES:,}-byte limit."
            if expected is not None and expected > MAX_METADATA_BYTES:
                raise MetadataCacheError(limit_error)
            with destination.open("wb") as output:
                # Measure the actual XML bytes against the length and time limits while saving the
                # response.
                for chunk in response.iter_raw():
                    if monotonic() >= deadline:
                        raise MetadataCacheError("The BIS SDMX transfer exceeded its time budget.")
                    received += len(chunk)
                    if received > MAX_METADATA_BYTES:
                        raise MetadataCacheError(limit_error)
                    output.write(chunk)
                    digest.update(chunk)
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        raise MetadataCacheError(f"The BIS SDMX request returned HTTP {status}.") from exc
    except httpx.RequestError as exc:
        raise MetadataCacheError(f"Could not download the BIS SDMX structure: {exc}") from exc

    if monotonic() >= deadline:
        raise MetadataCacheError("The BIS SDMX transfer exceeded its time budget.")
    if expected is not None and received != expected:
        raise MetadataCacheError(
            f"Incomplete BIS SDMX download. Expected {expected:,} bytes, received {received:,}."
        )
    return received, digest.hexdigest()


def load_cached_metadata(cache_dir: Path = DEFAULT_CACHE_DIR) -> MetadataCapture:
    """Load the active local structure and verify it against its metadata manifest."""
    directory = cache_dir / METADATA_DIR
    try:
        manifest = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
        if (
            manifest["schema_version"] != MANIFEST_VERSION
            or manifest["source_url"] != BIS_STRUCTURE_URL
        ):
            raise ValueError("Unexpected metadata source or manifest version.")
        retrieved_at = datetime.fromisoformat(manifest["retrieved_at"])
        if retrieved_at.utcoffset() is None:
            raise ValueError("Metadata retrieval time has no time zone.")
        sha256, byte_count = manifest["sha256"], manifest["byte_count"]
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("Invalid metadata digest.")
        xml_path = directory / f"{sha256}.xml"
        if (
            manifest["xml_path"] != xml_path.name
            or type(byte_count) is not int
            or not 0 < byte_count <= MAX_METADATA_BYTES
            or not xml_path.is_file()
            or xml_path.stat().st_size != byte_count
        ):
            raise ValueError("The cached structure path or byte count does not match the manifest.")
        # Validate the recorded XML digest before interpreting any structure or codelist.
        with xml_path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != sha256:
                raise ValueError("The cached structure digest does not match the manifest.")
        structure = _read_structure_xml(xml_path)
        if _structure_references(structure) != manifest["structure"]:
            raise ValueError("The cached structure references do not match the manifest.")
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise MetadataCacheError(
            f"No verified BIS SDMX metadata in {directory}. "
            f"Run bis-prates metadata --refresh. {exc}"
        ) from exc

    return MetadataCapture(
        BIS_STRUCTURE_URL, xml_path, sha256, byte_count, retrieved_at.astimezone(UTC), structure
    )


def refresh_metadata(
    client: httpx.Client, cache_dir: Path = DEFAULT_CACHE_DIR,
) -> MetadataCapture:
    """Publish a downloaded structure only after it passes the CBPOL checks."""
    directory = cache_dir / METADATA_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=directory, prefix=".refresh-") as temporary:
            staging = Path(temporary)
            staged_xml = staging / "structure.xml"
            byte_count, sha256 = _download_metadata(client, staged_xml)
            retrieved_at = datetime.now(UTC)
            # Validate the staged XML and its CBPOL references before changing the active version.
            structure = _read_structure_xml(staged_xml)
            xml_path = directory / f"{sha256}.xml"
            manifest = {
                "schema_version": MANIFEST_VERSION,
                "source_url": BIS_STRUCTURE_URL,
                "xml_path": xml_path.name,
                "sha256": sha256,
                "byte_count": byte_count,
                "retrieved_at": retrieved_at.isoformat(),
                "structure": _structure_references(structure),
            }
            staged_manifest = staging / MANIFEST_NAME
            staged_manifest.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            staged_xml.replace(xml_path)
            # The active manifest always points to a fully verified structure.
            staged_manifest.replace(directory / MANIFEST_NAME)
    except (OSError, SdmxStructureError) as exc:
        raise MetadataCacheError(f"Could not refresh BIS SDMX metadata: {exc}") from exc

    return MetadataCapture(BIS_STRUCTURE_URL, xml_path, sha256, byte_count, retrieved_at, structure)
