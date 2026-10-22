"""Sending Web Push messages: the app's own signing key, and one encrypted post to a device's push service."""

import base64
import binascii
import json
from pathlib import Path
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from notification_tester.store import PushSubscription, RawPushResponse

# The contact the push service sees in the signed claim. It must be a mailto: or https: URL;
# nothing is ever sent to it.
VAPID_SUBJECT = "mailto:notification-tester@example.com"
# How long the push service holds a message for a device that is offline.
PUSH_TTL_SECONDS = 3600

PUSH_SERVICE_NAMES = {
    "mozilla.com": "Mozilla",
    "googleapis.com": "Google",
    "apple.com": "Apple",
    "windows.com": "Microsoft",
    "notify.windows.com": "Microsoft",
}


def push_service_name(endpoint: str) -> str:
    host = urlparse(endpoint).hostname or ""
    for suffix, name in PUSH_SERVICE_NAMES.items():
        if host == suffix or host.endswith("." + suffix):
            return name
    return host or "an unknown push service"


class VapidKeys:
    """The app's signing key pair, made once and kept in the data directory."""

    def __init__(self, data_dir: Path) -> None:
        self._key_path = data_dir / "vapid_private_key.pem"
        if not self._key_path.is_file():
            data_dir.mkdir(parents=True, exist_ok=True)
            vapid = Vapid()
            vapid.generate_keys()
            vapid.save_key(str(self._key_path))
            # The private key signs every push, so only the app's own user may read it.
            self._key_path.chmod(0o600)
        self._vapid = Vapid.from_file(str(self._key_path))

    @property
    def public_key(self) -> str:
        """The public key as the browser's subscribe call wants it: unpadded base64url of the raw point."""
        raw = self._vapid.public_key.public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    @property
    def vapid(self) -> Vapid:
        return self._vapid


def send_push(keys: VapidKeys, subscription: PushSubscription, payload: dict) -> RawPushResponse:
    """Post one encrypted message to the device's push service and return its answer exactly as received."""
    try:
        answer = webpush(
            subscription_info=subscription.model_dump(),
            data=json.dumps(payload),
            vapid_private_key=keys.vapid,
            vapid_claims={"sub": VAPID_SUBJECT},
            ttl=PUSH_TTL_SECONDS,
            headers={"Urgency": "high"},
            timeout=20,
        )
    except WebPushException as e:
        if e.response is None:
            return RawPushResponse(url=subscription.endpoint, status_code=None, headers={}, body="", error=str(e))
        answer = e.response
    except (requests.RequestException, ValueError, binascii.Error) as e:
        # A connection failure, or keys the browser handed over that do not decode.
        return RawPushResponse(
            url=subscription.endpoint, status_code=None, headers={}, body="", error=f"{type(e).__name__}: {e}"
        )
    # webpush answers a string only in curl mode, which is never used here.
    assert isinstance(answer, requests.Response)
    response = answer
    return RawPushResponse(
        url=subscription.endpoint,
        status_code=response.status_code,
        headers=dict(response.headers),
        body=response.text,
    )
