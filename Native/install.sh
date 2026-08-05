#!/usr/bin/env bash
# Installs the Firefox native-messaging manifest and checks prerequisites.
set -euo pipefail

BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MANIFEST_DIR="$HOME/.mozilla/native-messaging-hosts"

echo "== building =="
make -C "$BASE"

echo "== native messaging manifest =="
# Firefox, not Chrome: this project uses browser.* and allowed_extensions.
mkdir -p "$MANIFEST_DIR"
python3 - "$BASE" "$MANIFEST_DIR" <<'PY'
import json, sys, pathlib
base, dest = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
m = json.loads((base / "local.chess_listener.json").read_text())
m["path"] = str(base / "chess-listener-host")     # must be absolute
out = dest / (m["name"] + ".json")                # filename must match "name"
out.write_text(json.dumps(m, indent=2) + "\n")
print("  wrote", out)
print("  path ->", m["path"])
PY

echo "== prerequisites =="
check() { if [ -n "${2:-}" ]; then echo "  OK      $1"; else echo "  MISSING $1"; fi; }

check "stockfish"        "$(command -v stockfish || ([ -x /usr/games/stockfish ] && echo y))"
check "lc0"              "$([ -x "$BASE/Engine/lc0" ] && echo y)"
check "maia-1500 net"    "$([ -r "$BASE/Engine/maia-chess/maia_weights/maia-1500.pb.gz" ] && echo y)"
check "PyQt6"            "$(python3 -c 'import PyQt6' 2>/dev/null && echo y)"

echo
echo "Load the extension: about:debugging -> This Firefox -> Load Temporary Add-on"
echo "  -> pick $BASE/../Extension/manifest.json"
echo "Log: /tmp/chess-listener.log"
