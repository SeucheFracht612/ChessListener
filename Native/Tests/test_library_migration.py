#!/usr/bin/env python3
"""Hermetic coverage for the 0.9.5 user-library storage split."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


NATIVE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(NATIVE))
import study_store


START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def library_payload(label):
    return json.dumps(
        {
            "version": 2,
            "games": [{
                "id": label,
                "initial_fen": START,
                "moves": [],
                "label": label,
                "reviews": {},
            }],
            "studies": [],
        },
        separators=(",", ":"),
    ).encode("utf-8")


class LibraryMigrationTests(unittest.TestCase):
    def environment(self, data_home, **extra):
        values = {"XDG_DATA_HOME": str(data_home)}
        values.update(extra)
        return mock.patch.dict(os.environ, values, clear=True)

    def test_default_store_atomically_moves_legacy_library(self):
        with tempfile.TemporaryDirectory() as raw:
            data_home = Path(raw) / "data"
            legacy = data_home / "chess-listener" / "reviews.json"
            legacy.parent.mkdir(parents=True)
            expected = library_payload("legacy-game")
            legacy.write_bytes(expected)

            with self.environment(data_home):
                store = study_store.ReviewStore()
                destination = data_home / "chess-listener-library" / "reviews.json"
                self.assertEqual(store.path, destination)
                self.assertEqual(store.migration["action"], "migrated")
                self.assertEqual(store.list_games()[0]["id"], "legacy-game")

            self.assertFalse(legacy.exists())
            self.assertEqual(destination.read_bytes(), expected)
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(destination.parent.glob("*.migrating")), [])

    def test_existing_newer_library_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as raw:
            data_home = Path(raw) / "data"
            legacy = data_home / "chess-listener" / "reviews.json"
            destination = data_home / "chess-listener-library" / "reviews.json"
            legacy.parent.mkdir(parents=True)
            destination.parent.mkdir(parents=True)
            old_bytes = library_payload("old-game")
            new_bytes = library_payload("new-game")
            legacy.write_bytes(old_bytes)
            destination.write_bytes(new_bytes)

            with self.environment(data_home):
                store = study_store.ReviewStore()

            self.assertEqual(store.migration["action"], "archived")
            self.assertEqual(destination.read_bytes(), new_bytes)
            self.assertFalse(legacy.exists())
            recovery = store.migration["recovery"]
            self.assertIsNotNone(recovery)
            self.assertEqual(recovery.read_bytes(), old_bytes)
            self.assertEqual(
                list(destination.parent.glob("reviews.legacy-*.json")), [recovery]
            )

    def test_matching_destination_only_removes_duplicate_legacy_copy(self):
        with tempfile.TemporaryDirectory() as raw:
            data_home = Path(raw) / "data"
            legacy = data_home / "chess-listener" / "reviews.json"
            destination = data_home / "chess-listener-library" / "reviews.json"
            legacy.parent.mkdir(parents=True)
            destination.parent.mkdir(parents=True)
            expected = library_payload("same-game")
            legacy.write_bytes(expected)
            destination.write_bytes(expected)

            with self.environment(data_home):
                result = study_store.migrate_legacy_library()

            self.assertEqual(result["action"], "deduplicated")
            self.assertFalse(legacy.exists())
            self.assertEqual(destination.read_bytes(), expected)
            self.assertEqual(list(destination.parent.glob("reviews.legacy-*")), [])

    def test_dry_run_reports_conflict_without_changing_either_file(self):
        with tempfile.TemporaryDirectory() as raw:
            data_home = Path(raw) / "data"
            legacy = data_home / "chess-listener" / "reviews.json"
            destination = data_home / "chess-listener-library" / "reviews.json"
            legacy.parent.mkdir(parents=True)
            destination.parent.mkdir(parents=True)
            old_bytes = library_payload("old-game")
            new_bytes = library_payload("new-game")
            legacy.write_bytes(old_bytes)
            destination.write_bytes(new_bytes)

            with self.environment(data_home):
                result = study_store.migrate_legacy_library(dry_run=True)

            self.assertEqual(result["action"], "archived")
            self.assertEqual(legacy.read_bytes(), old_bytes)
            self.assertEqual(destination.read_bytes(), new_bytes)
            self.assertFalse(result["recovery"].exists())

    def test_explicit_environment_override_is_exact_and_never_migrates(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data_home = root / "data"
            override = root / "chosen" / "my-library.json"
            legacy = data_home / "chess-listener" / "reviews.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(library_payload("legacy-game"))

            with self.environment(data_home, CHESSLISTENER_LIBRARY=str(override)):
                store = study_store.ReviewStore()

            self.assertEqual(store.path, override)
            self.assertIsNone(store.migration)
            self.assertTrue(legacy.exists())
            self.assertFalse(
                (data_home / "chess-listener-library" / "reviews.json").exists()
            )

    def test_explicit_constructor_path_never_migrates(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data_home = root / "data"
            explicit = root / "tests" / "reviews.json"
            legacy = data_home / "chess-listener" / "reviews.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(library_payload("legacy-game"))

            with self.environment(data_home):
                store = study_store.ReviewStore(explicit)

            self.assertEqual(store.path, explicit)
            self.assertIsNone(store.migration)
            self.assertTrue(legacy.exists())

    def test_relative_library_override_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            with self.environment(
                Path(raw) / "data", CHESSLISTENER_LIBRARY="relative/reviews.json"
            ):
                with self.assertRaisesRegex(
                    study_store.ReviewLibraryError, "absolute path"
                ):
                    study_store.ReviewStore()

    def test_relative_xdg_data_home_is_rejected(self):
        with mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": "relative-data"}, clear=True
        ):
            with self.assertRaisesRegex(
                study_store.ReviewLibraryError, "XDG_DATA_HOME.*absolute"
            ):
                study_store.ReviewStore()

    def test_override_inside_installed_runtime_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime = root / "installed-runtime"
            selected = runtime / "user-reviews.json"
            with self.environment(
                root / "data", CHESSLISTENER_LIBRARY=str(selected)
            ), mock.patch.object(
                study_store, "_installed_runtime", return_value=runtime
            ):
                with self.assertRaisesRegex(
                    study_store.ReviewLibraryError, "inside the managed"
                ):
                    study_store.ReviewStore()

            self.assertFalse(selected.exists())

    def test_symlinked_legacy_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            data_home = root / "data"
            target = root / "outside.json"
            target.write_bytes(library_payload("outside"))
            legacy = data_home / "chess-listener" / "reviews.json"
            legacy.parent.mkdir(parents=True)
            legacy.symlink_to(target)

            with self.environment(data_home):
                with self.assertRaisesRegex(OSError, "not a regular file"):
                    study_store.migrate_legacy_library()

            self.assertTrue(legacy.is_symlink())
            self.assertEqual(target.read_bytes(), library_payload("outside"))

    def test_nonregular_legacy_path_fails_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            data_home = Path(raw) / "data"
            legacy = data_home / "chess-listener" / "reviews.json"
            legacy.mkdir(parents=True)

            with self.environment(data_home):
                with self.assertRaisesRegex(OSError, "not a regular file"):
                    study_store.migrate_legacy_library()

            self.assertTrue(legacy.is_dir())

    def test_symlinked_destination_parent_cannot_alias_legacy_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runtime = root / "data" / "chess-listener"
            runtime.mkdir(parents=True)
            legacy = runtime / "reviews.json"
            expected = library_payload("only-copy")
            legacy.write_bytes(expected)
            library_directory = root / "data" / "chess-listener-library"
            library_directory.symlink_to(runtime, target_is_directory=True)
            destination = library_directory / "reviews.json"

            with self.assertRaisesRegex(
                OSError, "resolves inside the managed runtime|symbolic-link"
            ):
                study_store.migrate_legacy_library(
                    destination=destination, legacy=legacy
                )

            self.assertTrue(legacy.exists())
            self.assertEqual(legacy.read_bytes(), expected)
            self.assertEqual(destination.read_bytes(), expected)

    def test_destination_inside_runtime_is_rejected_even_without_legacy_file(self):
        with tempfile.TemporaryDirectory() as raw:
            runtime = Path(raw) / "chess-listener"
            destination = runtime / "chess-listener-library" / "reviews.json"
            legacy = runtime / "reviews.json"

            with self.assertRaisesRegex(OSError, "inside the managed runtime"):
                study_store.migrate_legacy_library(
                    destination=destination, legacy=legacy
                )

            self.assertFalse(destination.exists())
            self.assertFalse(legacy.exists())

    def test_no_legacy_file_allows_unrelated_symlinked_data_parent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            external = root / "external-data"
            external.mkdir()
            linked_data = root / "linked-data"
            linked_data.symlink_to(external, target_is_directory=True)
            legacy = root / "runtime" / "reviews.json"
            destination = linked_data / "chess-listener-library" / "reviews.json"

            result = study_store.migrate_legacy_library(
                destination=destination, legacy=legacy
            )

            self.assertEqual(result["action"], "none")
            self.assertFalse(destination.exists())

    def test_legacy_source_survives_if_preserved_target_changes_before_unlink(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            legacy = root / "runtime" / "reviews.json"
            destination = root / "library" / "reviews.json"
            legacy.parent.mkdir()
            expected = library_payload("source")
            replacement = library_payload("replacement")
            legacy.write_bytes(expected)
            original_compare = study_store._same_file_contents

            def replace_before_final_compare(source, target):
                Path(target).write_bytes(replacement)
                return original_compare(source, target)

            with mock.patch.object(
                study_store,
                "_same_file_contents",
                side_effect=replace_before_final_compare,
            ):
                with self.assertRaisesRegex(OSError, "source was preserved"):
                    study_store.migrate_legacy_library(
                        destination=destination, legacy=legacy
                    )

            self.assertEqual(legacy.read_bytes(), expected)
            self.assertEqual(destination.read_bytes(), replacement)

    def test_legacy_source_survives_if_different_target_appears_before_copy(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            legacy = root / "runtime" / "reviews.json"
            destination = root / "library" / "reviews.json"
            legacy.parent.mkdir()
            destination.parent.mkdir()
            expected = library_payload("source")
            appeared = library_payload("appeared")
            legacy.write_bytes(expected)

            def race_copy(_source, target):
                Path(target).write_bytes(appeared)
                raise FileExistsError("simulated create race")

            with mock.patch.object(
                study_store,
                "_atomic_copy_without_overwrite",
                side_effect=race_copy,
            ):
                with self.assertRaisesRegex(OSError, "different contents"):
                    study_store.migrate_legacy_library(
                        destination=destination, legacy=legacy
                    )

            self.assertEqual(legacy.read_bytes(), expected)
            self.assertEqual(destination.read_bytes(), appeared)


if __name__ == "__main__":
    unittest.main()
