#!/usr/bin/env python3
"""Small atomic JSON store for local reviews and cached engine results."""

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import uuid

import study as study_rules


SCHEMA_VERSION = 2
MAX_GAMES = 50
MAX_STUDIES = 100
MAX_BYTES = 16 * 1024 * 1024
GAME_RESULTS = {"1-0", "0-1", "1/2-1/2", "*"}
RUNTIME_DIRECTORY = "chess-listener"
LIBRARY_DIRECTORY = "chess-listener-library"
LIBRARY_FILENAME = "reviews.json"


class ReviewLibraryError(OSError):
    """An existing local library could not be used without risking data loss."""


def _library_error(path, reason):
    return ReviewLibraryError(
        f"Local library {path} {reason}. The existing file was left unchanged"
    )


def _data_home():
    configured = os.environ.get("XDG_DATA_HOME")
    if configured:
        selected = Path(configured)
        if not selected.is_absolute():
            raise ReviewLibraryError(
                "XDG_DATA_HOME must be an absolute path; no library file was changed"
            )
        return selected
    return Path.home() / ".local" / "share"


def _installed_runtime():
    directory = Path(__file__).resolve().parent
    marker = directory / ".chess-listener-install"
    try:
        details = marker.lstat()
    except OSError:
        return None
    return directory if stat.S_ISREG(details.st_mode) else None


def default_path():
    override = os.environ.get("CHESSLISTENER_LIBRARY")
    if override:
        selected = Path(override)
        if not selected.is_absolute():
            raise ReviewLibraryError(
                "CHESSLISTENER_LIBRARY must be an absolute path; "
                "no library file was changed"
            )
        runtime = _installed_runtime()
        if runtime is not None and _resolved_is_within(selected, runtime):
            raise ReviewLibraryError(
                "CHESSLISTENER_LIBRARY resolves inside the managed ChessListener "
                "runtime; no library file was changed"
            )
        return selected
    return _data_home() / LIBRARY_DIRECTORY / LIBRARY_FILENAME


def legacy_default_path():
    """Return the pre-0.9.5 library path inside the managed runtime prefix."""
    return _data_home() / RUNTIME_DIRECTORY / LIBRARY_FILENAME


def _path_stat(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _require_regular_file(path, description):
    details = _path_stat(path)
    if details is None:
        return None
    if not stat.S_ISREG(details.st_mode):
        raise OSError(f"{description} is not a regular file: {path}")
    return details


def _require_plain_parent_chain(path, description):
    """Reject directory aliases for data-preservation operations.

    A symlinked ``chess-listener-library`` directory can resolve back into the
    managed ``chess-listener`` runtime.  In that layout two different-looking
    ``reviews.json`` paths may be the same directory entry; treating them as
    duplicate copies and unlinking the legacy spelling deletes the only file.
    Inspect every existing parent without resolving it so that migration fails
    before it copies or unlinks anything.
    """
    current = Path(os.path.abspath(path)).parent
    while True:
        try:
            details = current.lstat()
        except FileNotFoundError:
            details = None
        if details is not None:
            if stat.S_ISLNK(details.st_mode):
                raise OSError(
                    f"{description} uses a symbolic-link directory: {current}"
                )
            if not stat.S_ISDIR(details.st_mode):
                raise OSError(
                    f"{description} parent is not a directory: {current}"
                )
        parent = current.parent
        if parent == current:
            return
        current = parent


def _resolved_is_within(path, directory):
    try:
        resolved_path = str(Path(path).resolve(strict=False))
        resolved_directory = str(Path(directory).resolve(strict=False))
        return os.path.commonpath([resolved_path, resolved_directory]) == resolved_directory
    except (OSError, ValueError) as error:
        raise OSError(f"could not validate library paths: {error}") from error


def _require_destination_outside_runtime(legacy, destination):
    if _resolved_is_within(destination, legacy.parent):
        raise OSError(
            "ChessListener user library resolves inside the managed runtime: "
            f"{destination}"
        )


def _require_safe_migration_paths(legacy, destination):
    _require_destination_outside_runtime(legacy, destination)
    _require_plain_parent_chain(legacy, "legacy ChessListener library")
    _require_plain_parent_chain(destination, "ChessListener user library")


def _fingerprint(details):
    return (
        details.st_dev,
        details.st_ino,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _same_file_contents(first, second):
    first_stat = _require_regular_file(first, "legacy ChessListener library")
    second_stat = _require_regular_file(second, "ChessListener user library")
    if first_stat is None or second_stat is None:
        return False
    return (
        first_stat.st_size == second_stat.st_size
        and _digest(first) == _digest(second)
    )


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_copy_without_overwrite(source, destination):
    """Publish a complete copy with create-if-absent semantics.

    Both temporary file and destination live in the same directory.  A hard
    link publishes the already-fsynced inode atomically and, unlike replace,
    cannot clobber a destination created by another ChessListener process.
    """
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".migrating", dir=destination.parent
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as input_file, os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            for chunk in iter(lambda: input_file.read(1024 * 1024), b""):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        persisted = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(persisted)
        finally:
            os.close(persisted)
        os.link(temporary, destination)
        temporary.unlink()
        _fsync_directory(destination.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _recovery_path(directory, source):
    digest = _digest(source)
    base = directory / f"reviews.legacy-{digest}.json"
    candidate = base
    suffix = 1
    while _path_stat(candidate) is not None:
        if _same_file_contents(source, candidate):
            return candidate
        candidate = directory / f"reviews.legacy-{digest}.{suffix}.json"
        suffix += 1
    return candidate


def migrate_legacy_library(destination=None, legacy=None, dry_run=False):
    """Move the old in-runtime library to the separate user-data directory.

    The authoritative destination is never overwritten.  If it already holds
    different bytes, the legacy file is atomically preserved beside it under a
    content-addressed recovery name.  The legacy source is unlinked only after
    a complete destination/recovery copy exists and only if the source path did
    not change during the operation.
    """
    destination = Path(destination) if destination is not None else (
        _data_home() / LIBRARY_DIRECTORY / LIBRARY_FILENAME
    )
    legacy = Path(legacy) if legacy is not None else legacy_default_path()
    result = {
        "action": "none",
        "source": legacy,
        "destination": destination,
        "recovery": None,
        "dry_run": bool(dry_run),
    }
    # This check is required even without a legacy file.  The installer and
    # uninstaller use the explicit legacy runtime path; accepting a new library
    # anywhere below that directory would let uninstall remove it recursively.
    _require_destination_outside_runtime(legacy, destination)
    if legacy == destination:
        return result

    source_stat = _require_regular_file(legacy, "legacy ChessListener library")
    if source_stat is None:
        return result
    _require_safe_migration_paths(legacy, destination)
    initial_fingerprint = _fingerprint(source_stat)

    destination_stat = _require_regular_file(
        destination, "ChessListener user library"
    )
    if destination_stat is not None:
        try:
            aliases_source = os.path.samefile(legacy, destination)
        except OSError as error:
            raise OSError(f"could not compare library paths: {error}") from error
        if aliases_source:
            raise OSError(
                "legacy and user library paths refer to the same file; "
                "the legacy file was preserved"
            )
    if destination_stat is None:
        result["action"] = "migrated"
        target = destination
    elif _same_file_contents(legacy, destination):
        result["action"] = "deduplicated"
        target = destination
    else:
        recovery = _recovery_path(destination.parent, legacy)
        result["action"] = "archived"
        result["recovery"] = recovery
        target = recovery

    if dry_run:
        return result

    if result["action"] != "deduplicated":
        _require_safe_migration_paths(legacy, destination)
        try:
            _atomic_copy_without_overwrite(legacy, target)
        except FileExistsError:
            # A concurrent migration won the create race.  It is safe to use
            # that file only when it contains the same complete source bytes.
            if not _same_file_contents(legacy, target):
                raise OSError(
                    f"migration target appeared with different contents: {target}"
                )

    current_stat = _require_regular_file(legacy, "legacy ChessListener library")
    if current_stat is None:
        return result
    if _fingerprint(current_stat) != initial_fingerprint:
        raise OSError(
            "legacy ChessListener library changed during migration; "
            "the source was preserved"
        )
    if not _same_file_contents(legacy, target):
        raise OSError(
            "the preserved library copy changed during migration; "
            "the legacy source was preserved"
        )
    legacy.unlink()
    _fsync_directory(legacy.parent)
    return result


def _print_migration(result):
    source = result["source"]
    destination = result["destination"]
    prefix = "Would " if result["dry_run"] else ""
    action = result["action"]
    if action == "none":
        print(f"User library: {destination} (no legacy migration needed)")
    elif action == "migrated":
        verb = "migrate" if result["dry_run"] else "Migrated"
        print(f"{prefix}{verb} saved games and studies: {source} -> {destination}")
    elif action == "deduplicated":
        verb = (
            "remove matching legacy copy"
            if result["dry_run"]
            else "Removed matching legacy copy"
        )
        print(f"{prefix}{verb}: {source}")
        print(f"User library remains at: {destination}")
    elif action == "archived":
        kept = "Would keep" if result["dry_run"] else "Kept"
        print(f"{kept} existing user library: {destination}")
        verb = "preserve" if result["dry_run"] else "Preserved"
        print(
            f"{prefix}{verb} different legacy library as recovery copy: "
            f"{result['recovery']}"
        )


def _migration_cli(arguments):
    arguments = list(arguments)
    if not arguments or arguments.pop(0) != "--migrate-legacy":
        print(
            "usage: study_store.py --migrate-legacy "
            "[--legacy /absolute/path/chess-listener/reviews.json] [--dry-run]",
            file=sys.stderr,
        )
        return 2
    legacy = None
    dry_run = False
    while arguments:
        option = arguments.pop(0)
        if option == "--dry-run" and not dry_run:
            dry_run = True
        elif option == "--legacy" and legacy is None and arguments:
            legacy = Path(arguments.pop(0))
        else:
            print(f"error: invalid migration option: {option}", file=sys.stderr)
            return 2
    if legacy is not None and (
        not legacy.is_absolute()
        or legacy.name != LIBRARY_FILENAME
        or legacy.parent.name != RUNTIME_DIRECTORY
    ):
        print(
            "error: --legacy must be an absolute "
            ".../chess-listener/reviews.json path",
            file=sys.stderr,
        )
        return 2
    try:
        result = migrate_legacy_library(legacy=legacy, dry_run=dry_run)
        _print_migration(result)
        return 0
    except OSError as error:
        print(
            f"error: could not preserve the ChessListener user library: {error}",
            file=sys.stderr,
        )
        return 1


def game_id(initial_fen, moves):
    material = (initial_fen + "\n" + "|".join(moves)).encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:20]


def record_id(record):
    """Return the stable identity of one locally stored game.

    Browser sessions are distinct even when the exact same moves are replayed
    (common while testing against a bot).  A session id therefore participates
    in the stored-record identity when one is available.  Imported/legacy
    records retain the content-derived id used by schema versions 1 and 2.

    Repeated end-of-game notifications for one session resolve to the same id,
    which makes completed-game saving naturally idempotent.
    """
    identifier = str(record.get("session_id") or "").strip()[:256]
    if not identifier:
        return game_id(record["initial_fen"], record["moves"])
    material = (
        "session\n" + identifier + "\n" + record["initial_fen"]
        + "\n" + "|".join(record["moves"])
    ).encode("utf-8")
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
        self.migration = None
        if path is not None:
            self.path = Path(path)
            return
        self.path = default_path()
        # An explicit library path is an exact opt-out: never inspect, move,
        # merge, or otherwise reinterpret a caller-managed archive.
        if not os.environ.get("CHESSLISTENER_LIBRARY"):
            self.migration = migrate_legacy_library(destination=self.path)

    @staticmethod
    def _empty():
        return {"version": SCHEMA_VERSION, "games": [], "studies": []}

    @staticmethod
    def _archive_guard(details, raw):
        return (
            details.st_dev,
            details.st_ino,
            details.st_size,
            details.st_mtime_ns,
            details.st_ctime_ns,
            hashlib.sha256(raw).digest(),
        )

    def _read_bytes(self):
        """Read one stable regular-file snapshot, or report a missing archive.

        ``lstat`` and ``O_NOFOLLOW`` make a library symlink an error rather
        than an invitation to read or replace some unrelated target.  The
        before/open/after identity checks also prevent a concurrently replaced
        file from being mistaken for the snapshot that mutations build on.
        """
        try:
            before = self.path.lstat()
        except FileNotFoundError:
            return None, None
        except OSError as error:
            raise _library_error(
                self.path, f"could not be inspected ({error})"
            ) from error
        if not stat.S_ISREG(before.st_mode):
            raise _library_error(self.path, "is not a regular file")
        if before.st_size > MAX_BYTES:
            raise _library_error(self.path, "exceeds the 16 MB safety limit")

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags)
        except OSError as error:
            raise _library_error(
                self.path, f"could not be read ({error})"
            ) from error
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
            ):
                raise _library_error(self.path, "changed while it was being opened")
            chunks = []
            total = 0
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_BYTES:
                    raise _library_error(
                        self.path, "exceeds the 16 MB safety limit"
                    )
                chunks.append(chunk)
            after_read = os.fstat(descriptor)
        except OSError as error:
            if isinstance(error, ReviewLibraryError):
                raise
            raise _library_error(
                self.path, f"could not be read ({error})"
            ) from error
        finally:
            os.close(descriptor)

        try:
            after_path = self.path.lstat()
        except OSError as error:
            raise _library_error(
                self.path, f"changed while it was being read ({error})"
            ) from error
        if (
            _fingerprint(opened) != _fingerprint(after_read)
            or _fingerprint(opened) != _fingerprint(after_path)
        ):
            raise _library_error(self.path, "changed while it was being read")
        raw = b"".join(chunks)
        return raw, self._archive_guard(after_path, raw)

    def _validate_analysis_line(self, line, prefix):
        if not isinstance(line, dict):
            raise _library_error(self.path, f"has a non-object {prefix} line")
        cp = line.get("cp")
        mate = line.get("mate")
        try:
            if cp is not None:
                int(cp)
            if mate is not None:
                int(mate)
            int(line.get("rank", 1))
            int(line.get("depth", 0))
        except (TypeError, ValueError) as error:
            raise _library_error(self.path, f"has an invalid {prefix} score") from error
        pv = line.get("pv", [])
        if not isinstance(pv, list) or any(not isinstance(move, str) for move in pv):
            raise _library_error(self.path, f"has an invalid {prefix} principal variation")

    def _validate_game(self, game, index):
        prefix = f"game entry {index + 1}"
        identifier = game.get("id")
        initial_fen = game.get("initial_fen")
        moves = game.get("moves")
        if not isinstance(identifier, str) or not identifier:
            raise _library_error(self.path, f"has an invalid {prefix} id")
        if not isinstance(initial_fen, str) or not initial_fen.strip():
            raise _library_error(self.path, f"has an invalid {prefix} initial_fen")
        if not isinstance(moves, list) or any(
            not isinstance(move, str) for move in moves
        ):
            raise _library_error(self.path, f"has an invalid {prefix} move list")
        try:
            canonical = study_rules.canonical_fen(initial_fen)
            board = study_rules.san.Board(canonical)
            replayed_positions = [board.fen()]
            for move in moves:
                board = board.apply_uci(move)
                replayed_positions.append(board.fen())
        except (AttributeError, ValueError) as error:
            raise _library_error(
                self.path, f"has an illegal {prefix} position or move ({error})"
            ) from error
        metadata = game.get("metadata", {})
        reviews = game.get("reviews", {})
        if not isinstance(metadata, dict) or not isinstance(reviews, dict):
            raise _library_error(self.path, f"has invalid {prefix} metadata or reviews")
        for key, review in reviews.items():
            if not isinstance(key, str) or not isinstance(review, dict):
                raise _library_error(self.path, f"has an invalid cached review in {prefix}")
            for field in ("results", "positions", "position_analyses"):
                if field in review and not isinstance(review[field], list):
                    raise _library_error(
                        self.path, f"has an invalid cached-review {field} in {prefix}"
                    )
            if "settings" in review and not isinstance(review["settings"], dict):
                raise _library_error(
                    self.path, f"has invalid cached-review settings in {prefix}"
                )
            try:
                int(review.get("created_at", 0))
            except (TypeError, ValueError) as error:
                raise _library_error(
                    self.path, f"has an invalid cached-review timestamp in {prefix}"
                ) from error
            results = review.get("results", [])
            positions = review.get("positions", [])
            position_analyses = review.get("position_analyses", [])
            if len(results) != len(moves) or positions != replayed_positions:
                raise _library_error(
                    self.path, f"has an incomplete cached review in {prefix}"
                )
            if position_analyses and len(position_analyses) != len(positions):
                raise _library_error(
                    self.path, f"has invalid position analyses in {prefix}"
                )
            for ply_index, result in enumerate(results, start=1):
                if not isinstance(result, dict):
                    raise _library_error(
                        self.path, f"has a non-object cached result in {prefix}"
                    )
                required = (
                    "ply", "loss", "classification", "san", "fen_before",
                    "fen_after", "uci", "eval", "depth",
                )
                if any(field not in result for field in required):
                    raise _library_error(
                        self.path, f"has an incomplete cached result in {prefix}"
                    )
                if type(result["ply"]) is not int or type(result["depth"]) is not int:
                    raise _library_error(
                        self.path, f"has invalid cached-result numbers in {prefix}"
                    )
                result_ply = result["ply"]
                loss = result["loss"]
                if (
                    isinstance(loss, bool)
                    or not isinstance(loss, (int, float))
                    or not math.isfinite(float(loss))
                    or loss < 0
                    or result["depth"] < 0
                ):
                    raise _library_error(
                        self.path, f"has an invalid cached-result loss in {prefix}"
                    )
                if (
                    result_ply != ply_index
                    or not isinstance(result["classification"], str)
                    or not isinstance(result["san"], str)
                    or not isinstance(result.get("best", ""), str)
                    or (
                        result.get("best", "")
                        and not study_rules.UCI_RE.fullmatch(result["best"])
                    )
                    or result["fen_before"] != positions[ply_index - 1]
                    or result["fen_after"] != positions[ply_index]
                    or result["uci"] != moves[ply_index - 1]
                ):
                    raise _library_error(
                        self.path, f"has an inconsistent cached result in {prefix}"
                    )
                if result.get("best"):
                    try:
                        study_rules.san.Board(result["fen_before"]).apply_uci(
                            result["best"]
                        )
                    except ValueError as error:
                        raise _library_error(
                            self.path, f"has an illegal cached best move in {prefix}"
                        ) from error
                lines = result.get("lines", [])
                if not isinstance(lines, list):
                    raise _library_error(
                        self.path, f"has invalid cached-result lines in {prefix}"
                    )
                for line in lines:
                    self._validate_analysis_line(line, f"{prefix} cached-result")
                score = result.get("eval_score", {})
                self._validate_analysis_line(score, f"{prefix} cached-result evaluation")
            for analysis in position_analyses:
                if not isinstance(analysis, list):
                    raise _library_error(
                        self.path, f"has invalid position analyses in {prefix}"
                    )
                for line in analysis:
                    self._validate_analysis_line(line, f"{prefix} position-analysis")

    def _validate_study(self, item, index):
        try:
            clean = study_rules.normalise_study(item)
        except (KeyError, TypeError, ValueError) as error:
            raise _library_error(
                self.path, f"has an invalid study entry {index + 1} ({error})"
            ) from error
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier or clean.get("id") != identifier:
            raise _library_error(
                self.path, f"has an invalid study entry {index + 1} id"
            )
        for field in ("created_at", "updated_at"):
            try:
                int(item.get(field, 0))
            except (TypeError, ValueError) as error:
                raise _library_error(
                    self.path,
                    f"has an invalid study entry {index + 1} {field}",
                ) from error

    def _validate_games(self, games):
        if not isinstance(games, list) or len(games) > MAX_GAMES:
            raise _library_error(self.path, "has an invalid games collection")
        for index, game in enumerate(games):
            if not isinstance(game, dict):
                raise _library_error(self.path, "has a non-object game entry")
            self._validate_game(game, index)

    def _validate_studies(self, studies):
        if not isinstance(studies, list) or len(studies) > MAX_STUDIES:
            raise _library_error(self.path, "has an invalid studies collection")
        for index, item in enumerate(studies):
            if not isinstance(item, dict):
                raise _library_error(self.path, "has a non-object study entry")
            self._validate_study(item, index)

    def _load_validated(self):
        raw, guard = self._read_bytes()
        if raw is None:
            return self._empty(), None
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise _library_error(self.path, "is not valid UTF-8 JSON") from error
        try:
            data = json.loads(text)
        except (ValueError, json.JSONDecodeError) as error:
            raise _library_error(
                self.path, f"contains invalid JSON ({error})"
            ) from error
        if not isinstance(data, dict):
            raise _library_error(self.path, "has an invalid top-level JSON value")

        version = data.get("version")
        games = data.get("games")
        if type(version) is not int:
            raise _library_error(self.path, "has an invalid schema")
        self._validate_games(games)
        # 0.8.0 used schema 1 for reviews only.  Migration is intentionally
        # lossless and lazy: the next mutation writes schema 2 atomically.
        if version == 1:
            return ({
                "version": SCHEMA_VERSION,
                "games": games,
                "studies": [],
            }, guard)
        studies = data.get("studies")
        if version != SCHEMA_VERSION:
            raise _library_error(
                self.path, f"uses unsupported schema version {version}"
            )
        self._validate_studies(studies)
        return data, guard

    def load(self):
        data, _guard = self._load_validated()
        return data

    def archive_present(self):
        """Return whether any directory entry currently occupies the path."""
        try:
            self.path.lstat()
            return True
        except FileNotFoundError:
            return False
        except OSError as error:
            raise _library_error(
                self.path, f"could not be inspected ({error})"
            ) from error

    def list_games(self):
        return list(self.load()["games"])

    def list_studies(self, query=""):
        output = []
        words = [word.casefold() for word in str(query).split() if word]
        for raw in self.load()["studies"]:
            item = study_rules.normalise_study(raw)
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
        source = str(record.get("source", previous.get("source", "")) or "")
        if source not in {"exact", "inferred", "manual"}:
            source = ""
        result = str(record.get("result", previous.get("result", "*")) or "*")
        if result not in GAME_RESULTS:
            result = "*"
        return {
            "id": record_id(record),
            "initial_fen": record["initial_fen"],
            "moves": list(record["moves"]),
            "result": result,
            "label": record.get("label") or f"Local game · {len(record['moves'])} plies",
            "metadata": normalise_metadata(record.get("metadata")),
            "imported": bool(record.get("imported", previous.get("imported", False))),
            "completed": bool(record.get("completed", previous.get("completed", False))),
            "history_complete": bool(
                record.get("history_complete", previous.get("history_complete", False))
            ),
            "position_only": bool(
                record.get("position_only", previous.get("position_only", False))
            ),
            "source": source,
            "session_id": str(
                record.get("session_id", previous.get("session_id", "")) or ""
            )[:256],
            "updated_at": int(time.time()),
            "reviews": reviews,
        }

    def save_record(self, record):
        """Upsert an imported game even before an engine review is run."""
        data, guard = self._load_validated()
        identifier = record_id(record)
        previous = next(
            (game for game in data["games"] if game.get("id") == identifier), {}
        )
        games = [game for game in data["games"] if game.get("id") != identifier]
        games.insert(0, self._entry(record, previous, dict(previous.get("reviews") or {})))
        data["games"] = games[:MAX_GAMES]
        self._write(data, guard)
        return identifier

    def save_completed_game(self, record):
        """Idempotently save one completed browser game.

        Returns ``(identifier, created)`` so the UI can distinguish the first
        save from repeated native/content-script end events without keeping a
        second deduplication database.
        """
        completed = dict(record)
        completed["completed"] = True
        identifier = record_id(completed)
        created = self.find(identifier) is None
        saved = self.save_record(completed)
        return saved, created

    def save_review(self, record, settings, results, positions, position_analyses=None):
        data, guard = self._load_validated()
        identifier = record_id(record)
        previous = next(
            (game for game in data["games"] if game.get("id") == identifier), {}
        )
        games = [game for game in data["games"] if game.get("id") != identifier]
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
        self._write(data, guard)
        return identifier, key

    def delete(self, identifier):
        data, guard = self._load_validated()
        remaining = [game for game in data["games"] if game.get("id") != identifier]
        if len(remaining) == len(data["games"]):
            return False
        data["games"] = remaining
        self._write(data, guard)
        return True

    def save_study(self, raw):
        item = study_rules.normalise_study(raw)
        data, guard = self._load_validated()
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
        self._write(data, guard)
        return identifier

    def delete_study(self, identifier):
        data, guard = self._load_validated()
        remaining = [
            item for item in data["studies"] if item.get("id") != identifier
        ]
        if len(remaining) == len(data["studies"]):
            return False
        data["studies"] = remaining
        self._write(data, guard)
        return True

    def _write(self, data, expected_guard):
        data = {
            "version": SCHEMA_VERSION,
            "games": list(data.get("games") or [])[:MAX_GAMES],
            "studies": list(data.get("studies") or [])[:MAX_STUDIES],
        }
        self._validate_games(data["games"])
        self._validate_studies(data["studies"])
        payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        if len(payload.encode("utf-8")) > MAX_BYTES:
            raise ValueError("Review library has reached its 16 MB limit")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".reviews-", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            _current_raw, current_guard = self._read_bytes()
            if current_guard != expected_guard:
                raise _library_error(
                    self.path,
                    "changed after it was loaded; retry after checking the file",
                )
            if expected_guard is None:
                # Create-if-absent means a library that appeared after the
                # missing-file read can never be overwritten by this process.
                try:
                    os.link(temporary, self.path)
                except FileExistsError as error:
                    raise _library_error(
                        self.path,
                        "appeared while a new library was being saved",
                    ) from error
                os.unlink(temporary)
            else:
                os.replace(temporary, self.path)
            _fsync_directory(self.path.parent)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    raise SystemExit(_migration_cli(sys.argv[1:]))
