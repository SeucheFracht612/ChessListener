#!/usr/bin/env python3
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pgn_import
import review


START = pgn_import.STANDARD_FEN


class PgnImportTests(unittest.TestCase):
    def test_mainline_comments_variations_nags_and_metadata(self):
        record = pgn_import.parse_pgn("""
[Event "Friendly"]
[White "Ada"]
[Black "Grace"]
[Result "1-0"]

1. e4 $1 (1. d4 d5 (1... Nf6)) e5
2.Nf3 Nc6 {main line only} 3. Bb5 a6 ; ignored to newline
4. Ba4 Nf6 1-0
""")
        self.assertEqual(
            record["moves"],
            ["e2e4", "e7e5", "g1f3", "b8c6", "f1b5", "a7a6", "b5a4", "g8f6"],
        )
        self.assertEqual(record["metadata"]["White"], "Ada")
        self.assertEqual(record["result"], "1-0")
        self.assertEqual(record["label"], "Ada – Grace · 1-0")

    def test_custom_fen_and_promotion_without_equals(self):
        record = pgn_import.parse_pgn("""
[Event "Composition"]
[SetUp "1"]
[FEN "7k/P7/8/8/8/8/8/K7 w - - 0 1"]
[Result "*"]

1. a8N *
""")
        self.assertEqual(record["moves"], ["a7a8n"])
        self.assertTrue(record["final_fen"].startswith("N6k/8/"))

    def test_castling_figurines_and_coordinate_notation(self):
        record = pgn_import.parse_pgn("""
[SetUp "1"]
[FEN "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1"]

1. 0-0 0-0-0 2. ♖a2 h8h7 *
""")
        self.assertEqual(record["moves"], ["e1g1", "e8c8", "a1a2", "h8h7"])

    def test_annotated_export_round_trip(self):
        moves = ["e2e4", "e7e5", "g1f3"]
        items = [
            {"classification": "Best", "loss": 0, "eval": "+0.20", "best": move}
            for move in moves
        ]
        exported = review.annotated_pgn(
            START, moves, items, "*", {"White": "Local", "Black": "Guest"}
        )
        imported = pgn_import.parse_pgn(exported)
        self.assertEqual(imported["moves"], moves)
        self.assertEqual(imported["metadata"]["White"], "Local")

    def test_file_import_and_utf8_bom(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = os.path.join(temporary, "one-game.pgn")
            with open(path, "w", encoding="utf-8-sig") as output:
                output.write("[Result \"*\"]\n\n1. e4 e5 *\n")
            self.assertEqual(pgn_import.parse_pgn_file(path)["moves"], ["e2e4", "e7e5"])
            latin = os.path.join(temporary, "latin.pgn")
            with open(latin, "wb") as output:
                output.write('[White "André"]\n\n*\n'.encode("iso-8859-1"))
            self.assertEqual(pgn_import.parse_pgn_file(latin)["metadata"]["White"], "André")

    def test_zero_move_fen_is_valid_local_position(self):
        record = pgn_import.parse_pgn("""
[SetUp "1"]
[FEN "8/8/8/8/8/8/4K3/7k w - - 12 42"]
[Result "1/2-1/2"]

1/2-1/2
""")
        self.assertEqual(record["moves"], [])
        self.assertEqual(record["initial_fen"], "8/8/8/8/8/8/4K3/7k w - - 12 42")

    def test_rejects_illegal_ambiguous_incomplete_and_multiple_games(self):
        bad_inputs = (
            "1. e5 *",
            "[SetUp \"1\"]\n\n*",
            "1. e4 (1... e5 *",
            "[Result \"1-0\"]\n\n1. e4 0-1",
            "[Event \"One\"]\n\n1. e4 *\n[Event \"Two\"]\n\n1. d4 *",
            "[SetUp \"1\"]\n[FEN \"8/8/8/8/8/8/3NK2N/7k w - - 0 1\"]\n\n1. Nf3 *",
        )
        for text in bad_inputs:
            with self.subTest(text=text):
                with self.assertRaises(pgn_import.ImportError):
                    pgn_import.parse_pgn(text)

    def test_fen_validation_is_strict(self):
        self.assertEqual(pgn_import.canonical_fen(START), START)
        for fen in (
            "8/8/8/8/8/8/4K3/8 w - - 0 1",
            "8/8/8/8/8/8/4K3/7k x - - 0 1",
            "8/8/8/8/8/8/4K3/7k w QK - 0 1",
            "8/8/8/8/8/8/4K3/7k w - - -1 1",
            "8/8/8/8/8/8/4K3/7k w - - 0",
            "8/8/8/8/4P3/4N3/4K3/7k b - e3 0 1",
        ):
            with self.subTest(fen=fen):
                with self.assertRaises(pgn_import.ImportError):
                    pgn_import.canonical_fen(fen)


if __name__ == "__main__":
    unittest.main()
