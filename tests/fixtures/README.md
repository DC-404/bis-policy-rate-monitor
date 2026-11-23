# Synthetic CBPOL fixtures

The [ZIP](synthetic-cbpol-flat.zip) contains one member named
`WS_CBPOL_csv_flat.csv`. Its [readable source](WS_CBPOL_csv_flat.csv) matches
the 18-column header and labelled cell format from the BIS flat export. All
observations and narrative metadata in this fixture are invented. Use the ZIP
to exercise fetch and transform locally. [Source contract](../../docs/source-contract.md)

## Row guide

Numbers start at the first data record after the header. Each record uses
unit `368`, multiplier `0`, and a blank `TIME_FORMAT`. The report should
explain the blank once for the capture and validate dates using `FREQ`.

| Record | Series and period | Rate and status | Expected handling |
| ---: | --- | --- | --- |
| 1 | `D.XM` on 2025-11-14 | `4.00`, `A` | Historical rate selected for the euro area. |
| 2 | `D.XM` on 2025-11-14 | `4.00`, `A` | All 18 cells match record 1. Count and collapse one exact repeat. |
| 3 | `D.XM` on 2025-11-16 | `NaN`, `M` | The exact anniversary is explicitly missing. Historical lookup uses record 1. |
| 4 | `D.XM` on 2026-11-16 | `4.50`, `A` | Latest usable euro area rate. |
| 5 | `D.XM` on 2026-11-17 | `NaN`, `M` | Newest dated euro area row is explicitly missing. Latest lookup uses record 4. |
| 6 | `M.XM` for 2026-12 | `9.75`, `A` | Deliberately later than all daily rows. Exclude before processing daily dates or selecting a latest rate. |
| 7 | `D.US` on 2025-11-17 | `3.25`, `A` | Selected historical rate. Its quoted compilation text includes both `: ` and a comma. |
| 8 | `D.US` on 2026-11-18 | `3.00`, `A` | Latest US rate. The older and newer observations describe different synthetic instruments. |
| 9 | `D.US` on 2026-11-18 | `3.00`, `A` | Only the label after `US` differs from record 8. Use one rate and record a label warning. |
| 10 | `D.GB` on 2025-11-17 | `4.25`, `A` | Historically usable rate in the series that will be blocked. |
| 11 | `D.GB` on 2026-11-17 | `5.00`, `A` | The same key appears in record 12 with a different rate token and status. |
| 12 | `D.GB` on 2026-11-17 | `NaN`, `M` | Block the GB series and report the disputed date and fields. |
| 13 | `D.CH` on 2026-11-17 | `-0.25`, `A` | A valid negative rate. The fixture has no Swiss rate at or before its anniversary. |

The archive contains 13 data records, including 12 daily records and one
monthly record. There are nine distinct daily keys before any series is
blocked, one exact repeat, one label discrepancy and one conflicting key.

## Expected snapshots

All rates are in per cent per year. `—` denotes an unavailable output value.

| Requested → BIS | Latest date and rate | Anniversary | Historical date and rate | Change (bp) | Explanation |
| --- | --- | --- | --- | ---: | --- |
| `EA → XM` | 2026-11-16, `4.50` | 2025-11-16 | 2025-11-14, `4.00` | +50 | The newest dated row and the anniversary row are explicitly missing. Show both selections and two days of historical lag. |
| `US → US` | 2026-11-18, `3.00` | 2025-11-18 | 2025-11-17, `3.25` | −25 | The anniversary has no source row. Show one day of lag, the label warning and the instrument change caution. |
| `GB → GB` | — | — | — | — | Series blocked because the two rows dated 2026-11-17 disagree on value and status. |
| `CH → CH` | 2026-11-17, `-0.25` | 2025-11-17 | — | — | Insufficient history. |
| `JP → JP` | — | — | — | — | No daily rows for this request in the fixture. |

## Rebuild and inspect

Run these commands from the repository root with Python available. Rebuild the
ZIP after editing the readable CSV, then check the member name and integrity.

```bash
python -m zipfile -c tests/fixtures/synthetic-cbpol-flat.zip tests/fixtures/WS_CBPOL_csv_flat.csv
python -m zipfile -l tests/fixtures/synthetic-cbpol-flat.zip
python -m zipfile -t tests/fixtures/synthetic-cbpol-flat.zip
```

## SDMX structure fixture

The [synthetic XML](synthetic-cbpol-structure.xml) models the pinned CBPOL
flow with a synthetic DSD and versioned frequency and area codelists. The
different IDs and versions exercise reference resolution. The tests create
incompatible variants in temporary directories. The XML is an offline
fixture for structural metadata. The flat ZIP fixture supplies observations.
[Structure checks](../test_sdmx_metadata.py)
