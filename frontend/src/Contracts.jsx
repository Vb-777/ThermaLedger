import { useEffect, useState } from "react";
import { api } from "./api.js";

export default function Contracts({ siteId }) {
  const [contracts, setContracts] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api
      .contracts()
      .then((body) => setContracts(body.results || []))
      .catch((err) => setError(err.message));
  }, []);

  if (error) return <div className="error">{error}</div>;
  if (!contracts) return <p className="muted">Loading contract terms...</p>;

  const shown = siteId ? contracts.filter((c) => c.site === siteId) : contracts;

  if (shown.length === 0) {
    return (
      <div className="card">
        <p className="muted">No contracts on file for this selection.</p>
      </div>
    );
  }

  return (
    <div className="grid" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))" }}>
      {shown.map((contract) => (
        <div className="card" key={contract.id}>
          <h2>{contract.reference}</h2>
          <p className="muted" style={{ marginTop: -8 }}>
            {contract.site_code} · {contract.tariff_name}
          </p>

          <table>
            <tbody>
              <tr>
                <td>Service</td>
                <td className="num">{contract.service}</td>
              </tr>
              <tr>
                <td>Started</td>
                <td className="num">
                  {new Date(contract.starts_on).toLocaleDateString("en-IN")}
                </td>
              </tr>
              <tr>
                <td>Ends</td>
                <td className="num">
                  {contract.ends_on
                    ? new Date(contract.ends_on).toLocaleDateString("en-IN")
                    : "Open-ended"}
                </td>
              </tr>
              <tr>
                <td>Payment terms</td>
                <td className="num">{contract.payment_terms_days} days</td>
              </tr>
              <tr>
                <td>Minimum offtake</td>
                <td className="num">
                  {Number(contract.minimum_offtake_units).toLocaleString("en-IN")} units
                </td>
              </tr>
            </tbody>
          </table>

          <h2 style={{ marginTop: 18 }}>Tariff bands</h2>
          <table>
            <thead>
              <tr>
                <th>Band</th>
                <th className="num">Rate per unit</th>
              </tr>
            </thead>
            <tbody>
              {contract.slabs.map((slab, index) => (
                <tr key={index}>
                  <td>
                    {slab.upto_units
                      ? `Up to ${Number(slab.upto_units).toLocaleString("en-IN")} units`
                      : "Above the last band"}
                  </td>
                  <td className="num">
                    {slab.rate_rupees_per_unit.toLocaleString("en-IN", {
                      style: "currency",
                      currency: "INR",
                    })}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <p className="hint">
            Consumption below the minimum offtake is billed at the minimum. Bands are
            cumulative: each band is priced at its own rate, not the whole volume at the
            top rate.
          </p>
        </div>
      ))}
    </div>
  );
}
