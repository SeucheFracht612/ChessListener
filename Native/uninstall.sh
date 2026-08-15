#!/usr/bin/env bash
# Remove only a marked ChessListener user installation and its owned manifest.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
if [ -n "${CHESSLISTENER_PREFIX:-}" ]; then
    INSTALL_PREFIX="$CHESSLISTENER_PREFIX"
elif [ -f "$SCRIPT_DIR/.chess-listener-install" ]; then
    INSTALL_PREFIX="$SCRIPT_DIR"
else
    INSTALL_PREFIX="$DEFAULT_DATA_HOME/chess-listener"
fi
MANIFEST_DIR_OVERRIDE="${CHESSLISTENER_MANIFEST_DIR:-}"
DRY_RUN=0

usage() {
    cat <<'EOF'
Usage: ./Native/uninstall.sh [--dry-run] [--prefix PATH]

Removes the stable native-host installation and its Firefox manifest. It does
not remove the source checkout, Firefox profile, or saved overlay preferences.
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
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

# Guard every recursive-removal target independently. The basename and marker
# checks prevent a typo or hostile environment variable from widening scope.
case "$INSTALL_PREFIX" in
    /*/chess-listener) ;;
    *)
        echo "error: refusing unsafe prefix (must end in /chess-listener):" >&2
        echo "  $INSTALL_PREFIX" >&2
        exit 1
        ;;
esac
if [ "$INSTALL_PREFIX" = / ] || [ "$INSTALL_PREFIX" = "$HOME" ] || \
   [ -L "$INSTALL_PREFIX" ]; then
    echo "error: refusing unsafe or symlinked uninstall target: $INSTALL_PREFIX" >&2
    exit 1
fi

if [ ! -e "$INSTALL_PREFIX" ]; then
    echo "ChessListener is not installed at $INSTALL_PREFIX."
elif [ ! -f "$INSTALL_PREFIX/.chess-listener-install" ] || \
     [ "$(sed -n '1p' "$INSTALL_PREFIX/.chess-listener-install")" != \
       "ChessListener user installation" ]; then
    echo "error: refusing to remove an unmarked directory: $INSTALL_PREFIX" >&2
    exit 1
elif [ "$DRY_RUN" -eq 1 ]; then
    echo "Would remove installation: $INSTALL_PREFIX"
else
    rm -rf -- "$INSTALL_PREFIX"
    echo "Removed installation: $INSTALL_PREFIX"
fi

if [ -e "$MANIFEST_PATH" ] && command -v python3 >/dev/null 2>&1; then
    MANIFEST_STATUS="$(python3 - "$MANIFEST_PATH" \
        "$INSTALL_PREFIX/chess-listener-host" "$DRY_RUN" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
expected_host = sys.argv[2]
dry_run = sys.argv[3] == "1"
try:
    manifest = json.loads(path.read_text(encoding="utf-8"))
except (OSError, ValueError):
    print("unrecognized")
    raise SystemExit

if manifest.get("name") != "local.chess_listener" or \
   manifest.get("path") != expected_host:
    print("unrelated")
elif dry_run:
    print("would-remove")
else:
    path.unlink()
    print("removed")
PY
    )"
    case "$MANIFEST_STATUS" in
        removed) echo "Removed Firefox manifest: $MANIFEST_PATH" ;;
        would-remove) echo "Would remove Firefox manifest: $MANIFEST_PATH" ;;
        *)
            echo "Left manifest untouched because it was not recognized as this installation:"
            echo "  $MANIFEST_PATH"
            ;;
    esac
elif [ -e "$MANIFEST_PATH" ]; then
    echo "Left Firefox manifest untouched because Python 3 is unavailable:"
    echo "  $MANIFEST_PATH"
fi

echo "The source checkout and overlay preferences were left untouched."
