// The phone page: signs this browser up for pushes, shows its notification state, and lists what it received.

const $ = (id) => document.getElementById(id);
const DEVICE_ID_KEY = "notification-tester-device-id";
const DEVICE_NAME_KEY = "notification-tester-device-name";
const LOCAL_TESTS_KEY = "notification-tester-local-tests";

const deviceId = localStorage.getItem(DEVICE_ID_KEY) || crypto.randomUUID();
localStorage.setItem(DEVICE_ID_KEY, deviceId);

// Chromium-family browsers all say "Chrome" in the user agent; their brand list tells Google Chrome,
// Edge and Brave apart from a plain Chromium build (which usually has no push service).
function detectBrowser() {
  const ua = navigator.userAgent;
  if (/Firefox\//.test(ua)) return "Firefox";
  if (/Edg\//.test(ua)) return "Edge";
  if (/Chrome\//.test(ua)) {
    if (navigator.brave) return "Brave";
    const brands = (navigator.userAgentData && navigator.userAgentData.brands || []).map((b) => b.brand);
    if (brands.includes("Google Chrome")) return "Chrome";
    if (brands.includes("Microsoft Edge")) return "Edge";
    if (brands.includes("Opera")) return "Opera";
    return brands.length ? "Chromium" : "Chrome";
  }
  if (/Safari\//.test(ua)) return "Safari";
  return "Browser";
}

const PLATFORM_NAMES = { android: "Android", ios: "iOS", mac: "Mac", windows: "Windows", linux: "Linux", other: "Device" };

function detectPlatform() {
  const ua = navigator.userAgent;
  return /Android/.test(ua) ? "android" : /iPhone|iPad/.test(ua) ? "ios" : /Mac OS X/.test(ua) ? "mac" : /Windows/.test(ua) ? "windows" : /Linux/.test(ua) ? "linux" : "other";
}

function defaultDeviceName() {
  return `${PLATFORM_NAMES[detectPlatform()]} - ${detectBrowser()}`;
}

$("device-name").value = localStorage.getItem(DEVICE_NAME_KEY) || defaultDeviceName();

function logEvent(text, isProblem = false) {
  // Also kept on the server, so the device's activity can be read from anywhere.
  fetch("/api/device-log", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ device_id: deviceId, source: "page", message: `[${document.querySelector('meta[name="app-version"]').content}] ${text}`, is_problem: isProblem }),
  }).catch(() => { /* offline; the list below still shows it */ });
  const item = document.createElement("li");
  const time = new Date().toLocaleTimeString();
  item.innerHTML = `<span class="meta">${time}</span> `;
  const message = document.createElement("span");
  message.textContent = text;
  if (isProblem) message.className = "result-bad";
  item.appendChild(message);
  $("events").prepend(item);
}

function setStatus(id, dotClass, text) {
  $(id).innerHTML = `<span class="dot ${dotClass}"></span>`;
  $(id).appendChild(document.createTextNode(text));
}

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

function urlBase64ToBytes(base64) {
  const padded = (base64 + "=".repeat((4 - (base64.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  return Uint8Array.from(atob(padded), (c) => c.charCodeAt(0));
}

let registration = null;
let signedUpDevice = null;

async function registerWorker() {
  if (!("serviceWorker" in navigator)) {
    logEvent("This browser has no service workers, so it cannot receive pushes.", true);
    return null;
  }
  try {
    registration = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    await navigator.serviceWorker.ready;
    return registration;
  } catch (error) {
    logEvent(`Could not install the background script: ${error.message}`, true);
    return null;
  }
}

function isInstalledAsApp() {
  return ["standalone", "fullscreen", "minimal-ui"].some((mode) => matchMedia(`(display-mode: ${mode})`).matches);
}

async function refreshStatus() {
  const hasNotifications = "Notification" in window;
  const permission = hasNotifications ? Notification.permission : "unsupported";
  if (permission === "granted") setStatus("status-permission", "ok", "Allowed");
  else if (permission === "denied") setStatus("status-permission", "bad", "Blocked");
  else if (permission === "default") setStatus("status-permission", "warn", "Not asked yet");
  else setStatus("status-permission", "bad", "This browser has no notifications");

  const maxActions = hasNotifications && Notification.maxActions !== undefined ? Notification.maxActions : null;
  setStatus("status-buttons", maxActions ? "ok" : maxActions === 0 ? "bad" : "off",
    maxActions === null ? "This browser doesn't say; send a Yes / No to find out" : maxActions === 0 ? "None" : `Up to ${maxActions}`);

  const isInstalled = isInstalledAsApp();
  setStatus("status-installed", isInstalled ? "ok" : "warn", isInstalled ? "Yes, opened as an app" : "No, running in a browser tab");

  let subscription = null;
  if (registration && "PushManager" in window) subscription = await registration.pushManager.getSubscription();
  try {
    const { devices } = await api("/api/devices");
    signedUpDevice = devices.find((d) => d.device_id === deviceId) || null;
  } catch (error) {
    logEvent(`Could not reach the server: ${error.message}`, true);
  }
  if (!("PushManager" in window)) setStatus("status-push", "bad", "This browser has no push support");
  else if (subscription && signedUpDevice && signedUpDevice.status === "active" && signedUpDevice.subscription.endpoint === subscription.endpoint)
    setStatus("status-push", "ok", `Signed up through ${signedUpDevice.push_service}`);
  else if (signedUpDevice && signedUpDevice.status === "expired") setStatus("status-push", "bad", "Sign-up expired. Tap Allow notifications again.");
  else if (subscription) setStatus("status-push", "warn", "Subscribed, but the server doesn't know this device. Tap Allow notifications.");
  else setStatus("status-push", "off", "Not signed up");

  $("sign-out-button").hidden = !(subscription || signedUpDevice);
  $("allow-button").textContent = permission === "denied" ? "Notifications blocked: how to fix" : subscription && signedUpDevice ? "Sign up again" : "Allow notifications";
  $("allow-button").classList.toggle("big", !(subscription && signedUpDevice) && permission !== "denied");
  $("install-button").hidden = isInstalled || !installPrompt;

  const hint = $("hint");
  const tips = setupTips(permission, isInstalled);
  hint.textContent = tips.join(" ");
  hint.hidden = tips.length === 0;
}

function browserAndPlatform() {
  return { browser: detectBrowser(), platform: detectPlatform() };
}

const CHROMIUM_FAMILY = ["Chrome", "Edge", "Chromium", "Brave", "Opera"];

// Where to turn notifications back on once they are blocked: the browser never asks again, and an
// installed app's window has no address bar, so each browser and window kind gets its own directions.
function blockedFix(browser, platform, isInstalled) {
  const reload = platform === "mac" ? "reload (Cmd+R)" : "reload";
  if (platform === "android") {
    return browser === "Firefox"
      ? "Notifications are blocked for this site. Open it in a Firefox tab, tap the lock icon in the address bar, allow notifications, then reload here."
      : "Notifications are blocked for this site. Open it in a browser tab, tap the icon at the left of the address bar, allow notifications, then reload here.";
  }
  if (CHROMIUM_FAMILY.includes(browser) && isInstalled) {
    return `Notifications are blocked for this app, and ${browser} won't ask again. Open the app's menu (the three dots at the top right of this window), choose App info, and turn Notifications on. Or open this page in a normal ${browser} tab and use the icon at the left of the address bar: the app shares that setting. Then ${reload}.`;
  }
  if (browser === "Safari") {
    return isInstalled
      ? `Notifications are blocked for this app. Open System Settings, then Notifications, find this app, and turn on Allow notifications. Then ${reload}.`
      : `Notifications are blocked for this site. In Safari, open Settings, then Websites, then Notifications, set this site to Allow, then ${reload}.`;
  }
  return `Notifications are blocked for this site, and ${browser} won't ask again. Click the icon at the left of the address bar, turn Notifications on, then ${reload}.`;
}

function setupTips(permission, isInstalled) {
  const { browser, platform } = browserAndPlatform();
  if (window.self !== window.top) {
    return ["This page is open inside your workspace window, which may not be able to show notifications. Copy this page's address into a normal browser tab and use it there."];
  }
  const tips = [];
  if (permission === "default") tips.push("Next: click Allow notifications below, then accept the browser's prompt.");
  if (permission === "denied") tips.push(blockedFix(browser, platform, isInstalled));
  if (platform === "mac") {
    if (permission === "granted") tips.push(`If allowed notifications still don't appear, open System Settings, then Notifications, then ${browser}${isInstalled ? " (or this app's own entry)" : ""}, and turn on Allow notifications. Focus modes can hide them too.`);
    if (permission !== "denied" && !isInstalled && !installPrompt && CHROMIUM_FAMILY.includes(browser)) tips.push("Optional: install it as an app with the install icon at the right of the address bar.");
    if (permission !== "denied" && !isInstalled && browser === "Safari") tips.push("Optional: choose File, then Add to Dock, to use it as an app.");
  } else if (platform === "android" && !isInstalled) {
    tips.push("For the most realistic test, add this page to your Home screen from the browser menu (Firefox: menu, then Add app to Home screen) and open it from that icon.");
  } else if (platform === "ios" && !isInstalled) {
    tips.push("On iPhone, notifications only work after you tap Share, then Add to Home Screen, and open the app from that icon.");
  }
  return tips;
}

async function allowNotifications() {
  if (!("Notification" in window)) return logEvent("This browser has no notifications.", true);
  if (Notification.permission === "denied") {
    logEvent(blockedFix(detectBrowser(), detectPlatform(), isInstalledAsApp()), true);
    $("hint").scrollIntoView({ behavior: "smooth", block: "center" });
    return;
  }
  logEvent("Asking for permission...");
  const result = await Notification.requestPermission();
  logEvent(`Permission answer: ${result}`, result !== "granted");
  if (result !== "granted") return refreshStatus();
  if (!registration && !(await registerWorker())) return refreshStatus();
  try {
    const { public_key } = await api("/api/vapid-public-key");
    let subscription = await registration.pushManager.getSubscription();
    if (subscription) {
      const existingKey = subscription.options && subscription.options.applicationServerKey;
      const matches = existingKey && btoa(String.fromCharCode(...new Uint8Array(existingKey))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "") === public_key;
      if (!matches) { await subscription.unsubscribe(); subscription = null; logEvent("Dropped an old push sign-up made with a different key."); }
    }
    if (!subscription) {
      logEvent("Signing up with the browser's push service...");
      subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToBytes(public_key) });
    }
    const json = subscription.toJSON();
    const name = $("device-name").value.trim() || defaultDeviceName();
    localStorage.setItem(DEVICE_NAME_KEY, name);
    signedUpDevice = await api("/api/devices", {
      method: "POST",
      body: JSON.stringify({ device_id: deviceId, name, user_agent: navigator.userAgent, subscription: { endpoint: json.endpoint, keys: json.keys } }),
    });
    logEvent(`Signed up through ${signedUpDevice.push_service}. This device now shows on the Send page.`);
  } catch (error) {
    logEvent(`Sign-up failed: ${error.message}`, true);
    if (detectBrowser() === "Chromium") {
      logEvent("This Chromium build has no connection to a push service, so it cannot receive pushes. Test on this device only still works; for pushes use Google Chrome, Edge, Firefox or Safari.", true);
    }
  }
  refreshStatus();
}

async function localTest() {
  if (!("Notification" in window) || Notification.permission !== "granted") return logEvent("Allow notifications first.", true);
  if (!registration && !(await registerWorker())) return;
  try {
    await registration.showNotification("Local test", { body: "Shown by this device, without the server", icon: "/static/icon-192.png", tag: "local-test" });
    const tests = JSON.parse(localStorage.getItem(LOCAL_TESTS_KEY) || "[]");
    tests.unshift(new Date().toISOString());
    localStorage.setItem(LOCAL_TESTS_KEY, JSON.stringify(tests.slice(0, 20)));
    logEvent("Showed a local test notification.");
    renderReceived();
  } catch (error) {
    logEvent(`Local test failed: ${error.message}`, true);
  }
}

async function signOut() {
  try {
    const subscription = registration && (await registration.pushManager.getSubscription());
    if (subscription) await subscription.unsubscribe();
    if (signedUpDevice) await api(`/api/devices/${encodeURIComponent(deviceId)}`, { method: "DELETE" });
    signedUpDevice = null;
    logEvent("Signed this device out.");
  } catch (error) {
    logEvent(`Sign-out failed: ${error.message}`, true);
  }
  refreshStatus();
}

async function renameDevice() {
  const name = $("device-name").value.trim();
  if (!name) return;
  localStorage.setItem(DEVICE_NAME_KEY, name);
  if (!signedUpDevice) return;
  try {
    signedUpDevice = await api(`/api/devices/${encodeURIComponent(deviceId)}`, { method: "PATCH", body: JSON.stringify({ name }) });
    logEvent(`Renamed this device to "${name}".`);
  } catch (error) {
    logEvent(`Rename failed: ${error.message}`, true);
  }
}

function describeInteraction(i) {
  const at = formatTime(i.at);
  if (i.kind === "reply") return `replied "${i.reply}" at ${at}`;
  if (i.kind === "action") return `tapped ${i.action} at ${at}`;
  if (i.kind === "clicked") return `tapped it at ${at}`;
  return `dismissed at ${at}`;
}

function formatTime(iso) {
  return iso ? new Date(iso).toLocaleTimeString() : "";
}

async function renderReceived() {
  let sends = [];
  try {
    ({ sends } = await api(`/api/sends?device_id=${encodeURIComponent(deviceId)}`));
  } catch (error) {
    return;
  }
  const local = JSON.parse(localStorage.getItem(LOCAL_TESTS_KEY) || "[]").map((at) => ({ local: true, at }));
  const items = [...sends.map((s) => ({ send: s, at: s.created_at })), ...local].sort((a, b) => (a.at < b.at ? 1 : -1));
  const list = $("received");
  list.innerHTML = "";
  if (!items.length) {
    list.innerHTML = '<li class="empty">Nothing sent to this device yet. Open the Send page on another screen and send one.</li>';
    return;
  }
  for (const item of items.slice(0, 50)) {
    const row = document.createElement("li");
    if (item.local) {
      row.innerHTML = `<span class="tag local">This device</span><strong>Local test</strong> - shown without the server<div class="meta">${formatTime(item.at)}</div>`;
    } else {
      const s = item.send;
      row.id = `send-${s.send_id}`;
      const state = s.shown_at
        ? `<span class="result-ok">shown ${formatTime(s.shown_at)}</span>${s.clicked_at ? `, tapped ${formatTime(s.clicked_at)}` : ""}`
        : s.status === "accepted"
          ? `<span class="result-wait">${s.via === "live" ? "handed to this page" : "accepted by the push service"}, not shown yet</span>`
          : `<span class="${s.status === "waiting" || s.status === "sending" ? "result-wait" : "result-bad"}"></span>`;
      const viaLabel = s.via === "live" ? "Live" : "Push";
      const fromChat = s.source === "chat" ? '<span class="tag chat">From chat</span>' : "";
      row.innerHTML = `${fromChat}<span class="tag ${s.via}">${viaLabel}</span><strong></strong> - <span class="body"></span><div class="meta">sent ${formatTime(s.sent_at || s.created_at)}${s.delay_seconds ? ` (${s.delay_seconds} s delay)` : ""} - ${state}</div>`;
      row.querySelector("strong").textContent = s.title;
      row.querySelector(".body").textContent = s.body;
      if (s.style && s.style !== "plain") row.insertAdjacentHTML("afterbegin", `<span class="tag style">${s.style}</span>`);
      if (s.interactions && s.interactions.length) {
        const done = document.createElement("div");
        done.className = "meta";
        done.textContent = s.interactions.map(describeInteraction).join(", ");
        row.appendChild(done);
      }
      const empty = row.querySelector(".meta span:empty");
      if (empty) empty.textContent = s.status_detail;
    }
    list.appendChild(row);
  }
}


// Live mode: while this page is open it holds a stream to the server, and a live send arrives on it
// directly, with no push service involved. The page shows the notification itself.
let liveSource = null;

function connectLive() {
  if (liveSource) liveSource.close();
  const name = $("device-name").value.trim() || defaultDeviceName();
  liveSource = new EventSource(`/api/live/${encodeURIComponent(deviceId)}/events?name=${encodeURIComponent(name)}`, { withCredentials: true });
  setStatus("status-live", "warn", "Connecting...");
  liveSource.addEventListener("hello", () => {
    setStatus("status-live", "ok", "Connected");
  });
  liveSource.addEventListener("notification", (event) => showLiveNotification(JSON.parse(event.data)));
  liveSource.onerror = () => {
    setStatus("status-live", liveSource.readyState === EventSource.CLOSED ? "bad" : "warn",
      liveSource.readyState === EventSource.CLOSED ? "Disconnected. Reload to reconnect." : "Reconnecting...");
  };
}

async function showLiveNotification(data) {
  if (!data.is_update) logEvent(`A live notification arrived: "${data.title}"`);
  if (!("Notification" in window) || Notification.permission !== "granted") {
    logEvent("Could not show it: notifications are not allowed on this device.", true);
    return renderReceived();
  }
  try {
    const worker = registration || (await registerWorker());
    if (worker && worker.active) {
      // The background script shows it, so buttons, styles and reports work the same as for a push.
      worker.active.postMessage({ kind: "show-live", payload: data });
    } else {
      new Notification(data.title, { body: data.body || "", icon: "/static/icon-192.png", tag: data.send_id });
      await api(`/api/sends/${encodeURIComponent(data.send_id)}/receipt`, { method: "POST", body: JSON.stringify({ device_id: deviceId, event: "shown" }) });
    }
  } catch (error) {
    logEvent(`Could not show the live notification: ${error.message}`, true);
  }
  setTimeout(renderReceived, 800);
}

let installPrompt = null;
window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  installPrompt = event;
  refreshStatus();
});
window.addEventListener("appinstalled", () => {
  installPrompt = null;
  logEvent("Installed as an app. Open it from its own icon.");
  refreshStatus();
});
$("install-button").addEventListener("click", async () => {
  if (!installPrompt) return;
  installPrompt.prompt();
  const { outcome } = await installPrompt.userChoice;
  logEvent(`Install answer: ${outcome}`);
  installPrompt = null;
  refreshStatus();
});

$("allow-button").addEventListener("click", allowNotifications);
$("local-test-button").addEventListener("click", localTest);
$("sign-out-button").addEventListener("click", signOut);
$("device-name").addEventListener("change", () => { renameDevice(); connectLive(); });

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.addEventListener("message", (event) => {
    if (event.data && event.data.kind === "push-received") logEvent("A push arrived while this page was open.");
    if (event.data && event.data.kind === "notification-clicked") logEvent(event.data.action ? `A notification's "${event.data.action}" button was tapped.` : "A notification was tapped.");
    renderReceived();
  });
}
if (navigator.permissions && navigator.permissions.query) {
  navigator.permissions.query({ name: "notifications" }).then((status) => {
    status.onchange = () => {
      logEvent(`Notification permission changed to: ${Notification.permission}`);
      refreshStatus();
    };
  }).catch(() => { /* this browser cannot report permission changes */ });
}
window.addEventListener("focus", () => refreshStatus());
document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  clearBadge();
  refreshStatus();
  renderReceived();
  if (!liveSource || liveSource.readyState === EventSource.CLOSED) connectLive();
});
setInterval(() => { if (!document.hidden) renderReceived(); }, 4000);

// The page and the background script each carry the version they were served with. After a change
// the browser can keep running the old background script for a while, so the page checks and, when
// it is behind, fetches the new one and reloads once.
const PAGE_VERSION = document.querySelector('meta[name="app-version"]').content;
const RELOADED_FOR_KEY = "notification-tester-reloaded-for";

function workerVersion(worker) {
  return new Promise((resolve) => {
    const channel = new MessageChannel();
    const timer = setTimeout(() => resolve(null), 1500);
    channel.port1.onmessage = (event) => { clearTimeout(timer); resolve(event.data.version); };
    worker.postMessage({ kind: "version?" }, [channel.port2]);
  });
}

async function checkVersion() {
  const { app_version: serverVersion } = await api("/api/version");
  if (PAGE_VERSION !== serverVersion) {
    setStatus("status-version", "warn", "Outdated page, reloading...");
    if (sessionStorage.getItem(RELOADED_FOR_KEY) !== serverVersion) {
      sessionStorage.setItem(RELOADED_FOR_KEY, serverVersion);
      location.reload();
    }
    return;
  }
  const worker = registration && registration.active;
  if (worker) worker.postMessage({ kind: "hello", device_id: deviceId });
  if (!worker) return setStatus("status-version", "off", `${serverVersion} (background script not installed yet)`);
  const running = await workerVersion(worker);
  if (running === serverVersion) return setStatus("status-version", "ok", `${serverVersion}, up to date`);
  setStatus("status-version", "warn", `Background script is ${running || "an old version"}; updating...`);
  await registration.update();
  navigator.serviceWorker.addEventListener("controllerchange", () => checkVersion(), { once: true });
  setTimeout(checkVersion, 3000);
}

async function clearBadge(isAsked = false) {
  if (!navigator.clearAppBadge) {
    if (isAsked) logEvent("This browser has no app badge, so there is nothing to clear.");
    return;
  }
  try {
    await navigator.clearAppBadge();
    if (isAsked) logEvent("Cleared the app badge.");
  } catch (error) {
    if (isAsked) logEvent(`Could not clear the app badge: ${error.message}`, true);
  }
}

// Seeing the app counts as reading what the badge counted.
clearBadge();
$("clear-badge-button").addEventListener("click", () => clearBadge(true));

(async () => {
  if ("serviceWorker" in navigator) {
    registration = (await navigator.serviceWorker.getRegistration("/")) || null;
    if (registration || (window.Notification && Notification.permission === "granted")) await registerWorker();
  }
  await refreshStatus();
  renderReceived();
  connectLive();
  checkVersion().catch((error) => setStatus("status-version", "bad", `Could not check: ${error.message}`));
})();
