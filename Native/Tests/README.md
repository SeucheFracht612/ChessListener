# Native host tests

`e2e.py` drives `chess-listener-host` from both ends at once: it speaks the
browser's length-prefixed native-messaging protocol on one side, and
`stub_overlay.py` stands in for `overlay.py` on the other.

It exists because of one specific regression class: the host used to run the
engine synchronously inside its message loop, so a fast sequence of moves
stalled everything. These checks fail if that ever comes back.

    # needs a stockfish binary on the system
    GAP=0.04 BUDGET=600 python3 Tests/e2e.py

What it asserts:

* every position message is answered in under 150 ms (it is normally under 5)
* every position reaches the overlay as a board frame, none are lost in a burst
* evaluations are published, and the last one belongs to the last position
* a live `SET` sent mid-session is acknowledged

Knobs:

* `GAP` - seconds between moves. `0.008` is faster than any real premove burst.
* `BUDGET` - Stockfish milliseconds per position. `0` means Continuous.

Worth running under ThreadSanitizer after touching the threading:

    make clean
    make CFLAGS="-std=c11 -O1 -g -Wall -Wextra -pthread -fsanitize=thread" \
         LDFLAGS="-pthread -fsanitize=thread"
    TSAN_OPTIONS="halt_on_error=0 log_path=/tmp/tsan" python3 Tests/e2e.py
    make clean && make          # do not ship the sanitizer build

Maia is not covered: the sandbox this was written in has no lc0 build, so
`CHESSLISTENER_LC0` is pointed at a nonexistent path and the host exercises the
Stockfish-only path.
