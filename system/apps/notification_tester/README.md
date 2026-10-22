# Notification tester

A small Studio app for trying OS notifications on phones and desktops. Register a
device at `/`, send from `/send`, and inspect delivery receipts, device activity,
button presses and replies. Compare Web Push with direct delivery to an open page.

## Install into Studio

From a standalone copy of this package, run:

```bash
bash install.sh --studio-root /home/user/workspace
```

The target must be an existing Imbue Studio workspace. This is an app package,
not a replacement for Studio. The installer copies app code, resolves platform
dependencies from the target workspace, installs the app's tool, syncs all
workspace packages, registers a free port and starts its own supervised service.
It does not show a window. Add `--open` to show it after validation.

Use `--port 8180` to choose a free port. Repeating the command updates an
installer-owned installation and preserves its port, devices, history and VAPID
key. An older unmanaged installation requires inspection and explicit
`--adopt-existing`. See [installation](docs/installation.md) for prerequisites,
ownership checks and optional chat notification forwarding.

## Try it

1. Get the app's HTTPS share link from Studio's app list. On the receiving
   device, open that link directly in a browser, sign in, and install/add it to
   the Home Screen where appropriate. Do not request permission inside an iframe.
2. On the device page, allow notifications and register the device. Check both
   the app version and background worker version.
3. Open `/send` on the same app origin in another tab or device. Choose a device,
   Push, Live or Both, and a style. Start with Plain.
4. Check the OS notification and the send's receipt. Try buttons/replies and
   inspect the activity log. Both can show two notifications while the page is open.

Push can work after the page closes; Live requires an open, connected page.
OS settings, Focus/Do Not Disturb, battery policy and browser configuration still
control whether and when a visible alert appears. HTTP success from a push
provider means accepted for delivery, not shown to a person.

## Documentation

- [Installation and chat forwarding](docs/installation.md)
- [Delivery architecture and API](docs/architecture.md)
- [Browser behavior, rich actions and diagnosis](docs/browser-guide.md)
- [Operations, upgrades and backup](docs/operations.md)

The app serves its route and model reference at `/api/schema`. Every send has a
raw record link at `/sends/<send_id>/raw`; push-provider responses, receipts and
interaction details remain available there. Device activity is also available
at `/api/device-log`.

## Source and portability

In Studio source, `system/apps/notification_tester/` is the authoritative app.
From the Studio root, export a clean saved snapshot with:

```bash
uv run python system/apps/notification_tester/scripts/export.py /absolute/empty/destination
```

The deterministic export contains this directory's tracked code, assets, tests,
installer and docs, plus `SOURCE.md` and a SHA-256 file manifest. It carries no
source history, app data, signing keys, subscriptions, logs or chat records.
The standalone tree has the same app layout at its root. Its installer copies
that layout back to the target Studio app directory. Exporting requires the
authoritative monorepo; it is not a tool for importing workspace history into a
standalone copy.

This is the initial portable package. Full hardening and cross-browser coverage
are deliberately deferred until after initial publication. The existing tests
travel with the app; installation checks are described in the operations guide.
