#!/usr/bin/env python3
"""Small deterministic UCI engine used by the integration tests.

It implements only the protocol surface ChessListener needs. Keeping this in
the repository makes the tests independent of the system package layout,
network access, Stockfish speed, and machine load.
"""

import re
import sys


MULTIPV_MOVES = ("e2e4", "d2d4", "g1f3", "c2c4", "b1c3")


def reply(line):
    print(line, flush=True)


def main():
    multipv = 1
    searching = False

    for raw in sys.stdin:
        command = raw.strip()

        if command == "uci":
            reply("id name ChessListener deterministic test engine")
            reply("id author ChessListener")
            reply("option name Threads type spin default 1 min 1 max 128")
            reply("option name MultiPV type spin default 1 min 1 max 5")
            reply("uciok")
        elif command == "isready":
            reply("readyok")
        elif command.startswith("setoption name MultiPV value "):
            match = re.search(r"value\s+(\d+)$", command)
            if match:
                multipv = max(1, min(len(MULTIPV_MOVES), int(match.group(1))))
        elif command.startswith("go"):
            for rank, move in enumerate(MULTIPV_MOVES[:multipv], 1):
                score = 31 - (rank - 1) * 18
                bound = " lowerbound" if rank == 2 else ""
                reply(
                    f"info depth 12 seldepth 18 multipv {rank} "
                    f"score cp {score}{bound} nodes 4096 pv {move} e7e5"
                )

            if command == "go infinite":
                searching = True
            else:
                reply(f"bestmove {MULTIPV_MOVES[0]}")
        elif command == "stop":
            if searching:
                reply(f"bestmove {MULTIPV_MOVES[0]}")
                searching = False
        elif command == "quit":
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
