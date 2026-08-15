#!/usr/bin/env python3
"""Headless stand-in for ``overlay.py`` used by native integration tests.

It starts immediately, logs every host frame, and can deterministically send a
live settings update after a configured number of position frames. No sleeps
or GUI dependencies are involved.
"""

import json
import os
import sys


LOG = os.environ.get("CHESSLISTENER_STUB_LOG", "/tmp/chess-listener-frames.jsonl")
SET_AFTER = int(os.environ.get("CHESSLISTENER_STUB_SET_AFTER_POSITIONS", "0"))
PROTOCOL = os.environ.get("CHESSLISTENER_STUB_PROTOCOL", "1")


def send(command):
    os.write(sys.stdout.fileno(), (command + "\n").encode("utf-8"))


def main():
    budget = os.environ.get("BUDGET", "100")
    send(
        f"START protocol={PROTOCOL} ui_version=0.2.1-test budget={budget} "
        "maia=1900 threads=1 multipv=3"
    )

    positions = 0
    settings_sent = False

    with open(LOG, "a", encoding="utf-8") as log:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                frame = json.loads(line)
            except json.JSONDecodeError as error:
                print(f"stub: bad JSON from host: {error}", file=sys.stderr)
                return 1

            log.write(line + "\n")
            log.flush()

            if frame.get("type") == "position":
                positions += 1

            if SET_AFTER > 0 and positions >= SET_AFTER and not settings_sent:
                # Exercise option changes while board snapshots are arriving.
                send("SET budget=90 maia=1900 threads=1 multipv=2")
                settings_sent = True

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
