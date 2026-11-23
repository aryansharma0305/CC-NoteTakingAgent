// History page: every stored job (up to MAX_JOBS), with search, status filters,
// sorting, live progress for running jobs, and per-job actions.

import * as api from "../lib/api.js";
import { TERMINAL, canRetry, describeJob, elapsedText, fmtTs } from "../lib/status-text.js";
import { clearFinished, getJobs, getSettings, removeJob, updateJob } from "../lib/storage.js";
import { thumbUrl } from "../lib/youtube.js";

const $ = (id) => document.getElementById(id);
const POLL_MS = 3000;
const PREF_KEY = "vn-history-prefs";
let jobs = [];
let serverDown = false;
const prefs = loadPrefs();

function loadPrefs() {
  try {
    return { filter: "all", sort: "new", ...JSON.parse(localStorage.getItem(PREF_KEY) || "{}") };
  } catch {
    return { filter: "all", sort: "new" };
  }
}
function savePrefs() {
  try {
    localStorage.setItem(PREF_KEY, JSON.stringify({ filter: prefs.filter, sort: prefs.sort }));
  } catch {
    /* private window: preferences just aren't remembered */
  }
}

const send = (type, extra = {}) =>
  new Promise((resolve) => chrome.runtime.sendMessage({ type, ...extra }, (r) => resolve(r || {})));

const isActive = (j) => !TERMINAL.has(j.status);
const GROUPS = {
  all: () => true,
  active: isActive,
  done: (j) => j.status === "COMPLETED" || j.status === "COMPLETED_WITH_WARNINGS",
  warn: (j) => j.status === "COMPLETED_WITH_WARNINGS",
  failed: (j) => ["FAILED", "DEAD_LETTERED", "REJECTED"].includes(j.status),
};
const PILL = {
  QUEUED: "Queued", TRANSCRIBING: "Transcribing", SUMMARIZING: "Summarizing", EXTRACTING_FRAMES: "Capturing frames",
  PUBLISHING: "Publishing", COMPLETED: "Done", COMPLETED_WITH_WARNINGS: "Done · warnings", FAILED: "Failed",
  DEAD_LETTERED: "Gave up", REJECTED: "Not accepted",
};

const submittedAt = (j) => (j.created_at && Date.parse(j.created_at)) || j.submittedAt || 0;

function when(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return sameDay ? `Today ${time}` : `${d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" })} ${time}`;
}

function highlight(el, text, q) {
  el.replaceChildren();
  if (!q) {
    el.textContent = text;
    return;
  }
  const lower = text.toLowerCase();
  let i = 0;
  for (let at = lower.indexOf(q, i); at !== -1; at = lower.indexOf(q, i)) {
    el.append(text.slice(i, at));
    const m = document.createElement("mark");
    m.textContent = text.slice(at, at + q.length);
    el.append(m);
    i = at + q.length;
  }
  el.append(text.slice(i));
}

function button(label, cls, onClick, title) {
  const b = document.createElement("button");
  b.className = cls;
  b.textContent = label;
  if (title) b.title = title;
  b.addEventListener("click", onClick);
  return b;
}

// ------------------------------------------------------------------- render --

function renderRow(job, q) {
  const node = $("row-tpl").content.firstElementChild.cloneNode(true);
  const d = describeJob(job);
  node.dataset.tone = d.tone;
  const wrap = node.querySelector(".thumb-wrap");
  if (job.video_id) {
    node.querySelector(".thumb").src = thumbUrl(job.video_id);
    wrap.href = `https://www.youtube.com/watch?v=${job.video_id}`;
  } else wrap.remove();
  if (job.duration_sec) {
    const dur = node.querySelector(".dur");
    dur.textContent = fmtTs(job.duration_sec);
    dur.hidden = false;
  }
  highlight(node.querySelector(".title"), job.title || job.video_title || "YouTube video", q);
  node.querySelector(".pill").textContent = PILL[job.status] || job.status;

  const meta = [when(submittedAt(job))];
  const o = job.options || {};
  if (o.detail) meta.push(o.detail[0].toUpperCase() + o.detail.slice(1));
  if (o.language) meta.push(o.language);
  if (job.result && job.result.frames_requested) meta.push(`${job.result.frames_published}/${job.result.frames_requested} frames`);
  if (job.job_id) meta.push(isActive(job) ? `running ${elapsedText(job)}` : `took ${elapsedText(job)}`);
  node.querySelector(".meta").textContent = meta.filter(Boolean).join(" · ");

  if (isActive(job)) {
    const bar = node.querySelector(".bar");
    bar.hidden = false;
    bar.querySelector(".bar-fill").style.width = `${Math.max(3, job.progress || 0)}%`;
  }
  node.querySelector(".detail").textContent = isActive(job) ? `${d.headline} ${d.detail}` : d.detail;

  const actions = node.querySelector(".actions");
  const url = job.result && job.result.notion_page_url;
  if (url) {
    actions.append(button("Open in Notion", "primary", () => chrome.tabs.create({ url })));
    actions.append(button("Copy link", "ghost", async (e) => {
      await navigator.clipboard.writeText(url).catch(() => {});
      e.target.textContent = "Copied ✓";
      setTimeout(() => (e.target.textContent = "Copy link"), 1500);
    }));
  }
  const permanent = ["FAILED", "DEAD_LETTERED", "REJECTED"].includes(job.status) && !canRetry(job);
  if (job.video_id && !isActive(job) && !permanent) {
    const label = canRetry(job) ? "Try again" : "Re-run";
    actions.append(button(label, "secondary", async (e) => {
      e.target.disabled = true;
      e.target.textContent = "Submitting…";
      const r = await send("RETRY_JOB", { key: job.key });
      e.target.textContent = r.ok ? "Submitted ✓" : "Couldn't submit";
      if (!r.ok && r.message) alert(r.message);
      await load();
    }, "Take notes on this video again with the same settings (reuses earlier results, so it is quick)"));
    if (GROUPS.done(job)) {
      actions.append(button("Regenerate", "ghost", async (e) => {
        e.target.disabled = true;
        e.target.textContent = "Submitting…";
        const r = await send("RETRY_JOB", { key: job.key, fresh: true });
        e.target.textContent = r.ok ? "Submitted ✓" : "Couldn't submit";
        await load();
      }, "Make brand-new notes: re-run the AI and recapture frames instead of reusing earlier results"));
    }
  }
  actions.append(button("Remove", "ghost danger", async () => {
    await removeJob(job.key);
    await load();
  }, "Remove from this list (the Notion page is kept)"));
  return node;
}

function renderStats() {
  const done = jobs.filter(GROUPS.done);
  const frames = done.reduce((n, j) => n + ((j.result && j.result.frames_published) || 0), 0);
  const times = done.map((j) => (j.updated_at && j.created_at ? Date.parse(j.updated_at) - Date.parse(j.created_at) : 0))
    .filter((t) => t > 0);
  const avg = times.length ? Math.round(times.reduce((a, b) => a + b, 0) / times.length / 1000) : 0;
  const stats = [
    [jobs.length, "videos"],
    [done.length, "notes published"],
    [jobs.filter(isActive).length, "in progress"],
    [frames, "frames captured"],
    [avg ? (avg < 60 ? `${avg}s` : `${Math.floor(avg / 60)}m ${avg % 60}s`) : "—", "average time"],
  ];
  $("stats").replaceChildren(...stats.map(([v, l]) => {
    const el = document.createElement("div");
    el.className = "stat";
    el.innerHTML = "<b></b><span></span>";
    el.querySelector("b").textContent = v;
    el.querySelector("span").textContent = l;
    return el;
  }));
}

function render() {
  const q = $("q").value.trim().toLowerCase();
  for (const b of document.querySelectorAll(".chips button")) {
    const f = b.dataset.filter;
    b.setAttribute("aria-selected", String(f === prefs.filter));
    b.querySelector("span").textContent = jobs.filter(GROUPS[f]).length;
  }
  let shown = jobs.filter(GROUPS[prefs.filter] || GROUPS.all)
    .filter((j) => !q || (j.title || j.video_title || "").toLowerCase().includes(q));
  if (prefs.sort === "old") shown = [...shown].sort((a, b) => submittedAt(a) - submittedAt(b));
  else if (prefs.sort === "title") shown = [...shown].sort((a, b) => (a.title || "").localeCompare(b.title || ""));
  else shown = [...shown].sort((a, b) => submittedAt(b) - submittedAt(a));

  $("list").replaceChildren(...shown.map((j) => renderRow(j, q)));
  $("empty").hidden = shown.length > 0;
  if (!shown.length) {
    const none = !jobs.length;
    $("empty-title").innerHTML = none ? "<strong>No notes yet</strong>" : "<strong>Nothing matches</strong>";
    $("empty-text").textContent = none
      ? "Open a YouTube video and click Take Notes in the player, press Alt+Shift+N, or right-click a video link."
      : "Try a different search or filter.";
  }
  $("clear").disabled = !jobs.some((j) => TERMINAL.has(j.status));
  $("banner").hidden = !serverDown;
  $("banner").textContent = "Can't reach the notes server — showing the last known status.";
  renderStats();
}

// --------------------------------------------------------------- data --------

async function load() {
  jobs = await getJobs();
  render();
}

async function poll() {
  const active = jobs.filter((j) => j.job_id && isActive(j));
  if (!active.length) return;
  const { gatewayUrl } = await getSettings();
  const results = await Promise.all(active.map((j) => api.getJob(gatewayUrl, j.job_id)));
  serverDown = results.every((r) => r.network);
  for (let i = 0; i < active.length; i++) {
    const r = results[i];
    if (r.status === 404) await updateJob(active[i].key, { status: "FAILED", error: { code: "JOB_NOT_FOUND" } });
    else if (r.ok && r.data) await updateJob(active[i].key, { ...r.data, title: r.data.video_title || active[i].title });
  }
  send("JOBS_CHANGED");
  await load();
}

function exportCsv() {
  const cols = ["title", "video_url", "status", "submitted", "notion_page_url", "frames_published", "frames_requested",
                "detail", "language"];
  const esc = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const rows = jobs.map((j) => [
    j.title || j.video_title, j.video_id ? `https://www.youtube.com/watch?v=${j.video_id}` : "", j.status,
    new Date(submittedAt(j)).toISOString(), j.result && j.result.notion_page_url,
    j.result && j.result.frames_published, j.result && j.result.frames_requested,
    j.options && j.options.detail, j.options && j.options.language,
  ].map(esc).join(","));
  const blob = new Blob([[cols.join(","), ...rows].join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `video-notes-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// ------------------------------------------------------------- wiring --------

for (const b of document.querySelectorAll(".chips button")) {
  b.addEventListener("click", () => {
    prefs.filter = b.dataset.filter;
    savePrefs();
    render();
  });
}
$("sort").value = prefs.sort;
$("sort").addEventListener("change", () => {
  prefs.sort = $("sort").value;
  savePrefs();
  render();
});
$("q").addEventListener("input", render);
$("export").addEventListener("click", exportCsv);
$("settings").addEventListener("click", () => chrome.runtime.openOptionsPage());
$("clear").addEventListener("click", async () => {
  const n = await clearFinished(TERMINAL);
  if (n) await load();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== $("q")) {
    e.preventDefault();
    $("q").focus();
  }
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && Object.keys(changes).some((k) => k === "jobIndex" || k.startsWith("job:"))) load();
});

await load();
await poll();
setInterval(poll, POLL_MS);
setInterval(() => jobs.some(isActive) && render(), 1000); // tick "running 2m 05s" while anything runs
