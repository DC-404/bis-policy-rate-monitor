"""Explain area requests using structural metadata and captured daily coverage."""

from collections.abc import Mapping, Set
from dataclasses import dataclass
from typing import Literal

AreaCodeStatus = Literal["defined", "defined_no_daily", "unknown", "codelist_mismatch"]


@dataclass(frozen=True, slots=True)
class AreaDiagnostic:
    """The area code's standing and any guidance for the request."""

    code_status: AreaCodeStatus
    has_daily_series: bool
    message: str | None = None
    suggestion: str | None = None


def diagnose_area(
    bis_code: str,
    *,
    area_labels: Mapping[str, str],
    daily_areas: Set[str],
    usable_areas: Set[str],
) -> AreaDiagnostic:
    """Classify a canonical BIS code against one codelist and a flat capture."""
    # SDMX code membership and observations in this capture answer separate questions.
    has_daily = bis_code in daily_areas
    if bis_code in area_labels:
        if has_daily:
            return AreaDiagnostic("defined", True)
        return AreaDiagnostic(
            "defined_no_daily",
            False,
            f"{bis_code} is defined in the cached BIS codelist. "
            "This capture has no daily observations for it.",
        )
    if has_daily:
        return AreaDiagnostic(
            "codelist_mismatch",
            True,
            f"The daily capture contains {bis_code}. "
            "Its code is absent from the cached BIS area codelist.",
        )

    transposed = bis_code[::-1]
    # A suggestion must be defined by BIS and actually produce a usable rate in this capture.
    candidates = usable_areas & area_labels.keys()
    if transposed in candidates:
        suggestion = transposed
    else:
        nearby = [
            code for code in candidates
            if len(code) == len(bis_code)
            and sum(a != b for a, b in zip(code, bis_code, strict=True)) == 1
        ]
        # A tied one-character edit offers no clear recommendation.
        suggestion = nearby[0] if len(nearby) == 1 else None
    message = f"{bis_code} is unknown in the cached BIS area codelist."
    if suggestion:
        message += f" Try {suggestion} from this capture's usable daily series."
    return AreaDiagnostic("unknown", False, message, suggestion)
