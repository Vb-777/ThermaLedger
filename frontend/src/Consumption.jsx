import { useEffect, useMemo, useState } from "react";
import {
  BarElement,
  CategoryScale,
  Chart as ChartJS,
  Legend,
  LinearScale,
  Tooltip,
} from "chart.js";
import { Bar } from "react-chartjs-2";
import { api } from "./api.js";

ChartJS.register(CategoryScale, LinearScale, BarElement, Tooltip, Legend);

const RANGES = [
  { days: 7, label: "7 days" },
  { days: 30, label: "30 days" },
  { days: 90, label: "90 days" },
];

export default function Consumption({ siteId }) {
  const [days, setDays] = useState(30);
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    api
      .consumption(days, siteId)
      .then((body) => !cancelled && setData(body))
      .catch((err) => !cancelled && setError(err.message));
    return () => {
      cancelled = true;
    };
  }, [days, siteId]);

  const chart = useMemo(() => {
    if (!data) return null;
    return {
      labels: data.points.map((p) =>
        new Date(p.bucket).toLocaleDateString("en-IN", { day: "2-digit", month: "short" })
      ),
      datasets: [
        {
          label: "Consumption",
          data: data.points.map((p) => Number(p.units)),
          backgroundColor: "#0d6b57",
          borderRadius: 3,
          maxBarThickness: 28,
        },
      ],
    };
  }, [data]);

  const total = data ? data.points.reduce((sum, p) => sum + Number(p.units), 0) : 0;
  const peak = data && data.points.length
    ? Math.max(...data.points.map((p) => Number(p.units)))
    : 0;
  const average = data && data.points.length ? total / data.points.length : 0;

  if (error) return <div className="error">{error}</div>;

  return (
    <>
      <div className="grid" style={{ marginBottom: 16 }}>
        <div className="card stat">
          <div className="label">Total, last {days} days</div>
          <div className="value">{total.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</div>
          <div className="note">units</div>
        </div>
        <div className="card stat">
          <div className="label">Daily average</div>
          <div className="value">{average.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</div>
          <div className="note">units per day</div>
        </div>
        <div className="card stat">
          <div className="label">Peak day</div>
          <div className="value">{peak.toLocaleString("en-IN", { maximumFractionDigits: 0 })}</div>
          <div className="note">units</div>
        </div>
      </div>

      <div className="card">
        <div className="toolbar">
          <h2 style={{ margin: 0, flex: 1 }}>Metered consumption</h2>
          {RANGES.map((range) => (
            <button
              key={range.days}
              onClick={() => setDays(range.days)}
              aria-selected={days === range.days}
              style={{
                border: "1px solid var(--line)",
                background: days === range.days ? "var(--accent-soft)" : "var(--card)",
                color: days === range.days ? "var(--accent)" : "var(--ink-soft)",
                borderRadius: 999,
                padding: "5px 12px",
                cursor: "pointer",
                font: "inherit",
                fontSize: 13,
              }}
            >
              {range.label}
            </button>
          ))}
        </div>

        {!data ? (
          <p className="muted">Loading readings...</p>
        ) : data.points.length === 0 ? (
          <p className="muted">No readings in this period.</p>
        ) : (
          <div className="chart-wrap">
            <Bar
              data={chart}
              options={{
                maintainAspectRatio: false,
                plugins: { legend: { display: false } },
                scales: {
                  x: { grid: { display: false } },
                  y: { beginAtZero: true, grid: { color: "#eef2f1" } },
                },
              }}
            />
          </div>
        )}
        <p className="hint">
          Each bar is the rise in the meter register across that day, not the register
          value. Readings are cumulative, so a missed transmission costs resolution,
          not billed units.
        </p>
      </div>
    </>
  );
}
