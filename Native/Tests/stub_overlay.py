"""Headless stand-in for overlay.py, used by e2e.py.

Speaks the same control protocol: START immediately, one live SET a moment
later, and logs every frame the host publishes.
"""

import json
import os
import sys
import threading
import time

LOG = os.environ.get("CHESSLISTENER_STUB_LOG", "/tmp/overlay_frames.jsonl")


def send(command):
    os.write(sys.stdout.fileno(), (command + "\n").encode())


def later():
    # Change strength while searches are in flight, which is the case the live
    # settings panel actually creates.
    time.sleep(1.2)
    send("SET budget=250 maia=1900 threads=1 multipv=2")


def main():
    send("START budget=" + os.environ.get("BUDGET","600") + " maia=1900 threads=2 multipv=3")
    threading.Thread(target=later, daemon=True).start()

    with open(LOG, "a") as log:
        for line in sys.stdin:
            line = line.strip()

            if not line:
                continue

            try:
                json.loads(line)
            except json.JSONDecodeError as error:
                print(f"stub: bad JSON from host: {error}", file=sys.stderr)
                continue

            log.write(line + "\n")
            log.flush()


if __name__ == "__main__":
    main()
