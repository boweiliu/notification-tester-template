---
title: "Notification tester"
description: "Test phone and desktop Web Push and Live notifications, rich actions, browser support and delivery diagnostics"
thumbnail: "template.svg"
version: v2
format: v2
---

# Notification tester

This file is the manifest for the **Notification tester** template (slug:
`notification-tester-template`). It is the one document a future agent reads to understand,
present, and adapt this template. If you are an agent in a workspace that was
created from this template, this file is your script: read all of it, then
follow "How to adapt it" below.

## What it is

Test phone and desktop Web Push and Live notifications, rich actions, browser support and delivery diagnostics

Notification tester answers one practical question: will a notification from this workspace actually reach my phone or computer, and what does it look like when it does? A receiving page signs a device up (a phone with the app on its Home Screen, a desktop browser, or an installed desktop web app). A sending page then sends that device a test notification and shows what happened to it: whether the browser vendor's push service accepted it, whether the device reported showing it, and which button or reply the person used.

It tries two delivery paths side by side. **Push** goes through the browser vendor's push service and can arrive with the page closed. **Live** goes straight from the workspace to a page that is open. Nine notification styles (plain, Yes/No buttons, quick replies, a reply page, an inline reply, a big picture, sticky with buzz, updating, and an app badge) show what each browser and operating system really supports. Every send keeps its raw record, including the push service's answer, one click from the sender page. The app also copies each chat notification the agent sends the user onto the user's own devices, so it works as a second notification channel as well as a test bench.

## How it works

The snapshot includes these paths (each is a repo-root-relative path copied
from the original agent onto a clean default-workspace-template base):

- `system/apps/notification_tester`
- `system/supervisord.conf.d/notification-tester.conf`
- `.agents/skills/notify-user/scripts/notify_user.py`
- `.agents/skills/notify-user/scripts/notify_user_test.py`

- `system/apps/notification_tester` is the app: a Flask backend that serves the receiving page (`/`), the sending page (`/send`), a reply page (`/reply`), a service worker, a web app manifest and a JSON API (described at `/api/schema`). It also carries its own documentation (`docs/`), an installer for existing workspaces (`install.sh`, `scripts/install.py`), a deterministic export script (`scripts/export.py`) and the optional notifier hook (`integration/`).
- `system/supervisord.conf.d/notification-tester.conf` runs it as the supervised program `notification-tester`. The program registers port 8080 with `forward_port.py` (which lists the app in the workspace and gives it its HTTPS share address) and then starts the `notification-tester` command. The app listens on loopback only and keeps running with no window open, because it holds Live connections and receives callbacks from devices.
- `.agents/skills/notify-user/scripts/notify_user.py` (and its test) is the workspace's existing notify-user script with one additive, best-effort step: after the normal notification it posts a copy to the tester's `/api/chat-notifications` when the tester is registered. When the tester is absent nothing is tried, and a failure prints one warning without changing the exit code.

**Push path.** A device's browser creates a subscription with the app's public signing key and the backend stores it. A send encrypts its payload, signs it with the private key (`pywebpush`) and posts it to the endpoint the browser chose (Mozilla, Google or Apple, depending on the browser). The browser wakes the app's service worker, which shows the notification and posts a receipt back. **Live path.** The open device page holds an event stream to `/api/live/<device_id>/events`; the backend queues the notification there and the page asks its service worker to show it. **Both** sends over each path independently, with separate receipts and no deduplication.

State lives in `data/.apps/notification-tester/` (device subscriptions, send history with raw provider responses, device activity log, chat-copy settings and a signing key created on first start). It is never part of this template: each workspace creates its own key, and devices must register again. Browser asset versions are fingerprinted at startup and checked by the device page, so a stale service worker is flagged rather than mistaken for a browser limitation.

A workspace created from this template installs the app's command at build time, like every app with an `app.toml`. A workspace that merged the template into an existing one has the files but not the command; see the first item under Requirements.

## Recipe

This template is version `v1`. It is not a fork of the
workspace it came from -- it is DERIVED from it by a recipe: include these
paths, leave these out, apply these published-version rules. An update re-runs
the recipe against the current workspace and publishes the result as the next
version, so anything excluded stays excluded even though it still exists in the
source workspace.

The recipe is machine-read, so it lives in the sibling
[`template.toml`](template.toml) -- its `[recipe]` table -- along with
the structured requirements and the environment this template needs
installed. That file is authoritative for all of it; this one holds the prose.

## Requirements

Everything the adopting agent must deal with before this template is really
theirs. Two kinds of entry, handled at different times:

- **Activation** -- what must be SET UP before anything runs, in the
  machine-readable `requires_` forms below. The adopting agent acts on these
  ITSELF, first, before asking anything.
- **Adaptation** -- what must be DECIDED or REWIRED, in prose. Worked through
  interactively with the user, after activation.


**Activation:** none. The app needs no external account, latchkey permission, secret or LLM access, and its Python packages are pinned in this repository's `uv.lock`.

**Adaptation** (decide or rewire with the user, in this order):

- **Install the app into the running workspace.** A workspace built from this template already has the command. A workspace that merged the template into an existing one must install it once: from the workspace root run `bash system/apps/notification_tester/install.sh --studio-root <workspace root>` (add `--port <free port>` to move off 8080). It installs the command, syncs packages, rewrites the service definition under installer ownership, starts the service and waits for `/health`. No window opens. Running it from the app's own directory in place is supported.
- **Give each device an HTTPS address and a sign-in.** Phones cannot reach the workspace's loopback address, and browsers only allow notification permission on HTTPS. The user must share the app through Studio's sharing, then open the share link directly (not inside the Studio window) on each device and sign in. Phones also need the page added to the Home Screen where the platform requires it (iOS and iPadOS 16.4 or newer).
- **Decide whether chat notifications are copied to devices.** The setting defaults to on, on both channels, so once the app is registered every chat notification is also pushed to every registered device. Ask the user whether to keep that, change the channel, or turn it off on the sender page.
- **Reconcile the notify-user script.** The included `notify_user.py` is the version this template was built on, plus the copy step. If the adopting workspace's own copy has moved on, add the small `copy_to_notification_tester` step to it instead of overwriting the newer script. `install.sh --enable-chat-forwarding` can add the same step to a recognised notifier.
- **Check port 8080.** If another program in the adopting workspace already uses it, reinstall with `--port` so the service definition is regenerated on a free port.

## Environment

What this template needs INSTALLED, beyond what the template already has.
Declared in `template.toml`'s `[environment]` table; an adopting agent
converges it at ITS OWN pinned apt snapshot timestamp, so package versions come
out consistent with the rest of that agent's environment rather than frozen to
whatever this publisher happened to have.

Nothing extra -- runs on the stock workspace environment.

The app's Python packages (`pywebpush`, `flask-sock` and their dependencies) come from the lockfile and are installed with the app's own command. There are no apt packages, global tools or environment units to install.

## How to adapt it

Instructions for the NEXT agent -- the one adapting this template into a
new agent. This is the `use-template` skill's template path; in short:

1. Read this entire file first, especially "Requirements" below. It holds two
   kinds of entry and they are handled at different times: the machine-readable
   `requires_` lines are ACTIVATION (set them up before anything runs), and
   the prose bullets are ADAPTATION (decide or rewire them afterwards).
2. Present the template to the user in plain, non-technical language: what
   it is, what it does, and what it needs from them (name the activation
   requirements).
3. Ask whether they want to use the same connectors (e.g. their own Slack).
   If YES: ACTIVATE FIRST -- initiate every `requires_permission` line NOW
   via a latchkey permission request (see the `latchkey` skill; the request
   opens the approval/login flow in the Imbue Studio app), wire up any
   `requires_secret` values, start the services, and get the app showing
   THE USER'S OWN DATA. Done for a data-backed app means the user can open it
   and see their own data -- NOT that a service starts or an endpoint returns
   200. Then tell them it is live and to take a look.
4. Only AFTER that (or immediately, if they chose different connectors -- the
   swap is then the first adaptation) ask: "How do you want to adapt it?"
5. Work through each requirement interactively, one at a time. Translate each
   into plain language, ask for a decision only when you genuinely need one,
   and resolve the obvious ones yourself.
6. When done, append a dated entry to "Adaptation history" below (never
   rewrite earlier entries) and commit.

## Publication history

This template's changelog: what each published version changed. The PUBLISHER
appends one entry per version (newest last); earlier entries are never rewritten.
This is distinct from "Adaptation history" below, which is the ADOPTERS' log.

### v1 (2026-10-22) -- First publication: the portable Notification tester app with its installer, documentation and chat-notification copy step

### v2 (2026-10-22) -- Hardening pass: fixed the blocked-permission help button, bounded the send history, owner-only signing key, raw-record link on every send, working chat-forwarding install on a stock notifier, repeatable failed installs, and added browser, API, installer and export tests

## Adaptation history

Each agent that adapts this template appends one dated entry below. Earlier
entries are never rewritten.
