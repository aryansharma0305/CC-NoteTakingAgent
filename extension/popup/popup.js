// Popup: renders job cards from chrome.storage and polls GET /jobs/{id} every 2 s
// for unfinished jobs while it is open. Never shows raw JSON.

import * as api from "../lib/api.js";
import { STEPS, TERMINAL, describeJob, fmtTs, needsReconnect, stepIndex } from "../lib/status-text.js";
import { getAuth, getJobs, getSettings, removeJob, updateJob } from "../lib/storage.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 2000;
let jobs = [];
let serverDown = false;

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

function button(label, cls, onClick) {
  const b = document.createElement("button");
  b.className = cls;
  b.textContent = label;
  b.addEventListener("click", onClick);
  return b;
}

function renderCard(job) {
  const node = $("job-tpl").content.firstElementChild.cloneNode(true);
  const d = describeJob(job);
  node.dataset.tone = d.tone;

  const thumb = node.querySelector(".thumb");
  if (job.video_id) thumb.src = `https://i.ytimg.com/vi/${job.video_id}/mqdefault.jpg`;
  else thumb.remove();
  node.querySelector(".title").textContent = job.title || job.video_title || "YouTube video";
  const sub = [timeAgo(job.submittedAt)];
  if (job.duration_sec) sub.push(fmtTs(job.duration_sec));
  node.querySelector(".sub").textContent = sub.filter(Boolean).join(" · ");

  // Stepper: Queued → Transcribing → Summarizing → Extracting frames → Publishing (→ Done)
  const stepper = node.querySelector(".stepper");
  const terminal = TERMINAL.has(job.status);
  const failed = job.status === "FAILED" || job.status === "DEAD_LETTERED" || job.status === "REJECTED";
  // For a failed job, mark the stage it died in.
  const failedAt = failed && job.error && job.error.stage
    ? stepIndex({ TRANSCRIBE: "TRANSCRIBING", SUMMARIZE: "SUMMARIZING", EXTRACT_FRAMES: "EXTRACTING_FRAMES",
                  PUBLISH: "PUBLISHING" }[job.error.stage])
    : failed ? 0 : -1;
  const cur = terminal && !failed ? STEPS.length : stepIndex(job.status);
  STEPS.forEach((step, i) => {
    const li = document.createElement("li");
    li.textContent = step.label;
    if (failed) {
      li.className = i < failedAt ? "done" : i === failedAt ? "failed" : "";
    } else if (i < cur) li.className = "done";
    else if (i === cur) li.className = d.tone === "waiting" ? "waiting" : "active";
    stepper.appendChild(li);
  });
  const doneLi = document.createElement("li");
  doneLi.textContent = "Done";
  if (terminal && !failed) doneLi.className = job.status === "COMPLETED" ? "done" : "done warn";
  stepper.appendChild(doneLi);

  const pct = terminal ? (failed ? 100 : 100) : Math.max(3, job.progress || 0);
  node.querySelector(".bar-fill").style.width = `${pct}%`;
  node.querySelector(".headline").textContent = d.headline;
  node.querySelector(".detail").textContent = d.detail;

  const actions = node.querySelector(".actions");
  const url = job.result && job.result.notion_page_url;
  if (url) actions.appendChild(button("View in Notion", "primary", () => chrome.tabs.create({ url })));
  if (needsReconnect(job)) actions.appendChild(button("Reconnect Notion", "secondary", connect));
  if (!actions.children.length) actions.remove();

  node.querySelector(".dismiss").addEventListener("click", async () => {
    await removeJob(job.key);
    await load();
  });
  return node;
}

function render() {
  const list = $("jobs");
  list.replaceChildren(...jobs.map(renderCard));
  list.hidden = !jobs.length || !$("connect").hidden;
  $("empty").hidden = !!jobs.length || !$("connect").hidden;
  const banner = $("banner");
  banner.hidden = !serverDown;
  if (serverDown) banner.textContent = "Can't reach the notes server right now — showing the last known status.";
}

async function load() {
  const [auth, list] = await Promise.all([getAuth(), getJobs()]);
  $("connect").hidden = !!auth;
  const pill = $("conn");
  pill.hidden = !auth;
  if (auth) pill.textContent = auth.workspaceName || "Notion";
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

$("connect-btn").addEventListener("click", connect);
$("settings").addEventListener("click", () => chrome.runtime.openOptionsPage());
$("use-token").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && (changes.jobIndex || changes.auth)) load();
});

await load();
await poll();
setInterval(poll, POLL_MS);
// Re-render every second so "retrying in N s" counts down smoothly.
setInterval(render, 1000);
