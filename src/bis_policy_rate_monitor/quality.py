"""Resolve repeated daily keys and retain metadata and quality evidence."""

from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

from bis_policy_rate_monitor.fetch import DEFAULT_CACHE_DIR
from bis_policy_rate_monitor.flat_csv import (
    CodedCell,
    FlatHeader,
    FlatObservation,
    parse_observation,
)
from bis_policy_rate_monitor.transform import ScanResult, scan_daily_archive

HISTORY_FIELDS = ("TITLE", "SOURCE_REF", "COMPILATION", "SUPP_INFO_BREAKS")
MAX_EXAMPLES_PER_KIND = 5
MAX_EXAMPLE_VALUE_CHARS = 160


@dataclass(frozen=True, slots=True)
class ConflictRecord:
    """A source record and its values for the fields disputed at a daily key."""

    record_number: int
    values: dict[str, str | None]


@dataclass(frozen=True, slots=True)
class QualityEvent:
    kind: str
    date: date
    fields: tuple[str, ...]
    records: tuple[int, ...]
    previous_date: date | None = None
    evidence: tuple[ConflictRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class SeriesQuality:
    blocked: bool
    conflicting_keys: int
    cosmetic_keys: int
    unsupported_unit_rows: int
    metadata_changes: int
    examples: tuple[QualityEvent, ...]


@dataclass(frozen=True, slots=True)
class _StoredRow:
    record_number: int
    raw_value: str
    source_attributes: tuple[str, ...]
    eligible: bool
    missing: bool


@dataclass(frozen=True, slots=True)
class QualifiedCapture:
    scan: ScanResult
    series_quality: dict[str, SeriesQuality]
    exact_repeats: int
    unique_keys: int
    emitted_rows: int
    eligible_rates: int
    explicit_missing_rows: int
    other_ineligible_rows: int
    _header: FlatHeader = field(repr=False)
    _by_area: dict[str, dict[date, _StoredRow]] = field(repr=False)
    _conflicts: set[tuple[str, date]] = field(repr=False)
    _label_disagreements: set[tuple[str, date]] = field(repr=False)
    _precision_disagreements: set[tuple[str, date]] = field(repr=False)

    def observations(self) -> Iterator[FlatObservation]:
        """Yield one unambiguous row per key in area and date order."""
        for area, rows in sorted(self._by_area.items()):
            blocked = self.series_quality[area].blocked
            for period, stored in sorted(rows.items()):
                key = area, period
                if key in self._conflicts:
                    continue
                # Retain the unambiguous dates and metadata even when the series is withheld.
                row = _restore_row(self._header, period, stored)
                label_disagrees = key in self._label_disagreements
                precision_disagrees = key in self._precision_disagreements
                if blocked or label_disagrees or precision_disagrees:
                    row = replace(
                        row,
                        eligible_rate=None if blocked else row.eligible_rate,
                        area=CodedCell(area, None) if label_disagrees else row.area,
                        decimals=CodedCell(None, None) if precision_disagrees else row.decimals,
                    )
                yield row


def _restore_row(header: FlatHeader, day: date, row: _StoredRow) -> FlatObservation:
    """Reconstruct a source row from its date, value, and shared attributes."""
    period = day.isoformat()
    shared = iter(row.source_attributes)
    cells = []
    for name in header.field_ids:
        if name == "TIME_PERIOD":
            cells.append(period)
        elif name == "OBS_VALUE":
            cells.append(row.raw_value)
        else:
            cells.append(next(shared))
    return parse_observation(header, cells, row.record_number)


def _blocking_fields(first: FlatObservation, other: FlatObservation) -> tuple[str, ...]:
    """Identify differences in value or interpretation at the same daily key."""
    # Equivalent decimal spellings are cosmetic. Ineligible values keep their source tokens.
    if first.eligible_rate is not None and other.eligible_rate is not None:
        changed_value = first.eligible_rate != other.eligible_rate
    else:
        changed_value = first.raw_value != other.raw_value
    differences = ["OBS_VALUE"] if changed_value else []
    for name, left, right in (
        ("OBS_STATUS", first.status.code, other.status.code),
        ("OBS_CONF", first.confidentiality.code, other.confidentiality.code),
        ("UNIT_MEASURE", first.unit.code, other.unit.code),
        ("UNIT_MULT", first.multiplier.code, other.multiplier.code),
        ("TITLE", first.title, other.title),
        ("SOURCE_REF", first.source_ref, other.source_ref),
        ("COMPILATION", first.compilation, other.compilation),
        ("SUPP_INFO_BREAKS", first.break_notes, other.break_notes),
        ("OBS_PRE_BREAK", first.pre_break, other.pre_break),
    ):
        if left != right:
            differences.append(name)
    return tuple(differences)


class _Events:
    def __init__(self) -> None:
        """Initialize event counts and the examples shown in the manifest."""
        self.counts: Counter[str] = Counter()
        self.examples: list[QualityEvent] = []

    def add(self, event: QualityEvent) -> None:
        """Count every quality event and keep examples up to the per-kind limit."""
        self.counts[event.kind] += 1
        if self.counts[event.kind] <= MAX_EXAMPLES_PER_KIND:
            self.examples.append(event)


class _DailyIndex:
    def __init__(self, header: FlatHeader) -> None:
        """Index one daily flow by area and date while sharing repeated attributes."""
        self.header = header
        # The scanner has already fixed the flow and frequency for every accepted key.
        self.by_area: dict[str, dict[date, _StoredRow]] = defaultdict(dict)
        self.variants: dict[tuple[str, date], dict[tuple[str, tuple[str, ...]], _StoredRow]] = {}
        self.events: dict[str, _Events] = defaultdict(_Events)
        # Reuse identical strings and attribute tuples without changing the source cells.
        self.values: dict[str, str] = {}
        self.attributes: dict[tuple[str, ...], tuple[str, ...]] = {}
        attribute_fields = [f for f in header.field_ids if f not in {"TIME_PERIOD", "OBS_VALUE"}]
        self.attribute_positions = [header.positions[field] for field in attribute_fields]
        self.history_positions = [
            (field, position)
            for position, field in enumerate(attribute_fields) if field in HISTORY_FIELDS
        ]
        self.exact_repeats = 0

    def add(self, row: FlatObservation) -> None:
        """Keep the first row and distinct alternatives and count exact repeats."""
        area, day = row.area.code, row.period
        assert area is not None and isinstance(day, date)
        value = self.values.setdefault(row.raw_value, row.raw_value)
        fields = tuple(row.raw_cells[position] for position in self.attribute_positions)
        attributes = self.attributes.setdefault(fields, fields)
        record = _StoredRow(
            row.record_number, value, attributes, row.eligible_rate is not None,
            row.value_state == "missing",
        )
        unit_fields = tuple(
            field for reason, field in (
                ("unsupported_unit", "UNIT_MEASURE"), ("unsupported_multiplier", "UNIT_MULT")
            ) if reason in row.issues
        )
        if unit_fields:
            self.events[area].add(
                QualityEvent("unsupported_unit", day, unit_fields, (row.record_number,))
            )

        series = self.by_area[area]
        first = series.setdefault(day, record)
        if first is record:
            return
        # The shared strings and tuples let identity compare the raw cells for this key.
        if value is first.raw_value and attributes is first.source_attributes:
            self.exact_repeats += 1
            return
        # Save each distinct version so duplicates can be resolved after the scan, regardless of
        # input order.
        alternatives = self.variants.setdefault((area, day), {})
        if alternatives.setdefault((value, attributes), record) is not record:
            self.exact_repeats += 1

    def _conflict_record(self, row: FlatObservation, fields: tuple[str, ...]) -> ConflictRecord:
        """Capture bounded examples without publishing restricted observation values."""
        values: dict[str, str | None] = {}
        for field_id in fields:
            if field_id in {"OBS_VALUE", "OBS_PRE_BREAK"} and "restricted_value" in row.issues:
                values[field_id] = None
                continue
            position = self.header.positions.get(field_id)
            value = row.raw_cells[position] if position is not None else ""
            if field_id == "OBS_STATUS":
                value = row.status.code or ""
            if len(value) > MAX_EXAMPLE_VALUE_CHARS:
                value = value[:MAX_EXAMPLE_VALUE_CHARS] + "…"
            values[field_id] = value
        return ConflictRecord(row.record_number, values)

    def finish(self, scan: ScanResult) -> QualifiedCapture:
        """Resolve duplicates and compute series quality from the complete scan."""
        conflicts = set()
        label_disagreements = set()
        precision_disagreements = set()
        # Classify same-key alternatives before building the per-area quality result.
        for (area, day), alternatives in sorted(self.variants.items()):
            key = area, day
            first = _restore_row(self.header, day, self.by_area[area][day])
            blocking: set[str] = set()
            changed: set[str] = set()
            examples = {first.record_number: first}
            cosmetic_examples = {first.record_number}
            for stored in alternatives.values():
                other = _restore_row(self.header, day, stored)
                differences = set(_blocking_fields(first, other))
                if differences - blocking:
                    examples[other.record_number] = other
                blocking.update(differences)
                source_differences = {
                    field for field, left, right in
                    zip(self.header.field_ids, first.raw_cells, other.raw_cells, strict=True)
                    if left != right
                }
                if source_differences - changed:
                    cosmetic_examples.add(other.record_number)
                changed.update(source_differences)
                if first.area.label != other.area.label:
                    label_disagreements.add(key)
                if first.decimals.code != other.decimals.code:
                    precision_disagreements.add(key)

            if blocking:
                conflicts.add(key)
                fields, records = tuple(sorted(blocking)), tuple(sorted(examples))
                self.events[area].add(
                    QualityEvent(
                        "duplicate_conflict", day, fields, records,
                        evidence=tuple(self._conflict_record(examples[record], fields)
                                       for record in records),
                    )
                )
            else:
                self.events[area].add(
                    QualityEvent("cosmetic_difference", day, tuple(sorted(changed)),
                                 tuple(sorted(cosmetic_examples)))
                )

        summaries = {}
        eligible = missing = emitted = 0
        for area, rows in sorted(self.by_area.items()):
            events = self.events[area]
            # A substantive conflict or unsupported unit withholds calculations for the affected
            # series.
            blocked = bool(events.counts["duplicate_conflict"] or events.counts["unsupported_unit"])
            previous = None
            for day, stored in sorted(rows.items()):
                if (area, day) in conflicts:
                    continue
                emitted += 1
                eligible += stored.eligible and not blocked
                missing += stored.missing
                if previous:
                    older_day, older = previous
                    # Compare dated attributes in chronological order to expose potential instrument
                    # changes.
                    if older.source_attributes is not stored.source_attributes:
                        fields = tuple(
                            field for field, index in self.history_positions
                            if older.source_attributes[index] != stored.source_attributes[index]
                        )
                        if fields:
                            events.add(QualityEvent(
                                "metadata_change", day, fields,
                                (older.record_number, stored.record_number), older_day,
                            ))
                previous = day, stored
            summaries[area] = SeriesQuality(
                blocked=blocked,
                conflicting_keys=events.counts["duplicate_conflict"],
                cosmetic_keys=events.counts["cosmetic_difference"],
                unsupported_unit_rows=events.counts["unsupported_unit"],
                metadata_changes=events.counts["metadata_change"],
                examples=tuple(events.examples),
            )

        return QualifiedCapture(
            scan=scan,
            series_quality=summaries,
            exact_repeats=self.exact_repeats,
            unique_keys=emitted + len(conflicts),
            emitted_rows=emitted,
            eligible_rates=eligible,
            explicit_missing_rows=missing,
            other_ineligible_rows=emitted - eligible - missing,
            _header=self.header,
            _by_area=self.by_area,
            _conflicts=conflicts,
            _label_disagreements=label_disagreements,
            _precision_disagreements=precision_disagreements,
        )


def qualify_daily_archive(cache_dir: Path = DEFAULT_CACHE_DIR) -> QualifiedCapture:
    """Scan the archive and resolve duplicates after the member passes its checks."""
    index = None

    def collect(header: FlatHeader, row: FlatObservation) -> None:
        """Initialize the index from the parsed header and add each usable key."""
        nonlocal index
        if index is None:
            index = _DailyIndex(header)
        index.add(row)

    scan = scan_daily_archive(collect, cache_dir)
    assert index is not None  # A successful scan contains daily rows with usable keys.
    return index.finish(scan)
