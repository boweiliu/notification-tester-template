"""The device, send and reply pages in a real browser, against a throwaway server over fresh state.

The browser here has no push service, so the device page's sign-up is given a stand-in subscription whose endpoint
is the local stand-in push service; nothing is ever sent to a real device.
"""

import json
import threading
from collections.abc import Iterator

import pytest
from playwright.sync_api import Browser
from playwright.sync_api import BrowserContext
from playwright.sync_api import Page
from playwright.sync_api import expect
from werkzeug.serving import make_server

from notification_tester import runner
from notification_tester.store import Device
from notification_tester.store import SendRecord

pytestmark = [pytest.mark.browser, pytest.mark.timeout(120, func_only=False)]

DEVICE_ID_KEY = "notification-tester-device-id"

# Stands in for the browser's push sign-up: answers with the subscription the test made, and notes the key the
# page subscribed with.
FAKE_PUSH_MANAGER = """
(() => {
  const fake = %s;
  let current = null;
  const asSubscription = (key) => ({
    endpoint: fake.endpoint,
    options: { applicationServerKey: key },
    toJSON: () => ({ endpoint: fake.endpoint, expirationTime: null, keys: fake.keys }),
    unsubscribe: async () => { current = null; return true; },
  });
  PushManager.prototype.subscribe = async function (options) {
    const key = new Uint8Array(options.applicationServerKey).buffer;
    window.__subscribedWithKey = btoa(String.fromCharCode(...new Uint8Array(key)))
      .replace(/\\+/g, "-").replace(/\\//g, "_").replace(/=+$/, "");
    current = asSubscription(key);
    return current;
  };
  PushManager.prototype.getSubscription = async function () { return current; };
})();
"""


@pytest.fixture
def origin(app_state) -> Iterator[str]:
    """The app, served on a free loopback port over this test's fresh state."""
    server = make_server("127.0.0.1", 0, runner.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def context(module_browser: Browser, origin: str) -> Iterator[BrowserContext]:
    """A browser profile that has allowed this app's notifications."""
    context = module_browser.new_context(viewport={"width": 900, "height": 1000})
    context.grant_permissions(["notifications"], origin=origin)
    try:
        yield context
    finally:
        context.close()


def open_device_page(context: BrowserContext, origin: str) -> tuple[Page, str]:
    """The device page, its background script running and its live connection up; answers the page's device id."""
    page = context.new_page()
    page.goto(origin + "/")
    expect(page.locator("#status-live")).to_have_text("Connected")
    expect(page.locator("#status-version")).to_have_text(f"{runner.APP_VERSION}, up to date")
    return page, page.evaluate(f"localStorage.getItem({json.dumps(DEVICE_ID_KEY)})")


def a_send(app_state, **fields: object) -> SendRecord:
    record = SendRecord.model_validate(
        {"device_id": "phone", "device_name": "Phone", "via": "push", "title": "Hello", "body": "From the test",
         "delay_seconds": 0, "created_at": runner.now_iso(), "status": "accepted", **fields}
    )
    app_state.store.put_send(record)
    return record


def a_device(app_state, device_id: str, name: str, **fields: object) -> Device:
    device = Device.model_validate(
        {"device_id": device_id, "name": name, "user_agent": "pytest", "push_service": "Mozilla",
         "subscription": {"endpoint": f"http://127.0.0.1:9/push/{device_id}", "keys": {"p256dh": "k", "auth": "a"}},
         "signed_up_at": runner.now_iso(), "status": "active", **fields}
    )
    app_state.store.put_device(device)
    return device


def test_a_device_signs_up_with_the_apps_key_and_shows_on_the_send_page(context, origin, app_state, push_service, make_subscription) -> None:
    subscription = make_subscription(push_service.endpoint("browser"))
    context.add_init_script(FAKE_PUSH_MANAGER % json.dumps(subscription.as_json()))
    page, device_id = open_device_page(context, origin)
    expect(page.locator("#status-push")).to_have_text("Not signed up")
    expect(page.locator("#status-permission")).to_have_text("Allowed")

    page.fill("#device-name", "Test laptop")
    page.click("#allow-button")

    expect(page.locator("#status-push")).to_have_text("Signed up through 127.0.0.1")
    expect(page.locator("#allow-button")).to_have_text("Sign up again")
    expect(page.locator("#events")).to_contain_text("Signed up through 127.0.0.1. This device now shows on the Send page.")
    assert page.evaluate("window.__subscribedWithKey") == app_state.vapid_keys.public_key
    device = app_state.store.device(device_id)
    assert device is not None and device.name == "Test laptop" and device.status == "active"
    assert device.subscription.model_dump() == subscription.as_json()

    sender = context.new_page()
    sender.goto(origin + "/send")
    row = sender.locator(f"#device-{device_id}")
    expect(row).to_contain_text("Test laptop")
    expect(row).to_contain_text("Push through 127.0.0.1 - signed up just now")
    expect(row.locator(".live-state")).to_have_text("Live: connected")
    expect(row.locator("input[type=checkbox]")).to_be_checked()
    assert push_service.received == []


def test_a_live_send_is_shown_by_the_background_script_and_confirmed(context, origin, app_state) -> None:
    device_page, device_id = open_device_page(context, origin)
    sender = context.new_page()
    sender.goto(origin + "/send")
    row = sender.locator(f"#device-{device_id}")
    expect(row).to_contain_text("Not signed up for pushes - live only")
    expect(row.locator("input[type=checkbox]")).not_to_be_checked()

    row.locator("input[type=checkbox]").check()
    sender.click("#delivery label:has-text('Live')")
    sender.select_option("#style", "yes-no")
    expect(sender.locator("#style-description")).to_contain_text("Two buttons")
    sender.fill("#title", "Live check")
    sender.click("#send-button")

    sent = sender.locator("#sent li").first
    expect(sent.locator("strong")).to_have_text("Live check")
    expect(sent.locator(".tag.live")).to_have_text("Live")
    expect(sent.locator(".tag.style")).to_have_text("Yes / No")
    expect(sent).to_contain_text("Handed to the open page")
    expect(sent).to_contain_text("device showed it at", timeout=15000)
    (record,) = app_state.store.sends()
    assert (record.via, record.style, record.status, record.title) == ("live", "yes-no", "accepted", "Live check")
    assert record.shown_at is not None and record.device_app_version == runner.APP_VERSION
    expect(device_page.locator(f"#send-{record.send_id}")).to_contain_text("shown")
    expect(device_page.locator("#events")).to_contain_text('A live notification arrived: "Live check"')


def test_the_send_page_shows_devices_receipts_older_versions_and_raw_records(context, origin, app_state) -> None:
    a_device(app_state, "phone", "Phone")
    a_device(app_state, "old-tablet", "Old tablet", status="expired", status_detail="Mozilla says this device is no longer signed up (410)")
    pushed = a_send(
        app_state, send_id="pushed1", style="yes-no", source="chat", shown_at=runner.now_iso(), device_max_actions=2,
        device_app_version="old12345", sent_at=runner.now_iso(), status_detail="Mozilla accepted it (201)",
        raw_response={"url": "http://127.0.0.1:9/push/phone", "status_code": 201, "headers": {"Location": "x"}, "body": ""},
        interactions=[{"at": runner.now_iso(), "kind": "action", "action": "yes"}, {"at": runner.now_iso(), "kind": "reply", "action": "reply", "reply": "On my way"}],
    )
    a_send(app_state, send_id="live1", via="live", status="failed", status_detail="The device's page isn't open, so there was no live connection")
    page = context.new_page()

    page.goto(origin + "/send")

    expect(page.locator("#device-phone input[type=checkbox]")).to_be_checked()
    expect(page.locator("#device-old-tablet .meta.result-bad")).to_have_text("Mozilla says this device is no longer signed up (410)")
    expect(page.locator("#device-old-tablet input[type=checkbox]")).not_to_be_checked()
    push_row = page.locator("#send-pushed1")
    expect(push_row.locator(".tag.chat")).to_have_text("From chat")
    expect(push_row.locator(".verdict.result-ok")).to_have_text("Mozilla accepted it (201)")
    expect(push_row).to_contain_text("device showed it at")
    expect(push_row.locator(".interactions")).to_contain_text(
        'shown by an older app version (old12345) - device shows up to 2 buttons - tapped "yes" at'
    )
    expect(push_row.locator(".interactions")).to_contain_text('replied "On my way" at')
    live_row = page.locator("#send-live1")
    expect(live_row.locator(".verdict.result-bad")).to_have_text("The device's page isn't open, so there was no live connection")
    expect(live_row.locator(".raw-link")).to_have_text("view raw record")

    push_row.locator(".raw-link", has_text="view raw response").click()
    expect(page.locator("h1")).to_have_text("Raw record")
    assert json.loads(page.locator("pre.raw").inner_text()) == pushed.model_dump(mode="json")


def test_the_send_page_explains_empty_states_and_saves_chat_forwarding(context, origin, app_state) -> None:
    page = context.new_page()
    page.goto(origin + "/send")

    expect(page.locator("#devices")).to_have_text("No devices yet. Open this app on your phone and tap Allow notifications.")
    expect(page.locator("#sent")).to_have_text("Nothing sent yet.")
    expect(page.locator("#chat-forwarding-enabled")).to_be_checked()
    expect(page.locator("#chat-forwarding-via input[value=both]")).to_be_checked()
    page.click("#send-button")
    expect(page.locator("#send-error")).to_have_text("Pick at least one device.")

    page.uncheck("#chat-forwarding-enabled")

    expect(page.locator("#chat-forwarding-status")).to_have_text("Saved. Chat notifications won't be copied.")
    expect(page.locator("#chat-forwarding-via input[value=live]")).to_be_disabled()
    assert app_state.store.chat_forwarding().is_enabled is False
    page.click("#device-activity summary")
    expect(page.locator("#device-log")).to_contain_text("No device has reported anything yet.")


def test_a_device_running_an_old_background_script_updates_itself(context, origin, monkeypatch) -> None:
    page, _ = open_device_page(context, origin)
    monkeypatch.setattr(runner, "APP_VERSION", "newer123")

    page.reload()

    # The reloaded page carries the new version but the old background script is still running: it fetches the new
    # one, and reports up to date once that one has taken over.
    expect(page.locator("meta[name=app-version]")).to_have_attribute("content", "newer123")
    expect(page.locator("#status-version")).to_have_text("newer123, up to date", timeout=20000)
    version = page.evaluate(
        """() => new Promise((resolve) => {
          const channel = new MessageChannel();
          channel.port1.onmessage = (event) => resolve(event.data.version);
          navigator.serviceWorker.controller.postMessage({ kind: "version?" }, [channel.port2]);
        })"""
    )
    assert version == "newer123"


def test_blocked_notifications_get_directions_instead_of_another_prompt(module_browser: Browser, origin: str) -> None:
    context = module_browser.new_context()
    context.add_init_script("Object.defineProperty(Notification, 'permission', { get: () => 'denied' });")
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(origin + "/")
    expect(page.locator("#status-permission")).to_have_text("Blocked")
    expect(page.locator("#allow-button")).to_have_text("Notifications blocked: how to fix")

    page.click("#allow-button")

    expect(page.locator("#events .result-bad").first).to_contain_text("Notifications are blocked for this site")
    assert errors == []
    context.close()


def test_the_reply_page_sends_the_typed_answer_back(context, origin, app_state) -> None:
    record = a_send(app_state, send_id="ask1", style="reply-page", title="Lunch?", body="Where shall we go")
    page = context.new_page()

    page.goto(origin + f"/reply?send_id={record.send_id}&device_id=phone")

    expect(page.locator("#original")).to_have_text("Replying to: Lunch? - Where shall we go")
    page.click("#reply-button")
    expect(page.locator("#reply-status")).to_have_text("Type a reply first.")
    page.fill("#reply-text", "The noodle place")
    page.click("#reply-button")
    expect(page.locator("#reply-status")).to_have_text("Reply sent. It shows on the Send page.")
    expect(page.locator("#reply-button")).to_be_disabled()
    saved = app_state.store.send("ask1")
    assert saved is not None and [(i.kind, i.reply) for i in saved.interactions] == [("reply", "The noodle place")]
    assert saved.clicked_at is not None
