# Verification and design notes

## Included example

The four files in [out/](../out/) use the 19 November 2026 BIS catalogue
release, retrieved at `2026-11-21T18:14:14.129521+00:00`. The flat ZIP's
SHA-256 is `79f614ab22c21d172694cd48e9a9d551c7196f53c525008bc7ea4da8831d6494`.
The CSV and JSON carry that provenance on every record. The Markdown report
shows it in the Source section, and the chart carries it in the PNG metadata.

An independent read of the raw CSV, using the Python standard library,
checked the five latest and historical daily rates and recomputed the changes
with `Decimal`. Both source records for each area had status `A`, unit
`368` and multiplier `0`. The record positions count from 1 after the header.

| BIS area | Latest observation | Historical observation | Change |
| --- | --- | --- | ---: |
| `US` | 682137 · 2026-11-17 · 3.875% | 681772 · 2025-11-17 · 3.875% | 0 bp |
| `XM` | 692320 · 2026-11-17 · 2.5% | 691955 · 2025-11-17 · 2% | +50 bp |
| `GB` | 267450 · 2026-11-16 · 4% | 267189 · 2025-11-14 · 4% | 0 bp |
| `JP` | 418437 · 2026-11-17 · 1.25% | 418176 · 2025-11-17 · 0.5% | +75 bp |
| `CH` | 100763 · 2026-11-17 · 0% | 100502 · 2025-11-17 · 0% | 0 bp |

The UK anniversary was 2025-11-16. The source had no daily record on that
date, so the calculation used 2025-11-14 and the report explains the
two-day lag. A replay from the saved capture reproduced all four sample
files byte for byte. Running the quick start uses the latest BIS capture
available and can give different rates, dates and retrieval provenance.
[Sample report](../out/report.md) · [Quick start](../README.md#generate-the-report)

## Offline checks

The suite uses synthetic input, loopback applications and mocked network
responses. Key checks cover explicit missing values, duplicate keys with
conflicting interpretations, calendar-year comparisons and precision,
cache integrity and publication recovery, and SDMX and browser API paths.
[Fixture guide](../tests/fixtures/README.md) · [Test commands](command-reference.md#lint-and-verification)

## SDMX area diagnostics

The pinned BIS SDMX dataflow `BIS:WS_CBPOL(1.0)` references the DSD
`BIS:BIS_CBPOL(1.0)`. The tested structure provides
`BIS:CL_BIS_GL_REF_AREA(1.0)` with 239 area codes and
`BIS:CL_FREQ(1.0)` with a daily code. `pysdmx` reads the structure and the
monitor verifies the expected versions and references. The browser requests
its area list and report in parallel, so the server serializes XML validation
to avoid a race while lxml initializes the schema. The flat ZIP provides the
observations and the SDMX response has its own provenance.

In the verified capture, `EA` resolves to `XM` with daily observations,
`AF` is defined in the codelist with no daily rows, and `ZZ` is unknown
to the codelist. A failed or incompatible SDMX refresh preserves the last
verified structure. Flat-only reports can still use the daily series.
[SDMX commands](command-reference.md#optional-sdmx-area-diagnostics) · [SDMX tests](../tests/test_sdmx_metadata.py)

## Web monitor and refresh

The React monitor uses a local FastAPI server with the same report
calculation as the CLI. The ISO date filter limits the chart window. The
calendar-year lookup uses the earlier observations it needs. Gap markers
preserve the source pattern of usable days.

Refresh obtains a validated ZIP, rebuilds the daily transform and reloads
the last submitted selection. If refresh or the subsequent report request
fails, the monitor keeps the last displayed report with its retrieval time.
The packaged bundle is built from the frontend lockfile.
[Web commands](command-reference.md#web-monitor-controls) · [Workflow tests](../tests/test_report_cli.py)

## Scope and operational limits

The monitor handles daily CBPOL rates expressed in percent per year with
multiplier zero. A conflicting value or attribute that affects interpretation
withholds calculations for the affected series. Comparing revisions across
captures would need a separate policy and a versioned history.

Use a single writer per cache and report destination. The report writer
stages all four files and rolls back recoverable replacement failures. A
reader can briefly see files from different generations during replacement.
A shared service could publish immutable report bundles behind one version
pointer, enforce a shared write lock, and monitor staleness and data quality.
The browser monitor is intended for local use.
[Source contract](source-contract.md) · [Report publication](../src/bis_policy_rate_monitor/output.py)
