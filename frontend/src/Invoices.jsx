import { useEffect, useState } from "react";
import { api } from "./api.js";

const rupees = (paise) =>
  (paise / 100).toLocaleString("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 2,
  });

const period = (invoice) =>
  new Date(invoice.period_start).toLocaleDateString("en-IN", {
    month: "short",
    year: "numeric",
  });

function InvoiceDrawer({ id, onClose }) {
  const [invoice, setInvoice] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api.invoice(id).then(setInvoice).catch((err) => setError(err.message));
  }, [id]);

  return (
    <div className="drawer-backdrop" onClick={onClose}>
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <header>
          <div>
            <h2>{invoice?.number || "Invoice"}</h2>
            <div className="muted">
              {invoice ? `${invoice.site_code} · ${period(invoice)}` : ""}
            </div>
          </div>
          <button className="close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </header>

        {error && <div className="error">{error}</div>}
        {!invoice && !error && <p className="muted">Loading...</p>}

        {invoice && (
          <>
            <table>
              <thead>
                <tr>
                  <th>Description</th>
                  <th className="num">Units</th>
                  <th className="num">Amount</th>
                </tr>
              </thead>
              <tbody>
                {invoice.lines.map((line) => (
                  <tr key={line.position}>
                    <td>{line.description}</td>
                    <td className="num">
                      {Number(line.units) ? Number(line.units).toLocaleString("en-IN") : "-"}
                    </td>
                    <td className="num">{rupees(line.amount_paise)}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            <div className="totals">
              <div>
                <span>Subtotal</span>
                <span>{rupees(invoice.subtotal_paise)}</span>
              </div>
              {invoice.adjustments_paise !== 0 && (
                <div>
                  <span>Adjustments</span>
                  <span>{rupees(invoice.adjustments_paise)}</span>
                </div>
              )}
              <div>
                <span>Tax</span>
                <span>{rupees(invoice.tax_paise)}</span>
              </div>
              <div className="grand">
                <span>Total</span>
                <span>{rupees(invoice.total_paise)}</span>
              </div>
            </div>

            <p className="hint">
              Metered {Number(invoice.consumption_units).toLocaleString("en-IN")} units;
              billed {Number(invoice.billed_units).toLocaleString("en-IN")}. Due{" "}
              {new Date(invoice.due_on).toLocaleDateString("en-IN")}.
            </p>
          </>
        )}
      </div>
    </div>
  );
}

export default function Invoices({ siteId }) {
  const [invoices, setInvoices] = useState(null);
  const [error, setError] = useState(null);
  const [open, setOpen] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setInvoices(null);
    api
      .invoices(siteId)
      .then((body) => !cancelled && setInvoices(body.results || []))
      .catch((err) => !cancelled && setError(err.message));
    return () => {
      cancelled = true;
    };
  }, [siteId]);

  if (error) return <div className="error">{error}</div>;

  return (
    <div className="card">
      <h2>Billing history</h2>
      {!invoices ? (
        <p className="muted">Loading invoices...</p>
      ) : invoices.length === 0 ? (
        <p className="muted">
          No invoices yet. They appear here once a billing period has been run.
        </p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Invoice</th>
              <th>Site</th>
              <th>Period</th>
              <th className="num">Billed units</th>
              <th className="num">Total</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {invoices.map((invoice) => (
              <tr
                key={invoice.id}
                className="clickable"
                onClick={() => setOpen(invoice.id)}
              >
                <td>{invoice.number}</td>
                <td>{invoice.site_code}</td>
                <td>{period(invoice)}</td>
                <td className="num">
                  {Number(invoice.billed_units).toLocaleString("en-IN", {
                    maximumFractionDigits: 0,
                  })}
                </td>
                <td className="num">{rupees(invoice.total_paise)}</td>
                <td>
                  <span className={`pill ${invoice.status}`}>{invoice.status}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {open && <InvoiceDrawer id={open} onClose={() => setOpen(null)} />}
    </div>
  );
}
