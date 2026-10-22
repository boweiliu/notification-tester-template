# Architecture and API

The Flask/Werkzeug backend serves a receiving page (`/`), a sending page
(`/send`), a reply page (`/reply`), a PWA manifest and `/sw.js`. The service
worker receives Push messages and page messages for Live notifications, shows
notifications with the same style logic, and posts receipts and interactions.

## Delivery paths

**Push:** browser creates a PushSubscription using the app's public VAPID key;
the backend stores that subscription. A send encrypts its JSON payload and signs
the request using the retained private VAPID key. `pywebpush` posts it to the
subscription's browser-selected push endpoint. The browser can wake the service
worker when the page is closed. The provider is generally Mozilla for Firefox,
Google for Chrome/Chromium builds using FCM, or Apple for Safari. A Chromium
brand alone does not establish whether Push is configured in that build.

The subscription endpoint is the source of truth for which provider is used.
The provider carries encrypted payloads but still sees delivery metadata.
An ordinary PWA cannot use an indefinitely running service worker socket as a
replacement for that provider. A different native app or push stack would be a
different architecture. Push TTL is one hour; urgency is high. The signing
subject defaults to the nonpersonal placeholder in `push.py`; operators may
set an appropriate contact there before deployment.

**Live:** the open device page keeps an EventSource connection to
`/api/live/<device_id>/events`. The backend queues notifications to that
connection, and the page asks its service worker to show them. Heartbeats are
sent every ten seconds; streams reconnect after their thirty-minute lifetime.
Live avoids the push-provider hop but cannot guarantee delivery while a tab is
suspended or closed. Its connection status can lag until the next write.

**Both:** independent sends over each channel, with separate receipts. An open
page with a valid Push subscription can display two notifications. There is no
cross-channel deduplication. Timers handle delayed sends and updating styles;
timers and Live presence are in memory and are lost on restart.

## Raw records and diagnostic meaning

The JSON store preserves device subscriptions, send requests, source (`manual`
or `chat`), source chat identifiers when supplied, provider response URL/status/
headers/body, delivery receipts, actions, replies and versions. Device activity
records preserve page and service worker diagnostics. These are operational
records, not analytics that discard their source.

The send page links `/sends/<send_id>/raw`, which renders the stored record as
pretty-printed JSON. `/api/sends` and `/api/device-log` expose the corresponding
data. A provider acceptance is distinct from a `shown` receipt; a `shown` receipt
means the browser's show call completed, not that the user saw or read an alert.
A missing receipt can mean an offline device, stale code, authentication trouble
or a failed network callback. A click/action/reply is a further, independent
observation. Firefox can emit a close event after a click; the server filters
that from the dismissal interpretation.

## API discovery

`GET /api/schema` serves generated OpenAPI with request/response models and all
routes. Use that document for exact field names and allowed values.

| Route | Purpose |
| --- | --- |
| `GET /health` | `{"status":"ok"}` readiness probe |
| `GET /api/version` | Current browser asset fingerprint |
| `GET /api/styles` | Style IDs, options and descriptions |
| `GET /api/vapid-public-key` | Browser subscription public key |
| `GET, POST /api/devices` | List or register devices |
| `PATCH, DELETE /api/devices/<device_id>` | Rename or remove a registration |
| `GET, POST /api/sends` | Inspect or create sends |
| `POST /api/sends/<send_id>/receipt` | Shown/click/action/reply/dismissal reports |
| `GET, POST /api/device-log` | Read or append diagnostics |
| `GET, PUT /api/chat-forwarding` | Copying settings |
| `POST /api/chat-notifications` | Forward a chat message using app settings |

A chat copy body is `{"message":"Example message","title":"Optional title",
"chat_id":"optional-source-id"}`. A successful request returns HTTP 201,
including when forwarding is disabled. It can notify every active registration
and connected Live page, so installation checks must not use a real data store.

## Trust boundary

The backend binds loopback. Studio's HTTPS sharing and account gate protect
remote access; the app does not implement its own user accounts or per-device
authorization. A caller with app access can inspect subscription and message
records and request sends. Do not expose the raw port to an untrusted network
without equivalent authentication and TLS. Share URLs and data backups must be
treated as private. A browser's expired Studio sign-in can prevent receipt/log
POSTs or later subscription operations even when Push itself arrives.
