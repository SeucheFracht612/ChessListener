#!/usr/bin/env python3
"""Hermetic checks for installed-script path persistence and safe removal."""

import gzip
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile


NATIVE = Path(__file__).resolve().parents[1]
MAIA_RATINGS = range(1100, 2000, 100)


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


def create_maia_payload(base, executable_text):
    engine = base / "Engine"
    weights = engine / "maia-chess" / "maia_weights"
    weights.mkdir(parents=True, exist_ok=True)
    write_executable(engine / "lc0", executable_text)
    for rating in MAIA_RATINGS:
        with gzip.open(weights / f"maia-{rating}.pb.gz", "wb") as output:
            output.write(f"lifecycle Maia {rating}\n".encode())


def create_install_source(source):
    source.mkdir(parents=True, exist_ok=True)
    for name in ("install.sh", "update.sh", "uninstall.sh"):
        shutil.copy2(NATIVE / name, source / name)
    for name in ("overlay.py", "san.py", "local.chess_listener.json"):
        shutil.copy2(NATIVE / name, source / name)
    (source / "Makefile").write_text(
        """.PHONY: all clean
all: chess-listener-host
chess-listener-host:
	cp /bin/true chess-listener-host
clean:
	rm -f chess-listener-host
""",
        encoding="utf-8",
    )


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
        reinstall_source = root / "reinstall-source" / "Native"
        reinstall_runtime = root / "reinstall-prefix" / "chess-listener"
        reinstall_manifest_dir = root / "reinstall-firefox-manifests"
        unsafe_runtime = root / "unsafe-prefix" / "chess-listener"
        unsafe_sentinel = root / "unsafe-lc0-was-executed"
        symlink_runtime = root / "symlink-prefix" / "chess-listener"
        symlink_target = root / "symlink-target"
        symlink_sentinel = root / "symlink-lc0-was-executed"

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
        write_executable(fake_bin / "ldd", "#!/usr/bin/env bash\nexit 0\n")
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

        # A source ZIP has no Maia payload of its own. Reinstalling it must
        # preserve an existing, fully validated managed runtime.
        create_install_source(reinstall_source)
        reinstall_runtime.mkdir(parents=True)
        (reinstall_runtime / ".chess-listener-install").write_text(
            "ChessListener user installation\n", encoding="utf-8"
        )
        create_maia_payload(
            reinstall_runtime,
            """#!/usr/bin/env python3
import sys
for raw in sys.stdin:
    if raw.strip() == "uci":
        print("id name lifecycle Maia", flush=True)
        print("uciok", flush=True)
    elif raw.strip() == "quit":
        break
""",
        )
        installed_lc0 = reinstall_runtime / "Engine" / "lc0"
        installed_weights = (
            reinstall_runtime / "Engine" / "maia-chess" / "maia_weights"
        )
        installed_library = reinstall_runtime / "Engine" / "lib" / "libfixture.so"
        installed_library.parent.mkdir()
        installed_library.write_bytes(b"lifecycle shared-library fixture\x00\xff\n")
        lc0_before = installed_lc0.read_bytes()
        library_before = installed_library.read_bytes()
        weights_before = {
            rating: (installed_weights / f"maia-{rating}.pb.gz").read_bytes()
            for rating in MAIA_RATINGS
        }
        reinstall_environment = environment | {
            "CHESSLISTENER_PREFIX": str(reinstall_runtime),
            "CHESSLISTENER_MANIFEST_DIR": str(reinstall_manifest_dir),
        }

        preserved = run([reinstall_source / "install.sh"], reinstall_environment)
        assert "Preserving installed Maia runtime:" in preserved, preserved
        assert "preserved validated installed Maia runtime" in preserved, preserved
        assert installed_lc0.read_bytes() == lc0_before
        assert installed_library.read_bytes() == library_before
        for rating, expected_bytes in weights_before.items():
            assert (
                installed_weights / f"maia-{rating}.pb.gz"
            ).read_bytes() == expected_bytes

        # A second reinstall follows the same preservation path and must be
        # idempotent for the complete optional runtime, including Engine/lib.
        preserved_again = run(
            [reinstall_source / "install.sh"], reinstall_environment
        )
        assert "Preserving installed Maia runtime:" in preserved_again, preserved_again
        assert "preserved validated installed Maia runtime" in preserved_again
        assert installed_lc0.read_bytes() == lc0_before
        assert installed_library.read_bytes() == library_before
        for rating, expected_bytes in weights_before.items():
            assert (
                installed_weights / f"maia-{rating}.pb.gz"
            ).read_bytes() == expected_bytes

        preserved_check = run(
            [reinstall_runtime / "install.sh", "--check"],
            reinstall_environment,
        )
        assert "Maia installed:" in preserved_check, preserved_check
        assert "Diagnostics passed." in preserved_check, preserved_check

        # If that managed payload later becomes incomplete, reinstall keeps
        # the existing fail-closed behavior and removes all managed Maia files.
        (installed_weights / "maia-1500.pb.gz").unlink()
        invalid_removed = run(
            [reinstall_source / "install.sh"], reinstall_environment
        )
        assert "invalid managed payload removed" in invalid_removed, invalid_removed
        assert not installed_lc0.exists()
        assert all(
            not (installed_weights / f"maia-{rating}.pb.gz").exists()
            for rating in MAIA_RATINGS
        )

        # Never execute an apparent lc0 from an unmarked target while deciding
        # whether a previous Maia installation may be preserved.
        create_maia_payload(
            unsafe_runtime,
            f"""#!/usr/bin/env python3
from pathlib import Path
import sys
Path({str(unsafe_sentinel)!r}).write_text("executed\\n")
for raw in sys.stdin:
    if raw.strip() == "uci":
        print("uciok", flush=True)
    elif raw.strip() == "quit":
        break
""",
        )
        (unsafe_runtime / ".chess-listener-install").write_text(
            "not a ChessListener installation\n", encoding="utf-8"
        )
        unsafe_environment = environment | {
            "CHESSLISTENER_PREFIX": str(unsafe_runtime),
            "CHESSLISTENER_MANIFEST_DIR": str(root / "unsafe-manifests"),
        }
        unsafe_refused = run(
            [reinstall_source / "install.sh"], unsafe_environment, expected=1
        )
        assert "refusing to overwrite an unmarked non-empty directory" in unsafe_refused
        assert not unsafe_sentinel.exists(), unsafe_refused

        # Even a correctly marked directory reached through a symlink is not a
        # preservation candidate; refuse it without executing its apparent lc0.
        create_maia_payload(
            symlink_target,
            f"""#!/usr/bin/env python3
from pathlib import Path
import sys
Path({str(symlink_sentinel)!r}).write_text("executed\\n")
for raw in sys.stdin:
    if raw.strip() == "uci":
        print("uciok", flush=True)
    elif raw.strip() == "quit":
        break
""",
        )
        (symlink_target / ".chess-listener-install").write_text(
            "ChessListener user installation\n", encoding="utf-8"
        )
        symlink_runtime.parent.mkdir(parents=True)
        symlink_runtime.symlink_to(symlink_target, target_is_directory=True)
        symlink_environment = environment | {
            "CHESSLISTENER_PREFIX": str(symlink_runtime),
            "CHESSLISTENER_MANIFEST_DIR": str(root / "symlink-manifests"),
        }
        symlink_refused = run(
            [reinstall_source / "install.sh"], symlink_environment, expected=1
        )
        assert "refusing to install through symlink" in symlink_refused
        assert not symlink_sentinel.exists(), symlink_refused

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
