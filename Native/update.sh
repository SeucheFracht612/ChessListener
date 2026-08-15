#!/usr/bin/env bash
# Reinstall ChessListener from an updated source checkout.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$SCRIPT_DIR/install.sh"
INSTALLED_COPY=0

# An installed copy records the checkout and manifest directory it came from.
# Prefer those values so custom installations remain stable across updates.
if [ -f "$SCRIPT_DIR/.chess-listener-install" ]; then
    INSTALLED_COPY=1
    if [ ! -r "$SCRIPT_DIR/.install-source" ]; then
        echo "error: installed runtime has no recorded source checkout" >&2
        exit 1
    fi
    SOURCE_DIR="$(sed -n '1p' "$SCRIPT_DIR/.install-source")"
    case "$SOURCE_DIR" in
        /*) ;;
        *)
            echo "error: recorded source path is not absolute" >&2
            exit 1
            ;;
    esac
    if [ ! -x "$SOURCE_DIR/install.sh" ]; then
        echo "error: the original source checkout is unavailable at:" >&2
        echo "  $SOURCE_DIR" >&2
        echo "Clone/update ChessListener, then run its Native/install.sh again." >&2
        exit 1
    fi
    INSTALLER="$SOURCE_DIR/install.sh"

    if [ -z "${CHESSLISTENER_MANIFEST_DIR:-}" ]; then
        if [ ! -r "$SCRIPT_DIR/.manifest-dir" ]; then
            echo "error: installed runtime has no recorded Firefox manifest directory" >&2
            exit 1
        fi
        CHESSLISTENER_MANIFEST_DIR="$(sed -n '1p' "$SCRIPT_DIR/.manifest-dir")"
        case "$CHESSLISTENER_MANIFEST_DIR" in
            /*) export CHESSLISTENER_MANIFEST_DIR ;;
            *)
                echo "error: recorded Firefox manifest directory is not absolute" >&2
                exit 1
                ;;
        esac
    fi
fi

if [ ! -x "$INSTALLER" ]; then
    echo "error: installer is not executable: $INSTALLER" >&2
    exit 1
fi

if [ "$INSTALLED_COPY" -eq 1 ] && [ -z "${CHESSLISTENER_PREFIX:-}" ]; then
    CHESSLISTENER_PREFIX="$SCRIPT_DIR" exec "$INSTALLER" "$@"
fi

exec "$INSTALLER" "$@"
