#!/usr/bin/env python3
"""Small atomic JSON store for local reviews and cached engine results."""

import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

import study as study_rules


SCHEMA_VERSION = 2
MAX_GAMES = 50
MAX_STUDIES = 100
MAX_BYTES = 16 * 1024 * 1024


def default_path():
    override = os.environ.get("CHESSLISTENER_LIBRARY")
    if override:
        return Path(override)
    root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "chess-listener" / "reviews.json"


def game_id(initial_fen, moves):
    material = (initial_fen + "\n" + "|".join(moves)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:20]


def settings_key(settings):
    stable = {
        "time_ms": int(settings.get("time_ms", 350)),
        "lines": int(settings.get("lines", 2)),
        "threads": int(settings.get("threads", 2)),
        "thresholds": list(settings.get("thresholds") or (15, 35, 80, 160, 300)),
    }
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]


def normalise_metadata(metadata):
    """Keep the local library human-readable and bounded.

    PGN headers are untrusted file input.  The parser already limits them, but
    the store also validates its public API so future importers cannot place
    arbitrary objects or unbounded strings in the JSON archive.
    """
    if not isinstance(metadata, dict):
        return {}
    output = {}
    for key, value in list(metadata.items())[:64]:
        name = str(key)[:64]
        if not name or not name.replace("_", "").isalnum():
            continue
        output[name] = str(value)[:1024]
    return output


class ReviewStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else default_path()

    def load(self):
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, ValueError, json.JSONDecodeError):
            return {"version": SCHEMA_VERSION, "games": [], "studies": []}
        # 0.8.0 used schema 1 for reviews only.  Migration is intentionally
        # lossless and lazy: the next mutation writes schema 2 atomically.
        if data.get("version") == 1 and isinstance(data.get("games"), list):
            return {
                "version": SCHEMA_VERSION,
                "games": data["games"],
                "studies": [],
            }
        if (
            data.get("version") != SCHEMA_VERSION
            or not isinstance(data.get("games"), list)
            or not isinstance(data.get("studies"), list)
        ):
            return {"version": SCHEMA_VERSION, "games": [], "studies": []}
        return data

    def list_games(self):
        return list(self.load()["games"])

    def list_studies(self, query=""):
        output = []
        words = [word.casefold() for word in str(query).split() if word]
        for raw in self.load()["studies"]:
            try:
                item = study_rules.normalise_study(raw)
            except ValueError:
                continue
            item["created_at"] = int(raw.get("created_at", 0))
            item["updated_at"] = int(raw.get("updated_at", 0))
            haystack = " ".join(
                [item.get("title", "")]
                + list(item.get("metadata", {}).values())
                + [
                    value
                    for node in item["nodes"].values()
                    for value in (
                        node.get("name", ""), node.get("comment", ""),
                        node.get("move", ""),
                    )
                ]
            ).casefold()
            if all(word in haystack for word in words):
                output.append(item)
        return output

    def find(self, identifier):
        return next(
            (game for game in self.load()["games"] if game.get("id") == identifier),
            None,
        )

    def find_study(self, identifier):
        return next(
            (item for item in self.list_studies() if item.get("id") == identifier),
            None,
        )

    def cached_review(self, identifier, key):
        game = self.find(identifier)
        if game is None:
            return None
        cached = game.get("reviews", {}).get(key)
        return cached if isinstance(cached, dict) else None

    def _entry(self, record, previous, reviews):
        return {
            "id": game_id(record["initial_fen"], record["moves"]),
            "initial_fen": record["initial_fen"],
            "moves": list(record["moves"]),
            "result": record.get("result", "*"),
            "label": record.get("label") or f"Local game · {len(record['moves'])} plies",
            "metadata": normalise_metadata(record.get("metadata")),
            "imported": bool(record.get("imported", previous.get("imported", False))),
            "updated_at": int(time.time()),
            "reviews": reviews,
        }

    def save_record(self, record):
        """Upsert an imported game even before an engine review is run."""
        data = self.load()
        identifier = game_id(record["initial_fen"], record["moves"])
        previous = next(
            (game for game in data["games"] if game.get("id") == identifier), {}
        )
        games = [game for game in data["games"] if game.get("id") != identifier]
        games.insert(0, self._entry(record, previous, dict(previous.get("reviews") or {})))
        data["games"] = games[:MAX_GAMES]
        self._write(data)
        return identifier

    def save_review(self, record, settings, results, positions, position_analyses=None):
        data = self.load()
        identifier = game_id(record["initial_fen"], record["moves"])
        games = [game for game in data["games"] if game.get("id") != identifier]
        previous = next(
            (game for game in data["games"] if game.get("id") == identifier), {}
        )
        reviews = dict(previous.get("reviews") or {})
        key = settings_key(settings)
        reviews[key] = {
            "settings": {
                "time_ms": int(settings.get("time_ms", 350)),
                "lines": int(settings.get("lines", 2)),
                "threads": int(settings.get("threads", 2)),
                "thresholds": list(settings.get("thresholds") or (15, 35, 80, 160, 300)),
            },
            "results": results,
            "positions": positions,
            "position_analyses": position_analyses or [],
            "created_at": int(time.time()),
        }
        entry = self._entry(record, previous, reviews)
        games.insert(0, entry)
        data["games"] = games[:MAX_GAMES]
        self._write(data)
        return identifier, key

    def delete(self, identifier):
        data = self.load()
        remaining = [game for game in data["games"] if game.get("id") != identifier]
        if len(remaining) == len(data["games"]):
            return False
        data["games"] = remaining
        self._write(data)
        return True

    def save_study(self, raw):
        item = study_rules.normalise_study(raw)
        data = self.load()
        identifier = item.get("id")
        if not identifier:
            identifier = uuid.uuid4().hex[:20]
            item["id"] = identifier
        previous = next(
            (saved for saved in data["studies"] if saved.get("id") == identifier),
            {},
        )
        now = int(time.time())
        item["created_at"] = int(previous.get("created_at", now))
        item["updated_at"] = now
        studies = [
            saved for saved in data["studies"] if saved.get("id") != identifier
        ]
        studies.insert(0, item)
        data["studies"] = studies[:MAX_STUDIES]
        self._write(data)
        return identifier

    def delete_study(self, identifier):
        data = self.load()
        remaining = [
            item for item in data["studies"] if item.get("id") != identifier
        ]
        if len(remaining) == len(data["studies"]):
            return False
        data["studies"] = remaining
        self._write(data)
        return True

    def _write(self, data):
        data = {
            "version": SCHEMA_VERSION,
            "games": list(data.get("games") or [])[:MAX_GAMES],
            "studies": list(data.get("studies") or [])[:MAX_STUDIES],
        }
        payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        if len(payload.encode("utf-8")) > MAX_BYTES:
            raise ValueError("Review library has reached its 16 MB limit")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".reviews-", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
