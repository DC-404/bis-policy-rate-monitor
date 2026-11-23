"""Stage and publish the four files that make up one report."""

import csv
import json
import shutil
from pathlib import Path

from bis_policy_rate_monitor.chart import CHART_PATH, write_chart
from bis_policy_rate_monitor.report import REPORT_PATH, write_report
from bis_policy_rate_monitor.summary import ReportSummary

CSV_NAME = "summary.csv"
JSON_NAME = "summary.json"
OUTPUT_NAMES = (CSV_NAME, JSON_NAME, CHART_PATH.name, REPORT_PATH.name)


class ReportError(Exception):
    """The report files could not be generated or published."""


def _write_summaries(summary: ReportSummary, staging: Path) -> None:
    """Serialize the same snapshot rows to staged CSV and JSON files."""
    # Use one set of exact serialized values for both machine-readable formats.
    rows = summary.records()
    if not rows:
        raise ValueError("At least one area is required for a report.")

    with (staging / CSV_NAME).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    with (staging / JSON_NAME).open("w", encoding="utf-8") as stream:
        json.dump(rows, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def _publish(staging: Path, out_dir: Path) -> None:
    """Replace the report files and restore prior versions if a replacement fails."""
    # Each file is replaced individually, so keep backups for a recoverable switch failure.
    backups = staging / "previous"
    backups.mkdir()
    for name in OUTPUT_NAMES:
        target = out_dir / name
        if target.is_symlink() or target.is_file():
            shutil.copy2(target, backups / name, follow_symlinks=False)
        elif target.exists():
            raise OSError(f"Expected a report file at {target}.")

    try:
        for name in OUTPUT_NAMES:
            (staging / name).replace(out_dir / name)
    except (OSError, KeyboardInterrupt) as exc:
        try:
            # A staged file that has already moved identifies one of the paths to restore or remove.
            for name in OUTPUT_NAMES:
                if (staging / name).exists():
                    continue
                previous, target = backups / name, out_dir / name
                if previous.is_symlink() or previous.exists():
                    previous.replace(target)
                else:
                    target.unlink(missing_ok=True)
        except (OSError, KeyboardInterrupt) as recovery_error:
            raise ReportError(
                f"Publication failed ({exc}) and recovery stopped ({recovery_error}). "
                f"Check {staging} for the remaining staged files and backups."
            ) from recovery_error
        raise


def write_outputs(summary: ReportSummary, out_dir: Path = Path("out")) -> tuple[Path, ...]:
    """Build every report file before replacing any result in the output folder."""
    staging = out_dir / ".report-staging"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            # A fixed staging directory gives one writer exclusive access to this output folder.
            staging.mkdir()
        except FileExistsError as exc:
            raise ReportError(
                f"Report staging already exists at {staging}. "
                "Wait for the active run or inspect the staging folder if it was interrupted."
            ) from exc
    except OSError as exc:
        raise ReportError(f"Could not prepare the report folder {out_dir}: {exc}") from exc

    keep_staging = False
    try:
        # Finish rendering and writing all four artifacts before replacing any visible report files.
        _write_summaries(summary, staging)
        write_chart(summary, staging / CHART_PATH.name)
        write_report(summary, staging / REPORT_PATH.name)
        _publish(staging, out_dir)
    except ReportError:
        keep_staging = True
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ReportError(f"Could not publish the report in {out_dir}: {exc}") from exc
    finally:
        if not keep_staging:
            try:
                shutil.rmtree(staging)
            except OSError as exc:
                raise ReportError(f"Could not remove report staging at {staging}: {exc}") from exc

    return tuple(out_dir / name for name in OUTPUT_NAMES)
