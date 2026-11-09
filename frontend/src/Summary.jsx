import { useEffect, useState } from "react";
import { api } from "./api.js";

export default function Summary() {
  const [summary, setSummary] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api.summary().then(setSummary).catch((err) => setError(err.message));
  }, []);

  if (error) return <div className="error">{error}</div>;
  if (!summary) return <p className="muted">Loading summary...</p>;

  const chain = summary.audit_chain;

  return (
    <>
      <div className="grid">
        <div className="card stat">
          <div className="label">Sites</div>
          <div className="value">{summary.sites}</div>
        </div>
        <div className="card stat">
          <div className="label">Meters</div>
          <div className="value">{summary.meters.active}</div>
          <div className="note">
            {summary.meters.total} total
            {summary.meters.quarantined > 0 &&
              `, ${summary.meters.quarantined} quarantined`}
          </div>
        </div>
        <div className="card stat">
          <div className="label">Readings, last 24h</div>
          <div className="value">{summary.readings_last_24h.toLocaleString("en-IN")}</div>
          <div className="note">
            {summary.latest_reading_at
              ? `latest ${new Date(summary.latest_reading_at).toLocaleString("en-IN")}`
              : "no readings yet"}
          </div>
        </div>
        <div className="card stat">
          <div className="label">Outstanding</div>
          <div className="value">
            {summary.invoices.outstanding_rupees.toLocaleString("en-IN", {
              style: "currency",
              currency: "INR",
              maximumFractionDigits: 0,
            })}
          </div>
          <div className="note">
            {summary.invoices.outstanding_count} of {summary.invoices.total} invoices
          </div>
        </div>
      </div>

      <div className="card" style={{ marginTop: 16 }}>
        <h2>Audit trail</h2>
        <p style={{ margin: 0 }}>
          {chain.intact ? (
            <>
              <span className="pill paid">Intact</span>{" "}
              {chain.events.toLocaleString("en-IN")} events verified end to end.
            </>
          ) : (
            <>
              <span className="pill issued">Broken</span> verification stopped after{" "}
              {chain.events} events.
            </>
          )}
        </p>
        <p className="hint">
          Every invoice issued, replayed or adjusted is appended to a hash chain. Each
          entry is hashed together with the one before it, so an edit to any past entry
          invalidates everything after it and is detected here.
        </p>
      </div>
    </>
  );
}
