"""The notification styles a send can use: what each looks like, as the options the browser's showNotification takes."""

from imbue.imbue_common.frozen_model import FrozenModel
from pydantic import Field

ICON = "/static/icon-192.png"
# The small one-colour icon Android shows in the status bar.
BADGE = "/static/badge-96.png"
SAMPLE_IMAGE = "/static/sample-picture.png"


class NotificationStyle(FrozenModel):
    """One way a notification can look and behave."""

    style_id: str = Field(description="The style's id, as a send names it")
    name: str = Field(description="What the send page calls it")
    description: str = Field(description="What it does, and where it is known to work")
    options: dict[str, object] = Field(description="The showNotification options it adds (actions, image, vibration, ...)")
    part_bodies: tuple[str, ...] = Field(
        default=(), description="For a notification that updates in place: the text of each later update"
    )
    part_interval_seconds: int = Field(default=4, description="Seconds between updates")
    app_badge_count: int | None = Field(default=None, description="A count to put on the installed app's icon")


STYLES: tuple[NotificationStyle, ...] = (
    NotificationStyle(
        style_id="plain",
        name="Plain",
        description="Title and message only.",
        options={},
    ),
    NotificationStyle(
        style_id="yes-no",
        name="Yes / No",
        description="Two buttons; the tap is recorded without opening anything. Stays until answered where supported.",
        options={
            "actions": [{"action": "yes", "title": "Yes"}, {"action": "no", "title": "No"}],
            "requireInteraction": True,
        },
    ),
    NotificationStyle(
        style_id="quick-replies",
        name="Quick replies",
        description="Three canned answers. Most browsers show only the first two; the device page says how many it can.",
        options={
            "actions": [
                {"action": "on-it", "title": "On it"},
                {"action": "later", "title": "Later"},
                {"action": "stop", "title": "Stop"},
            ],
        },
    ),
    NotificationStyle(
        style_id="reply-page",
        name="Reply (opens a box)",
        description="A Reply button that opens a small page with the text box ready, plus an Open button.",
        options={"actions": [{"action": "reply", "title": "Reply"}, {"action": "open", "title": "Open"}]},
    ),
    NotificationStyle(
        style_id="inline-reply",
        name="Typed reply (experimental)",
        description=(
            "Asks for a text box inside the notification itself. Only some Chromium builds ever supported it; "
            "elsewhere it shows as a plain Reply button that opens the reply page."
        ),
        options={
            "actions": [{"action": "reply", "title": "Reply", "type": "text", "placeholder": "Type a reply..."}],
        },
    ),
    NotificationStyle(
        style_id="big-picture",
        name="Big picture",
        description="A large image inside the notification and a status-bar badge. Shown by Chrome on Android and Windows.",
        options={"image": SAMPLE_IMAGE, "badge": BADGE},
    ),
    NotificationStyle(
        style_id="sticky-buzz",
        name="Sticky with buzz",
        description="Stays on screen until dealt with, vibrates in a pattern, and alerts again if one is already showing.",
        options={
            "requireInteraction": True,
            "vibrate": [200, 100, 200, 100, 600],
            "renotify": True,
            "tag": "sticky-buzz",
        },
    ),
    NotificationStyle(
        style_id="updating",
        name="Updating in place",
        description="One notification whose text changes three times, a few seconds apart, without new alerts.",
        options={"tag": "updating", "renotify": False},
        part_bodies=("Step 2 of 3: still going...", "Step 3 of 3: done."),
    ),
    NotificationStyle(
        style_id="app-badge",
        name="App badge",
        description="Puts a count of 3 on the installed app's icon (Dock or Home screen); opening the device page clears it.",
        options={},
        app_badge_count=3,
    ),
)

STYLES_BY_ID = {style.style_id: style for style in STYLES}
