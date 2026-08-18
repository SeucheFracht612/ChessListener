#!/usr/bin/env python3
"""Local, cancellable post-game review for ChessListener.

The live engine lanes deliberately never call this module.  A review owns a
separate Stockfish process and works from the native host's legally replayed
UCI game record, so deeper retrospective work cannot delay board capture.
"""

import os
import queue
import select
import shutil
import subprocess
import threading
import time

import san


MATE_SCORE = 100000
DEFAULT_STOCKFISH = "/usr/games/stockfish"


def resolve_stockfish_executable(settings=None, *, environ=None,
                                 is_executable=None, which=None):
    """Resolve the local UCI engine using the same order as installation.

    A job-local ``engine`` remains useful for tests and advanced callers.  The
    documented environment override comes next.  Otherwise prefer the common
    distro location before consulting PATH, matching ``install.sh`` and the
    live native host.

    The lookup collaborators are injectable so discovery tests never depend
    on which packages happen to be installed on the machine running them.
    """
    settings = settings if isinstance(settings, dict) else {}
    environment = os.environ if environ is None else environ
    configured = settings.get("engine") or environment.get(
        "CHESSLISTENER_STOCKFISH"
    )
    if configured:
        return configured

    executable_check = is_executable or (
        lambda path: os.access(path, os.X_OK)
    )
    if executable_check(DEFAULT_STOCKFISH):
        return DEFAULT_STOCKFISH

    path_lookup = which or shutil.which
    discovered = path_lookup("stockfish")
    if discovered:
        return discovered

    raise FileNotFoundError(
        "Stockfish was not found at /usr/games/stockfish or on PATH; "
        "install Stockfish or set CHESSLISTENER_STOCKFISH"
    )


def score_value(cp=None, mate=None, **_ignored):
    if mate is not None:
        return (MATE_SCORE - min(abs(int(mate)), 999)) * (1 if int(mate) > 0 else -1)
    return int(cp or 0)


def evaluation_loss(before, after, side):
    """Centipawn-equivalent loss for the player who just moved."""
    delta = score_value(**before) - score_value(**after)
    return max(0, delta if side == "w" else -delta)


def classify_move(loss, played, best, thresholds=None):
    limits = thresholds or (15, 35, 80, 160, 300)
    if played == best or loss <= limits[0]:
        return "Best"
    if loss <= limits[1]:
        return "Excellent"
    if loss <= limits[2]:
        return "Good"
    if loss <= limits[3]:
        return "Inaccuracy"
    if loss <= limits[4]:
        return "Mistake"
    return "Blunder"


def format_eval(score):
    mate = score.get("mate")
    if mate is not None:
        return f"#{mate}"
    return f"{int(score.get('cp') or 0) / 100:+.2f}"


def _pgn_header_value(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def annotated_pgn(initial_fen, moves, reviews=None, result="*", metadata=None):
    board = san.Board(initial_fen)
    tokens = []
    reviews = reviews or []
    for index, move in enumerate(moves):
        notation = board.san(move)
        if board.white_to_move:
            tokens.append(f"{board.fullmove_number}.")
        elif index == 0:
            tokens.append(f"{board.fullmove_number}...")
        tokens.append(notation)
        if index < len(reviews):
            item = reviews[index]
            comment = f"{item['classification']}; loss {item['loss'] / 100:.2f}; eval {item['eval']}"
            if item.get("best") and item["best"] != move:
                try:
                    best_name = board.san(item["best"])
                except ValueError:
                    best_name = item["best"]
                comment += f"; best {best_name}"
            tokens.append("{" + comment.replace("}", "") + "}")
        board = board.apply_uci(move)
    if result == "*" and not board.legal_moves():
        if board.in_check(board.white_to_move):
            result = "0-1" if board.white_to_move else "1-0"
        else:
            result = "1/2-1/2"
    metadata = metadata if isinstance(metadata, dict) else {}
    headers = []
    seen = set()
    for key in ("Event", "Site", "Date", "Round", "White", "Black", "WhiteElo", "BlackElo"):
        value = metadata.get(key)
        if value is not None and value != "":
            headers.append(f'[{key} "{_pgn_header_value(value)}"]')
            seen.add(key)
    if "Event" not in seen:
        headers.append('[Event "ChessListener Local Review"]')
    if "Site" not in seen:
        headers.append('[Site "Local"]')
    headers.append(f'[Result "{_pgn_header_value(result)}"]')
    standard = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
    if initial_fen != standard:
        headers.extend(('[SetUp "1"]', f'[FEN "{initial_fen}"]'))
    return "\n".join(headers) + "\n\n" + " ".join(tokens + [result]) + "\n"


class UciEngine:
    def __init__(self, path, threads=2, multipv=2):
        self.process = subprocess.Popen(
            [path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )
        self.buffer = bytearray()
        self.write_lock = threading.Lock()
        self.send("uci")
        self.wait("uciok", 10)
        self.send(f"setoption name Threads value {max(1, threads)}")
        self.send(f"setoption name MultiPV value {max(1, multipv)}")
        self.send("isready")
        self.wait("readyok", 10)

    def send(self, line):
        with self.write_lock:
            self.process.stdin.write((line + "\n").encode("ascii"))
            self.process.stdin.flush()

    def read_line(self, deadline):
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                raw = bytes(self.buffer[:newline])
                del self.buffer[:newline + 1]
                return raw.decode("utf-8", "replace").strip()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Stockfish response timed out")
            ready, _write, _error = select.select(
                [self.process.stdout.fileno()], [], [], remaining
            )
            if not ready:
                raise TimeoutError("Stockfish response timed out")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError("Stockfish closed its output")
            self.buffer.extend(chunk)

    def wait(self, prefix, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.read_line(deadline)
            if line.startswith(prefix):
                return line
        raise TimeoutError(f"Stockfish did not answer {prefix}")

    def analyse(self, fen, milliseconds, multipv, cancelled):
        self.send(f"setoption name MultiPV value {max(1, multipv)}")
        self.send(f"position fen {fen}")
        self.send(f"go movetime {max(25, milliseconds)}")
        lines = {}
        deadline = time.monotonic() + max(10.0, milliseconds / 1000.0 + 5.0)
        stop_sent = False
        while True:
            if cancelled() and not stop_sent:
                self.send("stop")
                stop_sent = True
                deadline = min(deadline, time.monotonic() + 3.0)
            try:
                raw = self.read_line(min(deadline, time.monotonic() + 0.1))
            except TimeoutError:
                if time.monotonic() >= deadline:
                    raise
                continue
            fields = raw.split()
            if not fields:
                continue
            if fields[0] == "bestmove":
                break
            if fields[0] != "info" or "pv" not in fields or "depth" not in fields:
                continue
            try:
                rank = int(fields[fields.index("multipv") + 1]) if "multipv" in fields else 1
                depth = int(fields[fields.index("depth") + 1])
                score_at = fields.index("score")
                score_kind, score_raw = fields[score_at + 1], int(fields[score_at + 2])
                pv_at = fields.index("pv")
            except (ValueError, IndexError):
                continue
            # UCI scores are from the side-to-move POV.  The rest of
            # ChessListener uses White POV so a review remains comparable
            # across consecutive positions.
            white_sign = 1 if fen.split()[1] == "w" else -1
            score_raw *= white_sign
            score = {"cp": score_raw, "mate": None}
            if score_kind == "mate":
                score = {"cp": None, "mate": score_raw}
            lines[rank] = {"rank": rank, "depth": depth, **score, "pv": fields[pv_at + 1:]}
        return [lines[key] for key in sorted(lines)]

    def close(self):
        if self.process.poll() is None:
            try:
                self.send("quit")
                self.process.wait(timeout=2)
            except Exception:
                self.process.kill()
                self.process.wait(timeout=2)
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except (AttributeError, OSError):
                pass


class ReviewJob(threading.Thread):
    def __init__(self, initial_fen, moves, settings, output, identity=None):
        super().__init__(daemon=True)
        self.initial_fen = initial_fen
        self.moves = list(moves)
        self.settings = dict(settings)
        self.output = output
        # Copy the caller-created immutable game/settings/generation token.
        # Every queue item carries it, so a late worker can never be mistaken
        # for whichever game happens to be visible when it finishes.
        self.identity = dict(identity or {})
        self.cancel_event = threading.Event()
        self.engine = None

    def cancel(self):
        self.cancel_event.set()
        if self.engine is not None:
            try:
                self.engine.send("stop")
            except (BrokenPipeError, OSError):
                pass

    def emit(self, kind, **payload):
        self.output.put({"type": kind, "review_identity": dict(self.identity), **payload})

    def run(self):
        engine = None
        try:
            path = resolve_stockfish_executable(self.settings)
            engine = UciEngine(path, self.settings.get("threads", 2), self.settings.get("lines", 2))
            self.engine = engine
            board = san.Board(self.initial_fen)
            positions = [board.fen()]
            sans = []
            for move in self.moves:
                sans.append(board.san(move))
                board = board.apply_uci(move)
                positions.append(board.fen())

            analyses = []
            for index, fen in enumerate(positions):
                if self.cancel_event.is_set():
                    self.emit("cancelled")
                    return
                terminal = san.Board(fen)
                if not terminal.legal_moves():
                    if terminal.in_check(terminal.white_to_move):
                        mate = -1 if terminal.white_to_move else 1
                        lines = [{"rank": 1, "depth": 0, "cp": None,
                                  "mate": mate, "pv": []}]
                    else:
                        lines = [{"rank": 1, "depth": 0, "cp": 0,
                                  "mate": None, "pv": []}]
                else:
                    lines = engine.analyse(
                        fen, self.settings.get("time_ms", 350),
                        self.settings.get("lines", 2), self.cancel_event.is_set,
                    )
                analyses.append(lines)
                self.emit("progress", done=index + 1, total=len(positions))

            reviews = []
            for index, move in enumerate(self.moves):
                before_lines = analyses[index]
                after_lines = analyses[index + 1]
                if not before_lines or not after_lines:
                    raise RuntimeError("Stockfish returned no review line")
                before, after = before_lines[0], after_lines[0]
                side = positions[index].split()[1]
                loss = evaluation_loss(before, after, side)
                best = before.get("pv", [""])[0] if before.get("pv") else ""
                classification = classify_move(
                    loss, move, best, self.settings.get("thresholds")
                )
                reviews.append({
                    "ply": index + 1, "uci": move, "san": sans[index],
                    "classification": classification, "loss": loss,
                    "eval": format_eval(after), "eval_score": after,
                    "best": best, "depth": before.get("depth", 0),
                    "lines": before_lines,
                    "fen_before": positions[index], "fen_after": positions[index + 1],
                })
            self.emit(
                "complete", reviews=reviews, positions=positions, sans=sans,
                # One principal score per game position is sufficient for the
                # graph because full pre-move alternatives already live in
                # each review row. A zero-move FEN has no row, so retain its
                # configured MultiPV lines for the position detail instead.
                position_analyses=(
                    [list(analyses[0])] if not self.moves
                    else [lines[:1] for lines in analyses]
                ),
            )
        except Exception as error:
            self.emit("error", message=str(error))
        finally:
            if engine is not None:
                engine.close()
            self.engine = None


def start_review(initial_fen, moves, settings=None, identity=None):
    output = queue.Queue()
    job = ReviewJob(initial_fen, moves, settings or {}, output, identity)
    job.start()
    return job, output


class PositionJob(threading.Thread):
    """One isolated Analysis-Lab-style search for a review branch node."""
    def __init__(self, fen, settings, output, generation):
        super().__init__(daemon=True)
        self.fen = fen
        self.settings = dict(settings)
        self.output = output
        self.generation = generation
        self.cancel_event = threading.Event()
        self.engine = None

    def cancel(self):
        self.cancel_event.set()
        if self.engine is not None:
            try:
                self.engine.send("stop")
            except (BrokenPipeError, OSError):
                pass

    def run(self):
        engine = None
        try:
            board = san.Board(self.fen)
            if not board.legal_moves():
                mate = None
                cp = 0
                if board.in_check(board.white_to_move):
                    mate = -1 if board.white_to_move else 1
                    cp = None
                lines = [{"rank": 1, "depth": 0, "cp": cp,
                          "mate": mate, "pv": []}]
            else:
                path = resolve_stockfish_executable(self.settings)
                engine = UciEngine(
                    path, self.settings.get("threads", 2),
                    self.settings.get("lines", 2),
                )
                self.engine = engine
                lines = engine.analyse(
                    self.fen, self.settings.get("time_ms", 350),
                    self.settings.get("lines", 2), self.cancel_event.is_set,
                )
            if not self.cancel_event.is_set():
                self.output.put({"type": "position_complete", "fen": self.fen,
                                 "generation": self.generation, "lines": lines})
        except Exception as error:
            if not self.cancel_event.is_set():
                self.output.put({"type": "position_error", "generation": self.generation,
                                 "message": str(error)})
        finally:
            if engine is not None:
                engine.close()
            self.engine = None


def start_position_analysis(fen, settings=None, generation=0):
    output = queue.Queue()
    job = PositionJob(fen, settings or {}, output, generation)
    job.start()
    return job, output
