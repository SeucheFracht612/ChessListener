"""Skipped-snapshot recovery checks for the native position tracker."""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from e2e import INITIAL, apply_move, recv, send, start_host, stop_host


OPENING = [
    "e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6",
    "d2d3", "f8c5", "c2c3", "d7d6", "b1d2", "e8g8",
    "e1g1", "c8e6", "c4b3", "d8d7", "h2h3", "a7a6", "f1e1",
]


def snapshot(proc, board):
    send(proc, {
        "type": "position_snapshot",
        "board": board,
        "visually_flipped": False,
    })
    return recv(proc)


def run_case(gap_plies, moves_after_gap=4):
    """Drop ``gap_plies`` snapshots and verify subsequent tracking.

    Returns ``(reason_at_gap, reasons_after_gap, seconds_at_gap)``.
    """
    with tempfile.TemporaryDirectory(prefix="chess-listener-catchup-") as temp:
        proc = start_host(os.path.join(temp, "frames.jsonl"))
        try:
            board = INITIAL
            index = 0
            snapshot(proc, board)

            # Two clean moves first, so the tracker is definitely locked on.
            for _ in range(2):
                board = apply_move(board, OPENING[index])
                index += 1
                snapshot(proc, board)

            # Apply the gap, but send only its final board.
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

            return gap_reason, after, elapsed
        finally:
            stop_host(proc)


def main():
    failures = []

    # Gaps inside the reconstruction limit must be recovered exactly.
    for gap in (2, 3, 4, 5, 6):
        reason, after, elapsed = run_case(gap)
        recovered = all(item == "move_recorded" for item in after)
        print(
            f"gap of {gap} plies -> {reason:<17} "
            f"then {'all tracked' if recovered else after}  "
            f"({elapsed * 1000:.0f} ms)"
        )

        if reason != "move_recorded":
            failures.append(f"{gap}-ply gap was not reconstructed ({reason})")
        if not recovered:
            failures.append(f"did not track cleanly after a {gap}-ply gap")
        if elapsed > 2.0:
            failures.append(f"{gap}-ply search took {elapsed:.1f}s")

    # Past the search cap, exact reconstruction is optional, but the tracker
    # must resynchronise and continue instead of freezing permanently.
    for gap in (8, 11):
        reason, after, elapsed = run_case(gap)
        print(f"gap of {gap} plies -> {reason:<17} then {after}")

        if reason == "move_recorded":
            recovered_from = 0
        elif reason == "resynchronising":
            recovered_from = 1
        else:
            failures.append(f"{gap}-ply gap gave an unexpected {reason}")
            continue

        tail = after[recovered_from:]
        if not tail or not all(item == "move_recorded" for item in tail):
            failures.append(f"never recovered after a {gap}-ply gap: {after}")

        if elapsed > 2.0:
            failures.append(f"{gap}-ply recovery took {elapsed:.1f}s")

    # Back-to-back gaps model a long premove chain.
    with tempfile.TemporaryDirectory(prefix="chess-listener-catchup-") as temp:
        proc = start_host(os.path.join(temp, "frames.jsonl"))
        try:
            board = INITIAL
            snapshot(proc, board)
            index = 0
            reasons = []

            for _ in range(4):
                for _ in range(2):
                    board = apply_move(board, OPENING[index])
                    index += 1
                reasons.append(snapshot(proc, board)["reason"])
        finally:
            stop_host(proc)

    print(f"four 2-ply gaps in a row -> {reasons}")
    if not all(reason == "move_recorded" for reason in reasons):
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
