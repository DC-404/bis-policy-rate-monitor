import {
  CartesianGrid, Line, LineChart, ReferenceDot, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts'
import { areaCodes } from './ReportDetails.jsx'

const colors = ['#086b67', '#3463ab', '#a45020', '#884b93', '#526440', '#263952']
const day = 86_400_000
const monthYear = new Intl.DateTimeFormat('en-GB', { month: 'short', year: 'numeric', timeZone: 'UTC' })
const dayMonth = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' })
const gapLabels = {
  absent_date: 'No observation',
  explicit_missing: 'Reported missing',
  ineligible: 'Ineligible rate',
}

/** Align plottable series by UTC date and retain the API rows for tooltips. */
function chartData(series, records) {
  const byCode = new Map(records.map(row => [row.bis_area, row]))
  const byDate = new Map()
  // A series with no eligible points is explained in the table and has no plotted line.
  const lines = series.filter(item => item.points.some(point => point.rate != null)).map((item, index) => {
    const code = item.bis_area
    for (const point of item.points) {
      if (!byDate.has(point.date)) {
        byDate.set(point.date, { date: point.date, time: Date.parse(point.date), sourcePoints: {} })
      }
      const row = byDate.get(point.date)
      row.sourcePoints[code] = point
      // Convert only the plot coordinate. The tooltip uses the exact string and API gap reason.
      row[code] = point.rate == null ? null : Number(point.rate)
    }
    const area = byCode.get(code)
    return { code, name: area.area_label, codes: areaCodes(area), color: colors[index % colors.length] }
  })
  const rows = [...byDate.values()].sort((a, b) => a.time - b.time)
  return { lines, rows }
}

/** Explain the actual rate or gap at the active date. */
function RateTooltip({ active, payload, lines }) {
  const row = payload?.[0]?.payload
  if (!active || !row) return null
  return (
    <div className="chart-tooltip">
      <time dateTime={row.date}>{row.date}</time>
      <ul>
        {lines.filter(({ code }) => row.sourcePoints[code]).map(({ code, name, color }) => {
          const { rate, gap } = row.sourcePoints[code]
          return (
            <li key={code}>
              <i style={{ backgroundColor: color }} />{name}
              <span>{rate == null ? gapLabels[gap] : `${rate}%`}</span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

/** Plot the selected daily histories with gaps and isolated usable points. */
export default function RateChart({ series, records, start }) {
  const { rows, lines } = chartData(series, records)
  const first = rows[0]?.time
  const last = rows.at(-1)?.time
  // Pad a single observation date so the axis has a visible domain.
  const oneDate = first === last
  const dateFormat = last - first > 120 * day ? monthYear : dayMonth

  return (
    <section className="panel history" aria-labelledby="history-heading">
      <div className="panel-heading">
        <h2 id="history-heading">Daily policy rates</h2>
        <p>Chart start {start} · Calendar dates</p>
      </div>
      {rows.length === 0 ? (
        <p className="chart-empty">No eligible daily rates in this chart window.</p>
      ) : (
        <>
          <div className="chart-frame">
            <ResponsiveContainer width="100%" height="100%" minWidth={0}>
              <LineChart data={rows} margin={{ top: 10, right: 18, bottom: 4, left: 0 }} accessibilityLayer>
                <CartesianGrid vertical={false} stroke="#dce6e8" />
                <XAxis
                  dataKey="time"
                  type="number"
                  scale="time"
                  domain={oneDate ? [first - day / 2, first + day / 2] : [first, last]}
                  ticks={oneDate ? [first] : undefined}
                  tickCount={8}
                  tickFormatter={time => dateFormat.format(time)}
                  minTickGap={22}
                  tickMargin={9}
                  tickLine={false}
                  stroke="#516673"
                  fontSize={12}
                />
                <YAxis
                  width={47}
                  unit="%"
                  domain={['auto', 'auto']}
                  tickLine={false}
                  axisLine={false}
                  stroke="#516673"
                  fontSize={12}
                />
                <Tooltip content={<RateTooltip lines={lines} />} filterNull={false} />
                {lines.map(({ code, name, color }) => (
                  <Line
                    key={code}
                    dataKey={code}
                    name={name}
                    stroke={color}
                    strokeWidth={2.2}
                    dot={false}
                    activeDot={{ r: 4 }}
                    connectNulls={false}
                    isAnimationActive={false}
                  />
                ))}
                {lines.flatMap(({ code, color }) => rows.map((row, index) => {
                  // A valid observation surrounded by gaps needs a point to remain visible.
                  if (row[code] == null || rows[index - 1]?.[code] != null || rows[index + 1]?.[code] != null) {
                    return null
                  }
                  return <ReferenceDot key={`${code}-${row.date}`} x={row.time} y={row[code]} r={3} fill={color} stroke="none" />
                }))}
              </LineChart>
            </ResponsiveContainer>
          </div>
          <ul className="chart-legend" aria-label="Chart legend">
            {lines.map(({ code, name, codes, color }) => (
              <li key={code}><i style={{ backgroundColor: color }} />{name} <span>({codes})</span></li>
            ))}
          </ul>
          <p className="chart-key">
            Lines join consecutive usable days. Absent or unusable rates leave gaps, which can look
            dotted when repeated. Filled circles mark usable rates with no adjacent usable day in
            the chart. Hovering highlights the selected date.
          </p>
        </>
      )}
    </section>
  )
}
