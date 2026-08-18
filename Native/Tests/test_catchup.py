"""Fast-path, delayed-recovery, and exact-history reconciliation checks."""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from e2e import (
    DEFAULT_SESSION_ID,
    INITIAL,
    apply_move,
    load_frames,
    recv_response,
    send,
    send_history,
    send_snapshot,
    start_host,
    stop_host,
)


OPENING = [
    "e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6",
    "d2d3", "f8c5", "c2c3", "d7d6", "b1d2", "e8g8",
    "e1g1", "c8e6", "c4b3", "d8d7", "h2h3", "a7a6", "f1e1",
]


def board_from_fen(fen):
    cells = []
    for row in fen.split()[0].split("/"):
        for character in row:
            if character.isdigit():
                cells.extend("." * int(character))
            else:
                cells.append(character)
    board = "".join(cells)
    if len(board) != 64:
        raise AssertionError(f"bad test FEN: {fen}")
    return board


def snapshot(proc, board, sequence, *, recovery=None):
    return send_snapshot(proc, board, sequence, recovery=recovery)


def run_gap_case(gap_plies, moves_after_gap=3):
    """Drop snapshots, delay recovery, then verify continued tracking."""
    with tempfile.TemporaryDirectory(prefix="chess-listener-catchup-") as temp:
        proc = start_host(os.path.join(temp, "frames.jsonl"))
        try:
            board = INITIAL
            index = 0
            sequence = 1
            snapshot(proc, board, sequence)

            for _ in range(2):
                board = apply_move(board, OPENING[index])
                index += 1
                sequence += 1
                snapshot(proc, board, sequence)

            for _ in range(gap_plies):
                board = apply_move(board, OPENING[index])
                index += 1

            began = time.monotonic()
            sequence += 1
            fast_reply = snapshot(proc, board, sequence)
            fast_elapsed = time.monotonic() - began

            began = time.monotonic()
            sequence += 1
            recovery_reply = snapshot(proc, board, sequence, recovery=True)
            recovery_elapsed = time.monotonic() - began

            if recovery_reply["reason"] == "recovery_pending":
                recovery_reply = send_history(
                    proc, board, OPENING[:index], 1, sequence
                )

            after = []
            for _ in range(moves_after_gap):
                board = apply_move(board, OPENING[index])
                index += 1
                sequence += 1
                after.append(snapshot(proc, board, sequence)["reason"])

            return (
                fast_reply["reason"],
                recovery_reply["reason"],
                after,
                fast_elapsed,
                recovery_elapsed,
            )
        finally:
            stop_host(proc)


def assert_delayed_recovery(failures):
    for gap in (2, 3, 4, 5, 6, 8, 11):
        fast, recovered, after, fast_elapsed, recovery_elapsed = run_gap_case(gap)
        print(
            f"gap {gap:2d}: fast={fast:<15} recovery={recovered:<20} "
            f"fast {fast_elapsed * 1000:5.1f} ms, "
            f"recovery {recovery_elapsed * 1000:5.1f} ms"
        )

        if fast != "synchronising":
            failures.append(f"{gap}-ply gap entered DFS on the hot path ({fast})")
        if recovered not in {"move_recorded", "history_reconciled"}:
            failures.append(f"{gap}-ply gap never recovered ({recovered})")
        if not after or not all(reason == "move_recorded" for reason in after):
            failures.append(f"tracking failed after {gap}-ply recovery: {after}")
        if fast_elapsed > 0.20:
            failures.append(
                f"ordinary {gap}-ply mismatch blocked {fast_elapsed:.3f}s"
            )
        if recovery_elapsed > 0.20:
            failures.append(
                f"bounded {gap}-ply recovery blocked {recovery_elapsed:.3f}s"
            )

    with tempfile.TemporaryDirectory(prefix="chess-listener-catchup-") as temp:
        proc = start_host(os.path.join(temp, "frames.jsonl"))
        try:
            board = INITIAL
            sequence = 1
            snapshot(proc, board, sequence)
            index = 0
            reasons = []
            for _ in range(4):
                for _ in range(2):
                    board = apply_move(board, OPENING[index])
                    index += 1
                sequence += 1
                reasons.append(snapshot(proc, board, sequence)["reason"])
                sequence += 1
                reasons.append(
                    snapshot(proc, board, sequence, recovery=True)["reason"]
                )
        finally:
            stop_host(proc)

    print(f"four delayed 2-ply gaps -> {reasons}")
    expected = ["synchronising", "move_recorded"] * 4
    if reasons != expected:
        failures.append(f"consecutive delayed recoveries failed: {reasons}")

    # After continuity breaks, a later layout can coincidentally be one legal
    # move from the stale trusted board. It must not escape synchronising state
    # through the ordinary hot path.
    with tempfile.TemporaryDirectory(prefix="chess-listener-catchup-") as temp:
        frame_log = os.path.join(temp, "frames.jsonl")
        proc = start_host(frame_log)
        try:
            after_e4 = apply_move(INITIAL, "e2e4")
            snapshot(proc, INITIAL, 1)
            snapshot(proc, after_e4, 2)
            skipped = apply_move(after_e4, "e7e5")
            skipped = apply_move(skipped, "g1f3")
            first = snapshot(proc, skipped, 3)
            coincidental = apply_move(after_e4, "c7c5")
            second = snapshot(proc, coincidental, 4)
            recovered = snapshot(proc, coincidental, 5, recovery=True)
            time.sleep(0.05)
            sources = [
                frame.get("source") for frame in load_frames(frame_log)
                if frame.get("type") == "position"
            ]
        finally:
            stop_host(proc)

    if first.get("reason") != "synchronising" or \
            second.get("reason") != "synchronising" or \
            recovered.get("reason") != "move_recorded" or \
            not sources or sources[-1] != "inferred":
        failures.append(
            "a coincidental one-ply board escaped the continuity guard: "
            f"{first}, {second}, {recovered}"
        )


def assert_consecutive_board_fallback(failures):
    """Resume only from a proven move between two pending observations."""
    with tempfile.TemporaryDirectory(prefix="chess-listener-catchup-") as temp:
        frame_log = os.path.join(temp, "frames.jsonl")
        proc = start_host(frame_log)
        try:
            board = INITIAL
            index = 0
            sequence = 1
            snapshot(proc, board, sequence)

            # Establish a trusted position, then omit eight plies so bounded
            # recovery is guaranteed to stop short of the pending board.
            for _ in range(2):
                board = apply_move(board, OPENING[index])
                index += 1
                sequence += 1
                snapshot(proc, board, sequence)

            time.sleep(0.05)
            trusted_positions = sum(
                frame.get("type") == "position"
                for frame in load_frames(frame_log)
            )

            for _ in range(8):
                board = apply_move(board, OPENING[index])
                index += 1

            sequence += 1
            mismatch = snapshot(proc, board, sequence)
            sequence += 1
            failed_recovery = snapshot(proc, board, sequence, recovery=True)

            # An idle board and a two-ply jump are not a unique legal move and
            # must leave the trusted evaluation held in synchronising state.
            sequence += 1
            idle = snapshot(proc, board, sequence)
            for _ in range(2):
                board = apply_move(board, OPENING[index])
                index += 1
            sequence += 1
            ambiguous = snapshot(proc, board, sequence)
            time.sleep(0.05)
            held_positions = sum(
                frame.get("type") == "position"
                for frame in load_frames(frame_log)
            )

            # The next board is exactly one uniquely legal white move from
            # the latest pending observation, so inferred tracking can resume.
            board = apply_move(board, OPENING[index])
            index += 1
            sequence += 1
            resumed = snapshot(proc, board, sequence)
            time.sleep(0.08)
            inferred_sources = [
                frame.get("source") for frame in load_frames(frame_log)
                if frame.get("type") == "position"
            ]

            # Exact history remains authoritative and corrects guessed clocks
            # and other hidden state after the fallback.
            exact = send_history(
                proc, board, OPENING[:index], 1, sequence
            )
            time.sleep(0.08)
            exact_sources = [
                frame.get("source") for frame in load_frames(frame_log)
                if frame.get("type") == "position"
            ]
        finally:
            stop_host(proc)

    print(
        "consecutive-board fallback ->",
        mismatch.get("reason"), failed_recovery.get("reason"),
        idle.get("reason"), ambiguous.get("reason"),
        resumed.get("reason"), exact.get("reason"),
    )
    if mismatch.get("reason") != "synchronising" or \
            failed_recovery.get("reason") != "recovery_pending" or \
            idle.get("reason") != "synchronising" or \
            ambiguous.get("reason") != "synchronising" or \
            held_positions != trusted_positions or \
            resumed.get("reason") != "move_recorded" or \
            resumed.get("uci") != OPENING[index - 1] or \
            not inferred_sources or inferred_sources[-1] != "inferred" or \
            exact.get("reason") != "history_reconciled" or \
            not exact_sources or exact_sources[-1] != "exact":
        failures.append(
            "guarded consecutive-board fallback failed or adopted an "
            "unproven idle/ambiguous board: "
            f"{mismatch}, {failed_recovery}, {idle}, {ambiguous}, "
            f"{resumed}, {exact}; position frames "
            f"{trusted_positions}->{held_positions}"
        )


def assert_history_replay(failures):
    # SAN covers castling, capture, check/annotation stripping, and origin
    # disambiguation. The same final board is independently proven by UCI.
    san = [
        "e4!", "e5", "Nf3", "Nc6", "Bc4", "Nf6", "d3", "Bc5",
        "c3", "d6", "Nbd2", "0-0", "O-O",
    ]
    uci = OPENING[:13]
    board = INITIAL
    for move in uci:
        board = apply_move(board, move)

    for notation, moves in (("san", san), ("uci", uci)):
        with tempfile.TemporaryDirectory(prefix="chess-listener-history-") as temp:
            frame_log = os.path.join(temp, "frames.jsonl")
            proc = start_host(frame_log)
            try:
                waiting = snapshot(proc, board, 1)
                reply = send_history(
                    proc, board, moves, 1, 1, notation=notation
                )
            finally:
                stop_host(proc)

            records = [
                frame for frame in load_frames(frame_log)
                if frame.get("type") == "game_record"
            ]

        print(f"{notation.upper()} replay -> {reply.get('reason')}")
        if waiting.get("reason") != "waiting_for_second_frame" or \
                reply.get("reason") != "history_reconciled":
            failures.append(f"{notation} history was not accepted: {reply}")
        if reply.get("uci") != "e1g1":
            failures.append(f"{notation} history lost final castling move: {reply}")
        if not records or records[-1].get("uci_moves") != "|".join(uci) or \
                records[-1].get("move_count") != len(uci):
            failures.append(
                f"{notation} history did not publish canonical review record: "
                f"{records[-1] if records else None}"
            )

    ep_fen = "rnbqkbnr/1pp1pppp/p2P4/8/8/8/PPPP1PPP/RNBQKBNR b KQkq - 0 3"
    ep_board = board_from_fen(ep_fen)
    ep_san = ["e4", "a6", "e5", "d5", "exd6 e.p."]
    with tempfile.TemporaryDirectory(prefix="chess-listener-history-") as temp:
        proc = start_host(os.path.join(temp, "frames.jsonl"))
        try:
            snapshot(proc, ep_board, 1)
            reply = send_history(proc, ep_board, ep_san, 1, 1, notation="san")
        finally:
            stop_host(proc)
    print(f"SAN en-passant -> {reply.get('reason')}")
    if reply.get("reason") != "history_reconciled" or \
            reply.get("fen") != ep_fen:
        failures.append(f"SAN en-passant replay failed: {reply}")

    promotion_start = "8/P6k/8/8/8/8/7p/K7 w - - 0 1"
    promotion_fen = "Q7/7k/8/8/8/8/8/K6q w - - 0 2"
    promotion_board = board_from_fen(promotion_fen)
    with tempfile.TemporaryDirectory(prefix="chess-listener-history-") as temp:
        proc = start_host(os.path.join(temp, "frames.jsonl"))
        try:
            snapshot(proc, promotion_board, 1)
            reply = send_history(
                proc,
                promotion_board,
                ["a8Q", "h1=Q+"],
                1,
                1,
                notation="san",
                initial_fen=promotion_start,
            )
        finally:
            stop_host(proc)
    print(f"SAN promotion -> {reply.get('reason')}")
    if reply.get("reason") != "history_reconciled" or \
            reply.get("fen") != promotion_fen:
        failures.append(f"SAN promotion replay failed: {reply}")

    # Regression from the fast bot game that originally remained several
    # plies behind. These are the exact tokens emitted by the paired move-row
    # parser, including the queen capture and the final pawn capture.
    screenshot_san = [
        "e4", "Nf6", "Qg4", "Nxg4", "Nf3",
        "d5", "d4", "Ne3", "Nc3", "dxe4",
    ]
    screenshot_uci = [
        "e2e4", "g8f6", "d1g4", "f6g4", "g1f3",
        "d7d5", "d2d4", "g4e3", "b1c3", "d5e4",
    ]
    screenshot_board = INITIAL
    for move in screenshot_uci:
        screenshot_board = apply_move(screenshot_board, move)
    with tempfile.TemporaryDirectory(prefix="chess-listener-history-") as temp:
        proc = start_host(os.path.join(temp, "frames.jsonl"))
        try:
            snapshot(proc, screenshot_board, 1)
            reply = send_history(
                proc, screenshot_board, screenshot_san, 1, 1,
                notation="san",
            )
        finally:
            stop_host(proc)
    print(f"screenshot SAN replay -> {reply.get('reason')}")
    if reply.get("reason") != "history_reconciled" or \
            reply.get("uci") != "d5e4":
        failures.append(f"screenshot SAN replay failed: {reply}")


def assert_history_transactionality(failures):
    initial_fen = "4k3/8/8/8/8/2N3N1/8/4K3 w - - 0 1"
    final_fen = "4k3/8/8/8/4N3/6N1/8/4K3 b - - 1 1"
    board = board_from_fen(final_fen)

    with tempfile.TemporaryDirectory(prefix="chess-listener-history-") as temp:
        proc = start_host(os.path.join(temp, "frames.jsonl"))
        try:
            snapshot(proc, board, 1)
            ambiguous = send_history(
                proc, board, ["Ne4"], 1, 1, notation="san",
                initial_fen=initial_fen,
            )
            corrected = send_history(
                proc, board, ["Nce4"], 1, 1, notation="san",
                initial_fen=initial_fen,
            )
            stale = send_history(
                proc, board, ["Nce4"], 1, 1, notation="san",
                initial_fen=initial_fen,
            )
            wrong_snapshot = send_history(
                proc, board, ["Nce4"], 2, 0, notation="san",
                initial_fen=initial_fen,
            )
            wrong_session = send_history(
                proc, board, ["Nce4"], 2, 1, notation="san",
                session_id="different-session", initial_fen=initial_fen,
            )
        finally:
            stop_host(proc)

    print(
        "history rejection ->", ambiguous.get("reason"),
        corrected.get("reason"), stale.get("reason")
    )
    expected = (
        ambiguous.get("reason") == "history_replay_failed" and
        corrected.get("reason") == "history_reconciled" and
        stale.get("reason") == "stale_history" and
        wrong_snapshot.get("reason") == "history_snapshot_mismatch" and
        wrong_session.get("reason") == "session_mismatch"
    )
    if not expected:
        failures.append(
            "history rejection was not transactional: "
            f"{ambiguous}, {corrected}, {stale}, {wrong_snapshot}, {wrong_session}"
        )


def assert_exact_revision_behavior(failures):
    with tempfile.TemporaryDirectory(prefix="chess-listener-history-") as temp:
        frame_log = os.path.join(temp, "frames.jsonl")
        proc = start_host(frame_log)
        try:
            snapshot(proc, INITIAL, 1)
            board = apply_move(INITIAL, "e2e4")
            snapshot(proc, board, 2)
            time.sleep(0.08)
            before_count = sum(
                frame.get("type") == "position" for frame in load_frames(frame_log)
            )
            confirmed = send_history(proc, board, ["e2e4"], 1, 2)
            time.sleep(0.08)
            after_count = sum(
                frame.get("type") == "position" for frame in load_frames(frame_log)
            )
        finally:
            stop_host(proc)

    if confirmed.get("reason") != "history_confirmed" or \
            before_count != after_count:
        failures.append(
            f"exact confirmation restarted analysis: {confirmed}, "
            f"positions {before_count}->{after_count}"
        )

    with tempfile.TemporaryDirectory(prefix="chess-listener-history-") as temp:
        frame_log = os.path.join(temp, "frames.jsonl")
        proc = start_host(frame_log)
        try:
            board = apply_move(INITIAL, "e2e4")
            manual = (
                "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/"
                "RNBQKBNR b KQkq - 12 7"
            )
            send(proc, {
                "type": "session_command",
                "session_id": DEFAULT_SESSION_ID,
                "command": "set_fen",
                "payload": manual,
            })
            recv_response(proc)
            snapshot(proc, board, 1)
            before_count = sum(
                frame.get("type") == "position" for frame in load_frames(frame_log)
            )
            reconciled = send_history(proc, board, ["e2e4"], 1, 1)
            time.sleep(0.08)
            after_count = sum(
                frame.get("type") == "position" for frame in load_frames(frame_log)
            )
        finally:
            stop_host(proc)

    expected_fen = (
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/"
        "RNBQKBNR b KQkq e3 0 1"
    )
    if reconciled.get("reason") != "history_reconciled" or \
            reconciled.get("fen") != expected_fen or \
            after_count <= before_count:
        failures.append(
            f"hidden state was not corrected: {reconciled}, "
            f"positions {before_count}->{after_count}"
        )


def main():
    failures = []
    assert_delayed_recovery(failures)
    assert_consecutive_board_fallback(failures)
    assert_history_replay(failures)
    assert_history_transactionality(failures)
    assert_exact_revision_behavior(failures)

    print()
    if failures:
        for failure in failures:
            print("FAIL:", failure)
        return 1

    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
