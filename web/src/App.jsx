import { lazy, Suspense, useEffect, useState } from 'react'
import { CaptureSource, SnapshotTable, areaCodes, utcTime } from './ReportDetails.jsx'

// Load the chart separately so the report form and table can render first.
const RateChart = lazy(() => import('./RateChart.jsx'))
const defaults = { countries: 'US,EA,GB,JP,CH', start: '2015-01-01' }
const loadingReport = { data: null, error: '', loading: true }
const idleRefresh = { loading: false, source: null, error: '' }

/** Read a JSON response and use the API's error detail when available. */
async function requestApi(path, options) {
  const unavailable = 'Could not reach the data API. Check that FastAPI is running and try again.'
  const response = await fetch(path, options).catch(() => null)
  const body = response && await response.json().catch(() => null)
  if (!response?.ok || !body) {
    throw new Error(typeof body?.detail === 'string' ? body.detail : unavailable)
  }
  return body
}

/** Manage selection, report requests, and refresh for the local monitor. */
export default function App() {
  const [filters, setFilters] = useState(defaults)
  // Keep the last submitted selection separate from edits still in the form.
  const [submitted, setSubmitted] = useState(defaults)
  const [catalogue, setCatalogue] = useState({ data: null, error: '' })
  const [report, setReport] = useState(loadingReport)
  const [refresh, setRefresh] = useState(idleRefresh)
  const busy = report.loading || refresh.loading

  useEffect(() => {
    // Reuse the area list until a successful refresh changes the available capture.
    if (catalogue.data) return
    const controller = new AbortController()
    const { signal } = controller

    requestApi('/api/areas', { signal }).then(({ areas }) => {
      if (!signal.aborted) setCatalogue({ data: areas, error: '' })
    }).catch(error => {
      if (!signal.aborted) setCatalogue({ data: null, error: error.message })
    })

    return () => controller.abort()
  }, [catalogue.data, submitted])

  useEffect(() => {
    // Abort superseded reads so a late response cannot replace a newer selection.
    const controller = new AbortController()
    const { signal } = controller
    const query = new URLSearchParams(submitted)

    requestApi(`/api/report?${query}`, { signal }).then(data => {
      if (!signal.aborted) setReport({ data, error: '', loading: false })
    }).catch(error => {
      if (!signal.aborted) {
        setReport(previous => ({ ...previous, error: error.message, loading: false }))
      }
    })

    return () => controller.abort()
  }, [submitted])

  /** Append the selected area unless either its request code or BIS code is present. */
  function addArea(requested) {
    if (!requested) return
    const area = catalogue.data.find(item => item.requested_area === requested)
    const codes = filters.countries.split(',').map(code => code.trim().toUpperCase()).filter(Boolean)
    // The selection must contain only one spelling for each BIS series.
    if (!codes.includes(requested) && !codes.includes(area.bis_area)) {
      setFilters({ ...filters, countries: [...codes, requested].join(',') })
    }
  }

  /** Submit the current form as a new report selection. */
  function runReport(event) {
    event.preventDefault()
    if (busy) return
    setRefresh(idleRefresh)
    setReport(loadingReport)
    setSubmitted({ ...filters })
  }

  /** Refresh the flat capture and reload the last report after successful publication. */
  async function refreshData() {
    if (busy) return
    setRefresh({ ...idleRefresh, loading: true })
    try {
      const { source } = await requestApi('/api/refresh', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
      })
      setRefresh({ ...idleRefresh, source })
      // Keep the last result and retrieval time visible until the replacement arrives.
      setReport(previous => ({ ...previous, error: '', loading: true }))
      setCatalogue({ data: null, error: '' })
      // Reload the last submitted selection while keeping any edits in the form.
      setSubmitted({ ...submitted })
    } catch (error) {
      setRefresh({ ...idleRefresh, error: `Refresh failed. ${error.message}` })
    }
  }

  // Distinguish download/transform failure from failure to read an already refreshed report.
  const reportError = report.error && refresh.source
    ? `Data refreshed. The report failed to reload. ${report.error}`
    : report.error
  const error = refresh.loading ? '' : refresh.error || reportError
  const previousCapture = report.data?.source.retrieved_at

  return (
    <>
      <header className="masthead">
        <div className="shell">
          <h1>BIS Policy Rate Monitor</h1>
          <p>Daily central bank policy rates and 12-month changes</p>
        </div>
      </header>
      <main className="shell">
        <form className="panel filters" aria-label="Report filters" onSubmit={runReport}>
          <label>
            <span>Area codes</span>
            <input
              required
              aria-describedby="area-hint"
              value={filters.countries}
              onChange={event => setFilters({ ...filters, countries: event.target.value })}
              autoComplete="off"
              spellCheck={false}
            />
          </label>
          <label>
            <span>Add captured area</span>
            <select
              value=""
              disabled={!catalogue.data?.length}
              onChange={event => addArea(event.target.value)}
            >
              <option value="">
                {catalogue.error ? 'Area list unavailable' : catalogue.data ? 'Choose an area' : 'Loading areas…'}
              </option>
              {catalogue.data?.map(area => (
                <option key={area.bis_area} value={area.requested_area}>
                  {area.area_label} ({areaCodes(area)}){area.blocked ? ' · Withheld' : ''}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Chart start</span>
            <input
              type="date"
              required
              value={filters.start}
              onChange={event => setFilters({ ...filters, start: event.target.value })}
            />
          </label>
          <div className="filter-actions">
            <button type="submit" disabled={busy}>Run report</button>
            <button type="button" className="secondary" disabled={busy} onClick={refreshData}>
              Refresh data
            </button>
          </div>
          <p id="area-hint" className="hint">
            Separate area codes with commas. Refresh data reruns the last submitted report.
          </p>
        </form>

        {refresh.loading && <p className="feedback" role="status">Downloading and validating BIS data…</p>}
        {report.loading && (
          <p className="feedback" role="status">
            {refresh.source ? 'Data refreshed. Reloading the report…' : 'Loading policy rates…'}
          </p>
        )}
        {!busy && refresh.source && !error && (
          <p className="feedback" role="status">
            Data refreshed. ZIP retrieved {utcTime(previousCapture)}.
          </p>
        )}
        {error && (
          <p className="feedback error" role="alert">
            {error}
            {previousCapture && (
              <> Showing the previous result from the ZIP retrieved {utcTime(previousCapture)}.</>
            )}
          </p>
        )}
        {report.data && (
          <div className="results">
            <SnapshotTable records={report.data.summary} />
            <Suspense fallback={<p className="feedback" role="status">Loading chart…</p>}>
              <RateChart series={report.data.series} records={report.data.summary} start={submitted.start} />
            </Suspense>
            <CaptureSource report={report.data} />
          </div>
        )}
      </main>
    </>
  )
}
