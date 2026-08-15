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

PROTOCOL_VERSION = 2
HOST_VERSION = "0.3.0"
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
        "extension_version": "0.3.0-test",
    })
    reply = recv(proc)

    if reply.get("type") != "hello" or reply.get("ok") is not True:
        raise AssertionError(f"native handshake was rejected: {reply}")
    if reply.get("protocol_version") != PROTOCOL_VERSION:
        raise AssertionError(f"native handshake chose wrong protocol: {reply}")
    if not isinstance(reply.get("capabilities"), list):
        raise AssertionError(f"native handshake has no capabilities: {reply}")
    required = {
        "session_v2", "state_override", "streaming_analysis", "last_move"
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
    send(proc, payload)
    return recv_response(proc, async_messages=async_messages)


def host_environment(
    frame_log,
    *,
    set_after_positions="0",
    overlay_protocol=PROTOCOL_VERSION,
    controls="",
    controls_after_positions="0",
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
        ),
    )


def start_host(
    frame_log,
    *,
    set_after_positions="0",
    session_id=DEFAULT_SESSION_ID,
    controls="",
    controls_after_positions="0",
):
    proc = launch_host(
        frame_log,
        set_after_positions=set_after_positions,
        controls=controls,
        controls_after_positions=controls_after_positions,
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
