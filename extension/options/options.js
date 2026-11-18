import * as api from "../lib/api.js";
import { getAuth, getSettings, saveSettings } from "../lib/storage.js";

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
}

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
  if (r.network || !r.data) {
    const li = document.createElement("li");
    li.className = "bad";
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
