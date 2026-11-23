# BIS Policy Rate Monitor

Open the included example using the BIS capture retrieved on 21 November 2026.
[Report](out/report.md) · [Chart](out/policy_rates.png) · [CSV](out/summary.csv) · [JSON](out/summary.json)

The monitor reports each area's latest usable daily policy rate and its
12-month change in basis points. It uses the BIS flat CSV ZIP for observations,
with SDMX area diagnostics and a local React monitor. [Source contract](docs/source-contract.md)

## Environment

With Conda available, run these commands from the repository root.

```bash
conda env create -f environment.yml
conda activate bis-prates
```

This installs Python, Node.js, and all project dependencies for the report,
web monitor, and tests. [Update an existing environment](docs/command-reference.md#existing-environment-and-editor-setup)

## Generate the report

```bash
bis-prates run --out-dir cache/review-report
```

Open `cache/review-report/report.md` with the chart alongside it. The command
writes the Markdown, PNG, CSV, and JSON to that directory, which keeps the
included examples in `out/` available for comparison. It downloads the flat
ZIP when there is no verified capture less than 24 hours old, then builds the
daily transform and report. [Report options and individual stages](docs/command-reference.md#report-and-cache-controls)

## Run the web monitor

```bash
bis-prates web
```

Open `http://127.0.0.1:8000`. The launcher uses the verified cache or prepares
one if needed, downloading from BIS when it needs a ZIP. Change the areas or
chart start and choose **Run report**. Choose **Refresh data** to update the
capture. Press Ctrl+C to stop the server.

The submission includes the built frontend. To rebuild it from source, follow
the [frontend build instructions](docs/command-reference.md#build-the-frontend-from-source).

## Run tests

```bash
python -m pytest -q -rs
```

The full environment runs the offline suite, including the web and SDMX cases.
[Lint and verification](docs/command-reference.md#lint-and-verification)

The [verification walkthrough](docs/command-reference.md#verify-a-fresh-checkout)
covers the CLI, SDMX diagnostics, tests, and the browser from a fresh
checkout. The command reference also covers custom caches and Vite development.

## Source and attribution

Data source: [Bank for International Settlements, Central bank policy rates](https://data.bis.org/topics/CBPOL),
via the [BIS bulk downloads catalogue](https://data.bis.org/bulkdownload).
Use is subject to the [BIS terms](https://data.bis.org/help/legal). This is an
independent interview exercise, not a BIS product.

## AI usage and validation

I used OpenAI Codex (GPT-6.2 Sol) to help plan the work increments and assist in drafting and implementing the
Python CLI, data pipeline, tests and documentation. I directed the scope,
reviewed the work in small phases and challenged the agent regularly along the way - for example assumptions about observation
identity, duplicate handling, missing values and area codes. These reviews and challenges shaped the direction and decisions of the codebase, such as the decision to withhold conflicted series and preserve both the
requested `EA` code and the BIS `XM` code in the results.

Validation included offline tests for missing values, duplicate conflicts,
date selection, exact rate changes and publication failures, plus an independent
read of the BIS ZIP to check the final reports. Comparing the BIS structure
with the flat export exposed a potential risk. The structure required `TIME_FORMAT`,
and this flat export left it blank throughout. Requiring a populated field
would have rejected the capture, so instead the tool checks dates using `FREQ` and records
the discrepancy in the report. [Source contract](docs/source-contract.md) · [Verification evidence](docs/verification.md)
