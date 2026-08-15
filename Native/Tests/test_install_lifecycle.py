#!/usr/bin/env python3
"""Hermetic checks for installed-script path persistence and safe removal."""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile


NATIVE = Path(__file__).resolve().parents[1]


def write_executable(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def run(command, environment, expected=0):
    completed = subprocess.run(
        [str(item) for item in command],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
        check=False,
    )
    if completed.returncode != expected:
        raise AssertionError(
            f"{command!r} returned {completed.returncode}, expected {expected}\n"
            f"{completed.stdout}"
        )
    return completed.stdout


def main():
    with tempfile.TemporaryDirectory(prefix="chess-listener-lifecycle-") as raw:
        root = Path(raw)
        runtime = root / "custom-prefix" / "chess-listener"
        manifest_dir = root / "custom-firefox-manifests"
        home = root / "home"
        fake_bin = root / "bin"
        fake_python = root / "fake-python"
        source = root / "source"
        unmarked = root / "unmarked" / "chess-listener"

        for directory in (
            runtime,
            manifest_dir,
            home,
            fake_bin,
            fake_python / "PyQt6",
            source,
            unmarked,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        for name in ("install.sh", "update.sh", "uninstall.sh"):
            shutil.copy2(NATIVE / name, runtime / name)

        (runtime / ".chess-listener-install").write_text(
            "ChessListener user installation\n", encoding="utf-8"
        )
        (runtime / ".install-source").write_text(
            f"{source}\n", encoding="utf-8"
        )
        (runtime / ".manifest-dir").write_text(
            f"{manifest_dir}\n", encoding="utf-8"
        )
        shutil.copy2("/bin/true", runtime / "chess-listener-host")
        (runtime / "overlay.py").write_text("# lifecycle fixture\n", encoding="utf-8")
        (runtime / "san.py").write_text("# lifecycle fixture\n", encoding="utf-8")

        manifest_path = manifest_dir / "local.chess_listener.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "name": "local.chess_listener",
                    "description": "lifecycle fixture",
                    "path": str(runtime / "chess-listener-host"),
                    "type": "stdio",
                    "allowed_extensions": ["chess-listener@local"],
                }
            )
            + "\n",
            encoding="utf-8",
        )

        shutil.copy2("/bin/true", fake_bin / "firefox")
        write_executable(
            fake_bin / "stockfish",
            """#!/usr/bin/env python3
import sys
for raw in sys.stdin:
    command = raw.strip()
    if command == "uci":
        print("id name lifecycle fixture", flush=True)
        print("uciok", flush=True)
    elif command == "quit":
        break
""",
        )
        (fake_python / "PyQt6" / "__init__.py").write_text("", encoding="utf-8")
        (fake_python / "PyQt6" / "QtWidgets.py").write_text(
            "class QApplication:\n    pass\n", encoding="utf-8"
        )

        # The installed updater must delegate to this recorded checkout while
        # preserving both custom lifecycle paths.
        write_executable(
            source / "install.sh",
            """#!/usr/bin/env bash
set -euo pipefail
printf 'prefix=%s\n' "${CHESSLISTENER_PREFIX:-}"
printf 'manifest=%s\n' "${CHESSLISTENER_MANIFEST_DIR:-}"
printf 'arguments=%s\n' "$*"
""",
        )

        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(home),
                "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
                "PYTHONPATH": (
                    f"{fake_python}{os.pathsep}{environment['PYTHONPATH']}"
                    if environment.get("PYTHONPATH")
                    else str(fake_python)
                ),
            }
        )
        environment.pop("CHESSLISTENER_PREFIX", None)
        environment.pop("CHESSLISTENER_MANIFEST_DIR", None)

        checked = run([runtime / "install.sh", "--check"], environment)
        assert "Diagnostics passed." in checked
        assert str(runtime) in checked
        assert "persisted Firefox manifest directory" in checked
        assert checked.count("Maia disabled:") == 1, checked
        assert checked.count("Stockfish-only mode remains fully usable") == 1, checked

        delegated = run([runtime / "update.sh", "--check"], environment)
        assert f"prefix={runtime}" in delegated, delegated
        assert f"manifest={manifest_dir}" in delegated, delegated
        assert "arguments=--check" in delegated, delegated

        dry_run = run([runtime / "uninstall.sh", "--dry-run"], environment)
        assert f"Would remove installation: {runtime}" in dry_run
        assert f"Would remove Firefox manifest: {manifest_path}" in dry_run
        assert runtime.exists() and manifest_path.exists()

        # A marked basename is not sufficient: an unmarked directory must be
        # refused and left untouched.
        refused_environment = environment | {
            "CHESSLISTENER_MANIFEST_DIR": str(manifest_dir)
        }
        refused = run(
            [NATIVE / "uninstall.sh", "--prefix", unmarked],
            refused_environment,
            expected=1,
        )
        assert "refusing to remove an unmarked directory" in refused
        assert unmarked.exists() and manifest_path.exists()

        removed = run([runtime / "uninstall.sh"], environment)
        assert f"Removed installation: {runtime}" in removed
        assert f"Removed Firefox manifest: {manifest_path}" in removed
        assert not runtime.exists()
        assert not manifest_path.exists()
        assert source.exists(), "uninstall must never remove the source checkout"

    print("install lifecycle: OK")


if __name__ == "__main__":
    main()
