# ChessListener

ChessListener is a local analysis companion for Chess.com boards. A Firefox
extension observes the board, a native C host rebuilds the chess position,
Stockfish supplies objective analysis, and an optional Maia/lc0 model predicts
a plausible human move for a selected rating. Its Analysis Lab also lets you
explore legal variations on the miniature board without changing the tracked
live game.

After a verified game, **Local Game Review** can run a separate retrospective
Stockfish pass over every position. It provides a move-by-move board and
timeline, conservative classifications, evaluation loss, turning points,
alternatives and continuations, and annotated PGN export. Everything runs on
your machine; no game or analysis is uploaded.

Review Explorer adds a clickable evaluation graph, move filters, per-side
summaries, cached reruns, and a bounded local multi-game library. Any reviewed
position can be explored as an interactive local branch even after the
Chess.com session has ended.

ChessListener also imports a local PGN main line or an exact six-field FEN. The
record is legally replayed before it is accepted, then uses the same review,
graph, engine, export, and exploration tools. A standalone local mode works
without opening Firefox or Chess.com.

Version 0.9.5 gives the whole application a cohesive **Analyst's Desk** UI.
Live analysis remains a compact companion, while Review and Saved Studies
expand into responsive local workspaces. A completed game now keeps its final
board visible beside direct actions to save, review, explore, or export it.
Analysis Lab trees can still be named, annotated, stored locally, searched,
reopened offline, extended with legal branches, and exported as a full
variation PGN with evaluation snapshots.

ChessListener deliberately does not try to decide whether a page is a
spectator, bot, test, or participant game. That keeps local testing possible
and avoids a brittle site-policy detector. You are responsible for using it
where analysis is allowed; site and tournament rules remain authoritative.
See the [Chess.com Fair Play Policy](https://www.chess.com/legal/fair-play).

## Current support

- Linux x86-64 only.
- Firefox 115 or newer, installed as a normal distribution package.
- Debian and Ubuntu are the primary supported distributions.
- Chess.com pages containing a supported live board, including bot and
  spectator games.
- Standard-chess move history exposed by the current page is used when
  available. Chess960 and other variants are not yet reconstructed by the
  authoritative-history path.
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

Node.js is a **test-only** dependency on both Debian and Ubuntu. It is not used
by the installed extension or native host, but it is required for `make check`,
`make test`, and the popup portion of `make visual-test`:

```bash
sudo apt install nodejs
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

That directory contains replaceable program/runtime files only. Saved games,
studies, and review caches live separately under
`${XDG_DATA_HOME:-$HOME/.local/share}/chess-listener-library`, so reinstalling
or uninstalling the native host does not remove them.

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

By default Maia is optional. A fully validated Maia runtime in the source
checkout replaces the installed copy. When reinstalling from a source archive
that contains no Maia payload, an already-installed runtime is preserved only
if all nine nets still pass the complete validation. If neither copy validates,
the incomplete managed payload (including its private `Engine/lib` directory)
is removed and ChessListener continues with Stockfish. A validated source
replacement also replaces `Engine/lib` as a unit, so libraries from an older
lc0 backend cannot remain on the installed search path. To make Maia mandatory
for a particular installation:

```bash
./Native/install.sh --require-maia
```

### 5. Load the Firefox extension

1. Open `about:debugging` in Firefox.
2. Select **This Firefox**.
3. Select **Load Temporary Add-on…**.
4. Choose `Extension/manifest.json` from this checkout.
5. Open a supported Chess.com board. The first visible board can claim the
   single analysis session automatically, or use the toolbar popup and select
   **Analyze this tab**.

The repository does **not** currently publish a Mozilla-signed XPI. Firefox
release builds remove temporary add-ons when the browser exits, so step 3 must
be repeated after restarting Firefox. A permanent end-user install requires a
properly packaged and Mozilla-signed extension; native-host installation alone
does not change that Firefox restriction.

### Standalone local review and studies

The installed overlay can open directly into Review & Import without Firefox,
native messaging, or a Chess.com session:

```bash
~/.local/share/chess-listener/overlay.py --local
```

From a source checkout, use `python3 Native/overlay.py --local`. This mode runs
the same local Stockfish review, historical explorer, and Saved Studies page.
It does not start the live Stockfish/Maia lanes because there is no browser
board to follow.

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

## Sessions and recovery

ChessListener analyzes exactly one browser game at a time. Boards in other
tabs are remembered but cannot replace the owner automatically. Use the
Firefox toolbar popup to inspect the active game, explicitly switch with
**Analyze this tab**, request **Re-read board**, or **Stop session**.

Navigation, game completion, and closing the owning tab end its session and
stop position analysis. Closing the overlay is sticky for that session: a
later board mutation does not immediately reopen it. An unexpected native-host
failure gets one automatic reconnect and latest-position replay; after that,
the popup reports the failure and waits for an explicit action.

The overlay's circular-arrow **Recovery** page contains the manual tools:

- re-read the current Chess.com board;
- replace hidden state using the visible pieces plus side to move, castling,
  en-passant, and move counters;
- apply an exact six-field FEN;
- restart Stockfish and Maia; or
- stop the current session.

The native host validates replacement FENs before changing its authoritative
position. A rejected value leaves the prior position intact. Board orientation
is tracked separately, so flipping a board no longer requires a move before
the overlay follows it.

### Fast path and state confidence

Board capture, history reconciliation, and recovery use separate clocks. An
ordinary unique legal move is published to Stockfish immediately after a short
confirming DOM read; it does not wait for the move list or run a multi-ply
search. The move list is sampled later and replayed through the native legal
move generator. ChessListener trusts it only when a complete replay lands on
the displayed 64-square board.

The small badge in the overlay header describes the resulting state:

- **Exact** — hidden state is known from the initial position, unique legal
  tracking, or a complete validated history replay.
- **Inferred** — a bounded board-only recovery was needed, so hidden history
  may have been reconstructed conservatively.
- **Manual** — the current state descends from a FEN supplied through Recovery.
- **Syncing** — the visible board disagrees with the last trustworthy state.
  The existing board and evaluation stay visible while ChessListener waits for
  history; it does not immediately analyze a guessed FEN.

If exact history has not arrived after the grace period, a recovery snapshot
may run a shallow search with both a node limit and a 50 ms wall-clock limit.
That work is never run for an ordinary one-ply transition. A later validated
history can promote an identical state to Exact without restarting Stockfish,
or correct different hidden FEN fields under a new state revision.

Move-list selectors on Chess.com are not a public interface and may change.
They are treated as extraction adapters, not as proof: legal replay and board
equality are the stability boundary. When no complete history is exposed,
ChessListener continues with its board-only tracking and the manual Recovery
tools remain available.

## Analysis Lab

Analysis Lab is a separate, session-local analysis board. Entering Explore
copies the currently displayed trusted position into a native-validated branch;
it does not apply a Recovery FEN and cannot alter the authoritative live game.

The miniature board accepts drag-and-drop, click-to-move, and keyboard input.
Legal destinations are highlighted, illegal moves leave the branch unchanged,
and promotions require an explicit queen, rook, bishop, or knight choice.
Root, Undo, Redo, Go Live, and Resume controls navigate the branch while the
native host validates every committed move using the same rules as live state
tracking.

Stockfish candidates are shown as selectable MultiPV rows with score, depth,
and a configurable SAN continuation. A selected line can be stepped through as
a read-only PV without starting additional searches; **Explore here** turns the
displayed continuation into a new editable branch. Maia remains independent
and can be disabled entirely.

While an explicit branch is open, ChessListener continues capturing and
reconciling the real game in the background. The default behaviour preserves
the branch and displays a live-update notice. A setting can instead return to
Live automatically. A new game or session replacement always discards the old
branch so analysis cannot cross games.

The **What the line shows** section is deliberately factual. It can describe
the principal continuation, expected best reply, candidate-score difference,
checks, captures and recaptures, castling, promotions, mate, and the actual
piece inventory at the displayed horizon. It does not claim that Stockfish has
explained its internal reasoning or manufacture strategic statements such as
"improves king safety" when the line does not prove them.

The Settings page puts everyday analysis choices first and keeps Analysis Lab,
Review, board/display, and advanced engine controls in collapsible sections.
The configurable choices include analysis strength, threads, candidate count,
Maia rating or Off, Explore strength, continuation length, explanation detail,
evaluation perspective, live-follow behaviour, candidate expansion, board
markings, coordinates, opacity, always-on-top, reduced motion, and whether
Compact mode is remembered. The board piece style is global: **Outline set**
uses the outline family for both colours, while **Solid silhouettes** uses the
filled family for both. More MultiPV candidates divide the same Stockfish
search effort, so a larger count may reach less depth in the same time.

## Saved Studies

Open **Saved Studies** from the title bar or its labeled overflow menu. While
Analysis Lab is open,
press **Save** in its toolbar (or **Save Lab** on the study page) to capture the
entire variation tree currently known to the UI, not only the selected line.
You can also press **New** to start a one-position study from the current live,
review, imported, or already-saved position.

Saved studies are independent of the browser session and native branch IDs.
Move pieces on the study board to add a continuation; replaying an existing
move selects that child instead of duplicating it. Root, parent, and forward
controls navigate the tree, while the tree itself exposes every alternative
and remembers which subtrees were collapsed. Each position can have a
variation name and a longer comment. Search matches titles, metadata, move
coordinates, variation names, and comments. Those text fields are copied into
the local model immediately and saved atomically after a short debounce;
Saving, Saved, and Failed states remain visible, and a failed write blocks a
navigation that would otherwise discard the visible edit.

Selecting a node can automatically run a finite local Stockfish pass using the
configured Explore strength, threads, and candidate count. The result is a
bounded evaluation snapshot attached to that position. Settings can disable
automatic study analysis or keep evaluations temporary instead of saving
them. This separate Stockfish process never changes or stalls live analysis,
Recovery, Maia, or a native Analysis Lab branch.

**Export** writes a complete annotated PGN: the first child at each position is
the main continuation and siblings are recursive PGN side variations. Names,
comments, honest lower/upper evaluation bounds, exact `[%eval]` and
`[%depth]` tags where available, and custom SetUp/FEN roots are retained. Every
loaded or exported tree is legally replayed; invalid moves, cycles,
unreachable nodes, and stored FENs that disagree with their path fail closed.

## Local Review, PGN/FEN Import, and Library

Open **Local Review** from the title bar, finished-game panel, or labeled
overflow menu. ChessListener can use a complete
move history verified from the live page, **Import PGN…**, or **Import FEN…**.
PGN import keeps one main line, ignores comments and nested side variations,
and legally matches every SAN token before converting it to canonical UCI.
Custom positions require all six FEN fields. Invalid, ambiguous, oversized, or
multi-game files leave the current record untouched.

When a tracked game finishes, the live page keeps the final board and presents
**Save game**, **Run local review**, **Explore final position**, and **Export
PGN**. Saving is idempotent for the completed browser session, and
**Automatically save completed games** is independent of automatic review.
If Chess.com exposes an exact result token it is retained; otherwise the local
record uses `*` rather than guessing. If complete move history is unavailable,
ChessListener can still save the validated final position as a clearly marked
position-only SetUp/FEN record; it never substitutes whichever library game
happened to be open. The same actions remain available from Review later.

Press **Run local review** at any time, or enable automatic review in Settings.
A separate Stockfish process analyses every position, so the stronger
historical pass never stalls live capture, Maia, or Analysis Lab. A FEN with
no moves is still a complete one-position review and can immediately be used
with **Explore here**.

Select any row or evaluation-graph point to revisit its position. Each row shows SAN, a
conservative classification, and mover-relative evaluation loss. The detail
panel shows the engine depth, preferred move, and configured number of SAN
continuations. Mistakes and blunders count as turning points. Review can be
cancelled and rerun at another strength; progress is shown position by
position. Filters can isolate errors, turning points, and forcing moves, while
the overview compares White and Black average loss and error counts.

**Explore here** copies the selected reviewed FEN into a separate interactive
local branch. It supports legal drag/click moves, promotion choice, undo, best
arrow, and fresh MultiPV continuations after the browser session has ended.
Left/Right, Home, and End navigate the historical timeline.

Up to 50 games/positions, 100 studies, and setting-specific cached reviews are stored in
`$XDG_DATA_HOME/chess-listener-library/reviews.json` (normally
`~/.local/share/chess-listener-library/reviews.json`) with atomic replacement
and a 16 MB combined cap. A study is additionally capped at 512 positions. Imported
player/event/result metadata is retained. Imports and explicitly or
automatically saved completed games are written before analysis, and identical
settings reuse the cached review. Different review settings remain separately
cached for the same game. The saved selector can reopen or delete records.
**Export PGN** writes the legal game with local review
comments and custom SetUp/FEN headers where required. A schema-1 library from
0.8 is migrated in memory and rewritten atomically on the next change.

An existing library is never treated as empty merely because it is malformed,
unreadable, oversized, or not a regular file. ChessListener preserves the
original bytes, disables library mutations, and shows an actionable warning;
live analysis remains available while the archive is repaired or restored.
Writes also fail closed if another process changes the library between reading
and saving it.

On the first 0.9.5 local launch, an older default
`$XDG_DATA_HOME/chess-listener/reviews.json` is moved atomically to the separate
library directory. Install, update, and uninstall also protect a legacy
`reviews.json` physically inside their exact managed runtime—including custom
prefixes and installations whose `XDG_DATA_HOME` later changed. An existing
new library is never
overwritten: if the two files differ, the old file is preserved beside it as a
content-addressed `reviews.legacy-….json` recovery copy and the lifecycle script
reports both paths. `--check` and uninstall `--dry-run` report the same plan
without changing any file. Advanced users may set `CHESSLISTENER_LIBRARY` to an
exact absolute JSON path **outside every managed `.../chess-listener` runtime**.
The running overlay uses that override as-is; install/uninstall scripts do not
manage or migrate arbitrary override paths. They only protect the old
`reviews.json` at the root of a managed runtime before removing it.

The classification thresholds are intentionally conservative and the numeric
loss is an engine-evaluation change, not literal material. ChessListener does
not reproduce Chess.com's proprietary accuracy score and does not call a
cloud service. Custom-FEN games are exported with SetUp/FEN headers.

## Uninstall

Preview the exact targets, then remove them:

```bash
./Native/uninstall.sh --dry-run
./Native/uninstall.sh
```

The uninstaller removes only a marked `.../chess-listener` runtime directory
and a manifest that points to that exact host. It refuses symlinked, unmarked,
or broadly scoped paths. It leaves the Git checkout, Firefox profile, saved
overlay preferences, and the separate saved-game/study library untouched. If
it finds a pre-0.9.5 library inside the runtime, it first preserves that file
in the separate library directory (or reports the action in `--dry-run` mode)
and refuses to continue if preservation cannot be completed safely.

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
| `Extension/content.js` | Detects supported Chess.com boards/routes, sends the low-latency board lane, and separately extracts delayed move-history candidates. |
| `Extension/background.js` | Brokers one explicit game session across tabs, orders board/history replay on reconnect, and verifies protocol compatibility. |
| `Native/main.c` | Parses native messages, tracks legal live state, validates full history replay, owns isolated Analysis Lab branches, and runs only bounded delayed board recovery. |
| `Native/analysis.c` | Runs revision- and target-scoped latest-position-wins Stockfish and Maia lanes, switching the existing engines between Live and Explore without mixing results. |
| `Native/uci.c` | Manages persistent UCI engine processes, UCI parsing, timeouts, and cancellation. |
| `Native/overlay.c` | Owns the pipe and lifecycle of the Python overlay process. |
| `Native/overlay.py` | Renders the PyQt6 board, evaluation, arrows, settings, and move information. |
| `Native/san.py` | Converts UCI moves and principal variations to SAN for display. |
| `Native/explanations.py` | Derives bounded, factual descriptions from legally replayed engine lines. |
| `Native/review.py` | Runs the isolated local post-game Stockfish pass, move classifications, and annotated PGN export. |
| `Native/study.py` | Validates persistent variation trees, manages legal local branches, and exports recursive annotated study PGN. |
| `Native/study_store.py` | Atomically stores the bounded local game/study library and setting-specific review caches. |
| `Native/pgn_import.py` | Parses one local PGN main line, validates six-field FENs, and legally converts SAN to canonical UCI. |

The browser-to-host channel uses Firefox's length-prefixed native-messaging
format. Release 0.9.5 uses **protocol version 4**. The extension sends a `hello`
containing its protocol and release version; the host replies with its version
and capabilities before accepting session-scoped snapshots or recovery
commands. The host and Python overlay also negotiate protocol 4 before engines
begin. A mismatch is rejected instead of silently mixing incompatible
components.

Protocol 4 keeps `position_snapshot` as the latency-critical lane and retains a
separate session- and snapshot-scoped `history_reconcile` message. Every board,
evaluation, and source update carries a monotonic state revision, so late
Stockfish, Maia, history, and recovery results cannot cross a correction or a
new game. Analysis Lab frames additionally carry an explicit Live or Explore
target plus branch/node identity; live updates cannot overwrite a branch and
late branch results cannot appear after returning to Live.

If Firefox reports an incompatible protocol, update and reinstall the native
host, reload the extension, and run `./Native/install.sh --check`.

## Development

Useful targets from the repository root:

```bash
make             # optimized native host
make check       # Python, JavaScript, and shell syntax/source checks
make test        # clean/current build plus deterministic test suite
make visual-test # strict real-PyQt screen matrix and HTML contact sheet
make debug       # debug build
make asan        # sanitizer build/tests where supported
make tsan        # focused Stockfish/Maia race check where supported
make clean
```

Do not trust committed or left-over object files when debugging a source
change. Start with `make clean`; CI does the same.

The extension tests can also be run directly as documented by the test output.
Native integration tests speak the actual length-prefixed browser protocol and
use a headless overlay plus deterministic UCI doubles.

With PyQt6 available, `make visual-test` renders the strict 0.9.5 matrix to
`Native/.build/visual-ui/` and writes `index.html` for human inspection. It
covers narrow, normal, wide-workspace, and enlarged-text layouts plus both
piece families and important loading, error, cancellation, save, and
finished-game states.

The engine-lane regression test deliberately delays Maia and verifies that a
Stockfish result still arrives first, that rapid supersession favors the new
position, and that Maia later enriches only the matching state revision.

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

### The state badge stays on Syncing or Inferred

Use the overlay's **Recovery** button and select **Re-read board** first. A
Syncing badge means the visible board could not yet be connected legally to the
last trusted state; the old evaluation is intentionally being retained. An
Inferred badge means bounded board-only recovery succeeded but no complete move
history was available to verify every hidden field.

If the board is correct, analysis can continue in Inferred mode. For an exact
manual recovery, paste a six-field FEN in the Recovery page. Enabling
`CHESSLISTENER_DEBUG=1` records history acceptance or rejection in the local
host log without changing the capture path.

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

A fresh source ZIP cannot provision Maia because Git archives do not contain
submodule contents and ChessListener does not distribute an lc0 binary. It can
preserve a fully validated Maia runtime already in the marked installation.
If an earlier reinstall already removed that runtime, restore it once from your
known working checkout (or rebuild lc0 and initialize the Maia submodule), then
run `./Native/install.sh --require-maia`.

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

Release 0.9.5 intentionally does not classify a page as participant,
spectator, bot, or test play. The same capture path remains available for bot
games and local feature testing. Decide whether analysis is allowed in your
context and follow the applicable site, event, and tournament rules.

## License

Copyright (C) 2026 SeucheFracht612.

ChessListener's original source code is licensed under the GNU General Public
License, version 3 only (`GPL-3.0-only`). See [`LICENSE`](LICENSE). This program
comes with no warranty, to the extent permitted by law.

Third-party components and user-installed dependencies keep their own license
terms; the repository license does not relicense them. See
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) before distributing a build.
