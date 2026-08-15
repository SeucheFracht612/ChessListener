#!/usr/bin/env bash
# Build and install ChessListener's native host for the current user.
# Supported target: distro-packaged Firefox on Linux x86-64.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
if [ -n "${CHESSLISTENER_PREFIX:-}" ]; then
    INSTALL_PREFIX="$CHESSLISTENER_PREFIX"
elif [ -f "$SCRIPT_DIR/.chess-listener-install" ]; then
    INSTALL_PREFIX="$SCRIPT_DIR"
else
    INSTALL_PREFIX="$DEFAULT_DATA_HOME/chess-listener"
fi
MANIFEST_DIR_OVERRIDE="${CHESSLISTENER_MANIFEST_DIR:-}"
CHECK_ONLY=0
REQUIRE_MAIA=0
REQUIRED_FAILURES=0
MAIA_SOURCE=""
MAIA_REASON="not checked"

usage() {
    cat <<'EOF'
Usage: ./Native/install.sh [--check] [--require-maia] [--prefix PATH]

  (no option)       clean-build and install/update the native host
  --check           run diagnostics without changing any files
  --require-maia    fail installation unless lc0 and every Maia net work
  --prefix PATH     use a non-default absolute path ending in /chess-listener
  -h, --help        show this help

Environment overrides:
  CHESSLISTENER_PREFIX       same purpose as --prefix
  CHESSLISTENER_MANIFEST_DIR Firefox native-messaging manifest directory
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --check)
            CHECK_ONLY=1
            ;;
        --require-maia)
            REQUIRE_MAIA=1
            ;;
        --prefix)
            shift
            if [ "$#" -eq 0 ]; then
                echo "error: --prefix requires an absolute path" >&2
                exit 2
            fi
            INSTALL_PREFIX="$1"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "error: unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
    shift
done

case "$INSTALL_PREFIX" in
    /*) ;;
    *)
        echo "error: install prefix must be absolute: $INSTALL_PREFIX" >&2
        exit 2
        ;;
esac

if [ -n "$MANIFEST_DIR_OVERRIDE" ]; then
    MANIFEST_DIR="$MANIFEST_DIR_OVERRIDE"
elif [ -r "$INSTALL_PREFIX/.manifest-dir" ]; then
    MANIFEST_DIR="$(sed -n '1p' "$INSTALL_PREFIX/.manifest-dir")"
else
    MANIFEST_DIR="$HOME/.mozilla/native-messaging-hosts"
fi
case "$MANIFEST_DIR" in
    /*) ;;
    *)
        echo "error: Firefox manifest directory must be absolute: $MANIFEST_DIR" >&2
        exit 2
        ;;
esac
MANIFEST_PATH="$MANIFEST_DIR/local.chess_listener.json"
case "$INSTALL_PREFIX" in
    /*/chess-listener) ;;
    *)
        echo "error: install prefix must end in /chess-listener: $INSTALL_PREFIX" >&2
        exit 2
        ;;
esac

ok() {
    printf '  OK       %s\n' "$1"
}

optional() {
    printf '  OPTIONAL %s\n' "$1"
}

required_missing() {
    printf '  MISSING  %s\n' "$1"
    REQUIRED_FAILURES=$((REQUIRED_FAILURES + 1))
}

find_stockfish() {
    if [ -x /usr/games/stockfish ]; then
        printf '%s\n' /usr/games/stockfish
    elif command -v stockfish >/dev/null 2>&1; then
        command -v stockfish
    else
        return 1
    fi
}

find_firefox() {
    if command -v firefox >/dev/null 2>&1; then
        command -v firefox
    elif command -v firefox-esr >/dev/null 2>&1; then
        command -v firefox-esr
    else
        return 1
    fi
}

uci_responds() {
    local executable="$1"
    local output

    if ! command -v timeout >/dev/null 2>&1; then
        return 1
    fi

    if ! output="$(printf 'uci\nquit\n' | timeout 15 "$executable" 2>&1)"; then
        return 1
    fi

    case "$output" in
        *uciok*) return 0 ;;
        *) return 1 ;;
    esac
}

validate_maia_at() {
    local base="$1"
    local lc0="$base/Engine/lc0"
    local weight_dir="$base/Engine/maia-chess/maia_weights"
    local rating
    local weight
    local dependencies
    local output
    local library_path="$base/Engine/lib"

    if [ ! -x "$lc0" ]; then
        MAIA_REASON="lc0 is not executable at $lc0"
        return 1
    fi

    for rating in 1100 1200 1300 1400 1500 1600 1700 1800 1900; do
        weight="$weight_dir/maia-$rating.pb.gz"
        if [ ! -s "$weight" ]; then
            MAIA_REASON="missing or empty maia-$rating.pb.gz"
            return 1
        fi
    done

    if ! command -v gzip >/dev/null 2>&1; then
        MAIA_REASON="gzip is unavailable; Maia weights cannot be integrity-checked"
        return 1
    fi
    for rating in 1100 1200 1300 1400 1500 1600 1700 1800 1900; do
        weight="$weight_dir/maia-$rating.pb.gz"
        if ! gzip -t "$weight" >/dev/null 2>&1; then
            MAIA_REASON="maia-$rating.pb.gz is not a valid gzip stream"
            return 1
        fi
    done

    if ! command -v ldd >/dev/null 2>&1; then
        MAIA_REASON="ldd is unavailable; lc0 runtime dependencies cannot be checked"
        return 1
    fi

    if ! dependencies="$(LD_LIBRARY_PATH="$library_path${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
        ldd "$lc0" 2>&1)"; then
        MAIA_REASON="ldd could not inspect lc0"
        return 1
    fi
    case "$dependencies" in
        *"not found"*)
            MAIA_REASON="lc0 has unresolved shared-library dependencies"
            return 1
            ;;
    esac

    if ! command -v timeout >/dev/null 2>&1; then
        MAIA_REASON="timeout is unavailable; lc0 startup cannot be checked safely"
        return 1
    fi

    for rating in 1100 1200 1300 1400 1500 1600 1700 1800 1900; do
        weight="$weight_dir/maia-$rating.pb.gz"
        if ! output="$(printf 'uci\nquit\n' | \
            LD_LIBRARY_PATH="$library_path${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
            timeout 30 "$lc0" "--weights=$weight" 2>&1)"; then
            MAIA_REASON="lc0 could not start with the Maia $rating net"
            return 1
        fi
        case "$output" in
            *uciok*) ;;
            *)
                MAIA_REASON="Maia $rating did not complete the UCI handshake"
                return 1
                ;;
        esac
    done

    MAIA_SOURCE="$base"
    MAIA_REASON="all Maia 1100-1900 gzip nets load and complete a UCI handshake"
    return 0
}

check_platform_and_runtime() {
    local os="unknown"
    local arch="unknown"
    local stockfish=""

    echo "Platform and required runtime"

    if command -v uname >/dev/null 2>&1; then
        os="$(uname -s)"
        arch="$(uname -m)"
    fi

    if [ "$os" = Linux ]; then
        ok "Linux"
    else
        required_missing "Linux is required (detected: $os)"
    fi

    case "$arch" in
        x86_64|amd64) ok "x86-64 architecture" ;;
        *) required_missing "x86-64 is required (detected: $arch)" ;;
    esac

    if find_firefox >/dev/null 2>&1; then
        ok "Firefox ($(find_firefox))"
    else
        required_missing "Firefox executable on PATH"
    fi

    if command -v python3 >/dev/null 2>&1; then
        ok "Python 3 ($(command -v python3))"
    else
        required_missing "Python 3"
    fi

    if command -v python3 >/dev/null 2>&1 && \
       python3 -c 'from PyQt6.QtWidgets import QApplication' >/dev/null 2>&1; then
        ok "PyQt6"
    else
        required_missing "PyQt6 for the overlay (Debian/Ubuntu: python3-pyqt6)"
    fi

    if stockfish="$(find_stockfish)" && uci_responds "$stockfish"; then
        ok "Stockfish UCI engine ($stockfish)"
    elif [ -n "$stockfish" ]; then
        required_missing "Stockfish exists at $stockfish but failed its UCI handshake"
    else
        required_missing "Stockfish (Debian/Ubuntu: stockfish)"
    fi

    if command -v make >/dev/null 2>&1 && command -v cc >/dev/null 2>&1; then
        ok "C build toolchain (make + cc)"
    else
        required_missing "C build toolchain (Debian/Ubuntu: build-essential)"
    fi
}

check_optional_maia() {
    echo "Optional human-move model"
    if [ -x "$SCRIPT_DIR/Engine/lc0" ] || \
       [ -x "$INSTALL_PREFIX/Engine/lc0" ]; then
        echo "  Checking gzip integrity and UCI startup for all nine nets; this may take several minutes."
    fi

    if [ "$CHECK_ONLY" -eq 0 ]; then
        if validate_maia_at "$SCRIPT_DIR"; then
            optional "Local Maia runtime available: $MAIA_REASON"
            return
        fi
    elif validate_maia_at "$INSTALL_PREFIX"; then
        optional "Maia installed: $MAIA_REASON"
        return
    elif [ "$SCRIPT_DIR" != "$INSTALL_PREFIX" ] && \
         validate_maia_at "$SCRIPT_DIR"; then
        optional "Local Maia runtime available: $MAIA_REASON"
        return
    fi

    optional "Maia disabled: $MAIA_REASON"
    optional "Stockfish-only mode remains fully usable"
    if [ "$REQUIRE_MAIA" -eq 1 ]; then
        required_missing "--require-maia was requested"
    fi
}

check_installed_files() {
    local expected_host="$INSTALL_PREFIX/chess-listener-host"

    echo "Installed native host"
    if [ -f "$INSTALL_PREFIX/.chess-listener-install" ] && \
       [ "$(sed -n '1p' "$INSTALL_PREFIX/.chess-listener-install")" = "ChessListener user installation" ]; then
        ok "installation marker ($INSTALL_PREFIX)"
    else
        required_missing "ChessListener installation at $INSTALL_PREFIX"
    fi

    if [ -r "$INSTALL_PREFIX/.manifest-dir" ] && \
       [ "$(sed -n '1p' "$INSTALL_PREFIX/.manifest-dir")" = "$MANIFEST_DIR" ]; then
        ok "persisted Firefox manifest directory"
    else
        required_missing "persisted manifest directory in $INSTALL_PREFIX/.manifest-dir"
    fi

    if [ -x "$expected_host" ]; then
        ok "native host executable"
    else
        required_missing "$expected_host"
    fi

    if [ -r "$INSTALL_PREFIX/overlay.py" ] && [ -r "$INSTALL_PREFIX/san.py" ]; then
        ok "overlay Python modules"
    else
        required_missing "overlay.py and san.py in $INSTALL_PREFIX"
    fi

    if [ ! -r "$MANIFEST_PATH" ]; then
        required_missing "Firefox manifest at $MANIFEST_PATH"
    elif python3 - "$MANIFEST_PATH" "$expected_host" 2>/dev/null <<'PY'
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
valid = (
    manifest.get("name") == "local.chess_listener"
    and manifest.get("path") == sys.argv[2]
    and manifest.get("type") == "stdio"
    and "chess-listener@local" in manifest.get("allowed_extensions", [])
)
raise SystemExit(0 if valid else 1)
PY
    then
        ok "Firefox manifest points to the stable host"
    else
        required_missing "valid Firefox manifest at $MANIFEST_PATH"
    fi
}

print_result() {
    echo
    if [ "$REQUIRED_FAILURES" -eq 0 ]; then
        echo "Diagnostics passed."
        return 0
    fi

    printf 'Diagnostics failed: %d required check(s) did not pass.\n' \
        "$REQUIRED_FAILURES" >&2
    return 1
}

if [ "$CHECK_ONLY" -eq 1 ]; then
    check_platform_and_runtime
    echo
    check_installed_files
    echo
    check_optional_maia
    print_result
    exit $?
fi

if [ -f "$SCRIPT_DIR/.chess-listener-install" ]; then
    echo "error: the installed install.sh only supports --check" >&2
    echo "Run '$SCRIPT_DIR/update.sh' to rebuild from the recorded source checkout." >&2
    exit 1
fi

echo "ChessListener user installation"
echo "  source:  $PROJECT_ROOT"
echo "  target:  $INSTALL_PREFIX"
echo "  Firefox: $MANIFEST_PATH"
echo

check_platform_and_runtime
echo
check_optional_maia
if ! print_result; then
    echo "Install the missing required packages, then run this command again." >&2
    exit 1
fi

if [ -L "$INSTALL_PREFIX" ]; then
    echo "error: refusing to install through symlink: $INSTALL_PREFIX" >&2
    exit 1
fi
if [ -e "$INSTALL_PREFIX" ] && [ ! -d "$INSTALL_PREFIX" ]; then
    echo "error: install prefix exists but is not a directory: $INSTALL_PREFIX" >&2
    exit 1
fi
if [ -d "$INSTALL_PREFIX" ] && \
   [ ! -f "$INSTALL_PREFIX/.chess-listener-install" ] && \
   [ -n "$(find "$INSTALL_PREFIX" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    echo "error: refusing to overwrite an unmarked non-empty directory: $INSTALL_PREFIX" >&2
    exit 1
fi

echo
echo "Clean build"
make -C "$SCRIPT_DIR" clean
make -C "$SCRIPT_DIR"
if [ ! -x "$SCRIPT_DIR/chess-listener-host" ]; then
    echo "error: build completed without an executable native host" >&2
    exit 1
fi

echo
echo "Copying runtime files"
install -d -m 0755 "$INSTALL_PREFIX" "$INSTALL_PREFIX/Engine" "$MANIFEST_DIR"
printf '%s\n' "ChessListener user installation" > "$INSTALL_PREFIX/.chess-listener-install"
printf '%s\n' "$SCRIPT_DIR" > "$INSTALL_PREFIX/.install-source"
printf '%s\n' "$MANIFEST_DIR" > "$INSTALL_PREFIX/.manifest-dir"
install -m 0755 "$SCRIPT_DIR/chess-listener-host" "$INSTALL_PREFIX/chess-listener-host"
install -m 0755 "$SCRIPT_DIR/overlay.py" "$INSTALL_PREFIX/overlay.py"
install -m 0644 "$SCRIPT_DIR/san.py" "$INSTALL_PREFIX/san.py"
install -m 0755 "$SCRIPT_DIR/install.sh" "$INSTALL_PREFIX/install.sh"
install -m 0755 "$SCRIPT_DIR/update.sh" "$INSTALL_PREFIX/update.sh"
install -m 0755 "$SCRIPT_DIR/uninstall.sh" "$INSTALL_PREFIX/uninstall.sh"

if [ -n "$MAIA_SOURCE" ] && [ "$MAIA_SOURCE" != "$INSTALL_PREFIX" ]; then
    install -d -m 0755 \
        "$INSTALL_PREFIX/Engine/maia-chess/maia_weights"
    install -m 0755 "$MAIA_SOURCE/Engine/lc0" "$INSTALL_PREFIX/Engine/lc0"
    for rating in 1100 1200 1300 1400 1500 1600 1700 1800 1900; do
        install -m 0644 \
            "$MAIA_SOURCE/Engine/maia-chess/maia_weights/maia-$rating.pb.gz" \
            "$INSTALL_PREFIX/Engine/maia-chess/maia_weights/maia-$rating.pb.gz"
    done
    if [ -d "$MAIA_SOURCE/Engine/lib" ]; then
        install -d -m 0755 "$INSTALL_PREFIX/Engine/lib"
        cp -a "$MAIA_SOURCE/Engine/lib/." "$INSTALL_PREFIX/Engine/lib/"
    fi
    echo "  installed optional Maia runtime"
else
    # The stable prefix is installer-owned. Remove any legacy managed Maia
    # payload rather than silently retaining the unproven binary removed in
    # 0.2.1; a user who wants Maia supplies a validated local source runtime.
    rm -f -- "$INSTALL_PREFIX/Engine/lc0"
    for rating in 1100 1200 1300 1400 1500 1600 1700 1800 1900; do
        rm -f -- \
            "$INSTALL_PREFIX/Engine/maia-chess/maia_weights/maia-$rating.pb.gz"
    done
    echo "  optional Maia runtime not installed (legacy managed payload removed)"
fi

python3 - "$SCRIPT_DIR/local.chess_listener.json" "$MANIFEST_PATH" \
    "$INSTALL_PREFIX/chess-listener-host" <<'PY'
import json
import os
import pathlib
import sys
import tempfile

template_path = pathlib.Path(sys.argv[1])
destination = pathlib.Path(sys.argv[2])
host_path = pathlib.Path(sys.argv[3])
manifest = json.loads(template_path.read_text(encoding="utf-8"))

if manifest.get("name") != "local.chess_listener":
    raise SystemExit("native manifest template has an unexpected name")

manifest["path"] = str(host_path)
destination.parent.mkdir(parents=True, exist_ok=True)
fd, temporary_name = tempfile.mkstemp(
    prefix=destination.name + ".", dir=destination.parent, text=True
)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as temporary:
        json.dump(manifest, temporary, indent=2)
        temporary.write("\n")
    os.chmod(temporary_name, 0o644)
    os.replace(temporary_name, destination)
finally:
    if os.path.exists(temporary_name):
        os.unlink(temporary_name)
PY

echo "  wrote $MANIFEST_PATH"
echo
echo "Installation complete."
echo "Run '$INSTALL_PREFIX/install.sh --check' for diagnostics."
echo "Load Extension/manifest.json through about:debugging as documented in README.md."
