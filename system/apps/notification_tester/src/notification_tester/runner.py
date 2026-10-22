"""Test phone and desktop notifications: sign a device up on one page, send it pushed or live notifications from another.

Services run from /home/user/workspace (the repo root). Conventions:

- Persistent state (anything written and read across runs -- cursors,
  caches, snapshots, user records): read and write it under ``DATA_DIR``
  (defined below), never a hardcoded ``data/.apps/notification-tester/`` at the
  call site. ``DATA_DIR`` defaults to ``data/.apps/notification-tester/`` but
  honors the ``NOTIFICATION_TESTER_DATA_DIR`` env var, so an editing agent can point a
  throwaway instance at a *copy* of the data instead of the live store
  (see the update-app skill). Do NOT use ``Path(__file__)``-based
  paths for state -- the bug to avoid is one process writing to
  ``/home/user/workspace/data/.apps/...`` while another reads from
  ``/home/user/workspace/system/apps/<pkg>/data/...``.
- Static assets shipped alongside this file (templates, default
  configs, bundled JSON): ``Path(__file__).parent / "assets/..."`` is
  fine and is the right pattern.
- Listen port: bind ``PORT`` (defined below), which defaults to this
  app's assigned port but honors the ``NOTIFICATION_TESTER_PORT`` env var, so
  an editing agent can boot a throwaway instance on a *spare* port
  alongside the live one (see the update-app skill). Never hardcode
  the port at the ``run_simple`` call.

This is a synchronous Flask app served by the threaded Werkzeug server.
The app owns its own browser origin (the forwarder routes
``http://notification-tester.<workspace-host>/`` straight to this port), so it serves
at ``/`` and root-absolute URLs, cookies, and service workers all work
unmodified -- nothing rewrites anything. Use ``flask_sock`` if you need
WebSockets.
"""

import base64
import hashlib
import html
import json
import logging
import os
import threading
import uuid
from pathlib import Path
from typing import Literal

from app_schema.api_schema import (
    DEFAULT_API_SCHEMA_PATH,
    RouteModels,
    RouteTable,
    SchemalessRoute,
    register_api_schema_route,
)
from app_schema.routes import error_response, model_response, parse_request_body
from flask import Flask, Response, abort, request, send_file
from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field
from werkzeug.serving import run_simple

from notification_tester.live import LiveDevice, LiveHub
from notification_tester.push import VapidKeys, push_service_name, send_push
from notification_tester.store import (
    ChatForwardingSettings,
    DeliveryChoice,
    Device,
    DeviceLogEntry,
    Interaction,
    PushSubscription,
    SendRecord,
    SendSource,
    SendVia,
    Store,
    now_iso,
)
from notification_tester.styles import ICON, STYLES, STYLES_BY_ID, NotificationStyle

# Persistent state for this app lives under DATA_DIR. It defaults to
# ``data/.apps/notification-tester/`` but is overridable via the ``NOTIFICATION_TESTER_DATA_DIR`` env var
# so a throwaway instance can run against a *copy* of the data while editing --
# see the update-app skill. Always read/write state through DATA_DIR;
# never hardcode ``data/.apps/notification-tester/`` at a call site, or the override is
# bypassed. A writing call site should ``DATA_DIR.mkdir(parents=True,
# exist_ok=True)`` before writing.
DATA_DIR = Path(os.environ.get("NOTIFICATION_TESTER_DATA_DIR", "data/.apps/notification-tester"))

# Listen port. Defaults to this app's assigned port but is overridable via
# the ``NOTIFICATION_TESTER_PORT`` env var so an editing agent can boot a throwaway
# instance on a spare port next to the live one (see the update-app skill).
# Never hardcode the port at the ``run_simple`` call, or the override is bypassed.
PORT = int(os.environ.get("NOTIFICATION_TESTER_PORT", "8080"))

# The browser-side modules the workspace shell builds and every app serves from
# its own origin: the app contract (how a page talks to the shell framing it) and
# the element context menu (the right-click menu whose last rows hand the
# clicked element to a chat). A module import is a fetch without cookies, which
# the forwarder refuses across origins, so they are served here rather than from
# the shell. Relative to the repo root the service runs from, like DATA_DIR.
SHELL_STATIC_MODULES_DIR = Path("system/apps/system_interface/imbue/system_interface/static/_static")
SHELL_STATIC_MODULE_NAMES = ('app_contract.js', 'context_menu.js')

# The script every page serves (keep it on every page): it connects the page to
# the shell, reports where the page is on the handshake so the shell can reopen
# this app's window at the same place, and installs the element context menu.
# A page visited outside the shell runs it harmlessly: nothing arrives, and the
# menu's Explain and Modify rows grey out.
SHELL_PAGE_SCRIPT_BODY = """
  import { connectToShell } from "/_static/app_contract.js";
  import { installElementContextMenu } from "/_static/context_menu.js";
  let handshake = null;
  const connection = connectToShell({
    onHandshake: (received) => {
      handshake = received;
      connection.location(location.pathname + location.search, document.title);
    },
  });
  installElementContextMenu({ connection, handshake: () => handshake });
"""
SHELL_PAGE_SCRIPT = f'<script type="module">{SHELL_PAGE_SCRIPT_BODY}</script>'


def inline_script_source(body: str) -> str:
    """The Content-Security-Policy source admitting one inline <script> whose body is exactly ``body``."""
    return "'sha256-" + base64.b64encode(hashlib.sha256(body.encode()).digest()).decode() + "'"


# Every HTML page is served under this policy (the app is reachable from the
# internet once the workspace is shared): scripts only from this origin plus the
# inline blocks listed here by hash, fetches, sockets and images only from this
# origin, inline styles, frames of any http(s) page, no plugins. Anything else a
# page pulls in (a CDN script, Google Fonts, an image hosted elsewhere, a
# third-party API called from the browser) is blocked until its origin is added
# to the matching directive; a new inline <script> needs its
# ``inline_script_source`` added.
# The browser posts every block to ``/_csp-report`` below, which logs it on
# stderr (the supervisor log), so a blocked resource is never silent; the
# ``'report-sample'`` makes the browser include where a blocked inline script
# is and how it starts, since the report otherwise names it only as "inline".
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    f"script-src 'self' 'report-sample' {inline_script_source(SHELL_PAGE_SCRIPT_BODY)}; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src http: https:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; "
    "report-uri /_csp-report"
)
BROWSER_EXTENSION_SCHEMES = ("chrome-extension:", "moz-extension:", "safari-web-extension:")


class HealthResponse(FrozenModel):
    """What the health probe answers."""

    status: str = Field(description="Always 'ok'")


class VapidPublicKeyResponse(FrozenModel):
    """The key a browser needs to sign up for this app's pushes."""

    public_key: str = Field(description="The app's public signing key, unpadded base64url")


class DeviceSignUpRequest(FrozenModel):
    """A browser signing up (or signing up again) for notifications."""

    device_id: str = Field(min_length=1, max_length=100, description="The id the device's page made for itself")
    name: str = Field(min_length=1, max_length=100, description="What to call the device on the send page")
    user_agent: str = Field(max_length=1000, description="The browser's user agent string")
    subscription: PushSubscription = Field(description="The browser's push subscription")


class DeviceRenameRequest(FrozenModel):
    """A new name for a signed-up device."""

    name: str = Field(min_length=1, max_length=100, description="What to call the device on the send page")


class DeviceListResponse(FrozenModel):
    """Every signed-up device, newest sign-up first, and every device with a page open right now."""

    devices: tuple[Device, ...] = Field(description="The devices signed up for pushes")
    live_devices: tuple[LiveDevice, ...] = Field(description="The devices whose page holds a live connection now")


class RemovedResponse(FrozenModel):
    """The answer to removing something."""

    removed: bool = Field(description="Always true: the thing is gone")


class SendRequest(FrozenModel):
    """Notifications to send, one per chosen device."""

    device_ids: tuple[str, ...] = Field(min_length=1, description="The devices to send to")
    title: str = Field(min_length=1, max_length=200, description="The notification's title")
    body: str = Field(max_length=1000, description="The notification's message")
    delay_seconds: int = Field(ge=0, le=300, description="How long to wait before sending")
    via: DeliveryChoice = Field(
        default="push", description="Through the push service, live to the open page, or both (one send each)"
    )
    style: str = Field(default="plain", description="The notification style, an id from /api/styles")


class AppVersionResponse(FrozenModel):
    """Which version of the browser-side files the server is handing out now."""

    app_version: str = Field(description="A short fingerprint of the device page, send page and background script")


class DeviceLogRequest(FrozenModel):
    """One line a device's page or background script reports about itself."""

    device_id: str = Field(default="unknown", max_length=100, description="The device reporting")
    source: Literal["page", "worker"] = Field(description="The device page, or the background script")
    message: str = Field(min_length=1, max_length=2000, description="What happened")
    is_problem: bool = Field(default=False, description="Whether it was an error")


class DeviceLogResponse(FrozenModel):
    """A device log, newest first."""

    entries: tuple[DeviceLogEntry, ...] = Field(description="The entries")


class StyleListResponse(FrozenModel):
    """The notification styles a send can use."""

    styles: tuple[NotificationStyle, ...] = Field(description="The styles, in the order the send page lists them")


class ChatNotificationRequest(FrozenModel):
    """A notification a chat just sent the user, to copy to the devices here."""

    message: str = Field(min_length=1, max_length=1000, description="The notification's text")
    title: str = Field(default="", max_length=200, description="The chat's optional title for it")
    chat_id: str = Field(default="", max_length=200, description="The chat that sent it")


class ChatNotificationResponse(FrozenModel):
    """What became of a copied chat notification."""

    is_forwarded: bool = Field(description="False when copying is switched off")
    sends: tuple[SendRecord, ...] = Field(description="One send per device and channel it went to")


class SendListResponse(FrozenModel):
    """Sends, newest first."""

    sends: tuple[SendRecord, ...] = Field(description="The sends")


class ReceiptRequest(FrozenModel):
    """A device reporting what happened to a notification on its screen."""

    device_id: str = Field(description="The device reporting")
    event: Literal["shown", "clicked", "action", "reply", "dismissed"] = Field(
        description="It appeared, its body was tapped, a button was tapped, a reply was sent, or it was dismissed"
    )
    action: str = Field(default="", max_length=50, description="The button's id, for a button tap or a reply")
    reply: str = Field(default="", max_length=2000, description="The text typed, for a reply")
    max_actions: int | None = Field(
        default=None, description="How many buttons the browser says it can show, reported with 'shown'"
    )
    app_version: str = Field(default="", max_length=20, description="The version of the background script reporting")


# What every route exchanges, which the app's API document publishes and every parse and answer
# is checked against (build-app skill, "The app describes its own API"): a route that reads or
# answers JSON names its pydantic models here and uses ``parse_request_body`` and
# ``model_response``; a page, a file or a stream is declared schemaless with the reason.
STATIC_DIR = Path(__file__).parent / "static"


def compute_app_version() -> str:
    """A short fingerprint of the browser-side files, so a device can tell whether it runs the current ones."""
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.iterdir()):
        if path.suffix in (".js", ".html", ".css"):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:8]


APP_VERSION = compute_app_version()
APP_VERSION_PLACEHOLDER = "__APP_VERSION__"

ROUTE_TABLE: RouteTable = {
    ("GET", "/"): SchemalessRoute(reason="the app's page"),
    ("GET", "/send"): SchemalessRoute(reason="the send page"),
    ("GET", "/static/{filename}"): SchemalessRoute(reason="a stylesheet or script the pages load"),
    ("GET", "/_static/{basename}"): SchemalessRoute(reason="a browser-side module the shell builds"),
    ("GET", "/health"): RouteModels(response_model=HealthResponse),
    ("GET", "/sw.js"): SchemalessRoute(reason="the service worker script the browser installs"),
    ("GET", "/manifest.webmanifest"): SchemalessRoute(reason="the install manifest the browser reads"),
    ("GET", "/sends/{send_id}/raw"): SchemalessRoute(reason="a page showing one send's raw record"),
    ("GET", "/api/live/{device_id}/events"): SchemalessRoute(reason="a server-sent event stream an open page reads"),
    ("POST", "/api/chat-notifications"): RouteModels(
        request_model=ChatNotificationRequest, response_model=ChatNotificationResponse, success_status=201
    ),
    ("GET", "/api/chat-forwarding"): RouteModels(response_model=ChatForwardingSettings),
    ("PUT", "/api/chat-forwarding"): RouteModels(request_model=ChatForwardingSettings, response_model=ChatForwardingSettings),
    ("GET", "/reply"): SchemalessRoute(reason="the page a Reply button opens"),
    ("GET", "/api/styles"): RouteModels(response_model=StyleListResponse),
    ("GET", "/api/version"): RouteModels(response_model=AppVersionResponse),
    ("GET", "/api/device-log"): RouteModels(response_model=DeviceLogResponse),
    ("POST", "/api/device-log"): RouteModels(request_model=DeviceLogRequest, response_model=DeviceLogEntry, success_status=201),
    ("GET", "/api/vapid-public-key"): RouteModels(response_model=VapidPublicKeyResponse),
    ("GET", "/api/devices"): RouteModels(response_model=DeviceListResponse),
    ("POST", "/api/devices"): RouteModels(request_model=DeviceSignUpRequest, response_model=Device),
    ("PATCH", "/api/devices/{device_id}"): RouteModels(request_model=DeviceRenameRequest, response_model=Device),
    ("DELETE", "/api/devices/{device_id}"): RouteModels(response_model=RemovedResponse),
    ("GET", "/api/sends"): RouteModels(response_model=SendListResponse),
    ("POST", "/api/sends"): RouteModels(request_model=SendRequest, response_model=SendListResponse, success_status=201),
    ("POST", "/api/sends/{send_id}/receipt"): RouteModels(request_model=ReceiptRequest, response_model=SendRecord),
    ("POST", "/_csp-report"): SchemalessRoute(reason="the browser's report of what the page's policy blocked"),
}

logger = logging.getLogger(__name__)

store = Store(DATA_DIR)
vapid_keys = VapidKeys(DATA_DIR)
live_hub = LiveHub()

app = Flask("notification_tester", static_folder=None)
# The app describes its own API: an OpenAPI document of every route below, served at the
# manifest's api_schema_path.
register_api_schema_route(
    app, DEFAULT_API_SCHEMA_PATH, title='Notification tester', description='Test phone and desktop notifications: sign a device up on one page, send it pushed or live notifications from another', route_table=ROUTE_TABLE
)


@app.after_request
def add_content_security_policy(response: Response) -> Response:
    if response.mimetype == "text/html":
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
    return response


def render_page(filename: str) -> Response:
    html = (STATIC_DIR / filename).read_text().replace(APP_VERSION_PLACEHOLDER, APP_VERSION)
    return Response(html.replace("</body>", SHELL_PAGE_SCRIPT + "</body>"), mimetype="text/html")


@app.route("/")
def index() -> Response:
    """The page a device opens to sign up for notifications and see what it received."""
    return render_page("phone.html")


@app.route("/send")
def send_page() -> Response:
    """The page that sends notifications to signed-up devices."""
    return render_page("send.html")


@app.route("/reply")
def reply_page() -> Response:
    """The page a notification's Reply button opens, with the text box ready."""
    return render_page("reply.html")


@app.route("/api/device-log")
def get_device_log() -> Response:
    """What devices reported about themselves, newest first; pass device_id for one device."""
    return model_response(DeviceLogResponse(entries=store.device_log(request.args.get("device_id"))))


@app.route("/api/device-log", methods=["POST"])
def add_device_log() -> Response:
    """Record one line a device's page or background script reports about itself."""
    request_body = parse_request_body(DeviceLogRequest)
    entry = DeviceLogEntry(
        at=now_iso(),
        device_id=request_body.device_id,
        source=request_body.source,
        message=request_body.message,
        is_problem=request_body.is_problem,
        user_agent=request.headers.get("User-Agent", "")[:300],
    )
    store.add_device_log(entry)
    return model_response(entry, 201)


@app.route("/api/version")
def app_version() -> Response:
    """Which version of the browser-side files the server hands out now."""
    return model_response(AppVersionResponse(app_version=APP_VERSION))


@app.route("/api/styles")
def list_styles() -> Response:
    """The notification styles a send can use."""
    return model_response(StyleListResponse(styles=STYLES))


@app.route("/static/<filename>")
def static_file(filename: str) -> Response:
    """A stylesheet or script the pages load."""
    if filename not in {path.name for path in STATIC_DIR.iterdir()}:
        abort(404)
    response = send_file((STATIC_DIR / filename).absolute())
    # Always check with the server, so a device never keeps running an old script after a change.
    response.headers["Cache-Control"] = "no-cache"
    return response



@app.route("/sw.js")
def service_worker() -> Response:
    """The service worker script the browser installs: it shows pushed notifications and reports them back."""
    script = (STATIC_DIR / "sw.js").read_text().replace(APP_VERSION_PLACEHOLDER, APP_VERSION)
    return Response(script, mimetype="text/javascript", headers={"Cache-Control": "no-cache"})


@app.route("/manifest.webmanifest")
def manifest() -> Response:
    """The install manifest that lets the browser add the app to the Home screen as its own app."""
    return send_file((STATIC_DIR / "manifest.webmanifest").absolute(), mimetype="application/manifest+json")


@app.route("/sends/<send_id>/raw")
def raw_send_page(send_id: str) -> Response:
    """A page showing one send's full record, including the push service's answer, as formatted JSON."""
    record = store.send(send_id)
    if record is None:
        abort(404)
    pretty = html.escape(json.dumps(record.model_dump(mode="json"), indent=2))
    return Response(
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>Send {html.escape(send_id)}</title><link rel=stylesheet href=/static/style.css></head>"
        f"<body><main><header><h1>Raw record</h1><a href=/send>Back to the send page</a></header>"
        f"<pre class=raw>{pretty}</pre></main>{SHELL_PAGE_SCRIPT}</body></html>",
        mimetype="text/html",
    )


@app.route("/api/vapid-public-key")
def vapid_public_key() -> Response:
    """The app's public signing key, which a browser needs to sign up for pushes."""
    return model_response(VapidPublicKeyResponse(public_key=vapid_keys.public_key))


@app.route("/api/devices")
def list_devices() -> Response:
    """Every signed-up device."""
    return model_response(DeviceListResponse(devices=store.devices(), live_devices=live_hub.devices()))


@app.route("/api/devices", methods=["POST"])
def sign_up_device() -> Response:
    """Sign a browser up for notifications, or refresh its sign-up."""
    request_body = parse_request_body(DeviceSignUpRequest)
    device = Device(
        device_id=request_body.device_id,
        name=request_body.name,
        user_agent=request_body.user_agent,
        subscription=request_body.subscription,
        push_service=push_service_name(request_body.subscription.endpoint),
        signed_up_at=now_iso(),
        status="active",
    )
    store.put_device(device)
    return model_response(device)


@app.route("/api/devices/<device_id>", methods=["PATCH"])
def rename_device(device_id: str) -> Response:
    """Rename a signed-up device."""
    request_body = parse_request_body(DeviceRenameRequest)
    device = store.device(device_id)
    if device is None:
        return error_response("No device with that id is signed up", 404)
    renamed = device.model_copy(update={"name": request_body.name})
    store.put_device(renamed)
    return model_response(renamed)


@app.route("/api/devices/<device_id>", methods=["DELETE"])
def remove_device(device_id: str) -> Response:
    """Forget a signed-up device."""
    if not store.remove_device(device_id):
        return error_response("No device with that id is signed up", 404)
    return model_response(RemovedResponse(removed=True))


@app.route("/api/sends")
def list_sends() -> Response:
    """Sends, newest first; pass device_id to see only one device's."""
    return model_response(SendListResponse(sends=store.sends(request.args.get("device_id"))[:200]))


@app.route("/api/sends", methods=["POST"])
def create_sends() -> Response:
    """Send a notification to each chosen device, through the push service, live, or both; now or after a delay."""
    request_body = parse_request_body(SendRequest)
    live_names = {live.device_id: live.name for live in live_hub.devices()}
    names: dict[str, str] = {}
    for device_id in request_body.device_ids:
        device = store.device(device_id)
        name = device.name if device is not None else live_names.get(device_id)
        if name is None:
            return error_response("One of those devices is neither signed up nor open", 404)
        names[device_id] = name
    if request_body.style not in STYLES_BY_ID:
        return error_response(f"There is no notification style called {request_body.style!r}", 400)
    vias: tuple[SendVia, ...] = ("push", "live") if request_body.via == "both" else (request_body.via,)
    targets = [(device_id, name, via) for device_id, name in names.items() for via in vias]
    records = start_sends(
        targets, request_body.title, request_body.body, request_body.delay_seconds, "send page", "", request_body.style
    )
    return model_response(SendListResponse(sends=records), 201)


@app.route("/api/chat-notifications", methods=["POST"])
def copy_chat_notification() -> Response:
    """Copy a notification a chat just sent the user to every device, on the channels the settings name."""
    request_body = parse_request_body(ChatNotificationRequest)
    settings = store.chat_forwarding()
    if not settings.is_enabled:
        return model_response(ChatNotificationResponse(is_forwarded=False, sends=()), 201)
    targets: list[tuple[str, str, SendVia]] = []
    if settings.via in ("push", "both"):
        targets += [(d.device_id, d.name, "push") for d in store.devices() if d.status == "active"]
    if settings.via in ("live", "both"):
        targets += [(live.device_id, live.name, "live") for live in live_hub.devices()]
    title = request_body.title or "Imbue Studio"
    records = start_sends(targets, title, request_body.message, 0, "chat", request_body.chat_id, "plain")
    return model_response(ChatNotificationResponse(is_forwarded=True, sends=records), 201)


@app.route("/api/chat-forwarding")
def get_chat_forwarding() -> Response:
    """Whether chat notifications are copied to the devices here, and on which channels."""
    return model_response(store.chat_forwarding())


@app.route("/api/chat-forwarding", methods=["PUT"])
def put_chat_forwarding() -> Response:
    """Switch copying chat notifications on or off, or change its channels."""
    settings = parse_request_body(ChatForwardingSettings)
    store.put_chat_forwarding(settings)
    return model_response(settings)


def start_sends(
    targets: list[tuple[str, str, SendVia]],
    title: str,
    body: str,
    delay_seconds: int,
    source: SendSource,
    chat_id: str,
    style: str,
) -> tuple[SendRecord, ...]:
    """Record one waiting send per (device, name, channel) and start the timer that delivers it."""
    records = []
    for device_id, name, via in targets:
        record = SendRecord(
            send_id=uuid.uuid4().hex[:12],
            device_id=device_id,
            device_name=name,
            via=via,
            title=title,
            body=body,
            delay_seconds=delay_seconds,
            created_at=now_iso(),
            status="waiting",
            status_detail=f"Sending in {delay_seconds} s" if delay_seconds else "Sending",
            source=source,
            chat_id=chat_id,
            style=style,
        )
        store.put_send(record)
        records.append(record)
        deliver = deliver_push if via == "push" else deliver_live
        timer = threading.Timer(delay_seconds, deliver, args=(record.send_id,))
        timer.daemon = True
        timer.start()
    return tuple(records)


@app.route("/api/live/<device_id>/events")
def live_events(device_id: str) -> Response:
    """The live connection an open device page holds: live sends arrive on it as server-sent events."""
    name = request.args.get("name", "")[:100] or "Unnamed device"
    response = Response(live_hub.stream(device_id, name), mimetype="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    return response


@app.route("/api/sends/<send_id>/receipt", methods=["POST"])
def record_receipt(send_id: str) -> Response:
    """Record that a device showed a notification, or that it was tapped."""
    request_body = parse_request_body(ReceiptRequest)
    record = store.send(send_id)
    if record is None or record.device_id != request_body.device_id:
        return error_response("No send with that id went to that device", 404)
    if request_body.event == "shown":
        updated = store.update_send(
            send_id,
            shown_at=record.shown_at or now_iso(),
            device_max_actions=request_body.max_actions if request_body.max_actions is not None else record.device_max_actions,
            device_app_version=request_body.app_version or record.device_app_version,
        )
    elif request_body.event == "dismissed" and record.interactions and record.interactions[-1].kind != "dismissed":
        # Firefox reports a dismissal when a tap closes the notification; after a tap it is not news.
        updated = record
    else:
        interaction = Interaction(
            at=now_iso(), kind=request_body.event, action=request_body.action, reply=request_body.reply
        )
        changes: dict[str, object] = {"interactions": (*record.interactions, interaction)}
        if request_body.event in ("clicked", "action", "reply"):
            changes["clicked_at"] = record.clicked_at or interaction.at
        updated = store.update_send(send_id, **changes)
    assert updated is not None
    return model_response(updated)


def notification_payload(record: SendRecord, body: str | None = None, is_update: bool = False) -> dict:
    """What the device receives: the text, the style's showNotification options, and what to report back with."""
    style = STYLES_BY_ID.get(record.style, STYLES_BY_ID["plain"])
    options: dict[str, object] = {"icon": ICON, "tag": record.send_id, **style.options}
    if is_update:
        # A later update replaces the shown notification quietly.
        options["silent"] = True
    return {
        "send_id": record.send_id,
        "device_id": record.device_id,
        "via": record.via,
        "style": record.style,
        "title": record.title,
        "body": record.body if body is None else body,
        "created_at": record.created_at,
        "options": options,
        "app_badge_count": style.app_badge_count,
        "is_update": is_update,
    }


def schedule_updates(record: SendRecord) -> None:
    """For a style that updates in place, send each later version of the text after its interval."""
    style = STYLES_BY_ID.get(record.style)
    if style is None:
        return
    for index, part_body in enumerate(style.part_bodies, start=1):
        timer = threading.Timer(style.part_interval_seconds * index, deliver_update, args=(record.send_id, part_body))
        timer.daemon = True
        timer.start()


def deliver_update(send_id: str, body: str) -> None:
    """Send one later version of an updating notification down the same channel as the first."""
    record = store.send(send_id)
    if record is None or record.status != "accepted":
        return
    payload = notification_payload(record, body=body, is_update=True)
    if record.via == "live":
        live_hub.deliver(record.device_id, payload)
        return
    device = store.device(record.device_id)
    if device is not None:
        raw = send_push(vapid_keys, device.subscription, payload)
        if raw.status_code is None or not 200 <= raw.status_code < 300:
            store.update_send(send_id, status_detail=f"{record.status_detail}; an update was not accepted")


def deliver_live(send_id: str) -> None:
    """Hand one waiting live send to the device's open pages, if any are connected."""
    record = store.send(send_id)
    if record is None:
        return
    delivered_to = live_hub.deliver(record.device_id, notification_payload(record))
    sent_at = now_iso()
    if delivered_to == 0:
        store.update_send(
            send_id, status="failed", status_detail="The device's page isn't open, so there was no live connection", sent_at=sent_at
        )
        return
    # A page that just closed still counts until its next heartbeat, so the count is not shown.
    updated = store.update_send(send_id, status="accepted", status_detail="Handed to the open page", sent_at=sent_at)
    if updated is not None:
        schedule_updates(updated)


def deliver_push(send_id: str) -> None:
    """Hand one waiting send to its device's push service and record the verdict."""
    record = store.send(send_id)
    if record is None:
        return
    device = store.device(record.device_id)
    if device is None:
        store.update_send(send_id, status="failed", status_detail="This device isn't signed up for pushes")
        return
    store.update_send(send_id, status="sending", status_detail="Sending")
    raw = send_push(vapid_keys, device.subscription, notification_payload(record))
    sent_at = now_iso()
    service = device.push_service
    if raw.status_code is None:
        store.update_send(
            send_id, status="failed", status_detail=f"Could not reach {service}: {raw.error}", raw_response=raw, sent_at=sent_at
        )
    elif 200 <= raw.status_code < 300:
        updated = store.update_send(
            send_id, status="accepted", status_detail=f"{service} accepted it ({raw.status_code})", raw_response=raw, sent_at=sent_at
        )
        if updated is not None:
            schedule_updates(updated)
    else:
        detail = f"{service} refused it ({raw.status_code})"
        if raw.status_code in (404, 410):
            detail = f"{service} says this device is no longer signed up ({raw.status_code})"
            store.put_device(device.model_copy(update={"status": "expired", "status_detail": detail}))
        store.update_send(send_id, status="rejected", status_detail=detail, raw_response=raw, sent_at=sent_at)


@app.route("/_csp-report", methods=["POST"])
def content_security_policy_report() -> Response:
    """Log what the page's security policy blocked, as the browser reports it."""
    # The browser's report of one thing the policy blocked on a page. A block
    # caused by a browser extension is the policy doing its job and is not logged.
    body = request.get_json(force=True, silent=True)
    report = body.get("csp-report") if isinstance(body, dict) else None
    if not isinstance(report, dict):
        return Response(status=204)
    blocked, page, source, directive, line, sample = (
        str(report.get(key, ""))[:500]
        for key in ("blocked-uri", "document-uri", "source-file", "effective-directive", "line-number", "script-sample")
    )
    if blocked.startswith(BROWSER_EXTENSION_SCHEMES) or source.startswith(BROWSER_EXTENSION_SCHEMES):
        return Response(status=204)
    # An inline script or style is reported as "inline": say where it is and how it starts instead.
    where = f" at {source}:{line}" if source and line else ""
    starting = f" starting {sample!r}" if sample else ""
    remedy = (
        "add its inline_script_source to CONTENT_SECURITY_POLICY if the page needs it"
        if blocked == "inline"
        else "add its origin to CONTENT_SECURITY_POLICY if the page needs it"
    )
    logger.error(
        f"ERROR Content-Security-Policy blocked {blocked!r} on {page!r} ({directive}){where}{starting}; {remedy}"
    )
    return Response(status=204)


@app.route("/_static/<basename>")
def shell_module(basename: str) -> Response:
    """One of the two browser-side modules the workspace shell builds and every app serves from its own origin."""
    # The two shell-built modules and nothing else: a name that is not one of
    # them is a 404, so this route can never read outside that directory.
    if basename not in SHELL_STATIC_MODULE_NAMES:
        abort(404)
    module_path = SHELL_STATIC_MODULES_DIR / basename
    if not module_path.is_file():
        abort(404)
    # Flask resolves a relative path against the app's own directory, not the cwd.
    return send_file(module_path.absolute(), mimetype="text/javascript")


@app.route("/health")
def health() -> Response:
    """Whether the app is up."""
    return model_response(HealthResponse(status="ok"))


def fail_sends_cut_off_by_restart() -> None:
    """A delayed send lives in a timer, so one still waiting when the app last stopped was never sent."""
    for record in store.sends():
        if record.status in ("waiting", "sending"):
            store.update_send(record.send_id, status="failed", status_detail="The app restarted before this was sent")


def main() -> None:
    fail_sends_cut_off_by_restart()
    run_simple(
        "127.0.0.1", PORT, app, threaded=True, use_reloader=False, use_debugger=False
    )


if __name__ == "__main__":
    main()
