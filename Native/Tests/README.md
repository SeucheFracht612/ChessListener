# ChessListener tests

Run the complete suite from the repository root:

    make test

The test target forcibly recompiles the native host before running anything,
so old object files or a checked-in executable cannot hide a broken checkout.
It also checks Python, JavaScript, and shell syntax and runs the extension's
Node-based protocol tests when present.

`test_install_lifecycle.py` creates an isolated marked runtime with a custom
install prefix and Firefox manifest directory. It verifies installed
diagnostics, update delegation/path persistence, uninstall dry-run, refusal to
touch an unmarked directory, and removal of only the recognized runtime and
manifest. It uses fake local dependencies and never changes the real user
installation.

`e2e.py` drives `chess-listener-host` from both ends at once: it speaks the
browser's length-prefixed native-messaging protocol (including the version
handshake) on one side, while `stub_overlay.py` stands in for `overlay.py` on
the other. `fake_uci_engine.py` is a deterministic UCI engine, so neither a
system Stockfish installation nor network access is required.

The integration checks fail if the host stalls the browser while analysis is
running, drops board frames during a burst, publishes stale analysis as final,
or fails to apply a live settings change.

What it asserts:

* ordinary single-ply e2e messages are answered in under 250 ms (normally <5)
* skipped-snapshot catch-up completes within its separate 2-second bound
* every position reaches the overlay as a board frame, none are lost in a burst
* evaluations are published, and the last one belongs to the last position
* a live `SET` sent mid-session is acknowledged

Knobs:

* `GAP` - optional seconds between moves; the default of `0` creates a burst.
* `CHESSLISTENER_TEST_TIMEOUT` - total deadline for asynchronous overlay state.
* `CHESSLISTENER_TEST_MESSAGE_TIMEOUT` - deadline for one native response.

For a debug build, or to build and run the integration checks under
AddressSanitizer and UndefinedBehaviorSanitizer:

    make debug
    make asan

Maia is intentionally disabled in these tests. The host exercises its
Stockfish-only fallback while avoiding the large lc0 binary and weight files.
