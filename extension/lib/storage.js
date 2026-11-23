// chrome.storage.local layout
//   settings: { gatewayUrl, parentId, noteOptions: { detail, max_frames, study_aids, include_transcript, language } }
//   auth:     { accessToken, workspaceName, workspaceIcon, templateId, mode: "oauth" | "internal" }
//   jobIndex: [jobKey, ...]           newest first, max 200 -- written by background.js (and removals by UI pages)
//   job:<key>: { key, job_id, video_id, title, status, progress, ... }  written by background + popup

// Per-job preferences sent as `options` with every POST /jobs. null = server default.
export const DEFAULT_NOTE_OPTIONS = {
  detail: "standard", // brief | standard | detailed
  max_frames: null, // 0-12, null = server default (MAX_FRAMES)
  study_aids: true, // key takeaways, glossary, review questions
  include_transcript: true, // collapsible full transcript on the page
  language: "", // "" = same language as the video
};
export const DEFAULT_SETTINGS = { gatewayUrl: "http://localhost:8000", parentId: "", noteOptions: DEFAULT_NOTE_OPTIONS };
export const MAX_JOBS = 200; // the history page shows them all; the popup the newest few

export async function getSettings() {
  const { settings } = await chrome.storage.local.get("settings");
  const merged = { ...DEFAULT_SETTINGS, ...(settings || {}) };
  merged.noteOptions = { ...DEFAULT_NOTE_OPTIONS, ...((settings && settings.noteOptions) || {}) };
  return merged;
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

// The options object for POST /jobs: drop "server default" values.
export function jobOptionsPayload(noteOptions) {
  const o = { ...DEFAULT_NOTE_OPTIONS, ...(noteOptions || {}) };
  const out = { detail: o.detail, study_aids: !!o.study_aids, include_transcript: !!o.include_transcript };
  if (o.max_frames !== null && o.max_frames !== "" && o.max_frames !== undefined) out.max_frames = Number(o.max_frames);
  if (o.language) out.language = o.language;
  if (o.fresh) out.fresh = true; // one-off: skip the server's result cache
  return out;
}

export async function clearFinished(terminal) {
  const jobs = await getJobs();
  const gone = jobs.filter((j) => terminal.has(j.status)).map((j) => j.key);
  if (!gone.length) return 0;
  const { jobIndex } = await chrome.storage.local.get("jobIndex");
  await chrome.storage.local.set({ jobIndex: (jobIndex || []).filter((k) => !gone.includes(k)) });
  await chrome.storage.local.remove(gone.map(jobKey));
  return gone.length;
}

export async function removeJob(key) {
  const { jobIndex } = await chrome.storage.local.get("jobIndex");
  await chrome.storage.local.set({ jobIndex: (jobIndex || []).filter((k) => k !== key) });
  await chrome.storage.local.remove(jobKey(key));
}
