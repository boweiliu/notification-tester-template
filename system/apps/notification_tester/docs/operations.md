# Operations and backup

Run Studio commands from its root. The installed service is `notification-tester`.

```bash
supervisorctl status notification-tester
supervisorctl restart notification-tester
supervisorctl tail notification-tester stderr
uv run --no-sync workspace-layout list
```

The last command provides the registered/share link; append `/send` for the
sender. The default supervisor logs are
`/var/log/supervisor/notification-tester-stdout.log` and `-stderr.log`, rotated at
10 MB with three backups. An isolated installation can choose a different log
directory. `/health`, `/api/version` and `/api/schema` are safe readiness/read
checks; avoid sending notifications just to determine whether the server is up.

## Data that must survive

The default data directory is `data/.apps/notification-tester/`. The backend
honors `NOTIFICATION_TESTER_DATA_DIR`; `NOTIFICATION_TESTER_PORT` overrides its
default 8080 listen port. The generated service config sets both explicitly.
There is currently no host override: the backend binds `127.0.0.1`.

| File | Meaning |
| --- | --- |
| `vapid_private_key.pem` | Server signing key bound to existing subscriptions |
| `devices.json` | Device identities and raw Push subscriptions |
| `sends.json` | Request history, raw provider responses, receipts and interactions (newest 2,000 sends) |
| `device_log.json` | Receiving page and worker diagnostic events (newest 2,000 entries) |
| `chat_forwarding.json` | Enabled/channel preferences |

Back up the entire data directory privately, together with the app's stable
HTTPS origin/Studio registration information. Also preserve the install receipt
and generated service configuration for operational recovery. Never add data,
subscriptions, logs or private keys to a source export or public template.

Never delete or regenerate an existing VAPID key during installation or upgrade.
The backend creates one only when the file is absent, readable only by its
owner. Losing it invalidates the ability to use existing subscriptions; restore
the original key or have every device subscribe again. Keep restrictive
filesystem permissions and protect the backup as a secret. A code export is not a data backup.

For a consistent manual snapshot, stop this service, copy the data directory to
a private disk-backed backup location, then restart it. Do not restore a snapshot
over a running service. Stop, restore the whole consistent store and key, verify
ownership/permissions and restart. Restoring on a different HTTPS origin normally
requires fresh browser registration even when the server key is retained.

## Upgrade and remove

Repeat the installer with the new package. It restarts only this service and
does not overwrite data. On startup, waiting/sending items interrupted by a
restart are reported as failed; they are not replayed automatically. Updating
notification styles also use timers that cannot survive a process restart.
Asset edits require restart to recompute versions, followed by device version
checks. The process stays running with no windows to support API/Live use.

Removal is an explicit operational action: stop/remove this service definition
and unregister the app through Studio's normal tooling. Keep its data and key
unless the owner separately asks to delete them. If enabled, disable the app's
chat-forwarding setting or remove only the marked optional notifier block.

## Safe verification and current limits

Use a fresh isolated Studio tree, tool home, supervisor socket, logs and data for
installer checks. Exercise first install and repeat install; check the app pages,
API schema, registration, unchanged VAPID key, and name/port conflict refusal.
Use Playwright with a private browser context for page checks. Never point a
verification send at real subscriptions, and never call the real platform
notifier from an installation test.

The package's tests cover the API routes and stored records over fresh data, the
pages in a real Chromium against a throwaway server and a local stand-in push
service, the installer against a scratch Studio root with its own supervisord,
and the export. Run them from the Studio root with
`uv run pytest system/apps/notification_tester`. They never touch the installed
app's data or a real push service. They do not cover concurrent-process
durability or a multi-user authorization model, and they cannot stand in for a
real phone: OS presentation still needs the manual checks in the browser guide.
JSON persistence and in-memory Live/timers are
suited to a small single-workspace diagnostic app, not a delivery SLA. There is
no automated migration, delayed-send recovery or cross-channel deduplication.
