"""End-to-end tests of the portable installer (install.sh -> scripts/install.py) against a fake Studio root.

Every run is isolated: a fake Studio root under the test's temporary directory (with copies of the real
``forward_port.py`` and ``tool_env.py``, so registration really happens), a disposable tool home
(``TOOL_ENV_HOME``), a private supervisord with its own socket whose only programs are the fake root's, and a
fake ``uv`` on PATH. The fake ``uv`` installs a shim that runs the app code the installer copied into the
fake root, so the supervised service is the copy. Nothing touches the live workspace, its supervisord, its
tool directory or its notifier, and no notification can reach a real device or the platform.
"""

import base64
import hashlib
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

APP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_DIR.parents[2]
STOCK_NOTIFIER = Path(__file__).parent / "fixtures" / "stock_notify_user.py.txt"
APP_MANIFEST_BIN = Path(sys.executable).parent / "app-manifest"

NAME = "notification-tester"
OWNER_MARKER = "; Managed by notification-tester-portable-v1"
HOOK_MARKER = "# notification-tester portable copy"
STOCK_CALL_SITE = "    is_accepted = notify(\n        args.message, args.title, http=HttpClient(), environ=dict(os.environ)\n    )\n"
SOURCE_KEY_TEXT = "SOURCE TREE KEY: must never be copied into an installation\n"

# The fake uv: logs every call, installs a shim for `tool install`, hands `run --no-sync app-manifest` to the
# real app-manifest, and does nothing for anything else (sync, workspace-layout). Its settings live in a JSON
# file next to it. Kept free of indented imports and stream writes so the app's ratchets read it as data.
FAKE_UV_SOURCE = """
import json
import os
import sys
from pathlib import Path

config = json.loads((Path(__file__).parent / "fake_uv.json").read_text())
args = sys.argv[1:]
with open(config["log"], "a") as log:
    log.write(json.dumps(args) + "\\n")
if args[:2] == ["tool", "install"]:
    if config["fail_tool_install"]:
        sys.exit(3)
    dest = Path(args[args.index("--editable") + 1])
    shim = Path(os.environ["UV_TOOL_BIN_DIR"]) / "notification-tester"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text(config["shim"].replace("@SRC@", repr(str(dest / "src"))))
    shim.chmod(0o755)
elif args[:3] == ["run", "--no-sync", "app-manifest"]:
    os.execv(config["app_manifest"], [config["app_manifest"], *args[3:]])
"""

# The tool executable the fake uv installs: the installed copy's backend, on this test venv's interpreter.
SHIM_TEMPLATE = (
    f"#!{sys.executable}\n"
    "import sys\n"
    "sys.path.insert(0, @SRC@)\n"
    "from notification_tester.runner import main\n"
    "main()\n"
)

# Variables that could point a child at the live workspace, its gateway, or a parent agent's state.
_STRIPPED_PREFIXES = (
    "LATCHKEY_",
    "MINDS_",
    "MNGR_",
    "NOTIFICATION_TESTER_",
    "UV_TOOL_",
)
_STRIPPED_NAMES = ("TOOL_ENV_HOME", "VIRTUAL_ENV", "STUDIO_ROOT")


def clean_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_STRIPPED_PREFIXES) and key not in _STRIPPED_NAMES
    }


def wait_for(predicate: Callable[[], Any], timeout: float, what: str) -> Any:
    """Poll ``predicate`` until it answers something truthy, and return that; fail after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    pause = threading.Event()
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        pause.wait(0.1)
    pytest.fail(f"Timed out after {timeout} s waiting for {what}")


def unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def is_free(port: int) -> bool:
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def http_json(port: int, path: str, method: str = "GET", body: object = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.load(response)


def http_text(port: int, path: str) -> str:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}{path}", timeout=5
    ) as response:
        return response.read().decode()


def static_version(static_dir: Path) -> str:
    """The backend's app version for a static directory (runner.compute_app_version, recomputed independently)."""
    digest = hashlib.sha256()
    for path in sorted(static_dir.iterdir()):
        if path.suffix in (".js", ".html", ".css"):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:8]


def tree_snapshot(directory: Path) -> dict[str, bytes]:
    return {
        p.relative_to(directory).as_posix(): p.read_bytes()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def browser_keys() -> dict[str, str]:
    """Encryption keys shaped like a browser's push subscription (a real P-256 point and auth secret)."""
    public = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    )

    def encode(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return {"p256dh": encode(public), "auth": encode(secrets.token_bytes(16))}


class Harness:
    """One fake Studio root plus everything an isolated installer run needs around it."""

    def __init__(self, base: Path, source: Path) -> None:
        self.base = base
        self.source = source
        self.root = base / "studio"
        self.tool_home = base / "tool-home"
        self.log_dir = base / "logs"
        self.fake_bin = base / "fake-bin"
        self.uv_log = base / "uv-calls.jsonl"
        self.supervisor_config = base / "supervisor" / "supervisord.conf"
        self.dest = self.root / "system/apps/notification_tester"
        self.state = self.root / "data/.state/notification-tester-install"
        self.config = self.root / "system/supervisord.conf.d/notification-tester.conf"
        self.registry = self.root / "data/.state/apps.toml"
        self.data_dir = self.root / "data/.apps/notification-tester"
        self.notifier = self.root / ".agents/skills/notify-user/scripts/notify_user.py"
        self._build_root()
        self._build_fake_bin()

    def _build_root(self) -> None:
        root = self.root
        (root / "system/scripts").mkdir(parents=True)
        for script in ("forward_port.py", "tool_env.py"):
            shutil.copy2(
                REPO_ROOT / "system/scripts" / script, root / "system/scripts" / script
            )
        for lib, package in (
            ("system/libs/app_schema", "app-schema"),
            ("system/libs/app_manifest", "app-manifest"),
            ("libs/imbue_common", "imbue-common"),
        ):
            (root / lib).mkdir(parents=True)
            (root / lib / "pyproject.toml").write_text(
                f'[project]\nname = "{package}"\nversion = "0.0.0"\n'
            )
        (root / "system/supervisord.conf.d").mkdir(parents=True)
        (root / "data/.state").mkdir(parents=True)
        (root / "pyproject.toml").write_text(
            '[project]\nname = "fake-studio"\nversion = "0.0.0"\n\n'
            '[tool.uv.workspace]\nmembers = ["system/apps/*"]\n\n'
            '[tool.uv.sources]\nimbue-common = { path = "libs/imbue_common" }\n'
        )
        # The stock platform notifier and its test, which the app's manifest references.
        self.notifier.parent.mkdir(parents=True)
        self.notifier.write_text(STOCK_NOTIFIER.read_text())
        (self.notifier.parent / "notify_user_test.py").write_text(
            '"""Stand-in for the platform notifier test."""\n'
        )

    def _build_fake_bin(self) -> None:
        self.fake_bin.mkdir()
        uv = self.fake_bin / "uv"
        uv.write_text(f"#!{sys.executable}\n{FAKE_UV_SOURCE}")
        uv.chmod(0o755)
        self.set_fake_uv(fail_tool_install=False)
        # The installer runs on this venv's interpreter, whatever python3 the surrounding PATH offers.
        python3 = self.fake_bin / "python3"
        python3.write_text(f'#!/bin/bash\nexec {sys.executable} "$@"\n')
        python3.chmod(0o755)

    def set_fake_uv(self, *, fail_tool_install: bool) -> None:
        (self.fake_bin / "fake_uv.json").write_text(
            json.dumps(
                {
                    "log": str(self.uv_log),
                    "app_manifest": str(APP_MANIFEST_BIN),
                    "shim": SHIM_TEMPLATE,
                    "fail_tool_install": fail_tool_install,
                }
            )
        )

    @property
    def env(self) -> dict[str, str]:
        env = clean_env()
        env["PATH"] = f"{self.fake_bin}{os.pathsep}{env.get('PATH', '')}"
        env["TOOL_ENV_HOME"] = str(self.tool_home)
        return env

    def install(self, *args: str) -> subprocess.CompletedProcess[str]:
        command = [
            "bash",
            str(self.source / "install.sh"),
            "--studio-root",
            str(self.root),
            "--supervisor-config",
            str(self.supervisor_config),
            "--log-dir",
            str(self.log_dir),
            *args,
        ]
        return subprocess.run(
            command,
            env=self.env,
            cwd=self.base,
            capture_output=True,
            text=True,
            timeout=100,
        )

    def uv_calls(self) -> list[list[str]]:
        if not self.uv_log.exists():
            return []
        return [json.loads(line) for line in self.uv_log.read_text().splitlines()]

    def receipt(self) -> dict[str, object]:
        return json.loads((self.state / "receipt.json").read_text())

    def registry_rows(self) -> list[dict[str, Any]]:
        return tomllib.loads(self.registry.read_text()).get("apps", [])


class Supervisor:
    """A private supervisord whose programs are only the fake root's service configurations."""

    def __init__(self, harness: Harness) -> None:
        self.config = harness.supervisor_config
        self.config.parent.mkdir(parents=True)
        # Unix socket paths are limited to about 100 characters, so the socket gets a short directory of its own.
        self.socket_dir = Path(tempfile.mkdtemp(prefix="ntsv-", dir="/tmp"))
        socket_path = self.socket_dir / "s.sock"
        self.config.write_text(
            f"[unix_http_server]\nfile={socket_path}\n\n"
            f"[supervisord]\nlogfile={self.config.parent / 'supervisord.log'}\n"
            f"pidfile={self.config.parent / 'supervisord.pid'}\nchildlogdir={self.config.parent}\nnodaemon=true\n\n"
            "[rpcinterface:supervisor]\nsupervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface\n\n"
            f"[supervisorctl]\nserverurl=unix://{socket_path}\n\n"
            f"[include]\nfiles = {harness.root / 'system/supervisord.conf.d'}/*.conf\n"
        )
        self.process = subprocess.Popen(
            ["supervisord", "-c", str(self.config), "-n"],
            env=clean_env(),
            cwd=self.config.parent,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wait_for(
            lambda: self.ctl("pid").returncode == 0,
            20,
            "the private supervisord to answer",
        )

    def ctl(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["supervisorctl", "-c", str(self.config), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )

    def program_pid(self) -> int:
        return int(self.ctl("pid", NAME).stdout.strip())

    def shutdown(self) -> None:
        try:
            self.ctl("shutdown")
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        finally:
            shutil.rmtree(self.socket_dir, ignore_errors=True)


@pytest.fixture(scope="module")
def app_source(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A copy of this app's directory as an installer source, plus state the installer must never copy."""
    source = tmp_path_factory.mktemp("source") / "notification-tester"
    shutil.copytree(
        APP_DIR,
        source,
        ignore=shutil.ignore_patterns(".venv", ".pytest_cache", ".ty_cache", "data"),
    )
    (source / "data").mkdir()
    (source / "data/vapid_private_key.pem").write_text(SOURCE_KEY_TEXT)
    (source / "data/devices.json").write_text('[{"device_id": "source-device"}]')
    (source / ".venv/bin").mkdir(parents=True)
    (source / ".venv/bin/python").write_text("not a real interpreter\n")
    (source / "src/notification_tester/__pycache__").mkdir(exist_ok=True)
    (source / "src/notification_tester/__pycache__/marker.cpython-312.pyc").write_bytes(
        b"stale bytecode"
    )
    return source


@pytest.fixture
def harness(tmp_path: Path, app_source: Path) -> Harness:
    return Harness(tmp_path, app_source)


@pytest.fixture
def supervisor(harness: Harness) -> Iterator[Supervisor]:
    running = Supervisor(harness)
    try:
        yield running
    finally:
        running.shutdown()


def assert_refused(result: subprocess.CompletedProcess[str], reason: str) -> None:
    assert result.returncode == 1, result.stdout + result.stderr
    assert reason in result.stderr
    assert "Existing app data and signing keys were not replaced." in result.stderr
    assert "is healthy" not in result.stdout


def assert_nothing_installed(harness: Harness) -> None:
    assert not harness.dest.exists()
    assert not harness.config.exists()
    assert not (harness.state / "receipt.json").exists()
    assert harness.uv_calls() == []


@pytest.mark.timeout(120)
def test_fresh_install_copies_code_only_registers_and_serves(
    harness: Harness, supervisor: Supervisor
) -> None:
    port = unused_port()
    result = harness.install("--port", str(port))
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        f"Notification tester is healthy at http://localhost:{port}/" in result.stdout
    )

    config = harness.config.read_text()
    assert config.startswith(f"{OWNER_MARKER}\n[program:{NAME}]\n")
    assert f'NOTIFICATION_TESTER_PORT="{port}"' in config
    assert f'NOTIFICATION_TESTER_DATA_DIR="{harness.data_dir}"' in config
    assert f'MINDS_APPS_FILE="{harness.registry}"' in config
    assert f"directory={harness.root}\n" in config
    assert str(harness.root / "system/scripts/forward_port.py") in config
    assert str(harness.tool_home / ".local/bin" / NAME) in config
    assert (
        f"stderr_logfile={harness.log_dir / 'notification-tester-stderr.log'}\n"
        in config
    )
    assert harness.receipt() == {
        "owner": "notification-tester-portable-v1",
        "root": str(harness.root),
        "port": port,
    }

    # Only app code is copied: the source tree's data, environment and bytecode stay behind.
    for relative in (
        "app.toml",
        "install.sh",
        "src/notification_tester/runner.py",
        "docs/installation.md",
    ):
        assert (harness.dest / relative).read_bytes() == (
            harness.source / relative
        ).read_bytes()
    copied = [p.relative_to(harness.dest) for p in harness.dest.rglob("*")]
    assert not [p for p in copied if {"data", ".venv"} & set(p.parts)]
    # The running service writes its own bytecode; the source tree's bytecode is never copied.
    assert not [p for p in copied if p.name == "marker.cpython-312.pyc"]

    # The tool install and sync went through uv with the target's overrides and the disposable tool home.
    overrides = harness.state / "tool-overrides.txt"
    assert harness.uv_calls()[:2] == [
        [
            "tool",
            "install",
            "--editable",
            str(harness.dest),
            "--reinstall",
            "--overrides",
            str(overrides),
        ],
        ["sync", "--all-packages"],
    ]
    assert overrides.read_text().splitlines() == [
        f"app-schema @ {(harness.root / 'system/libs/app_schema').as_uri()}",
        f"app-manifest @ {(harness.root / 'system/libs/app_manifest').as_uri()}",
        f"imbue-common @ {(harness.root / 'libs/imbue_common').as_uri()}",
    ]
    assert (harness.tool_home / ".local/bin" / NAME).is_file()

    # The real forward_port.py registered the app from its manifest.
    rows = [r for r in harness.registry_rows() if r["name"] == NAME]
    assert len(rows) == 1
    assert rows[0]["url"] == f"http://localhost:{port}"
    assert rows[0]["program"] == NAME

    assert "RUNNING" in supervisor.ctl("status", NAME).stdout
    assert http_json(port, "/health") == {"status": "ok"}
    public_key = http_json(port, "/api/vapid-public-key")["public_key"]
    assert len(public_key) > 40
    key_file = harness.data_dir / "vapid_private_key.pem"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert "BEGIN" in key_file.read_text()
    assert SOURCE_KEY_TEXT not in key_file.read_text()


@pytest.mark.timeout(150)
def test_repeat_install_keeps_port_data_key_and_stale_files(
    harness: Harness, supervisor: Supervisor
) -> None:
    port = unused_port()
    first = harness.install("--port", str(port))
    assert first.returncode == 0, first.stdout + first.stderr
    public_key = http_json(port, "/api/vapid-public-key")["public_key"]

    devices = [
        {
            "device_id": "seeded-device",
            "name": "Seeded phone",
            "user_agent": "test",
            "subscription": {
                "endpoint": "http://127.0.0.1:9/push/x",
                "keys": browser_keys(),
            },
            "push_service": "127.0.0.1",
            "signed_up_at": "2026-01-01T00:00:00.000+00:00",
            "status": "active",
            "status_detail": "",
        }
    ]
    (harness.data_dir / "devices.json").write_text(json.dumps(devices, indent=2))
    (harness.data_dir / "chat_forwarding.json").write_text(
        json.dumps({"is_enabled": False, "via": "live"})
    )
    before = {
        name: (harness.data_dir / name).read_bytes()
        for name in ("devices.json", "chat_forwarding.json")
    }
    key_before = (harness.data_dir / "vapid_private_key.pem").read_bytes()
    stale = harness.dest / "src/notification_tester/static/zz-stale-marker.js"
    stale.write_text("// left behind by an older version\n")
    pid_before = supervisor.program_pid()

    second = harness.install()
    assert second.returncode == 0, second.stdout + second.stderr
    assert f"healthy at http://localhost:{port}/" in second.stdout
    assert harness.receipt()["port"] == port
    assert f'NOTIFICATION_TESTER_PORT="{port}"' in harness.config.read_text()
    assert supervisor.program_pid() != pid_before

    for name, content in before.items():
        assert (harness.data_dir / name).read_bytes() == content
    assert (harness.data_dir / "vapid_private_key.pem").read_bytes() == key_before
    assert http_json(port, "/api/vapid-public-key")["public_key"] == public_key
    # The restarted service reads the preserved data directory.
    assert [d["device_id"] for d in http_json(port, "/api/devices")["devices"]] == [
        "seeded-device"
    ]
    assert http_json(port, "/api/chat-forwarding") == {
        "is_enabled": False,
        "via": "live",
    }

    # Stale files are left alone, and the service runs the installed copy: it serves the stale file and its
    # version fingerprint covers the copy's static files, which differ from the source's by that file.
    assert stale.is_file()
    assert (
        http_text(port, "/static/zz-stale-marker.js")
        == "// left behind by an older version\n"
    )
    copy_version = static_version(harness.dest / "src/notification_tester/static")
    assert copy_version != static_version(
        harness.source / "src/notification_tester/static"
    )
    assert http_json(port, "/api/version") == {"app_version": copy_version}


@pytest.mark.timeout(120)
def test_automatic_port_skips_registered_and_busy_ports(
    harness: Harness, supervisor: Supervisor
) -> None:
    candidates = [p for p in range(8080, 9000) if is_free(p)]
    registered, busy, expected = candidates[:3]
    harness.registry.write_text(
        f'[[apps]]\nname = "other-app"\nurl = "http://localhost:{registered}"\n'
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", busy))
        listener.listen()
        result = harness.install()
    assert result.returncode == 0, result.stdout + result.stderr
    assert harness.receipt()["port"] == expected
    assert 8080 <= expected <= 8999
    assert f"healthy at http://localhost:{expected}/" in result.stdout
    assert http_json(expected, "/health") == {"status": "ok"}
    urls = {r["name"]: r["url"] for r in harness.registry_rows()}
    assert urls == {
        "other-app": f"http://localhost:{registered}",
        NAME: f"http://localhost:{expected}",
    }


@pytest.mark.timeout(60)
def test_occupied_port_is_refused_before_any_change(harness: Harness) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        result = harness.install("--port", str(port))
    assert_refused(result, "Requested port is already in use")
    assert_nothing_installed(harness)


@pytest.mark.timeout(60)
def test_foreign_service_config_declaring_the_program_is_refused(
    harness: Harness,
) -> None:
    foreign = harness.root / "system/supervisord.conf.d/legacy.conf"
    foreign.write_text("[program:notification-tester]\ncommand=/bin/true\n")
    result = harness.install("--port", str(unused_port()))
    assert_refused(
        result, "Another service configuration already declares notification-tester"
    )
    assert foreign.read_text() == "[program:notification-tester]\ncommand=/bin/true\n"
    assert_nothing_installed(harness)
    assert not harness.state.exists()


@pytest.mark.timeout(60)
def test_registry_row_owned_by_another_program_is_refused(harness: Harness) -> None:
    registry = '[[apps]]\nname = "notification-tester"\nurl = "http://localhost:8123"\nprogram = "something-else"\n'
    harness.registry.write_text(registry)
    result = harness.install("--port", str(unused_port()))
    assert_refused(result, "An unrelated registered app owns notification-tester")
    assert harness.registry.read_text() == registry
    assert_nothing_installed(harness)


@pytest.mark.timeout(60)
def test_unmanaged_existing_app_dir_is_refused_with_and_without_adoption(
    harness: Harness,
) -> None:
    harness.dest.mkdir(parents=True)
    (harness.dest / "app.toml").write_text('name = "someone-else"\n')
    (harness.dest / "pyproject.toml").write_text('[project]\nname = "someone-else"\n')
    (harness.dest / "keep.txt").write_text("theirs\n")
    before = tree_snapshot(harness.dest)

    plain = harness.install("--port", str(unused_port()))
    assert_refused(plain, "Unmanaged installation exists")
    adopted = harness.install("--port", str(unused_port()), "--adopt-existing")
    assert_refused(adopted, "Existing app ownership conflicts")

    assert tree_snapshot(harness.dest) == before
    assert not harness.config.exists()
    assert not (harness.state / "receipt.json").exists()
    assert harness.uv_calls() == []


@pytest.mark.timeout(120)
def test_adopting_an_older_copy_of_this_app(
    harness: Harness, supervisor: Supervisor
) -> None:
    shutil.copytree(
        harness.source,
        harness.dest,
        ignore=shutil.ignore_patterns("data", ".venv", "__pycache__"),
    )
    old_config = "[program:notification-tester]\ncommand=/bin/bash -c 'exec notification_tester'\n"
    harness.config.write_text(old_config)
    port = unused_port()

    refused = harness.install("--port", str(port))
    assert_refused(refused, "Unmanaged installation exists")
    assert harness.config.read_text() == old_config

    adopted = harness.install("--port", str(port), "--adopt-existing")
    assert adopted.returncode == 0, adopted.stdout + adopted.stderr
    assert harness.config.read_text().startswith(OWNER_MARKER)
    assert harness.receipt()["port"] == port
    assert http_json(port, "/health") == {"status": "ok"}


@pytest.mark.timeout(120)
def test_replaced_managed_config_is_refused(
    harness: Harness, supervisor: Supervisor
) -> None:
    port = unused_port()
    first = harness.install("--port", str(port))
    assert first.returncode == 0, first.stdout + first.stderr
    generated = harness.config.read_text()
    replaced = generated.split("\n", 1)[1]
    assert OWNER_MARKER not in replaced
    harness.config.write_text(replaced)

    repeat = harness.install()
    assert_refused(repeat, "Managed service configuration has been replaced")
    assert harness.config.read_text() == replaced


@pytest.mark.timeout(150)
def test_failed_install_can_be_repeated_after_fixing_the_cause(
    harness: Harness, supervisor: Supervisor
) -> None:
    port = unused_port()
    harness.set_fake_uv(fail_tool_install=True)
    failed = harness.install("--port", str(port))
    assert failed.returncode == 1
    assert "Existing app data and signing keys were not replaced." in failed.stderr
    assert (harness.dest / "app.toml").is_file()
    assert not harness.config.exists()

    harness.set_fake_uv(fail_tool_install=False)
    repeated = harness.install()
    assert repeated.returncode == 0, repeated.stdout + repeated.stderr
    assert f"healthy at http://localhost:{port}/" in repeated.stdout
    assert http_json(port, "/health") == {"status": "ok"}


@pytest.mark.timeout(60)
def test_target_without_platform_files_is_not_a_studio_root(harness: Harness) -> None:
    (harness.root / "system/scripts/tool_env.py").unlink()
    result = harness.install("--port", str(unused_port()))
    assert_refused(result, "Target is not a supported Studio root")
    assert_nothing_installed(harness)
    assert not harness.state.exists()


@pytest.mark.timeout(180)
def test_chat_forwarding_hooks_the_stock_notifier_once_and_copies_chat_notifications(
    harness: Harness, supervisor: Supervisor
) -> None:
    original = harness.notifier.read_text()
    assert original.count(STOCK_CALL_SITE) == 1
    port = unused_port()
    first = harness.install("--port", str(port), "--enable-chat-forwarding")
    assert first.returncode == 0, first.stdout + first.stderr

    hooked = harness.notifier.read_text()
    assert hooked.count(HOOK_MARKER) == 1
    # One block is inserted right after the platform send, and nothing else changes.
    end = original.index(STOCK_CALL_SITE) + len(STOCK_CALL_SITE)
    block = hooked[end : end + len(hooked) - len(original)]
    assert hooked == original[:end] + block + original[end:]
    assert block.startswith(f"    {HOOK_MARKER}")
    assert "integration/forward_chat_notification.py" in block
    compile(hooked, str(harness.notifier), "exec")
    assert (harness.state / "notifier-before-forwarding.py").read_text() == original

    again = harness.install("--enable-chat-forwarding")
    assert again.returncode == 0, again.stdout + again.stderr
    assert "Existing chat forwarding kept." in again.stdout
    assert harness.notifier.read_text() == hooked
    assert (harness.state / "notifier-before-forwarding.py").read_text() == original

    # A device whose push service is a closed local port: the copy is recorded and fails harmlessly.
    closed_port = unused_port()
    endpoint = f"http://127.0.0.1:{closed_port}/push/x"
    http_json(
        port,
        "/api/devices",
        "POST",
        {
            "device_id": "test-device",
            "name": "Test device",
            "user_agent": "test",
            "subscription": {"endpoint": endpoint, "keys": browser_keys()},
        },
    )
    env = clean_env()
    env.update(
        LATCHKEY_GATEWAY="http://127.0.0.1:1",
        LATCHKEY_GATEWAY_PASSWORD="not-a-real-password",
        MNGR_AGENT_ID="test-agent",
        MINDS_CHAT_ID="test-chat",
        MINDS_APPS_FILE=str(harness.registry),
    )
    notified = subprocess.run(
        [sys.executable, str(harness.notifier), "Done."],
        env=env,
        cwd=harness.root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # The platform's own result stays authoritative: its send failed, so the notifier still reports failure.
    assert notified.returncode == 1
    assert "the notification did not go out" in notified.stderr
    assert "notification-tester:" not in notified.stderr

    def finished_chat_sends() -> list[dict[str, Any]]:
        sends = [
            s for s in http_json(port, "/api/sends")["sends"] if s["source"] == "chat"
        ]
        return (
            sends
            if sends and all(s["status"] not in ("waiting", "sending") for s in sends)
            else []
        )

    sends = wait_for(
        finished_chat_sends, 30, "the forwarded chat notification to finish"
    )
    assert len(sends) == 1
    send = sends[0]
    assert (
        send["device_id"],
        send["via"],
        send["body"],
        send["title"],
        send["chat_id"],
    ) == (
        "test-device",
        "push",
        "Done.",
        "Imbue Studio",
        "test-chat",
    )
    assert send["status"] == "failed"
    assert send["raw_response"]["url"] == endpoint


@pytest.mark.timeout(120)
def test_chat_forwarding_leaves_an_unknown_notifier_unchanged(
    harness: Harness, supervisor: Supervisor
) -> None:
    unknown = harness.notifier.read_text().replace(
        STOCK_CALL_SITE,
        "    is_accepted = notify(args.message, args.title, http=HttpClient(), environ=dict(os.environ))\n",
    )
    harness.notifier.write_text(unknown)
    port = unused_port()
    result = harness.install("--port", str(port), "--enable-chat-forwarding")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "Chat forwarding skipped: notifier version has a different call site."
        in result.stderr
    )
    assert f"healthy at http://localhost:{port}/" in result.stdout
    assert harness.notifier.read_text() == unknown
    assert not (harness.state / "notifier-before-forwarding.py").exists()
