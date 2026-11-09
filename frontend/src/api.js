const BASE = import.meta.env.VITE_API_BASE || "";
const TOKEN_KEY = "thermaledger.token";

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private browsing; the session simply does not persist */
  }
}

async function request(path, options = {}) {
  const token = getToken();
  const response = await fetch(`${BASE}/api/v1${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Token ${token}` } : {}),
      ...(options.headers || {}),
    },
  });
  if (response.status === 401 || response.status === 403) {
    setToken(null);
    throw new Error("Your session has expired. Please sign in again.");
  }
  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }
  return response.json();
}

export async function login(username, password) {
  const response = await fetch(`${BASE}/api/v1/auth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) throw new Error("Incorrect username or password.");
  const body = await response.json();
  setToken(body.token);
  return body.token;
}

export const api = {
  me: () => request("/me"),
  summary: () => request("/summary"),
  sites: () => request("/sites/"),
  invoices: (siteId) => request(`/invoices/${siteId ? `?site=${siteId}` : ""}`),
  invoice: (id) => request(`/invoices/${id}/`),
  contracts: () => request("/contracts/"),
  consumption: (days, siteId) =>
    request(`/consumption?days=${days}${siteId ? `&site=${siteId}` : ""}`),
};
