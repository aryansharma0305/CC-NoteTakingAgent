// Content script: injects a "Take Notes" button into the YouTube player toolbar.
// YouTube is a single-page app, so injection re-runs on yt-navigate-finish and via
// a MutationObserver, guarded by the element id so the button is never added twice.
// The video id is read from location.href at CLICK time, not injection time.
//
// The button mirrors the newest job for the video on screen (read from
// chrome.storage, which the background keeps fresh):
//   idle      "Take Notes"                     click -> new job
//   running   progress ring + "Summarizing 35%" (asks the background to refresh every 3 s)
//   done      "Notes ready ✓" (amber if warnings) click -> open the Notion page; Shift+click -> new job
//   failed    "Retry notes"                    click -> new job
(() => {
  const BTN_ID = "vn-take-notes-btn";
  const FLOAT_ID = "vn-take-notes-float";
  const TOAST_ID = "vn-toast";
  const TERMINAL = new Set(["COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "DEAD_LETTERED", "REJECTED"]);
  const LABEL = { QUEUED: "Queued", TRANSCRIBING: "Transcribing", SUMMARIZING: "Summarizing",
                  EXTRACTING_FRAMES: "Capturing frames", PUBLISHING: "Publishing" };
  const RING_C = 2 * Math.PI * 7; // circumference of the r=7 progress ring
  const DOC_ICON =
    '<svg class="vn-icon" viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">' +
    '<path fill="currentColor" d="M6 2h9l5 5v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zm8 1.5V8h4.5L14 3.5zM7 12h10v1.6H7V12zm0 3.4h10V17H7v-1.6zm0-6.8h5v1.6H7V8.6z"/>' +
    "</svg>";
  const RING =
    '<svg class="vn-ring" viewBox="0 0 18 18" width="18" height="18" aria-hidden="true" focusable="false">' +
    '<circle class="vn-ring__track" cx="9" cy="9" r="7"/><circle class="vn-ring__bar" cx="9" cy="9" r="7"/></svg>';
  let shownJob = null; // the job the button currently reflects
  let pollTimer = null;

  function currentVideoId() {
    try {
      const u = new URL(location.href);
      if (u.pathname === "/watch") return u.searchParams.get("v");
      const m = u.pathname.match(/^\/(?:shorts|live)\/([A-Za-z0-9_-]{11})/);
      return m ? m[1] : null;
    } catch {
      return null;
    }
  }

  function currentTitle() {
    const h = document.querySelector("ytd-watch-metadata h1 yt-formatted-string, h1.ytd-watch-metadata");
    const t = (h && h.textContent) || document.title || "";
    return t.replace(/\s*-\s*YouTube\s*$/, "").trim();
  }

  const ICONS = {
    info: '<path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z"/>',
    success: '<path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm-1.5 14.5-4-4 1.4-1.4 2.6 2.6 5.6-5.6 1.4 1.4-7 7z"/>',
    error: '<path d="M12 2a10 10 0 1 0 0 20 10 10 0 0 0 0-20zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z"/>',
  };

  function toast(message, tone = "info") {
    let el = document.getElementById(TOAST_ID);
    if (!el) {
      el = document.createElement("div");
      el.id = TOAST_ID;
      el.setAttribute("role", "status");
      el.setAttribute("aria-live", "polite");
      document.body.appendChild(el);
    }
    const t = ICONS[tone] ? tone : "info";
    el.className = `vn-toast vn-toast--${t} vn-toast--show`;
    el.innerHTML =
      `<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><g fill="currentColor">${ICONS[t]}</g></svg>` +
      '<div class="vn-toast__body"><div class="vn-toast__title">Video Notes</div><div class="vn-toast__msg"></div></div>';
    el.querySelector(".vn-toast__msg").textContent = message;
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove("vn-toast--show"), 5000);
  }

  function alive() {
    try {
      return !!(chrome.runtime && chrome.runtime.id); // false once the extension is reloaded
    } catch {
      return false;
    }
  }

  // Newest job for this video from chrome.storage (layout: lib/storage.js).
  async function jobForVideo(videoId) {
    if (!videoId || !alive()) return null;
    const { jobIndex } = await chrome.storage.local.get("jobIndex");
    const keys = (jobIndex || []).map((k) => `job:${k}`);
    if (!keys.length) return null;
    const data = await chrome.storage.local.get(keys);
    return keys.map((k) => data[k]).find((j) => j && j.video_id === videoId) || null; // index is newest first
  }

  function buttons() {
    return [document.getElementById(BTN_ID), document.getElementById(FLOAT_ID)].filter(Boolean);
  }

  function paint(btn, job) {
    const label = btn.querySelector(".vn-label");
    if (!label) return;
    const status = job && job.status;
    const url = job && job.result && job.result.notion_page_url;
    let mods = ["idle"]; // first entry is the state; the rest are style modifiers
    let text = "Take Notes";
    let title = "Take notes on this video (Alt+Shift+N)";
    if (job && !TERMINAL.has(status)) {
      const waiting = !!(job.waiting_until || job.retry_reason);
      mods = status === "QUEUED" || waiting ? ["busy", "indeterminate"] : ["busy"];
      text = waiting ? "Waiting…" : `${LABEL[status] || "Working"}${job.progress ? ` ${job.progress}%` : ""}`;
      title = waiting && job.retry_reason ? job.retry_reason : "Making notes — open the extension for details";
    } else if (url && (status === "COMPLETED" || status === "COMPLETED_WITH_WARNINGS")) {
      mods = status === "COMPLETED" ? ["ready"] : ["ready", "warn"];
      text = "Notes ready ✓";
      title = "Open the notes in Notion (Shift+click to take notes again)";
    } else if (status === "FAILED" || status === "DEAD_LETTERED") {
      mods = ["failed"];
      text = "Retry notes";
      title = "The last attempt failed — click to try again";
    }
    btn.className = btn.className.replace(/\s*vn-btn--\S+/g, "") + mods.map((m) => ` vn-btn--${m}`).join("");
    btn.dataset.state = mods[0];
    btn.title = title;
    btn.setAttribute("aria-label", text);
    label.textContent = text;
    const busy = btn.dataset.state === "busy";
    const icon = btn.querySelector(".vn-icon, .vn-ring");
    if (busy && !btn.querySelector(".vn-ring")) icon.outerHTML = RING;
    if (!busy && !btn.querySelector(".vn-icon")) icon.outerHTML = DOC_ICON;
    const bar = btn.querySelector(".vn-ring__bar");
    if (bar) {
      const p = Math.min(100, Math.max(4, (job && job.progress) || 0)) / 100;
      bar.style.strokeDasharray = `${RING_C}`;
      bar.style.strokeDashoffset = `${RING_C * (1 - p)}`;
    }
  }

  async function refreshButtons() {
    const job = await jobForVideo(currentVideoId()).catch(() => null);
    // A rejected submission (e.g. too long) is reported by toast; keep the button usable.
    shownJob = job && job.status !== "REJECTED" ? job : null;
    for (const b of buttons()) paint(b, shownJob);
    const active = shownJob && shownJob.job_id && !TERMINAL.has(shownJob.status);
    if (active && !pollTimer) {
      pollTimer = setInterval(() => {
        if (!alive() || !shownJob || TERMINAL.has(shownJob.status)) return;
        chrome.runtime.sendMessage({ type: "REFRESH_JOB", key: shownJob.key }, () => void chrome.runtime.lastError);
      }, 3000);
    } else if (!active && pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function submit(videoId, force) {
    toast("Sending to the notes server…");
    try {
      chrome.runtime.sendMessage(
        { type: "SUBMIT_JOB", videoId, url: `https://www.youtube.com/watch?v=${videoId}`, title: currentTitle(), force },
        (resp) => {
          if (chrome.runtime.lastError || !resp) {
            toast("The extension didn't answer. Reload this page and try again.", "error");
            return;
          }
          toast(resp.message, resp.tone || (resp.ok ? "success" : "error"));
          refreshButtons();
        }
      );
    } catch {
      toast("The extension was updated. Reload this page to use it.", "error");
    }
  }

  function onClick(ev) {
    ev.preventDefault();
    ev.stopPropagation();
    const videoId = currentVideoId();
    if (!videoId) {
      toast("Open a video first.", "error");
      return;
    }
    if (!alive()) {
      toast("The extension was updated. Reload this page to use it.", "error");
      return;
    }
    const btn = ev.currentTarget;
    const job = shownJob && shownJob.video_id === videoId ? shownJob : null;
    const state = btn.dataset.state;
    if (state === "ready" && !ev.shiftKey) {
      window.open(job.result.notion_page_url, "_blank", "noopener");
      return;
    }
    if (state === "busy") {
      toast("Notes for this video are already in progress — open the extension to watch.");
      return;
    }
    btn.disabled = true;
    setTimeout(() => (btn.disabled = false), 1500);
    submit(videoId, state === "ready" || state === "failed");
  }

  function makeButton(id, extraClass) {
    const b = document.createElement("button");
    b.id = id;
    b.type = "button";
    b.className = extraClass;
    b.title = "Take notes on this video (Alt+Shift+N)";
    b.setAttribute("aria-label", "Take Notes");
    b.innerHTML = `${DOC_ICON}<span class="vn-label">Take Notes</span>`;
    b.addEventListener("click", onClick);
    paint(b, shownJob);
    refreshButtons();
    return b;
  }

  function inject() {
    const onVideo = !!currentVideoId();
    const existing = document.getElementById(BTN_ID);
    const floating = document.getElementById(FLOAT_ID);
    if (!onVideo) {
      if (floating) floating.remove();
      return;
    }
    if (existing && existing.isConnected) {
      if (floating) floating.remove();
      return;
    }
    const controls = document.querySelector("#movie_player .ytp-right-controls, .ytp-right-controls");
    if (controls) {
      controls.insertBefore(makeButton(BTN_ID, "ytp-button vn-btn"), controls.firstChild);
      if (floating) floating.remove();
      return;
    }
    // Player controls not found (layout change / embedded player): floating fallback.
    if (!floating) document.body.appendChild(makeButton(FLOAT_ID, "vn-float-btn"));
  }

  let scheduled = false;
  function scheduleInject() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      inject();
    });
  }

  // Toasts for submissions started elsewhere (keyboard shortcut, right-click menu).
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "VN_TOAST") {
      toast(msg.message, msg.tone || "info");
      refreshButtons();
    }
  });

  // The background (and popup) write job updates to chrome.storage; repaint on any job change.
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "local" || !alive()) return;
    if (Object.keys(changes).some((k) => k === "jobIndex" || k.startsWith("job:"))) refreshButtons();
  });

  // New video in the same tab (YouTube is a single-page app): show that video's state.
  window.addEventListener("yt-navigate-finish", refreshButtons);
  window.addEventListener("yt-navigate-finish", scheduleInject);
  window.addEventListener("yt-page-data-updated", scheduleInject);
  new MutationObserver(scheduleInject).observe(document.documentElement, { childList: true, subtree: true });
  scheduleInject();
})();
