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

PROTOCOL_VERSION = 1
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


def handshake(proc):
    send(proc, {
        "type": "hello",
        "protocol_version": PROTOCOL_VERSION,
        "extension_version": "0.2.1-test",
    })
    reply = recv(proc)

    if reply.get("type") != "hello" or reply.get("ok") is not True:
        raise AssertionError(f"native handshake was rejected: {reply}")
    if reply.get("protocol_version") != PROTOCOL_VERSION:
        raise AssertionError(f"native handshake chose wrong protocol: {reply}")
    if not isinstance(reply.get("capabilities"), list):
        raise AssertionError(f"native handshake has no capabilities: {reply}")

    return reply


def host_environment(
    frame_log, *, set_after_positions="0", overlay_protocol=PROTOCOL_VERSION
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
        BUDGET="100",
    )
    return environment


def launch_host(
    frame_log, *, set_after_positions="0", overlay_protocol=PROTOCOL_VERSION
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
        ),
    )


def start_host(frame_log, *, set_after_positions="0"):
    proc = launch_host(frame_log, set_after_positions=set_after_positions)
    handshake(proc)
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
        "host_version": "0.2.1",
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

        frame_log = os.path.join(temp, "overlay-frames.jsonl")
        proc = start_host(frame_log, set_after_positions="3")

        try:
            board = INITIAL
            latencies = []

            send(proc, {
                "type": "position_snapshot",
                "board": board,
                "visually_flipped": False,
            })
            start_reply = recv(proc)
            print("start   ->", start_reply.get("reason"))

            for move in MOVES:
                board = apply_move(board, move)
                began = time.monotonic()
                send(proc, {
                    "type": "position_snapshot",
                    "board": board,
                    "visually_flipped": False,
                })
                reply = recv(proc)
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
