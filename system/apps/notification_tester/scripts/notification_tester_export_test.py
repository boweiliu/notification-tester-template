"""Tests of scripts/export.py, run against a scratch git repository holding a copy of this app's tracked files.

The export reads git state (HEAD, cleanliness, tracked files), so each test commits a copy of the app into its
own repository under the test's temporary directory and runs that copy's export script. This worktree is never
modified.
"""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_DIR.parents[2]
PREFIX = "system/apps/notification_tester"
# Made fresh each run, so the only copies are the planted ones (this file is itself exported).
SECRET_TEXT = f"private key material {uuid.uuid4().hex}"


def git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *args,
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


class ScratchRepo:
    """A git repository whose only commit is a copy of this app's tracked files, at the app's real path."""

    def __init__(self, base: Path) -> None:
        self.root = base / "repo"
        self.app = self.root / PREFIX
        self.root.mkdir()
        git(self.root, "init", "-q")
        git(self.root, "config", "user.name", "Export Test")
        git(self.root, "config", "user.email", "export-test@example.com")
        tracked = git(REPO_ROOT, "ls-files", "-z", "--", PREFIX).split("\0")
        for path in filter(None, tracked):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / path, target)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "Copy of the app")
        self.commit = git(self.root, "rev-parse", "HEAD").strip()
        # Ignored local state and bytecode the export must leave behind.
        (self.app / "data").mkdir()
        (self.app / "data/vapid_private_key.pem").write_text(SECRET_TEXT)
        (self.app / "data/devices.json").write_text(
            json.dumps([{"device_id": "d", "secret": SECRET_TEXT}])
        )
        (self.app / "src/notification_tester/__pycache__").mkdir(exist_ok=True)
        (
            self.app / "src/notification_tester/__pycache__/runner.cpython-312.pyc"
        ).write_bytes(SECRET_TEXT.encode())
        assert git(self.root, "status", "--porcelain") == ""

    def tracked_modes(self) -> dict[str, int]:
        """Each tracked app file, relative to the app, with the permission bits git records for it."""
        modes = {}
        for line in git(self.root, "ls-files", "-s", "--", PREFIX).splitlines():
            meta, path = line.split("\t", 1)
            modes[Path(path).relative_to(PREFIX).as_posix()] = (
                0o755 if meta.split()[0] == "100755" else 0o644
            )
        return modes

    def export(self, destination: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.app / "scripts/export.py"), str(destination)],
            capture_output=True,
            text=True,
            timeout=60,
        )


def exported_tree(destination: Path) -> dict[str, tuple[int, bytes]]:
    return {
        p.relative_to(destination).as_posix(): (
            stat.S_IMODE(p.stat().st_mode),
            p.read_bytes(),
        )
        for p in sorted(destination.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def repo(tmp_path: Path) -> ScratchRepo:
    return ScratchRepo(tmp_path)


@pytest.mark.timeout(60)
def test_clean_export_holds_exactly_the_tracked_files_with_provenance(
    repo: ScratchRepo, tmp_path: Path
) -> None:
    destination = tmp_path / "export"
    result = repo.export(destination)
    assert result.returncode == 0, result.stderr

    tracked = repo.tracked_modes()
    tree = exported_tree(destination)
    assert set(tree) == set(tracked) | {"SOURCE.md", "EXPORT-MANIFEST.json"}
    for relative, mode in tracked.items():
        assert tree[relative] == (mode, (repo.app / relative).read_bytes()), relative
    assert tree["install.sh"][0] == 0o755
    assert tree["README.md"][0] == 0o644

    manifest = json.loads((destination / "EXPORT-MANIFEST.json").read_text())
    assert set(manifest) == set(tracked)
    for relative, digest in manifest.items():
        assert (
            hashlib.sha256((destination / relative).read_bytes()).hexdigest() == digest
        ), relative

    source_note = (destination / "SOURCE.md").read_text()
    assert f"`{repo.commit}`" in source_note
    assert f"`{PREFIX}`" in source_note

    # Ignored data, keys and bytecode stay behind.
    assert not [
        path for path in tree if {"data", "__pycache__"} & set(Path(path).parts)
    ]
    assert not [
        path for path, (_, content) in tree.items() if SECRET_TEXT.encode() in content
    ]


@pytest.mark.timeout(60)
def test_exports_of_one_revision_are_identical(
    repo: ScratchRepo, tmp_path: Path
) -> None:
    first = repo.export(tmp_path / "first")
    second = repo.export(tmp_path / "second")
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert exported_tree(tmp_path / "first") == exported_tree(tmp_path / "second")


@pytest.mark.timeout(60)
def test_uncommitted_change_to_a_tracked_file_blocks_the_export(
    repo: ScratchRepo, tmp_path: Path
) -> None:
    (repo.app / "README.md").write_text("edited but not saved\n")
    destination = tmp_path / "export"
    result = repo.export(destination)
    assert result.returncode != 0
    assert "Save all app changes before exporting" in result.stderr
    assert not destination.exists()


@pytest.mark.timeout(60)
def test_untracked_app_file_blocks_the_export(
    repo: ScratchRepo, tmp_path: Path
) -> None:
    (repo.app / "notes.txt").write_text("new and not ignored\n")
    destination = tmp_path / "export"
    result = repo.export(destination)
    assert result.returncode != 0
    assert "Save all app changes before exporting" in result.stderr
    assert not destination.exists()


@pytest.mark.timeout(60)
def test_non_empty_destination_is_refused_and_left_alone(
    repo: ScratchRepo, tmp_path: Path
) -> None:
    destination = tmp_path / "export"
    destination.mkdir()
    (destination / "keep.txt").write_text("already here\n")
    result = repo.export(destination)
    assert result.returncode != 0
    assert "Destination must be empty" in result.stderr
    assert exported_tree(destination) == {
        "keep.txt": (
            stat.S_IMODE((destination / "keep.txt").stat().st_mode),
            b"already here\n",
        )
    }


@pytest.mark.timeout(60)
def test_empty_existing_destination_is_accepted(
    repo: ScratchRepo, tmp_path: Path
) -> None:
    destination = tmp_path / "export"
    destination.mkdir()
    result = repo.export(destination)
    assert result.returncode == 0, result.stderr
    assert set(exported_tree(destination)) == set(repo.tracked_modes()) | {
        "SOURCE.md",
        "EXPORT-MANIFEST.json",
    }
