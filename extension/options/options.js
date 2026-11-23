import * as api from "../lib/api.js";
import { DEFAULT_NOTE_OPTIONS, getAuth, getSettings, saveSettings } from "../lib/storage.js";

const $ = (id) => document.getElementById(id);

function flash(text, ok = true) {
  const m = $("msg");
  m.textContent = text;
  m.className = `msg ${ok ? "ok" : "bad"}`;
  m.hidden = false;
  clearTimeout(m._t);
  m._t = setTimeout(() => (m.hidden = true), 5000);
}

function send(type, extra = {}) {
  return new Promise((resolve) => chrome.runtime.sendMessage({ type, ...extra }, (r) => resolve(r || {})));
}

// Accept a Notion URL or a raw id; return the 32-hex id (dashes optional).
function parseNotionId(input) {
  const s = (input || "").trim();
  if (!s) return "";
  const m = s.replace(/-/g, "").match(/([0-9a-f]{32})(?:\?|$|#)/i) || s.replace(/-/g, "").match(/([0-9a-f]{32})/i);
  return m ? m[1] : null;
}

async function refresh() {
  const [settings, auth] = await Promise.all([getSettings(), getAuth()]);
  $("gateway").value = settings.gatewayUrl;
  $("parent").value = settings.parentId || "";
  $("auth-status").textContent = auth
    ? `Connected to ${auth.workspaceName || "Notion"} (${auth.mode === "internal" ? "internal integration" : "OAuth"}).`
    : "Not connected.";
  $("disconnect").hidden = !auth;
  $("connect").textContent = auth ? "Reconnect Notion" : "Connect Notion";
  const chip = $("auth-chip");
  chip.textContent = auth ? auth.workspaceName || "Connected" : "Not connected";
  chip.className = `chip ${auth ? "ok" : ""}`;
  renderStyle(settings.noteOptions);
}

// ---- note style ----
function renderStyle(o) {
  for (const r of document.querySelectorAll('input[name="detail"]')) r.checked = r.value === (o.detail || "standard");
  const serverDefault = o.max_frames === null || o.max_frames === undefined || o.max_frames === "";
  $("frames-default").checked = serverDefault;
  $("frames").disabled = serverDefault;
  $("frames").value = serverDefault ? 5 : o.max_frames;
  updateFramesLabel();
  $("study").checked = o.study_aids !== false;
  $("transcript").checked = o.include_transcript !== false;
  $("language").value = o.language || "";
}

function updateFramesLabel() {
  const v = Number($("frames").value);
  $("frames-val").textContent = $("frames-default").checked ? "server default" : v === 0 ? "text only" : String(v);
}

function readStyle() {
  const checked = document.querySelector('input[name="detail"]:checked');
  return {
    detail: checked ? checked.value : "standard",
    max_frames: $("frames-default").checked ? null : Number($("frames").value),
    study_aids: $("study").checked,
    include_transcript: $("transcript").checked,
    language: $("language").value,
  };
}

$("frames").addEventListener("input", updateFramesLabel);
$("frames-default").addEventListener("change", () => {
  $("frames").disabled = $("frames-default").checked;
  updateFramesLabel();
});
$("save-style").addEventListener("click", async () => {
  await saveSettings({ noteOptions: readStyle() });
  flash("Note style saved. New jobs will use it.");
});
$("reset-style").addEventListener("click", async () => {
  await saveSettings({ noteOptions: { ...DEFAULT_NOTE_OPTIONS } });
  renderStyle(DEFAULT_NOTE_OPTIONS);
  flash("Note style reset to defaults.");
});
$("shortcuts").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.tabs.create({ url: "chrome://extensions/shortcuts" });
});

$("save-gateway").addEventListener("click", async () => {
  const url = $("gateway").value.trim() || "http://localhost:8000";
  try {
    const origin = new URL(url).origin + "/*";
    const granted = await chrome.permissions.request({ origins: [origin] }).catch(() => false);
    if (!granted && !/^http:\/\/(localhost|127\.0\.0\.1):8000$/.test(new URL(url).origin)) {
      flash("Permission to talk to that server was not granted.", false);
      return;
    }
  } catch {
    flash("That isn't a valid URL.", false);
    return;
  }
  await saveSettings({ gatewayUrl: url });
  flash("Server address saved.");
});

$("test").addEventListener("click", async () => {
  const { gatewayUrl } = await getSettings();
  const list = $("health");
  list.replaceChildren();
  list.hidden = false;
  const r = await api.getHealth(gatewayUrl);
  const chip = $("server-state");
  const allOk = r.ok && r.data;
  chip.textContent = allOk ? "Online" : r.network ? "Unreachable" : "Degraded";
  chip.className = `chip ${allOk ? "ok" : "bad"}`;
  if (r.network || !r.data) {
    const li = document.createElement("li");
    li.className = "bad wide";
    li.textContent = `Can't reach ${gatewayUrl}. Is the server running (docker compose up)?`;
    list.appendChild(li);
    return;
  }
  const names = { gateway: "API gateway", postgres: "Job database", rabbitmq: "Message broker",
                  redis: "Rate limiter", s3: "Frame storage" };
  for (const [k, label] of Object.entries(names)) {
    const li = document.createElement("li");
    const ok = r.data[k] === "ok";
    li.className = ok ? "ok" : "bad";
    li.textContent = `${label}: ${ok ? "reachable" : "not reachable"}`;
    list.appendChild(li);
  }
});

$("connect").addEventListener("click", async () => {
  $("connect").disabled = true;
  const r = await send("CONNECT_NOTION");
  $("connect").disabled = false;
  if (r.ok) {
    if (r.templateId && !$("parent").value) flash(`Connected to ${r.workspaceName}. Notes go to the "Video Notes" page.`);
    else flash(`Connected to ${r.workspaceName}.`);
  } else flash(r.message || "Couldn't connect.", false);
  refresh();
});

$("disconnect").addEventListener("click", async () => {
  await send("DISCONNECT_NOTION");
  flash("Disconnected from Notion.");
  refresh();
});

$("save-token").addEventListener("click", async () => {
  const r = await send("SAVE_INTERNAL_TOKEN", { token: $("token").value });
  if (r.ok) {
    $("token").value = "";
    flash("Token saved. Make sure a page is shared with the integration.");
  } else flash(r.message || "Couldn't save the token.", false);
  refresh();
});

$("save-parent").addEventListener("click", async () => {
  const id = parseNotionId($("parent").value);
  if (id === null) {
    flash("That doesn't look like a Notion page URL or id.", false);
    return;
  }
  await saveSettings({ parentId: id });
  flash(id ? "Destination page saved." : "Destination cleared; using the default.");
  refresh();
});

refresh();
$("test").click(); // show server status right away
