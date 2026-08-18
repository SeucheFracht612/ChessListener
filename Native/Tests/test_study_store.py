#!/usr/bin/env python3
import os
import sys
import tempfile
import unittest
import json
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import study_store
import study


START = study.STANDARD_FEN


class StoreTests(unittest.TestCase):
    def test_atomic_library_cache_and_delete(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = study_store.ReviewStore(os.path.join(temporary, "reviews.json"))
            record = {"initial_fen": "8/8/8/8/8/8/4K3/7k w - - 0 1",
                      "moves": [], "result": "1/2-1/2", "label": "Test",
                      "metadata": {"White": "Ada", "Black": "Grace"},
                      "imported": True}
            settings = {"time_ms": 350, "lines": 2,
                        "thresholds": (15, 35, 80, 160, 300)}
            identifier = store.save_record(record)
            self.assertEqual(store.list_games()[0]["reviews"], {})
            identifier, key = store.save_review(
                record, settings, [], [record["initial_fen"]],
                [[{"rank": 1, "cp": 15, "mate": None, "pv": []}]],
            )
            self.assertEqual(store.list_games()[0]["label"], "Test")
            self.assertEqual(store.list_games()[0]["metadata"]["White"], "Ada")
            self.assertTrue(store.list_games()[0]["imported"])
            self.assertEqual(store.cached_review(identifier, key)["positions"], [record["initial_fen"]])
            self.assertEqual(
                store.cached_review(identifier, key)["position_analyses"][0][0]["cp"], 15
            )
            updated = dict(record)
            updated["label"] = "Updated metadata"
            self.assertEqual(store.save_record(updated), identifier)
            self.assertEqual(store.list_games()[0]["label"], "Updated metadata")
            self.assertIsNotNone(store.cached_review(identifier, key))
            self.assertTrue(store.delete(identifier))
            self.assertEqual(store.list_games(), [])

    def test_settings_have_distinct_cache_keys(self):
        self.assertNotEqual(
            study_store.settings_key({"time_ms": 150, "lines": 2}),
            study_store.settings_key({"time_ms": 800, "lines": 2}),
        )

    def test_library_keeps_only_newest_fifty_games(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = study_store.ReviewStore(os.path.join(temporary, "reviews.json"))
            settings = {"time_ms": 150, "lines": 1}
            for index in range(52):
                record = {"initial_fen": f"8/8/8/8/8/8/4K3/7k w - - {index} 1",
                          "moves": [], "result": "*", "label": f"Game {index}"}
                store.save_review(record, settings, [], [record["initial_fen"]])
            games = store.list_games()
            self.assertEqual(len(games), 50)
            self.assertEqual(games[0]["label"], "Game 51")

    def test_schema_one_reviews_migrate_without_data_loss(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = os.path.join(temporary, "reviews.json")
            with open(path, "w", encoding="utf-8") as output:
                json.dump({"version": 1, "games": [{"id": "old"}]}, output)
            store = study_store.ReviewStore(path)
            self.assertEqual(store.list_games(), [{"id": "old"}])
            self.assertEqual(store.list_studies(), [])
            identifier = store.save_study(study.new_study("First study", START))
            self.assertEqual(store.list_games(), [{"id": "old"}])
            self.assertEqual(store.find_study(identifier)["title"], "First study")
            self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8"))["version"], 2)

    def test_studies_search_update_and_delete_without_erasing_reviews(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = study_store.ReviewStore(os.path.join(temporary, "reviews.json"))
            record = {"initial_fen": START, "moves": [], "label": "A game"}
            store.save_record(record)
            first = study.new_study("Sicilian plans", START, {"White": "Ada"})
            first, child, _ = study.add_move(first, "0", "e2e4")
            first["nodes"][child]["name"] = "Open Sicilian"
            first["nodes"][child]["comment"] = "Compare the Najdorf setup"
            identifier = store.save_study(first)
            saved = store.find_study(identifier)
            self.assertEqual(saved["selected"], child)
            self.assertEqual(len(store.list_games()), 1)
            self.assertEqual(len(store.list_studies("najdorf")), 1)
            self.assertEqual(len(store.list_studies("Ada Sicilian")), 1)
            self.assertEqual(store.list_studies("French"), [])

            saved["title"] = "Updated study"
            self.assertEqual(store.save_study(saved), identifier)
            self.assertEqual(store.list_studies()[0]["title"], "Updated study")
            self.assertTrue(store.delete_study(identifier))
            self.assertEqual(store.list_studies(), [])
            self.assertEqual(len(store.list_games()), 1)

    def test_library_keeps_only_newest_hundred_studies(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = study_store.ReviewStore(os.path.join(temporary, "reviews.json"))
            for index in range(102):
                store.save_study(study.new_study(f"Study {index}", START))
            studies = store.list_studies()
            self.assertEqual(len(studies), 100)
            self.assertEqual(studies[0]["title"], "Study 101")


if __name__ == "__main__":
    unittest.main()
