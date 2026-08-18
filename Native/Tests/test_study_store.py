#!/usr/bin/env python3
import os
import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import study_store
import study


START = study.STANDARD_FEN
AFTER_E4 = "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1"


def cached_e4_result(name):
    score = {"rank": 1, "depth": 12, "cp": 15, "mate": None, "pv": []}
    return {
        "name": name,
        "ply": 1,
        "uci": "e2e4",
        "san": "e4",
        "classification": "Best",
        "loss": 0,
        "eval": "+0.15",
        "eval_score": score,
        "best": "e2e4",
        "depth": 12,
        "lines": [score],
        "fen_before": START,
        "fen_after": AFTER_E4,
    }


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

    def test_completed_game_save_is_idempotent_per_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = study_store.ReviewStore(os.path.join(temporary, "reviews.json"))
            record = {
                "initial_fen": START,
                "moves": ["e2e4", "e7e5"],
                "result": "*",
                "label": "Bot test",
                "session_id": "browser-session-17",
                "history_complete": True,
            }
            identifier, created = store.save_completed_game(record)
            self.assertTrue(created)
            repeated, created_again = store.save_completed_game(record)
            self.assertEqual(repeated, identifier)
            self.assertFalse(created_again)
            self.assertEqual(len(store.list_games()), 1)
            self.assertTrue(store.find(identifier)["completed"])
            self.assertTrue(store.find(identifier)["history_complete"])

            other_session = dict(record, session_id="browser-session-18")
            other_identifier, other_created = store.save_completed_game(other_session)
            self.assertTrue(other_created)
            self.assertNotEqual(other_identifier, identifier)
            self.assertEqual(len(store.list_games()), 2)

    def test_position_only_completion_preserves_its_honest_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = study_store.ReviewStore(os.path.join(temporary, "reviews.json"))
            record = {
                "initial_fen": "8/8/8/8/8/8/4K3/7k w - - 0 1",
                "moves": [],
                "result": "*",
                "label": "Final position only",
                "session_id": "fallback-session",
                "history_complete": False,
                "position_only": True,
                "source": "inferred",
            }
            identifier, created = store.save_completed_game(record)
            self.assertTrue(created)
            saved = store.find(identifier)
            self.assertTrue(saved["completed"])
            self.assertFalse(saved["history_complete"])
            self.assertTrue(saved["position_only"])
            self.assertEqual(saved["source"], "inferred")
            invalid = dict(record, session_id="fallback-invalid", result="White won")
            invalid_identifier, _created = store.save_completed_game(invalid)
            self.assertEqual(store.find(invalid_identifier)["result"], "*")

    def test_settings_have_distinct_cache_keys(self):
        self.assertNotEqual(
            study_store.settings_key({"time_ms": 150, "lines": 2}),
            study_store.settings_key({"time_ms": 800, "lines": 2}),
        )

    def test_multiple_review_settings_preserve_caches_and_completion_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = study_store.ReviewStore(os.path.join(temporary, "reviews.json"))
            record = {
                "initial_fen": START,
                "moves": ["e2e4"],
                "label": "Completed live game",
                "session_id": "session-with-two-reviews",
                "completed": True,
                "history_complete": True,
            }
            identifier, _created = store.save_completed_game(record)
            fast = {"time_ms": 150, "lines": 1}
            deep = {"time_ms": 800, "lines": 3}
            fast_result = cached_e4_result("fast")
            deep_result = cached_e4_result("deep")
            store.save_review(record, fast, [fast_result], [START, AFTER_E4])
            store.save_review(record, deep, [deep_result], [START, AFTER_E4])

            saved = store.find(identifier)
            self.assertEqual(len(saved["reviews"]), 2)
            self.assertEqual(
                store.cached_review(identifier, study_store.settings_key(fast))["results"],
                [fast_result],
            )
            self.assertEqual(
                store.cached_review(identifier, study_store.settings_key(deep))["results"],
                [deep_result],
            )
            self.assertTrue(saved["completed"])
            self.assertTrue(saved["history_complete"])
            self.assertEqual(saved["session_id"], record["session_id"])

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
            old = {
                "id": "old",
                "initial_fen": START,
                "moves": [],
                "reviews": {},
            }
            with open(path, "w", encoding="utf-8") as output:
                json.dump({"version": 1, "games": [old]}, output)
            store = study_store.ReviewStore(path)
            self.assertEqual(store.list_games(), [old])
            self.assertEqual(store.list_studies(), [])
            identifier = store.save_study(study.new_study("First study", START))
            self.assertEqual(store.list_games(), [old])
            self.assertEqual(store.find_study(identifier)["title"], "First study")
            self.assertEqual(json.loads(Path(path).read_text(encoding="utf-8"))["version"], 2)

    def test_every_mutation_fails_closed_on_truncated_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reviews.json"
            original = b'{"version":2,"games":[{"id":"unfinished"}'
            path.write_bytes(original)
            store = study_store.ReviewStore(path)
            record = {"initial_fen": START, "moves": [], "label": "Import"}
            settings = {"time_ms": 150, "lines": 1}
            operations = {
                "import/save record": lambda: store.save_record(record),
                "completed-game autosave": lambda: store.save_completed_game(record),
                "review save": lambda: store.save_review(
                    record, settings, [], [START]
                ),
                "game delete": lambda: store.delete("unfinished"),
                "study save": lambda: store.save_study(
                    study.new_study("Protected", START)
                ),
                "study delete": lambda: store.delete_study("unfinished"),
            }
            for name, operation in operations.items():
                with self.subTest(name=name):
                    with self.assertRaisesRegex(
                        study_store.ReviewLibraryError, "left unchanged"
                    ):
                        operation()
                    self.assertEqual(path.read_bytes(), original)

    def test_invalid_game_schema_is_not_dropped_on_next_save(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reviews.json"
            original = json.dumps({
                "version": 2,
                "games": [{"id": "missing-position", "moves": []}],
                "studies": [],
            }).encode("utf-8")
            path.write_bytes(original)
            store = study_store.ReviewStore(path)
            with self.assertRaisesRegex(
                study_store.ReviewLibraryError, "initial_fen"
            ):
                store.save_record({"initial_fen": START, "moves": []})
            self.assertEqual(path.read_bytes(), original)

    def test_malformed_nested_review_cache_fails_whole_archive_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reviews.json"
            original = json.dumps({
                "version": 2,
                "games": [{
                    "id": "valid-game-broken-cache",
                    "initial_fen": START,
                    "moves": [],
                    "reviews": {"broken": {
                        "results": [{}],
                        "positions": [START],
                        "position_analyses": [],
                        "created_at": 1,
                    }},
                }],
                "studies": [],
            }).encode("utf-8")
            path.write_bytes(original)
            store = study_store.ReviewStore(path)
            with self.assertRaisesRegex(
                study_store.ReviewLibraryError, "cached review"
            ):
                store.list_games()
            with self.assertRaises(study_store.ReviewLibraryError):
                store.save_record({"initial_fen": START, "moves": []})
            self.assertEqual(path.read_bytes(), original)

    def test_non_string_cached_best_move_cannot_reach_board_painting(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reviews.json"
            result = cached_e4_result("broken-best")
            result["best"] = {"move": "e2e4"}
            original = json.dumps({
                "version": 2,
                "games": [{
                    "id": "broken-best",
                    "initial_fen": START,
                    "moves": ["e2e4"],
                    "reviews": {"broken": {
                        "results": [result],
                        "positions": [START, AFTER_E4],
                        "position_analyses": [],
                        "created_at": 1,
                    }},
                }],
                "studies": [],
            }).encode("utf-8")
            path.write_bytes(original)
            with self.assertRaisesRegex(
                study_store.ReviewLibraryError, "inconsistent cached result"
            ):
                study_store.ReviewStore(path).list_games()
            self.assertEqual(path.read_bytes(), original)

    def test_string_cached_loss_cannot_reach_timeline_arithmetic(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reviews.json"
            result = cached_e4_result("string-loss")
            result["loss"] = "0"
            original = json.dumps({
                "version": 2,
                "games": [{
                    "id": "string-loss",
                    "initial_fen": START,
                    "moves": ["e2e4"],
                    "reviews": {"broken": {
                        "results": [result],
                        "positions": [START, AFTER_E4],
                        "position_analyses": [],
                        "created_at": 1,
                    }},
                }],
                "studies": [],
            }).encode("utf-8")
            path.write_bytes(original)
            with self.assertRaisesRegex(
                study_store.ReviewLibraryError, "cached-result loss"
            ):
                study_store.ReviewStore(path).list_games()
            self.assertEqual(path.read_bytes(), original)

    def test_illegal_saved_game_is_preserved_and_not_loaded(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reviews.json"
            original = json.dumps({
                "version": 2,
                "games": [{
                    "id": "illegal",
                    "initial_fen": START,
                    "moves": ["e2e5"],
                    "reviews": {},
                }],
                "studies": [],
            }).encode("utf-8")
            path.write_bytes(original)
            with self.assertRaisesRegex(
                study_store.ReviewLibraryError, "illegal game entry"
            ):
                study_store.ReviewStore(path).list_games()
            self.assertEqual(path.read_bytes(), original)

    def test_oversized_existing_archive_fails_before_reading_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reviews.json"
            path.write_bytes(b"x" * (study_store.MAX_BYTES + 1))
            with mock.patch.object(
                study_store.os,
                "open",
                side_effect=AssertionError("oversized archive must not be opened"),
            ):
                with self.assertRaisesRegex(
                    study_store.ReviewLibraryError, "16 MB"
                ):
                    study_store.ReviewStore(path).list_games()
            self.assertEqual(path.stat().st_size, study_store.MAX_BYTES + 1)

    def test_invalid_study_is_not_silently_skipped_or_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reviews.json"
            original = json.dumps({
                "version": 2,
                "games": [],
                "studies": [{"id": "broken", "nodes": {}}],
            }).encode("utf-8")
            path.write_bytes(original)
            store = study_store.ReviewStore(path)
            with self.assertRaisesRegex(
                study_store.ReviewLibraryError, "invalid study"
            ):
                store.list_studies()
            with self.assertRaises(study_store.ReviewLibraryError):
                store.save_study(study.new_study("New", START))
            self.assertEqual(path.read_bytes(), original)

    def test_unreadable_regular_archive_is_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reviews.json"
            original = b'{"version":2,"games":[],"studies":[]}'
            path.write_bytes(original)
            store = study_store.ReviewStore(path)
            real_open = study_store.os.open

            def deny_library(candidate, flags, *args, **kwargs):
                if Path(candidate) == path and flags & os.O_RDONLY == os.O_RDONLY:
                    raise PermissionError("test read denial")
                return real_open(candidate, flags, *args, **kwargs)

            with mock.patch.object(study_store.os, "open", side_effect=deny_library):
                with self.assertRaisesRegex(
                    study_store.ReviewLibraryError, "could not be read"
                ):
                    store.save_record({"initial_fen": START, "moves": []})
            self.assertEqual(path.read_bytes(), original)

    def test_nonregular_and_symlink_archives_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = {"initial_fen": START, "moves": []}

            directory = root / "directory.json"
            directory.mkdir()
            with self.assertRaisesRegex(
                study_store.ReviewLibraryError, "not a regular file"
            ):
                study_store.ReviewStore(directory).save_record(record)
            self.assertTrue(directory.is_dir())

            target = root / "target.json"
            original = b'{"version":2,"games":[],"studies":[]}'
            target.write_bytes(original)
            link = root / "linked.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(
                study_store.ReviewLibraryError, "not a regular file"
            ):
                study_store.ReviewStore(link).save_record(record)
            self.assertTrue(link.is_symlink())
            self.assertEqual(target.read_bytes(), original)

    def test_file_appearing_during_first_save_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reviews.json"
            original = b"owner-created bytes"

            class AppearingStore(study_store.ReviewStore):
                reads = 0

                def _read_bytes(inner_self):
                    inner_self.reads += 1
                    if inner_self.reads == 1:
                        return None, None
                    if not path.exists():
                        path.write_bytes(original)
                    return super()._read_bytes()

            with self.assertRaisesRegex(
                study_store.ReviewLibraryError, "changed after it was loaded"
            ):
                AppearingStore(path).save_record(
                    {"initial_fen": START, "moves": []}
                )
            self.assertEqual(path.read_bytes(), original)

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
