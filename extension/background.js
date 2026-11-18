// Background service worker: owns Notion OAuth, job submission and background
// polling (badge + completion notification). The popup polls on its own while open.

import * as api from "./lib/api.js";
import { TERMINAL, describeJob } from "./lib/status-text.js";
import { addJob, clearAuth, getAuth, getJobs, getSettings, setAuth, updateJob } from "./lib/storage.js";

const POLL_ALARM = "vn-poll";

// ---------------------------------------------------------------- OAuth ----

async function connectNotion() {
  const { gatewayUrl } = await getSettings();
  const cfg = await api.getOAuthConfig(gatewayUrl);
  if (cfg.network) throw new Error("Can't reach the notes server. Check the server address in Settings.");
  if (!cfg.ok || !cfg.data || !cfg.data.configured) {
    throw new Error("Notion sign-in isn't configured on the server. Use an internal integration token in Settings.");
  }
  const redirectUri = chrome.identity.getRedirectURL(); // https://<extension-id>.chromiumapp.org/
  const state = crypto.randomUUID();
  const url = new URL(cfg.data.authorize_url);
  url.searchParams.set("client_id", cfg.data.client_id);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("owner", "user");
  url.searchParams.set("redirect_uri", redirectUri);
  url.searchParams.set("state", state);

  let finalUrl;
  try {
    finalUrl = await chrome.identity.launchWebAuthFlow({ url: url.toString(), interactive: true });
  } catch (e) {
    throw new Error("Notion sign-in was closed before it finished.");
  }
  const back = new URL(finalUrl);
  if (back.searchParams.get("error")) throw new Error("Notion access was not granted.");
  if (back.searchParams.get("state") !== state) throw new Error("Sign-in response didn't match; please try again.");
  const code = back.searchParams.get("code");
  if (!code) throw new Error("Notion didn't return an authorization code.");

  const ex = await api.exchangeCode(gatewayUrl, code, redirectUri);
  if (!ex.ok || !ex.data || !ex.data.access_token) {
    const msg = ex.data && ex.data.error ? ex.data.error.message : "Couldn't finish connecting to Notion.";
    throw new Error(msg);
  }
  const auth = {
    mode: "oauth",
    accessToken: ex.data.access_token,
    workspaceName: ex.data.workspace_name || "Notion",
    templateId: ex.data.duplicated_template_id || null,
    connectedAt: Date.now(),
  };
  await setAuth(auth);
  await chrome.action.setBadgeText({ text: "" });
  return { workspaceName: auth.workspaceName, templateId: auth.templateId };
}

// ---------------------------------------------------------------- jobs -----

async function submitJob({ videoId, url, title }) {
  const auth = await getAuth();
  if (!auth) {
    await chrome.action.setBadgeBackgroundColor({ color: "#d93025" });
    await chrome.action.setBadgeText({ text: "!" });
    return { ok: false, tone: "error", message: "Connect Notion first: click the Video Notes icon in the toolbar." };
  }
  const settings = await getSettings();
  const parentId = settings.parentId || auth.templateId || undefined;
  const res = await api.submitJob(settings.gatewayUrl, {
    youtube_url: url || `https://www.youtube.com/watch?v=${videoId}`,
    notion_token: auth.accessToken,
    notion_parent_id: parentId,
  });

  const base = { video_id: videoId, title: title || "YouTube video", submittedAt: Date.now() };
  if (res.status === 202 && res.data) {
    const job = {
      ...base,
      key: res.data.job_id,
      job_id: res.data.job_id,
      title: res.data.video_title || base.title,
      duration_sec: res.data.duration_sec,
      status: "QUEUED",
      progress: 0,
    };
    await addJob(job);
    await ensurePolling();
    await refreshBadge();
    return { ok: true, tone: "info", message: "Queued — open the extension to track progress." };
  }
  const error = res.network
    ? { code: "SERVER_UNREACHABLE" }
    : (res.data && res.data.error) || { code: `HTTP_${res.status}` };
  if (error.code === "MISSING_NOTION_TOKEN") await clearAuth();
  const job = { ...base, key: `local-${Date.now()}`, job_id: null, status: "REJECTED", error };
  await addJob(job);
  return { ok: false, tone: "error", message: describeJob(job).detail };
}

async function pollActiveJobs() {
  const { gatewayUrl } = await getSettings();
  const jobs = (await getJobs()).filter((j) => j.job_id && !TERMINAL.has(j.status));
  for (const job of jobs) {
    const res = await api.getJob(gatewayUrl, job.job_id);
    if (res.status === 404) {
      await updateJob(job.key, { status: "FAILED", error: { code: "JOB_NOT_FOUND" } });
      continue;
    }
    if (!res.ok || !res.data) continue;
    const before = job.status;
    const next = await updateJob(job.key, { ...res.data, title: res.data.video_title || job.title });
    if (next && !TERMINAL.has(before) && TERMINAL.has(next.status)) notifyFinished(next);
  }
  await refreshBadge();
  const stillActive = (await getJobs()).some((j) => j.job_id && !TERMINAL.has(j.status));
  if (!stillActive) await chrome.alarms.clear(POLL_ALARM);
}

function notifyFinished(job) {
  const d = describeJob(job);
  chrome.notifications.create(`vn:${job.key}`, {
    type: "basic",
    iconUrl: "icons/icon128.png",
    title: `${d.headline}: ${job.title || "Video notes"}`,
    message: d.detail,
    priority: 0,
  });
}

async function refreshBadge() {
  const jobs = await getJobs();
  const active = jobs.filter((j) => j.job_id && !TERMINAL.has(j.status)).length;
  await chrome.action.setBadgeBackgroundColor({ color: "#3b5bdb" });
  await chrome.action.setBadgeText({ text: active ? String(active) : "" });
}

async function ensurePolling() {
  const existing = await chrome.alarms.get(POLL_ALARM);
  // Chrome's minimum alarm period is 30 s; the popup polls every 2 s while open.
  if (!existing) await chrome.alarms.create(POLL_ALARM, { periodInMinutes: 0.5, delayInMinutes: 0.5 });
}

// ------------------------------------------------------------- wiring ------

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    switch (msg && msg.type) {
      case "SUBMIT_JOB":
        return submitJob(msg);
      case "CONNECT_NOTION":
        return { ok: true, ...(await connectNotion()) };
      case "SAVE_INTERNAL_TOKEN": {
        const token = (msg.token || "").trim();
        if (!token) return { ok: false, message: "Paste a Notion integration token first." };
        await setAuth({ mode: "internal", accessToken: token, workspaceName: "Internal integration",
                        templateId: null, connectedAt: Date.now() });
        await chrome.action.setBadgeText({ text: "" });
        return { ok: true };
      }
      case "DISCONNECT_NOTION":
        await clearAuth();
        return { ok: true };
      case "JOBS_CHANGED":
        await ensurePolling();
        await refreshBadge();
        return { ok: true };
      default:
        return { ok: false, message: "Unknown request" };
    }
  })()
    .then(sendResponse)
    .catch((e) => sendResponse({ ok: false, tone: "error", message: e.message || String(e) }));
  return true; // async response
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === POLL_ALARM) pollActiveJobs();
});

chrome.notifications.onClicked.addListener(async (id) => {
  if (!id.startsWith("vn:")) return;
  const key = id.slice(3);
  const job = (await getJobs()).find((j) => j.key === key);
  const url = job && job.result && job.result.notion_page_url;
  if (url) chrome.tabs.create({ url });
  chrome.notifications.clear(id);
});

chrome.runtime.onStartup.addListener(() => {
  ensurePolling();
  refreshBadge();
});

chrome.runtime.onInstalled.addListener(async (details) => {
  if (details.reason === "install") {
    chrome.runtime.openOptionsPage();
  }
  refreshBadge();
});
