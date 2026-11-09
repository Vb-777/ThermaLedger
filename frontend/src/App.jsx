import { useCallback, useEffect, useState } from "react";
import { api, getToken, login, setToken } from "./api.js";
import Consumption from "./Consumption.jsx";
import Invoices from "./Invoices.jsx";
import Contracts from "./Contracts.jsx";
import Summary from "./Summary.jsx";

function Login({ onSignedIn }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(username, password);
      onSignedIn();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <div className="card">
        <div className="brand" style={{ marginBottom: 6 }}>
          <h1>ThermaLedger</h1>
        </div>
        <p className="muted" style={{ marginTop: 0 }}>
          Customer portal - consumption, billing and contract terms.
        </p>
        <form onSubmit={submit}>
          <label htmlFor="username">Username</label>
          <input
            id="username"
            value={username}
            autoComplete="username"
            onChange={(e) => setUsername(e.target.value)}
            required
          />
          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            value={password}
            autoComplete="current-password"
            onChange={(e) => setPassword(e.target.value)}
            required
          />
          {error && (
            <div className="error" style={{ marginTop: 16 }}>
              {error}
            </div>
          )}
          <button className="primary" type="submit" disabled={busy}>
            {busy ? "Signing in..." : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}

const TABS = [
  { id: "consumption", label: "Consumption" },
  { id: "invoices", label: "Billing history" },
  { id: "contracts", label: "Contract terms" },
  { id: "summary", label: "Account summary" },
];

export default function App() {
  const [authed, setAuthed] = useState(Boolean(getToken()));
  const [me, setMe] = useState(null);
  const [sites, setSites] = useState([]);
  const [siteId, setSiteId] = useState("");
  const [tab, setTab] = useState("consumption");
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const [profile, siteList] = await Promise.all([api.me(), api.sites()]);
      setMe(profile);
      setSites(siteList.results || []);
      setError(null);
    } catch (err) {
      setError(err.message);
      if (/session has expired/i.test(err.message)) setAuthed(false);
    }
  }, []);

  useEffect(() => {
    if (authed) load();
  }, [authed, load]);

  function signOut() {
    setToken(null);
    setAuthed(false);
    setMe(null);
  }

  if (!authed) {
    return <Login onSignedIn={() => setAuthed(true)} />;
  }

  return (
    <div className="app">
      <div className="topbar">
        <div className="brand">
          <h1>ThermaLedger</h1>
          <span>Customer portal</span>
        </div>
        <div className="whoami">
          <strong>{me?.tenant_name || "Loading..."}</strong>
          {me?.username} ·{" "}
          <button
            onClick={signOut}
            style={{
              border: "none",
              background: "none",
              padding: 0,
              font: "inherit",
              color: "var(--accent)",
              cursor: "pointer",
            }}
          >
            Sign out
          </button>
        </div>
      </div>

      {error && <div className="error" style={{ marginBottom: 16 }}>{error}</div>}

      <div className="toolbar">
        <label htmlFor="site" className="muted">
          Site
        </label>
        <select id="site" value={siteId} onChange={(e) => setSiteId(e.target.value)}>
          <option value="">All sites</option>
          {sites.map((site) => (
            <option key={site.id} value={site.id}>
              {site.code} - {site.name}
            </option>
          ))}
        </select>
      </div>

      <div className="tabs" role="tablist">
        {TABS.map((item) => (
          <button
            key={item.id}
            role="tab"
            aria-selected={tab === item.id}
            onClick={() => setTab(item.id)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {tab === "consumption" && <Consumption siteId={siteId} />}
      {tab === "invoices" && <Invoices siteId={siteId} />}
      {tab === "contracts" && <Contracts siteId={siteId} />}
      {tab === "summary" && <Summary />}
    </div>
  );
}
