"""Shared fixtures for the notification tester's tests: fresh app state, a stand-in push service, browser subscriptions.

Nothing here reads or writes the app's real data. Importing ``notification_tester.runner`` builds its store and
signing key from ``NOTIFICATION_TESTER_DATA_DIR``, so this file points that at a scratch directory before any test
module imports the app, and the ``app_state`` fixture swaps in a fresh store, key and live hub per test.
"""

import base64
import os
import secrets
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask import Flask, Response, request
from werkzeug.serving import make_server

os.environ["NOTIFICATION_TESTER_DATA_DIR"] = tempfile.mkdtemp(
    prefix="notification-tester-import-"
)

from notification_tester import (
    runner,  # noqa: E402 (the data directory above must be set first)
)
from notification_tester.live import LiveHub  # noqa: E402
from notification_tester.push import VapidKeys  # noqa: E402
from notification_tester.store import Store  # noqa: E402


class AppState:
    """The app's state for one test: where it is kept, and the objects the routes use."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.store = Store(data_dir)
        self.vapid_keys = VapidKeys(data_dir)
        self.live_hub = LiveHub()


@pytest.fixture
def app_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppState:
    """A fresh, empty store, signing key and live hub, installed into the running app for this test."""
    state = AppState(tmp_path / "data")
    monkeypatch.setattr(runner, "store", state.store)
    monkeypatch.setattr(runner, "vapid_keys", state.vapid_keys)
    monkeypatch.setattr(runner, "live_hub", state.live_hub)
    return state


class BrowserSubscription:
    """A push subscription made the way a browser makes one: its own key pair and auth secret, so a test can decrypt."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        self.auth_secret = secrets.token_bytes(16)
        public = self.private_key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.keys = {"p256dh": _base64url(public), "auth": _base64url(self.auth_secret)}

    def as_json(self) -> dict:
        """What the browser's ``subscription.toJSON()`` hands the page."""
        return {"endpoint": self.endpoint, "keys": self.keys}


def _base64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class ReceivedPush:
    """One message the stand-in push service received."""

    def __init__(self, path: str, headers: dict[str, str], body: bytes) -> None:
        self.path = path
        self.headers = headers
        self.body = body


class PushService:
    """A local stand-in for a browser vendor's push service: records each post and answers with a chosen status."""

    def __init__(self, origin: str) -> None:
        self.origin = origin
        self.received: list[ReceivedPush] = []
        # The answer for each endpoint path; anything not listed is accepted with 201.
        self.statuses: dict[str, int] = {}
        self.arrived = threading.Condition()

    def endpoint(self, name: str) -> str:
        return f"{self.origin}/push/{name}"

    def wait_for_count(self, count: int, timeout: float = 5.0) -> list[ReceivedPush]:
        with self.arrived:
            self.arrived.wait_for(lambda: len(self.received) >= count, timeout=timeout)
            return list(self.received)


@pytest.fixture
def push_service() -> Iterator[PushService]:
    """A push service on a free loopback port, for the length of one test."""
    service_app = Flask("stand-in-push-service")
    holder: list[PushService] = []

    @service_app.route("/push/<name>", methods=["POST"])
    def receive(name: str) -> Response:
        service = holder[0]
        with service.arrived:
            service.received.append(
                ReceivedPush(request.path, dict(request.headers), request.get_data())
            )
            service.arrived.notify_all()
        status = service.statuses.get(request.path, 201)
        return Response("stand-in answer", status=status, headers={"X-Stand-In": "yes"})

    server = make_server("127.0.0.1", 0, service_app, threaded=True)
    holder.append(PushService(f"http://127.0.0.1:{server.port}"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield holder[0]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def make_subscription() -> Callable[[str], BrowserSubscription]:
    return BrowserSubscription


def wait_until(predicate: Callable[[], object], timeout: float = 5.0) -> object:
    """Poll ``predicate`` until it answers something truthy, and return that; fail the test after ``timeout``."""
    done = threading.Event()
    deadline_hit = threading.Timer(timeout, done.set)
    deadline_hit.start()
    try:
        while not done.is_set():
            value = predicate()
            if value:
                return value
            done.wait(0.02)
    finally:
        deadline_hit.cancel()
    pytest.fail(f"Timed out after {timeout} s waiting for {predicate}")


@pytest.fixture
def wait() -> Callable[..., object]:
    return wait_until
