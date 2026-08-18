#!/usr/bin/env python3
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import study
import pgn_import


START = study.STANDARD_FEN


class StudyTests(unittest.TestCase):
    def build_tree(self):
        item = study.new_study("Sicilian ideas", START, {"Site": "My desk"})
        item, e4, reused = study.add_move(item, "0", "e2e4")
        self.assertFalse(reused)
        item, c5, _ = study.add_move(item, e4, "c7c5")
        item, nf3, _ = study.add_move(item, c5, "g1f3")
        item, d4, _ = study.add_move(item, "0", "d2d4")
        item, d5, _ = study.add_move(item, d4, "d7d5")
        item["nodes"][e4]["name"] = "Open Sicilian"
        item["nodes"][e4]["comment"] = "My main candidate."
        item["nodes"][e4]["analysis"] = {
            "final": True,
            "lines": [{"rank": 1, "depth": 18, "cp": 31, "mate": None,
                       "bound": "exact", "pv": ["c7c5", "g1f3"]}],
        }
        item["nodes"][d4]["name"] = "Queen's pawn alternative"
        item["nodes"][d4]["collapsed"] = True
        item["selected"] = nf3
        return study.normalise_study(item), (e4, c5, nf3, d4, d5)

    def test_branching_annotations_snapshots_and_paths(self):
        item, (e4, _c5, nf3, d4, _d5) = self.build_tree()
        self.assertEqual(study.path_to_node(item, nf3), ["e2e4", "c7c5", "g1f3"])
        self.assertEqual(item["nodes"][e4]["name"], "Open Sicilian")
        self.assertEqual(item["nodes"][e4]["analysis"]["lines"][0]["cp"], 31)
        self.assertTrue(item["nodes"][d4]["collapsed"])

        same, same_id, reused = study.add_move(item, "0", "e2e4")
        self.assertTrue(reused)
        self.assertEqual(same_id, e4)
        self.assertEqual(len(same["nodes"]), len(item["nodes"]))

    def test_annotated_pgn_contains_named_rav_comments_and_eval(self):
        item, _ids = self.build_tree()
        output = study.annotated_pgn(item)
        self.assertIn('[Event "Sicilian ideas"]', output)
        self.assertIn("1. e4", output)
        self.assertIn("( 1. d4", output)
        self.assertIn("Variation: Open Sicilian", output)
        self.assertIn("My main candidate.", output)
        self.assertIn("[%eval 0.31]", output)
        self.assertIn("[%depth 18]", output)
        self.assertIn("Variation: Queen's pawn alternative", output)
        self.assertTrue(output.rstrip().endswith("*"))
        # ChessListener's strict importer must be able to replay the exported
        # main line while safely ignoring its comments and RAV branches.
        self.assertEqual(
            pgn_import.parse_pgn(output)["moves"],
            ["e2e4", "c7c5", "g1f3"],
        )

    def test_custom_fen_headers_and_black_move_number(self):
        fen = "8/8/8/8/8/8/7r/4K2k b - - 0 42"
        item = study.new_study("Black to move", fen)
        item, _node, _ = study.add_move(item, "0", "h2e2")
        output = study.annotated_pgn(item)
        self.assertIn('[SetUp "1"]', output)
        self.assertIn(f'[FEN "{fen}"]', output)
        self.assertIn("42... Re2+", output)

    def test_capture_from_analysis_lab_rekeys_and_validates(self):
        after_e4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"
        nodes = {
            9: {"parent": None, "children": [12], "fen": START, "last": ""},
            12: {"parent": 9, "children": [], "fen": after_e4, "last": "e2e4",
                 "analysis": {"lines": [{"rank": 1, "depth": 10, "cp": 20,
                                            "pv": "e7e5 g1f3"}]}},
        }
        item = study.from_explore_tree("Captured", nodes, 9, 12)
        self.assertEqual(item["root"], "0")
        self.assertEqual(item["selected"], "1")
        self.assertEqual(item["nodes"]["1"]["move"], "e2e4")
        self.assertEqual(item["nodes"]["1"]["analysis"]["lines"][0]["pv"],
                         ["e7e5", "g1f3"])

    def test_rejects_illegal_mismatched_cyclic_and_unreachable_trees(self):
        item = study.new_study("Broken", START)
        item["nodes"]["1"] = {
            "parent": "0", "children": [], "move": "e2e5", "fen": START,
        }
        item["nodes"]["0"]["children"] = ["1"]
        with self.assertRaisesRegex(ValueError, "illegal"):
            study.normalise_study(item)

        item = study.new_study("Broken", START)
        item["nodes"]["1"] = {
            "parent": "0", "children": [], "move": "e2e4", "fen": START,
        }
        item["nodes"]["0"]["children"] = ["1"]
        with self.assertRaisesRegex(ValueError, "FEN"):
            study.normalise_study(item)

        item = study.new_study("Broken", START)
        item["nodes"]["1"] = {
            "parent": "1", "children": ["1"], "move": "e2e4", "fen": START,
        }
        with self.assertRaisesRegex(ValueError, "unreachable"):
            study.normalise_study(item)

    def test_bounds_and_invalid_pv_are_preserved_honestly(self):
        item = study.new_study("Bounds", START)
        item["nodes"]["0"]["analysis"] = {
            "lines": [{"rank": 1, "depth": 12, "cp": 42,
                       "bound": "lowerbound", "pv": "e2e4 junk e7e5"}],
        }
        clean = study.normalise_study(item)
        line = clean["nodes"]["0"]["analysis"]["lines"][0]
        self.assertEqual(line["pv"], ["e2e4"])
        self.assertEqual(line["bound"], "lowerbound")

    def test_position_limit_is_checked_before_tree_replay(self):
        item = study.new_study("Too large", START)
        template = dict(item["nodes"]["0"])
        for index in range(1, study.MAX_STUDY_NODES + 1):
            item["nodes"][str(index)] = dict(template)
        with self.assertRaisesRegex(ValueError, "1 to 512"):
            study.normalise_study(item)


if __name__ == "__main__":
    unittest.main(verbosity=2)
