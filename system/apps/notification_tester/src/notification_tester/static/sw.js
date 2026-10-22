// The service worker: the browser wakes it when a push arrives. It shows the notification in the
// style the send asked for, tells the server it appeared, and reports what was done with it:
// a tap, a button, a typed reply, or a dismissal.

// Filled in by the server with a fingerprint of the app's browser-side files.
const APP_VERSION = "__APP_VERSION__";

// The device id, learned from the page or from a notification; the log uses it.
let knownDeviceId = "unknown";

async function log(message, isProblem = false, deviceId = null) {
  try {
    await fetch("/api/device-log", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ device_id: deviceId || knownDeviceId, source: "worker", message: `[${APP_VERSION}] ${message}`, is_problem: isProblem }),
    });
  } catch (error) {
    // Nowhere left to report it.
  }
}

self.addEventListener("error", (event) => { log(`Uncaught error: ${event.message}`, true); });
self.addEventListener("unhandledrejection", (event) => { log(`Unhandled failure: ${event.reason && event.reason.message ? event.reason.message : event.reason}`, true); });

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

async function report(data, event, extra = {}) {
  if (!data.send_id || !data.device_id) {
    await log(`Could not report "${event}": the notification carries no send id (data: ${JSON.stringify(data).slice(0, 200)})`, true);
    return;
  }
  knownDeviceId = data.device_id;
  try {
    const response = await fetch(`/api/sends/${encodeURIComponent(data.send_id)}/receipt`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({ device_id: data.device_id, event, ...extra }),
    });
    if (!response.ok) await log(`Reporting "${event}" for ${data.send_id} was refused (${response.status})`, true, data.device_id);
  } catch (error) {
    await log(`Reporting "${event}" for ${data.send_id} failed: ${error.message}`, true, data.device_id);
  }
}

async function tellOpenPages(message) {
  const pages = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const page of pages) page.postMessage(message);
}

// Shared with the device page, which shows live notifications through this same registration.
async function showStyledNotification(data) {
  const options = { body: data.body || "", ...(data.options || {}), data };
  if (!options.tag) options.tag = data.send_id;
  await self.registration.showNotification(data.title || "Notification tester", options);
  if (data.app_badge_count != null) {
    if (!self.navigator.setAppBadge) {
      await log("App badge: this browser has no badge support", false, data.device_id);
    } else {
      try {
        await self.navigator.setAppBadge(data.app_badge_count);
        await log(`App badge: set to ${data.app_badge_count}`, false, data.device_id);
      } catch (error) {
        await log(`App badge: refused (${error.message})`, true, data.device_id);
      }
    }
  }
  if (!data.is_update) {
    await report(data, "shown", {
      max_actions: typeof Notification !== "undefined" && Notification.maxActions !== undefined ? Notification.maxActions : null,
      app_version: APP_VERSION,
    });
  }
}

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (error) {
    data = { title: "Notification tester", body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(
    (async () => {
      try {
        await showStyledNotification(data);
      } catch (error) {
        await log(`Could not show push ${data.send_id}: ${error.message}`, true, data.device_id);
      }
      await tellOpenPages({ kind: "push-received", send_id: data.send_id });
    })(),
  );
});

self.addEventListener("message", (event) => {
  if (event.data && event.data.kind === "hello" && event.data.device_id) knownDeviceId = event.data.device_id;
  if (event.data && event.data.kind === "show-live") {
    const payload = event.data.payload;
    event.waitUntil(showStyledNotification(payload).catch((error) => log(`Could not show live ${payload.send_id}: ${error.message}`, true, payload.device_id)));
  }
  if (event.data && event.data.kind === "version?" && event.ports[0]) event.ports[0].postMessage({ version: APP_VERSION });
});

async function openPage(path) {
  const pages = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  for (const page of pages) {
    if ("navigate" in page && path !== "/") {
      await page.focus();
      return page.navigate(path);
    }
  }
  if (pages.length && path === "/") return pages[0].focus();
  return self.clients.openWindow(path);
}

self.addEventListener("notificationclick", (event) => {
  const data = event.notification.data || {};
  const action = event.action || "";
  const reply = typeof event.reply === "string" ? event.reply : null;
  event.notification.close();
  event.waitUntil(
    (async () => {
      if (self.navigator.clearAppBadge) await self.navigator.clearAppBadge().catch(() => {});
      await log(`Tap on ${data.send_id || "a notification"}: button "${action || "(body)"}", typed reply ${reply === null ? "not offered" : `"${reply}"`}`, false, data.device_id);
      try {
        await handleClick(data, action);
      } catch (error) {
        await log(`Handling the tap failed: ${error.message}`, true, data.device_id);
      }
    })(),
  );

  async function handleClick(data, action) {
      if (!action) {
        await report(data, "clicked");
        await openPage("/");
      } else if (action === "reply" && typeof event.reply === "string") {
        // A browser that supports typed replies hands the text over here.
        await report(data, "reply", { action, reply: event.reply });
      } else if (action === "reply") {
        await report(data, "action", { action });
        await openPage(`/reply?send_id=${encodeURIComponent(data.send_id || "")}&device_id=${encodeURIComponent(data.device_id || "")}`);
      } else if (action === "open") {
        await report(data, "action", { action });
        await openPage("/");
      } else {
        await report(data, "action", { action });
      }
      await tellOpenPages({ kind: "notification-clicked", send_id: data.send_id, action });
  }
});

self.addEventListener("notificationclose", (event) => {
  const data = event.notification.data || {};
  event.waitUntil((async () => {
    await log(`Dismissed ${data.send_id || "a notification"}`, false, data.device_id);
    await report(data, "dismissed");
  })());
});
