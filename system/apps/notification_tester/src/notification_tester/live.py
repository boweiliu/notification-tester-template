"""Live delivery: open device pages hold a stream to the server, and a live send goes straight down it."""

import json
import queue
import threading
import time
from collections.abc import Iterator

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

from notification_tester.store import now_iso

# How often an idle stream carries a comment line. Writing is the only way the server learns a page
# went away, so this bounds how long a closed page still shows as connected.
HEARTBEAT_SECONDS = 10
# How long one stream lasts before the server ends it; the page's EventSource reconnects on its own,
# so this only bounds how long a stuck connection can hold a server thread.
MAX_STREAM_SECONDS = 30 * 60


class LiveDevice(FrozenModel):
    """A device whose page is open right now with a live connection."""

    device_id: str = Field(description="The device's id")
    name: str = Field(description="The name the page reported")
    connected_since: str = Field(description="When its oldest open connection started, ISO 8601 UTC")
    connection_count: int = Field(description="How many of its pages are connected")


class _Connection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.connected_at = now_iso()
        self.messages: queue.Queue[dict] = queue.Queue()


class LiveHub:
    """The open connections, by device."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._connections: dict[str, list[_Connection]] = {}

    def devices(self) -> tuple[LiveDevice, ...]:
        with self._lock:
            return tuple(
                LiveDevice(
                    device_id=device_id,
                    name=connections[-1].name,
                    connected_since=min(c.connected_at for c in connections),
                    connection_count=len(connections),
                )
                for device_id, connections in self._connections.items()
                if connections
            )

    def is_connected(self, device_id: str) -> bool:
        with self._lock:
            return bool(self._connections.get(device_id))

    def deliver(self, device_id: str, message: dict) -> int:
        """Queue ``message`` on every open connection of the device; answers how many got it."""
        with self._lock:
            connections = list(self._connections.get(device_id, ()))
        for connection in connections:
            connection.messages.put(message)
        return len(connections)

    def stream(self, device_id: str, name: str) -> Iterator[str]:
        """The server-sent event stream one open page reads, for as long as it stays connected."""
        connection = _Connection(name)
        with self._lock:
            self._connections.setdefault(device_id, []).append(connection)
        try:
            yield _event("hello", {"connected_at": connection.connected_at})
            deadline = time.monotonic() + MAX_STREAM_SECONDS
            while time.monotonic() < deadline:
                try:
                    message = connection.messages.get(timeout=HEARTBEAT_SECONDS)
                except queue.Empty:
                    yield ": still here\n\n"
                    continue
                yield _event("notification", message)
        finally:
            with self._lock:
                remaining = [c for c in self._connections.get(device_id, []) if c is not connection]
                if remaining:
                    self._connections[device_id] = remaining
                else:
                    self._connections.pop(device_id, None)


def _event(kind: str, data: dict) -> str:
    return f"event: {kind}\ndata: {json.dumps(data)}\n\n"
