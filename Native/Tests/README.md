# ChessListener tests

Run the complete suite from the repository root:

    make test

The test target forcibly recompiles the native host before running anything,
so old object files or a checked-in executable cannot hide a broken checkout.
It also checks Python, JavaScript, and shell syntax and runs every extension
Node test under `Extension/Tests/`.

The extension fixtures cover single-owner tab arbitration, explicit switching,
session-storage restoration after a Firefox event-page restart, sticky stop and
dismissal, one crash retry, stale route/capture rejection, forced re-read,
orientation changes, pointer cancellation, game-over lifecycle, popup action
errors, fast confirming reads, delayed recovery, complete-history extraction,
history rewrites/takebacks, reconnect ordering, and recovery-button state.

`test_install_lifecycle.py` creates an isolated marked runtime with a custom
install prefix and Firefox manifest directory. It verifies installed
diagnostics, update delegation/path persistence, uninstall dry-run, refusal to
touch unmarked or symlinked directories, and removal of only the recognized
runtime and manifest. It also performs ZIP-style reinstalls with a fake Maia
runtime, proving that validated lc0, all nine nets, and local libraries are
preserved byte-for-byte while incomplete payloads are removed. It uses fake
local dependencies and never changes the real user installation.

`e2e.py` drives `chess-listener-host` from both ends at once: it speaks the
browser's length-prefixed native-messaging protocol 4 (including version and
capability negotiation) on one side, while `stub_overlay.py` stands in for
`overlay.py` on the other. It also exercises session isolation and monotonic
snapshots, exact/rejected FEN recovery, initial-position repetition, forced
refresh, orientation-only updates, scoped UI commands, validated UCI/SAN
history, bounded delayed catch-up, state provenance, intentional dismissal,
and the protocol-4 Analysis Lab lifecycle. Analysis Lab coverage includes a
PV-seeded branch, alternatives, goto/live/resume, live-board updates that do
not overwrite the selected branch, exact analysis target identities, stale
branch/base/node rejection, transactional start rejection, and session-scoped
destruction. Castling, en passant, and underpromotion exercise the native move
validator. Its settings checks cover Maia Off and explorer budgets for
same-as-live (`-1`), continuous (`0`), and both clamps. UCI score bounds are
checked through parsing, White-POV serialization (including black-to-move bound
inversion), and explanation suppression.
`fake_uci_engine.py` is deterministic, so neither a system
Stockfish installation nor network access is required.

`test_overlay.py` runs focused Analysis Lab UI interactions with Qt's offscreen
platform when PyQt6 is installed. It cleanly reports a skip when PyQt6 is not
available, so headless/minimal build environments can still run the suite. It
also covers imported records, zero-move FEN analysis, and standalone local
review mode. Its Saved Studies fixture captures an Analysis Lab tree, persists
annotations/evaluation snapshots, adds and reopens branches, filters the local
library, and runs generation-scoped local analysis. `test_study.py` validates
tree legality, bounds, paths, special starting positions, recursive PGN side
variations, and transactional corruption rejection. `test_pgn_import.py`
verifies legal SAN-to-UCI replay, comments,
nested variations, NAGs, metadata, custom FENs, castling, figurines,
promotion, export round trips, and transactional rejection of malformed or
multi-game input.

The integration checks fail if the host stalls the browser while analysis is
running, drops board frames during a burst, publishes stale analysis as final,
or fails to apply a live settings change.

What it asserts:

* ordinary single-ply e2e messages are answered in under 250 ms (normally <5)
* ordinary move handling never enters the multi-ply recovery search
* delayed catch-up obeys its separate 50 ms wall-clock and shared-node budget
* every position reaches the overlay as a board frame, none are lost in a burst
* evaluations are published, and the last one belongs to the last position
* an artificially slow Maia cannot delay the first Stockfish result
* a live `SET` sent mid-session is acknowledged
* explorer analysis never crosses a newer branch/node/live target
* live snapshots continue at normal latency while a branch is selected

Knobs:

* `GAP` - optional seconds between moves; the default of `0` creates a burst.
* `CHESSLISTENER_TEST_TIMEOUT` - total deadline for asynchronous overlay state.
* `CHESSLISTENER_TEST_MESSAGE_TIMEOUT` - deadline for one native response.

For a debug build, or to build and run the integration checks under
AddressSanitizer and UndefinedBehaviorSanitizer:

    make debug
    make asan

The independent engine lanes also have a focused optional race check on
platforms where ThreadSanitizer is available:

    make tsan

Most integration scenarios intentionally disable Maia. The engine-lane test
uses a tiny deterministic Maia-compatible process with an artificial delay;
it never needs the real lc0 binary or weight files.
