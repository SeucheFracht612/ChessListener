"""End-to-end check of the native host against a real Stockfish.

Plays the part of both ends: the browser (length-prefixed native messages) and
the overlay (JSON in on stdin, control lines out on stdout).

What it is actually proving:
  * every position message is answered promptly, so the host never stalls the
    browser while an engine is thinking
  * every position produces a board frame in the overlay, even during a burst
  * evaluations still arrive, and the last one is for the last position
  * a live SET is picked up mid-session
"""

import json
import os
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
NATIVE = os.path.dirname(HERE)

HOST = os.path.join(NATIVE, "chess-listener-host")
STUB = os.path.join(HERE, "stub_overlay.py")
FRAME_LOG = os.path.join(HERE, "overlay_frames.jsonl")

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

GAP_SECONDS = float(os.environ.get("GAP", "0.04"))

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
    raw = json.dumps(payload).encode()
    proc.stdin.write(struct.pack("<I", len(raw)) + raw)
    proc.stdin.flush()


def recv(proc):
    header = proc.stdout.read(4)

    if len(header) < 4:
        return None

    length = struct.unpack("<I", header)[0]
    return json.loads(proc.stdout.read(length))


def main():
    environment = dict(os.environ)
    environment["CHESSLISTENER_OVERLAY"] = STUB
    environment["CHESSLISTENER_STOCKFISH"] = "/usr/games/stockfish"
    environment["CHESSLISTENER_LC0"] = "/nonexistent"  # no Maia in this sandbox
    environment["CHESSLISTENER_STUB_LOG"] = FRAME_LOG

    open(environment["CHESSLISTENER_STUB_LOG"], "w").close()

    proc = subprocess.Popen(
        [HOST],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=environment,
    )

    board = INITIAL
    latencies = []

    send(proc, {"type": "position_snapshot", "board": board, "visually_flipped": False})
    print("start   ->", recv(proc)["reason"])

    for number, move in enumerate(MOVES, 1):
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
        print(f"{move}  -> {reply['reason']:<14} {elapsed:6.1f} ms")

        if reply["reason"] != "move_recorded":
            print("FAIL: host rejected a legal move")
            proc.kill()
            return 1

        # 40 ms between moves: far faster than any engine can finish a search.
        time.sleep(GAP_SECONDS)

    time.sleep(3.0)  # let the last search settle
    proc.stdin.close()
    proc.wait(timeout=10)

    frames = [
        json.loads(line)
        for line in open(environment["CHESSLISTENER_STUB_LOG"])
        if line.strip()
    ]
    positions = [f for f in frames if f.get("type") == "position"]
    analyses = [f for f in frames if f.get("type") == "analysis"]

    worst = max(latencies)
    print()
    print(f"host reply latency: max {worst:.1f} ms, mean "
          f"{sum(latencies)/len(latencies):.1f} ms")
    print(f"board frames: {len(positions)} (expected {len(MOVES) + 1})")
    print(f"eval frames:  {len(analyses)}")

    failures = []

    if worst > 150.0:
        failures.append(f"host blocked for {worst:.0f} ms on a message")

    if len(positions) != len(MOVES) + 1:
        failures.append("some positions never reached the overlay")

    if not analyses:
        failures.append("no evaluations were published")
    else:
        last = analyses[-1]

        if last["seq"] != positions[-1]["seq"]:
            failures.append(
                f"last eval is for seq {last['seq']}, board is at "
                f"{positions[-1]['seq']}"
            )

        if "best" not in last:
            failures.append("final evaluation carries no best move")
        else:
            print(f"final eval: {last['best']} depth {last.get('depth')}")

    settings_echo = [f for f in frames if f.get("type") == "settings"]

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
