"""The JSON store: what survives a restart, and the bounds on what it keeps."""

import json
import stat

import pytest

from notification_tester import store as store_module
from notification_tester.push import VapidKeys
from notification_tester.store import (
    ChatForwardingSettings,
    Device,
    DeviceLogEntry,
    PushSubscription,
    SendRecord,
    Store,
)


def a_device(
    device_id: str, signed_up_at: str = "2026-01-01T00:00:00.000+00:00"
) -> Device:
    return Device(
        device_id=device_id,
        name=device_id.title(),
        user_agent="pytest",
        subscription=PushSubscription.model_validate(
            {
                "endpoint": f"https://push.example.org/{device_id}",
                "keys": {"p256dh": "k", "auth": "a"},
            }
        ),
        push_service="push.example.org",
        signed_up_at=signed_up_at,
        status="active",
    )


def a_send(send_id: str, created_at: str) -> SendRecord:
    return SendRecord.model_validate(
        {
            "send_id": send_id,
            "device_id": "d",
            "device_name": "D",
            "via": "push",
            "title": "t",
            "body": "b",
            "delay_seconds": 0,
            "created_at": created_at,
            "status": "accepted",
            "raw_response": {
                "url": "https://push.example.org/d",
                "status_code": 201,
                "headers": {},
                "body": send_id,
            },
        }
    )


def test_everything_written_is_read_back_by_the_next_process(tmp_path) -> None:
    first = Store(tmp_path)
    first.put_device(a_device("phone"))
    first.put_send(a_send("s1", "2026-01-01T00:00:00Z"))
    first.update_send("s1", shown_at="2026-01-01T00:00:01Z")
    first.put_chat_forwarding(ChatForwardingSettings(is_enabled=False, via="push"))
    first.add_device_log(
        DeviceLogEntry(
            at="t",
            device_id="phone",
            source="page",
            message="hi",
            is_problem=False,
            user_agent="u",
        )
    )

    second = Store(tmp_path)

    assert second.device("phone") == a_device("phone")
    reread = second.send("s1")
    assert reread is not None and reread.shown_at == "2026-01-01T00:00:01Z"
    assert second.chat_forwarding() == ChatForwardingSettings(
        is_enabled=False, via="push"
    )
    assert [e.message for e in second.device_log()] == ["hi"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "chat_forwarding.json",
        "device_log.json",
        "devices.json",
        "sends.json",
    ]


def test_an_empty_data_directory_is_an_empty_store_with_default_settings(
    tmp_path,
) -> None:
    store = Store(tmp_path / "not-yet")

    assert store.devices() == () and store.sends() == () and store.device_log() == ()
    assert store.chat_forwarding() == ChatForwardingSettings(
        is_enabled=True, via="both"
    )
    assert (
        store.update_send("nope", status="failed") is None
        and store.remove_device("nope") is False
    )


def test_devices_list_newest_sign_up_first(tmp_path) -> None:
    store = Store(tmp_path)
    store.put_device(a_device("old", "2026-01-01T00:00:00.000+00:00"))
    store.put_device(a_device("new", "2026-02-01T00:00:00.000+00:00"))

    assert [d.device_id for d in store.devices()] == ["new", "old"]


def test_the_send_history_keeps_only_the_newest_sends_with_their_raw_answers(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(store_module, "SEND_HISTORY_LIMIT", 3)
    store = Store(tmp_path)
    for index in range(5):
        store.put_send(a_send(f"s{index}", f"2026-01-01T00:00:0{index}Z"))

    assert [s.send_id for s in store.sends()] == ["s4", "s3", "s2"]
    assert store.send("s0") is None
    saved = json.loads((tmp_path / "sends.json").read_text())
    assert sorted(s["send_id"] for s in saved) == ["s2", "s3", "s4"]
    assert sorted(s["raw_response"]["body"] for s in saved) == ["s2", "s3", "s4"]
    assert (
        store_module.SEND_HISTORY_LIMIT == 3 and store_module.DEVICE_LOG_LIMIT == 2000
    )


def test_the_device_log_keeps_only_its_newest_entries(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(store_module, "DEVICE_LOG_LIMIT", 2)
    store = Store(tmp_path)
    for index in range(4):
        store.add_device_log(
            DeviceLogEntry(
                at=str(index),
                device_id="d",
                source="worker",
                message=f"m{index}",
                is_problem=False,
                user_agent="",
            )
        )

    assert [e.message for e in store.device_log()] == ["m3", "m2"]
    assert [
        e["message"] for e in json.loads((tmp_path / "device_log.json").read_text())
    ] == ["m2", "m3"]
    assert [e.message for e in Store(tmp_path).device_log(limit=1)] == ["m3"]


def test_writes_replace_the_file_whole_and_leave_no_scratch_behind(tmp_path) -> None:
    store = Store(tmp_path)
    store.put_device(a_device("phone"))
    store.put_device(a_device("tablet"))

    assert not list(tmp_path.glob("*.tmp"))
    assert {
        d["device_id"] for d in json.loads((tmp_path / "devices.json").read_text())
    } == {"phone", "tablet"}


def test_the_signing_key_is_made_once_private_and_then_kept(tmp_path) -> None:
    keys = VapidKeys(tmp_path / "data")
    key_file = tmp_path / "data" / "vapid_private_key.pem"
    original = key_file.read_bytes()

    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert VapidKeys(tmp_path / "data").public_key == keys.public_key
    assert key_file.read_bytes() == original


@pytest.mark.parametrize("bad", ["[{]", '{"not": "a list"}'])
def test_a_damaged_store_file_stops_the_app_rather_than_being_overwritten(
    tmp_path, bad
) -> None:
    (tmp_path / "devices.json").write_text(bad)

    with pytest.raises((ValueError, TypeError)):
        Store(tmp_path)

    assert (tmp_path / "devices.json").read_text() == bad
