// Thin client for the gateway REST API. Never throws raw JSON at the UI:
// every call resolves to { ok, status, data } or { ok: false, status: 0, network: true }.

async function request(base, path, { method = "GET", body, timeoutMs = 10000 } = {}) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(`${base.replace(/\/+$/, "")}${path}`, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    });
    let data = null;
    try {
      data = await res.json();
    } catch {
      data = null;
    }
    return { ok: res.ok, status: res.status, data };
  } catch (e) {
    return { ok: false, status: 0, network: true, data: null, reason: e && e.name };
  } finally {
    clearTimeout(timer);
  }
}

export const submitJob = (base, payload) =>
  request(base, "/jobs", { method: "POST", body: payload, timeoutMs: 15000 });

export const getJob = (base, jobId) => request(base, `/jobs/${encodeURIComponent(jobId)}`);

export const getResults = (base, jobId) => request(base, `/jobs/${encodeURIComponent(jobId)}/results`);

export const getHealth = (base) => request(base, "/healthz", { timeoutMs: 6000 });

export const getOAuthConfig = (base) => request(base, "/oauth/notion/config");

export const exchangeCode = (base, code, redirectUri) =>
  request(base, "/oauth/notion/exchange", { method: "POST", body: { code, redirect_uri: redirectUri } });
