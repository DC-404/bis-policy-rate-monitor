"""Check the pinned SDMX references and safe publication of validated structure XML."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

import httpx
import pytest
from typer.testing import CliRunner

from bis_policy_rate_monitor import metadata_cache
from bis_policy_rate_monitor.cli import app
from bis_policy_rate_monitor.metadata_cache import (
    MetadataCacheError,
    load_cached_metadata,
    refresh_metadata,
)
from bis_policy_rate_monitor.sdmx_metadata import SdmxStructureError, read_cbpol_structure

pytest.importorskip("pysdmx")
pytest.importorskip("lxml")
pytest.importorskip("sdmxschemas")
pytest.importorskip("xmltodict")

STRUCTURE_FIXTURE = Path(__file__).parent / "fixtures" / "synthetic-cbpol-structure.xml"


def test_cbpol_structure_follows_declared_versioned_references() -> None:
    """Follow structure and codelist references instead of guessing their IDs."""
    structure = read_cbpol_structure(STRUCTURE_FIXTURE)

    assert structure.flow_urn == "Dataflow=BIS:WS_CBPOL(1.0)"
    # The fixture uses distinct TEST IDs and versions to catch hard-coded references.
    assert structure.dsd_urn == "DataStructure=TEST:DS_CBPOL_TEST(2.0)"
    assert structure.dimensions == ("FREQ", "REF_AREA", "TIME_PERIOD")
    assert structure.frequency_codelist_urn == "Codelist=TEST:CL_FREQ_TEST(1.1)"
    assert structure.area_codelist_urn == "Codelist=TEST:CL_AREA_TEST(2.5)"
    assert structure.area_labels == {"AF": "Afghanistan", "US": "United States", "XM": "Euro area"}


def test_cbpol_structure_requires_the_pinned_flow_version(tmp_path: Path) -> None:
    """Reject a version change that the current monitor has not been verified against."""
    xml = STRUCTURE_FIXTURE.read_text(encoding="utf-8")
    path = tmp_path / "different-flow-version.xml"
    # Modify the flow version while leaving the rest of the message parseable.
    path.write_text(xml.replace('id="WS_CBPOL" version="1.0"', 'id="WS_CBPOL" version="1.1"'),
                    encoding="utf-8")

    with pytest.raises(SdmxStructureError, match=r"Dataflow=BIS:WS_CBPOL\(1\.0\)"):
        read_cbpol_structure(path)


def test_cbpol_structure_rejects_a_changed_dimension_order(tmp_path: Path) -> None:
    """Require CBPOL's ordered key even when the same component IDs are present."""
    tree = ElementTree.parse(STRUCTURE_FIXTURE)
    dimensions = tree.find(".//{*}DimensionList")
    assert dimensions is not None
    freq, area, period = dimensions
    # Keep all dimensions and swap only the series-key components.
    dimensions[:] = [area, freq, period]
    path = tmp_path / "reordered-dimensions.xml"
    tree.write(path, encoding="utf-8", xml_declaration=True)

    with pytest.raises(SdmxStructureError, match="Expected FREQ, REF_AREA, TIME_PERIOD"):
        read_cbpol_structure(path)


def test_cbpol_structure_reports_an_unresolved_area_codelist_reference(tmp_path: Path) -> None:
    """Explain unresolved structural references before using the area lookup."""
    xml = STRUCTURE_FIXTURE.read_text(encoding="utf-8")
    path = tmp_path / "unresolved-area-codelist.xml"
    # Rename the codelist definition so the DSD reference no longer resolves.
    unresolved = xml.replace('id="CL_AREA_TEST" version="2.5" package=',
                             'id="MISSING_AREA_LIST" version="2.5" package=')
    path.write_text(unresolved, encoding="utf-8")

    with pytest.raises(SdmxStructureError, match="Codelist=TEST:MISSING_AREA_LIST"):
        read_cbpol_structure(path)


def test_cbpol_structure_reports_truncated_xml(tmp_path: Path) -> None:
    """Convert unreadable structure XML into an actionable application error."""
    xml = STRUCTURE_FIXTURE.read_bytes()
    path = tmp_path / "truncated.xml"
    # Cut the fixture in the middle of the response to model an interrupted download.
    path.write_bytes(xml[:len(xml) // 2])

    with pytest.raises(SdmxStructureError, match="Could not read the CBPOL structure"):
        read_cbpol_structure(path)


def metadata_client(body: bytes, headers: dict[str, str] | None = None) -> httpx.Client:
    """Serve the exact structure bytes and headers through a mock transport."""
    return httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))
    ))


def test_metadata_cli_refreshes_then_inspects_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the structure request, its stored provenance and network-free reuse."""
    body = STRUCTURE_FIXTURE.read_bytes()
    requests = []
    client = httpx.Client

    # Check the version-pinned endpoint and media type at the HTTP boundary.
    def reply(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == (
            "https://stats.bis.org/api/v2/structure/dataflow/BIS/WS_CBPOL/1.0"
            "?references=descendants"
        )
        assert request.headers["Accept"] == "application/vnd.sdmx.structure+xml;version=2.1"
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, stream=httpx.ByteStream(body))

    monkeypatch.setattr(httpx, "Client", lambda **options: client(
        transport=httpx.MockTransport(reply), **options
    ))
    runner = CliRunner()
    args = ["metadata", "--cache-dir", str(tmp_path)]
    missing = runner.invoke(app, args)
    assert missing.exit_code == 1 and "Run bis-prates metadata --refresh" in missing.stderr
    assert requests == []

    refreshed = runner.invoke(app, [*args, "--refresh"])
    inspected = runner.invoke(app, args)

    assert refreshed.exit_code == inspected.exit_code == 0, (refreshed.output, inspected.output)
    assert "Refreshed" in refreshed.stdout and "Cached" in inspected.stdout
    assert "Codelist=TEST:CL_AREA_TEST(2.5) (3 codes)" in inspected.stdout
    # Only refresh should use HTTP. Inspection reads the verified local XML.
    assert len(requests) == 1
    capture = load_cached_metadata(tmp_path)
    manifest = json.loads((tmp_path / "metadata" / "metadata-manifest.json").read_text())
    assert capture.sha256 == hashlib.sha256(body).hexdigest() == manifest["sha256"]
    assert capture.xml_path.read_bytes() == body and capture.byte_count == len(body)
    assert capture.xml_path.name == manifest["xml_path"] == f"{capture.sha256}.xml"
    assert capture.retrieved_at == datetime.fromisoformat(manifest["retrieved_at"])
    assert capture.retrieved_at.tzinfo == UTC
    assert manifest["structure"]["dsd_urn"] == "DataStructure=TEST:DS_CBPOL_TEST(2.0)"


@pytest.mark.parametrize("failure", ["incomplete-transfer", "missing-daily-code"])
def test_refresh_failure_preserves_verified_metadata(tmp_path: Path, failure: str) -> None:
    """Keep the previous XML and manifest after transfer or structure validation fails."""
    body = STRUCTURE_FIXTURE.read_bytes()
    with metadata_client(body) as client:
        previous = refresh_metadata(client, tmp_path)
    manifest = previous.xml_path.parent / "metadata-manifest.json"
    saved_manifest = manifest.read_bytes()
    # Exercise both incomplete HTTP bodies and well-formed but incompatible metadata.
    if failure == "incomplete-transfer":
        headers = {"Content-Length": str(len(body))}
        reply, reason = body[:len(body) // 2], "Incomplete BIS SDMX download"
    else:
        headers = None
        reply = body.replace(b'id="D"', b'id="W"')
        reason = r"CL_FREQ_TEST.*for FREQ lacks daily code D"

    with metadata_client(reply, headers) as client, pytest.raises(MetadataCacheError, match=reason):
        refresh_metadata(client, tmp_path)

    # The previous version must remain byte-identical and still parse successfully.
    assert manifest.read_bytes() == saved_manifest
    assert load_cached_metadata(tmp_path) == previous
    assert sorted(path.name for path in manifest.parent.iterdir()) == sorted(
        [manifest.name, previous.xml_path.name]
    )


def test_schema_compilation_failure_reports_metadata_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrap a schema initialization failure so the optional metadata path can recover."""
    from lxml.etree import XMLSchemaParseError
    from pysdmx import io

    with metadata_client(STRUCTURE_FIXTURE.read_bytes()) as client:
        refresh_metadata(client, tmp_path)

    # Inject the lxml error deterministically instead of depending on thread scheduling.
    def fail_schema(path: Path, *, validate: bool) -> None:
        raise XMLSchemaParseError("Could not initialise the schema")

    monkeypatch.setattr(io, "read_sdmx", fail_schema)

    # The cache interface should expose a metadata error rather than the library exception.
    with pytest.raises(MetadataCacheError, match="Could not initialise the schema"):
        load_cached_metadata(tmp_path)


def test_first_refresh_rejects_oversized_unadvertised_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bound the XML download even when the server provides no length header."""
    body = STRUCTURE_FIXTURE.read_bytes()
    # Set a small cap and stream the fixture without a declared byte count.
    monkeypatch.setattr(metadata_cache, "MAX_METADATA_BYTES", len(body) - 1)
    with metadata_client(body) as client, pytest.raises(MetadataCacheError, match="byte limit"):
        refresh_metadata(client, tmp_path)

    assert list((tmp_path / "metadata").iterdir()) == []


def test_refresh_checks_elapsed_transfer_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enforce the metadata deadline without waiting for the wall clock."""
    # Advance the injected clock from transfer start to beyond the budget.
    ticks = iter((0, metadata_cache.METADATA_TRANSFER_SECONDS + 1))
    monkeypatch.setattr(metadata_cache, "monotonic", lambda: next(ticks))
    body = STRUCTURE_FIXTURE.read_bytes()
    with metadata_client(body) as client, pytest.raises(MetadataCacheError, match="time budget"):
        refresh_metadata(client, tmp_path)

    assert list((tmp_path / "metadata").iterdir()) == []


def test_cache_inspection_checks_the_xml_digest(tmp_path: Path) -> None:
    """Detect changed cached XML that still has the correct byte count."""
    body = STRUCTURE_FIXTURE.read_bytes()
    with metadata_client(body) as client:
        capture = refresh_metadata(client, tmp_path)
    # Replace the area name with an equally long value to bypass a size-only check.
    capture.xml_path.write_bytes(body.replace(b"Euro area", b"Euro zone"))

    with pytest.raises(MetadataCacheError, match="digest does not match"):
        load_cached_metadata(tmp_path)


def test_failed_manifest_switch_leaves_previous_xml_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the last valid XML active when publishing a new pointer fails."""
    body = STRUCTURE_FIXTURE.read_bytes()
    with metadata_client(body) as client:
        previous = refresh_metadata(client, tmp_path)
    replace = Path.replace

    # Fail only the publication step so the replacement can otherwise validate normally.
    def interrupt_manifest(source: Path, target: Path) -> Path:
        if target.name == "metadata-manifest.json":
            raise OSError("simulated metadata manifest switch failure")
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", interrupt_manifest)
    newer = body.replace(b"Euro area", b"Euro zone")
    with (
        metadata_client(newer) as client,
        pytest.raises(MetadataCacheError, match="switch failure"),
    ):
        refresh_metadata(client, tmp_path)

    assert load_cached_metadata(tmp_path) == previous
