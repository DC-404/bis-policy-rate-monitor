"""Read and check the BIS policy rate flow's structural metadata."""

from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from bis_policy_rate_monitor.flat_csv import FLOW_ID

CBPOL_DIMENSIONS = ("FREQ", "REF_AREA", "TIME_PERIOD")
_xml_validation_lock = Lock()


class SdmxStructureError(ValueError):
    """The structure message cannot be used to interpret CBPOL."""


@dataclass(frozen=True, slots=True)
class CbpolStructure:
    """The versioned references and area labels used by the monitor."""

    flow_urn: str
    dsd_urn: str
    dimensions: tuple[str, ...]
    frequency_codelist_urn: str
    area_codelist_urn: str
    area_labels: dict[str, str]


def read_cbpol_structure(path: Path) -> CbpolStructure:
    """Validate a local SDMX structure message against the CBPOL contract."""
    try:
        from lxml.etree import XMLSchemaParseError, XMLSyntaxError
        from pysdmx.errors import PysdmxError
        from pysdmx.io import read_sdmx
        from pysdmx.model import Codelist

        try:
            # Browser requests can arrive together and race during lxml schema initialization.
            with _xml_validation_lock:
                message = read_sdmx(path, validate=True)
            flow = message.get_dataflow(f"Dataflow={FLOW_ID}")
            # Follow the versioned reference declared by the flow to its structure.
            dsd = message.get_data_structure_definition(flow.structure)
        except (
            KeyError, OSError, PysdmxError, ValueError, XMLSchemaParseError, XMLSyntaxError,
        ) as exc:
            raise SdmxStructureError(
                f"Could not read the CBPOL structure at {path}: {exc}"
            ) from exc
    except ImportError as exc:
        raise SdmxStructureError(
            'Install the project with python -m pip install -e ".[sdmx]" to read SDMX metadata.'
        ) from exc

    # A valid SDMX message can still define dimensions incompatible with this daily pipeline.
    dimensions = tuple(component.id for component in dsd.components.dimensions)
    if dimensions != CBPOL_DIMENSIONS:
        expected = ", ".join(CBPOL_DIMENSIONS)
        actual = ", ".join(dimensions) or "(none)"
        raise SdmxStructureError(
            f"{dsd.short_urn} has incompatible dimensions. Expected {expected}. Found {actual}."
        )

    # Validate that the frequency concept points to a codelist that defines the daily code used
    # here.
    frequency_codes = dsd.components["FREQ"].enumeration
    if not isinstance(frequency_codes, Codelist) or frequency_codes.sdmx_type != "codelist":
        raise SdmxStructureError(f"{dsd.short_urn} requires a codelist for FREQ.")
    if frequency_codes["D"] is None:
        raise SdmxStructureError(f"{frequency_codes.short_urn} for FREQ lacks daily code D.")

    # Use the codelist referenced by the actual REF_AREA dimension for area diagnostics.
    area_codes = dsd.components["REF_AREA"].enumeration
    if not isinstance(area_codes, Codelist) or area_codes.sdmx_type != "codelist":
        raise SdmxStructureError(f"{dsd.short_urn} requires a codelist for REF_AREA.")
    if not area_codes:
        raise SdmxStructureError(f"{area_codes.short_urn} for REF_AREA contains no codes.")

    return CbpolStructure(
        flow_urn=flow.short_urn,
        dsd_urn=dsd.short_urn,
        dimensions=dimensions,
        frequency_codelist_urn=frequency_codes.short_urn,
        area_codelist_urn=area_codes.short_urn,
        area_labels={code.id: code.name for code in area_codes},
    )
