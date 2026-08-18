#!/usr/bin/env python3
"""Deterministic UCI double for the independent-engine-lane test.

The same executable behaves like instant Stockfish without ``--weights`` and
like deliberately slow Maia when lc0's weights argument is present. This
recreates the regression: a 750 ms Maia query must not hold back Stockfish.
"""

import os
import sys
import time


IS_MAIA = any(argument.startswith("--weights=") for argument in sys.argv[1:])
MAIA_DELAY = float(os.environ.get("CHESSLISTENER_FAKE_MAIA_DELAY", "0.75"))


def reply(line):
    print(line, flush=True)


def emit_result():
    reply("info depth 11 score cp 24 nodes 2048 pv e2e4 e7e5")
    reply("bestmove e2e4")


def main():
    searching = False

    for raw in sys.stdin:
        command = raw.strip()

        if command == "uci":
            reply("id name ChessListener lane test engine")
            reply("option name Threads type spin default 1 min 1 max 128")
            reply("option name MultiPV type spin default 1 min 1 max 5")
            reply("option name MinibatchSize type spin default 1 min 1 max 1024")
            reply("option name MaxPrefetch type spin default 0 min 0 max 1024")
            reply("option name Backend type string default eigen")
            reply("uciok")
        elif command == "isready":
            reply("readyok")
        elif command.startswith("go"):
            if IS_MAIA:
                time.sleep(MAIA_DELAY)
                emit_result()
            elif command == "go infinite":
                reply("info depth 10 score cp 31 nodes 1024 pv e2e4 e7e5")
                searching = True
            else:
                emit_result()
        elif command == "stop":
            if searching:
                reply("bestmove e2e4")
                searching = False
        elif command == "quit":
            return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
