# Command reference

The [quick start](../README.md) covers the standard Conda setup, report,
web monitor, and tests. Run the following commands from the repository root
with `bis-prates` activated unless the command creates or updates the environment.

## Verify a fresh checkout

Run from the root of the cloned repository. If `bis-prates` already exists,
use the environment update command in the next section before activating it.

```bash
conda env create -f environment.yml
conda activate bis-prates
python -m pip check
python -m pytest -q -rs
python -m ruff check .
bis-prates run --out-dir cache/walkthrough-report
```

All 116 tests should pass with zero skips in the full environment. Open
`cache/walkthrough-report/report.md` in Markdown preview and check the chart
alongside it. The command writes four files to that directory and records the
current BIS provenance. The included [sample report](../out/report.md) uses
the fixed capture retrieved on 21 November 2026.

Retrieve the pinned structure, inspect the verified cache, and generate a
report that shows the area diagnostics.

```bash
bis-prates metadata --refresh
bis-prates metadata
bis-prates report --countries EA,AF,ZZ --out-dir cache/walkthrough-sdmx
```

Open `cache/walkthrough-sdmx/report.md`. In the verified example `EA` maps to
`XM`, `AF` is defined with no daily rows, and `ZZ` is unknown to the
codelist. The flat ZIP and SDMX XML have separate source checksums.

Start the included web bundle in the same terminal.

```bash
bis-prates web
```

Open `http://127.0.0.1:8000/`. Enter `EA,GB` and chart start
`2026-01-01`, then choose **Run report**. Check the requested and BIS area
codes, both observation dates, the chart window, and the source timestamp.
The chart window limits display. The snapshot can still use observations
before January 2026. Choose **Refresh data** and wait for the retrieval
time to update. The archive checksum can stay the same when BIS returns
the same ZIP. Press Ctrl+C to stop the server.

The ordered steps share the default cache. The tests and included sample
outputs can be reviewed offline once the dependencies are installed. Live
ZIP and XML retrieval and browser refresh require access to BIS.

## Existing environment and editor setup

Update an existing environment before activating it.

```bash
conda env update --name bis-prates --file environment.yml
conda activate bis-prates
```

The environment definition installs Python and Node.js, and makes an
editable install of the project with the `dev`, `sdmx`, and `web` extras.
Select the active interpreter in your editor to discover tests and import
the package.

```bash
python -c 'import sys; print(sys.executable)'
python -c 'import bis_policy_rate_monitor; print(bis_policy_rate_monitor.__file__)'
```

The package import should resolve to `src/bis_policy_rate_monitor/` in this
checkout. If the IDE uses a different environment, select the interpreter
above. Use `python -m pytest -q -rs` to display skip reasons. A Python setup
without FastAPI skips the API tests, so use the full Conda environment to
verify all shipped paths.

## Report and cache controls

```bash
bis-prates run --countries EA,US --start 2026-11-16 \
  --cache-dir cache/custom --out-dir cache/custom-report
```

`--countries` accepts comma-separated area codes. `EA` resolves to BIS code
`XM`, and both appear in the report. `--start` limits the chart window.
The snapshot can use earlier observations for its 12-month comparison.
`--out-dir` defaults to `out/`. Use a separate directory to preserve the
included examples.

Use the stage commands to examine their outputs or regenerate the report
from the already verified transform.

```bash
bis-prates fetch
bis-prates transform
bis-prates report --countries US,EA,GB,JP,CH --start 2015-01-01
```

Use the same `--cache-dir` for each stage if you select a non-default cache.
`fetch` reuses a verified ZIP retrieved less than 24 hours ago. A second
fetch within that period shows `Reused` with the same retrieval time and
SHA-256. Both `fetch` and `run` accept `--refresh` to force retrieval.
`transform` publishes the daily and metadata CSVs. `report` reads the verified
local files and reports an actionable error if the transform is missing or
belongs to a different ZIP.

```bash
python -m json.tool cache/fetch-manifest.json
python -m json.tool cache/transform-manifest.json
```

The transform manifest records checksums, the source ZIP and data quality
findings. Use `source_record` to locate the original CSV record, counting
from 1 after the header. Use `metadata_id` to join the derived observation
to its attributes. [Transform manifest](../src/bis_policy_rate_monitor/derived.py) · [Sample reconciliation](verification.md#included-example)

### Report files

| File | Contents |
| --- | --- |
| `summary.csv` and `summary.json` | One record per requested area with both area codes, status, actual dates, rates, change in basis points, metadata, and ZIP provenance. Exact rates and changes are decimal strings. Unavailable values are CSV blanks and JSON `null`. |
| `policy_rates.png` | Daily histories with gaps, a chart key, and labels for withheld or unavailable series. PNG metadata carries the source URL, retrieval time, digest, and latest observation dates. |
| `report.md` | Rate table, chart, calculation method, data quality findings, and source. Open in Markdown Preview with the PNG alongside it. |

[Detailed summary fields](../src/bis_policy_rate_monitor/summary.py)

## Optional SDMX area diagnostics

With a verified ZIP and transform available, retrieve and inspect the
pinned BIS CBPOL structure, then regenerate the selected report.

```bash
bis-prates metadata --refresh
bis-prates metadata
bis-prates report --countries EA,AF,ZZ --out-dir cache/sdmx-example
```

The `metadata` command displays versioned structure identifiers,
retrieval time, and SHA-256. Use the same `--cache-dir` as the report.
The active manifest is `cache/metadata/metadata-manifest.json` in the
default cache. A failed metadata refresh preserves the previous verified
metadata. Reports can use the flat capture alone.

SDMX distinguishes a code defined by BIS from a daily series available
in the flat capture. In the verified example, `EA` resolves to the usable
`XM` series, `AF` is defined with no daily rows, and `ZZ` is unknown in the
codelist. CSV and JSON add `area_code_status`, `has_daily_series`,
`area_suggestion`, and `sdmx_*` provenance when the XML verifies.
[SDMX verification and recovery](verification.md#sdmx-area-diagnostics)

## Web monitor controls

Use a different cache and port to keep a web session separate from your
terminal reviews. The launcher prepares an empty cache if needed.

```bash
bis-prates web --cache-dir cache/web-review --port 8001
```

Use `--port` to choose a different localhost port. The built UI and API
share one FastAPI process. Startup checks the ZIP and transform and prepares
missing artifacts. The chart start controls display, and the comparison keeps
its 12-month lookup. Hover over or focus the chart to inspect a daily point.
[Launcher implementation](../src/bis_policy_rate_monitor/cli.py)

**Refresh data** downloads and validates a new BIS ZIP, rebuilds its
transform, and reloads the last submitted report. A failed refresh leaves
any displayed result visible with its retrieval time. A live verification
took about 44 seconds. The SDMX XML has its own refresh action in the CLI.
[Refresh verification and failure recovery](verification.md#web-monitor-and-refresh)

To update the flat capture in the terminal, run both commands and reload the
page after the transform completes.

```bash
bis-prates fetch --refresh
bis-prates transform
```

Use the same cache directory in both commands and the launcher.
Finish CLI writes to the same cache before starting a browser refresh.

### Build the frontend from source

The launcher serves `web/dist/`. Build it once when using a source checkout
without the bundle, and rebuild after frontend changes.

```bash
npm --prefix web ci
npm --prefix web run build
```

### Vite development workflow

Prepare a local cache with `bis-prates fetch` and `bis-prates transform`.
With `bis-prates` activated in both terminals, start the API in one terminal
and Vite in the other.

```bash
python -m uvicorn bis_policy_rate_monitor.api:app --host 127.0.0.1 --port 8000
```

```bash
npm --prefix web run dev
```

Open `http://127.0.0.1:5173`. Vite forwards `/api` to FastAPI on port 8000.
For a custom cache, set `BIS_PRATES_CACHE_DIR` before launching the API
process. Use one Uvicorn worker when testing refresh.

### API requests

These requests use the default API port from the launcher or the API-only
process.

```bash
curl -sS http://127.0.0.1:8000/api/areas
curl -sS 'http://127.0.0.1:8000/api/report?countries=US,EA&start=2025-01-01'
```

The report returns the CLI's snapshot records, dated chart points with
explicit gaps, and capture provenance. To explicitly refresh the configured
cache, use the write endpoint.

```bash
curl -sS --json '{}' http://127.0.0.1:8000/api/refresh
```

[Response shapes and errors](../src/bis_policy_rate_monitor/api.py)

## Lint and verification

```bash
python -m ruff check .
```

The offline suite uses invented observations and a mocked HTTP transport.
The real-data reconciliation independently checked all five included
results, their observation dates, and the provenance in the four files.
[Report reconciliation and testing](verification.md)
