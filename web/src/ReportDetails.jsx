const statuses = {
  ok: 'Available',
  unavailable: 'Unavailable',
  no_valid_latest: 'No eligible rate',
  insufficient_history: 'Limited history',
  series_blocked: 'Withheld',
  incompatible_units: 'Withheld',
}

/** Display both area codes when the request uses an alias. */
export function areaCodes(area) {
  const { requested_area: requested, bis_area: bis } = area
  return requested === bis ? bis : `${requested} / ${bis}`
}

/** Show the API's exact rate string and the date selected by the calculation. */
function Rate({ value, date }) {
  if (value == null) return <span className="muted" aria-label="Unavailable">—</span>
  return (
    <>
      <span className="rate">{value}%</span>
      <time dateTime={date}>{date}</time>
    </>
  )
}

/** Explain the API outcome, fallback dates, and source warnings for one row. */
function SnapshotStatus({ row }) {
  // Available rates can still need a codelist discrepancy, fallback or staleness note.
  const showDetail = row.status !== 'ok' || row.area_code_status === 'codelist_mismatch'
  return (
    <>
      <span className={`status status-${row.status}`}>{statuses[row.status] || row.status}</span>
      {showDetail && <p>{row.status_detail}</p>}
      {row.latest_reason && row.latest_rate != null && <p>Showing the latest usable rate.</p>}
      {row.historical_reason && row.historical_rate != null && (
        <p>Comparison uses a usable rate before the anniversary.</p>
      )}
      {row.latest_is_stale && <p>Latest rate was {row.latest_age_days} days old at ZIP retrieval.</p>}
    </>
  )
}

/** Render snapshot records shared with the CLI report. */
export function SnapshotTable({ records }) {
  return (
    <section className="panel" aria-labelledby="snapshot-heading">
      <div className="panel-heading">
        <h2 id="snapshot-heading">Latest rates and 12-month changes</h2>
        <p>Rates in per cent per year. Dates show the observations used.</p>
      </div>
      <div className="table-scroll" tabIndex={0} role="region" aria-labelledby="snapshot-heading">
        <table>
          <thead>
            <tr>
              <th scope="col">Area</th>
              <th scope="col">Latest rate</th>
              <th scope="col">12-month rate</th>
              <th scope="col">Change (bp)</th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {records.map(row => (
              <tr key={row.bis_area}>
                <th scope="row">
                  {row.area_label}
                  {row.area_label !== areaCodes(row) && <small>{areaCodes(row)}</small>}
                </th>
                <td><Rate value={row.latest_rate} date={row.latest_date} /></td>
                <td><Rate value={row.historical_rate} date={row.historical_date} /></td>
                <td className="change">
                  {row.change_bp == null
                    ? <span className="muted" aria-label="Unavailable">—</span>
                    : `${Number(row.change_bp) > 0 ? '+' : ''}${row.change_bp}`}
                </td>
                <td className="status-cell"><SnapshotStatus row={row} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

/** Render capture timestamps in UTC for consistent provenance across browsers. */
export function utcTime(iso) {
  return `${new Date(iso).toISOString().slice(0, 16).replace('T', ' ')} UTC`
}

/** Show the ZIP provenance, capture notes, and optional SDMX metadata. */
export function CaptureSource({ report }) {
  const { source, summary, quality_notes: notes } = report
  // The verified SDMX capture is shared across rows and can be omitted in flat-only reports.
  const metadata = summary.find(row => row.sdmx_source_sha256)
  return (
    <section className="panel source" aria-labelledby="source-heading">
      <h2 id="source-heading">Source and data quality</h2>
      <p>
        <a href={source.archive_url}>Bank for International Settlements flat ZIP</a>
        {source.catalogue_release_date && ` · Release ${source.catalogue_release_date}`}
        {' · Retrieved '}
        <time dateTime={source.retrieved_at}>{utcTime(source.retrieved_at)}</time>
      </p>
      {notes.length > 0 && <ul>{notes.map(note => <li key={note}>{note}</li>)}</ul>}
      <details>
        <summary>Capture checksums and metadata</summary>
        <dl>
          <dt>ZIP SHA-256</dt><dd><code>{source.sha256}</code></dd>
          {metadata && (
            <>
              <dt>SDMX area codelist</dt>
              <dd><a href={metadata.sdmx_source_url}>{metadata.sdmx_area_codelist}</a></dd>
              <dt>XML retrieved</dt>
              <dd><time dateTime={metadata.sdmx_source_retrieved_at}>{utcTime(metadata.sdmx_source_retrieved_at)}</time></dd>
              <dt>XML SHA-256</dt><dd><code>{metadata.sdmx_source_sha256}</code></dd>
            </>
          )}
        </dl>
      </details>
    </section>
  )
}
