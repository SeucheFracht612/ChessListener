"""End-to-end check of the native host with deterministic test doubles.

The test speaks Firefox's length-prefixed native-messaging protocol on one
side. ``stub_overlay.py`` speaks the overlay control/data protocol on the
other, while ``fake_uci_engine.py`` supplies deterministic UCI output.
"""

import json
import os
import select
import struct
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
NATIVE = os.path.dirname(HERE)

HOST = os.path.abspath(
    os.environ.get("CHESSLISTENER_HOST", os.path.join(NATIVE, "chess-listener-host"))
)
STUB = os.path.join(HERE, "stub_overlay.py")
FAKE_UCI = os.path.abspath(
    os.environ.get("CHESSLISTENER_FAKE_UCI", os.path.join(HERE, "fake_uci_engine.py"))
)

PROTOCOL_VERSION = 4
HOST_VERSION = "0.9.5"
DEFAULT_SESSION_ID = "e2e-session"
MESSAGE_TIMEOUT = float(os.environ.get("CHESSLISTENER_TEST_MESSAGE_TIMEOUT", "3"))
TEST_TIMEOUT = float(os.environ.get("CHESSLISTENER_TEST_TIMEOUT", "6"))
GAP_SECONDS = float(os.environ.get("GAP", "0"))

INITIAL = (
    "rnbqkbnr"
    "pppppppp"
    "........"
    "........"
    "........"
    "........"
    "PPPPPPPP"
    "RNBQKBNR"
)

MOVES = [
    "e2e4", "e7e5", "g1f3", "b8c6", "f1c4", "g8f6",
    "d2d3", "f8c5", "c2c3", "d7d6", "b1d2", "e8g8",
]


def index_of(square):
    return (8 - int(square[1])) * 8 + (ord(square[0]) - ord("a"))


def apply_move(board, move):
    cells = list(board)
    origin, target = index_of(move[0:2]), index_of(move[2:4])
    piece = cells[origin]
    cells[origin] = "."
    cells[target] = piece

    # Only castling needs special handling for this test's move list.
    if piece in "Kk" and abs(origin - target) == 2:
        if target > origin:
            cells[index_of("h1" if piece == "K" else "h8")] = "."
            cells[target - 1] = "R" if piece == "K" else "r"
        else:
            cells[index_of("a1" if piece == "K" else "a8")] = "."
            cells[target + 1] = "R" if piece == "K" else "r"

    return "".join(cells)


def send(proc, payload):
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    proc.stdin.write(struct.pack("<I", len(raw)) + raw)
    proc.stdin.flush()


def read_exact(proc, length, timeout=MESSAGE_TIMEOUT):
    """Read exactly ``length`` bytes, with a total deadline.

    A blocking ``file.read`` let a broken host hang CI forever. Reading the
    pipe descriptor directly makes a missing or truncated response a clear
    test failure instead.
    """
    deadline = time.monotonic() + timeout
    chunks = []
    remaining = length
    descriptor = proc.stdout.fileno()

    while remaining:
        wait = deadline - time.monotonic()
        if wait <= 0:
            raise TimeoutError(f"host did not provide {length} response bytes")

        ready, _, _ = select.select([descriptor], [], [], wait)
        if not ready:
            raise TimeoutError(f"host response timed out after {timeout:.1f}s")

        chunk = os.read(descriptor, remaining)
        if not chunk:
            raise EOFError(
                f"host closed stdout after {length - remaining}/{length} bytes"
            )

        chunks.append(chunk)
        remaining -= len(chunk)

    return b"".join(chunks)


def recv(proc, timeout=MESSAGE_TIMEOUT):
    header = read_exact(proc, 4, timeout)
    length = struct.unpack("<I", header)[0]
    if length == 0 or length > 1024 * 1024:
        raise ValueError(f"invalid native-message response length: {length}")
    return json.loads(read_exact(proc, length, timeout).decode("utf-8"))


def recv_response(proc, timeout=MESSAGE_TIMEOUT, async_messages=None):
    """Receive the next request response, skipping asynchronous UI events."""
    deadline = time.monotonic() + timeout

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("host did not provide a request response")
        message = recv(proc, remaining)
        if message.get("type") in {"overlay_command", "overlay_event"}:
            if async_messages is not None:
                async_messages.append(message)
        else:
            return message


def handshake(proc):
    send(proc, {
        "type": "hello",
        "protocol_version": PROTOCOL_VERSION,
        "extension_version": "0.9.5-test",
    })
    reply = recv(proc)

    if reply.get("type") != "hello" or reply.get("ok") is not True:
        raise AssertionError(f"native handshake was rejected: {reply}")
    if reply.get("protocol_version") != PROTOCOL_VERSION:
        raise AssertionError(f"native handshake chose wrong protocol: {reply}")
    if not isinstance(reply.get("capabilities"), list):
        raise AssertionError(f"native handshake has no capabilities: {reply}")
    required = {
        "session_v2", "state_override", "streaming_analysis", "last_move",
        "history_reconciliation", "state_revision", "state_source",
        "analysis_lab", "analysis_target",
    }
    if not required.issubset(reply["capabilities"]):
        raise AssertionError(f"native handshake misses capabilities: {reply}")

    return reply


def start_session(proc, session_id=DEFAULT_SESSION_ID, game_key="e2e-game"):
    send(proc, {
        "type": "session_start",
        "session_id": session_id,
        "page_instance_id": "e2e-page",
        "route_generation": 1,
        "game_key": game_key,
        "url": "https://www.chess.com/game/live/e2e",
        "trigger": "test",
    })
    reply = recv_response(proc)
    if reply.get("reason") != "session_started":
        raise AssertionError(f"session start was rejected: {reply}")
    return reply


def send_snapshot(
    proc,
    board,
    snapshot_seq,
    *,
    session_id=DEFAULT_SESSION_ID,
    visually_flipped=False,
    async_messages=None,
    force=None,
    recovery=None,
):
    payload = {
        "type": "position_snapshot",
        "session_id": session_id,
        "snapshot_seq": snapshot_seq,
        "board": board,
        "visually_flipped": visually_flipped,
    }
    if force is not None:
        payload["force"] = force
    if recovery is not None:
        payload["recovery"] = recovery
    send(proc, payload)
    return recv_response(proc, async_messages=async_messages)


def send_history(
    proc,
    displayed_board,
    history_moves,
    history_seq,
    snapshot_seq,
    *,
    notation="uci",
    session_id=DEFAULT_SESSION_ID,
    initial_fen=None,
    history_complete=True,
    game_result=None,
):
    payload = {
        "type": "history_reconcile",
        "session_id": session_id,
        "history_seq": history_seq,
        "snapshot_seq": snapshot_seq,
        "displayed_board": displayed_board,
        "history_notation": notation,
        "history_moves": "|".join(history_moves),
        "history_complete": history_complete,
        "captured_at": int(time.time() * 1000),
    }
    if initial_fen is not None:
        payload["initial_fen"] = initial_fen
    if game_result is not None:
        payload["game_result"] = game_result
    send(proc, payload)
    return recv_response(proc)


def host_environment(
    frame_log,
    *,
    set_after_positions="0",
    overlay_protocol=PROTOCOL_VERSION,
    controls="",
    controls_after_positions="0",
    start_settings="",
    set_payload="",
):
    environment = dict(os.environ)
    environment.update(
        CHESSLISTENER_OVERLAY=STUB,
        CHESSLISTENER_STOCKFISH=FAKE_UCI,
        CHESSLISTENER_LC0="/nonexistent/chess-listener-lc0",
        CHESSLISTENER_MAIA_NET="/nonexistent/chess-listener-maia.pb.gz",
        CHESSLISTENER_STUB_LOG=frame_log,
        CHESSLISTENER_STUB_SET_AFTER_POSITIONS=str(set_after_positions),
        CHESSLISTENER_STUB_PROTOCOL=str(overlay_protocol),
        CHESSLISTENER_STUB_CONTROLS=controls,
        CHESSLISTENER_STUB_CONTROLS_AFTER_POSITIONS=str(
            controls_after_positions
        ),
        CHESSLISTENER_STUB_START_SETTINGS=start_settings,
        CHESSLISTENER_STUB_SET_PAYLOAD=(
            set_payload or
            "budget=90 explore_budget=-1 maia=1900 threads=1 multipv=2"
        ),
        BUDGET="100",
    )
    return environment


def launch_host(
    frame_log,
    *,
    set_after_positions="0",
    overlay_protocol=PROTOCOL_VERSION,
    controls="",
    controls_after_positions="0",
    start_settings="",
    set_payload="",
):
    for path, description in ((HOST, "native host"), (FAKE_UCI, "fake UCI engine")):
        if not os.path.isfile(path) or not os.access(path, os.X_OK):
            raise RuntimeError(f"{description} is not executable: {path}")

    return subprocess.Popen(
        [HOST],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=host_environment(
            frame_log,
            set_after_positions=set_after_positions,
            overlay_protocol=overlay_protocol,
            controls=controls,
            controls_after_positions=controls_after_positions,
            start_settings=start_settings,
            set_payload=set_payload,
        ),
    )


def start_host(
    frame_log,
    *,
    set_after_positions="0",
    session_id=DEFAULT_SESSION_ID,
    controls="",
    controls_after_positions="0",
    start_settings="",
    set_payload="",
):
    proc = launch_host(
        frame_log,
        set_after_positions=set_after_positions,
        controls=controls,
        controls_after_positions=controls_after_positions,
        start_settings=start_settings,
        set_payload=set_payload,
    )
    handshake(proc)
    if session_id is not None:
        start_session(proc, session_id)
    return proc


def assert_protocol_mismatch_rejected(frame_log):
    """An incompatible extension must be rejected before UI/engine startup."""
    proc = launch_host(frame_log)
    send(proc, {
        "type": "hello",
        "protocol_version": PROTOCOL_VERSION + 100,
        "extension_version": "incompatible-test",
    })
    reply = recv(proc)

    if reply.get("ok") is not False or reply.get("reason") != "incompatible_protocol":
        proc.kill()
        proc.wait(timeout=2)
        raise AssertionError(f"incompatible protocol was not rejected: {reply}")

    if proc.stdin is not None and not proc.stdin.closed:
        proc.stdin.close()
    proc.wait(timeout=2)

    if proc.returncode == 0:
        raise AssertionError("host reported success after incompatible protocol")


def assert_overlay_protocol_mismatch_reported(frame_log):
    """A stale overlay must produce a browser-visible error before exit."""
    proc = launch_host(
        frame_log, overlay_protocol=PROTOCOL_VERSION + 100)
    handshake(proc)
    reply = recv(proc)

    expected = {
        "type": "error",
        "ok": False,
        "reason": "incompatible_overlay_protocol",
        "protocol_version": PROTOCOL_VERSION,
        "host_version": HOST_VERSION,
    }

    if reply != expected:
        proc.kill()
        proc.wait(timeout=2)
        raise AssertionError(
            f"overlay protocol failure was not reported clearly: {reply}")

    if proc.stdin is not None and not proc.stdin.closed:
        proc.stdin.close()
    proc.wait(timeout=2)

    if proc.returncode == 0:
        raise AssertionError("host reported success after overlay mismatch")


def stop_host(proc):
    if proc.stdin is not None and not proc.stdin.closed:
        proc.stdin.close()

    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        raise AssertionError("native host did not shut down after browser EOF")

    if proc.returncode != 0:
        raise AssertionError(f"native host exited with status {proc.returncode}")


def load_frames(path):
    try:
        with open(path, encoding="utf-8") as stream:
            lines = stream.readlines()
    except FileNotFoundError:
        return []

    frames = []
    for line in lines:
        try:
            frames.append(json.loads(line))
        except json.JSONDecodeError:
            # The writer may be between write() and flush(); retry on the next
            # poll rather than treating a momentary partial line as a failure.
            continue
    return frames


def wait_for_frames(path, predicate, timeout=TEST_TIMEOUT):
    deadline = time.monotonic() + timeout
    frames = []

    while time.monotonic() < deadline:
        frames = load_frames(path)
        if predicate(frames):
            return frames
        time.sleep(0.01)

    raise TimeoutError(
        f"overlay condition was not reached within {timeout:.1f}s; "
        f"captured {len(frames)} frames"
    )


def assert_session_state_machine(frame_log):
    proc = start_host(frame_log, session_id=None)
    try:
        missing = send_snapshot(proc, INITIAL, 1, session_id="alpha")
        if missing.get("reason") != "session_required":
            raise AssertionError(f"snapshot before session was accepted: {missing}")

        start_session(proc, "alpha", "game-alpha")
        initial = send_snapshot(proc, INITIAL, 1, session_id="alpha")
        if initial.get("reason") != "game_started":
            raise AssertionError(f"initial alpha snapshot failed: {initial}")

        stale = send_snapshot(proc, INITIAL, 1, session_id="alpha")
        if stale.get("reason") != "stale_snapshot":
            raise AssertionError(f"nonmonotonic sequence was accepted: {stale}")

        after_e4 = apply_move(INITIAL, "e2e4")
        mismatch = send_snapshot(proc, after_e4, 2, session_id="old-alpha")
        if mismatch.get("reason") != "session_mismatch":
            raise AssertionError(f"mismatched session was accepted: {mismatch}")

        recorded = send_snapshot(proc, after_e4, 2, session_id="alpha")
        if recorded.get("reason") != "move_recorded":
            raise AssertionError(f"alpha state was damaged by rejection: {recorded}")

        # Starting another session must reset both chess state and sequence.
        start_session(proc, "beta", "game-beta")
        old = send_snapshot(proc, after_e4, 3, session_id="alpha")
        if old.get("reason") != "session_mismatch":
            raise AssertionError(f"stale alpha frame crossed into beta: {old}")

        beta = send_snapshot(proc, INITIAL, 0, session_id="beta")
        if beta.get("reason") != "game_started":
            raise AssertionError(f"beta did not start from clean state: {beta}")

        send(proc, {
            "type": "session_end",
            "session_id": "alpha",
            "reason": "stale-end",
        })
        stale_end = recv_response(proc)
        if stale_end.get("reason") != "session_mismatch":
            raise AssertionError(f"stale session ended beta: {stale_end}")

        send(proc, {
            "type": "session_end",
            "session_id": "beta",
            "reason": "test-complete",
        })
        ended = recv_response(proc)
        if ended.get("reason") != "session_ended":
            raise AssertionError(f"active session did not end: {ended}")

        after_end = send_snapshot(proc, INITIAL, 1, session_id="beta")
        if after_end.get("reason") != "session_required":
            raise AssertionError(f"snapshot after session end was accepted: {after_end}")
    finally:
        stop_host(proc)

    frames = load_frames(frame_log)
    beta_start = next(
        (
            index
            for index, frame in enumerate(frames)
            if frame.get("type") == "session" and
            frame.get("event") == "started" and
            frame.get("session_id") == "beta"
        ),
        None,
    )
    beta_position = next(
        (
            index
            for index, frame in enumerate(frames)
            if beta_start is not None and index > beta_start and
            frame.get("type") == "position"
        ),
        None,
    )
    beta_end = next(
        (
            index
            for index, frame in enumerate(frames)
            if frame.get("type") == "session" and
            frame.get("event") == "ended" and
            frame.get("reason") == "test-complete"
        ),
        None,
    )
    if beta_start is None or beta_position is None or beta_end is None:
        raise AssertionError(f"missing beta lifecycle frames: {frames}")
    if any(
        frame.get("type") == "analysis"
        for frame in frames[beta_start + 1:beta_position]
    ):
        raise AssertionError("an old-session analysis crossed beta start")
    if any(
        frame.get("type") == "analysis" for frame in frames[beta_end + 1:]
    ):
        raise AssertionError("analysis continued after beta session end")


def assert_fen_override(frame_log):
    proc = start_host(frame_log, session_id="fen-session")
    try:
        after_e4 = apply_move(INITIAL, "e2e4")
        waiting = send_snapshot(
            proc, after_e4, 1, session_id="fen-session")
        if waiting.get("reason") != "waiting_for_second_frame":
            raise AssertionError(f"midgame hold setup failed: {waiting}")

        authoritative = (
            "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/"
            "RNBQKBNR b KQkq e3 17 41"
        )
        send(proc, {
            "type": "session_command",
            "session_id": "fen-session",
            "command": "set_fen",
            "payload": authoritative,
        })
        applied = recv_response(proc)
        if applied.get("reason") != "fen_applied" or \
                applied.get("fen") != authoritative:
            raise AssertionError(f"valid FEN override failed: {applied}")

        # Commands alter chess history, not browser ordering.
        stale = send_snapshot(
            proc, after_e4, 1, session_id="fen-session")
        if stale.get("reason") != "stale_snapshot":
            raise AssertionError(f"FEN override reset snapshot sequence: {stale}")

        send(proc, {
            "type": "session_command",
            "session_id": "fen-session",
            "command": "set_fen",
            # Claims white castling rights without a rook on h1.
            "payload": "4k3/8/8/8/8/8/8/4K3 w K - 0 1",
        })
        invalid = recv_response(proc)
        if invalid.get("reason") != "invalid_fen":
            raise AssertionError(f"structurally invalid FEN was accepted: {invalid}")

        recovery_frames = wait_for_frames(
            frame_log,
            lambda frames: any(
                frame.get("type") == "recovery" and
                frame.get("action") == "set_fen" and
                frame.get("accepted") is False
                for frame in frames
            ),
        )
        rejected = next(
            frame
            for frame in reversed(recovery_frames)
            if frame.get("type") == "recovery" and
            frame.get("action") == "set_fen" and
            frame.get("accepted") is False
        )
        if rejected.get("ok") is not False or rejected.get("kind") != "warn":
            raise AssertionError(f"FEN rejection feedback was unclear: {rejected}")

        after_e5 = apply_move(after_e4, "e7e5")
        continued = send_snapshot(
            proc, after_e5, 2, session_id="fen-session")
        expected = (
            "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/"
            "RNBQKBNR w KQkq e6 0 42"
        )
        if continued.get("reason") != "move_recorded" or \
                continued.get("fen") != expected:
            raise AssertionError(
                f"exact FEN state was not used for the next move: {continued}"
            )

        send(proc, {
            "type": "session_command",
            "session_id": "fen-session",
            "command": "restart_engines",
        })
        restarted = recv_response(proc)
        if restarted.get("reason") != "engines_restarted":
            raise AssertionError(f"engine restart command failed: {restarted}")
    finally:
        stop_host(proc)


def assert_rescan_failure_feedback(frame_log):
    proc = start_host(frame_log, session_id="rescan-session")
    try:
        send(proc, {
            "type": "session_command",
            "session_id": "rescan-session",
            "command": "rescan_result",
            "payload": "no_supported_board",
        })
        reply = recv_response(proc)
        if reply.get("reason") != "rescan_failed":
            raise AssertionError(
                f"rescan failure feedback was rejected: {reply}"
            )

        frames = wait_for_frames(
            frame_log,
            lambda items: any(
                frame.get("type") == "recovery" and
                frame.get("action") == "rescan" and
                frame.get("accepted") is False
                for frame in items
            ),
        )
        rejected = next(
            frame
            for frame in reversed(frames)
            if frame.get("type") == "recovery" and
            frame.get("action") == "rescan" and
            frame.get("accepted") is False
        )
        if rejected.get("kind") != "warn" or \
                "No supported Chess.com board" not in rejected.get("text", ""):
            raise AssertionError(
                f"rescan failure feedback was unclear: {rejected}"
            )

        send(proc, {
            "type": "session_command",
            "session_id": "rescan-session",
            "command": "rescan_result",
            "payload": "untrusted_reason",
        })
        invalid = recv_response(proc)
        if invalid.get("reason") != "invalid_rescan_result":
            raise AssertionError(
                f"unknown rescan result was accepted: {invalid}"
            )
    finally:
        stop_host(proc)


def assert_initial_board_repetition(frame_log):
    proc = start_host(frame_log, session_id="repetition-session")
    try:
        board = INITIAL
        first = send_snapshot(
            proc, board, 1, session_id="repetition-session")
        if first.get("reason") != "game_started":
            raise AssertionError(f"repetition setup failed: {first}")

        reply = first
        for sequence, move in enumerate(
            ("g1f3", "g8f6", "f3g1", "f6g8"), 2
        ):
            board = apply_move(board, move)
            reply = send_snapshot(
                proc, board, sequence, session_id="repetition-session")
            if reply.get("reason") != "move_recorded":
                raise AssertionError(
                    f"repetition move {move} was not tracked: {reply}"
                )

        expected = (
            "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/"
            "RNBQKBNR w KQkq - 4 3"
        )
        if board != INITIAL or reply.get("fen") != expected or \
                reply.get("uci") != "f6g8":
            raise AssertionError(
                f"returning to the initial layout reset game state: {reply}"
            )
    finally:
        stop_host(proc)


def assert_forced_duplicate_refresh(frame_log):
    proc = start_host(frame_log, session_id="refresh-session")
    try:
        send_snapshot(proc, INITIAL, 1, session_id="refresh-session")
        board = apply_move(INITIAL, "e2e4")
        moved = send_snapshot(proc, board, 2, session_id="refresh-session")
        if moved.get("reason") != "move_recorded":
            raise AssertionError(f"refresh setup move failed: {moved}")

        before = wait_for_frames(
            frame_log,
            lambda frames: any(
                frame.get("type") == "analysis" and frame.get("final") is True
                for frame in frames
            ),
        )
        position_count = sum(
            frame.get("type") == "position" for frame in before
        )

        refreshed = send_snapshot(
            proc,
            board,
            3,
            session_id="refresh-session",
            force=True,
        )
        if refreshed.get("reason") != "position_refreshed":
            raise AssertionError(f"forced duplicate stayed suppressed: {refreshed}")

        frames = wait_for_frames(
            frame_log,
            lambda items: sum(
                item.get("type") == "position" for item in items
            ) > position_count,
        )
        positions = [
            frame for frame in frames if frame.get("type") == "position"
        ]
        if positions[-1].get("last") != "e2e4":
            raise AssertionError(
                f"forced refresh lost last-move state: {positions[-1]}"
            )
        refreshed_sequence = positions[-1].get("seq")
        wait_for_frames(
            frame_log,
            lambda items: any(
                item.get("type") == "analysis" and
                item.get("seq") == refreshed_sequence and
                item.get("final") is True
                for item in items
            ),
        )

        duplicate = send_snapshot(
            proc,
            board,
            4,
            session_id="refresh-session",
            force=False,
        )
        if duplicate.get("reason") != "duplicate":
            raise AssertionError(f"ordinary duplicate changed behavior: {duplicate}")
    finally:
        stop_host(proc)


def assert_orientation_only_update(frame_log):
    proc = start_host(frame_log, session_id="orientation-session")
    try:
        started = send_snapshot(
            proc,
            INITIAL,
            1,
            session_id="orientation-session",
            visually_flipped=False,
        )
        if started.get("reason") != "game_started":
            raise AssertionError(f"orientation setup failed: {started}")

        before = wait_for_frames(
            frame_log,
            lambda frames: any(
                frame.get("type") == "analysis" and frame.get("final") is True
                for frame in frames
            ),
        )
        analysis_count = sum(
            frame.get("type") == "analysis" for frame in before
        )
        position_count = sum(
            frame.get("type") == "position" for frame in before
        )

        flipped = send_snapshot(
            proc,
            INITIAL,
            2,
            session_id="orientation-session",
            visually_flipped=True,
        )
        if flipped.get("reason") != "orientation_updated":
            raise AssertionError(f"duplicate flip was not applied: {flipped}")

        after = wait_for_frames(
            frame_log,
            lambda frames: any(
                frame.get("type") == "orientation" and
                frame.get("flip") is True
                for frame in frames
            ),
        )
        time.sleep(0.15)
        after = load_frames(frame_log)

        if sum(frame.get("type") == "position" for frame in after) != \
                position_count:
            raise AssertionError("orientation change republished the position")
        if sum(frame.get("type") == "analysis" for frame in after) != \
                analysis_count:
            raise AssertionError("orientation change triggered engine analysis")
    finally:
        stop_host(proc)


def assert_game_result_transport(frame_log):
    """Prove the bounded result survives the complete native/UI transport."""
    board = apply_move(INITIAL, "e2e4")
    proc = start_host(frame_log, session_id="result-exact")
    try:
        send_snapshot(proc, board, 1, session_id="result-exact")
        exact = send_history(
            proc, board, ["e2e4"], 1, 1,
            session_id="result-exact", game_result="1-0",
        )
        frames = wait_for_frames(
            frame_log,
            lambda items: any(
                item.get("type") == "game_record" and
                item.get("result") == "1-0"
                for item in items
            ),
        )
        exact_records = [
            item for item in frames if item.get("type") == "game_record"
        ]
        if exact.get("reason") != "history_reconciled" or \
                not exact_records or exact_records[-1].get("result") != "1-0":
            raise AssertionError(
                f"exact game result did not reach the overlay: {exact}, {exact_records}"
            )

        start_session(proc, "result-omitted", "result-omitted-game")
        send_snapshot(proc, board, 1, session_id="result-omitted")
        omitted = send_history(
            proc, board, ["e2e4"], 1, 1,
            session_id="result-omitted",
        )
        frames = wait_for_frames(
            frame_log,
            lambda items: len([
                item for item in items if item.get("type") == "game_record"
            ]) >= 2,
        )
        records = [
            item for item in frames if item.get("type") == "game_record"
        ]
        if omitted.get("reason") != "history_reconciled" or \
                records[-1].get("result") != "*":
            raise AssertionError(
                f"omitted game result was not conservatively unknown: {omitted}, {records[-1]}"
            )

        record_count = len(records)
        invalid = send_history(
            proc, board, ["e2e4"], 2, 1,
            session_id="result-omitted", game_result="White won",
        )
        time.sleep(0.05)
        after_invalid = [
            item for item in load_frames(frame_log)
            if item.get("type") == "game_record"
        ]
        if invalid.get("reason") != "invalid_game_result" or \
                len(after_invalid) != record_count or \
                after_invalid[-1].get("result") != "*":
            raise AssertionError(
                "invalid result emitted or mutated a game record: "
                f"{invalid}, before={records}, after={after_invalid}"
            )

        # The final result is independent of DOM move-history availability.
        # A position-only completion must still retain an exact visible result.
        start_session(proc, "result-end-exact", "result-end-exact-game")
        send_snapshot(proc, board, 1, session_id="result-end-exact")
        send(proc, {
            "type": "session_end",
            "session_id": "result-end-exact",
            "reason": "game_end",
            "game_result": "1-0",
        })
        exact_end = recv_response(proc)
        frames = wait_for_frames(
            frame_log,
            lambda items: any(
                item.get("type") == "session" and
                item.get("event") == "ended" and
                item.get("result") == "1-0"
                for item in items
            ),
        )
        if exact_end.get("reason") != "session_ended":
            raise AssertionError(
                f"history-free result did not end its session: {exact_end}"
            )

        start_session(proc, "result-end-invalid", "result-end-invalid-game")
        send_snapshot(proc, board, 1, session_id="result-end-invalid")
        send(proc, {
            "type": "session_end",
            "session_id": "result-end-invalid",
            "reason": "game_end",
            "game_result": "White won",
        })
        invalid_end = recv_response(proc)
        if invalid_end.get("reason") != "invalid_game_result":
            raise AssertionError(
                f"malformed session result was accepted: {invalid_end}"
            )

        send(proc, {
            "type": "session_end",
            "session_id": "result-end-invalid",
            "reason": "navigation",
            "game_result": "0-1",
        })
        ordinary_end = recv_response(proc)
        frames = wait_for_frames(
            frame_log,
            lambda items: any(
                item.get("type") == "session" and
                item.get("event") == "ended" and
                item.get("reason") == "navigation"
                for item in items
            ),
        )
        navigation_frames = [
            item for item in frames
            if item.get("type") == "session" and
            item.get("event") == "ended" and
            item.get("reason") == "navigation"
        ]
        if ordinary_end.get("reason") != "session_ended" or \
                not navigation_frames or \
                navigation_frames[-1].get("result") != "*":
            raise AssertionError(
                "non-game end retained a result: "
                f"{ordinary_end}, {navigation_frames}"
            )
    finally:
        stop_host(proc)


def assert_overlay_event_bridge(frame_log):
    fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    controls = (
        "RESCAN stale-session|"
        "RESCAN bridge-session|"
        f"FEN bridge-session {fen}|"
        "RESTART bridge-session|"
        "STOP bridge-session"
    )
    proc = start_host(
        frame_log,
        session_id="bridge-session",
        controls=controls,
        controls_after_positions="1",
    )
    try:
        asynchronous = []
        started = send_snapshot(
            proc,
            INITIAL,
            1,
            session_id="bridge-session",
            async_messages=asynchronous,
        )
        if started.get("reason") != "game_started":
            raise AssertionError(f"bridge setup failed: {started}")

        deadline = time.monotonic() + MESSAGE_TIMEOUT
        while len(asynchronous) < 4:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            asynchronous.append(recv(proc, remaining))

        expected = [
            {
                "type": "overlay_command",
                "command": "rescan",
                "session_id": "bridge-session",
            },
            {
                "type": "overlay_command",
                "command": "set_fen",
                "session_id": "bridge-session",
                "payload": fen,
            },
            {
                "type": "overlay_command",
                "command": "restart_engines",
                "session_id": "bridge-session",
            },
            {
                "type": "overlay_command",
                "command": "stop_session",
                "session_id": "bridge-session",
            },
        ]
        if asynchronous != expected:
            raise AssertionError(
                f"overlay commands were not framed atomically: {asynchronous}"
            )
    finally:
        stop_host(proc)


def assert_overlay_dismissed_event(frame_log):
    proc = launch_host(frame_log, controls="QUIT")
    handshake(proc)
    event = recv(proc, TEST_TIMEOUT)
    expected = {"type": "overlay_event", "event": "dismissed"}
    if event != expected:
        proc.kill()
        proc.wait(timeout=2)
        raise AssertionError(f"overlay dismissal was not announced: {event}")

    if proc.stdin is not None and not proc.stdin.closed:
        proc.stdin.close()
    proc.wait(timeout=5)
    if proc.returncode != 0:
        raise AssertionError(
            f"host exited with {proc.returncode} after overlay dismissal"
        )


def assert_active_overlay_dismissed_event(frame_log):
    proc = start_host(
        frame_log,
        session_id="dismiss-session",
        controls="QUIT dismiss-session",
        controls_after_positions="1",
    )
    send(proc, {
        "type": "position_snapshot",
        "session_id": "dismiss-session",
        "snapshot_seq": 1,
        "board": INITIAL,
        "visually_flipped": False,
    })

    expected = {
        "type": "overlay_event",
        "event": "dismissed",
        "session_id": "dismiss-session",
    }
    deadline = time.monotonic() + TEST_TIMEOUT
    received = []
    while time.monotonic() < deadline:
        message = recv(proc, deadline - time.monotonic())
        received.append(message)
        if message == expected:
            break
    else:
        proc.kill()
        proc.wait(timeout=2)
        raise AssertionError(
            f"active overlay dismissal was not session-scoped: {received}"
        )

    if proc.stdin is not None and not proc.stdin.closed:
        proc.stdin.close()
    proc.wait(timeout=5)
    if proc.returncode != 0:
        raise AssertionError(
            f"host exited with {proc.returncode} after active dismissal"
        )


def session_command(proc, session_id, command, payload=None):
    message = {
        "type": "session_command",
        "session_id": session_id,
        "command": command,
    }
    if payload is not None:
        message["payload"] = payload
    send(proc, message)
    return recv_response(proc)


def assert_analysis_lab(frame_log):
    session_id = "lab-session"
    initial_fen = (
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/"
        "RNBQKBNR w KQkq - 0 1"
    )
    after_e4_fen = (
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/"
        "RNBQKBNR b KQkq e3 0 1"
    )
    proc = start_host(frame_log, session_id=session_id)
    try:
        started = send_snapshot(proc, INITIAL, 1, session_id=session_id)
        if started.get("reason") != "game_started":
            raise AssertionError(f"lab setup failed: {started}")

        opened = session_command(
            proc, session_id, "explore_start",
            initial_fen + "|e2e4,e7e5",
        )
        if opened.get("reason") != "explore_started" or \
                opened.get("node_id") != 2:
            raise AssertionError(f"PV-seeded explorer failed: {opened}")
        branch_id = opened["branch_id"]

        frames = wait_for_frames(
            frame_log,
            lambda items: any(
                item.get("type") == "position" and
                item.get("mode") == "explore" and
                item.get("branch_id") == branch_id and
                item.get("node_id") == 2
                for item in items
            ),
        )
        frames = wait_for_frames(
            frame_log,
            lambda items: any(
                item.get("type") == "analysis" and
                item.get("mode") == "explore" and
                item.get("branch_id") == branch_id and
                item.get("node_id") == 2 and
                item.get("target_revision") == item.get("seq") and
                len(item.get("lines", [])) >= 2 and
                item.get("lines", [{}, {}])[1].get("bound") == "lowerbound"
                for item in items
            ),
        )
        seeded_analysis = next(
            item for item in reversed(frames)
            if item.get("type") == "analysis" and
            item.get("mode") == "explore" and
            item.get("branch_id") == branch_id and
            item.get("node_id") == 2
        )
        if seeded_analysis.get("best", {}).get("bound") != "exact" or \
                len(seeded_analysis.get("lines", [])) < 2 or \
                seeded_analysis["lines"][1].get("bound") != "lowerbound":
            raise AssertionError(
                f"UCI score bounds were not preserved: {seeded_analysis}")
        branch_position_index = max(
            index for index, item in enumerate(frames)
            if item.get("type") == "position" and
            item.get("mode") == "explore" and
            item.get("branch_id") == branch_id
        )

        illegal = session_command(
            proc, session_id, "explore_move", f"{branch_id} 2 e2e4")
        if illegal.get("reason") != "illegal_move":
            raise AssertionError(f"illegal lab move was accepted: {illegal}")

        stale_branch = session_command(
            proc, session_id, "explore_goto", f"{branch_id + 1000} 0")
        if stale_branch.get("reason") != "stale_branch":
            raise AssertionError(f"stale branch was accepted: {stale_branch}")
        unknown_node = session_command(
            proc, session_id, "explore_goto", f"{branch_id} 255")
        if unknown_node.get("reason") != "unknown_node":
            raise AssertionError(f"unknown branch node was accepted: {unknown_node}")

        root = session_command(
            proc, session_id, "explore_goto", f"{branch_id} 0")
        if root.get("reason") != "explore_position_selected" or \
                root.get("node_id") != 0:
            raise AssertionError(f"lab goto root failed: {root}")
        root_frames = wait_for_frames(
            frame_log,
            lambda items: any(
                item.get("type") == "analysis" and
                item.get("mode") == "explore" and
                item.get("branch_id") == branch_id and
                item.get("node_id") == 0
                for item in items
            ),
        )
        root_position_index = max(
            index for index, item in enumerate(root_frames)
            if item.get("type") == "position" and
            item.get("mode") == "explore" and
            item.get("branch_id") == branch_id and
            item.get("node_id") == 0
        )
        if any(
            item.get("type") == "analysis" and
            item.get("mode") == "explore" and
            item.get("node_id") == 2
            for item in root_frames[root_position_index + 1:]
        ):
            raise AssertionError("old-node analysis crossed a newer target")

        alternative = session_command(
            proc, session_id, "explore_move", f"{branch_id} 0 d2d4")
        if alternative.get("reason") != "explore_move_applied":
            raise AssertionError(f"lab alternative failed: {alternative}")
        alternative_node = alternative["node_id"]

        live_board = apply_move(INITIAL, "e2e4")
        live_reply = send_snapshot(
            proc, live_board, 2, session_id=session_id)
        if live_reply.get("reason") != "move_recorded":
            raise AssertionError(f"live state stalled behind explorer: {live_reply}")

        frames = wait_for_frames(
            frame_log,
            lambda items: any(
                item.get("type") == "live_update" and
                item.get("fen") == after_e4_fen
                for item in items
            ),
        )
        if any(
            item.get("type") == "position" and
            item.get("mode") == "live" and
            item.get("fen") == after_e4_fen
            for item in frames[branch_position_index + 1:]
        ):
            raise AssertionError("a real move overwrote the selected lab branch")

        stale_base = session_command(
            proc, session_id, "explore_start", initial_fen)
        if stale_base.get("reason") != "stale_base":
            raise AssertionError(f"stale explorer base was accepted: {stale_base}")

        live = session_command(
            proc, session_id, "explore_live", str(branch_id))
        if live.get("reason") != "explore_live":
            raise AssertionError(f"return to live failed: {live}")
        live_frames = wait_for_frames(
            frame_log,
            lambda items: any(
                item.get("type") == "position" and
                item.get("mode") == "live" and
                item.get("fen") == after_e4_fen
                for item in items
            ),
        )
        live_frames = wait_for_frames(
            frame_log,
            lambda items: any(
                item.get("type") == "analysis" and
                item.get("mode") == "live" and
                item.get("fen") == after_e4_fen and
                len(item.get("lines", [])) >= 2
                for item in items
            ),
        )
        black_analysis = next(
            item for item in reversed(live_frames)
            if item.get("type") == "analysis" and
            item.get("mode") == "live" and
            item.get("fen") == after_e4_fen and
            len(item.get("lines", [])) >= 2
        )
        if black_analysis["lines"][1].get("bound") != "upperbound":
            raise AssertionError(
                "black-to-move score inversion did not invert the UCI bound: "
                f"{black_analysis}")

        resumed = session_command(
            proc, session_id, "explore_resume",
            f"{branch_id} {alternative_node}",
        )
        if resumed.get("reason") != "explore_resumed":
            raise AssertionError(f"branch resume failed: {resumed}")

        # The branch tree is backed by the native legal move generator, not by
        # permissive GUI piece dragging. Exercise the stateful special moves.
        special_moves = (
            ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1"),
            ("7k/8/8/3pP3/8/8/8/K7 w - d6 0 1", "e5d6"),
            ("7k/P7/8/8/8/8/8/K7 w - - 0 1", "a7a8n"),
        )
        for fen, uci in special_moves:
            applied = session_command(proc, session_id, "set_fen", fen)
            if applied.get("reason") != "fen_applied":
                raise AssertionError(f"special-move FEN failed: {applied}")
            special = session_command(proc, session_id, "explore_start", fen)
            if special.get("reason") != "explore_started":
                raise AssertionError(f"special branch failed: {special}")
            branch_id = special["branch_id"]
            played = session_command(
                proc, session_id, "explore_move", f"{branch_id} 0 {uci}")
            if played.get("reason") != "explore_move_applied" or \
                    played.get("last") != uci:
                raise AssertionError(f"special lab move {uci} failed: {played}")

        start_session(proc, "lab-replacement", "replacement")
        frames = wait_for_frames(
            frame_log,
            lambda items: any(
                item.get("type") == "explore" and
                item.get("event") == "destroyed" and
                item.get("branch_id") == branch_id
                for item in items
            ),
        )
        replacement_index = max(
            index for index, item in enumerate(frames)
            if item.get("type") == "session" and
            item.get("event") == "started" and
            item.get("session_id") == "lab-replacement"
        )
        if any(
            item.get("type") == "analysis" and
            item.get("mode") == "explore"
            for item in frames[replacement_index + 1:]
        ):
            raise AssertionError("branch analysis crossed a session replacement")
    finally:
        stop_host(proc)


def assert_analysis_settings(temp):
    """Exercise Analysis Lab-only settings through the real control thread."""
    cases = (
        (
            "continuous",
            "budget=100 explore_budget=-1 maia=0 threads=1 multipv=3",
            "budget=90 explore_budget=0 maia=0 threads=1 multipv=2",
            0,
        ),
        (
            "lower-clamp",
            "budget=100 explore_budget=-1 maia=0 threads=1 multipv=3",
            "budget=90 explore_budget=-999 maia=0 threads=1 multipv=2",
            100,
        ),
        (
            "upper-clamp",
            "budget=100 explore_budget=-1 maia=0 threads=1 multipv=3",
            "budget=90 explore_budget=999999 maia=0 threads=1 multipv=2",
            10000,
        ),
    )

    for name, start_settings, set_payload, expected_budget in cases:
        frame_log = os.path.join(temp, f"analysis-settings-{name}.jsonl")
        proc = start_host(
            frame_log,
            set_after_positions="1",
            session_id=f"settings-{name}",
            start_settings=start_settings,
            set_payload=set_payload,
        )
        try:
            reply = send_snapshot(
                proc, INITIAL, 1, session_id=f"settings-{name}")
            if reply.get("reason") != "game_started":
                raise AssertionError(f"settings setup failed: {reply}")
            frames = wait_for_frames(
                frame_log,
                lambda items: any(
                    item.get("type") == "ready" for item in items
                ) and any(
                    item.get("type") == "settings" for item in items
                ),
            )
        finally:
            stop_host(proc)

        ready = next(item for item in frames if item.get("type") == "ready")
        settings = next(
            item for item in reversed(frames)
            if item.get("type") == "settings"
        )
        if ready.get("explore_budget_ms") != -1 or \
                ready.get("maia_rating") != 0 or ready.get("maia") is not False:
            raise AssertionError(
                f"Maia-off/same-live startup settings were lost: {ready}")
        if settings.get("explore_budget_ms") != expected_budget or \
                settings.get("maia_rating") != 0:
            raise AssertionError(
                f"Analysis Lab setting clamp failed ({name}): {settings}")


def main():
    failures = []

    with tempfile.TemporaryDirectory(prefix="chess-listener-e2e-") as temp:
        assert_protocol_mismatch_rejected(
            os.path.join(temp, "rejected-overlay-frames.jsonl")
        )
        print("protocol mismatch -> rejected before startup")

        assert_overlay_protocol_mismatch_reported(
            os.path.join(temp, "bad-overlay-protocol-frames.jsonl")
        )
        print("overlay mismatch  -> reported to browser before exit")

        assert_session_state_machine(
            os.path.join(temp, "session-state-frames.jsonl")
        )
        print("session state     -> isolated and monotonic")

        assert_fen_override(
            os.path.join(temp, "fen-override-frames.jsonl")
        )
        print("FEN override      -> exact state and continuation")

        assert_rescan_failure_feedback(
            os.path.join(temp, "rescan-failure-frames.jsonl")
        )
        print("rescan failure    -> returned to recovery UI")

        assert_initial_board_repetition(
            os.path.join(temp, "initial-repetition-frames.jsonl")
        )
        print("initial repetition -> retained clocks and history")

        assert_forced_duplicate_refresh(
            os.path.join(temp, "forced-refresh-frames.jsonl")
        )
        print("forced duplicate  -> republished and reanalysed")

        assert_orientation_only_update(
            os.path.join(temp, "orientation-frames.jsonl")
        )
        print("orientation       -> updated without reanalysis")

        assert_game_result_transport(
            os.path.join(temp, "game-result-frames.jsonl")
        )
        print("game result       -> bounded through native/UI transport")

        assert_overlay_event_bridge(
            os.path.join(temp, "event-bridge-frames.jsonl")
        )
        print("overlay commands  -> serialized to browser")

        assert_overlay_dismissed_event(
            os.path.join(temp, "dismissed-frames.jsonl")
        )
        print("overlay dismissal -> announced before shutdown")

        assert_active_overlay_dismissed_event(
            os.path.join(temp, "active-dismissed-frames.jsonl")
        )
        print("active dismissal  -> retained its session id")

        assert_analysis_lab(
            os.path.join(temp, "analysis-lab-frames.jsonl")
        )
        print("analysis lab      -> isolated branch, live updates, resume")

        assert_analysis_settings(temp)
        print("lab settings      -> same/live, continuous, clamps, Maia off")

        frame_log = os.path.join(temp, "overlay-frames.jsonl")
        proc = start_host(frame_log, set_after_positions="3")

        try:
            board = INITIAL
            latencies = []

            start_reply = send_snapshot(proc, board, 1)
            print("start   ->", start_reply.get("reason"))

            for snapshot_seq, move in enumerate(MOVES, 2):
                board = apply_move(board, move)
                began = time.monotonic()
                reply = send_snapshot(proc, board, snapshot_seq)
                elapsed = (time.monotonic() - began) * 1000.0
                latencies.append(elapsed)
                print(
                    f"{move}  -> {reply.get('reason', '<missing>'):<14} "
                    f"{elapsed:6.1f} ms"
                )

                if reply.get("reason") != "move_recorded":
                    failures.append(f"host rejected legal move {move}: {reply}")
                    break

                if GAP_SECONDS > 0:
                    time.sleep(GAP_SECONDS)

            expected_positions = len(MOVES) + 1

            def session_complete(frames):
                positions = [f for f in frames if f.get("type") == "position"]
                analyses = [f for f in frames if f.get("type") == "analysis"]
                settings = [f for f in frames if f.get("type") == "settings"]
                return (
                    len(positions) >= expected_positions
                    and bool(settings)
                    and bool(analyses)
                    and analyses[-1].get("seq") == positions[-1].get("seq")
                )

            frames = wait_for_frames(frame_log, session_complete)
        finally:
            stop_host(proc)

        positions = [f for f in frames if f.get("type") == "position"]
        analyses = [f for f in frames if f.get("type") == "analysis"]
        settings_echo = [f for f in frames if f.get("type") == "settings"]

        worst = max(latencies, default=0.0)
        mean = sum(latencies) / len(latencies) if latencies else 0.0
        print()
        print(f"host reply latency: max {worst:.1f} ms, mean {mean:.1f} ms")
        print(f"board frames: {len(positions)} (expected {expected_positions})")
        print(f"eval frames:  {len(analyses)}")

        if worst > 250.0:
            failures.append(f"host blocked for {worst:.0f} ms on a message")

        if len(positions) != expected_positions:
            failures.append("some positions never reached the overlay")

        expected_last_moves = [None, *MOVES]
        observed_last_moves = [position.get("last") for position in positions]
        if observed_last_moves != expected_last_moves:
            failures.append(
                "position last-move sequence is wrong: "
                f"{observed_last_moves!r}"
            )

        # Frame ordering is the protocol's race-safety guarantee: analysis for
        # a sequence must never arrive before the board frame that establishes
        # that sequence in the overlay.
        position_index = {
            frame.get("seq"): index
            for index, frame in enumerate(frames)
            if frame.get("type") == "position"
        }
        for index, frame in enumerate(frames):
            if frame.get("type") != "analysis":
                continue
            seq = frame.get("seq")
            if seq not in position_index:
                failures.append(f"analysis for seq {seq} has no position frame")
            elif index < position_index[seq]:
                failures.append(f"analysis for seq {seq} preceded its position")

        if not analyses:
            failures.append("no evaluations were published")
        else:
            last = analyses[-1]
            if last.get("seq") != positions[-1].get("seq"):
                failures.append(
                    f"last eval is for seq {last.get('seq')}, board is at "
                    f"{positions[-1].get('seq')}"
                )
            if "best" not in last:
                failures.append("final evaluation carries no best move")
            else:
                print(f"final eval: {last['best']} depth {last.get('depth')}")
            if last.get("last") != MOVES[-1]:
                failures.append(
                    "final evaluation carries wrong last move: "
                    f"{last.get('last')!r}"
                )

        if not settings_echo:
            failures.append("live SET was never acknowledged")
        else:
            print(f"settings echo: {settings_echo[-1]}")

    print()
    if failures:
        for failure in failures:
            print("FAIL:", failure)
        return 1

    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
