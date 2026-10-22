// The page a notification's Reply button opens: shows what it answers, and sends the typed reply back.

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const sendId = params.get("send_id") || "";
const deviceId = params.get("device_id") || localStorage.getItem("notification-tester-device-id") || "";

async function loadOriginal() {
  const response = await fetch(`/api/sends?device_id=${encodeURIComponent(deviceId)}`, { credentials: "include" });
  if (!response.ok) return ($("original").textContent = "Could not load the notification.");
  const { sends } = await response.json();
  const send = sends.find((s) => s.send_id === sendId);
  if (!send) return ($("original").textContent = "That notification is no longer listed.");
  $("original").textContent = "";
  const title = document.createElement("strong");
  title.textContent = send.title;
  $("original").append("Replying to: ", title, ` - ${send.body}`);
}

$("reply-button").addEventListener("click", async () => {
  const reply = $("reply-text").value.trim();
  if (!reply) return ($("reply-status").textContent = "Type a reply first.");
  $("reply-button").disabled = true;
  const response = await fetch(`/api/sends/${encodeURIComponent(sendId)}/receipt`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ device_id: deviceId, event: "reply", action: "reply", reply }),
  });
  $("reply-status").textContent = response.ok ? "Reply sent. It shows on the Send page." : `Could not send the reply (${response.status}).`;
  $("reply-button").disabled = response.ok;
});

loadOriginal();
