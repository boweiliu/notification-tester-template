#!/usr/bin/env python3
"""Export only tracked app files into a new, empty standalone tree (no history/state)."""

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    root = Path(subprocess.check_output(["git", "-C", str(source), "rev-parse", "--show-toplevel"], text=True).strip())
    prefix = source.relative_to(root)
    commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain", "--", str(prefix)], text=True)
    if dirty:
        parser.error("Save all app changes before exporting; only a clean app can be exported.")
    target = args.destination.resolve()
    if target.exists() and any(target.iterdir()):
        parser.error("Destination must be empty; exports never overwrite an existing tree.")
    target.mkdir(parents=True, exist_ok=True)
    paths = subprocess.check_output(["git", "-C", str(root), "ls-files", "-z", "--", str(prefix)], text=True).split("\0")
    hashes = {}
    for path in sorted(filter(None, paths)):
        relative = Path(path).relative_to(prefix)
        if any(p in {".git", "__pycache__", ".pytest_cache", ".venv", "data"} for p in relative.parts):
            continue
        original = root / path
        if original.is_symlink():
            parser.error(f"Unexpected symlink: {relative}")
        dest = target / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, dest)
        dest.chmod(0o755 if original.stat().st_mode & 0o111 else 0o644)
        hashes[relative.as_posix()] = hashlib.sha256(original.read_bytes()).hexdigest()
    (target / "SOURCE.md").write_text(
        "# Source provenance\n\n"
        f"Exported mechanically from `{prefix.as_posix()}` at source revision `{commit}`.\n\n"
        "The source revision identifies provenance only; no source history is included. "
        "The app directory is authoritative. Regenerate this tree with its scripts/export.py; "
        "do not maintain a second hand-edited copy. Runtime state, signing keys, subscriptions, "
        "logs and chat records are excluded. Studio platform dependencies come from the target "
        "workspace, as described in docs/installation.md.\n"
    )
    (target / "EXPORT-MANIFEST.json").write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n")
    (target / ".gitignore").write_text("__pycache__/\n*.pyc\n.venv/\n.pytest_cache/\n.ty_cache/\ndata/\n*.egg-info/\n")


if __name__ == "__main__":
    main()
