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

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="chesslistener-qt-")
os.environ["CHESSLISTENER_LIBRARY"] = os.path.join(
    tempfile.mkdtemp(prefix="chesslistener-library-"), "reviews.json"
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QApplication
except ImportError:
    print("SKIP overlay interaction tests: PyQt6 is not installed")
    raise SystemExit(0)

import overlay


START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
AFTER_E5 = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2"


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
        self.assertTrue(QSettings(overlay.ORGANIZATION, overlay.APPLICATION).value("review/latest", ""))
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
