// The send page: pick signed-up devices, send them a notification, and follow what happened to each send.

const $ = (id) => document.getElementById(id);
const checked = new Set();
let knownDevices = [];

async function api(path, options = {}) {
  const response = await fetch(path, { credentials: "include", headers: { "Content-Type": "application/json" }, ...options });
  if (response.status === 401) throw new Error("Your workspace sign-in ran out. Reload this page to sign in again.");
  if (!response.ok) {
    let detail = `${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (error) { /* not JSON */ }
    throw new Error(detail);
  }
  return response.json();
}

function relativeTime(iso) {
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return `${Math.round(seconds / 86400)} days ago`;
}

function time(iso) {
  return iso ? new Date(iso).toLocaleTimeString() : "";
}

async function loadDevices() {
  const response = await api("/api/devices");
  const live = new Map(response.live_devices.map((l) => [l.device_id, l]));
  // A device whose page is open but never signed up for pushes can still get live sends.
  const liveOnly = response.live_devices
    .filter((l) => !response.devices.some((d) => d.device_id === l.device_id))
    .map((l) => ({ device_id: l.device_id, name: l.name, status: "live-only" }));
  const devices = [...response.devices, ...liveOnly];
  const isFirstLoad = knownDevices.length === 0 && checked.size === 0;
  knownDevices = devices;
  if (isFirstLoad) for (const d of devices) if (d.status === "active") checked.add(d.device_id);
  const list = $("devices");
  list.innerHTML = "";
  if (!devices.length) {
    list.innerHTML = '<li class="empty">No devices yet. Open this app on your phone and tap Allow notifications.</li>';
    return;
  }
  for (const d of devices) {
    const row = document.createElement("li");
    row.id = `device-${d.device_id}`;
    row.dataset.deviceId = d.device_id;
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = checked.has(d.device_id);
    box.addEventListener("change", () => (box.checked ? checked.add(d.device_id) : checked.delete(d.device_id)));
    const info = document.createElement("div");
    info.className = "grow";
    info.innerHTML = "<strong></strong><div class=meta></div>";
    info.querySelector("strong").textContent = d.name;
    info.querySelector(".meta").textContent =
      d.status === "expired" ? d.status_detail
        : d.status === "live-only" ? "Not signed up for pushes - live only"
          : `Push through ${d.push_service} - signed up ${relativeTime(d.signed_up_at)}`;
    if (d.status === "expired") info.querySelector(".meta").className = "meta result-bad";
    const liveState = document.createElement("span");
    liveState.className = "meta live-state";
    liveState.innerHTML = live.has(d.device_id)
      ? `<span class="dot ok"></span>Live: connected`
      : `<span class="dot off"></span>Live: not open`;
    const remove = document.createElement("button");
    remove.textContent = "Remove";
    remove.className = "small";
    remove.addEventListener("click", async () => {
      await api(`/api/devices/${encodeURIComponent(d.device_id)}`, { method: "DELETE" });
      checked.delete(d.device_id);
      loadDevices();
    });
    remove.hidden = d.status === "live-only";
    row.append(box, info, liveState, remove);
    list.appendChild(row);
  }
}

async function loadSends() {
  const { sends } = await api("/api/sends");
  const list = $("sent");
  list.innerHTML = "";
  if (!sends.length) {
    list.innerHTML = '<li class="empty">Nothing sent yet.</li>';
    return false;
  }
  let isAnyPending = false;
  for (const s of sends.slice(0, 50)) {
    const row = document.createElement("li");
    row.id = `send-${s.send_id}`;
    const pending = s.status === "waiting" || s.status === "sending" || (s.status === "accepted" && !s.shown_at && Date.now() - new Date(s.sent_at).getTime() < 120000);
    if (pending) isAnyPending = true;
    const verdictClass = s.status === "accepted" ? "result-ok" : pending ? "result-wait" : "result-bad";
    let shown = "";
    if (s.shown_at) shown = ` - <span class="result-ok">device showed it at ${time(s.shown_at)}</span>`;
    else if (s.status === "accepted") shown = ' - <span class="result-wait">device has not confirmed showing it</span>';
    const fromChat = s.source === "chat" ? '<span class="tag chat">From chat</span>' : "";
    row.innerHTML = `${fromChat}<span class="tag ${s.via}">${s.via === "live" ? "Live" : "Push"}</span><strong></strong> to <span class="device"></span>
      <div class="meta">${time(s.created_at)}${s.delay_seconds ? ` (+${s.delay_seconds} s)` : ""} - <span class="${verdictClass} verdict"></span>${shown}
 - <a class="raw-link" href="/sends/${encodeURIComponent(s.send_id)}/raw">${s.raw_response ? "view raw response" : "view raw record"}</a></div>`;
    row.querySelector("strong").textContent = s.title;
    row.querySelector(".device").textContent = s.device_name;
    row.querySelector(".verdict").textContent = s.status_detail;
    if (s.style && s.style !== "plain") {
      const styleTag = document.createElement("span");
      styleTag.className = "tag style";
      styleTag.textContent = styleNames.get(s.style) || s.style;
      row.prepend(styleTag);
    }
    const notes = [];
    if (s.shown_at && s.device_app_version !== appVersion) notes.push(s.device_app_version ? `shown by an older app version (${s.device_app_version})` : "shown by an older app version");
    if (s.device_max_actions !== null && s.device_max_actions !== undefined) notes.push(`device shows up to ${s.device_max_actions} buttons`);
    for (const i of s.interactions || []) {
      const at = time(i.at);
      if (i.kind === "reply") notes.push(`replied "${i.reply}" at ${at}`);
      else if (i.kind === "action") notes.push(`tapped "${i.action}" at ${at}`);
      else if (i.kind === "clicked") notes.push(`tapped the notification at ${at}`);
      else notes.push(`dismissed at ${at}`);
    }
    if (notes.length) {
      const line = document.createElement("div");
      line.className = "meta interactions";
      line.textContent = notes.join(" - ");
      row.appendChild(line);
    }
    list.appendChild(row);
  }
  return isAnyPending;
}

async function send() {
  $("send-error").textContent = "";
  const deviceIds = knownDevices.filter((d) => checked.has(d.device_id)).map((d) => d.device_id);
  if (!deviceIds.length) {
    $("send-error").textContent = "Pick at least one device.";
    return;
  }
  const delay = Number(document.querySelector('input[name="when"]:checked').value);
  const via = document.querySelector('input[name="via"]:checked').value;
  const style = $("style").value || "plain";
  $("send-button").disabled = true;
  try {
    await api("/api/sends", {
      method: "POST",
      body: JSON.stringify({ device_ids: deviceIds, title: $("title").value.trim() || "Test notification", body: $("body").value, delay_seconds: delay, via, style }),
    });
  } catch (error) {
    $("send-error").textContent = `Could not send: ${error.message}`;
  }
  $("send-button").disabled = false;
  poll();
}

async function loadDeviceLog() {
  if (!$("device-activity").open) return;
  const { entries } = await api("/api/device-log");
  const names = new Map(knownDevices.map((d) => [d.device_id, d.name]));
  const list = $("device-log");
  list.innerHTML = "";
  if (!entries.length) list.innerHTML = '<li class="empty">No device has reported anything yet.</li>';
  for (const e of entries.slice(0, 100)) {
    const row = document.createElement("li");
    row.innerHTML = `<span class="meta"></span> <span></span>`;
    row.firstChild.textContent = `${time(e.at)} ${names.get(e.device_id) || e.device_id} (${e.source === "worker" ? "background" : "page"})`;
    row.lastChild.textContent = e.message;
    if (e.is_problem) row.lastChild.className = "result-bad";
    list.appendChild(row);
  }
}

$("device-activity").addEventListener("toggle", loadDeviceLog);

let pollTimer = null;
async function poll() {
  clearTimeout(pollTimer);
  let isAnyPending = false;
  try {
    [, isAnyPending] = await Promise.all([loadDevices(), loadSends(), loadDeviceLog()]);
  } catch (error) {
    $("send-error").textContent = error.message;
  }
  pollTimer = setTimeout(poll, isAnyPending ? 1500 : 6000);
}

async function loadChatForwarding() {
  const settings = await api("/api/chat-forwarding");
  $("chat-forwarding-enabled").checked = settings.is_enabled;
  for (const radio of document.querySelectorAll('input[name="chat-via"]')) {
    radio.checked = radio.value === settings.via;
    radio.disabled = !settings.is_enabled;
  }
}

async function saveChatForwarding() {
  const via = document.querySelector('input[name="chat-via"]:checked');
  const settings = { is_enabled: $("chat-forwarding-enabled").checked, via: via ? via.value : "both" };
  try {
    await api("/api/chat-forwarding", { method: "PUT", body: JSON.stringify(settings) });
    $("chat-forwarding-status").textContent = settings.is_enabled ? "Saved. Chat notifications will be copied here." : "Saved. Chat notifications won't be copied.";
  } catch (error) {
    $("chat-forwarding-status").textContent = `Could not save: ${error.message}`;
  }
  loadChatForwarding();
}

const appVersion = document.querySelector('meta[name="app-version"]').content;
const styleNames = new Map();
const styleDescriptions = new Map();

async function loadStyles() {
  const { styles } = await api("/api/styles");
  for (const style of styles) {
    styleNames.set(style.style_id, style.name);
    styleDescriptions.set(style.style_id, style.description);
    const option = document.createElement("option");
    option.value = style.style_id;
    option.textContent = style.name;
    $("style").appendChild(option);
  }
  showStyleDescription();
}

function showStyleDescription() {
  $("style-description").textContent = styleDescriptions.get($("style").value) || "";
}

$("style").addEventListener("change", showStyleDescription);
loadStyles();

$("chat-forwarding-enabled").addEventListener("change", saveChatForwarding);
for (const radio of document.querySelectorAll('input[name="chat-via"]')) radio.addEventListener("change", saveChatForwarding);
loadChatForwarding();

$("send-button").addEventListener("click", send);
poll();
