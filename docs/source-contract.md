# How the monitor uses BIS policy rate data

The monitor takes daily observations from the BIS flat CSV export. This
note describes what the export contains, how the monitor decides which
rates it can use, and what a reader can trace back to the source.
[Verification evidence](verification.md)

## Source evidence

The BIS catalogue listed **Central bank policy rates (CSV, flat)** for
19 November 2026. The archive inspected on 20 November contained one
member, `WS_CBPOL_csv_flat.csv`. A ZIP of about 4.15 MB expanded to about
477 MB of CSV. It contained 709,984 daily rows and 25,172 monthly rows
across 49 areas. All `A` observations had finite rates, all `M` observations
contained the literal `NaN`, and every row used unit `368` and multiplier
`0`. There were no duplicate periods within a frequency and area.
[BIS bulk catalogue](https://data.bis.org/bulkdownload)

The SDMX dataflow `BIS:WS_CBPOL(1.0)` references `BIS:BIS_CBPOL(1.0)`.
The structure inspection with `pysdmx` verified the ordered dimensions
`FREQ`, `REF_AREA`, and `TIME_PERIOD`, the string measure `OBS_VALUE`,
and the attribute attachments. The flat ZIP provides the observations,
and the optional SDMX lookup provides definitions and area codes.
[SDMX verification](verification.md#sdmx-area-diagnostics)

## Fetch and provenance

The fetch command finds the named flat export in the catalogue. It caps
the HTTPS transfer size and duration and checks the ZIP member name and
declared expanded size. The transform streams the member, caps the bytes
read and reads through to the end so the ZIP checksum can be verified.
[Download limits](../src/bis_policy_rate_monitor/fetch.py) · [Streaming limits](../src/bis_policy_rate_monitor/transform.py)

The active archive is recorded in `fetch-manifest.json` and stored under
its SHA-256 digest. The manifest records the source URLs, UTC retrieval
time, byte count and digest, plus the catalogue release date, ETag and
`Last-Modified` when available. A verified archive is reused for up to
24 hours unless fetch is run with `--refresh`. A failed download or ZIP
check leaves the previous manifest in place. Reports can use a verified
saved capture at any age. [Cache controls](command-reference.md#report-and-cache-controls)

The transform records the archive digest, capture time, flow ID and
derived schema version. The report verifies the derived files against
their checksums and the active archive. Its outputs associate the
retrieval time and digest with the actual observation dates. Verified
SDMX metadata carries its own retrieval provenance.
[Derived schema and manifest](../src/bis_policy_rate_monitor/derived.py)

## Flat columns and parsing

The parser reads UTF-8 CSV and matches column IDs before the first colon,
so changing a caption or reordering columns is harmless. It reports
duplicate, empty, required-but-missing and unfamiliar IDs, along with
malformed rows. Absent optional columns are represented as missing
values and noted in the report.

| Fields | Column required by the parser | Use |
| --- | --- | --- |
| `STRUCTURE`, `STRUCTURE_ID`, `ACTION` | Required | Identifies a `dataflow` row for `BIS:WS_CBPOL(1.0)` with action `I`. |
| `FREQ`, `REF_AREA`, `TIME_PERIOD` | Required | Identifies an observation within that flow and determines how the period is parsed. |
| `OBS_VALUE`, `OBS_STATUS` | Required | Determines whether the original rate token can be used in calculations. |
| `UNIT_MEASURE`, `UNIT_MULT` | Required | Establishes the unit and multiplier. Daily calculations cover unit `368` with multiplier `0`. |
| `TIME_FORMAT`, `DECIMALS` | Optional | Retains time-format metadata and display precision where supplied. The DSD attaches both to the series. |
| `TITLE`, `SOURCE_REF`, `COMPILATION`, `SUPP_INFO_BREAKS` | Optional | Preserves dated information about the instrument, method, source and breaks. |
| `OBS_CONF`, `OBS_PRE_BREAK` | Optional | Carries the confidentiality code and any pre-break value. |

For coded fields, the parser splits a label from its code at the first
`: `. The original cells remain available during duplicate resolution.
The archive digest and logical CSV record number identify the source row.
Monthly rows are counted and excluded, and unsupported frequencies are
counted separately. A daily key needs two uppercase ASCII letters or digits
for the area code and a valid `YYYY-MM-DD` date. The transform counts invalid
keys and leaves those rows out of the daily tables.

The DSD declares `TIME_FORMAT` mandatory, but BIS left it blank throughout
the inspected flat export. The monitor uses `FREQ` to validate the period
and reports the discrepancy. `DECIMALS` affects display precision. The
calculation uses the exact rate. [Flat parser](../src/bis_policy_rate_monitor/flat_csv.py)

## Rate eligibility and duplicate handling

The parser interprets each rate with its status, confidentiality and
units. This preserves the difference between a rate of zero, a published
missing value and an observation the monitor cannot use.

| Source row | Result |
| --- | --- |
| `A` and a finite decimal in supported units | The rate is parsed as `Decimal` and is eligible if the confidentiality and series checks pass. |
| `M` and the literal `NaN` | The date and missing status are retained, with no eligible rate. |
| Blank, malformed or non-finite value, unsupported status, or status/value mismatch | The date is retained with a reason and no eligible rate. |
| Missing or unsupported unit or multiplier on a valid daily key | Calculations are withheld for the affected series, even when that row has no rate. |
| Explicit `OBS_CONF` other than `F` | The rate and pre-break value are withheld from published data. The parser accepts an absent confidentiality code for this public export. |

Within a capture the key is `(flow, FREQ, REF_AREA, TIME_PERIOD)`. The
series key omits the date. Duplicate checks run before rate selection
because a missing or restricted row may itself be part of a conflict.

- **Exact repeats.** All source cells agree, so the monitor keeps one
  copy and counts the repeats.
- **Conflicts.** A difference in rate, status, units, confidentiality or
  interpretation metadata withholds calculations for that series. A finite
  rate marked `A` and a `NaN` marked `M` at the same key are one example.
  The report shows the affected area, disputed fields and source record
  numbers. The examples redact restricted values.
- **Descriptive differences.** A different label for the same code, an
  equivalent finite rate such as `3.50` and `3.5`, or a difference confined
  to time format or display precision can produce a warning. The observation
  still contributes once. A disputed display precision leaves the exact
  value in neutral formatting.

Attributes are tied to dated observations by a metadata ID. A change in the
title, source, compilation or break notes across dates is recorded as a
possible change in instrument or method. When relevant to the selected pair,
the report calls it out. The monitor rejects captures with malformed CSV,
an incompatible envelope, no daily rows, or no daily rows with usable keys
and supported units. A valid capture containing only missing rates can still
be reported. [Quality resolution](../src/bis_policy_rate_monitor/quality.py) · [Scope and operational limits](verification.md#scope-and-operational-limits)

## Area selection and snapshot output

The monitor trims and uppercases requested codes, maps `EA` to BIS code
`XM`, and shows both codes. A request such as `EA,XM` produces a duplicate
selection error. Daily rows in the capture establish availability. A
verified SDMX codelist distinguishes a defined code with no daily rows
from an unknown code. If daily data exists for a code absent from that
codelist, the report flags the disagreement and retains the flat-data
result. [SDMX area diagnostics](verification.md#sdmx-area-diagnostics)

For each unblocked area, the comparison starts from its latest eligible
observation. The monitor subtracts one calendar year, using 28 February
for a leap-day anniversary in a non-leap year, and selects the latest
eligible observation on or before that cutoff. The change is
`(latest_rate - historical_rate) × 100` basis points. The chart's start
date limits display. The comparison can still use earlier observations.
The chart joins consecutive usable days, leaves gaps for unavailable rates
and marks isolated usable observations with a dot.
[Snapshot calculation](../src/bis_policy_rate_monitor/snapshot.py)

The Markdown, CSV and JSON report the same selected rates and dates.
The summaries distinguish a published missing value, an ineligible row
and a date with no observation. When a historical rate is unavailable,
the report still shows the latest usable rate and explains why the change
is blank. It warns when the latest usable rate is more than seven calendar
days before ZIP retrieval or the historical rate is more than seven days
before the anniversary. The report still shows the selected rates and dates.

Unavailable values appear as empty CSV cells, JSON `null` or a dash in
Markdown. CSV and JSON serialize the available rates and changes as exact
decimal strings. They also carry the selected observations' unit,
multiplier, decimals and title where available. [Summary fields](../src/bis_policy_rate_monitor/summary.py)

## Examples from the synthetic fixture

The test fixture includes an exact repeat for `XM`, conflicting rate and
status rows for `GB`, and a descriptive label difference for `US`. It also
tests an anniversary fallback with two days of lag. The fixture guide
lists the rows and how they affect the expected reports.
[Fixture guide](../tests/fixtures/README.md)
