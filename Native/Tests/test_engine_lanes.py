#!/usr/bin/env python3
"""Regression test for the Stockfish-fast / Maia-background architecture."""

import os
import subprocess
import tempfile
import time

from e2e import (
    HOST,
    INITIAL,
    PROTOCOL_VERSION,
    STUB,
    apply_move,
    handshake,
    load_frames,
    send_snapshot,
    session_command,
    start_session,
    stop_host,
    wait_for_frames,
)


HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_LANES = os.path.join(HERE, "fake_uci_lanes.py")


def launch(frame_log, maia_net):
    environment = dict(os.environ)
    environment.update(
        CHESSLISTENER_OVERLAY=STUB,
        CHESSLISTENER_STOCKFISH=FAKE_LANES,
        CHESSLISTENER_LC0=FAKE_LANES,
        CHESSLISTENER_MAIA_NET=maia_net,
        CHESSLISTENER_STUB_LOG=frame_log,
        CHESSLISTENER_STUB_PROTOCOL=str(PROTOCOL_VERSION),
        CHESSLISTENER_FAKE_MAIA_DELAY="0.75",
        CHESSLISTENER_MAIA_DEADLINE_MS="1500",
        BUDGET="1000",
    )
    return subprocess.Popen(
        [HOST],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=environment,
    )


def first_matching(path, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frames = load_frames(path)
        for index, frame in enumerate(frames):
            if predicate(frame):
                return index, frame, frames
        time.sleep(0.005)
    raise TimeoutError("expected overlay frame did not arrive")


def first_matching_after(path, start_index, predicate, timeout=5.0):
    """Find a frame emitted after a known target/session boundary."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frames = load_frames(path)
        for index in range(start_index, len(frames)):
            frame = frames[index]
            if predicate(frame):
                return index, frame, frames
        time.sleep(0.005)
    raise TimeoutError("expected post-boundary overlay frame did not arrive")


def main():
    if not os.path.isfile(HOST) or not os.access(HOST, os.X_OK):
        raise RuntimeError(f"native host is not executable: {HOST}")
    if not os.access(FAKE_LANES, os.X_OK):
        raise RuntimeError(f"lane test engine is not executable: {FAKE_LANES}")

    with tempfile.TemporaryDirectory(prefix="chess-listener-lanes-") as temp:
        frame_log = os.path.join(temp, "frames.jsonl")
        maia_net = os.path.join(temp, "maia-test.pb.gz")
        with open(maia_net, "wb") as stream:
            stream.write(b"deterministic-test-network\n")

        proc = launch(frame_log, maia_net)
        try:
            handshake(proc)
            start_session(proc, "lane-session", "lane-game")

            began = time.monotonic()
            reply = send_snapshot(
                proc, INITIAL, 1, session_id="lane-session")
            if reply.get("reason") != "game_started":
                raise AssertionError(f"initial position rejected: {reply}")

            frames = wait_for_frames(
                frame_log,
                lambda frames: any(
                    frame.get("type") == "position" for frame in frames
                ),
            )
            position = next(
                frame for frame in reversed(frames)
                if frame.get("type") == "position"
            )
            revision = position["seq"]

            _, first, _ = first_matching(
                frame_log,
                lambda frame: frame.get("type") == "analysis"
                and frame.get("seq") == revision
                and "best" in frame,
            )
            stockfish_ms = (time.monotonic() - began) * 1000.0
            if stockfish_ms >= 300.0:
                raise AssertionError(
                    f"Stockfish waited {stockfish_ms:.1f} ms for slow Maia"
                )
            if "human" in first:
                raise AssertionError("delayed Maia unexpectedly preceded Stockfish")

            _, merged, _ = first_matching(
                frame_log,
                lambda frame: frame.get("type") == "analysis"
                and frame.get("seq") == revision
                and "best" in frame
                and "human" in frame,
            )
            if not merged.get("lines"):
                raise AssertionError("late Maia frame erased Stockfish lines")

            # Supersede one slow Maia request. No human result belonging to the
            # abandoned revision may appear after the next board is committed.
            after_e4 = apply_move(INITIAL, "e2e4")
            moved = send_snapshot(
                proc, after_e4, 2, session_id="lane-session")
            if moved.get("reason") != "move_recorded":
                raise AssertionError(f"e4 rejected: {moved}")
            frames = wait_for_frames(
                frame_log,
                lambda frames: len([
                    frame for frame in frames if frame.get("type") == "position"
                ]) >= 2,
            )
            e4_position = next(
                frame for frame in reversed(frames)
                if frame.get("type") == "position"
            )
            e4_revision = e4_position["seq"]

            first_matching(
                frame_log,
                lambda frame: frame.get("type") == "analysis"
                and frame.get("seq") == e4_revision
                and "best" in frame,
            )

            after_e5 = apply_move(after_e4, "e7e5")
            send_e5 = time.monotonic()
            moved = send_snapshot(
                proc, after_e5, 3, session_id="lane-session")
            if moved.get("reason") != "move_recorded":
                raise AssertionError(f"e5 rejected: {moved}")

            positions = wait_for_frames(
                frame_log,
                lambda frames: len([
                    frame for frame in frames if frame.get("type") == "position"
                ]) >= 3,
            )
            e5_revision = [
                frame for frame in positions if frame.get("type") == "position"
            ][-1]["seq"]
            e5_position_index = max(
                index for index, frame in enumerate(positions)
                if frame.get("type") == "position"
                and frame.get("seq") == e5_revision
            )

            first_matching(
                frame_log,
                lambda frame: frame.get("type") == "analysis"
                and frame.get("seq") == e5_revision
                and "best" in frame,
            )
            e5_stockfish_ms = (time.monotonic() - send_e5) * 1000.0
            if e5_stockfish_ms >= 300.0:
                raise AssertionError(
                    f"supersession delayed Stockfish by {e5_stockfish_ms:.1f} ms"
                )

            frames = wait_for_frames(
                frame_log,
                lambda items: any(
                    frame.get("type") == "analysis"
                    and frame.get("seq") == e5_revision
                    and "best" in frame
                    and "human" in frame
                    for frame in items
                ),
                timeout=6.0,
            )
            if any(
                frame.get("type") == "analysis"
                and frame.get("seq") == e4_revision
                and "human" in frame
                for frame in frames[e5_position_index + 1 :]
            ):
                raise AssertionError("stale Maia result crossed a newer revision")

            # Repeat the supersession check across Analysis Lab target
            # boundaries.  Both the abandoned Live query and the abandoned
            # root-node query are deliberately still in Maia's slow lane when
            # the next target is selected; neither human result may be merged
            # into the final branch node.
            lab_session = "lane-lab-session"
            lab_boundary = len(load_frames(frame_log))
            start_session(proc, lab_session, "lane-lab-game")
            reply = send_snapshot(
                proc, INITIAL, 1, session_id=lab_session)
            if reply.get("reason") != "game_started":
                raise AssertionError(f"lab lane setup failed: {reply}")

            live_position_index, live_position, _ = first_matching_after(
                frame_log,
                lab_boundary,
                lambda frame: frame.get("type") == "position"
                and frame.get("mode") == "live",
            )
            live_target = live_position["target_revision"]
            _, live_fast, _ = first_matching_after(
                frame_log,
                live_position_index + 1,
                lambda frame: frame.get("type") == "analysis"
                and frame.get("mode") == "live"
                and frame.get("target_revision") == live_target
                and "best" in frame,
            )
            if "human" in live_fast:
                raise AssertionError("slow Maia unexpectedly completed on Live")

            initial_fen = (
                "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/"
                "RNBQKBNR w KQkq - 0 1"
            )
            opened = session_command(
                proc, lab_session, "explore_start", initial_fen)
            if opened.get("reason") != "explore_started":
                raise AssertionError(f"lab lane branch failed: {opened}")
            branch_id = opened["branch_id"]

            root_index, root_position, _ = first_matching_after(
                frame_log,
                live_position_index + 1,
                lambda frame: frame.get("type") == "position"
                and frame.get("mode") == "explore"
                and frame.get("branch_id") == branch_id
                and frame.get("node_id") == 0,
            )
            root_target = root_position["target_revision"]
            first_matching_after(
                frame_log,
                root_index + 1,
                lambda frame: frame.get("type") == "analysis"
                and frame.get("mode") == "explore"
                and frame.get("branch_id") == branch_id
                and frame.get("node_id") == 0
                and frame.get("target_revision") == root_target
                and "best" in frame,
            )

            moved = session_command(
                proc, lab_session, "explore_move", f"{branch_id} 0 e2e4")
            if moved.get("reason") != "explore_move_applied":
                raise AssertionError(f"lab lane move failed: {moved}")
            child_id = moved["node_id"]
            child_index, child_position, _ = first_matching_after(
                frame_log,
                root_index + 1,
                lambda frame: frame.get("type") == "position"
                and frame.get("mode") == "explore"
                and frame.get("branch_id") == branch_id
                and frame.get("node_id") == child_id,
            )
            child_target = child_position["target_revision"]
            _, _, lab_frames = first_matching_after(
                frame_log,
                child_index + 1,
                lambda frame: frame.get("type") == "analysis"
                and frame.get("mode") == "explore"
                and frame.get("branch_id") == branch_id
                and frame.get("node_id") == child_id
                and frame.get("target_revision") == child_target
                and "best" in frame
                and "human" in frame,
                timeout=8.0,
            )

            if any(
                frame.get("type") == "analysis"
                and "human" in frame
                and (
                    frame.get("mode") != "explore"
                    or frame.get("branch_id") != branch_id
                    or frame.get("node_id") != child_id
                    or frame.get("target_revision") != child_target
                )
                for frame in lab_frames[child_index + 1 :]
            ):
                raise AssertionError(
                    "stale Maia result crossed a Live/branch/node target"
                )

            print(
                "PASS: Stockfish first in "
                f"{stockfish_ms:.1f} ms; delayed Maia merged without stale "
                "revision or Analysis Lab target output"
            )
        finally:
            stop_host(proc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
