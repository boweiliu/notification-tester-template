#!/usr/bin/env python3
"""Install this app into an existing Studio workspace, without copying any app data."""

import argparse
import fcntl
import importlib.util
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

NAME = "notification-tester"
PACKAGE = Path("system/apps/notification_tester")
OWNER = "notification-tester-portable-v1"
# The platform notifier's send call in the Studio versions this installer knows; the copy goes right after it.
NOTIFIER_CALL_SITES = (
    "    is_accepted = notify(\n        args.message, args.title, http=HttpClient(), environ=dict(os.environ)\n    )\n",
    "    is_accepted = notify(args.message, args.title, http=http, environ=environ)\n",
)


class InstallError(Exception):
    """An installation precondition was not met."""


def run(args: list[str], root: Path, env: dict[str, str], *, capture: bool = False) -> str:
    result = subprocess.run(args, cwd=root, env=env, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout or ""


def read_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def check_plain_path(path: Path, root: Path) -> None:
    """Refuse symlinks anywhere in a path we own or overwrite."""
    if not path.is_relative_to(root):
        raise InstallError(f"Path escapes Studio root: {path}")
    for part in (path, *path.parents):
        if part == root:
            break
        if part.is_symlink():
            raise InstallError(f"Refusing symbolic link: {part}")


def free_port(port: int) -> bool:
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def integration(root: Path) -> None:
    """Add only a known, narrow call site; never replace the platform notifier."""
    notifier = root / ".agents/skills/notify-user/scripts/notify_user.py"
    if not notifier.is_file() or notifier.is_symlink():
        sys.stderr.write("Chat forwarding skipped: no supported Studio notifier. See docs/installation.md.\n")
        return
    original = notifier.read_text()
    if "copy_to_notification_tester(" in original or "# notification-tester portable copy" in original:
        sys.stdout.write("Existing chat forwarding kept.\n")
        return
    anchor = next((a for a in NOTIFIER_CALL_SITES if original.count(a) == 1), None)
    if anchor is None:
        sys.stderr.write("Chat forwarding skipped: notifier version has a different call site. See docs/installation.md.\n")
        return
    if "_REPO_ROOT =" not in original:
        sys.stderr.write("Chat forwarding skipped: notifier has no root locator.\n")
        return
    hook = (
        "    # notification-tester portable copy (Studio delivery above stays authoritative)\n"
        "    try:\n"
        "        import runpy\n"
        f"        runpy.run_path(str(_REPO_ROOT / {str(PACKAGE / 'integration/forward_chat_notification.py')!r}))[\"copy_notification\"](args.message, args.title, _REPO_ROOT)\n"
        "    except (OSError, ValueError, ImportError, KeyError) as exc:\n"
        "        sys.stderr.write(f\"notification-tester: optional forwarding unavailable ({type(exc).__name__})\\n\")\n"
    )
    updated = original.replace(anchor, anchor + hook)
    compile(updated, str(notifier), "exec")
    backup = root / "data/.state/notification-tester-install/notifier-before-forwarding.py"
    check_plain_path(backup, root)
    if not backup.exists():
        backup.write_text(original)
    notifier.write_text(updated)


def install(args: argparse.Namespace) -> None:
    root = args.studio_root.resolve()
    source = Path(__file__).resolve().parents[1]
    if any(c in str(root) for c in "\n\r%"):
        raise InstallError("Studio root cannot contain a newline or percent sign.")
    required = ["pyproject.toml", "system/scripts/forward_port.py", "system/scripts/tool_env.py",
                "system/libs/app_schema/pyproject.toml", "system/libs/app_manifest/pyproject.toml",
                "system/supervisord.conf.d"]
    if not all((root / p).exists() for p in required):
        raise InstallError("Target is not a supported Studio root (missing platform scripts or libraries).")
    if sys.version_info < (3, 11) or not shutil.which("uv") or not shutil.which("supervisorctl"):
        raise InstallError("Python 3.11+, uv and supervisorctl must be installed.")
    project = read_toml(root / "pyproject.toml")
    workspace = project.get("tool", {}).get("uv", {}).get("workspace", {})
    if "system/apps/*" not in workspace.get("members", []):
        raise InstallError("Studio must include system/apps/* in its uv workspace.")
    sources = project.get("tool", {}).get("uv", {}).get("sources", {})
    common = sources.get("imbue-common", {})
    if not isinstance(common, dict):
        raise InstallError("Studio must declare its imbue-common source.")
    if "git" in common and "rev" in common:
        git_url = common["git"]
        parsed = urllib.parse.urlsplit(git_url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise InstallError("imbue-common must use a credential-free HTTPS source.")
        common_req = f"imbue-common @ git+{git_url}@{common['rev']}"
        if common.get("subdirectory"):
            common_req += f"#subdirectory={common['subdirectory']}"
    elif "path" in common:
        common_req = f"imbue-common @ {(root / common['path']).resolve().as_uri()}"
    else:
        raise InstallError("imbue-common needs a pinned git revision or local path; never assume PyPI.")
    dest = root / PACKAGE
    state = root / "data/.state/notification-tester-install"
    config = root / "system/supervisord.conf.d/notification-tester.conf"
    registry = root / "data/.state/apps.toml"
    for path in (dest, state, config, registry):
        check_plain_path(path, root)
    for path in (state / "receipt.json", state / "tool-overrides.txt", state / "install.lock"):
        check_plain_path(path, root)
    for other_config in (root / "system/supervisord.conf.d").glob("*.conf"):
        if other_config != config and "[program:notification-tester]" in other_config.read_text():
            raise InstallError("Another service configuration already declares notification-tester.")
    state.mkdir(parents=True, exist_ok=True)
    with (state / "install.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt_path = state / "receipt.json"
        receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
        owned = receipt.get("owner") == OWNER and receipt.get("root") == str(root)
        if owned and config.exists() and f"; Managed by {OWNER}" not in config.read_text():
            raise InstallError("Managed service configuration has been replaced; inspect before continuing.")
        rows = read_toml(registry).get("apps", []) if registry.exists() else []
        existing = next((r for r in rows if r.get("name") == NAME), None)
        if existing and existing.get("program") != NAME:
            raise InstallError("An unrelated registered app owns notification-tester.")
        if not owned and (dest.exists() or config.exists() or existing):
            if not args.adopt_existing and dest.resolve() != source:
                raise InstallError("Unmanaged installation exists. Inspect it, then use --adopt-existing only for this app.")
            if not (dest / "app.toml").is_file() or read_toml(dest / "app.toml").get("name") != NAME:
                raise InstallError("Existing app ownership conflicts; refusing adoption.")
            if read_toml(dest / "pyproject.toml").get("project", {}).get("name") != NAME:
                raise InstallError("Existing package ownership conflicts.")
            if config.exists() and ("[program:notification-tester]" not in config.read_text()
                                    or "notification_tester" not in config.read_text()):
                raise InstallError("Existing service configuration belongs to another app.")
        reserved = {urllib.parse.urlsplit(r.get("url", "")).port for r in rows if r.get("name") != NAME}
        old_port = receipt.get("port")
        if not old_port and existing:
            old_port = urllib.parse.urlsplit(existing.get("url", "")).port
        port = args.port or old_port
        if port is None:
            port = next((p for p in range(8080, 9000) if p not in reserved and free_port(p)), None)
        if not isinstance(port, int) or not 1024 <= port <= 65535:
            raise InstallError("No usable port; specify --port between 1024 and 65535.")
        if port in reserved or (port != old_port and not free_port(port)):
            raise InstallError("Requested port is already in use.")
        claim = json.dumps({"owner": OWNER, "root": str(root), "port": port}, indent=2) + "\n"
        # Claim ownership before the first change lands, so a run that fails part-way
        # (dependencies, validation, startup) can simply be repeated once the cause is fixed.
        # An adopted, unmarked service configuration is claimed only once it has been regenerated.
        if not (config.exists() and f"; Managed by {OWNER}" not in config.read_text()):
            receipt_path.write_text(claim)
        # Do not erase stale files or touch data. Copy only app-owned source files.
        if dest.resolve() != source:
            for original in sorted(source.rglob("*")):
                relative = original.relative_to(source)
                if any(p in {".git", ".venv", "__pycache__", ".pytest_cache", ".ty_cache", "data"} or p.endswith(".egg-info")
                       for p in relative.parts):
                    continue
                if original.is_symlink():
                    raise InstallError(f"Unexpected source symlink: {relative}")
                if not original.is_file():
                    continue
                target = dest / relative
                check_plain_path(target, root)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, target)
        env = dict(os.environ)
        env.pop("VIRTUAL_ENV", None)
        # Studio's supported tool directory override, shared with its build/update paths.
        spec = importlib.util.spec_from_file_location("studio_tool_env", root / "system/scripts/tool_env.py")
        if spec is None or spec.loader is None:
            raise InstallError("Cannot load Studio tool environment settings.")
        tool_env = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool_env)
        tool_home = tool_env.tool_home()
        bin_dir = tool_env.bin_dir(tool_home)
        env.update(UV_TOOL_DIR=str(tool_env.tools_dir(tool_home)), UV_TOOL_BIN_DIR=str(bin_dir))
        env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
        env["MINDS_APPS_FILE"] = str(registry)
        overrides = state / "tool-overrides.txt"
        overrides.write_text(
            f"app-schema @ {(root / 'system/libs/app_schema').as_uri()}\n"
            f"app-manifest @ {(root / 'system/libs/app_manifest').as_uri()}\n{common_req}\n"
        )
        run(["uv", "tool", "install", "--editable", str(dest), "--reinstall", "--overrides", str(overrides)], root, env)
        run(["uv", "sync", "--all-packages"], root, env)
        forward = shlex.join([sys.executable, str(root / "system/scripts/forward_port.py"), "--manifest", str(dest / "app.toml"), "--url", f"http://localhost:{port}"])
        command = shlex.join(["/bin/bash", "-c", forward + " && exec " + shlex.quote(str(bin_dir / NAME))])
        oom = root / "system/services/oom_priority/bin/oom_tag_service.py"
        if oom.exists():
            command = shlex.join([sys.executable, str(oom), "user"]) + " " + command
        log_dir = Path(args.log_dir).resolve() if args.log_dir else Path("/var/log/supervisor")
        log_dir.mkdir(parents=True, exist_ok=True)
        # quote config environment values (supervisord uses CSV-style quoting).
        def env_value(value: str) -> str:
            if any(c in value for c in '\n\r%"'):
                raise InstallError("Unsupported quote or newline in environment path.")
            return '"' + value + '"'
        config.write_text(
            f"; Managed by {OWNER}\n[program:{NAME}]\ncommand={command}\ndirectory={root}\n"
            f"environment=NOTIFICATION_TESTER_PORT=\"{port}\",NOTIFICATION_TESTER_DATA_DIR={env_value(str(root / 'data/.apps/notification-tester'))},MINDS_APPS_FILE={env_value(str(registry))}\n"
            "autostart=true\nautorestart=true\nstartsecs=1\nstartretries=5\nstopasgroup=true\nkillasgroup=true\n"
            f"stdout_logfile={log_dir / (NAME + '-stdout.log')}\nstderr_logfile={log_dir / (NAME + '-stderr.log')}\n"
            "stdout_logfile_maxbytes=10MB\nstderr_logfile_maxbytes=10MB\nstdout_logfile_backups=3\nstderr_logfile_backups=3\n"
        )
        receipt_path.write_text(claim)
        run(["uv", "run", "--no-sync", "app-manifest", "validate-manifest", str(dest / "app.toml")], root, env)
        ctl = ["supervisorctl"] + (["-c", str(args.supervisor_config.resolve())] if args.supervisor_config else [])
        run(ctl + ["reread"], root, env)
        run(ctl + ["update", NAME], root, env)
        # update may have started it; restart also loads changed editable code on repeats.
        run(ctl + ["restart", NAME], root, env)
        status = run(ctl + ["status", NAME], root, env, capture=True)
        if "RUNNING" not in status:
            raise InstallError("The notification-tester supervisor program is not running.")
        deadline = time.monotonic() + 30
        healthy = False
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                    healthy = response.status == 200 and json.load(response).get("status") == "ok"
                if healthy:
                    break
            except (OSError, ValueError, urllib.error.URLError):
                pass
            threading.Event().wait(0.2)
        if not healthy:
            raise InstallError(f"Service did not become healthy; inspect {log_dir}.")
        if args.enable_chat_forwarding:
            integration(root)
        sys.stdout.write(f"Notification tester is healthy at http://localhost:{port}/ (device page), /send (sender).\n")
        if args.open:
            run(["uv", "run", "--no-sync", "workspace-layout", "open", NAME], root, env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--studio-root", type=Path, default=Path(os.environ.get("STUDIO_ROOT", "/home/user/workspace")))
    parser.add_argument("--port", type=int, help="Free port; otherwise keep existing choice or choose 8080–8999")
    parser.add_argument("--adopt-existing", action="store_true", help="Explicitly adopt a verified older installation of this same app")
    parser.add_argument("--enable-chat-forwarding", action="store_true", help="Best-effort additive Studio notifier hook")
    parser.add_argument("--open", action="store_true", help="Show the app after health validation")
    parser.add_argument("--supervisor-config", type=Path, help="Explicit supervisorctl config, useful for isolated installations")
    parser.add_argument("--log-dir", type=Path, help="Override supervisor log directory")
    args = parser.parse_args()
    try:
        install(args)
    except (InstallError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        sys.stderr.write(f"Installation stopped: {exc}\nExisting app data and signing keys were not replaced.\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
