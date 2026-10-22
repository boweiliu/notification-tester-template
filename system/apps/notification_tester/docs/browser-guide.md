# Browser setup and diagnosis

Use the app's top-level HTTPS URL, not its Studio iframe, for registration and
permission prompts. Loopback HTTP can support local development but a phone
cannot use the server's localhost. The manifest uses credentialed fetching so
it can load behind Studio's sign-in gate. Device permission and OS permission
are separate: both must allow notifications. After changing OS permission,
return to the device page and reload; reinstalling the app is usually unnecessary.
An installed PWA may lack an address bar, so use the browser/site settings or OS
notification settings to undo a blocked permission rather than repeatedly asking.

On iOS/iPadOS, Web Push is available to Home Screen web apps from 16.4 onward,
with permission requested through user interaction. This app's initial live
acceptance did not cover iOS or Android Chrome. Their presence in these setup
notes is not a claim that they were tested. See [WebKit's platform announcement](https://webkit.org/blog/13878/web-push-for-web-apps-on-ios-and-ipados/).

## Rich style experiments

These are diagnostic requests to the browser, not promises of an OS appearance.
Browsers can ignore unsupported options without error. Buttons depend on
`Notification.maxActions` and OS presentation; the app logs the reported limit.
See [MDN's action reference](https://developer.mozilla.org/en-US/docs/Web/API/Notification/actions).

| Style | What to look for |
| --- | --- |
| Plain | Title/body and basic shown/click receipts |
| Yes / No | Two action buttons and an action receipt |
| Quick replies | Three requested answers; browsers may show only two |
| Reply page | Reply button opens the app's text box |
| Inline reply | Experimental Chromium text action; may fall back to reply page |
| Big picture | Requested large image; often ignored |
| Sticky with buzz | Requests interaction, vibration and renotification; OS may ignore |
| Updating | Same tag replaces text over time; OS can coalesce updates |
| App badge | Requests count 3 on installed icon; requires Badging API support |

Initial manual acceptance on Android Firefox and macOS Chromium confirmed Push,
Live, action buttons and the reply page. Two buttons were visible in those
environments. Typed inline reply and the installed icon badge worked in the
macOS Chromium environment, but were unavailable in the Android Firefox
environment. Image and sticky/vibration options did not produce the requested
effect there. These are historical observations, not a maintained compatibility
matrix for all current versions. Test the exact browser/build and OS you use.

The badge is cleared when the receiving page opens/becomes visible, on a
notification tap, or through its Clear app badge button. Replies and Yes/No
actions remain in the tester's interaction log; no agent receives an answer.
The plain body tap returns to the tester rather than navigating to a source chat.

## Check versions before interpreting capability results

The backend fingerprints browser assets at process startup and injects that
version into pages and `/sw.js`. Static responses use `Cache-Control: no-cache`.
The device page asks the worker for its version, compares `/api/version`, requests
a worker update and reloads once when needed. `skipWaiting` and `clients.claim`
activate the current worker. The sender flags receipts from older worker versions.

Restart the backend after changing static assets so it computes a new fingerprint.
On each test device, compare both page and worker versions. Reload and allow the
worker update before trying rich styles. An absent version or `max_actions`
receipt can indicate an old worker rather than a browser limitation. Do not
clear all browser data as the first remedy; that also removes the subscription.

## When a notification seems missing

1. Check the app/worker versions and device registration. Test Plain first.
2. Inspect the send's provider response and receipt, then Device activity.
   HTTP 404/410 from a provider generally means a stale subscription; register
   again. HTTP 401/403 can indicate signing/claims trouble; inspect the raw answer.
3. Check browser site permissions and the OS permission for the actual browser
   or installed app. Browser brands that look alike can have different OS entries.
4. Check Focus/Do Not Disturb, battery restrictions, connectivity and expired
   Studio sign-in. Push can be delayed while a device is idle. A quiet OS banner
   is not proof that the server send failed.
5. For Live, keep the page active and connected. SSE buffering or a suspended
   tab can delay it; a service worker is not a permanently running process.

The app logs shown/click/reply events and callback failures. Browser developer
tools remain useful when authentication or networking prevents those logs from
reaching the backend. No provider response alone proves OS-level presentation.
