"""Skipped-snapshot recovery.

content.js debounces for 120 ms and double-reads for another 75 ms before it
sends, so anything faster than roughly a move every 200 ms arrives as a single
board frame that is several plies ahead. The host used to match exactly one
legal move against the new board, and on failure it *kept its stale position* --
so one skipped snapshot made every later frame fail too and the overlay stayed
frozen on an old position for the rest of the game.

Every case here is a gap the old code could not survive.

    python3 Tests/test_catchup.py
"""

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from e2e import HOST, INITIAL, apply_move, recv, send

HERE = os.path.dirname(os.path.abspath(__file__))

OPENING = [
    "e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6",
    "d2d3", "f8c5", "c2c3", "d7d6", "b1d2", "e8g8",
    "e1g1", "c8e6", "c4b3", "d8d7", "h2h3", "a7a6", "f1e1",
]


def start_host():
    environment = dict(os.environ)
    environment.update(
        CHESSLISTENER_OVERLAY=os.path.join(HERE, "stub_overlay.py"),
        CHESSLISTENER_STOCKFISH="/usr/games/stockfish",
        CHESSLISTENER_LC0="/nonexistent",
        CHESSLISTENER_STUB_LOG=os.path.join(HERE, "catchup_frames.jsonl"),
    )
    open(environment["CHESSLISTENER_STUB_LOG"], "w").close()

    return subprocess.Popen(
        [HOST],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=environment,
    )


def snapshot(proc, board):
    send(proc, {
        "type": "position_snapshot",
        "board": board,
        "visually_flipped": False,
    })
    return recv(proc)


def run_case(gap_plies, moves_after_gap=4):
    """Play a few moves, drop `gap_plies` worth of snapshots, then continue.

    Returns (reason_at_gap, [reasons after the gap], seconds spent on the gap).
    """
    proc = start_host()
    board = INITIAL
    index = 0

    snapshot(proc, board)

    # Two clean moves first, so the tracker is definitely locked on.
    for _ in range(2):
        board = apply_move(board, OPENING[index])
        index += 1
        snapshot(proc, board)

    # The gap: apply the moves but send only the final board.
    for _ in range(gap_plies):
        board = apply_move(board, OPENING[index])
        index += 1

    began = time.monotonic()
    gap_reason = snapshot(proc, board)["reason"]
    elapsed = time.monotonic() - began

    after = []

    for _ in range(moves_after_gap):
        board = apply_move(board, OPENING[index])
        index += 1
        after.append(snapshot(proc, board)["reason"])

    proc.stdin.close()
    proc.wait(timeout=10)
    return gap_reason, after, elapsed


def main():
    failures = []

    # A gap the search can reconstruct exactly.
    for gap in (2, 3, 4, 5, 6):
        reason, after, elapsed = run_case(gap)
        recovered = all(r == "move_recorded" for r in after)
        print(f"gap of {gap} plies -> {reason:<17} "
              f"then {'all tracked' if recovered else after}  "
              f"({elapsed * 1000:.0f} ms)")

        if reason != "move_recorded":
            failures.append(f"{gap}-ply gap was not reconstructed ({reason})")

        if not recovered:
            failures.append(f"did not track cleanly after a {gap}-ply gap")

        if elapsed > 2.0:
            failures.append(f"{gap}-ply search took {elapsed:.1f}s")

    # A gap past the search cap. Exact reconstruction is impossible, so the
    # requirement is only that it resynchronises and keeps working -- the old
    # code went dead here permanently.
    for gap in (8, 11):
        reason, after, elapsed = run_case(gap)
        print(f"gap of {gap} plies -> {reason:<17} then {after}")

        if reason == "move_recorded":
            # Deep gaps can still be reachable by luck; that is fine.
            recovered_from = 0
        elif reason == "resynchronising":
            # One frame of downtime is the documented cost, then it must track.
            recovered_from = 1
        else:
            failures.append(f"{gap}-ply gap gave an unexpected {reason}")
            continue

        tail = after[recovered_from:]

        if not tail or not all(r == "move_recorded" for r in tail):
            failures.append(
                f"never recovered after a {gap}-ply gap: {after}"
            )

    # Back-to-back gaps, which is what a long premove chain actually looks like.
    proc = start_host()
    board = INITIAL
    snapshot(proc, board)
    index = 0
    reasons = []

    for _ in range(4):
        for _ in range(2):
            board = apply_move(board, OPENING[index])
            index += 1

        reasons.append(snapshot(proc, board)["reason"])

    proc.stdin.close()
    proc.wait(timeout=10)
    print(f"four 2-ply gaps in a row -> {reasons}")

    if not all(r == "move_recorded" for r in reasons):
        failures.append(f"consecutive gaps not handled: {reasons}")

    print()

    if failures:
        for failure in failures:
            print("FAIL:", failure)
        return 1

    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
