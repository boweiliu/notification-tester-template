"""The app's routes, driven through Flask's test client over fresh state and a stand-in push service."""

import json
import logging
import shutil
from collections.abc import Callable
from pathlib import Path

import http_ece
import pytest
from app_schema.testing import assert_api_schema_describes_app
from cryptography.hazmat.primitives import serialization
from flask.testing import FlaskClient

from notification_tester import runner
from notification_tester.store import DEVICE_LOG_LIMIT, SendRecord
from notification_tester.styles import STYLES_BY_ID

_APP_TOML = Path(__file__).resolve().parents[2] / "app.toml"


@pytest.fixture
def client(app_state) -> FlaskClient:
    return runner.app.test_client()


def sign_up(
    client: FlaskClient,
    device_id: str,
    endpoint: str,
    name: str = "Test phone",
    keys: dict | None = None,
) -> dict:
    subscription = {
        "endpoint": endpoint,
        "keys": keys or {"p256dh": "BPublicKey", "auth": "secret"},
    }
    response = client.post(
        "/api/devices",
        json={
            "device_id": device_id,
            "name": name,
            "user_agent": "pytest",
            "subscription": subscription,
        },
    )
    assert response.status_code == 200, response.json
    device = response.json
    assert isinstance(device, dict)
    return device


def send(client: FlaskClient, device_ids: list[str], **fields: object):
    body = {
        "device_ids": device_ids,
        "title": "Hello",
        "body": "From the test",
        "delay_seconds": 0,
        **fields,
    }
    return client.post("/api/sends", json=body)


def settled(app_state, send_id: str) -> Callable[[], SendRecord | None]:
    """A predicate answering the send once it has left waiting/sending."""

    def check() -> SendRecord | None:
        record = app_state.store.send(send_id)
        return (
            record
            if record is not None and record.status not in ("waiting", "sending")
            else None
        )

    return check


def open_live_page(app_state, device_id: str, name: str = "Open page"):
    """What an open device page holds: a live stream, already past its hello."""
    stream = app_state.live_hub.stream(device_id, name)
    assert next(stream).startswith("event: hello\n")
    return stream


def next_live_payload(stream) -> dict:
    event = next(stream)
    assert event.startswith("event: notification\ndata: "), event
    return json.loads(event.split("data: ", 1)[1])


# The API document and the pages


def test_the_app_describes_its_api_the_way_its_manifest_says() -> None:
    assert_api_schema_describes_app(runner.app.test_client(), _APP_TOML)


@pytest.mark.parametrize(
    "path, title",
    [
        ("/", "Notification tester"),
        ("/send", "Send a notification"),
        ("/reply", "Reply"),
    ],
)
def test_each_page_carries_the_current_version_the_shell_script_and_the_security_policy(
    client, path, title
) -> None:
    response = client.get(path)

    page = response.get_data(as_text=True)
    assert response.status_code == 200
    assert f"<title>{title}</title>" in page
    assert f'<meta name="app-version" content="{runner.APP_VERSION}">' in page
    assert runner.APP_VERSION_PLACEHOLDER not in page
    assert page.count(runner.SHELL_PAGE_SCRIPT) == 1 and page.index(
        runner.SHELL_PAGE_SCRIPT
    ) < page.index("</body>")
    assert response.headers["Content-Security-Policy"] == runner.CONTENT_SECURITY_POLICY


def test_the_service_worker_is_served_at_the_root_scope_stamped_and_never_cached(
    client,
) -> None:
    response = client.get("/sw.js")

    script = response.get_data(as_text=True)
    assert response.mimetype == "text/javascript"
    assert response.headers["Cache-Control"] == "no-cache"
    assert f'const APP_VERSION = "{runner.APP_VERSION}";' in script
    assert "Content-Security-Policy" not in response.headers


def test_the_install_manifest_keeps_the_scope_and_start_page_installed_apps_rely_on(
    client,
) -> None:
    response = client.get("/manifest.webmanifest")

    manifest = json.loads(response.get_data())
    assert response.mimetype == "application/manifest+json"
    assert (
        manifest["id"],
        manifest["start_url"],
        manifest["scope"],
        manifest["display"],
    ) == ("/", "/", "/", "standalone")
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200


def test_static_files_are_served_unstale_and_nothing_outside_the_folder_is(
    client,
) -> None:
    style = client.get("/static/style.css")
    assert style.status_code == 200 and style.headers["Cache-Control"] == "no-cache"
    assert client.get("/static/missing.js").status_code == 404
    assert client.get("/static/..%2Frunner.py").status_code == 404
    assert client.get("/_static/not-a-shell-module.js").status_code == 404


def test_the_version_is_a_fingerprint_of_the_scripts_and_pages_only(
    tmp_path, monkeypatch, client
) -> None:
    assert client.get("/api/version").json == {"app_version": runner.APP_VERSION}
    copy = tmp_path / "static"
    shutil.copytree(runner.STATIC_DIR, copy)
    monkeypatch.setattr(runner, "STATIC_DIR", copy)
    assert runner.compute_app_version() == runner.APP_VERSION

    (copy / "icon-192.png").write_bytes(b"a different picture")
    assert runner.compute_app_version() == runner.APP_VERSION

    (copy / "sw.js").write_text((copy / "sw.js").read_text() + "\n// changed\n")
    changed = runner.compute_app_version()
    assert changed != runner.APP_VERSION and len(changed) == 8


def test_health_and_styles(client) -> None:
    assert client.get("/health").json == {"status": "ok"}
    styles = client.get("/api/styles").json["styles"]
    # Installed devices and stored sends name styles by these ids.
    assert [s["style_id"] for s in styles] == [
        "plain",
        "yes-no",
        "quick-replies",
        "reply-page",
        "inline-reply",
        "big-picture",
        "sticky-buzz",
        "updating",
        "app-badge",
    ]
    assert styles[1]["options"]["actions"] == [
        {"action": "yes", "title": "Yes"},
        {"action": "no", "title": "No"},
    ]


def test_the_public_key_is_the_raw_point_browsers_subscribe_with(
    client, app_state
) -> None:
    public_key = client.get("/api/vapid-public-key").json["public_key"]

    raw = runner.base64.urlsafe_b64decode(public_key + "=" * (-len(public_key) % 4))
    assert "=" not in public_key and len(raw) == 65 and raw[0] == 4
    assert raw == app_state.vapid_keys.vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


# Devices


@pytest.mark.parametrize(
    "endpoint, service",
    [
        ("https://updates.push.services.mozilla.com/wpush/v2/abc", "Mozilla"),
        ("https://fcm.googleapis.com/fcm/send/abc", "Google"),
        ("https://web.push.apple.com/abc", "Apple"),
        ("https://wns2-par02p.notify.windows.com/w/?token=abc", "Microsoft"),
        ("https://push.example.org/abc", "push.example.org"),
    ],
)
def test_a_sign_up_names_the_push_service_from_its_endpoint(
    client, endpoint, service
) -> None:
    assert sign_up(client, "d1", endpoint)["push_service"] == service


def test_devices_sign_up_again_rename_and_are_removed(client, app_state) -> None:
    sign_up(client, "older", "https://push.example.org/1", name="Older")
    first = sign_up(client, "phone", "https://push.example.org/2", name="Phone")
    app_state.store.put_device(
        app_state.store.device("phone").model_copy(
            update={"status": "expired", "status_detail": "gone"}
        )
    )

    again = sign_up(client, "phone", "https://push.example.org/3", name="Phone")

    devices = client.get("/api/devices").json
    assert [d["device_id"] for d in devices["devices"]] == ["phone", "older"]
    assert devices["live_devices"] == []
    assert (
        again["status"] == "active"
        and again["status_detail"] == ""
        and again["signed_up_at"] >= first["signed_up_at"]
    )
    assert (
        devices["devices"][0]["subscription"]["endpoint"]
        == "https://push.example.org/3"
    )

    renamed = client.patch("/api/devices/phone", json={"name": "Kitchen phone"})
    assert renamed.json["name"] == "Kitchen phone"
    assert client.patch("/api/devices/nobody", json={"name": "x"}).status_code == 404
    assert client.patch("/api/devices/phone", json={"name": ""}).status_code == 400

    assert client.delete("/api/devices/phone").json == {"removed": True}
    assert client.delete("/api/devices/phone").status_code == 404
    assert [d["device_id"] for d in client.get("/api/devices").json["devices"]] == [
        "older"
    ]
    saved = json.loads((app_state.data_dir / "devices.json").read_text())
    assert [d["device_id"] for d in saved] == ["older"]


def test_a_sign_up_without_keys_is_refused(client) -> None:
    response = client.post(
        "/api/devices",
        json={
            "device_id": "d",
            "name": "n",
            "user_agent": "",
            "subscription": {"endpoint": "https://push.example.org/x"},
        },
    )
    assert response.status_code == 400
    assert client.get("/api/devices").json["devices"] == []


# Sending


def test_a_send_to_an_unknown_device_or_style_is_refused_and_records_nothing(
    client, app_state
) -> None:
    sign_up(client, "phone", "https://push.example.org/1")

    assert send(client, ["phone", "nobody"]).status_code == 404
    assert send(client, ["phone"], style="no-such-style").status_code == 400
    assert send(client, [], style="plain").status_code == 400
    assert send(client, ["phone"], delay_seconds=301).status_code == 400
    assert app_state.store.sends() == ()


def test_a_push_is_encrypted_for_the_device_signed_and_its_answer_kept(
    client, app_state, push_service, make_subscription, wait
) -> None:
    subscription = make_subscription(push_service.endpoint("phone"))
    sign_up(client, "phone", subscription.endpoint, keys=subscription.keys)

    response = send(client, ["phone"], style="yes-no")

    assert response.status_code == 201
    (record,) = response.json["sends"]
    assert (
        record["status"],
        record["status_detail"],
        record["via"],
        record["source"],
    ) == ("waiting", "Sending", "push", "send page")
    done = wait(settled(app_state, record["send_id"]))
    assert done.status == "accepted"
    assert done.status_detail == "127.0.0.1 accepted it (201)"
    assert (
        done.raw_response.status_code == 201
        and done.raw_response.body == "stand-in answer"
    )
    assert (
        done.raw_response.headers["X-Stand-In"] == "yes"
        and done.raw_response.url == subscription.endpoint
    )
    assert done.sent_at is not None

    (push,) = push_service.wait_for_count(1)
    assert push.headers["Ttl"] == "3600" and push.headers["Urgency"] == "high"
    assert push.headers["Content-Encoding"] == "aes128gcm"
    assert (
        f"k={client.get('/api/vapid-public-key').json['public_key']}"
        in push.headers["Authorization"]
    )
    payload = json.loads(
        http_ece.decrypt(
            push.body,
            private_key=subscription.private_key,
            auth_secret=subscription.auth_secret,
        )
    )
    # The shape installed service workers read.
    assert payload == {
        "send_id": record["send_id"],
        "device_id": "phone",
        "via": "push",
        "style": "yes-no",
        "title": "Hello",
        "body": "From the test",
        "created_at": record["created_at"],
        "options": {
            "icon": "/static/icon-192.png",
            "tag": record["send_id"],
            **STYLES_BY_ID["yes-no"].options,
        },
        "app_badge_count": None,
        "is_update": False,
    }


def test_a_push_service_saying_the_subscription_is_gone_expires_the_device(
    client, app_state, push_service, make_subscription, wait
) -> None:
    subscription = make_subscription(push_service.endpoint("gone"))
    push_service.statuses["/push/gone"] = 410
    sign_up(client, "phone", subscription.endpoint, keys=subscription.keys)

    (record,) = send(client, ["phone"]).json["sends"]

    done = wait(settled(app_state, record["send_id"]))
    assert done.status == "rejected"
    assert (
        done.status_detail == "127.0.0.1 says this device is no longer signed up (410)"
    )
    assert done.raw_response.status_code == 410
    device = app_state.store.device("phone")
    assert device.status == "expired" and device.status_detail == done.status_detail


def test_a_push_service_refusal_keeps_the_device_and_an_unreachable_one_fails_the_send(
    client, app_state, push_service, make_subscription, wait
) -> None:
    refusing = make_subscription(push_service.endpoint("busy"))
    push_service.statuses["/push/busy"] = 500
    sign_up(client, "busy", refusing.endpoint, keys=refusing.keys)
    unreachable = make_subscription("http://127.0.0.1:9/push/closed")
    sign_up(client, "away", unreachable.endpoint, keys=unreachable.keys)

    by_device = {
        r["device_id"]: r["send_id"]
        for r in send(client, ["busy", "away"]).json["sends"]
    }

    refused = wait(settled(app_state, by_device["busy"]))
    assert (refused.status, refused.status_detail) == (
        "rejected",
        "127.0.0.1 refused it (500)",
    )
    assert app_state.store.device("busy").status == "active"
    failed = wait(settled(app_state, by_device["away"]))
    assert failed.status == "failed" and failed.status_detail.startswith(
        "Could not reach 127.0.0.1: "
    )
    assert failed.raw_response.status_code is None and failed.raw_response.error


def test_a_delayed_send_waits_and_one_the_app_stopped_before_is_failed_on_start(
    client, app_state
) -> None:
    sign_up(client, "phone", "https://push.example.org/1")
    (record,) = send(client, ["phone"], delay_seconds=300).json["sends"]
    assert (record["status"], record["status_detail"], record["delay_seconds"]) == (
        "waiting",
        "Sending in 300 s",
        300,
    )
    app_state.store.put_send(
        SendRecord.model_validate({**record, "send_id": "done1", "status": "accepted"})
    )

    runner.fail_sends_cut_off_by_restart()

    assert app_state.store.send(record["send_id"]).status == "failed"
    assert (
        app_state.store.send(record["send_id"]).status_detail
        == "The app restarted before this was sent"
    )
    assert app_state.store.send("done1").status == "accepted"


def test_a_live_send_goes_down_every_open_page_of_the_device(
    client, app_state, wait
) -> None:
    sign_up(client, "phone", "https://push.example.org/1", name="Phone")
    first_page = open_live_page(app_state, "phone")
    second_page = open_live_page(app_state, "phone")
    live = client.get("/api/devices").json["live_devices"]
    assert [(d["device_id"], d["connection_count"]) for d in live] == [("phone", 2)]

    (record,) = send(client, ["phone"], via="live", style="app-badge").json["sends"]

    for page in (first_page, second_page):
        payload = next_live_payload(page)
        assert payload["send_id"] == record["send_id"] and payload["via"] == "live"
        assert payload["app_badge_count"] == 3 and payload["is_update"] is False
    done = wait(settled(app_state, record["send_id"]))
    assert (done.status, done.status_detail, done.raw_response) == (
        "accepted",
        "Handed to the open page",
        None,
    )
    first_page.close()
    second_page.close()
    assert client.get("/api/devices").json["live_devices"] == []


def test_a_live_send_with_no_page_open_fails_and_a_live_only_device_can_be_sent_to(
    client, app_state, wait
) -> None:
    sign_up(client, "phone", "https://push.example.org/1")
    (closed,) = send(client, ["phone"], via="live").json["sends"]
    failed = wait(settled(app_state, closed["send_id"]))
    assert (failed.status, failed.status_detail) == (
        "failed",
        "The device's page isn't open, so there was no live connection",
    )

    page = open_live_page(app_state, "tab-only", name="A tab")
    (record,) = send(client, ["tab-only"], via="live").json["sends"]
    assert record["device_name"] == "A tab"
    assert next_live_payload(page)["send_id"] == record["send_id"]
    page.close()


def test_both_sends_once_through_each_channel(
    client, app_state, push_service, make_subscription, wait
) -> None:
    subscription = make_subscription(push_service.endpoint("phone"))
    sign_up(client, "phone", subscription.endpoint, keys=subscription.keys)
    page = open_live_page(app_state, "phone")

    records = send(client, ["phone"], via="both").json["sends"]

    assert sorted(r["via"] for r in records) == ["live", "push"]
    assert len({r["send_id"] for r in records}) == 2
    live_id = next(r["send_id"] for r in records if r["via"] == "live")
    assert next_live_payload(page)["send_id"] == live_id
    assert len(push_service.wait_for_count(1)) == 1
    for r in records:
        assert wait(settled(app_state, r["send_id"])).status == "accepted"
    page.close()


def test_an_updating_send_replaces_its_text_quietly_on_the_same_channel(
    client, app_state, monkeypatch, wait
) -> None:
    quick = STYLES_BY_ID["updating"].model_copy(update={"part_interval_seconds": 0.2})
    monkeypatch.setitem(runner.STYLES_BY_ID, "updating", quick)
    page = open_live_page(app_state, "phone", name="Phone")

    (record,) = send(client, ["phone"], via="live", style="updating").json["sends"]

    first, second, third = (next_live_payload(page) for _ in range(3))
    assert (first["body"], first["is_update"]) == ("From the test", False)
    assert [second["body"], third["body"]] == list(quick.part_bodies)
    for update in (second, third):
        assert update["is_update"] is True and update["options"]["silent"] is True
        assert (
            update["send_id"] == record["send_id"]
            and update["options"]["tag"] == "updating"
        )
    page.close()


# Receipts


def test_receipts_record_showing_taps_buttons_replies_and_a_real_dismissal(
    client, app_state
) -> None:
    sign_up(client, "phone", "https://push.example.org/1")
    (record,) = send(client, ["phone"], delay_seconds=300).json["sends"]
    url = f"/api/sends/{record['send_id']}/receipt"

    shown = client.post(
        url,
        json={
            "device_id": "phone",
            "event": "shown",
            "max_actions": 2,
            "app_version": "abc12345",
        },
    ).json
    assert (
        shown["shown_at"]
        and shown["device_max_actions"] == 2
        and shown["device_app_version"] == "abc12345"
    )
    again = client.post(url, json={"device_id": "phone", "event": "shown"}).json
    assert (
        again["shown_at"] == shown["shown_at"]
        and again["device_max_actions"] == 2
        and again["device_app_version"] == "abc12345"
    )

    tapped = client.post(
        url, json={"device_id": "phone", "event": "action", "action": "yes"}
    ).json
    assert tapped["clicked_at"] and [
        (i["kind"], i["action"]) for i in tapped["interactions"]
    ] == [("action", "yes")]
    # Firefox reports a dismissal when a tap closes the notification: not news after the tap.
    after_tap = client.post(url, json={"device_id": "phone", "event": "dismissed"}).json
    assert len(after_tap["interactions"]) == 1
    replied = client.post(
        url,
        json={
            "device_id": "phone",
            "event": "reply",
            "action": "reply",
            "reply": "On my way",
        },
    ).json
    assert replied["interactions"][-1]["reply"] == "On my way"
    assert replied["clicked_at"] == tapped["clicked_at"]

    assert (
        client.post(
            url, json={"device_id": "someone-else", "event": "shown"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/sends/nope/receipt", json={"device_id": "phone", "event": "shown"}
        ).status_code
        == 404
    )
    assert (
        client.post(url, json={"device_id": "phone", "event": "exploded"}).status_code
        == 400
    )


def test_a_dismissal_with_nothing_before_it_is_recorded(client, app_state) -> None:
    sign_up(client, "phone", "https://push.example.org/1")
    (record,) = send(client, ["phone"], delay_seconds=300).json["sends"]

    dismissed = client.post(
        f"/api/sends/{record['send_id']}/receipt",
        json={"device_id": "phone", "event": "dismissed"},
    ).json

    assert [i["kind"] for i in dismissed["interactions"]] == [
        "dismissed"
    ] and dismissed["clicked_at"] is None


# The send list and the raw record


def test_the_send_list_is_newest_first_filterable_and_capped(client, app_state) -> None:
    base = {
        "device_name": "n",
        "via": "push",
        "title": "t",
        "body": "b",
        "delay_seconds": 0,
        "status": "accepted",
    }
    for index in range(205):
        device = "a" if index % 2 else "b"
        app_state.store.put_send(
            SendRecord.model_validate(
                {
                    **base,
                    "send_id": f"s{index:03}",
                    "device_id": device,
                    "created_at": f"2026-01-01T00:00:{index // 60:02}.{index % 60:03}Z",
                }
            )
        )

    sends = client.get("/api/sends").json["sends"]
    assert (
        len(sends) == 200
        and sends[0]["send_id"] == "s204"
        and sends[-1]["send_id"] == "s005"
    )
    only_a = client.get("/api/sends?device_id=a").json["sends"]
    assert {s["device_id"] for s in only_a} == {"a"} and len(only_a) == 102


def test_the_raw_record_page_shows_the_whole_stored_send_escaped(
    client, app_state
) -> None:
    record = SendRecord.model_validate(
        {
            "send_id": "abc",
            "device_id": "d",
            "device_name": "<b>Phone</b>",
            "via": "push",
            "title": "<script>alert(1)</script>",
            "body": "b",
            "delay_seconds": 0,
            "created_at": "2026-01-01T00:00:00Z",
            "status": "accepted",
            "raw_response": {
                "url": "https://push.example.org/1",
                "status_code": 201,
                "headers": {"Location": "x"},
                "body": "ok",
            },
        }
    )
    app_state.store.put_send(record)

    response = client.get("/sends/abc/raw")

    page = response.get_data(as_text=True)
    assert response.status_code == 200 and response.headers["Content-Security-Policy"]
    assert (
        "<script>alert(1)</script>" not in page
        and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    )
    shown = runner.html.unescape(
        page.split("<pre class=raw>", 1)[1].split("</pre>", 1)[0]
    )
    assert json.loads(shown) == record.model_dump(mode="json")
    assert client.get("/sends/missing/raw").status_code == 404


# Chat notifications


def test_chat_notifications_go_to_active_devices_and_open_pages_by_default(
    client, app_state, push_service, make_subscription, wait
) -> None:
    active = make_subscription(push_service.endpoint("active"))
    sign_up(client, "active", active.endpoint, name="Active", keys=active.keys)
    sign_up(client, "expired", push_service.endpoint("expired"), name="Expired")
    app_state.store.put_device(
        app_state.store.device("expired").model_copy(update={"status": "expired"})
    )
    page = open_live_page(app_state, "tab", name="Tab")
    assert client.get("/api/chat-forwarding").json == {
        "is_enabled": True,
        "via": "both",
    }

    response = client.post(
        "/api/chat-notifications",
        json={"message": "Your report is ready", "chat_id": "chat-1"},
    )

    assert response.status_code == 201 and response.json["is_forwarded"] is True
    sends = response.json["sends"]
    assert sorted((s["device_id"], s["via"]) for s in sends) == [
        ("active", "push"),
        ("tab", "live"),
    ]
    for s in sends:
        assert (s["source"], s["chat_id"], s["title"], s["body"], s["style"]) == (
            "chat",
            "chat-1",
            "Imbue Studio",
            "Your report is ready",
            "plain",
        )
    assert next_live_payload(page)["title"] == "Imbue Studio"
    assert [p.path for p in push_service.wait_for_count(1)] == ["/push/active"]
    page.close()


def test_chat_forwarding_settings_are_kept_and_honoured(client, app_state) -> None:
    sign_up(client, "phone", "https://push.example.org/1")

    saved = client.put(
        "/api/chat-forwarding", json={"is_enabled": False, "via": "live"}
    )

    assert saved.json == {"is_enabled": False, "via": "live"}
    assert json.loads((app_state.data_dir / "chat_forwarding.json").read_text()) == {
        "is_enabled": False,
        "via": "live",
    }
    off = client.post(
        "/api/chat-notifications", json={"message": "Done", "title": "Report"}
    )
    assert off.status_code == 201 and off.json == {"is_forwarded": False, "sends": []}
    assert app_state.store.sends() == ()

    client.put("/api/chat-forwarding", json={"is_enabled": True, "via": "live"})
    live_only = client.post(
        "/api/chat-notifications", json={"message": "Done", "title": "Report"}
    ).json
    # Live only, and no page is open: nothing to send to, and the signed-up phone is not pushed.
    assert live_only == {"is_forwarded": True, "sends": []}
    assert (
        client.put(
            "/api/chat-forwarding", json={"is_enabled": True, "via": "carrier pigeon"}
        ).status_code
        == 400
    )
    assert (
        client.post("/api/chat-notifications", json={"message": ""}).status_code == 400
    )


# The device log and the policy report


def test_the_device_log_keeps_what_devices_report_newest_first(
    client, app_state
) -> None:
    for index in range(3):
        client.post(
            "/api/device-log",
            json={
                "device_id": "a" if index < 2 else "b",
                "source": "worker",
                "message": f"line {index}",
                "is_problem": index == 1,
            },
            headers={"User-Agent": "U" * 400},
        )

    entries = client.get("/api/device-log").json["entries"]
    assert [e["message"] for e in entries] == ["line 2", "line 1", "line 0"]
    assert entries[1]["is_problem"] is True and len(entries[0]["user_agent"]) == 300
    assert [
        e["message"] for e in client.get("/api/device-log?device_id=a").json["entries"]
    ] == ["line 1", "line 0"]
    assert (
        client.post(
            "/api/device-log", json={"source": "page", "message": "no id"}
        ).json["device_id"]
        == "unknown"
    )
    assert (
        client.post(
            "/api/device-log", json={"source": "elsewhere", "message": "x"}
        ).status_code
        == 400
    )
    assert len(json.loads((app_state.data_dir / "device_log.json").read_text())) == 4
    assert DEVICE_LOG_LIMIT == 2000


def test_policy_reports_are_logged_except_ones_caused_by_extensions(
    client, caplog
) -> None:
    caplog.set_level(logging.ERROR, logger="notification_tester.runner")
    report = {
        "blocked-uri": "https://cdn.example.org/x.js",
        "document-uri": "https://app/send",
        "effective-directive": "script-src",
    }

    assert client.post("/_csp-report", json={"csp-report": report}).status_code == 204
    assert (
        client.post(
            "/_csp-report",
            json={"csp-report": {**report, "blocked-uri": "chrome-extension://abc"}},
        ).status_code
        == 204
    )
    assert client.post("/_csp-report", data="not json").status_code == 204

    (logged,) = caplog.records
    assert (
        "blocked 'https://cdn.example.org/x.js' on 'https://app/send' (script-src)"
        in logged.getMessage()
    )
