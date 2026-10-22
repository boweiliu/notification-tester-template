"""Optional, standard-library-only copy of a Studio notification to this app.

Never replaces Studio's notification delivery or changes its result.
"""

import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path


def copy_notification(message: str, title: str | None, root: Path) -> None:
    """Copy to the registered local app, warning on failure; no registration means no-op."""
    if not message.strip():
        return
    registry = Path(os.environ.get("MINDS_APPS_FILE") or "data/.state/apps.toml")
    if not registry.is_absolute():
        registry = root / registry
    try:
        rows = tomllib.loads(registry.read_text()).get("apps", [])
        row = next((r for r in rows if r.get("name") == "notification-tester"), None)
        if row is None:
            return
        body = {"message": message.strip(), "chat_id": os.environ.get("MINDS_CHAT_ID") or os.environ.get("MNGR_AGENT_ID", "")}
        if title:
            body["title"] = title.strip()
        request = urllib.request.Request(
            row["url"].rstrip("/") + "/api/chat-notifications",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            if response.status != 201:
                sys.stderr.write("notification-tester: chat copy was not accepted\n")
    except (OSError, ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        sys.stderr.write(f"notification-tester: optional chat copy failed ({type(exc).__name__})\n")
