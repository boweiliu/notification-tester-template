# Installation

## Target prerequisites

- An existing Studio workspace with `system/apps/*` in its uv workspace,
  `system/libs/app_schema`, `system/libs/app_manifest`, `forward_port.py`,
  `tool_env.py` and a running supervisord including `system/supervisord.conf.d/*.conf`.
- Python 3.11 or newer, Bash, uv and supervisorctl. Studio itself may require a
  newer Python. Use the version its workspace declares.
- Permission to write the target app/config directories, Studio's tool directory
  (normally `/root/.local`), and supervisor logs. Network access or a warm uv
  cache is needed for dependencies. HTTPS sharing and account access are Studio's
  responsibility; the installer does not create or change them.
- `imbue-common` declared by a pinned HTTPS git source or local path in the
  target root's `pyproject.toml`. `app-schema` and `app-manifest` are target
  workspace libraries. These names must not be assumed to exist on PyPI.
- Studio's `notify-user` skill scripts (`.agents/skills/notify-user/scripts/notify_user.py`
  and `notify_user_test.py`). The app's manifest names them, and Studio's
  manifest validation refuses the install when they are missing.

```bash
bash /path/to/notification-tester/install.sh --studio-root /home/user/workspace
```

No browser window appears unless `--open` is passed. Optional `--port 8180`
selects a free port; otherwise the installer preserves its previous choice or
chooses an unregistered, available port between 8080 and 8999. The app always
listens on loopback. It is registered through Studio's `forward_port.py` and
remains running without a Studio window because it receives API calls and holds
Live connections. The service definition is generated for the selected root,
tool executable, port and data path, rather than copying hardcoded paths.

The installer uses Studio's `tool_env.py` settings, including `TOOL_ENV_HOME`
for isolated environments. It supplies uv's supported `--overrides` file for
the local `app-schema`, `app-manifest` and pinned `imbue-common` sources, then
runs `uv sync --all-packages` from the Studio root. Overrides are stored under
`data/.state/notification-tester-install/`. Other Studio tools are not reinstalled.

## Repeating or adopting an installation

Re-run the same command to update code. Installation ownership and the port live
in `data/.state/notification-tester-install/receipt.json`. The installer serializes
its own runs. It does not clear app data, remove stale files, rotate VAPID keys,
delete subscriptions or reset forwarding settings. The service restarts; pending
in-memory delayed sends cannot survive that restart.

It refuses an existing unowned app/config/registration by default. For a known
older installation of this exact app, inspect its manifest, package, service
configuration and data backup before using `--adopt-existing`. Adoption checks
the package and manifest identity and service command; it is an explicit assertion
of ownership, not a force-overwrite option for unrelated apps. A registration
owned by a different program, a different package identity, conflicting port or
symlinked destination is refused. Installing in place from the target's own app
directory is also supported. Do not run two installers or other service editors
against this app simultaneously.

Failures return nonzero. Source or tool changes may already have landed before
a dependency/startup failure; data is preserved, but installation is not a
transactional rollback. Fix the reported cause and repeat. Keep an ordinary code
backup when updating an unmanaged installation.

## Optional Studio chat notification copying

```bash
bash install.sh --studio-root /home/user/workspace --enable-chat-forwarding
```

The app exposes `POST /api/chat-notifications` regardless of this option. Its
sender page controls whether forwarding is enabled and which channels to use.
The default app setting is enabled, Both. The installer option connects Studio's
notifier to that endpoint; without it there is no automatic connection on a
fresh workspace. Existing native `copy_to_notification_tester` integration is
kept as-is, so it does not send a second copy.

For a recognized Studio notifier, the installer adds one best-effort call after
the original platform send. It preserves Studio delivery, watcher checks,
credentials handling and exit status. The helper is the standard-library-only
`integration/forward_chat_notification.py`, with a three-second timeout and a
registry lookup. The original notifier is backed up once in the install state
directory. The helper never needs a gateway key.

Unknown notifier versions are left unchanged with a warning. An integration
maintainer can call `copy_notification(message, title, studio_root_path)` from
that helper after the platform send, or directly POST the documented JSON API.
Do not replace the target's notifier wholesale. The app setting can disable
forwarding without changing the script; removing an additive hook requires
reviewing the marked block, rather than restoring an old backup over a newer
platform notifier. Replies are recorded in the tester; they do not answer chats.

## Isolated installs

For private validation, provide `--supervisor-config /absolute/test/supervisord.conf`
and `--log-dir /absolute/test/logs`, with `TOOL_ENV_HOME` pointing to a disposable
tool home. The supervisor must include only the test tree's service configs.
Use a fresh target tree and fresh data. Never register real device subscriptions
or invoke the platform notifier in installation checks. These switches do not
create a sandbox by themselves: supply a genuinely isolated root and supervisor.
