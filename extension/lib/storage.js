// chrome.storage.local layout
//   settings: { gatewayUrl, parentId }
//   auth:     { accessToken, workspaceName, workspaceIcon, templateId, mode: "oauth" | "internal" }
//   jobIndex: [jobKey, ...]           newest first, max 20 -- written only by background.js
//   job:<key>: { key, job_id, video_id, title, status, progress, ... }  written by background + popup

export const DEFAULT_SETTINGS = { gatewayUrl: "http://localhost:8000", parentId: "" };
export const MAX_JOBS = 20;

export async function getSettings() {
  const { settings } = await chrome.storage.local.get("settings");
  return { ...DEFAULT_SETTINGS, ...(settings || {}) };
}

export async function saveSettings(patch) {
  const current = await getSettings();
  const next = { ...current, ...patch };
  next.gatewayUrl = (next.gatewayUrl || DEFAULT_SETTINGS.gatewayUrl).replace(/\/+$/, "");
  await chrome.storage.local.set({ settings: next });
  return next;
}

export async function getAuth() {
  const { auth } = await chrome.storage.local.get("auth");
  return auth && auth.accessToken ? auth : null;
}

export async function setAuth(auth) {
  await chrome.storage.local.set({ auth });
}

export async function clearAuth() {
  await chrome.storage.local.remove("auth");
}

const jobKey = (key) => `job:${key}`;

export async function getJobs() {
  const { jobIndex } = await chrome.storage.local.get("jobIndex");
  const keys = (jobIndex || []).map(jobKey);
  if (!keys.length) return [];
  const data = await chrome.storage.local.get(keys);
  return (jobIndex || []).map((k) => data[jobKey(k)]).filter(Boolean);
}

export async function addJob(job) {
  const { jobIndex } = await chrome.storage.local.get("jobIndex");
  const index = [job.key, ...(jobIndex || []).filter((k) => k !== job.key)];
  const dropped = index.slice(MAX_JOBS);
  await chrome.storage.local.set({ jobIndex: index.slice(0, MAX_JOBS), [jobKey(job.key)]: job });
  if (dropped.length) await chrome.storage.local.remove(dropped.map(jobKey));
}

export async function updateJob(key, patch) {
  const k = jobKey(key);
  const data = await chrome.storage.local.get(k);
  if (!data[k]) return null;
  const next = { ...data[k], ...patch };
  await chrome.storage.local.set({ [k]: next });
  return next;
}

export async function removeJob(key) {
  const { jobIndex } = await chrome.storage.local.get("jobIndex");
  await chrome.storage.local.set({ jobIndex: (jobIndex || []).filter((k) => k !== key) });
  await chrome.storage.local.remove(jobKey(key));
}
