"""Stream and classify observations from the active BIS capture."""

import csv
import io
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from zipfile import BadZipFile, ZipExtFile, ZipFile
from zlib import error as CompressionError

from bis_policy_rate_monitor.fetch import (
    CSV_MEMBER_NAME,
    DEFAULT_CACHE_DIR,
    MAX_EXPANDED_BYTES,
    FetchError,
    FetchResult,
    load_cached_archive,
)
from bis_policy_rate_monitor.flat_csv import (
    SUPPORTED_MULTIPLIER,
    SUPPORTED_UNIT,
    FlatCsvError,
    FlatHeader,
    FlatObservation,
    parse_header,
    parse_observation,
)

MAX_CSV_RECORD_CHARS = 1024 * 1024


class TransformError(ValueError):
    """The active capture could not be transformed or its outputs could not be read."""


@dataclass(frozen=True, slots=True)
class ScanResult:
    source: FetchResult
    expanded_bytes: int
    total_rows: int
    daily_rows: int
    monthly_rows: int
    unsupported_frequency_rows: int
    invalid_daily_keys: int
    eligible_daily_rates: int
    explicit_missing_daily_rows: int
    other_ineligible_daily_rows: int
    issue_counts: dict[str, int]
    notes: tuple[str, ...]


class _BoundedMember(io.RawIOBase):
    def __init__(self, member: ZipExtFile, limit: int) -> None:
        """Set the expanded-byte limit and start counting the member's reads."""
        super().__init__()
        self.member = member
        self.limit = limit
        self.bytes_read = 0

    def readable(self) -> bool:
        """Declare the ZIP stream readable for the buffered text wrapper."""
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:
        # Allow one extra byte to distinguish EOF exactly at the cap from an overrun.
        """Read into the caller's buffer and reject expanded bytes beyond the limit."""
        available = self.limit - self.bytes_read + 1
        count = self.member.readinto(memoryview(buffer)[:available])
        self.bytes_read += count
        if self.bytes_read > self.limit:
            raise TransformError(
                f"The expanded BIS CSV exceeds the {self.limit:,}-byte read limit."
            )
        return count


def _csv_records(text: TextIO) -> Iterator[list[str]]:
    """Stream strict CSV records under a decoded-character limit per logical row."""
    remaining = MAX_CSV_RECORD_CHARS

    def bounded_lines() -> Iterator[str]:
        """Share the current row's character budget across quoted physical lines."""
        nonlocal remaining
        while line := text.readline(remaining + 1):
            remaining -= len(line)
            if remaining < 0:
                raise TransformError("A BIS CSV record exceeds the decoded character limit.")
            yield line

    for cells in csv.reader(bounded_lines(), strict=True):
        # The reader can consume several physical lines before it yields one CSV record.
        remaining = MAX_CSV_RECORD_CHARS
        yield cells


def scan_daily_archive(
    consume: Callable[[FlatHeader, FlatObservation], None],
    cache_dir: Path = DEFAULT_CACHE_DIR,
    *,
    expanded_limit: int = MAX_EXPANDED_BYTES,
) -> ScanResult:
    """Send daily rows with usable keys to a consumer and verify the full scan.

    Consumers can stage rows as they arrive. A returned result confirms that the
    entire member and its CRC were checked successfully.
    """
    if not 0 < expanded_limit <= MAX_EXPANDED_BYTES:
        raise TransformError(f"Set the expanded read limit between 1 and {MAX_EXPANDED_BYTES:,}.")
    try:
        source = load_cached_archive(cache_dir)
    except FetchError as exc:
        raise TransformError(str(exc)) from exc

    total = blank_time_formats = invalid_keys = 0
    daily = monthly = unsupported = 0
    eligible = explicit_missing = other_ineligible = 0
    supported_daily = False
    issues: Counter[str] = Counter()

    try:
        with (
            ZipFile(source.archive_path) as archive,
            archive.open(CSV_MEMBER_NAME) as member,
        ):
            bounded = _BoundedMember(member, expanded_limit)
            with io.TextIOWrapper(io.BufferedReader(bounded), encoding="utf-8", newline="") as text:
                records = _csv_records(text)
                header = parse_header(next(records, ()))
                # Number logical records after the header for traceable error reports and duplicate
                # evidence.
                for total, cells in enumerate(records, 1):
                    row = parse_observation(header, cells, total)
                    blank_time_formats += row.time_format.code is None
                    issues.update(row.issues)

                    if row.frequency.code == "M":
                        monthly += 1
                        continue
                    if row.frequency.code != "D":
                        unsupported += 1
                        continue
                    daily += 1
                    # Retain the key diagnostics while excluding rows that cannot identify an
                    # observation.
                    if "invalid_area" in row.issues or "invalid_period" in row.issues:
                        invalid_keys += 1
                        continue

                    if (
                        row.unit.code == SUPPORTED_UNIT
                        and row.multiplier.code == SUPPORTED_MULTIPLIER
                    ):
                        supported_daily = True
                    if row.eligible_rate is not None:
                        eligible += 1
                    elif row.value_state == "missing":
                        explicit_missing += 1
                    else:
                        other_ineligible += 1
                    consume(header, row)

            # Reading through EOF checks the member CRC. Compare our expanded count as well.
            if bounded.bytes_read != archive.getinfo(CSV_MEMBER_NAME).file_size:
                raise TransformError("The expanded BIS CSV size differs from the ZIP metadata.")
    except (
        BadZipFile, CompressionError, csv.Error, EOFError, FlatCsvError, OSError, UnicodeError
    ) as exc:
        raise TransformError(f"Could not scan the BIS flat CSV: {exc}") from exc

    if not daily:
        raise TransformError("The archive contains no daily BIS observations.")
    if not supported_daily:
        raise TransformError(
            f"No daily rows have a usable key, unit {SUPPORTED_UNIT} and multiplier "
            f"{SUPPORTED_MULTIPLIER}."
        )

    return ScanResult(
        source=source,
        expanded_bytes=bounded.bytes_read,
        total_rows=total,
        daily_rows=daily,
        monthly_rows=monthly,
        unsupported_frequency_rows=unsupported,
        invalid_daily_keys=invalid_keys,
        eligible_daily_rates=eligible,
        explicit_missing_daily_rows=explicit_missing,
        other_ineligible_daily_rows=other_ineligible,
        issue_counts=dict(sorted(issues.items())),
        notes=header.capture_notes(row_count=total, blank_time_format_rows=blank_time_formats),
    )
