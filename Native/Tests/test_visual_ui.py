#!/usr/bin/env python3
"""Deterministic offscreen visual audit for every ChessListener UI surface.

This is deliberately a screenshot *and* structure test rather than a brittle
collection of golden images.  Each scenario renders the real :mod:`overlay`
widgets at the narrow, medium, normal, large-text, and workspace sizes used by
the 0.9.5 UI review, writes a PNG, and checks that the scenario's essential
controls are visible, on-screen, and do not overlap. A machine-readable
manifest and an HTML contact sheet make the complete state matrix practical
to inspect with human vision.

Run on a machine with PyQt6 installed::

    QT_QPA_PLATFORM=offscreen \
    CHESSLISTENER_VISUAL_OUTPUT=/tmp/chesslistener-visuals \
        python3 Tests/test_visual_ui.py

Optional ``CHESSLISTENER_VISUAL_BASELINE`` points at a previous output
directory.  Matching PNGs are then compared with a small, configurable pixel
tolerance (``CHESSLISTENER_VISUAL_MAX_DIFF``, default 0.002).  Source-only
environments without PyQt6 skip cleanly, like ``test_overlay.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import html
import json
import os
from pathlib import Path
import queue
import re
import shutil
import sys
import tempfile
import unittest


# These must be fixed before QApplication/overlay are imported.  They isolate
# persistent UI state and pin scale/font metrics for repeatable local renders.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_SCALE_FACTOR", "1")
os.environ.setdefault("QT_FONT_DPI", "96")
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(
    prefix="chesslistener-visual-config-"
)
os.environ["CHESSLISTENER_LIBRARY"] = os.path.join(
    tempfile.mkdtemp(prefix="chesslistener-visual-library-"), "reviews.json"
)

NATIVE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NATIVE_DIR))

try:
    from PyQt6.QtCore import QPoint, QRect, QSettings, Qt, QT_VERSION_STR
    from PyQt6.QtGui import QFont, QImage
    from PyQt6.QtWidgets import (
        QApplication,
        QAbstractButton,
        QAbstractScrollArea,
        QCheckBox,
        QComboBox,
        QLabel,
        QLineEdit,
        QListWidget,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSpinBox,
        QTextEdit,
        QTreeWidget,
        QWidget,
    )
except ImportError:
    print("SKIP visual UI tests: PyQt6 is not installed")
    raise SystemExit(0)

import overlay


START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
VIEWPORTS = (
    ("normal", 420, 820),
    ("medium", 360, 720),
    ("narrow", 320, 620),
    ("large-text", 420, 820),
)
WORKSPACE_VIEWPORT = ("workspace", 920, 720)

INTERACTIVE_TYPES = (
    QAbstractButton,
    QPushButton,
    QComboBox,
    QSpinBox,
    QLineEdit,
    QTextEdit,
    QCheckBox,
    QListWidget,
    QTreeWidget,
    QProgressBar,
    QScrollArea,
)

LABELLED_CONTROL_TYPES = (
    QComboBox,
    QSpinBox,
    QLineEdit,
    QTextEdit,
    QListWidget,
    QTreeWidget,
    QProgressBar,
)


class FakeJob:
    """Non-threaded review job used by explorer loading-state fixtures."""

    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def is_alive(self):
        return not self.cancelled

    def join(self, timeout=None):
        del timeout


def fen_after(moves, initial=START):
    board = overlay.san_rules.Board(initial)
    for move in moves:
        board = board.apply_uci(move)
    return board.fen()


def frame(
    fen=START,
    *,
    mode="live",
    revision=1,
    branch=None,
    node=None,
    final=True,
    dense=False,
):
    """Return a complete, legal native-analysis fixture."""
    if fen == START:
        candidates = [
            ("e2e4", 31, "e2e4 e7e5 g1f3 b8c6 f1b5"),
            ("d2d4", 22, "d2d4 d7d5 c2c4 e7e6 b1c3"),
            ("g1f3", 17, "g1f3 d7d5 g2g3 g8f6 f1g2"),
            ("c2c4", 14, "c2c4 e7e5 b1c3 g8f6 g2g3"),
            ("g2g3", 8, "g2g3 d7d5 f1g2 g8f6 g1f3"),
        ]
        human = "d2d4"
    else:
        # Used after 1.e4 e5 2.Nf3, where Black is to move.
        candidates = [
            ("b8c6", 28, "b8c6 f1b5 a7a6 b5a4 g8f6"),
            ("g8f6", 21, "g8f6 f3e5 d7d6 e5f3 f6e4"),
            ("d7d6", 11, "d7d6 d2d4 e5d4 f3d4 g8f6"),
        ]
        human = "g8f6"
    if not dense:
        candidates = candidates[:3]
    lines = [
        {
            "rank": rank,
            "move": move,
            "cp": cp,
            "depth": 19 if final else 11,
            "bound": "exact" if final else (
                "lowerbound" if rank == 1 else "upperbound"
            ),
            "pv": pv,
            "final": final,
        }
        for rank, (move, cp, pv) in enumerate(candidates, start=1)
    ]
    result = {
        "type": "analysis",
        "mode": mode,
        "target_revision": revision,
        "live_revision": revision,
        "fen": fen,
        "source": "exact",
        "depth": 19 if final else 11,
        "final": final,
        "best": {
            "move": candidates[0][0],
            "cp": candidates[0][1],
            "bound": lines[0]["bound"],
            "pv": candidates[0][2],
        },
        "human": {"move": human},
        "lines": lines,
    }
    if branch is not None:
        result["branch_id"] = branch
        result["node_id"] = node
    return result


def activate_live(window, *, analysis=True, final=True, dense=False):
    """Put an Overlay into the normal active-session surface."""
    window.send_control = lambda _command: None
    window.start_command_sent = True
    for widget in (
        window.turn_dot,
        window.compact_button,
        window.recovery_button,
        window.review_button,
        window.study_button,
        window.settings_button,
    ):
        widget.show()
    window.stack.setCurrentWidget(window.analysis_page)
    window.apply_session({
        "event": "started",
        "session_id": "visual-session",
        "label": "Rapid game · Ada — Grace",
    })
    window.apply_position({
        "type": "position",
        "mode": "live",
        "target_revision": 1,
        "live_revision": 1,
        "seq": 1,
        "fen": START,
        "stm": "w",
        "flip": False,
        "source": "exact",
    })
    if analysis:
        window.apply_analysis(frame(final=final, dense=dense))


def prepare_startup_default(window):
    window.status_label.setText("ChessListener")


def prepare_startup_maximum(window):
    window.startup_budget.setCurrentIndex(window.startup_budget.count() - 1)
    window.startup_maia.setCurrentIndex(0)
    window.remember_check.setChecked(False)
    window.status_label.setText("Settings are stored only on this computer")


def prepare_live_waiting(window):
    activate_live(window, analysis=False)
    window.set_status("Waiting for the first board position…", "info", linger=False)


def prepare_live_searching(window):
    activate_live(window, final=False, dense=True)
    window.explanation_level = "detailed"
    window.line_expansion = "all"
    window.multipv = 5
    window.refresh_candidate_rows()
    window.refresh_explanation()
    window.set_status("Searching locally…", "info", linger=False)


def prepare_live_complete(window):
    activate_live(window, final=True, dense=True)
    window.explanation_level = "detailed"
    window.line_expansion = "selected"
    window.multipv = 5
    window.refresh_candidate_rows()
    window.refresh_explanation()
    window.set_status("Rapid game · Ada — Grace", "info", linger=False)


def prepare_live_engine_error(window):
    activate_live(window, final=True)
    window.set_status(
        "Stockfish stopped unexpectedly — open Recovery to restart it",
        "warn",
        linger=False,
    )


def prepare_live_syncing(window):
    activate_live(window, final=True)
    window.apply_state({
        "type": "state",
        "mode": "live",
        "target_revision": 1,
        "source": "inferred",
        "synchronising": True,
        "text": "Synchronising after a premove…",
    })


def prepare_live_preview(window):
    prepare_live_complete(window)
    window.select_candidate(0)
    window.preview_forward()
    window.preview_forward()
    window.set_status("Previewing Stockfish line", "info", linger=False)


def prepare_live_compact(window):
    activate_live(window, final=True)
    window.toggle_compact()


def set_piece_style(window, style):
    """Apply one glyph family to every board surface in the fixture."""
    if style not in {"outline", "solid"}:
        raise AssertionError(f"unsupported visual-test piece style: {style}")
    combo = getattr(window, "live_piece_style", None)
    if combo is not None:
        index = combo.findData(style)
        if index < 0:
            raise AssertionError(f"Piece style is missing from Settings: {style}")
        combo.setCurrentIndex(index)
    window.piece_style = style
    for name in ("board", "review_board", "study_board"):
        board = getattr(window, name, None)
        if isinstance(board, overlay.BoardView):
            board.set_display_preferences(
                style, getattr(window, "show_coordinates", True)
            )


def prepare_live_outline_pieces(window):
    activate_live(window, final=True)
    set_piece_style(window, "outline")


def prepare_live_solid_pieces(window):
    activate_live(window, final=True)
    set_piece_style(window, "solid")


def prepare_live_shared_origin_arrows(window):
    """Exercise the hardest arrow case: two sources leaving one piece."""
    activate_live(window, final=True)
    window.human_move = "e2e3"
    window.dirty = True


def apply_visual_final_position(window):
    moves = review_moves()
    window.apply_position({
        "type": "position",
        "mode": "live",
        "target_revision": 2,
        "live_revision": 2,
        "seq": 2,
        "fen": fen_after(moves),
        "stm": "w",
        "flip": False,
        "source": "exact",
        "last": moves[-1],
    })


def prepare_game_ended(window):
    activate_live(window, final=True)
    load_review_record(window, result="1-0")
    apply_visual_final_position(window)
    window.apply_session({"event": "ended", "reason": "game_ended"})


def prepare_game_ended_saved(window):
    activate_live(window, final=True)
    load_review_record(window, result="1-0")
    apply_visual_final_position(window)
    window.auto_save_completed = True
    window.apply_session({"event": "ended", "reason": "game_ended"})


def prepare_game_ended_no_history(window):
    activate_live(window, final=True)
    apply_visual_final_position(window)
    window.review_record = None
    window.apply_session({"event": "ended", "reason": "game_ended"})


def enter_lab(window, *, analysed, live_changed=False):
    activate_live(window, final=True)
    path = ["e2e4", "e7e5", "g1f3"]
    branch_fen = fen_after(path)
    window.pending_start_base = START
    window.pending_start_path = list(path)
    window.explore_pending = "start"
    window.apply_explore({
        "type": "explore",
        "event": "started",
        "branch_id": 7,
        "node_id": 3,
        "fen": branch_fen,
        "last": path[-1],
        "stm": "b",
    })
    if analysed:
        window.apply_analysis(frame(
            branch_fen,
            mode="explore",
            revision=1,
            branch=7,
            node=3,
            final=True,
        ))
    else:
        window.set_status("Analysing explored position…", "info", linger=False)
    if live_changed:
        after_e4 = fen_after(["e2e4"])
        window.apply_live_update({
            "type": "live_update",
            "live_revision": 2,
            "fen": after_e4,
            "stm": "b",
            "flip": False,
            "source": "exact",
            "last": "e2e4",
            "synchronising": False,
        })


def prepare_lab_searching(window):
    enter_lab(window, analysed=False)


def prepare_lab_complete(window):
    enter_lab(window, analysed=True, live_changed=True)
    window.explanation_level = "detailed"
    window.refresh_explanation()


def prepare_recovery_default(window):
    activate_live(window, final=True)
    window.open_recovery()
    window.recovery_rescan_button = window.recovery_controls[0]


def prepare_recovery_advanced(window):
    prepare_recovery_default(window)
    window.recovery_advanced.set_expanded(True)


def prepare_recovery_error(window):
    prepare_recovery_advanced(window)
    window.recovery_exact_fen.setText("8/8/8/8/8/8/8/8 w - -")
    window.recovery_error_label.setText(
        "FEN must contain board, turn, castling, en-passant and both counters"
    )
    window.recovery_error_label.show()


def prepare_settings_top(window):
    activate_live(window, final=True)
    window.open_settings()


def prepare_settings_bottom(window):
    prepare_settings_top(window)


def prepare_settings_lab(window):
    prepare_settings_top(window)
    window.settings_lab_group.set_expanded(True)


def prepare_settings_review(window):
    prepare_settings_top(window)
    window.settings_review_group.set_expanded(True)


def prepare_settings_display(window):
    prepare_settings_top(window)
    window.settings_display_group.set_expanded(True)


def prepare_settings_display_bottom(window):
    prepare_settings_display(window)


def prepare_settings_engine(window):
    prepare_settings_top(window)
    window.settings_engine_group.set_expanded(True)


def review_moves():
    return ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "b5a4", "g8f6"]


def load_review_record(window, result="*"):
    moves = review_moves()
    window.apply_game_record({
        "type": "game_record",
        "initial_fen": START,
        "uci_moves": "|".join(moves),
        "move_count": len(moves),
        "result": result,
        "label": "Ada — Grace · imported rapid",
        "metadata": {"White": "Ada", "Black": "Grace", "Event": "Visual audit"},
    })
    return moves


def review_payload(window):
    moves = review_moves()
    classes = [
        "Best", "Excellent", "Good", "Inaccuracy",
        "Best", "Mistake", "Good", "Blunder",
    ]
    losses = [0, 8, 20, 56, 0, 185, 23, 340]
    scores = [31, 24, 42, 96, 35, 220, 141, 410]
    board = overlay.san_rules.Board(START)
    positions = [board.fen()]
    reviews = []
    for index, move in enumerate(moves, start=1):
        before = board.fen()
        notation = board.san(move)
        board = board.apply_uci(move)
        after = board.fen()
        positions.append(after)
        score = scores[index - 1]
        reviews.append({
            "ply": index,
            "uci": move,
            "san": notation,
            "classification": classes[index - 1],
            "loss": losses[index - 1],
            "eval": f"{score / 100:+.2f}",
            "eval_score": {"cp": score, "mate": None},
            "best": move,
            "depth": 18,
            "fen_before": before,
            "fen_after": after,
            "lines": [{
                "rank": 1,
                "depth": 18,
                "cp": score,
                "mate": None,
                "pv": [move],
            }],
        })
    return {"reviews": reviews, "positions": positions}


def prepare_review_empty(window):
    activate_live(window, final=True)
    window.open_review()


def prepare_local_review_empty(window):
    window.send_control = lambda _command: None
    window.start_local_review_mode()


def prepare_review_ready(window):
    activate_live(window, final=True)
    load_review_record(window)
    window.open_review()


def prepare_review_running(window):
    prepare_review_ready(window)
    window.review_progress.setRange(0, 9)
    window.review_progress.setValue(4)
    window.review_progress.show()
    window.review_summary.setText(
        "Reviewing 8 plies locally… 4 of 9 positions complete"
    )
    window.review_start_button.setEnabled(False)
    window.review_start_button.setText("Reviewing…")
    window.review_cancel_button.setEnabled(True)
    window.review_cancel_button.setText("Cancel")


def prepare_review_cancelling(window):
    prepare_review_running(window)
    window.review_job = FakeJob()
    window.invalidate_review_job("Cancelling review…")


def prepare_review_complete(window):
    activate_live(window, final=True)
    load_review_record(window)
    window.review_settings_used = window.current_review_settings()
    window.finish_game_review(review_payload(window))
    window.open_review()
    window.select_review_ply(6)


def prepare_review_failure(window):
    prepare_review_ready(window)
    window.review_progress.setRange(0, 9)
    window.review_progress.setValue(3)
    window.review_summary.setText(
        "Review failed: Stockfish closed before the analysis completed"
    )
    window.review_start_button.setEnabled(True)
    window.review_cancel_button.setEnabled(False)


def prepare_review_explore(window):
    prepare_review_complete(window)
    window.select_review_ply(4)
    original = overlay.review_rules.start_position_analysis
    overlay.review_rules.start_position_analysis = (
        lambda _fen, _settings, _generation: (FakeJob(), queue.Queue())
    )
    try:
        window.toggle_review_explore()
        window.apply_review_explore_move("f1b5")
    finally:
        overlay.review_rules.start_position_analysis = original
    window.review_position_lines = [{
        "rank": 1,
        "depth": 20,
        "cp": 38,
        "mate": None,
        "pv": ["a7a6", "b5a4", "g8f6"],
    }]
    window.show_review_branch_analysis()


def populated_study(window):
    study = overlay.study_rules.new_study(
        "Ruy Lopez ideas",
        START,
        {"Event": "Personal opening notebook"},
    )
    study, e4, _ = overlay.study_rules.add_move(study, "0", "e2e4")
    study, e5, _ = overlay.study_rules.add_move(study, e4, "e7e5")
    study, nf3, _ = overlay.study_rules.add_move(study, e5, "g1f3")
    study, nc6, _ = overlay.study_rules.add_move(study, nf3, "b8c6")
    study, bb5, _ = overlay.study_rules.add_move(study, nc6, "f1b5")
    study, d4, _ = overlay.study_rules.add_move(study, "0", "d2d4")
    study, d5, _ = overlay.study_rules.add_move(study, d4, "d7d5")
    study["nodes"][bb5]["name"] = "Main Ruy Lopez branch"
    study["nodes"][bb5]["comment"] = (
        "Pin the c6 knight, then compare the quiet castle with an immediate "
        "central break."
    )
    study["nodes"][d4]["name"] = "Queen-pawn alternative"
    study["nodes"][d4]["collapsed"] = True
    study["nodes"][bb5]["analysis"] = {
        "depth": 20,
        "final": True,
        "captured_at": 1_700_000_000,
        "lines": [{
            "rank": 1,
            "depth": 20,
            "cp": 41,
            "mate": None,
            "bound": "exact",
            "pv": ["a7a6", "b5a4", "g8f6"],
        }],
    }
    study["selected"] = bb5
    return study, bb5, d5


def prepare_study_empty(window):
    activate_live(window, final=True)
    window.study_auto_analyse = False
    window.open_study()


def prepare_study_populated(window):
    activate_live(window, final=True)
    window.study_auto_analyse = False
    study, selected, _d5 = populated_study(window)
    window.current_study = study
    window.current_study_id = None
    window.study_node_id = selected
    if not window.persist_current_study(refresh_library=True):
        raise AssertionError("visual study fixture did not persist")
    window.open_study()
    window.refresh_study_tree()
    window.select_study_node(selected, analyse=False, persist=False)


def prepare_study_running(window):
    prepare_study_populated(window)
    window.study_analyse_button.setEnabled(False)
    window.study_detail.setText(
        "Analysing this saved position locally… depth 14, three candidate lines"
    )


def prepare_study_failure(window):
    prepare_study_populated(window)
    window.study_detail.setText(
        "Study analysis failed: Stockfish did not return a legal continuation"
    )


def prepare_study_saving(window):
    prepare_study_populated(window)
    current = window.study_comment_edit.toPlainText().rstrip()
    window.study_comment_edit.setPlainText(
        current + "\nSaving-state visual fixture."
    )
    window.study_autosave_timer.stop()


def prepare_study_save_failure(window):
    prepare_study_populated(window)
    current = window.study_comment_edit.toPlainText().rstrip()
    window.study_comment_edit.setPlainText(current + "\nRetained local edit.")
    window.study_autosave_timer.stop()
    window.study_dirty = True
    window.set_study_save_state(
        "Save failed — edits retained here. Try again.", failed=True
    )


def prepare_study_black_orientation(window):
    prepare_study_populated(window)
    window.select_data(window.live_workspace_orientation, "black", 0)
    window.apply_ui_preferences()


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    prepare: object
    required: tuple[str, ...]
    after_layout: object | None = None


def scroll_settings_bottom(window):
    areas = window.settings_page.findChildren(QScrollArea)
    if not areas:
        raise AssertionError("Settings page has no scroll area")
    bar = areas[0].verticalScrollBar()
    bar.setValue(bar.maximum())


def scroll_settings_to(window, name):
    areas = window.settings_page.findChildren(QScrollArea)
    if not areas:
        raise AssertionError("Settings page has no scroll area")
    widget = getattr(window, name)
    areas[0].ensureWidgetVisible(widget, 4, 8)


def scroll_settings_lab(window):
    scroll_settings_to(window, "settings_lab_group")


def scroll_settings_everyday_bottom(window):
    scroll_settings_to(window, "live_explanation")


def scroll_settings_review(window):
    scroll_settings_to(window, "live_review_sensitivity")


def scroll_settings_review_automation(window):
    scroll_settings_to(window, "live_study_snapshots")


def scroll_settings_display(window):
    scroll_settings_to(window, "live_workspace_orientation")


def scroll_settings_display_opacity(window):
    scroll_settings_to(window, "live_opacity")


def scroll_settings_display_arrows(window):
    scroll_settings_to(window, "live_played_arrow")


def scroll_settings_display_bottom(window):
    scroll_settings_to(window, "live_reduced_motion")


def scroll_settings_engine(window):
    scroll_settings_to(window, "settings_engine_group")


def scroll_recovery_advanced(window):
    areas = window.recovery_page.findChildren(QScrollArea)
    if not areas:
        raise AssertionError("Recovery page has no scroll area")
    areas[0].ensureWidgetVisible(window.recovery_side, 4, 8)


def scroll_recovery_fen(window):
    areas = window.recovery_page.findChildren(QScrollArea)
    if not areas:
        raise AssertionError("Recovery page has no scroll area")
    areas[0].ensureWidgetVisible(window.recovery_exact_fen, 4, 8)


def scroll_review_to(window, name):
    window.review_workspace_scroll.ensureWidgetVisible(
        getattr(window, name), 4, 8
    )


def scroll_review_ready(window):
    scroll_review_to(window, "review_start_button")


def scroll_review_running(window):
    scroll_review_to(window, "review_cancel_button")


def scroll_review_detail(window):
    scroll_review_to(window, "review_detail")


def scroll_study_to(window, name):
    window.study_workspace_scroll.ensureWidgetVisible(
        getattr(window, name), 4, 8
    )


def scroll_study_notes(window):
    scroll_study_to(window, "study_comment_edit")


def scroll_study_detail(window):
    scroll_study_to(window, "study_detail")


def scroll_study_save_state(window):
    scroll_study_to(window, "study_save_state_label")


def settle_compact(window):
    if window.compact:
        window.settle_after_compact()


SCENARIOS = (
    Scenario(
        "startup-default", "First-run startup with recommended defaults.",
        prepare_startup_default,
        ("startup_budget", "startup_maia", "remember_check", "start_button"),
    ),
    Scenario(
        "startup-maximum", "Long preset labels, Maia disabled, no persistence.",
        prepare_startup_maximum,
        ("startup_budget", "startup_maia", "remember_check", "start_button"),
    ),
    Scenario(
        "live-waiting", "Active game before any engine result exists.",
        prepare_live_waiting,
        ("board", "eval_bar", "live_toolbar", "recovery_button"),
    ),
    Scenario(
        "live-searching", "Streaming bounds, five lines, detailed explanation.",
        prepare_live_searching,
        ("board", "eval_bar", "candidate_scroll", "live_toolbar"),
    ),
    Scenario(
        "live-complete", "Dense final live analysis with Maia and PV details.",
        prepare_live_complete,
        ("board", "eval_bar", "candidate_scroll", "live_toolbar"),
    ),
    Scenario(
        "live-outline-pieces", "Both sides use the outline glyph family.",
        prepare_live_outline_pieces,
        ("board", "eval_bar", "candidate_scroll"),
    ),
    Scenario(
        "live-solid-pieces", "Both sides use solid silhouettes.",
        prepare_live_solid_pieces,
        ("board", "eval_bar", "candidate_scroll"),
    ),
    Scenario(
        "live-shared-origin-arrows",
        "Stockfish and Maia arrows leave the same source square.",
        prepare_live_shared_origin_arrows,
        ("board", "eval_bar", "candidate_scroll"),
    ),
    Scenario(
        "live-engine-error", "Recoverable engine failure in the title status.",
        prepare_live_engine_error,
        ("board", "eval_bar", "recovery_button"),
    ),
    Scenario(
        "live-syncing", "Retained analysis while board recovery catches up.",
        prepare_live_syncing,
        ("board", "eval_bar", "source_label", "recovery_button"),
    ),
    Scenario(
        "live-pv-preview", "Read-only follow-up preview two plies ahead.",
        prepare_live_preview,
        ("board", "eval_bar", "preview_label", "explore_button"),
    ),
    Scenario(
        "live-compact", "Minimal always-on-top analysis strip.",
        prepare_live_compact,
        ("eval_bar", "best_label", "compact_button"),
        settle_compact,
    ),
    Scenario(
        "live-game-ended", "Completed game with unsaved post-game actions.",
        prepare_game_ended,
        (
            "board", "game_finished_panel", "postgame_save_button",
            "postgame_review_button", "postgame_explore_button",
            "postgame_export_button", "postgame_auto_save",
        ),
    ),
    Scenario(
        "live-game-ended-saved", "Automatically saved completed-game state.",
        prepare_game_ended_saved,
        (
            "board", "game_finished_panel", "postgame_save_button",
            "postgame_review_button", "postgame_auto_save",
        ),
    ),
    Scenario(
        "live-game-ended-no-history",
        "Completed board without a verified move history.",
        prepare_game_ended_no_history,
        (
            "board", "game_finished_panel", "game_finished_detail",
            "postgame_save_button", "postgame_review_button",
        ),
    ),
    Scenario(
        "lab-searching", "Analysis Lab branch before its first result.",
        prepare_lab_searching,
        ("board", "eval_bar", "explore_toolbar", "breadcrumb_label"),
    ),
    Scenario(
        "lab-live-changed", "Completed branch analysis with a live-game update.",
        prepare_lab_complete,
        ("board", "eval_bar", "explore_toolbar", "live_update_button"),
    ),
    Scenario(
        "recovery-default", "Primary recovery path with repair tools collapsed.",
        prepare_recovery_default,
        ("recovery_rescan_button", "recovery_advanced"),
    ),
    Scenario(
        "recovery-advanced", "Expanded metadata repair without an error.",
        prepare_recovery_advanced,
        ("recovery_side", "castle_white_king"),
        scroll_recovery_advanced,
    ),
    Scenario(
        "recovery-error", "Invalid full FEN with actionable validation text.",
        prepare_recovery_error,
        ("recovery_exact_fen",),
        scroll_recovery_fen,
    ),
    Scenario(
        "settings-top", "Everyday analysis choices at the scroll origin.",
        prepare_settings_top,
        ("live_budget", "live_multipv", "live_maia"),
    ),
    Scenario(
        "settings-everyday-bottom",
        "Remaining everyday follow and explanation choices.",
        prepare_settings_bottom,
        ("live_follow", "live_explanation"),
        scroll_settings_everyday_bottom,
    ),
    Scenario(
        "settings-lab", "Expanded Analysis Lab settings.",
        prepare_settings_lab,
        ("settings_lab_group", "live_explore_budget", "live_pv_length"),
        scroll_settings_lab,
    ),
    Scenario(
        "settings-review", "Local Review strength and classification choices.",
        prepare_settings_review,
        (
            "live_review_strength", "live_review_lines",
            "live_review_sensitivity",
        ),
        scroll_settings_review,
    ),
    Scenario(
        "settings-review-automation",
        "Completed-game, Review, and Study automation choices.",
        prepare_settings_review,
        (
            "live_auto_save_completed", "live_review_auto",
            "live_study_auto", "live_study_snapshots",
        ),
        scroll_settings_review_automation,
    ),
    Scenario(
        "settings-display", "Evaluation, piece, and workspace board choices.",
        prepare_settings_display,
        (
            "live_eval_pov", "live_piece_style",
            "live_workspace_orientation",
        ),
        scroll_settings_display,
    ),
    Scenario(
        "settings-display-opacity", "Whole-window opacity control.",
        prepare_settings_display,
        ("live_opacity",),
        scroll_settings_display_opacity,
    ),
    Scenario(
        "settings-display-arrows", "Engine, Maia, and played-move marks.",
        prepare_settings_display,
        ("live_best_arrow", "live_human_arrow", "live_played_arrow"),
        scroll_settings_display_arrows,
    ),
    Scenario(
        "settings-display-bottom", "Remaining board and window preferences.",
        prepare_settings_display_bottom,
        (
            "live_coordinates", "live_always_on_top",
            "live_remember_compact", "live_reduced_motion",
        ),
        scroll_settings_display_bottom,
    ),
    Scenario(
        "settings-engine", "Expanded advanced engine controls.",
        prepare_settings_engine,
        ("settings_engine_group", "live_threads"),
        scroll_settings_engine,
    ),
    Scenario(
        "review-empty", "Local Review before a game or position is imported.",
        prepare_review_empty,
        (
            "review_import_button", "review_library_combo",
            "review_empty_panel", "review_close_button",
        ),
    ),
    Scenario(
        "review-local-empty", "Standalone local mode without Firefox.",
        prepare_local_review_empty,
        (
            "review_import_button", "review_library_combo",
            "review_empty_panel", "review_close_button",
        ),
    ),
    Scenario(
        "review-ready", "Verified record ready to be reviewed.",
        prepare_review_ready,
        ("review_summary", "review_start_button"),
        scroll_review_ready,
    ),
    Scenario(
        "review-running", "Partially completed local review with cancellation.",
        prepare_review_running,
        ("review_summary", "review_progress", "review_cancel_button"),
        scroll_review_running,
    ),
    Scenario(
        "review-cancelling", "Review cancellation is immediately visible.",
        prepare_review_cancelling,
        ("review_cancel_button", "review_summary"),
        scroll_review_running,
    ),
    Scenario(
        "review-complete", "Full result, graph, classifications, and explanation.",
        prepare_review_complete,
        ("review_board", "review_graph"),
    ),
    Scenario(
        "review-complete-detail",
        "Reviewed-move classification and local explanation.",
        prepare_review_complete,
        ("review_moves", "review_detail"),
        scroll_review_detail,
    ),
    Scenario(
        "review-failure", "Recoverable Stockfish failure after partial work.",
        prepare_review_failure,
        ("review_summary", "review_start_button"),
        scroll_review_ready,
    ),
    Scenario(
        "review-explore", "Independent continuation from a historical ply.",
        prepare_review_explore,
        ("review_explore_button", "review_undo_button", "review_detail"),
        scroll_review_detail,
    ),
    Scenario(
        "study-empty", "Saved Studies library with no studies yet.",
        prepare_study_empty,
        (
            "study_search", "study_library_combo", "study_empty_panel",
            "study_new_button",
        ),
    ),
    Scenario(
        "study-populated", "Named branches, notes, snapshots, and tree navigation.",
        prepare_study_populated,
        ("study_board", "study_tree"),
    ),
    Scenario(
        "study-populated-notes", "Editable study and branch annotations.",
        prepare_study_populated,
        ("study_title_edit", "study_name_edit", "study_comment_edit"),
        scroll_study_notes,
    ),
    Scenario(
        "study-black-orientation",
        "Saved workspace held from Black's side while Live remains unchanged.",
        prepare_study_black_orientation,
        ("study_board",),
    ),
    Scenario(
        "study-running", "Saved-position snapshot analysis in progress.",
        prepare_study_running,
        ("study_detail",),
        scroll_study_detail,
    ),
    Scenario(
        "study-failure", "Inline failure while annotations remain editable.",
        prepare_study_failure,
        ("study_detail",),
        scroll_study_detail,
    ),
    Scenario(
        "study-saving", "Debounced annotation save in progress.",
        prepare_study_saving,
        ("study_comment_edit", "study_save_state_label"),
        scroll_study_save_state,
    ),
    Scenario(
        "study-save-failure", "Annotation save failure retains the visible edit.",
        prepare_study_save_failure,
        ("study_comment_edit", "study_save_state_label"),
        scroll_study_save_state,
    ),
)


def widget_rect_in_window(widget, window):
    origin = widget.mapTo(window, QPoint(0, 0))
    return QRect(origin, widget.size())


def painted_board_geometry(board, window):
    origin = board.mapTo(window, QPoint(0, 0))
    rect = board.board_rect()
    return [
        round(origin.x() + rect.x(), 2),
        round(origin.y() + rect.y(), 2),
        round(rect.width(), 2),
        round(rect.height(), 2),
    ]


def ancestor_of_type(widget, kind):
    parent = widget.parentWidget()
    while parent is not None:
        if isinstance(parent, kind):
            return parent
        parent = parent.parentWidget()
    return None


def widget_name(widget):
    if widget.objectName():
        return f"{type(widget).__name__}#{widget.objectName()}"
    if hasattr(widget, "text"):
        text = str(widget.text()).replace("\n", " ").strip()
        if text:
            return f"{type(widget).__name__}({text[:48]!r})"
    if isinstance(widget, QComboBox):
        return f"QComboBox({widget.currentText()[:48]!r})"
    return type(widget).__name__


def flatten_required(window, names):
    widgets = []
    missing = []
    for name in names:
        value = getattr(window, name, None)
        if value is None:
            missing.append(name)
            continue
        values = value if isinstance(value, (tuple, list)) else (value,)
        for widget in values:
            if isinstance(widget, QWidget):
                widgets.append(widget)
            else:
                missing.append(name)
    return widgets, missing


def has_accessible_label(widget, page):
    if str(widget.accessibleName()).strip():
        return True
    if isinstance(widget, QAbstractButton) and str(widget.text()).strip():
        return True
    return any(label.buddy() is widget for label in page.findChildren(QLabel))


def audit_structure(window, scenario):
    """Return fatal contract failures and softer visual-audit warnings."""
    fatal = []
    warnings = []
    bounds = window.rect()
    required, missing = flatten_required(window, scenario.required)
    required_ids = {id(widget) for widget in required}
    for name in missing:
        fatal.append(f"missing required widget {name}")

    required_rects = []
    for widget in required:
        label = widget_name(widget)
        if not widget.isVisibleTo(window):
            if (
                isinstance(widget, overlay.TitleAction)
                and window.overflow_button.isVisibleTo(window)
            ):
                # Narrow chrome intentionally routes hidden destinations into
                # the accessible "More destinations" menu.
                continue
            fatal.append(f"required widget hidden: {label}")
            continue
        rect = widget_rect_in_window(widget, window)
        if rect.width() <= 0 or rect.height() <= 0:
            fatal.append(f"required widget has empty geometry: {label}")
            continue
        intersection = rect.intersected(bounds)
        if intersection != rect:
            fatal.append(
                f"required widget leaves window: {label} "
                f"at {rect.x()},{rect.y()} {rect.width()}x{rect.height()}"
            )
        required_rects.append((widget, rect, label))

    # Essential controls must never occupy the same pixels.  Ancestor/child
    # pairs are excluded because a composite control legitimately contains its
    # viewport or editor.
    for index, (first, first_rect, first_name) in enumerate(required_rects):
        for second, second_rect, second_name in required_rects[index + 1:]:
            if first.isAncestorOf(second) or second.isAncestorOf(first):
                continue
            overlap = first_rect.intersected(second_rect)
            if overlap.width() > 2 and overlap.height() > 2:
                fatal.append(
                    f"required widgets overlap: {first_name} / {second_name}"
                )

    page = window.stack.currentWidget()
    controls = page.findChildren(INTERACTIVE_TYPES) if page is not None else []
    # Include the top title bar controls, which are outside the page stack.
    controls.extend(window.title_bar.findChildren(INTERACTIVE_TYPES))
    seen = set()
    visible_controls = []
    for widget in controls:
        if id(widget) in seen:
            continue
        seen.add(id(widget))
        if not widget.isVisibleTo(window):
            continue
        if (
            isinstance(widget, QLineEdit)
            and widget.objectName() == "qt_spinbox_lineedit"
            and ancestor_of_type(widget, QSpinBox) is not None
        ):
            # QSpinBox owns this private editor; the labelled spin box is the
            # accessible control and is audited separately.
            continue
        rect = widget_rect_in_window(widget, window)
        scroll = ancestor_of_type(widget, QAbstractScrollArea)
        if scroll is not None:
            viewport = scroll.viewport()
            viewport_rect = widget_rect_in_window(viewport, window)
            if rect.intersected(viewport_rect).isEmpty():
                # This control is merely above or below the intentionally
                # scrollable viewport; it is not a clipping defect.
                continue
        visible_region = widget.visibleRegion().boundingRect()
        if visible_region.isEmpty():
            warnings.append(f"fully clipped control: {widget_name(widget)}")
            continue
        visible_controls.append((widget, rect))
        if rect.intersected(bounds) != rect:
            warnings.append(f"partly offscreen control: {widget_name(widget)}")
        if visible_region != widget.rect():
            warnings.append(f"ancestor-clipped control: {widget_name(widget)}")

        # Accessibility is part of the visual contract: every action needs a
        # spoken name and keyboard focus, and every data control needs either
        # an accessible name or a real QLabel buddy.
        if isinstance(widget, QAbstractButton):
            if not has_accessible_label(widget, page):
                fatal.append(f"unnamed button: {widget_name(widget)}")
            if (
                widget.isEnabled()
                and widget.focusPolicy() == Qt.FocusPolicy.NoFocus
            ):
                fatal.append(f"keyboard-inaccessible button: {widget_name(widget)}")
            if window.title_bar.isAncestorOf(widget) and (
                widget.width() < 32 or widget.height() < 32
            ):
                fatal.append(
                    f"title action target below 32px: {widget_name(widget)} "
                    f"is {widget.width()}x{widget.height()}"
                )
            if widget.objectName() == "primary" and widget.height() < 40:
                fatal.append(
                    f"primary action target below 40px: {widget_name(widget)} "
                    f"is {widget.width()}x{widget.height()}"
                )
        elif isinstance(widget, LABELLED_CONTROL_TYPES):
            if not has_accessible_label(widget, page):
                problem = f"unlabelled control: {widget_name(widget)}"
                if id(widget) in required_ids:
                    fatal.append(problem)
                else:
                    warnings.append(problem)

        # Spot labels that cannot plausibly fit.  This is advisory: platform
        # font fallback can move the boundary by a few pixels.
        metrics = widget.fontMetrics()
        if (
            isinstance(widget, QPushButton)
            and widget.text()
            and widget.objectName() != "titleButton"
            and len(widget.text().strip()) > 2
        ):
            needed = metrics.horizontalAdvance(widget.text()) + 12
            if needed > widget.width():
                warnings.append(
                    f"button text may clip: {widget_name(widget)} "
                    f"needs {needed}px, has {widget.width()}px"
                )
        elif isinstance(widget, QComboBox) and widget.currentText():
            needed = metrics.horizontalAdvance(widget.currentText()) + 30
            if needed > widget.width():
                warnings.append(
                    f"combo text may clip: {widget_name(widget)} "
                    f"needs {needed}px, has {widget.width()}px"
                )

    # Page scroll areas must reflow horizontally. A hidden horizontal bar can
    # still mean content is being silently clipped, so inspect its range too.
    if page is not None:
        for area in page.findChildren(QScrollArea):
            if not area.isVisibleTo(window):
                continue
            bar = area.horizontalScrollBar()
            if bar.maximum() - bar.minimum() > 2:
                fatal.append(
                    f"horizontal overflow in {widget_name(area)}: "
                    f"range {bar.minimum()}..{bar.maximum()}"
                )

        visible_boards = [
            board for board in page.findChildren(overlay.BoardView)
            if board.isVisibleTo(window)
        ]
        for board in visible_boards:
            if not str(board.accessibleName()).strip():
                fatal.append("visible board has no accessible name")
            if len(board.grid) != 64:
                fatal.append(
                    f"board model has {len(board.grid)} cells instead of 64"
                )
            painted = board.board_rect()
            if (
                painted.width() <= 0
                or painted.width() != painted.height()
                or int(painted.width()) % 8 != 0
            ):
                fatal.append(
                    "board does not expose an integral 8 × 8 square grid"
                )
            if board.piece_style not in {"outline", "solid"}:
                fatal.append(f"invalid board piece style {board.piece_style!r}")
            expected_style = getattr(window, "piece_style", board.piece_style)
            if board.piece_style != expected_style:
                fatal.append(
                    f"mixed piece families: window={expected_style!r}, "
                    f"board={board.piece_style!r}"
                )

            local_boards = {
                getattr(window, "review_board", None),
                getattr(window, "study_board", None),
            }
            if board in local_boards:
                expected_override = {
                    "white": False, "black": True,
                }.get(getattr(window, "workspace_orientation", "follow"))
                if board.orientation_override != expected_override:
                    fatal.append(
                        "workspace orientation mismatch: "
                        f"expected {expected_override!r}, got "
                        f"{board.orientation_override!r}"
                    )
            elif board is getattr(window, "board", None):
                if board.orientation_override is not None:
                    fatal.append(
                        "live board inherited a local workspace orientation"
                    )
            for move_name in ("best_move", "human_move", "last_move"):
                move = str(getattr(board, move_name, "") or "")
                if move and re.fullmatch(
                    r"[a-h][1-8][a-h][1-8][qrbn]?", move
                ) is None:
                    fatal.append(
                        f"{move_name} cannot stay inside the board: {move!r}"
                    )

        if page is not getattr(window, "analysis_page", None):
            source = getattr(window, "source_label", None)
            if isinstance(source, QWidget) and source.isVisibleTo(window):
                fatal.append("non-analysis workspace retained the live source badge")

    for label_name in ("page_title_label", "status_label"):
        label = getattr(window, label_name, None)
        if not isinstance(label, QLabel) or not label.isVisibleTo(window):
            continue
        text = str(label.text()).strip()
        if not text or label.wordWrap():
            continue
        needed = label.fontMetrics().horizontalAdvance(text)
        if needed <= label.width():
            continue
        problem = (
            f"title text clips: {widget_name(label)} needs {needed}px, "
            f"has {label.width()}px"
        )
        if label_name == "page_title_label":
            fatal.append(problem)
        else:
            warnings.append(problem)

    return fatal, sorted(set(warnings)), len(visible_controls)


def image_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def image_difference(actual_path, expected_path, channel_tolerance=2):
    """Return fraction of pixels with a visible RGBA channel difference."""
    actual = QImage(str(actual_path)).convertToFormat(
        QImage.Format.Format_RGBA8888
    )
    expected = QImage(str(expected_path)).convertToFormat(
        QImage.Format.Format_RGBA8888
    )
    if actual.isNull() or expected.isNull():
        return 1.0
    if actual.size() != expected.size():
        return 1.0
    first_bits = actual.bits()
    second_bits = expected.bits()
    first_bits.setsize(actual.sizeInBytes())
    second_bits.setsize(expected.sizeInBytes())
    first = bytes(first_bits)
    second = bytes(second_bits)
    changed = 0
    pixels = actual.width() * actual.height()
    for offset in range(0, min(len(first), len(second)), 4):
        if any(
            abs(first[offset + channel] - second[offset + channel])
            > channel_tolerance
            for channel in range(4)
        ):
            changed += 1
    return changed / max(1, pixels)


def write_contact_sheet(output_dir, entries):
    cards = []
    for entry in entries:
        issues = entry["fatal"] + entry["warnings"]
        issue_html = "".join(
            f"<li>{html.escape(issue)}</li>" for issue in issues
        ) or "<li class='ok'>No structural findings</li>"
        cards.append(
            f"<article data-scenario='{html.escape(entry['scenario'])}' "
            f"data-viewport='{html.escape(entry['viewport'])}' "
            f"data-has-issues='{str(bool(issues)).lower()}'>"
            f"<h2>{html.escape(entry['scenario'])} · "
            f"{html.escape(entry['viewport'])}</h2>"
            f"<p>{html.escape(entry['description'])}</p>"
            f"<a href='{html.escape(entry['file'])}'><img loading='lazy' "
            f"src='{html.escape(entry['file'])}' alt='Screenshot'></a>"
            f"<ul>{issue_html}</ul>"
            "</article>"
        )
    document = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ChessListener visual state matrix</title>
<style>
:root { color-scheme: dark; font-family: system-ui, sans-serif; }
body { margin: 0; padding: 24px; background: #111318; color: #e8ebf0; }
header { position: sticky; z-index: 2; top: 0; max-width: 1100px; margin: -24px auto 24px; padding: 20px 0 14px; background: #111318f2; border-bottom: 1px solid #343841; }
main { display: grid; grid-template-columns: repeat(auto-fit,minmax(340px,1fr)); gap: 20px; }
article { min-width: 0; padding: 14px; border: 1px solid #343841; border-radius: 12px; background: #1b1e23; }
article[hidden] { display: none; }
h1 { margin: 0 0 8px; } h2 { font-size: 16px; margin: 0 0 6px; }
p, li { color: #aeb6c4; font-size: 13px; } .ok { color: #9fe0b5; }
img { display: block; width: auto; max-width: 100%; height: auto; margin: 12px auto; box-shadow: 0 8px 24px #0008; }
ul { margin: 8px 0 0; padding-left: 20px; }
nav { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 12px; }
input[type="search"], select, label { min-height: 34px; border: 1px solid #465047; border-radius: 5px; padding: 6px 9px; background: #20241f; color: #e8ebf0; font: inherit; }
label { display: inline-flex; align-items: center; gap: 6px; }
label input { min-height: auto; }
#visible-count { margin-left: auto; color: #aeb6c4; font-size: 12px; }
</style></head><body><header><h1>ChessListener UI state matrix</h1>
<p>Real offscreen PyQt widgets. Open an image for its full-resolution render.</p>
<nav aria-label="Screenshot filters">
<input id="scenario-filter" type="search" placeholder="Filter scenarios" aria-label="Filter scenarios">
<select id="viewport-filter" aria-label="Filter viewport"><option value="">All viewports</option>
<option>normal</option><option>medium</option><option>narrow</option><option>large-text</option><option>workspace</option></select>
<label><input id="issues-filter" type="checkbox"> Findings only</label>
<span id="visible-count" aria-live="polite"></span>
</nav></header><main>""" + "".join(cards) + """</main>
<script>
const cards = [...document.querySelectorAll('article')];
const scenario = document.querySelector('#scenario-filter');
const viewport = document.querySelector('#viewport-filter');
const issues = document.querySelector('#issues-filter');
const count = document.querySelector('#visible-count');
function filterCards() {
  const query = scenario.value.trim().toLowerCase();
  let shown = 0;
  for (const card of cards) {
    const visible = (!query || card.dataset.scenario.includes(query)) &&
      (!viewport.value || card.dataset.viewport === viewport.value) &&
      (!issues.checked || card.dataset.hasIssues === 'true');
    card.hidden = !visible;
    shown += visible ? 1 : 0;
  }
  count.textContent = `${shown} of ${cards.length} renders`;
}
scenario.addEventListener('input', filterCards);
viewport.addEventListener('change', filterCards);
issues.addEventListener('change', filterCards);
filterCards();
</script></body></html>\n"""
    (output_dir / "index.html").write_text(document, encoding="utf-8")


def load_popup_visual_entries(output_dir):
    """Load the optional deterministic Playwright companion matrix."""
    path = output_dir / "popup-visual-manifest.json"
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [{
            "scenario": "popup-manifest",
            "description": "Firefox popup visual companion",
            "viewport": "normal",
            "requested_size": [320, 0],
            "actual_size": [0, 0],
            "page": "Firefox popup",
            "file": "",
            "sha256": "",
            "visible_controls": 0,
            "font_pixel_height": 0,
            "live_board_geometry": None,
            "fatal": [f"could not read popup visual manifest: {error}"],
            "warnings": [],
            "baseline_diff": None,
        }]
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return []
    accepted = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        filename = str(entry.get("file", ""))
        normalized = {
            "scenario": str(entry.get("scenario", "popup-unknown")),
            "description": str(entry.get("description", "Firefox popup")),
            "viewport": str(entry.get("viewport", "normal")),
            "requested_size": list(entry.get("requested_size") or [320, 0]),
            "actual_size": list(entry.get("actual_size") or [0, 0]),
            "page": "Firefox popup",
            "file": filename,
            "sha256": str(entry.get("sha256", "")),
            "visible_controls": int(entry.get("visible_controls", 0)),
            "font_pixel_height": float(entry.get("font_pixel_height", 0)),
            "live_board_geometry": None,
            "fatal": [str(value) for value in entry.get("fatal", [])],
            "warnings": [str(value) for value in entry.get("warnings", [])],
            "baseline_diff": None,
        }
        if not filename or not (output_dir / filename).is_file():
            normalized["fatal"].append(
                f"missing popup screenshot {filename or '(unnamed)'}"
            )
        accepted.append(normalized)
    return accepted


class VisualUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setApplicationName("ChessListener Visual Audit")
        cls.app.setStyle("Fusion")
        cls.base_font = QFont(cls.app.font())
        configured = os.environ.get("CHESSLISTENER_VISUAL_OUTPUT", "").strip()
        cls.output_dir = Path(configured) if configured else Path(
            tempfile.mkdtemp(prefix="chesslistener-visual-output-")
        )
        cls.output_dir.mkdir(parents=True, exist_ok=True)
        baseline = os.environ.get("CHESSLISTENER_VISUAL_BASELINE", "").strip()
        cls.baseline_dir = Path(baseline) if baseline else None
        try:
            cls.max_diff = float(
                os.environ.get("CHESSLISTENER_VISUAL_MAX_DIFF", "0.002")
            )
        except ValueError:
            cls.max_diff = 0.002

    def clean_persistence(self):
        settings = QSettings(overlay.ORGANIZATION, overlay.APPLICATION)
        settings.clear()
        settings.sync()
        try:
            os.unlink(os.environ["CHESSLISTENER_LIBRARY"])
        except FileNotFoundError:
            pass

    def render_one(self, scenario, viewport_name, width, height, ordinal):
        self.clean_persistence()
        font = QFont(self.base_font)
        if viewport_name == "large-text":
            base_size = font.pointSizeF()
            font.setPointSizeF(max(12.0, base_size * 1.35))
        self.app.setFont(font)
        window = overlay.Overlay()
        if viewport_name == "large-text":
            # The production stylesheet establishes explicit pixel sizes, so
            # changing QApplication's font alone cannot exercise text zoom.
            # Scale every QSS font declaration as well, before the fixture is
            # laid out, to make clipping/reflow failures real and repeatable.
            window.setStyleSheet(re.sub(
                r"font-size:\s*(\d+)px",
                lambda match: (
                    f"font-size: {max(int(match.group(1)) + 1, round(int(match.group(1)) * 1.4))}px"
                ),
                window.styleSheet(),
            ))
        window.send_control = lambda _command: None
        try:
            scenario.prepare(window)
            window.resize(width, height)
            window.show()
            self.app.processEvents()
            window.layout().activate()
            window.stack.updateGeometry()
            if scenario.after_layout is not None:
                scenario.after_layout(window)
                window.layout().activate()
                self.app.processEvents()

            # State reducers intentionally repaint on a timer in production.
            # A visual fixture instead snaps animation to its target and paints
            # synchronously so two runs produce the same image.
            if window.dirty:
                window.dirty = False
                window.repaint_state()
            window.eval_bar.display_fraction = window.eval_bar.target_fraction
            window.eval_bar.update()
            for timer_name in (
                "frame_timer", "status_timer", "settings_timer",
                "geometry_timer", "review_timer", "review_position_timer",
                "study_position_timer",
            ):
                getattr(window, timer_name).stop()
            self.app.processEvents()

            fatal, warnings, visible_controls = audit_structure(window, scenario)
            if scenario.name != "live-compact" and (
                window.width() != width or window.height() != height
            ):
                fatal.append(
                    f"window did not honor requested {width}x{height}; "
                    f"actual size is {window.width()}x{window.height()}"
                )
            filename = f"{ordinal:02d}-{scenario.name}--{viewport_name}.png"
            path = self.output_dir / filename
            if not window.grab().save(str(path), "PNG"):
                fatal.append("Qt failed to save screenshot")
            baseline_diff = None
            if self.baseline_dir is not None:
                expected = self.baseline_dir / filename
                if expected.exists():
                    baseline_diff = image_difference(path, expected)
                    if baseline_diff > self.max_diff:
                        fatal.append(
                            f"pixel regression {baseline_diff:.4%} exceeds "
                            f"{self.max_diff:.4%}"
                        )
                else:
                    fatal.append(f"missing baseline image {filename}")
            return {
                "scenario": scenario.name,
                "description": scenario.description,
                "viewport": viewport_name,
                "requested_size": [width, height],
                "actual_size": [window.width(), window.height()],
                "page": window.stack.currentWidget().objectName()
                or type(window.stack.currentWidget()).__name__,
                "file": filename,
                "sha256": image_sha256(path) if path.exists() else "",
                "visible_controls": visible_controls,
                "font_pixel_height": window.fontMetrics().height(),
                "live_board_geometry": (
                    painted_board_geometry(window.board, window)
                    if window.board.isVisibleTo(window)
                    else None
                ),
                "fatal": fatal,
                "warnings": warnings,
                "baseline_diff": baseline_diff,
            }
        finally:
            window.cancel_game_review()
            if window.review_position_job is not None:
                window.review_position_job.cancel()
            window.review_position_timer.stop()
            window.review_position_job = None
            window.review_position_queue = None
            window.cancel_study_analysis()
            window.close()
            window.deleteLater()
            self.app.processEvents()
            self.app.setFont(self.base_font)

    def test_complete_visual_state_matrix(self):
        selected = {
            value.strip()
            for value in os.environ.get(
                "CHESSLISTENER_VISUAL_SCENARIOS", ""
            ).split(",")
            if value.strip()
        }
        scenarios = [
            (index, scenario)
            for index, scenario in enumerate(SCENARIOS, start=1)
            if not selected or scenario.name in selected
        ]
        self.assertTrue(scenarios, "No visual scenarios matched the filter")

        entries = []
        for ordinal, scenario in scenarios:
            viewports = list(VIEWPORTS)
            if scenario.name.startswith(("review-", "study-")):
                viewports.append(WORKSPACE_VIEWPORT)
            for viewport_name, width, height in viewports:
                with self.subTest(
                    scenario=scenario.name, viewport=viewport_name
                ):
                    entries.append(self.render_one(
                        scenario, viewport_name, width, height, ordinal
                    ))

        by_scenario_viewport = {
            (entry["scenario"], entry["viewport"]): entry
            for entry in entries
        }
        for _ordinal, scenario in scenarios:
            normal = by_scenario_viewport.get((scenario.name, "normal"))
            enlarged = by_scenario_viewport.get((scenario.name, "large-text"))
            if normal is None or enlarged is None:
                continue
            if enlarged["font_pixel_height"] <= normal["font_pixel_height"]:
                enlarged["fatal"].append(
                    "large-text font metrics did not exceed the normal render"
                )
            if enlarged["sha256"] == normal["sha256"]:
                enlarged["fatal"].append(
                    "large-text screenshot is pixel-identical to normal"
                )

        # Waiting, streaming, and first complete results must use the exact
        # same board allocation. Otherwise the overlay jumps when analysis
        # begins, which is especially disruptive beside a live game.
        for viewport_name, _width, _height in VIEWPORTS:
            waiting = by_scenario_viewport.get(("live-waiting", viewport_name))
            if waiting is None or waiting["live_board_geometry"] is None:
                continue
            for state_name in ("live-searching", "live-complete"):
                state = by_scenario_viewport.get((state_name, viewport_name))
                if state is None or state["live_board_geometry"] is None:
                    continue
                if state["live_board_geometry"] != waiting["live_board_geometry"]:
                    state["fatal"].append(
                        "live board geometry changed from waiting "
                        f"{waiting['live_board_geometry']} to "
                        f"{state['live_board_geometry']}"
                    )

        entries.extend(load_popup_visual_entries(self.output_dir))

        manifest = {
            "schema": 1,
            "app_version": overlay.APP_VERSION,
            "protocol_version": overlay.PROTOCOL_VERSION,
            "qt_version": QT_VERSION_STR,
            "platform": os.environ.get("QT_QPA_PLATFORM", ""),
            "viewports": {
                name: [width, height]
                for name, width, height in (*VIEWPORTS, WORKSPACE_VIEWPORT)
            },
            "entries": entries,
        }
        (self.output_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        write_contact_sheet(self.output_dir, entries)

        if os.environ.get("CHESSLISTENER_VISUAL_UPDATE_BASELINE") == "1":
            if self.baseline_dir is None:
                self.fail("UPDATE_BASELINE requires CHESSLISTENER_VISUAL_BASELINE")
            self.baseline_dir.mkdir(parents=True, exist_ok=True)
            for entry in entries:
                shutil.copy2(
                    self.output_dir / entry["file"],
                    self.baseline_dir / entry["file"],
                )
            shutil.copy2(
                self.output_dir / "manifest.json",
                self.baseline_dir / "manifest.json",
            )

        failures = [
            f"{entry['scenario']} ({entry['viewport']}): {problem}"
            for entry in entries
            for problem in entry["fatal"]
        ]
        print(
            f"Visual UI renders: {len(entries)} PNGs in {self.output_dir}"
        )
        if os.environ.get("CHESSLISTENER_VISUAL_STRICT", "1") != "0":
            self.assertFalse(failures, "\n" + "\n".join(failures))


if __name__ == "__main__":
    unittest.main(verbosity=2)
