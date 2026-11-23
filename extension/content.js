// Content script: injects a "Take Notes" button into the YouTube player toolbar.
// YouTube is a single-page app, so injection re-runs on yt-navigate-finish and via
// a MutationObserver, guarded by the element id so the button is never added twice.
// The video id is read from location.href at CLICK time, not injection time.
(() => {
  const BTN_ID = "vn-take-notes-btn";
  const FLOAT_ID = "vn-take-notes-float";
  const TOAST_ID = "vn-toast";

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

  function flashButton(btn, ok) {
    const label = btn.querySelector("span");
    if (!label) return;
    btn.classList.toggle("vn-btn--done", !!ok);
    label.textContent = ok ? "Queued ✓" : "Take Notes";
    clearTimeout(btn._t);
    btn._t = setTimeout(() => {
      label.textContent = "Take Notes";
      btn.classList.remove("vn-btn--done");
    }, 3000);
  }

  function onClick(ev) {
    ev.preventDefault();
    ev.stopPropagation();
    const videoId = currentVideoId();
    if (!videoId) {
      toast("Open a video first.", "error");
      return;
    }
    const btn = ev.currentTarget;
    btn.disabled = true;
    setTimeout(() => (btn.disabled = false), 1500);
    toast("Sending to the notes server…");
    try {
      chrome.runtime.sendMessage(
        { type: "SUBMIT_JOB", videoId, url: `https://www.youtube.com/watch?v=${videoId}`, title: currentTitle() },
        (resp) => {
          if (chrome.runtime.lastError || !resp) {
            toast("The extension didn't answer. Reload this page and try again.", "error");
            return;
          }
          toast(resp.message, resp.tone || (resp.ok ? "success" : "error"));
          if (resp.ok && !resp.duplicate) flashButton(btn, true);
        }
      );
    } catch {
      toast("The extension was updated. Reload this page to use it.", "error");
    }
  }

  function makeButton(id, extraClass) {
    const b = document.createElement("button");
    b.id = id;
    b.type = "button";
    b.className = extraClass;
    b.title = "Take notes on this video (Alt+Shift+N)";
    b.setAttribute("aria-label", "Take Notes");
    b.innerHTML =
      '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true" focusable="false">' +
      '<path fill="currentColor" d="M6 2h9l5 5v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2zm8 1.5V8h4.5L14 3.5zM7 12h10v1.6H7V12zm0 3.4h10V17H7v-1.6zm0-6.8h5v1.6H7V8.6z"/>' +
      "</svg><span>Take Notes</span>";
    b.addEventListener("click", onClick);
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
      const btn = document.getElementById(BTN_ID);
      if (btn && msg.tone === "success") flashButton(btn, true);
    }
  });

  window.addEventListener("yt-navigate-finish", scheduleInject);
  window.addEventListener("yt-page-data-updated", scheduleInject);
  new MutationObserver(scheduleInject).observe(document.documentElement, { childList: true, subtree: true });
  scheduleInject();
})();
