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
    for name in (
        "overlay.py", "san.py", "explanations.py", "review.py", "study_store.py", "study.py",
        "pgn_import.py", "local.chess_listener.json"
    ):
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


def snapshot_regular_files(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


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
        (runtime / "explanations.py").write_text(
            "# lifecycle fixture\n", encoding="utf-8"
        )
        (runtime / "review.py").write_text("# lifecycle fixture\n", encoding="utf-8")
        shutil.copy2(NATIVE / "study_store.py", runtime / "study_store.py")
        (runtime / "study.py").write_text("# lifecycle fixture\n", encoding="utf-8")
        (runtime / "pgn_import.py").write_text("# lifecycle fixture\n", encoding="utf-8")
        legacy_library = runtime / "reviews.json"
        legacy_library_bytes = json.dumps(
            {
                "version": 2,
                "games": [{"id": "uninstall-preservation-fixture"}],
                "studies": [],
            },
            separators=(",", ":"),
        ).encode("utf-8")
        legacy_library.write_bytes(legacy_library_bytes)
        separated_library = root / "current-data" / "chess-listener-library" / "reviews.json"

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
                # Deliberately differs from the recorded install prefix.  The
                # installed scripts must protect runtime/reviews.json even if
                # XDG_DATA_HOME changed since the older release wrote it.
                "XDG_DATA_HOME": str(root / "current-data"),
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
            "XDG_DATA_HOME": str(root / "reinstall-data"),
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
        assert not (reinstall_runtime / "Engine" / "lib").exists()

        # Replacing Maia from a newly validated source must replace Engine/lib
        # as one payload too; a stale backend library must not remain on the
        # installed LD_LIBRARY_PATH.
        create_maia_payload(
            reinstall_source,
            """#!/usr/bin/env python3
import sys
for raw in sys.stdin:
    if raw.strip() == "uci":
        print("id name replacement Maia", flush=True)
        print("uciok", flush=True)
    elif raw.strip() == "quit":
        break
""",
        )
        source_library = reinstall_source / "Engine" / "lib" / "libnew.so"
        source_library.parent.mkdir(parents=True)
        source_library.write_bytes(b"replacement shared-library fixture\n")
        stale_library = reinstall_runtime / "Engine" / "lib" / "libstale.so"
        stale_library.parent.mkdir(parents=True)
        stale_library.write_bytes(b"must be removed\n")
        replaced = run([reinstall_source / "install.sh"], reinstall_environment)
        assert "installed optional Maia runtime" in replaced, replaced
        assert not stale_library.exists()
        assert (
            reinstall_runtime / "Engine" / "lib" / "libnew.so"
        ).read_bytes() == source_library.read_bytes()
        shutil.rmtree(reinstall_source / "Engine")

        # Before 0.9.5 the default user library shared the install prefix.  A
        # pre-install library-only directory must be migrated out atomically
        # rather than making a first install look like an unsafe foreign tree.
        preinstall_data = root / "preinstall-data"
        preinstall_runtime = preinstall_data / "chess-listener"
        preinstall_manifest_dir = root / "preinstall-manifests"
        preinstall_runtime.mkdir(parents=True)
        preinstall_bytes = json.dumps(
            {
                "version": 2,
                "games": [{"id": "preinstall-library"}],
                "studies": [],
            },
            separators=(",", ":"),
        ).encode("utf-8")
        (preinstall_runtime / "reviews.json").write_bytes(preinstall_bytes)
        preinstall_environment = environment | {
            "XDG_DATA_HOME": str(preinstall_data),
            "CHESSLISTENER_PREFIX": str(preinstall_runtime),
            "CHESSLISTENER_MANIFEST_DIR": str(preinstall_manifest_dir),
        }
        preinstalled = run(
            [reinstall_source / "install.sh"], preinstall_environment
        )
        preinstall_library = (
            preinstall_data / "chess-listener-library" / "reviews.json"
        )
        assert "Migrated saved games and studies:" in preinstalled, preinstalled
        assert preinstall_library.read_bytes() == preinstall_bytes
        assert (preinstall_runtime / ".chess-listener-install").is_file()
        assert not (preinstall_runtime / "reviews.json").exists()

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

        # A valid installation marker does not make symlinked Engine paths
        # trustworthy.  Normal installs must stop before build/copy/removal,
        # while --check must report the unsafe layout without launching lc0.
        safe_lc0 = """#!/usr/bin/env python3
import sys
for raw in sys.stdin:
    if raw.strip() == "uci":
        print("id name safe lifecycle Maia", flush=True)
        print("uciok", flush=True)
    elif raw.strip() == "quit":
        break
"""
        for unsafe_part in (
            "Engine",
            "lc0",
            "lib",
            "maia-chess",
            "maia_weights",
            "weight",
        ):
            unsafe_engine_runtime = (
                root / f"unsafe-engine-{unsafe_part}" / "chess-listener"
            )
            unsafe_external = root / f"external-engine-{unsafe_part}"
            unsafe_engine_manifest = root / f"unsafe-engine-manifest-{unsafe_part}"
            unsafe_engine_sentinel = unsafe_external / "lc0-was-executed"
            unsafe_engine_runtime.mkdir(parents=True)
            unsafe_external.mkdir(parents=True)
            (unsafe_engine_runtime / ".chess-listener-install").write_text(
                "ChessListener user installation\n", encoding="utf-8"
            )
            shutil.copy2(NATIVE / "install.sh", unsafe_engine_runtime / "install.sh")
            shutil.copy2(
                NATIVE / "study_store.py", unsafe_engine_runtime / "study_store.py"
            )
            create_maia_payload(unsafe_engine_runtime, safe_lc0)

            malicious_lc0 = f"""#!/usr/bin/env python3
from pathlib import Path
import sys
Path({str(unsafe_engine_sentinel)!r}).write_text("executed\\n")
for raw in sys.stdin:
    if raw.strip() == "uci":
        print("uciok", flush=True)
    elif raw.strip() == "quit":
        break
"""
            if unsafe_part == "Engine":
                shutil.rmtree(unsafe_engine_runtime / "Engine")
                create_maia_payload(unsafe_external, malicious_lc0)
                (unsafe_engine_runtime / "Engine").symlink_to(
                    unsafe_external / "Engine", target_is_directory=True
                )
            elif unsafe_part == "lc0":
                (unsafe_engine_runtime / "Engine" / "lc0").unlink()
                write_executable(unsafe_external / "lc0", malicious_lc0)
                (unsafe_engine_runtime / "Engine" / "lc0").symlink_to(
                    unsafe_external / "lc0"
                )
            elif unsafe_part == "lib":
                external_lib = unsafe_external / "lib"
                external_lib.mkdir()
                (external_lib / "liboutside.so").write_bytes(b"outside library\n")
                (unsafe_engine_runtime / "Engine" / "lib").symlink_to(
                    external_lib, target_is_directory=True
                )
            elif unsafe_part == "maia-chess":
                create_maia_payload(unsafe_external, malicious_lc0)
                shutil.rmtree(unsafe_engine_runtime / "Engine" / "maia-chess")
                (unsafe_engine_runtime / "Engine" / "maia-chess").symlink_to(
                    unsafe_external / "Engine" / "maia-chess",
                    target_is_directory=True,
                )
            elif unsafe_part == "maia_weights":
                create_maia_payload(unsafe_external, malicious_lc0)
                shutil.rmtree(
                    unsafe_engine_runtime
                    / "Engine"
                    / "maia-chess"
                    / "maia_weights"
                )
                (
                    unsafe_engine_runtime
                    / "Engine"
                    / "maia-chess"
                    / "maia_weights"
                ).symlink_to(
                    unsafe_external
                    / "Engine"
                    / "maia-chess"
                    / "maia_weights",
                    target_is_directory=True,
                )
            else:
                external_weight = unsafe_external / "maia-1500.pb.gz"
                with gzip.open(external_weight, "wb") as output:
                    output.write(b"outside weight\n")
                runtime_weight = (
                    unsafe_engine_runtime
                    / "Engine"
                    / "maia-chess"
                    / "maia_weights"
                    / "maia-1500.pb.gz"
                )
                runtime_weight.unlink()
                runtime_weight.symlink_to(external_weight)

            external_before = snapshot_regular_files(unsafe_external)
            unsafe_engine_environment = environment | {
                "CHESSLISTENER_PREFIX": str(unsafe_engine_runtime),
                "CHESSLISTENER_MANIFEST_DIR": str(unsafe_engine_manifest),
                "XDG_DATA_HOME": str(root / f"unsafe-engine-data-{unsafe_part}"),
            }

            unsafe_normal = run(
                [reinstall_source / "install.sh"],
                unsafe_engine_environment,
                expected=1,
            )
            assert "refusing unsafe managed Maia layout" in unsafe_normal
            assert not unsafe_engine_sentinel.exists(), unsafe_normal
            assert snapshot_regular_files(unsafe_external) == external_before

            unsafe_check = run(
                [unsafe_engine_runtime / "install.sh", "--check"],
                unsafe_engine_environment,
                expected=1,
            )
            assert "MISSING  unsafe managed Maia layout" in unsafe_check
            assert "Diagnostics failed:" in unsafe_check
            assert not unsafe_engine_sentinel.exists(), unsafe_check
            assert snapshot_regular_files(unsafe_external) == external_before

        checked = run([runtime / "install.sh", "--check"], environment)
        assert "Diagnostics passed." in checked
        assert str(runtime) in checked
        assert "persisted Firefox manifest directory" in checked
        assert checked.count("Maia disabled:") == 1, checked
        assert checked.count("Stockfish-only mode remains fully usable") == 1, checked
        assert "Would migrate saved games and studies:" in checked, checked
        assert legacy_library.read_bytes() == legacy_library_bytes
        assert not separated_library.exists(), "--check must remain read-only"

        delegated = run([runtime / "update.sh", "--check"], environment)
        assert f"prefix={runtime}" in delegated, delegated
        assert f"manifest={manifest_dir}" in delegated, delegated
        assert "arguments=--check" in delegated, delegated

        dry_run = run([runtime / "uninstall.sh", "--dry-run"], environment)
        assert "Would migrate saved games and studies:" in dry_run, dry_run
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
        assert "Migrated saved games and studies:" in removed, removed
        assert f"Removed installation: {runtime}" in removed
        assert f"Removed Firefox manifest: {manifest_path}" in removed
        assert not runtime.exists()
        assert not manifest_path.exists()
        assert source.exists(), "uninstall must never remove the source checkout"
        assert separated_library.read_bytes() == legacy_library_bytes

    print("install lifecycle: OK")


if __name__ == "__main__":
    main()
