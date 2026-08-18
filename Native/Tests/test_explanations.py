#!/usr/bin/env python3
"""Deterministic, dependency-free tests for line-grounded explanations.

    python3 Tests/test_explanations.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import explanations


START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def line(move, pv, cp=None, mate=None, depth=17, final=True, **extra):
    result = {
        "move": move,
        "pv": pv,
        "depth": depth,
        "final": final,
    }
    if cp is not None:
        result["cp"] = cp
    if mate is not None:
        result["mate"] = mate
    result.update(extra)
    return result


class ExplanationTests(unittest.TestCase):
    def test_san_score_status_and_candidate_comparison(self):
        lines = [
            line("e2e4", "e2e4 e7e5 g1f3", cp=31),
            line("d2d4", "d2d4 d7d5", cp=10),
        ]
        result = explanations.build_explanation(
            START, lines, selected_rank=2, display_plies=6,
            level="compact", eval_pov="white"
        )

        self.assertEqual(result["heading"], "What the line shows")
        self.assertEqual(result["pv_san"], ["d4", "d5"])
        self.assertEqual(result["line_text"], "d4 d5")
        self.assertEqual(result["move_san"], "d4")
        self.assertEqual(result["score_text"], "+0.10")
        self.assertEqual(result["status_text"], "Final · depth 17")
        self.assertEqual(
            result["comparison_text"],
            "Compared with line 1 (e4), the evaluation is 0.21 pawns "
            "lower from White's perspective.",
        )
        self.assertFalse(result["truncated"])

    def test_white_black_and_side_to_move_score_inversion(self):
        lines = [
            line("e2e4", "e2e4", cp=31),
            line("d2d4", "d2d4", cp=10),
        ]
        black = explanations.build_explanation(
            START, lines, 2, eval_pov="black"
        )
        self.assertEqual(black["score_text"], "-0.10")
        self.assertIn("0.21 pawns higher from Black's perspective", black["comparison_text"])

        black_to_move = (
            "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR "
            "b KQkq e3 0 1"
        )
        black_lines = [
            line("e7e5", "e7e5", cp=20),
            line("d7d5", "d7d5", cp=50),
        ]
        side = explanations.build_explanation(
            black_to_move, black_lines, 2, eval_pov="side"
        )
        self.assertEqual(side["perspective"], "black")
        self.assertEqual(side["score_text"], "-0.50")
        self.assertIn("0.30 pawns lower from Black's perspective", side["comparison_text"])

    def test_mate_format_and_mate_comparison(self):
        lines = [
            line("e2e4", "e2e4", mate=3),
            line("d2d4", "d2d4", mate=5),
        ]
        result = explanations.build_explanation(START, lines, 2)
        self.assertEqual(result["score_text"], "#5")
        self.assertEqual(
            result["comparison_text"],
            "Line 1 (e4) is #3; the selected line is #5 from White's perspective.",
        )

        inverted = explanations.build_explanation(
            START, lines, 2, eval_pov="black"
        )
        self.assertEqual(inverted["score_text"], "#-5")

    def test_unsafe_score_comparisons_are_suppressed(self):
        unequal_depth = [
            line("e2e4", "e2e4", cp=31, depth=18),
            line("d2d4", "d2d4", cp=10, depth=17),
        ]
        self.assertEqual(
            explanations.build_explanation(START, unequal_depth, 2)["comparison_text"],
            "",
        )

        bounded = [
            line("e2e4", "e2e4", cp=31),
            line("d2d4", "d2d4", cp=10, bound="lowerbound"),
        ]
        result = explanations.build_explanation(START, bounded, 2)
        self.assertEqual(result["comparison_text"], "")
        self.assertEqual(result["status_text"], "Lower bound · depth 17")

        unlike_scores = [
            line("e2e4", "e2e4", mate=3),
            line("d2d4", "d2d4", cp=600),
        ]
        self.assertEqual(
            explanations.build_explanation(START, unlike_scores, 2)["comparison_text"],
            "",
        )

        mixed_final = [
            line("e2e4", "e2e4", cp=31, final=True),
            line("d2d4", "d2d4", cp=10, final=False),
        ]
        self.assertEqual(
            explanations.build_explanation(START, mixed_final, 2)["comparison_text"],
            "",
        )

    def test_malformed_pv_is_legally_truncated(self):
        result = explanations.build_explanation(
            START,
            [line("e2e4", "e2e4 e7e5 g1f3 not-a-move b8c6", cp=20)],
            display_plies=12,
        )
        self.assertEqual(result["pv_san"], ["e4", "e5", "Nf3"])
        self.assertTrue(result["truncated"])

        stale = explanations.build_explanation(
            START, [line("e2e4", "d2d4 d7d5", cp=20)]
        )
        self.assertEqual(stale["pv_san"], ["e4"])
        self.assertTrue(stale["truncated"])

        intentional = explanations.build_explanation(
            START,
            [line("e2e4", "e2e4 e7e5 g1f3 b8c6", cp=20)],
            display_plies=2,
        )
        self.assertEqual(intentional["pv_san"], ["e4", "e5"])
        self.assertFalse(intentional["truncated"])
        self.assertTrue(intentional["has_more"])

    def test_bad_input_never_raises_and_returns_empty_presentation(self):
        for fen, lines in (
            ("not a FEN", [line("e2e4", "e2e4", cp=20)]),
            (START, None),
            (START, [None, "bad"]),
            (START, [{"rank": 1, "move": object(), "pv": object()}]),
        ):
            result = explanations.build_explanation(fen, lines)
            self.assertEqual(result["pv_san"], [])
            self.assertEqual(result["line_text"], "")
            self.assertEqual(result["facts"], [])

    def test_check_and_checkmate_are_proven_from_replay(self):
        mate_fen = "6k1/5ppp/8/8/8/8/8/R6K w - - 0 1"
        result = explanations.build_explanation(
            mate_fen, [line("a1a8", "a1a8", mate=1)], level="compact"
        )
        self.assertEqual(result["pv_san"], ["Ra8#"])
        self.assertIn(
            "Ra8# ends the displayed line in checkmate.", result["facts"]
        )

        check_fen = "8/8/8/3k4/8/3K1Q2/8/8 w - - 0 1"
        check = explanations.build_explanation(
            check_fen, [line("f3f5", "f3f5", cp=30)]
        )
        self.assertEqual(check["pv_san"], ["Qf5+"])
        self.assertIn("Qf5+ gives check.", check["facts"])

    def test_only_legal_reply_is_stated_only_when_generated(self):
        fen = "7k/8/8/8/8/4K3/6Q1/8 w - - 0 1"
        result = explanations.build_explanation(
            fen, [line("e3f3", "e3f3 h8h7", cp=0)]
        )
        self.assertIn(
            "After Kf3, Kh7 is the only legal reply.", result["facts"]
        )

    def test_capture_recapture_and_horizon_inventory(self):
        result = explanations.build_explanation(
            START,
            [line("e2e4", "e2e4 d7d5 e4d5 d8d5", cp=0)],
            display_plies=4,
            level="detailed",
        )
        self.assertEqual(result["pv_san"], ["e4", "d5", "exd5", "Qxd5"])
        self.assertIn(
            "Qxd5 immediately recaptures a pawn on d5.", result["facts"]
        )
        self.assertIn("exd5 captures a pawn.", result["facts"])
        self.assertTrue(
            any(fact.startswith("At the displayed horizon, White has")
                for fact in result["facts"])
        )
        self.assertEqual(result["horizon_inventory"]["white"]["pawn"], 7)
        self.assertEqual(result["horizon_inventory"]["black"]["pawn"], 7)

    def test_en_passant_castling_and_promotion_wording(self):
        en_passant = explanations.build_explanation(
            "7k/8/8/3pP3/8/8/8/K7 w - d6 0 1",
            [line("e5d6", "e5d6", cp=20)],
        )
        self.assertIn("exd6 captures a pawn en passant.", en_passant["facts"])

        castling = explanations.build_explanation(
            "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1",
            [line("e1g1", "e1g1", cp=20)],
        )
        self.assertIn("O-O castles kingside.", castling["facts"])

        promotion = explanations.build_explanation(
            "8/P6k/8/8/8/8/8/K7 w - - 0 1",
            [line("a7a8q", "a7a8q", cp=900)],
        )
        self.assertIn(
            "a8=Q promotes the pawn to a queen.", promotion["facts"]
        )

    def test_explanation_levels_limit_or_disable_semantics(self):
        tactical = [line("e2e4", "e2e4 d7d5 e4d5 d8d5", cp=0)]
        off = explanations.build_explanation(START, tactical, level="off")
        self.assertEqual(off["facts"], [])
        self.assertEqual(off["comparison_text"], "")

        compact = explanations.build_explanation(START, tactical, level="compact")
        detailed = explanations.build_explanation(START, tactical, level="detailed")
        self.assertLessEqual(len(compact["facts"]), 2)
        self.assertLessEqual(len(detailed["facts"]), 3)

        quiet = explanations.build_explanation(
            START, [line("e2e4", "e2e4 e7e5", cp=20)]
        )
        self.assertEqual(
            quiet["facts"],
            ["No simple tactical feature was detected in the displayed continuation."],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
