"""The signed-up devices and the log of sends, kept as JSON files under the app's data directory."""

import json
import threading
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Literal

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

DeviceStatus = Literal["active", "expired"]
SendStatus = Literal["waiting", "sending", "accepted", "rejected", "failed"]
SendVia = Literal["push", "live"]
SendSource = Literal["send page", "chat"]
DeliveryChoice = Literal["push", "live", "both"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class PushKeys(FrozenModel):
    """The encryption keys the browser made for this subscription."""

    p256dh: str = Field(description="The browser's public key for encrypting messages to it")
    auth: str = Field(description="The browser's shared authentication secret")


class PushSubscription(FrozenModel):
    """Where the browser's push service accepts messages for one device, as the browser handed it over."""

    endpoint: str = Field(description="The push service URL messages for this device are posted to")
    keys: PushKeys = Field(description="The keys messages are encrypted with")


class Device(FrozenModel):
    """One browser that signed up for notifications."""

    device_id: str = Field(description="The id the device's page made for itself")
    name: str = Field(description="What the device is called on the send page")
    user_agent: str = Field(description="The browser's user agent string at sign-up")
    subscription: PushSubscription = Field(description="Where pushes for this device go")
    push_service: str = Field(description="Whose push service carries messages to it, e.g. Mozilla")
    signed_up_at: str = Field(description="When it last signed up, ISO 8601 UTC")
    status: DeviceStatus = Field(description="'expired' once the push service said the subscription is gone")
    status_detail: str = Field(default="", description="Why the device expired, when it did")


class RawPushResponse(FrozenModel):
    """Exactly what the push service answered, unprocessed."""

    url: str = Field(description="The push service URL the message was posted to")
    status_code: int | None = Field(description="The HTTP status, or none when no answer came back")
    headers: dict[str, str] = Field(description="The response headers")
    body: str = Field(description="The response body as text")
    error: str = Field(default="", description="The connection error, when the request itself failed")


InteractionKind = Literal["clicked", "action", "reply", "dismissed"]


class Interaction(FrozenModel):
    """Something the person did with a shown notification."""

    at: str = Field(description="When, ISO 8601 UTC")
    kind: InteractionKind = Field(description="Tapped the body, tapped a button, sent a reply, or dismissed it")
    action: str = Field(default="", description="The button's id, for a button tap or a reply")
    reply: str = Field(default="", description="The text typed, for a reply")


class SendRecord(FrozenModel):
    """One notification sent to one device, and what became of it."""

    send_id: str = Field(description="The send's id, carried in the push so the device can confirm it")
    device_id: str = Field(description="The device it went to")
    device_name: str = Field(description="The device's name at the time of sending")
    via: SendVia = Field(description="How it was delivered: through the push service, or live to an open page")
    title: str = Field(description="The notification's title")
    body: str = Field(description="The notification's message")
    delay_seconds: int = Field(description="How long after the request it was sent")
    created_at: str = Field(description="When it was requested, ISO 8601 UTC")
    sent_at: str | None = Field(default=None, description="When it was handed to the push service")
    status: SendStatus = Field(description="Where the send is: waiting, sending, or the push service's verdict")
    status_detail: str = Field(default="", description="The verdict in words")
    raw_response: RawPushResponse | None = Field(default=None, description="The push service's own answer (pushes only)")
    shown_at: str | None = Field(default=None, description="When the device reported showing it")
    clicked_at: str | None = Field(default=None, description="When the device reported it was tapped")
    source: SendSource = Field(default="send page", description="What asked for it: the send page, or a chat's notification")
    chat_id: str = Field(default="", description="The chat whose notification this copies, when it came from one")
    style: str = Field(default="plain", description="The notification style it used")
    interactions: tuple[Interaction, ...] = Field(default=(), description="What was done with it, in order")
    device_max_actions: int | None = Field(
        default=None, description="How many buttons the device's browser said it can show, when it said"
    )
    device_app_version: str = Field(
        default="", description="The version of the background script that showed it; empty from versions before this"
    )


class ChatForwardingSettings(FrozenModel):
    """Whether the workspace's chat notifications are copied to the devices here, and how."""

    is_enabled: bool = Field(default=True, description="Whether chat notifications are copied at all")
    via: DeliveryChoice = Field(default="both", description="Push, live, or both, to every device that can take it")


class DeviceLogEntry(FrozenModel):
    """One thing a device's page or background script did or ran into, as it reported it."""

    at: str = Field(description="When the server received it, ISO 8601 UTC")
    device_id: str = Field(description="The device reporting, or 'unknown' when its background script had no id yet")
    source: Literal["page", "worker"] = Field(description="The device page, or the background script")
    message: str = Field(description="What happened, in the device's words")
    is_problem: bool = Field(description="Whether it was an error")
    user_agent: str = Field(description="The reporting browser's user agent")


# The device log keeps only the newest entries.
DEVICE_LOG_LIMIT = 2000


class Store:
    """Devices and sends, held in memory and written through to two JSON files."""

    def __init__(self, data_dir: Path) -> None:
        self._devices_path = data_dir / "devices.json"
        self._sends_path = data_dir / "sends.json"
        self._settings_path = data_dir / "chat_forwarding.json"
        self._device_log_path = data_dir / "device_log.json"
        self._device_log: list[DeviceLogEntry] = [
            DeviceLogEntry.model_validate(item) for item in _read_list(self._device_log_path)
        ]
        self._lock = threading.Lock()
        self._devices: dict[str, Device] = {
            item["device_id"]: Device.model_validate(item) for item in _read_list(self._devices_path)
        }
        self._sends: dict[str, SendRecord] = {
            item["send_id"]: SendRecord.model_validate(item) for item in _read_list(self._sends_path)
        }

    def devices(self) -> tuple[Device, ...]:
        with self._lock:
            return tuple(sorted(self._devices.values(), key=lambda d: d.signed_up_at, reverse=True))

    def device(self, device_id: str) -> Device | None:
        with self._lock:
            return self._devices.get(device_id)

    def put_device(self, device: Device) -> None:
        with self._lock:
            self._devices[device.device_id] = device
            self._save_devices()

    def remove_device(self, device_id: str) -> bool:
        with self._lock:
            if self._devices.pop(device_id, None) is None:
                return False
            self._save_devices()
            return True

    def sends(self, device_id: str | None = None) -> tuple[SendRecord, ...]:
        with self._lock:
            records = [s for s in self._sends.values() if device_id is None or s.device_id == device_id]
        return tuple(sorted(records, key=lambda s: s.created_at, reverse=True))

    def send(self, send_id: str) -> SendRecord | None:
        with self._lock:
            return self._sends.get(send_id)

    def put_send(self, record: SendRecord) -> None:
        with self._lock:
            self._sends[record.send_id] = record
            self._save_sends()

    def update_send(self, send_id: str, **changes: object) -> SendRecord | None:
        with self._lock:
            record = self._sends.get(send_id)
            if record is None:
                return None
            updated = record.model_copy(update=changes)
            self._sends[send_id] = updated
            self._save_sends()
            return updated

    def chat_forwarding(self) -> ChatForwardingSettings:
        with self._lock:
            if not self._settings_path.is_file():
                return ChatForwardingSettings()
            return ChatForwardingSettings.model_validate_json(self._settings_path.read_text())

    def put_chat_forwarding(self, settings: ChatForwardingSettings) -> None:
        with self._lock:
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            self._settings_path.write_text(settings.model_dump_json(indent=2))

    def device_log(self, device_id: str | None = None, limit: int = 200) -> tuple[DeviceLogEntry, ...]:
        with self._lock:
            entries = [e for e in self._device_log if device_id is None or e.device_id == device_id]
        return tuple(reversed(entries[-limit:]))

    def add_device_log(self, entry: DeviceLogEntry) -> None:
        with self._lock:
            self._device_log = [*self._device_log, entry][-DEVICE_LOG_LIMIT:]
            _write_list(self._device_log_path, [e.model_dump(mode="json") for e in self._device_log])

    def _save_devices(self) -> None:
        _write_list(self._devices_path, [d.model_dump(mode="json") for d in self._devices.values()])

    def _save_sends(self) -> None:
        _write_list(self._sends_path, [s.model_dump(mode="json") for s in self._sends.values()])


def _read_list(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return json.loads(path.read_text())


def _write_list(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(items, indent=2))
    temporary.replace(path)
