// Popup: a "take notes on this tab" card, then job cards from chrome.storage.
// Polls GET /jobs/{id} every 2 s for unfinished jobs while open. Never shows raw JSON.

import * as api from "../lib/api.js";
import {
  STEPS,
  TERMINAL,
  canRetry,
  describeJob,
  elapsedText,
  fmtTs,
  needsReconnect,
  stepIndex,
} from "../lib/status-text.js";
import { clearFinished, getAuth, getJobs, getSettings, removeJob, saveSettings, updateJob } from "../lib/storage.js";
import { thumbUrl, videoIdFromUrl } from "../lib/youtube.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 2000;
const HEALTH_MS = 10000;
const POPUP_LIMIT = 6; // newest jobs shown here; the history page has the rest
const SHORT = { QUEUED: "Queued", TRANSCRIBING: "Transcript", SUMMARIZING: "Summary",
                EXTRACTING_FRAMES: "Frames", PUBLISHING: "Publish" };
let jobs = [];
let serverDown = false;
let auth = null;
let currentTab = null; // { videoId, title }
let detail = "standard";
const cardCache = new Map(); // job key -> { sig, node }: rebuild a card only when what it shows changes

function timeAgo(ts) {
  if (!ts) return "";
  const s = Math.round((Date.now() - ts) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(ts).toLocaleDateString();
}

function send(type, extra = {}) {
  return new Promise((resolve) => chrome.runtime.sendMessage({ type, ...extra }, (r) => resolve(r || {})));
}

function button(label, cls, onClick, title) {
  const b = document.createElement("button");
  b.className = cls;
  b.textContent = label;
  if (title) b.title = title;
  b.addEventListener("click", onClick);
  return b;
}

// ------------------------------------------------------------------ cards --

function renderCard(job) {
  const node = $("job-tpl").content.firstElementChild.cloneNode(true);
  const d = describeJob(job);
  node.dataset.tone = d.tone;

  const thumb = node.querySelector(".thumb");
  const wrap = node.querySelector(".thumb-wrap");
  if (job.video_id) {
    thumb.src = thumbUrl(job.video_id);
    wrap.href = `https://www.youtube.com/watch?v=${job.video_id}`;
  } else wrap.remove();
  if (job.duration_sec) {
    const dur = node.querySelector(".dur");
    dur.textContent = fmtTs(job.duration_sec);
    dur.hidden = false;
  }
  node.querySelector(".title").textContent = job.title || job.video_title || "YouTube video";
  const sub = [timeAgo(job.submittedAt)];
  const o = job.options || {};
  if (o.detail && o.detail !== "standard") sub.push(o.detail[0].toUpperCase() + o.detail.slice(1));
  if (o.language) sub.push(o.language);
  node.querySelector(".sub").textContent = sub.filter(Boolean).join(" · ");

  // Stepper: Queued → Transcript → Summary → Frames → Publish → Done
  const stepper = node.querySelector(".stepper");
  const terminal = TERMINAL.has(job.status);
  const failed = job.status === "FAILED" || job.status === "DEAD_LETTERED" || job.status === "REJECTED";
  const failedAt = failed && job.error && job.error.stage
    ? stepIndex({ TRANSCRIBE: "TRANSCRIBING", SUMMARIZE: "SUMMARIZING", EXTRACT_FRAMES: "EXTRACTING_FRAMES",
                  PUBLISH: "PUBLISHING" }[job.error.stage])
    : failed ? 0 : -1;
  const cur = terminal && !failed ? STEPS.length : stepIndex(job.status);
  STEPS.forEach((step, i) => {
    const li = document.createElement("li");
    li.textContent = SHORT[step.status] || step.label;
    li.title = step.label;
    if (failed) li.className = i < failedAt ? "done" : i === failedAt ? "failed" : "";
    else if (i < cur) li.className = "done";
    else if (i === cur) li.className = d.tone === "waiting" ? "waiting" : "active";
    stepper.appendChild(li);
  });
  const doneLi = document.createElement("li");
  doneLi.textContent = "Done";
  if (terminal && !failed) doneLi.className = job.status === "COMPLETED" ? "done" : "done warn";
  stepper.appendChild(doneLi);

  node.querySelector(".badge").style.setProperty("--pct", `${Math.max(8, job.progress || 0)}%`);
  node.querySelector(".headline").textContent = d.headline;
  node.querySelector(".detail").textContent = d.detail;
  node.querySelector(".elapsed").textContent = job.job_id ? elapsedText(job) : "";

  const actions = node.querySelector(".actions");
  const url = job.result && job.result.notion_page_url;
  if (url) actions.appendChild(button("View in Notion", "primary", () => chrome.tabs.create({ url })));
  if (url) {
    actions.appendChild(button("Copy link", "ghost", async (e) => {
      await navigator.clipboard.writeText(url).catch(() => {});
      e.target.textContent = "Copied ✓";
      setTimeout(() => (e.target.textContent = "Copy link"), 1500);
    }, "Copy the Notion page link"));
  }
  if (canRetry(job)) {
    actions.appendChild(button("Try again", "secondary", async (e) => {
      e.target.disabled = true;
      e.target.textContent = "Submitting…";
      const r = await send("RETRY_JOB", { key: job.key });
      if (r.ok) await removeJob(job.key);
      await load();
    }, "Submit this video again"));
  }
  if (needsReconnect(job)) actions.appendChild(button("Reconnect Notion", "secondary", connect));
  if (!actions.children.length) actions.remove();

  node.querySelector(".dismiss").addEventListener("click", async () => {
    await removeJob(job.key);
    await load();
  });
  return node;
}

// ------------------------------------------------------------ current tab --

function renderCurrent() {
  const hero = $("current");
  if (!auth || !currentTab) {
    hero.hidden = true;
    return;
  }
  hero.hidden = false;
  $("cur-thumb").src = thumbUrl(currentTab.videoId);
  $("cur-title").textContent = currentTab.title || "YouTube video";
  for (const b of document.querySelectorAll(".seg button")) {
    b.setAttribute("aria-checked", String(b.dataset.detail === detail));
  }
  const running = jobs.find((j) => j.video_id === currentTab.videoId && j.job_id && !TERMINAL.has(j.status));
  const go = $("cur-go");
  go.disabled = !!running;
  go.querySelector("span").textContent = running ? "Notes in progress…" : "Take notes on this video";
}

async function detectTab() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    const videoId = tab && tab.url ? videoIdFromUrl(tab.url) : null;
    currentTab = videoId ? { videoId, title: (tab.title || "").replace(/\s*-\s*YouTube\s*$/, "") } : null;
  } catch {
    currentTab = null;
  }
}

function heroMessage(text, cls) {
  const m = $("cur-msg");
  m.textContent = text;
  m.className = `small hero-msg ${cls || ""}`;
  m.hidden = !text;
}

async function takeNotesHere() {
  if (!currentTab) return;
  const go = $("cur-go");
  go.disabled = true;
  go.querySelector("span").textContent = "Sending…";
  const r = await send("SUBMIT_JOB", { videoId: currentTab.videoId, title: currentTab.title, options: { detail } });
  heroMessage(r.message || "", r.ok ? "good" : "bad");
  await load();
}

// ----------------------------------------------------------------- render --

function cardFor(job) {
  const d = describeJob(job);
  const sig = [job.status, job.progress, d.headline, d.detail, job.job_id ? elapsedText(job) : "", job.title,
               job.duration_sec, job.result && job.result.notion_page_url, timeAgo(job.submittedAt)].join("|");
  const hit = cardCache.get(job.key);
  if (hit && hit.sig === sig) return hit.node;
  const node = renderCard(job);
  if (hit) node.style.animation = "none"; // only animate cards the first time they appear
  cardCache.set(job.key, { sig, node });
  return node;
}

function render() {
  const list = $("jobs");
  const connected = $("connect").hidden;
  const keys = new Set(jobs.map((j) => j.key));
  for (const k of cardCache.keys()) if (!keys.has(k)) cardCache.delete(k);
  const shown = jobs.slice(0, POPUP_LIMIT);
  list.replaceChildren(...shown.map(cardFor));
  const extra = jobs.length - shown.length;
  $("more").hidden = !connected || extra <= 0;
  $("more-btn").textContent = `View all ${jobs.length} in history →`;
  list.hidden = !jobs.length || !connected;
  $("list-head").hidden = !jobs.length || !connected;
  $("count").textContent = jobs.length;
  $("clear").hidden = !jobs.some((j) => TERMINAL.has(j.status));
  $("empty").hidden = !!jobs.length || !connected;
  const banner = $("banner");
  banner.hidden = !serverDown;
  if (serverDown) banner.textContent = "Can't reach the notes server right now — showing the last known status.";
  renderCurrent();
}

async function load() {
  const [a, list, settings] = await Promise.all([getAuth(), getJobs(), getSettings()]);
  auth = a;
  detail = settings.noteOptions.detail || "standard";
  $("connect").hidden = !!auth;
  const pill = $("conn");
  pill.hidden = !auth;
  if (auth) {
    pill.textContent = auth.workspaceName || "Notion";
    pill.title = `Publishing to ${auth.workspaceName || "Notion"}`;
  }
  jobs = list;
  render();
}

async function poll() {
  const active = jobs.filter((j) => j.job_id && !TERMINAL.has(j.status));
  if (!active.length) return;
  const { gatewayUrl } = await getSettings();
  const results = await Promise.all(active.map((j) => api.getJob(gatewayUrl, j.job_id)));
  serverDown = results.every((r) => r.network);
  let changed = false;
  for (let i = 0; i < active.length; i++) {
    const r = results[i];
    if (r.status === 404) {
      await updateJob(active[i].key, { status: "FAILED", error: { code: "JOB_NOT_FOUND" } });
      changed = true;
    } else if (r.ok && r.data) {
      await updateJob(active[i].key, { ...r.data, title: r.data.video_title || active[i].title });
      changed = true;
    }
  }
  if (changed) {
    jobs = await getJobs();
    send("JOBS_CHANGED");
  }
  render();
}

async function checkHealth() {
  const { gatewayUrl } = await getSettings();
  const r = await api.getHealth(gatewayUrl);
  const el = $("server");
  const ok = r.ok && r.data;
  const down = r.data ? Object.entries(r.data).filter(([, v]) => v !== "ok").map(([k]) => k) : [];
  el.className = `server ${ok ? "ok" : "bad"}`;
  el.querySelector("span").textContent = ok ? "Server online" : r.network ? "Server offline" : "Server degraded";
  el.title = ok ? gatewayUrl : r.network ? `Can't reach ${gatewayUrl}` : `Not ready: ${down.join(", ")}`;
}

async function connect() {
  const btn = $("connect-btn");
  const err = $("connect-error");
  err.hidden = true;
  btn.disabled = true;
  btn.textContent = "Connecting…";
  const r = await send("CONNECT_NOTION");
  btn.disabled = false;
  btn.textContent = "Connect Notion";
  if (!r.ok) {
    $("connect").hidden = false;
    err.textContent = r.message || "Couldn't connect to Notion.";
    err.hidden = false;
    return;
  }
  await load();
}

// ----------------------------------------------------------------- wiring --

$("connect-btn").addEventListener("click", connect);
$("settings").addEventListener("click", () => chrome.runtime.openOptionsPage());
const openHistory = () => chrome.tabs.create({ url: chrome.runtime.getURL("history/history.html") });
$("history").addEventListener("click", openHistory);
$("more-btn").addEventListener("click", openHistory);
$("use-token").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});
$("cur-go").addEventListener("click", takeNotesHere);
$("clear").addEventListener("click", async () => {
  await clearFinished(TERMINAL);
  await load();
});
for (const b of document.querySelectorAll(".seg button")) {
  b.addEventListener("click", async () => {
    detail = b.dataset.detail;
    const s = await getSettings();
    await saveSettings({ noteOptions: { ...s.noteOptions, detail } }); // remembered for next time
    renderCurrent();
  });
}
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && (changes.jobIndex || changes.auth)) load();
});

await detectTab();
await load();
checkHealth();
await poll();
setInterval(poll, POLL_MS);
setInterval(checkHealth, HEALTH_MS);
// Re-render every second so countdowns and elapsed timers tick smoothly.
setInterval(render, 1000);
