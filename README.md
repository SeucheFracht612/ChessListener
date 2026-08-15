# ChessListener

ChessListener is a local analysis companion for **Chess.com games you are
spectating**. A Firefox extension observes the board, a native C host rebuilds
the chess position, Stockfish supplies objective analysis, and an optional
Maia/lc0 model predicts a plausible human move for a selected rating.

> **Fair-play boundary:** do not use ChessListener while playing a live game.
> It is intended only for games in which you are a spectator and for analysis
> after a game has ended. Site rules and tournament rules remain authoritative;
> see the [Chess.com Fair Play Policy](https://www.chess.com/legal/fair-play).

## Current support

- Linux x86-64 only.
- Firefox installed as a normal distribution package.
- Debian and Ubuntu are the primary supported distributions.
- Chess.com spectator pages.
- Stockfish is required. Maia is optional and needs a user-provided lc0
  executable; the overlay works in Stockfish-only mode when it is unavailable.

Firefox packaged as a Snap or Flatpak may not see the normal native-messaging
manifest or host because of sandboxing. Those packages are not currently a
supported installation target. Chrome/Chromium, macOS, Windows, ARM Linux, and
Lichess are not currently supported.

## Install

### 1. Install system dependencies

On Debian:

```bash
sudo apt update
sudo apt install build-essential python3 python3-pyqt6 stockfish firefox-esr
```

On Ubuntu, install the non-browser dependencies first:

```bash
sudo apt update
sudo apt install build-essential python3 python3-pyqt6 stockfish
```

Ubuntu's ordinary `firefox` package commonly installs the Snap build, whose
sandbox is not a supported native-messaging target here. Install Mozilla's
official DEB/APT Firefox instead, following Mozilla's maintained
[Install Firefox on Linux instructions](https://support.mozilla.org/en-US/kb/install-firefox-linux)
rather than copying a repository command that may become stale. The installer
accepts either a `firefox` or `firefox-esr` executable on `PATH`.

Maia additionally needs the repository submodules and a locally built or
otherwise provenance-verified lc0 executable at `Native/Engine/lc0`. That file
is intentionally ignored by Git and is not distributed by this repository.
Build lc0 using its upstream instructions, then copy the resulting executable
to that path. Depending on the selected lc0 backend, Debian/Ubuntu may also
need an OpenBLAS runtime such as `libopenblas0` or `libopenblas0-pthread`:

```bash
sudo apt install libopenblas0
```

Maia is deliberately optional. Do not block a Stockfish-only installation if
you have not built lc0 or your distribution uses a different OpenBLAS package.
See [`Native/Engine/README.md`](Native/Engine/README.md) for the exact local
layout and validation rules.

### 2. Clone everything

```bash
git clone --recurse-submodules https://github.com/SeucheFracht612/ChessListener.git
cd ChessListener
```

For an existing clone:

```bash
git submodule sync --recursive
git submodule update --init --recursive
```

### 3. Build and test

```bash
make clean
make
make test
```

`make test` always builds the current source and uses deterministic fake UCI
engines for integration coverage. A real Stockfish/Maia installation is not
required to run the normal automated test suite.

### 4. Install the native host

Completely exit Firefox before installing so no native-host process is using a
partially replaced runtime.

```bash
./Native/install.sh
./Native/install.sh --check
```

The install is idempotent. It clean-builds the native host and copies the
runtime into the stable per-user directory:

```text
${XDG_DATA_HOME:-$HOME/.local/share}/chess-listener
```

It then generates Firefox's manifest at:

```text
$HOME/.mozilla/native-messaging-hosts/local.chess_listener.json
```

The generated manifest points to the stable installed copy, never to a
developer's checkout. `--check` is read-only and verifies the platform,
Firefox, Python/PyQt6, Stockfish's UCI handshake, installed files, manifest,
lc0's dynamic libraries, the gzip integrity of every Maia 1100–1900 weight,
and a bounded lc0 UCI handshake with each of the nine nets. When Maia is
present, those engine-start checks can take several minutes.

Advanced users can select a custom prefix (which must end in
`/chess-listener`) and a non-default manifest directory for the first install:

```bash
CUSTOM_PREFIX=/absolute/custom/path/chess-listener
CHESSLISTENER_MANIFEST_DIR=/absolute/firefox/native-messaging-hosts \
  ./Native/install.sh --prefix "$CUSTOM_PREFIX"
"$CUSTOM_PREFIX/install.sh" --check
```

The resolved manifest directory is recorded inside the marked runtime. Its
installed `--check`, `update.sh`, and `uninstall.sh` reuse that exact directory
without requiring the environment variable again.

By default a missing, invalid, or incomplete user-provided Maia runtime is
reported as optional and ChessListener continues with Stockfish. To make Maia
mandatory for a particular installation:

```bash
./Native/install.sh --require-maia
```

### 5. Load the Firefox extension

1. Open `about:debugging` in Firefox.
2. Select **This Firefox**.
3. Select **Load Temporary Add-on…**.
4. Choose `Extension/manifest.json` from this checkout.
5. Open a supported Chess.com game as a spectator.

The repository does **not** currently publish a Mozilla-signed XPI. Firefox
release builds remove temporary add-ons when the browser exits, so step 3 must
be repeated after restarting Firefox. A permanent end-user install requires a
properly packaged and Mozilla-signed extension; native-host installation alone
does not change that Firefox restriction.

## Update

Updates are explicit: first update the checkout, then rebuild/reinstall it.
Completely exit Firefox before running these commands; restart it and reload
the temporary extension only after the update and diagnostics finish.

```bash
git pull --ff-only
git submodule sync --recursive
git submodule update --init --recursive
./Native/update.sh
./Native/install.sh --check
```

`update.sh` never runs `git pull` for you and never discards local changes. The
copy installed under the stable prefix also contains `update.sh`; it can locate
the original checkout while that checkout remains in the same place.

For a custom prefix or manifest directory, run the **installed** lifecycle
scripts so the recorded paths are preserved:

```bash
CUSTOM_PREFIX=/absolute/custom/path/chess-listener
"$CUSTOM_PREFIX/update.sh"
"$CUSTOM_PREFIX/install.sh" --check
```

Alternatively, pass the same `--prefix`/`CHESSLISTENER_PREFIX` and
`CHESSLISTENER_MANIFEST_DIR` values explicitly to source-tree commands.

Reload the temporary extension in `about:debugging` after an extension update.
Keep `Extension/` and the installed native host from the same release.

## Uninstall

Preview the exact targets, then remove them:

```bash
./Native/uninstall.sh --dry-run
./Native/uninstall.sh
```

The uninstaller removes only a marked `.../chess-listener` runtime directory
and a manifest that points to that exact host. It refuses symlinked, unmarked,
or broadly scoped paths. It leaves the Git checkout, Firefox profile, and saved
overlay preferences untouched.

For a custom installation, use its installed script; it reads the persisted
manifest directory before removing the runtime:

```bash
CUSTOM_PREFIX=/absolute/custom/path/chess-listener
"$CUSTOM_PREFIX/uninstall.sh" --dry-run
"$CUSTOM_PREFIX/uninstall.sh"
```

## Architecture

| Layer | Responsibility |
|---|---|
| `Extension/content.js` | Detects supported Chess.com boards/routes, filters unstable DOM snapshots, and sends board state. |
| `Extension/background.js` | Owns the Firefox native-messaging connection and verifies protocol compatibility. |
| `Native/main.c` | Parses native messages, reconstructs legal chess state, infers/catches up moves, and emits FEN. |
| `Native/analysis.c` | Runs latest-position-wins analysis with Stockfish and optional Maia. |
| `Native/uci.c` | Manages persistent UCI engine processes, UCI parsing, timeouts, and cancellation. |
| `Native/overlay.c` | Owns the pipe and lifecycle of the Python overlay process. |
| `Native/overlay.py` | Renders the PyQt6 board, evaluation, arrows, settings, and move information. |
| `Native/san.py` | Converts UCI moves and principal variations to SAN for display. |

The browser-to-host channel uses Firefox's length-prefixed native-messaging
format. Release 0.2.1 uses **protocol version 1**. The extension sends a `hello`
containing its protocol and release version; the host replies with its version
and capabilities before accepting positions. The host and Python overlay also
negotiate protocol 1 before engines begin. A mismatch is rejected instead of
silently mixing incompatible components.

If Firefox reports an incompatible protocol, update and reinstall the native
host, reload the extension, and run `./Native/install.sh --check`.

## Development

Useful targets from the repository root:

```bash
make             # optimized native host
make check       # Python, JavaScript, and shell syntax/source checks
make test        # clean/current build plus deterministic test suite
make debug       # debug build
make asan        # sanitizer build/tests where supported
make clean
```

Do not trust committed or left-over object files when debugging a source
change. Start with `make clean`; CI does the same.

The extension tests can also be run directly as documented by the test output.
Native integration tests speak the actual length-prefixed browser protocol and
use a headless overlay plus deterministic UCI doubles.

### Debug logging

Debug logging is off by default. Completely exit Firefox, then launch it from a
terminal with the environment flag so the native child inherits it:

```bash
CHESSLISTENER_DEBUG=1 firefox
```

For Debian's executable, use `firefox-esr` if appropriate. Logs are written to:

- `/tmp/chess-listener.log` for host/session state;
- `/tmp/chess-listener-engine.log` for UCI traffic.

These files can contain board positions, moves, and engine output. Remove them
before sharing if that game data is private. Extension-side errors appear in
the temporary extension's inspector from `about:debugging`.

## Troubleshooting

### “Native host not found”

Run:

```bash
./Native/install.sh --check
```

Confirm that the reported manifest path exists and points at the stable
installed executable. If Firefox is a Snap or Flatpak, use a distro-packaged
Firefox for the supported path. Restart Firefox after changing the manifest.

### The overlay does not open

Verify PyQt6 with the same system Python used by the host:

```bash
python3 -c 'from PyQt6.QtWidgets import QApplication; print("PyQt6 OK")'
```

Then enable debug logging and inspect `/tmp/chess-listener.log`. A stale
temporary extension/native-host pairing will be reported as a protocol error.

### Stockfish is unavailable

ChessListener looks first for `/usr/games/stockfish`, then for `stockfish` on
`PATH`. Confirm a UCI handshake:

```bash
printf 'uci\nquit\n' | /usr/games/stockfish
```

The output should contain `uciok`.

### Maia is unavailable

This is non-fatal. `./Native/install.sh --check` reports the first failing
condition. Common causes are uninitialized submodules, a missing weight among
1100 through 1900, or an unresolved OpenBLAS dependency:

```bash
git submodule update --init --recursive
ldd Native/Engine/lc0
./Native/install.sh --require-maia
```

`ldd` must not contain `not found`. More engine-layout detail is in
[`Native/Engine/README.md`](Native/Engine/README.md).

### The extension vanished after restart

That is expected for an unsigned temporary add-on. Load
`Extension/manifest.json` again through `about:debugging`. A signed XPI remains
a release-engineering task.

## Privacy and fair play

Board capture and engine analysis happen locally. ChessListener has no
telemetry service and does not upload observed positions or engine results.
The extension requests access to Chess.com pages and native messaging because
those are required for capture and the local host connection.

Debug logs are local but may preserve game positions until deleted. Chess.com
itself still receives the normal traffic generated by its website.

Release 0.2.1 does **not** automatically prove that you are only spectating; it
relies on user compliance. An automated spectator-only gate is deferred work.
Never run the tool on a game you are participating in, and stop using it if a
site or event rule prohibits spectator assistance.

## License

Copyright (C) 2026 SeucheFracht612.

ChessListener's original source code is licensed under the GNU General Public
License, version 3 only (`GPL-3.0-only`). See [`LICENSE`](LICENSE). This program
comes with no warranty, to the extent permitted by law.

Third-party components and user-installed dependencies keep their own license
terms; the repository license does not relicense them. See
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) before distributing a build.
