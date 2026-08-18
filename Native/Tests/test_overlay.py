#!/usr/bin/env python3
"""Headless reducer and interaction coverage for Analysis Lab.

Run with ``QT_QPA_PLATFORM=offscreen python3 Tests/test_overlay.py`` on an
installed PyQt6 runtime. Source-only CI environments without Qt skip cleanly;
syntax is still covered by the normal Makefile target.
"""

import os
import sys
import tempfile
import unittest
import queue
import json
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="chesslistener-qt-")
os.environ["CHESSLISTENER_LIBRARY"] = os.path.join(
    tempfile.mkdtemp(prefix="chesslistener-library-"), "reviews.json"
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from PyQt6.QtCore import QPoint, QSettings, Qt
    from PyQt6.QtGui import QCloseEvent
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
except ImportError:
    print("SKIP overlay interaction tests: PyQt6 is not installed")
    raise SystemExit(0)

import overlay


START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
AFTER_E5 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2"
AFTER_D4 = "rnbqkbnr/pppppppp/8/8/3P4/8/PPP1PPPP/RNBQKBNR b KQkq d3 0 1"


def analysis_frame(mode="live", revision=1, branch=None, node=None):
    frame = {
        "type": "analysis",
        "mode": mode,
        "target_revision": revision,
        "live_revision": 1,
        "fen": START,
        "source": "exact",
        "depth": 16,
        "best": {"move": "e2e4", "cp": 31, "pv": "e2e4 e7e5 g1f3 b8c6"},
        "human": {"move": "e2e4"},
        "lines": [
            {"move": "e2e4", "cp": 31, "depth": 16,
             "pv": "e2e4 e7e5 g1f3 b8c6"},
            {"move": "d2d4", "cp": 18, "depth": 16,
             "pv": "d2d4 d7d5 c2c4"},
        ],
    }
    if branch is not None:
        frame["branch_id"] = branch
        frame["node_id"] = node
    return frame


class OverlayLabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        settings = QSettings(overlay.ORGANIZATION, overlay.APPLICATION)
        settings.clear()
        settings.sync()
        try:
            os.unlink(os.environ["CHESSLISTENER_LIBRARY"])
        except FileNotFoundError:
            pass
        self.window = overlay.Overlay()
        self.commands = []
        self.window.send_control = self.commands.append
        self.window.start_command_sent = True
        self.window.apply_session({"event": "started", "session_id": "s", "label": "Game"})
        self.window.apply_position({
            "type": "position", "mode": "live", "target_revision": 1,
            "live_revision": 1, "seq": 1, "fen": START, "stm": "w",
            "flip": False, "source": "exact",
        })
        self.window.apply_analysis(analysis_frame())

    def tearDown(self):
        self.window.deleteLater()

    def start_explore(self, path=()):
        self.window.pending_start_base = START
        self.window.pending_start_path = list(path)
        self.window.explore_pending = "start"
        fen = START if not path else AFTER_E5
        self.window.apply_explore({
            "type": "explore", "event": "started", "branch_id": 7,
            "node_id": len(path), "fen": fen,
            "last": path[-1] if path else None,
        })

    def test_corrupt_library_is_preserved_and_surfaces_without_startup_crash(self):
        self.window.deleteLater()
        path = Path(os.environ["CHESSLISTENER_LIBRARY"])
        original = b'{"version":2,"games":[{"id":"truncated"}'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(original)

        self.window = overlay.Overlay()

        self.assertIn("Local library unavailable", self.window.status_text)
        self.assertIn("left unchanged", self.window.status_text)
        self.assertEqual(
            self.window.review_library_combo.currentText(),
            "Library unavailable — file preserved",
        )
        self.window.open_review()
        self.window.open_study()
        self.assertEqual(path.read_bytes(), original)

    def test_existing_empty_json_archive_retires_stale_legacy_snapshot(self):
        self.window.deleteLater()
        path = Path(os.environ["CHESSLISTENER_LIBRARY"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"version":2,"games":[],"studies":[]}', encoding="utf-8"
        )
        settings = QSettings(overlay.ORGANIZATION, overlay.APPLICATION)
        settings.setValue("review/latest", json.dumps({
            "record": {"initial_fen": START, "moves": []},
            "reviews": [],
            "positions": [START],
        }))
        settings.sync()

        self.window = overlay.Overlay()

        self.assertIsNone(self.window.review_record)
        self.assertFalse(settings.contains("review/latest"))
        self.assertEqual(path.read_text(encoding="utf-8"),
                         '{"version":2,"games":[],"studies":[]}')

    def test_string_loss_cache_is_preserved_and_cannot_crash_startup_timeline(self):
        self.window.deleteLater()
        path = Path(os.environ["CHESSLISTENER_LIBRARY"])
        path.parent.mkdir(parents=True, exist_ok=True)
        score = {"rank": 1, "depth": 12, "cp": 15, "mate": None, "pv": []}
        result = {
            "ply": 1,
            "uci": "e2e4",
            "san": "e4",
            "classification": "Best",
            "loss": "0",
            "eval": "+0.15",
            "eval_score": score,
            "best": "e2e4",
            "depth": 12,
            "lines": [score],
            "fen_before": START,
            "fen_after": AFTER_E4,
        }
        original = json.dumps({
            "version": 2,
            "games": [{
                "id": "string-loss",
                "initial_fen": START,
                "moves": ["e2e4"],
                "reviews": {"broken": {
                    "settings": {},
                    "results": [result],
                    "positions": [START, AFTER_E4],
                    "position_analyses": [],
                    "created_at": 1,
                }},
            }],
            "studies": [],
        }, separators=(",", ":")).encode("utf-8")
        path.write_bytes(original)

        self.window = overlay.Overlay()

        self.assertIn("Local library unavailable", self.window.status_text)
        self.assertIn("cached-result loss", self.window.status_text)
        self.assertIsNone(self.window.review_record)
        self.assertEqual(path.read_bytes(), original)

    def test_review_graph_hover_and_keyboard_describe_points(self):
        graph = overlay.ReviewGraph()
        graph.resize(320, 90)
        graph.set_values(
            [0, 31, -42],
            current=1,
            points=[
                {},
                {"san": "e4", "classification": "Best", "loss": 0},
                {"san": "e5", "classification": "Inaccuracy", "loss": 73},
            ],
        )
        graph.show()
        self.app.processEvents()
        try:
            QTest.mouseMove(graph, QPoint(graph.width() - 5, graph.height() // 2))
            self.app.processEvents()
            self.assertEqual(graph.hovered, 2)
            self.assertIn("Move 2: e5", graph.toolTip())
            self.assertIn("Inaccuracy", graph.accessibleDescription())

            selected = []
            graph.selected.connect(selected.append)
            graph.setFocus()
            QTest.keyClick(graph, Qt.Key.Key_Home)
            self.assertEqual(selected[-1], 0)
            graph.set_current(2)
            self.assertIn("Selected Move 2: e5", graph.accessibleDescription())
        finally:
            graph.close()
            graph.deleteLater()

    def test_candidate_selection_preview_and_explore_here_tree(self):
        self.window.select_candidate(0)
        self.window.preview_forward()
        self.window.preview_forward()
        self.assertEqual(self.window.fen, AFTER_E5)
        self.window.start_explore()
        self.assertEqual(
            self.commands[-1], f"EXPLORE_START s {START}|e2e4,e7e5"
        )

        self.window.apply_explore({
            "event": "started", "branch_id": 7, "node_id": 2,
            "fen": AFTER_E5, "last": "e7e5",
        })
        self.assertEqual(self.window.explore_root_node_id, 0)
        self.assertEqual(self.window.explore_nodes[2]["parent"], 1)
        self.assertEqual(self.window.last_san, "e5")
        self.window.update_breadcrumb()
        self.assertIn("e4", self.window.breadcrumb_label.text())
        self.assertIn("e5", self.window.breadcrumb_label.text())
        self.window.explore_undo()
        self.assertEqual(self.commands[-1], "EXPLORE_GOTO s 7 1")
        self.window.explore_pending = ""
        self.window.explore_root()
        self.assertEqual(self.commands[-1], "EXPLORE_GOTO s 7 0")

    def test_target_identity_and_live_update_isolation(self):
        self.start_explore()
        self.window.apply_position({
            "mode": "explore", "target_revision": 2, "fen": START,
            "branch_id": 7, "node_id": 0, "source": "exact",
        })
        self.window.apply_analysis(analysis_frame("explore", 2, 99, 0))
        self.assertEqual(self.window.best_move, "")
        self.window.apply_analysis(analysis_frame("explore", 2, 7, 1))
        self.assertEqual(self.window.best_move, "")
        self.window.apply_analysis(analysis_frame("explore", 1, 7, 0))
        self.assertEqual(self.window.best_move, "")
        wrong_fen = analysis_frame("explore", 2, 7, 0)
        wrong_fen["fen"] = AFTER_E4
        self.window.apply_analysis(wrong_fen)
        self.assertEqual(self.window.best_move, "")
        self.window.apply_analysis(analysis_frame("explore", 2, 7, 0))
        self.assertEqual(self.window.best_move, "e2e4")

        self.window.apply_position({
            "mode": "explore", "target_revision": "bad", "fen": START,
            "branch_id": 7, "node_id": 0,
        })
        self.assertEqual(self.window.fen, START)

        displayed = self.window.fen
        self.window.follow_live = "auto"
        self.window.apply_live_update({
            "type": "live_update", "live_revision": 1, "fen": START,
            "stm": "w", "flip": True, "source": "exact",
            "synchronising": True,
        })
        self.assertEqual(self.window.live_update_count, 0)
        self.assertFalse(any(command.startswith("EXPLORE_LIVE") for command in self.commands))
        self.window.follow_live = "notify"
        self.window.apply_live_update({
            "type": "live_update", "live_revision": 2, "fen": AFTER_E4,
            "stm": "b", "flip": False, "source": "exact",
            "last": "e2e4", "synchronising": False,
        })
        self.assertEqual(self.window.fen, displayed)
        self.assertEqual(self.window.live_snapshot["fen"], AFTER_E4)
        self.assertFalse(any(command.startswith("EXPLORE_LIVE") for command in self.commands))

        self.window.apply_live_update({
            "type": "live_update", "live_revision": 2, "fen": AFTER_E4,
            "stm": "b", "flip": True, "source": "inferred",
            "synchronising": True,
        })
        self.assertEqual(self.window.live_update_count, 1)
        self.assertEqual(self.window.live_snapshot["last_san"], "e4")

        self.window.follow_live = "auto"
        self.window.apply_live_update({
            "type": "live_update", "live_revision": 3, "fen": AFTER_E5,
            "stm": "w", "flip": False, "source": "exact",
            "last": "e7e5", "synchronising": False,
        })
        self.assertEqual(self.commands[-1], "EXPLORE_LIVE s 7")

    def test_live_move_cancels_read_only_preview_and_session_resets_tree(self):
        self.window.select_candidate(0)
        self.window.preview_forward()
        self.assertEqual(self.window.preview_step, 1)
        self.window.apply_position({
            "mode": "live", "target_revision": 1, "live_revision": 1,
            "seq": 1, "fen": START, "stm": "w", "flip": True,
            "source": "exact",
        })
        self.assertEqual(self.window.preview_step, 1)
        self.assertTrue(self.window.flip)
        self.window.apply_position({
            "mode": "live", "target_revision": 2, "live_revision": 2,
            "seq": 2, "fen": AFTER_E4, "stm": "b", "flip": False,
            "last": "e2e4", "source": "exact",
        })
        self.assertEqual(self.window.preview_step, 0)
        self.assertEqual(self.window.fen, AFTER_E4)

        self.start_explore()
        self.window.apply_session({"event": "started", "session_id": "new"})
        self.assertEqual(self.window.mode, "live")
        self.assertEqual(self.window.explore_nodes, {})
        self.assertIsNone(self.window.resume_branch_id)

    def test_promotion_targets_and_maia_off(self):
        fen = "7k/P7/8/8/8/8/8/K7 w - - 0 1"
        grid, side = overlay.fen_to_grid(fen)
        self.window.board.set_position(grid, side, False, fen)
        self.window.board.set_interactive(True)
        captured = []
        self.window.board.moveRequested.connect(captured.append)
        origin = overlay.square_index("a7")
        target = overlay.square_index("a8")
        self.assertTrue(self.window.board.select_square(origin))
        self.assertTrue(self.window.board.request_target(target))
        self.assertEqual(set(captured[-1]), {"a7a8q", "a7a8r", "a7a8b", "a7a8n"})

        statuses = []
        self.window.set_status = lambda text, kind="info", linger=True: statuses.append((text, kind))
        self.window.maia_rating = 0
        self.window.apply_ready({"stockfish": True, "maia": False})
        self.assertEqual(statuses[-1][0], "Engines ready")

    def test_reset_defaults(self):
        self.window.live_budget.setCurrentIndex(0)
        self.window.live_explore_budget.setCurrentIndex(3)
        self.window.live_multipv.setValue(5)
        self.window.live_maia.setCurrentIndex(0)
        self.window.live_pv_length.setCurrentIndex(4)
        self.window.live_follow.setCurrentIndex(1)
        self.window.live_explanation.setCurrentIndex(0)
        self.window.live_eval_pov.setCurrentIndex(1)
        self.window.live_line_expansion.setCurrentIndex(1)
        self.window.live_best_arrow.setChecked(False)
        self.window.live_study_auto.setChecked(False)
        self.window.live_study_snapshots.setChecked(False)
        self.window.live_opacity.setCurrentIndex(3)
        self.commands.clear()
        self.window.reset_settings_defaults()
        self.assertEqual(self.window.budget_ms, 400)
        self.assertEqual(self.window.explore_budget, -1)
        self.assertEqual(self.window.multipv, 3)
        self.assertEqual(self.window.maia_rating, 1900)
        self.assertEqual(self.window.pv_display_length, 6)
        self.assertEqual(self.window.follow_live, "notify")
        self.assertEqual(self.window.explanation_level, "compact")
        self.assertEqual(self.window.eval_pov, "white")
        self.assertEqual(self.window.line_expansion, "selected")
        self.assertTrue(self.window.show_best_arrow)
        self.assertTrue(self.window.study_auto_analyse)
        self.assertTrue(self.window.study_save_evals)
        self.assertEqual(self.window.opacity_percent, 100)
        self.assertTrue(self.commands[-1].startswith("SET budget=400"))

    def test_local_review_record_timeline_and_settings(self):
        self.window.apply_game_record({
            "type": "game_record", "initial_fen": START,
            "uci_moves": "e2e4|e7e5", "move_count": 2, "result": "*",
        })
        self.assertTrue(self.window.review_button.isEnabled())
        self.assertEqual(self.window.review_record["moves"], ["e2e4", "e7e5"])
        self.window.review_settings_used = self.window.current_review_settings()
        self.window.finish_game_review({
            "reviews": [
                {"ply": 1, "uci": "e2e4", "san": "e4",
                 "classification": "Best", "loss": 0, "eval": "+0.20",
                 "best": "e2e4", "depth": 18,
                 "fen_before": START, "fen_after": AFTER_E4,
                 "lines": [{"rank": 1, "depth": 18, "cp": 20,
                            "mate": None, "pv": ["e2e4", "e7e5"]}]},
                {"ply": 2, "uci": "e7e5", "san": "e5",
                 "classification": "Mistake", "loss": 190, "eval": "+2.10",
                 "best": "c7c5", "depth": 18,
                 "fen_before": AFTER_E4, "fen_after": AFTER_E5,
                 "lines": [{"rank": 1, "depth": 18, "cp": 210,
                            "mate": None, "pv": ["c7c5", "g1f3"]}]},
            ],
            "positions": [START, AFTER_E4, AFTER_E5],
        })
        self.assertEqual(self.window.review_moves.count(), 2)
        self.assertIn("1 turning point", self.window.review_summary.text())
        self.assertFalse(
            QSettings(overlay.ORGANIZATION, overlay.APPLICATION).value(
                "review/latest", ""
            )
        )
        self.assertEqual(len(self.window.review_store.list_games()), 1)
        original_review_start = overlay.review_rules.start_review
        overlay.review_rules.start_review = lambda *_args, **_kwargs: self.fail(
            "identical review should load from cache"
        )
        try:
            self.window.start_game_review()
        finally:
            overlay.review_rules.start_review = original_review_start
        self.assertIn("local cache", self.window.review_summary.text())
        self.window.select_review_move(1)
        self.assertIn("Stockfish preferred", self.window.review_detail.text())
        self.assertEqual(self.window.review_board.fen, AFTER_E5)

        self.window.live_review_strength.setCurrentIndex(2)
        self.window.live_review_lines.setValue(4)
        self.window.live_review_auto.setChecked(True)
        self.window.apply_ui_preferences()
        self.assertEqual(self.window.review_time_ms, 800)
        self.assertEqual(self.window.review_lines, 4)
        self.assertTrue(self.window.review_auto)

        self.assertEqual(len(self.window.review_graph.values), 3)
        self.window.select_review_ply(1)
        self.assertEqual(self.window.review_selected_ply, 1)
        self.window.review_forward()
        self.assertEqual(self.window.review_selected_ply, 2)
        self.window.review_back()
        self.assertEqual(self.window.review_selected_ply, 1)

        self.window.review_filter.setCurrentIndex(
            self.window.review_filter.findData("major")
        )
        self.window.refresh_review_timeline()
        self.assertEqual(self.window.review_visible_rows, [])
        self.assertEqual(self.window.review_moves.count(), 0)

        class FakeJob:
            def is_alive(self):
                return True
            def cancel(self):
                pass

        original = overlay.review_rules.start_position_analysis
        overlay.review_rules.start_position_analysis = (
            lambda _fen, _settings, _generation: (FakeJob(), __import__("queue").Queue())
        )
        try:
            self.window.select_review_ply(1)
            self.window.toggle_review_explore()
            self.assertEqual(self.window.review_mode, "explore")
            self.assertTrue(self.window.review_board.interactive)
            self.window.apply_review_explore_move("e7e5")
            self.assertEqual(self.window.review_branch, ["e7e5"])
            self.window.review_explore_undo()
            self.assertEqual(self.window.review_branch, [])
            self.window.leave_review_explore()
            self.assertEqual(self.window.review_mode, "game")
        finally:
            overlay.review_rules.start_position_analysis = original

    def test_late_review_result_is_ignored_after_game_switch(self):
        class FakeJob:
            def __init__(self):
                self.alive = True
                self.cancelled = False

            def is_alive(self):
                return self.alive

            def cancel(self):
                self.cancelled = True

        output = queue.Queue()
        job = FakeJob()
        captured = {}
        original = overlay.review_rules.start_review

        def start(_fen, _moves, _settings, identity):
            captured["identity"] = dict(identity)
            return job, output

        overlay.review_rules.start_review = start
        try:
            self.window.apply_game_record({
                "initial_fen": START, "uci_moves": "e2e4", "result": "*"
            })
            self.window.start_game_review()
            old_identity = captured["identity"]
            self.window.apply_game_record({
                "initial_fen": START, "uci_moves": "d2d4", "result": "*"
            })
            self.assertTrue(job.cancelled)
            output.put({
                "type": "complete",
                "review_identity": old_identity,
                "reviews": [{"ply": 1, "classification": "Blunder"}],
                "positions": [START, AFTER_E4],
                "position_analyses": [],
            })
            job.alive = False
            self.window.poll_review()
        finally:
            overlay.review_rules.start_review = original

        self.assertEqual(self.window.review_record["moves"], ["d2d4"])
        self.assertEqual(self.window.review_results, [])
        self.assertIsNone(self.window.review_job)
        current_id = overlay.study_store.record_id(self.window.review_record)
        self.assertIsNone(
            self.window.review_store.cached_review(
                current_id,
                overlay.study_store.settings_key(self.window.current_review_settings()),
            )
        )

    def test_legacy_review_migrates_once_and_deleted_game_stays_deleted(self):
        legacy = {
            "record": {
                "initial_fen": START,
                "moves": ["e2e4"],
                "result": "*",
                "label": "Legacy local review",
                "metadata": {"Event": "Legacy fixture"},
                "imported": True,
            },
            "reviews": [],
            "positions": [START, AFTER_E4],
            "position_analyses": [],
        }
        settings = QSettings(overlay.ORGANIZATION, overlay.APPLICATION)
        settings.setValue("review/latest", __import__("json").dumps(legacy))
        settings.sync()

        self.window.restore_review_archive()
        self.assertEqual(len(self.window.review_store.list_games()), 1)
        self.assertEqual(self.window.review_record["label"], "Legacy local review")
        self.assertFalse(settings.value("review/latest", ""))

        # Simulate a stale duplicate left by an older release and prove that
        # the explicit library deletion retires it as well.
        settings.setValue("review/latest", __import__("json").dumps(legacy))
        settings.sync()
        original_question = overlay.QMessageBox.question
        overlay.QMessageBox.question = lambda *_args, **_kwargs: (
            overlay.QMessageBox.StandardButton.Yes
        )
        try:
            self.window.delete_library_selection()
        finally:
            overlay.QMessageBox.question = original_question
        self.assertEqual(self.window.review_store.list_games(), [])
        self.assertFalse(settings.value("review/latest", ""))

        replacement = overlay.Overlay()
        replacement.send_control = lambda _command: None
        try:
            self.assertIsNone(replacement.review_record)
            self.assertEqual(replacement.review_store.list_games(), [])
        finally:
            replacement.deleteLater()

    def test_late_review_result_is_ignored_after_game_deletion(self):
        class FakeJob:
            def __init__(self):
                self.alive = True

            def is_alive(self):
                return self.alive

            def cancel(self):
                pass

        output = queue.Queue()
        job = FakeJob()
        captured = {}
        original_start = overlay.review_rules.start_review
        original_question = overlay.QMessageBox.question
        overlay.review_rules.start_review = (
            lambda _fen, _moves, _settings, identity: (
                captured.setdefault("identity", dict(identity)) and job,
                output,
            )
        )
        overlay.QMessageBox.question = lambda *_args, **_kwargs: (
            overlay.QMessageBox.StandardButton.Yes
        )
        try:
            self.window.apply_game_record({
                "initial_fen": START, "uci_moves": "e2e4", "result": "*"
            })
            identifier = self.window.review_store.save_record(self.window.review_record)
            self.window.review_game_id = identifier
            self.window.populate_review_library(identifier)
            self.window.start_game_review()
            self.window.delete_library_selection()
            output.put({
                "type": "complete",
                "review_identity": captured["identity"],
                "reviews": [{"ply": 1, "classification": "Best"}],
                "positions": [START, AFTER_E4],
            })
            job.alive = False
            self.window.poll_review()
        finally:
            overlay.review_rules.start_review = original_start
            overlay.QMessageBox.question = original_question
        self.assertIsNone(self.window.review_record)
        self.assertEqual(self.window.review_results, [])
        self.assertEqual(self.window.review_store.list_games(), [])

    def test_completed_game_auto_save_is_deduplicated_and_review_is_independent(self):
        self.window.auto_save_completed = True
        self.window.review_auto = False
        review_calls = []
        original_open = self.window.open_review_for_completed_game
        self.window.open_review_for_completed_game = (
            lambda run=False: review_calls.append(run) or True
        )
        try:
            self.window.apply_position({
                "type": "position", "mode": "live", "target_revision": 2,
                "live_revision": 2, "seq": 2, "fen": AFTER_E5,
                "stm": "w", "flip": False, "source": "exact",
                "last": "e7e5",
            })
            self.window.apply_game_record({
                "initial_fen": START,
                "uci_moves": "e2e4|e7e5",
                "result": "1-0",
                "history_complete": True,
            })
            self.window.apply_session({"event": "ended", "reason": "game_end"})
            self.window.apply_session({"event": "ended", "reason": "game_end"})
            games = self.window.review_store.list_games()
            self.assertEqual(len(games), 1)
            self.assertEqual(games[0]["result"], "1-0")
            self.assertTrue(games[0]["completed"])
            self.assertTrue(games[0]["history_complete"])
            self.assertEqual(
                self.window.completed_game_record["session_id"], "s"
            )
            self.assertEqual(review_calls, [])

            self.window.apply_session({
                "event": "started", "session_id": "second", "label": "Second"
            })
            self.window.apply_position({
                "type": "position", "mode": "live", "target_revision": 3,
                "live_revision": 3, "seq": 3, "fen": AFTER_D4,
                "stm": "b", "flip": False, "source": "exact",
                "last": "d2d4",
            })
            self.window.apply_game_record({
                "initial_fen": START, "uci_moves": "d2d4", "result": "*"
            })
            self.window.auto_save_completed = False
            self.window.review_auto = True
            self.window.apply_session({"event": "ended", "reason": "completed"})
            self.assertEqual(review_calls, [True])
            self.assertEqual(len(self.window.review_store.list_games()), 1)
        finally:
            self.window.open_review_for_completed_game = original_open

    def test_deleting_completed_review_reenables_postgame_save(self):
        self.window.auto_save_completed = True
        self.window.apply_position({
            "type": "position", "mode": "live", "target_revision": 2,
            "live_revision": 2, "seq": 2, "fen": AFTER_E5,
            "stm": "w", "flip": False, "source": "exact",
            "last": "e7e5",
        })
        self.window.apply_game_record({
            "initial_fen": START,
            "uci_moves": "e2e4|e7e5",
            "result": "1-0",
            "history_complete": True,
        })
        self.window.apply_session({"event": "ended", "reason": "game_end"})
        identifier = self.window.completed_game_id
        self.assertTrue(identifier)
        self.assertTrue(self.window.completed_game_saved)
        self.assertFalse(self.window.postgame_save_button.isEnabled())
        self.assertTrue(self.window.open_review_for_completed_game(run=False))

        original_question = overlay.QMessageBox.question
        overlay.QMessageBox.question = lambda *_args, **_kwargs: (
            overlay.QMessageBox.StandardButton.Yes
        )
        try:
            self.window.delete_library_selection()
        finally:
            overlay.QMessageBox.question = original_question

        self.assertIsNone(self.window.review_store.find(identifier))
        self.assertIsNone(self.window.completed_game_id)
        self.assertFalse(self.window.completed_game_saved)
        self.assertTrue(self.window.postgame_save_button.isEnabled())
        self.assertEqual(self.window.postgame_save_button.text(), "Save game")

    def test_game_end_uses_current_position_when_move_history_is_unavailable(self):
        self.window.auto_save_completed = True
        self.window.review_auto = False
        self.assertIsNone(self.window.live_game_record)
        self.window.apply_position({
            "type": "position", "mode": "live", "target_revision": 2,
            "live_revision": 2, "seq": 2, "fen": AFTER_E4, "stm": "b",
            "flip": False, "source": "exact", "last": "e2e4",
        })

        self.window.apply_session({
            "event": "ended", "reason": "game_end", "result": "not-a-result"
        })

        record = self.window.completed_game_record
        self.assertIsNotNone(record)
        self.assertEqual(record["session_id"], "s")
        self.assertEqual(record["initial_fen"], AFTER_E4)
        self.assertEqual(record["moves"], [])
        self.assertEqual(record["result"], "*")
        self.assertEqual(record["label"], "Final position only")
        self.assertTrue(record["position_only"])
        self.assertFalse(record["history_complete"])
        self.assertEqual(record["source"], "exact")
        self.assertTrue(self.window.completed_game_saved)
        saved = self.window.review_store.find(self.window.completed_game_id)
        self.assertTrue(saved["position_only"])
        self.assertFalse(saved["history_complete"])
        self.assertEqual(saved["source"], "exact")
        completed_identifier = self.window.completed_game_id
        exported = overlay.review_rules.annotated_pgn(
            record["initial_fen"], record["moves"], result=record["result"],
            metadata=record["metadata"],
        )
        self.assertIn('[SetUp "1"]', exported)
        self.assertIn(f'[FEN "{AFTER_E4}"]', exported)

        # A one-position record is still a useful local review/explore root.
        self.assertTrue(self.window.open_review_for_completed_game(run=False))
        self.assertEqual(self.window.review_positions, [AFTER_E4])
        self.assertTrue(self.window.completed_game_saved)
        self.assertEqual(self.window.completed_game_id, completed_identifier)

    def test_game_end_restores_authoritative_board_from_preview_and_lab(self):
        # Preview can be one or more hypothetical plies ahead. The completed
        # verified record and the board shown in Finished must both return to
        # the real final position.
        self.window.apply_position({
            "type": "position", "mode": "live", "target_revision": 2,
            "live_revision": 2, "seq": 2, "fen": AFTER_E4, "stm": "b",
            "flip": False, "source": "exact", "last": "e2e4",
        })
        self.window.apply_game_record({
            "initial_fen": START, "uci_moves": "e2e4", "result": "*",
            "history_complete": True,
        })
        self.window.preview_root_fen = AFTER_E4
        self.window.preview_root_grid, self.window.preview_root_side = (
            overlay.fen_to_grid(AFTER_E4)
        )
        self.window.preview_moves = ["e7e5"]
        self.window.preview_step = 1
        self.window.apply_preview_step()
        self.assertEqual(self.window.fen, AFTER_E5)
        self.window.apply_session({
            "event": "ended", "reason": "game_end", "result": "1-0"
        })
        self.window.flush_frame()
        self.assertEqual(self.window.fen, AFTER_E4)
        self.assertEqual(self.window.board.fen, AFTER_E4)
        self.assertEqual(self.window.completed_game_record["moves"], ["e2e4"])
        self.assertEqual(self.window.completed_game_record["result"], "1-0")

        # Start a second session whose live truth is kept separately while a
        # private Analysis Lab continuation owns the visible board.
        self.window.apply_session({
            "event": "started", "session_id": "lab-end", "label": "Lab end"
        })
        self.window.apply_position({
            "type": "position", "mode": "live", "target_revision": 3,
            "live_revision": 3, "seq": 3, "fen": AFTER_E4, "stm": "b",
            "flip": True, "source": "exact", "last": "e2e4",
        })
        self.window.apply_game_record({
            "initial_fen": START, "uci_moves": "e2e4", "result": "*",
            "history_complete": True,
        })
        live_grid, live_side = overlay.fen_to_grid(AFTER_E4)
        self.window.live_snapshot = {
            "fen": AFTER_E4, "grid": live_grid, "side": live_side,
            "flip": True, "last": "e2e4", "last_san": "e4",
            "source": "exact", "synchronising": False, "revision": 3,
        }
        lab_grid, lab_side = overlay.fen_to_grid(AFTER_E5)
        self.window.mode = "explore"
        self.window.fen = AFTER_E5
        self.window.grid = lab_grid
        self.window.side_to_move = lab_side
        self.window.board.set_position(lab_grid, lab_side, False, AFTER_E5)
        self.window.apply_session({"event": "ended", "reason": "game_end"})
        self.window.flush_frame()
        self.assertEqual(self.window.fen, AFTER_E4)
        self.assertEqual(self.window.board.fen, AFTER_E4)
        self.assertTrue(self.window.flip)
        self.assertTrue(self.window.board.flip)

    def test_game_end_rejects_stale_same_session_history_prefix(self):
        self.window.apply_position({
            "type": "position", "mode": "live", "target_revision": 2,
            "live_revision": 2, "seq": 2, "fen": AFTER_E5, "stm": "w",
            "flip": False, "source": "exact", "last": "e7e5",
        })
        # Legal and session-bound, but one ply behind the authoritative board.
        self.window.apply_game_record({
            "initial_fen": START, "uci_moves": "e2e4", "result": "1-0",
            "history_complete": True,
        })
        self.window.apply_session({"event": "ended", "reason": "game_end"})
        self.window.flush_frame()
        record = self.window.completed_game_record
        self.assertTrue(record["position_only"])
        self.assertFalse(record["history_complete"])
        self.assertEqual(record["initial_fen"], AFTER_E5)
        self.assertEqual(record["moves"], [])
        self.assertEqual(self.window.fen, AFTER_E5)
        self.assertEqual(self.window.board.fen, AFTER_E5)

    def test_auto_save_failure_warning_survives_game_end(self):
        self.window.apply_position({
            "type": "position", "mode": "live", "target_revision": 2,
            "live_revision": 2, "seq": 2, "fen": AFTER_E4, "stm": "b",
            "flip": False, "source": "exact", "last": "e2e4",
        })
        self.window.apply_game_record({
            "initial_fen": START, "uci_moves": "e2e4", "result": "*",
            "history_complete": True,
        })
        self.window.auto_save_completed = True
        original_save = self.window.review_store.save_completed_game

        def fail_save(_record):
            raise OSError("read-only test library")

        self.window.review_store.save_completed_game = fail_save
        try:
            self.window.apply_session({"event": "ended", "reason": "game_end"})
        finally:
            self.window.review_store.save_completed_game = original_save
        self.assertFalse(self.window.completed_game_saved)
        self.assertIn("Could not save completed game", self.window.status_text)
        self.assertEqual(self.window.status_label.objectName(), "statusWarn")

    def test_postgame_actions_are_bound_only_to_the_ending_live_session(self):
        self.window.apply_imported_record({
            "initial_fen": START,
            "moves": ["e2e4"],
            "result": "*",
            "label": "Previously imported",
            "metadata": {"Event": "Old game"},
        })
        old_identifier = self.window.review_game_id
        self.assertIsNotNone(old_identifier)
        self.window.auto_save_completed = True
        self.window.review_auto = True
        review_calls = []
        original_open = self.window.open_review_for_completed_game
        self.window.open_review_for_completed_game = (
            lambda run=False: review_calls.append(run) or True
        )
        try:
            # The old imported record remains selected, but it must never be
            # mistaken for the new browser session when no history arrives.
            self.window.apply_session({
                "event": "started", "session_id": "missing-history",
                "label": "New game",
            })
            self.assertEqual(self.window.review_record["label"], "Previously imported")
            self.window.fen = "not a valid FEN"
            self.window.apply_session({"event": "ended", "reason": "game_end"})
            self.assertIsNone(self.window.completed_game_record)
            self.assertFalse(self.window.completed_game_saved)
            self.assertFalse(self.window.save_completed_game())
            self.assertEqual(review_calls, [])
            self.assertEqual(
                [game["id"] for game in self.window.review_store.list_games()],
                [old_identifier],
            )

            # Once a verified record arrives for the active session, the end
            # actions and idempotent autosave are scoped to that exact session.
            self.window.apply_session({
                "event": "started", "session_id": "current-session",
                "label": "Current game",
            })
            self.window.apply_position({
                "type": "position", "mode": "live", "target_revision": 2,
                "live_revision": 2, "seq": 2, "fen": AFTER_D4,
                "stm": "b", "flip": False, "source": "exact",
                "last": "d2d4",
            })
            self.window.apply_game_record({
                "initial_fen": START,
                "uci_moves": "d2d4",
                "result": "1/2-1/2",
                "history_complete": True,
            })
            self.assertEqual(
                self.window.live_game_record["session_id"], "current-session"
            )
            self.window.apply_session({"event": "ended", "reason": "completed"})
            self.assertEqual(
                self.window.completed_game_record["session_id"], "current-session"
            )
            self.assertEqual(self.window.completed_game_record["moves"], ["d2d4"])
            self.assertTrue(self.window.completed_game_saved)
            self.assertEqual(review_calls, [True])
            saved = self.window.review_store.find(self.window.completed_game_id)
            self.assertEqual(saved["session_id"], "current-session")
            self.assertTrue(saved["completed"])
            self.assertEqual(len(self.window.review_store.list_games()), 2)
        finally:
            self.window.open_review_for_completed_game = original_open

    def test_completed_fallback_save_keeps_review_model_and_selection_paired(self):
        self.window.apply_imported_record({
            "initial_fen": START, "moves": ["e2e4"], "result": "*",
            "label": "Older imported game", "metadata": {"Event": "Old"},
        })
        old_identifier = self.window.review_game_id
        self.window.apply_session({
            "event": "started", "session_id": "fresh", "label": "Fresh game"
        })
        self.window.apply_position({
            "type": "position", "mode": "live", "target_revision": 2,
            "live_revision": 2, "seq": 2, "fen": AFTER_E4, "stm": "b",
            "flip": False, "source": "exact", "last": "e2e4",
        })
        self.window.auto_save_completed = True
        self.window.apply_session({"event": "ended", "reason": "game_end"})
        completed_identifier = self.window.completed_game_id
        self.assertNotEqual(completed_identifier, old_identifier)
        self.assertEqual(self.window.review_record["label"], "Older imported game")
        self.assertEqual(self.window.review_game_id, old_identifier)
        self.assertEqual(self.window.review_library_combo.currentData(), old_identifier)

        self.window.open_review()
        self.assertEqual(self.window.review_record["label"], "Older imported game")
        self.assertEqual(self.window.review_library_combo.currentData(), old_identifier)

        self.assertTrue(self.window.open_review_for_completed_game(run=False))
        self.assertTrue(self.window.review_record["position_only"])
        self.assertEqual(self.window.review_game_id, completed_identifier)
        self.assertEqual(
            self.window.review_library_combo.currentData(), completed_identifier
        )

    def test_preferences_persist_and_all_eval_povs_invert_correctly(self):
        self.window.live_follow.setCurrentIndex(
            self.window.live_follow.findData("auto")
        )
        self.window.live_explanation.setCurrentIndex(
            self.window.live_explanation.findData("detailed")
        )
        self.window.live_eval_pov.setCurrentIndex(
            self.window.live_eval_pov.findData("black")
        )
        self.window.live_line_expansion.setCurrentIndex(
            self.window.live_line_expansion.findData("all")
        )
        self.window.live_best_arrow.setChecked(False)
        self.window.live_study_auto.setChecked(False)
        self.window.live_study_snapshots.setChecked(False)
        self.window.apply_ui_preferences()
        self.window.settings.sync()

        restored = overlay.Overlay()
        self.assertEqual(restored.follow_live, "auto")
        self.assertEqual(restored.explanation_level, "detailed")
        self.assertEqual(restored.eval_pov, "black")
        self.assertEqual(restored.line_expansion, "all")
        self.assertFalse(restored.show_best_arrow)
        self.assertFalse(restored.study_auto_analyse)
        self.assertFalse(restored.study_save_evals)
        restored.deleteLater()

        self.assertEqual(overlay.score_for_pov(42, None, "white", "b"), (42, None))
        self.assertEqual(overlay.score_for_pov(42, 3, "black", "w"), (-42, -3))
        self.assertEqual(overlay.score_for_pov(42, 3, "side", "b"), (-42, -3))
        self.assertEqual(overlay.score_for_pov(42, 3, "side", "w"), (42, 3))
        self.assertEqual(
            overlay.format_line_score(
                {"bound": "lowerbound"}, -42, None, "black", "w"
            ),
            "≤-0.42",
        )

    def test_explanation_receives_streaming_and_final_state(self):
        self.window.explanation_level = "detailed"
        streaming = analysis_frame()
        streaming["final"] = False
        for line in streaming["lines"]:
            line["bound"] = "exact"
        self.window.apply_analysis(streaming)
        self.assertIn("Searching", self.window.explanation_label.text())
        self.assertFalse(self.window.lines[0]["final"])

        completed = analysis_frame()
        completed["final"] = True
        for line in completed["lines"]:
            line["bound"] = "exact"
        self.window.apply_analysis(completed)
        self.assertIn("Final", self.window.explanation_label.text())
        self.assertTrue(self.window.lines[0]["final"])

        bounded = analysis_frame()
        bounded["final"] = False
        bounded["lines"][0]["bound"] = "lowerbound"
        self.window.eval_pov = "black"
        self.window.explanation_level = "compact"
        self.window.apply_analysis(bounded)
        self.assertIn("≤-0.31", self.window.explanation_label.text())

    def test_replacement_destroy_preserves_inflight_explore_here_path(self):
        self.start_explore()
        self.window.apply_explore({"event": "live", "branch_id": 7})
        self.assertEqual(self.window.resume_branch_id, 7)

        self.window.pending_start_base = START
        self.window.pending_start_path = ["e2e4", "e7e5"]
        self.window.explore_pending = "start"
        self.window.apply_explore({
            "event": "destroyed", "branch_id": 7, "reason": "replaced"
        })
        self.assertEqual(self.window.explore_pending, "start")
        self.assertEqual(self.window.pending_start_path, ["e2e4", "e7e5"])

        self.window.apply_explore({
            "event": "started", "branch_id": 8, "node_id": 2,
            "fen": AFTER_E5, "last": "e7e5",
        })
        self.assertEqual(self.window.explore_branch_id, 8)
        self.assertEqual(self.window.explore_nodes[2]["parent"], 1)
        self.assertEqual(self.window.explore_root_node_id, 0)

    def test_imported_pgn_and_zero_move_fen_use_local_review_paths(self):
        record = overlay.pgn_import.parse_pgn("""
[White "Ada"]
[Black "Grace"]
[Result "*"]

1. e4 e5 *
""")
        self.window.apply_imported_record(record)
        self.assertEqual(self.window.review_record["moves"], ["e2e4", "e7e5"])
        self.assertEqual(self.window.review_record["metadata"]["White"], "Ada")
        self.assertEqual(len(self.window.review_positions), 3)
        self.assertEqual(self.window.review_selected_ply, 2)
        self.assertTrue(self.window.review_export_button.isEnabled())
        saved = self.window.review_store.find(self.window.review_game_id)
        self.assertEqual(saved["reviews"], {})
        self.assertTrue(saved["imported"])

        fen = "8/8/8/8/8/8/4K3/7k w - - 12 42"
        self.window.apply_imported_record({
            "initial_fen": fen, "moves": [], "result": "*",
            "label": "Imported position", "metadata": {"Event": "Imported FEN"},
        })
        self.assertEqual(self.window.review_positions, [fen])
        self.assertTrue(self.window.review_explore_button.isEnabled())
        self.window.review_settings_used = self.window.current_review_settings()
        self.window.finish_game_review({
            "reviews": [], "positions": [fen],
            "position_analyses": [[{
                "rank": 1, "depth": 16, "cp": 23, "mate": None, "pv": []
            }]],
        })
        self.assertIn("Position analysis complete", self.window.review_summary.text())
        self.assertEqual(len(self.window.review_graph.values), 1)
        cached = self.window.review_store.find(self.window.review_game_id)
        self.assertTrue(cached["reviews"])

        self.window.start_local_review_mode()
        self.assertTrue(self.window.local_mode)
        self.assertIs(self.window.stack.currentWidget(), self.window.review_page)

    def test_saved_study_tree_annotations_snapshots_search_and_local_moves(self):
        self.window.study_auto_analyse = False
        self.window.explore_nodes = {
            0: {
                "parent": None, "children": [1], "fen": START, "last": "",
                "analysis": {
                    "final": True,
                    "lines": [{"rank": 1, "depth": 16, "cp": 31,
                               "bound": "exact", "pv": "e2e4 e7e5"}],
                },
            },
            1: {
                "parent": 0, "children": [], "fen": AFTER_E4, "last": "e2e4",
                "analysis": {
                    "final": True,
                    "lines": [{"rank": 1, "depth": 14, "cp": 20,
                               "bound": "exact", "pv": "e7e5 g1f3"}],
                },
            },
        }
        self.window.mode = "explore"
        self.window.explore_root_node_id = 0
        self.window.explore_node_id = 1
        self.assertTrue(self.window.capture_analysis_lab("Opening notebook"))
        self.assertIs(self.window.stack.currentWidget(), self.window.study_page)
        self.assertEqual(len(self.window.current_study["nodes"]), 2)
        self.assertEqual(
            self.window.current_study["nodes"]["0"]["analysis"]["lines"][0]["cp"],
            31,
        )

        self.window.study_title_edit.setText("My opening notebook")
        self.window.study_name_edit.setText("Main candidate")
        self.window.study_comment_edit.setPlainText("Compare this with the quiet line.")
        self.window.save_study_annotation()
        self.assertEqual(
            self.window.current_study["nodes"]["1"]["name"], "Main candidate"
        )

        self.assertTrue(self.window.apply_study_move("e7e5"))
        e5 = self.window.study_node_id
        self.assertIsNone(self.window.study_position_job)
        self.window.study_go_root()
        self.assertTrue(self.window.apply_study_move("d2d4"))
        root = self.window.current_study["nodes"]["0"]
        self.assertEqual(len(root["children"]), 2)
        exported = overlay.study_rules.annotated_pgn(self.window.current_study)
        self.assertIn("1. e4", exported)
        self.assertIn("( 1. d4", exported)
        self.assertIn("Variation: Main candidate", exported)

        main_item = self.window.study_tree_items["1"]
        self.window.set_study_item_collapsed(main_item, True)
        saved = self.window.review_store.find_study(self.window.current_study_id)
        self.assertTrue(saved["nodes"]["1"]["collapsed"])
        self.window.study_search.setText("quiet line")
        self.assertEqual(self.window.study_library_combo.count(), 1)
        self.assertEqual(
            self.window.study_library_combo.currentData(), self.window.current_study_id
        )

        class FakeJob:
            def cancel(self):
                pass

            def is_alive(self):
                return True

        output = __import__("queue").Queue()
        original = overlay.review_rules.start_position_analysis
        overlay.review_rules.start_position_analysis = (
            lambda _fen, _settings, _generation: (FakeJob(), output)
        )
        try:
            self.window.study_search.clear()
            self.window.select_study_node(e5, analyse=False)
            self.window.analyse_study_node()
            output.put({
                "type": "position_complete",
                "generation": self.window.study_position_generation,
                "fen": self.window.current_study["nodes"][e5]["fen"],
                "lines": [{"rank": 1, "depth": 19, "cp": 55,
                           "mate": None, "pv": ["g1f3", "b8c6"]}],
            })
            self.window.poll_study_position()
        finally:
            overlay.review_rules.start_position_analysis = original
        snapshot = self.window.review_store.find_study(
            self.window.current_study_id
        )["nodes"][e5]["analysis"]
        self.assertEqual(snapshot["lines"][0]["cp"], 55)
        self.assertIn("+0.55", self.window.study_detail.text())

        self.window.close_study()
        self.assertIs(self.window.stack.currentWidget(), self.window.analysis_page)

    def test_study_edits_flush_before_switch_and_survive_save_failure(self):
        self.window.study_auto_analyse = False
        first = overlay.study_rules.new_study("First", START)
        first, child, _reused = overlay.study_rules.add_move(first, "0", "e2e4")
        first_id = self.window.review_store.save_study(first)
        second = overlay.study_rules.new_study("Second", START)
        second_id = self.window.review_store.save_study(second)

        self.assertTrue(self.window.load_study(self.window.review_store.find_study(first_id)))
        self.assertTrue(self.window.select_study_node("0", analyse=False))
        self.window.study_title_edit.setText("First edited")
        self.window.study_name_edit.setText("Root plan")
        self.window.study_comment_edit.setPlainText("Do not lose this note")
        self.assertTrue(self.window.study_dirty)
        self.assertTrue(self.window.load_study(self.window.review_store.find_study(second_id)))
        saved_first = self.window.review_store.find_study(first_id)
        self.assertEqual(saved_first["title"], "First edited")
        self.assertEqual(saved_first["nodes"]["0"]["name"], "Root plan")
        self.assertEqual(
            saved_first["nodes"]["0"]["comment"], "Do not lose this note"
        )

        self.assertTrue(self.window.load_study(saved_first))
        self.window.study_comment_edit.setPlainText("Retained after disk failure")
        original_save = self.window.review_store.save_study
        self.window.review_store.save_study = lambda _item: (_ for _ in ()).throw(
            OSError("disk full")
        )
        try:
            self.window.populate_study_library(first_id)
            second_index = self.window.study_library_combo.findData(second_id)
            self.window.study_library_combo.setCurrentIndex(second_index)
            self.assertEqual(self.window.current_study_id, first_id)
            self.assertEqual(self.window.study_library_combo.currentData(), first_id)

            child_item = self.window.study_tree_items[child]
            self.window.study_tree.setCurrentItem(child_item)
            self.assertEqual(self.window.study_node_id, "0")
            self.assertEqual(
                self.window.study_tree.currentItem().data(
                    0, overlay.Qt.ItemDataRole.UserRole
                ),
                "0",
            )
            self.assertFalse(self.window.select_study_node(child, analyse=False))
            self.assertEqual(self.window.study_node_id, "0")
            self.assertEqual(
                self.window.study_comment_edit.toPlainText(),
                "Retained after disk failure",
            )
            self.assertTrue(self.window.study_dirty)
            self.assertTrue(self.window.study_save_failed)
        finally:
            self.window.review_store.save_study = original_save
        self.assertTrue(self.window.flush_study_edits())
        self.assertFalse(self.window.study_dirty)
        self.assertEqual(
            self.window.review_store.find_study(first_id)["nodes"]["0"]["comment"],
            "Retained after disk failure",
        )

    def test_completed_study_analysis_never_repaints_a_different_node(self):
        self.window.study_auto_analyse = False
        item = overlay.study_rules.new_study("Node isolation", START)
        item, e4, _reused = overlay.study_rules.add_move(item, "0", "e2e4")
        item, e5, _reused = overlay.study_rules.add_move(item, e4, "e7e5")
        identifier = self.window.review_store.save_study(item)
        self.assertTrue(
            self.window.load_study(self.window.review_store.find_study(identifier))
        )
        self.assertTrue(self.window.select_study_node(e5, analyse=False))

        class FakeJob:
            def cancel(self):
                pass

            def is_alive(self):
                return True

        output = __import__("queue").Queue()
        original = overlay.review_rules.start_position_analysis
        overlay.review_rules.start_position_analysis = (
            lambda _fen, _settings, _generation: (FakeJob(), output)
        )
        try:
            self.window.analyse_study_node()
            generation = self.window.study_position_generation
            analysed_fen = self.window.current_study["nodes"][e5]["fen"]
            self.assertTrue(self.window.select_study_node("0", analyse=False))
            root_detail = self.window.study_detail.text()
            output.put({
                "type": "position_complete",
                "generation": generation,
                "fen": analysed_fen,
                "lines": [{
                    "rank": 1, "depth": 19, "cp": 777,
                    "mate": None, "pv": ["g1f3"],
                }],
            })
            self.window.poll_study_position()
        finally:
            overlay.review_rules.start_position_analysis = original

        self.assertEqual(self.window.study_node_id, "0")
        self.assertEqual(self.window.study_detail.text(), root_detail)
        self.assertNotIn("+7.77", self.window.study_detail.text())
        self.assertFalse(self.window.current_study["nodes"]["0"].get("analysis"))
        saved = self.window.review_store.find_study(identifier)
        self.assertEqual(saved["nodes"][e5]["analysis"]["lines"][0]["cp"], 777)

    def test_creating_study_cancels_analysis_owned_by_previous_tree(self):
        self.window.study_auto_analyse = False
        first = overlay.study_rules.new_study("First", START)
        first_id = self.window.review_store.save_study(first)
        self.assertTrue(
            self.window.load_study(self.window.review_store.find_study(first_id))
        )

        class FakeJob:
            def __init__(self):
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

            def is_alive(self):
                return True

        job = FakeJob()
        output = __import__("queue").Queue()
        original = overlay.review_rules.start_position_analysis
        overlay.review_rules.start_position_analysis = (
            lambda _fen, _settings, _generation: (job, output)
        )
        try:
            self.window.analyse_study_node()
            generation = self.window.study_position_generation
            self.assertTrue(self.window.create_study("Replacement", START))
            self.assertTrue(job.cancelled)
            output.put({
                "type": "position_complete",
                "generation": generation,
                "fen": START,
                "lines": [{
                    "rank": 1, "depth": 19, "cp": 888,
                    "mate": None, "pv": ["e2e4"],
                }],
            })
            self.window.poll_study_position()
        finally:
            overlay.review_rules.start_position_analysis = original

        root = self.window.current_study["nodes"][self.window.current_study["root"]]
        self.assertFalse(root.get("analysis"))
        self.assertNotIn("+8.88", self.window.study_detail.text())

    def test_window_close_flushes_pending_study_edits(self):
        item = overlay.study_rules.new_study("Close safety", START)
        identifier = self.window.review_store.save_study(item)
        self.assertTrue(
            self.window.load_study(self.window.review_store.find_study(identifier))
        )
        self.window.study_comment_edit.setPlainText("Written before close")
        self.assertTrue(self.window.study_dirty)

        event = QCloseEvent()
        self.window.closeEvent(event)

        self.assertTrue(event.isAccepted())
        saved = self.window.review_store.find_study(identifier)
        self.assertEqual(saved["nodes"]["0"]["comment"], "Written before close")
        self.assertFalse(self.window.study_dirty)

    def test_window_close_is_blocked_when_study_write_fails(self):
        item = overlay.study_rules.new_study("Close failure", START)
        identifier = self.window.review_store.save_study(item)
        self.assertTrue(
            self.window.load_study(self.window.review_store.find_study(identifier))
        )
        self.window.study_comment_edit.setPlainText("Keep this in the editor")
        original_save = self.window.review_store.save_study
        original_confirm = self.window.confirm_discard_failed_study_on_close
        self.window.review_store.save_study = lambda _item: (
            _ for _ in ()
        ).throw(OSError("disk full"))
        self.window.confirm_discard_failed_study_on_close = lambda: False
        try:
            event = QCloseEvent()
            self.window.closeEvent(event)
        finally:
            self.window.review_store.save_study = original_save
            self.window.confirm_discard_failed_study_on_close = original_confirm

        self.assertFalse(event.isAccepted())
        self.assertTrue(self.window.study_dirty)
        self.assertTrue(self.window.study_save_failed)
        self.assertEqual(
            self.window.study_comment_edit.toPlainText(),
            "Keep this in the editor",
        )
        self.assertIn("window remains open", self.window.study_save_state_label.text())
        self.assertFalse(any(command.startswith("QUIT") for command in self.commands))

    def test_window_close_can_explicitly_discard_after_study_write_failure(self):
        item = overlay.study_rules.new_study("Explicit discard", START)
        identifier = self.window.review_store.save_study(item)
        self.assertTrue(
            self.window.load_study(self.window.review_store.find_study(identifier))
        )
        self.window.study_comment_edit.setPlainText("Unsaved by explicit choice")
        original_save = self.window.review_store.save_study
        original_confirm = self.window.confirm_discard_failed_study_on_close
        self.window.review_store.save_study = lambda _item: (
            _ for _ in ()
        ).throw(OSError("read-only filesystem"))
        self.window.confirm_discard_failed_study_on_close = lambda: True
        try:
            event = QCloseEvent()
            self.window.closeEvent(event)
        finally:
            self.window.review_store.save_study = original_save
            self.window.confirm_discard_failed_study_on_close = original_confirm

        self.assertTrue(event.isAccepted())
        self.assertTrue(self.window.study_save_failed)
        self.assertEqual(
            self.window.review_store.find_study(identifier)["nodes"]["0"]["comment"],
            "",
        )
        self.assertTrue(any(command.startswith("QUIT") for command in self.commands))


if __name__ == "__main__":
    unittest.main(verbosity=2)
