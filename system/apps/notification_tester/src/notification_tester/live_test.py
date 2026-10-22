"""Live delivery: the streams open pages hold, and what the server sends down them."""

import json

from notification_tester import live
from notification_tester.live import LiveHub
from notification_tester.push import push_service_name


def test_a_stream_greets_carries_each_delivery_and_forgets_the_page_when_it_closes() -> (
    None
):
    hub = LiveHub()
    stream = hub.stream("phone", "Phone")

    hello = next(stream)
    assert hello.startswith("event: hello\ndata: ") and hello.endswith("\n\n")
    assert "connected_at" in json.loads(hello.split("data: ", 1)[1])
    assert hub.is_connected("phone") and [d.name for d in hub.devices()] == ["Phone"]

    assert hub.deliver("phone", {"title": "Hi"}) == 1
    assert next(stream) == 'event: notification\ndata: {"title": "Hi"}\n\n'
    assert hub.deliver("nobody", {"title": "Hi"}) == 0

    stream.close()
    assert not hub.is_connected("phone") and hub.devices() == ()


def test_two_pages_of_one_device_count_as_one_device_with_two_connections() -> None:
    hub = LiveHub()
    first = hub.stream("phone", "Old name")
    second = hub.stream("phone", "New name")
    next(first)
    next(second)

    (device,) = hub.devices()
    assert (device.device_id, device.name, device.connection_count) == (
        "phone",
        "New name",
        2,
    )
    assert hub.deliver("phone", {"n": 1}) == 2

    first.close()
    assert hub.devices()[0].connection_count == 1
    second.close()


def test_an_idle_stream_sends_a_heartbeat_and_ends_after_its_lifetime(
    monkeypatch,
) -> None:
    monkeypatch.setattr(live, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(live, "MAX_STREAM_SECONDS", 0.2)
    hub = LiveHub()

    events = list(hub.stream("phone", "Phone"))

    assert events[0].startswith("event: hello")
    assert set(events[1:]) == {": still here\n\n"}
    assert not hub.is_connected("phone")


def test_push_services_are_named_by_endpoint_host_suffix_only() -> None:
    assert push_service_name("https://fcm.googleapis.com/fcm/send/x") == "Google"
    assert (
        push_service_name("https://googleapis.com.evil.example/x")
        == "googleapis.com.evil.example"
    )
    assert push_service_name("not a url") == "an unknown push service"
