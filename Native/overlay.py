#!/usr/bin/env python3
"""ChessListener startup window and always-on-top analysis overlay.

The native host sends one JSON object per line on stdin. This process reserves
stdout for a small control protocol:

    START protocol=4 ui_version=0.9.0 budget=400 maia=1900 threads=2 multipv=3
    SET   budget=900 maia=1600 threads=4 multipv=3 explore_budget=-1
    RESCAN <session-id>
    FEN <session-id> rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1
    EXPLORE_START <session-id> <six-field-fen>[|uci1,uci2,...]
    EXPLORE_MOVE <session-id> <branch-id> <node-id> <uci>
    EXPLORE_GOTO <session-id> <branch-id> <node-id>
    EXPLORE_LIVE <session-id> <branch-id>
    EXPLORE_RESUME <session-id> <branch-id> <node-id>
    RESTART <session-id>
    STOP <session-id>
    QUIT <session-id>  # bare QUIT is used before a session starts

All diagnostics go to stderr, which the native host redirects to a local file.

Two rules keep this window responsive while a strong player fires off a premove
sequence:

  * the board is ONE widget with one paintEvent, not 64 QLabels carrying
    per-square stylesheets. Setting a stylesheet invalidates Qt's style cache
    for the whole tree, so the old design paid for 64 full style recomputations
    per position -- a burst of moves could stall the event loop for seconds.
  * incoming frames are coalesced. Only the newest state is ever painted, at
    most once per FRAME_INTERVAL_MS, no matter how fast the host publishes.
"""

import json
import math
import os
import sys
import queue
import time

try:
    import san as san_rules
except ImportError:  # pragma: no cover - overlay must still run without it
    san_rules = None

try:
    import explanations as explanation_rules
except ImportError:  # pragma: no cover - optional during source upgrades
    explanation_rules = None

try:
    import review as review_rules
except ImportError:  # pragma: no cover - source upgrade without review module
    review_rules = None

try:
    import study_store
except ImportError:  # pragma: no cover - source upgrade without library module
    study_store = None

try:
    import study as study_rules
except ImportError:  # pragma: no cover - source upgrade without study module
    study_rules = None

try:
    import pgn_import
except ImportError:  # pragma: no cover - source upgrade without import module
    pgn_import = None

from PyQt6.QtCore import (
    QPointF,
    QRectF,
    QSettings,
    QSocketNotifier,
    Qt,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRawFont,
    QTransform,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QFileDialog,
    QInputDialog,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

APP_ID = "chess-overlay"
ORGANIZATION = "ChessListener"
APPLICATION = "ChessListener"
APP_VERSION = "0.9.0"
PROTOCOL_VERSION = 4

FRAME_INTERVAL_MS = 16
SETTINGS_DEBOUNCE_MS = 300
STATUS_LINGER_MS = 4000
GEOMETRY_SAVE_MS = 600

# Frameless by default: this window floats over the browser, and a full title
# bar is a lot of chrome for a 340 px panel. KDE and GNOME both handle
# startSystemMove() on a frameless window, but set CHESSLISTENER_DECORATED=1 if
# a compositor disagrees.
DECORATED = os.environ.get("CHESSLISTENER_DECORATED", "") == "1"

OPACITY_CHOICES = ((100, "Opaque"), (94, "94%"), (86, "86%"), (78, "78%"))

# Board palette. Higher contrast between the two square colours than before,
# and pieces are filled shapes with a dark outline rather than glyphs tinted
# against a similar background -- that was the readability problem.
COLOR_LIGHT = QColor("#ead9bd")
COLOR_DARK = QColor("#a3814f")
COLOR_PIECE_WHITE = QColor("#fcfbf7")
COLOR_PIECE_BLACK = QColor("#25221e")
COLOR_PIECE_EDGE = QColor("#16130d")
COLOR_BEST = QColor("#3f7fd0")
COLOR_HUMAN = QColor("#d98b34")
COLOR_LAST = QColor("#e0c552")
COLOR_COORD = QColor("#7d8492")
COLOR_LEGAL = QColor("#6fa7db")
COLOR_SELECTED = QColor("#4f8de0")

COLOR_BG = QColor("#1a1c20")
COLOR_PANEL = QColor("#24272d")
COLOR_BAR_WHITE = QColor("#f1ede2")
COLOR_BAR_BLACK = QColor("#24211d")

# The solid (U+265A..265F) glyphs are used as silhouettes for both colours;
# fill and outline carry the colour instead. Outline glyphs render far too thin
# at overlay sizes.
GLYPH = {
    "k": "\u265a",
    "q": "\u265b",
    "r": "\u265c",
    "b": "\u265d",
    "n": "\u265e",
    "p": "\u265f",
}

PIECE_FONT_CANDIDATES = (
    "DejaVu Sans",
    "FreeSerif",
    "Noto Sans Symbols2",
    "Symbola",
    "Segoe UI Symbol",
)

BUDGET_PRESETS = (
    ("Fast", 150, "Lowest delay. Follows blitz and premove bursts closely."),
    ("Balanced", 400, "A responsive default for most games."),
    ("Strong", 900, "Deeper lines, still comfortable in rapid."),
    ("Deep", 1800, "Noticeably deeper; the bar keeps moving for a while."),
    ("Maximum", 3500, "For classical games where depth matters most."),
    (
        "Continuous",
        0,
        "Never stops thinking. Depth keeps climbing until the board moves, "
        "and the search is dropped the instant it does.",
    ),
)

MAIA_RATINGS = tuple(range(1100, 2000, 100))
FEN_PIECES = frozenset("PNBRQKpnbrqk")

PV_LENGTH_CHOICES = ((4, "4 plies"), (6, "6 plies"), (8, "8 plies"),
                     (12, "12 plies"), (0, "Full line"))
FOLLOW_LIVE_CHOICES = (("notify", "Notify me"), ("auto", "Follow automatically"))
EXPLANATION_CHOICES = (("off", "Off"), ("compact", "Compact"),
                       ("detailed", "Detailed"))
EVAL_POV_CHOICES = (
    ("white", "White"), ("black", "Black"), ("side", "Side to move")
)
LINE_EXPANSION_CHOICES = (("selected", "Selected line"), ("all", "All lines"))


def fen_to_grid(fen):
    """Return (64-square a8..h1 grid, side to move)."""
    parts = fen.split()

    if not parts:
        raise ValueError("FEN is empty")

    rows = parts[0].split("/")

    if len(rows) != 8:
        raise ValueError("bad FEN board field")

    grid = []

    for row in rows:
        square_count = 0

        for character in row:
            if character in "12345678":
                empty_count = int(character)
                grid.extend("." * empty_count)
                square_count += empty_count
            elif character in FEN_PIECES:
                grid.append(character)
                square_count += 1
            else:
                raise ValueError("bad FEN piece: " + character)

        if square_count != 8:
            raise ValueError("bad FEN rank: " + row)

    if len(grid) != 64:
        raise ValueError("FEN board does not contain 64 squares")

    side = parts[1] if len(parts) > 1 else "w"

    if side not in {"w", "b"}:
        raise ValueError("side to move must be 'w' or 'b'")

    return grid, side


def grid_to_fen_board(grid):
    """Encode an a8..h1 grid as the first FEN field."""
    if len(grid) != 64:
        raise ValueError("the visible board does not contain 64 squares")

    rows = []

    for start in range(0, 64, 8):
        encoded = []
        empty = 0

        for piece in grid[start : start + 8]:
            if piece == ".":
                empty += 1
                continue

            if piece not in FEN_PIECES:
                raise ValueError(f"the visible board contains an unknown piece: {piece}")

            if empty:
                encoded.append(str(empty))
                empty = 0
            encoded.append(piece)

        if empty:
            encoded.append(str(empty))

        rows.append("".join(encoded))

    return "/".join(rows)


def validate_fen_input(raw_fen):
    """Friendly UI validation; the native host remains authoritative."""
    fields = raw_fen.strip().split()

    if len(fields) != 6:
        raise ValueError("FEN needs all six fields")

    board, side, castling, en_passant, halfmove, fullmove = fields
    grid, parsed_side = fen_to_grid(f"{board} {side}")

    if parsed_side != side:
        raise ValueError("invalid side to move")
    if grid.count("K") != 1 or grid.count("k") != 1:
        raise ValueError("the position needs exactly one king of each colour")

    if castling != "-":
        if any(character not in "KQkq" for character in castling):
            raise ValueError("castling rights may only contain K, Q, k and q")
        if len(set(castling)) != len(castling):
            raise ValueError("castling rights contain a duplicate")
        canonical = "".join(
            character for character in "KQkq" if character in castling
        )
        if castling != canonical:
            raise ValueError("write castling rights in KQkq order")

    if en_passant != "-" and not (
        len(en_passant) == 2
        and en_passant[0] in "abcdefgh"
        and en_passant[1] in "36"
    ):
        raise ValueError("en-passant must be '-' or a square on rank 3 or 6")

    try:
        halfmove_value = int(halfmove)
        fullmove_value = int(fullmove)
    except ValueError as error:
        raise ValueError("move counters must be whole numbers") from error

    if halfmove_value < 0:
        raise ValueError("halfmove clock cannot be negative")
    if fullmove_value < 1:
        raise ValueError("fullmove number must be at least 1")

    return " ".join(fields)


def square_index(name):
    """Convert e4 to an index in an a8..h1 grid."""
    if not name or len(name) < 2:
        return None

    file_index = ord(name[0]) - ord("a")
    rank_index = ord(name[1]) - ord("1")

    if not (0 <= file_index < 8 and 0 <= rank_index < 8):
        return None

    return (7 - rank_index) * 8 + file_index


PIECE_LETTER = {"k": "K", "q": "Q", "r": "R", "b": "B", "n": "N"}


def to_san(grid, move):
    """Render a UCI move as algebraic, using the board it is played from.

    Approximate fallback, used only when san.py is unavailable: no
    disambiguation (Nbd2 comes out as Nd2) and no check or mate suffix, because
    both need legal move generation. Everything else is exact: piece letter,
    captures, en passant, castling, promotion.

    Prefer name_move(), which uses san.py when it is importable.
    """
    if not move or len(move) < 4:
        return move or ""

    origin = square_index(move[0:2])
    target = square_index(move[2:4])

    if origin is None or target is None:
        return move

    piece = grid[origin]

    if piece == ".":
        return move

    letter = piece.upper()
    captured = grid[target] != "."
    origin_file, target_file = origin % 8, target % 8

    if letter == "K" and abs(target_file - origin_file) == 2:
        return "O-O" if target_file > origin_file else "O-O-O"

    if letter == "P":
        # A diagonal pawn move onto an empty square is en passant.
        if origin_file != target_file:
            captured = True

        text = f"{chr(ord('a') + origin_file)}x" if captured else ""
        text += move[2:4]

        if len(move) > 4:
            text += "=" + move[4].upper()

        return text

    return PIECE_LETTER.get(piece.lower(), "") + ("x" if captured else "") + move[2:4]


def format_score(centipawns, mate):
    if mate is not None:
        if mate == 0:
            return "#"

        return f"#{'' if mate > 0 else '-'}{abs(mate)}"

    if centipawns is None:
        return "--"

    return f"{centipawns / 100:+.2f}"


def score_for_pov(centipawns, mate, pov, side_to_move):
    """Convert the host's stable white-POV score for display only."""
    invert = pov == "black" or (pov == "side" and side_to_move == "b")

    if not invert:
        return centipawns, mate

    return (
        -centipawns if centipawns is not None else None,
        -mate if mate is not None else None,
    )


def bound_for_pov(bound, pov, side_to_move):
    bound = str(bound or "exact").lower()
    invert = pov == "black" or (pov == "side" and side_to_move == "b")

    if invert and bound == "lowerbound":
        return "upperbound"
    if invert and bound == "upperbound":
        return "lowerbound"

    return bound


def format_line_score(line, centipawns, mate, pov, side_to_move):
    text = format_score(centipawns, mate)
    bound = bound_for_pov(line.get("bound"), pov, side_to_move)

    if bound == "lowerbound":
        return "≥" + text
    if bound == "upperbound":
        return "≤" + text

    return text


def format_line_status(line, pov, side_to_move):
    bound = bound_for_pov(line.get("bound"), pov, side_to_move)
    parts = []

    if bound == "lowerbound":
        parts.append("Lower bound")
    elif bound == "upperbound":
        parts.append("Upper bound")
    elif line.get("final") is True:
        parts.append("Final")
    elif line.get("final") is False:
        parts.append("Searching")

    try:
        depth = int(line.get("depth") or 0)
    except (TypeError, ValueError):
        depth = 0

    if depth > 0:
        parts.append(f"depth {depth}")

    return " · ".join(parts)


def pv_moves(line):
    """Return a defensive list of UCI tokens from an engine line frame."""
    raw = line.get("pv", "") if isinstance(line, dict) else ""

    if isinstance(raw, str):
        return [token for token in raw.split() if token]
    if isinstance(raw, (list, tuple)):
        return [str(token) for token in raw if token]

    return []


def win_fraction(centipawns, mate):
    """Map a white-POV score onto 0..1 for the bar.

    Raw centipawns make a bad bar: +900 and +2400 both just mean "winning" but
    would look wildly different. This is the standard logistic used by analysis
    boards, so the bar moves where the position actually changes.
    """
    if mate is not None:
        if mate == 0:
            return 1.0

        return 1.0 if mate > 0 else 0.0

    if centipawns is None:
        return 0.5

    clamped = max(-2000, min(2000, centipawns))
    return 0.5 + (2.0 / (1.0 + math.exp(-0.00368208 * clamped)) - 1.0) / 2.0


def setting_bool(value, default):
    if value is None:
        return default

    if isinstance(value, bool):
        return value

    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def resolve_piece_font():
    """Pick a font family that actually has the chess glyphs.

    Falling back silently to a family without them is how you end up with a
    board of empty squares, so check the codepoints rather than trusting the
    family name.
    """
    for family in PIECE_FONT_CANDIDATES:
        probe = QFont(family)
        probe.setPixelSize(48)

        try:
            raw = QRawFont.fromFont(probe)
        except Exception:  # pragma: no cover - defensive
            continue

        if all(raw.supportsCharacter(ord(glyph)) for glyph in GLYPH.values()):
            return family

    return QFont().family()


def name_move(fen, grid, uci):
    """Algebraic name for a UCI move, exact where possible.

    san.py does real move generation, so it gets disambiguation (Nbd2) and
    check/mate suffixes right; it is perft-checked against Kiwipete and friends.
    A stale FEN or a missing module falls through to the approximate grid
    version rather than showing nothing.
    """
    if not uci:
        return ""

    if san_rules is not None and fen:
        try:
            named = san_rules.Board(fen).san(uci)
        except (ValueError, IndexError, KeyError):
            named = uci

        if named != uci:
            return named

    return to_san(grid, uci)


class BoardView(QWidget):
    """The board, drawn in a single paintEvent.

    Holds no Qt child widgets and never touches a stylesheet, which is what
    makes it cheap enough to redraw on every frame of a premove burst.
    """

    moveRequested = pyqtSignal(object)
    interactionHint = pyqtSignal(str)

    def __init__(self, piece_family, parent=None):
        super().__init__(parent)
        self.piece_family = piece_family
        self.grid = ["."] * 64
        self.flip = False
        self.best_move = ""
        self.human_move = ""
        self.last_move = ""
        self.side_to_move = "w"
        self.fen = ""
        self.interactive = False
        self.selected_square = None
        self.focus_square = None
        self.legal_from_selected = []
        self.press_square = None
        self.dragging = False
        self.drag_position = QPointF()
        self._path_cache = {}
        self.setMinimumSize(200, 200)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Analysis chess board")

    def set_position(self, grid, side_to_move, flip, fen=""):
        changed = fen and fen != self.fen
        self.grid = list(grid)
        self.side_to_move = side_to_move
        self.flip = flip
        self.fen = fen or self.fen

        if changed:
            self.clear_selection()

    def set_moves(self, best_move, human_move, last_move):
        self.best_move = best_move or ""
        self.human_move = human_move or ""
        self.last_move = last_move or ""

    def set_interactive(self, enabled):
        enabled = bool(enabled and san_rules is not None and self.fen)

        if self.interactive == enabled:
            return

        self.interactive = enabled
        self.clear_selection()
        if not enabled:
            self.focus_square = None
        self.setCursor(
            Qt.CursorShape.OpenHandCursor
            if enabled
            else Qt.CursorShape.ArrowCursor
        )
        self.setAccessibleDescription(
            "Move pieces with drag and drop, or select an origin and target."
            if enabled
            else "Live position. Choose Explore to move pieces."
        )
        self.update()

    def clear_selection(self):
        self.selected_square = None
        self.legal_from_selected = []
        self.press_square = None
        self.dragging = False
        self.update()

    def legal_moves(self):
        if not self.interactive or san_rules is None or not self.fen:
            return []

        try:
            return san_rules.Board(self.fen).legal_uci_moves()
        except (ValueError, AttributeError):
            return []

    def select_square(self, index):
        legal = self.legal_moves()
        origin = san_rules.square_name(index) if san_rules is not None else ""
        selected = [move for move in legal if move.startswith(origin)]

        if not selected:
            self.clear_selection()
            return False

        self.selected_square = index
        self.focus_square = index
        self.legal_from_selected = selected
        targets = sorted({move[2:4] for move in selected})
        piece = self.grid[index]
        piece_name = {
            "p": "pawn", "n": "knight", "b": "bishop", "r": "rook",
            "q": "queen", "k": "king",
        }.get(piece.lower(), "piece")
        hint = f"{piece_name.title()} {origin} selected"

        if targets:
            hint += "; legal targets " + ", ".join(targets)

        self.setAccessibleDescription(hint)
        self.interactionHint.emit(hint)
        self.update()
        return True

    # -- geometry ---------------------------------------------------------

    def gutter(self):
        """Width of the coordinate strip on the left and bottom edges.

        Coordinates used to be painted inside the edge squares, where the back
        rank sits on top of them. A gutter costs a few pixels and is legible.
        """
        return max(9.0, min(self.width(), self.height()) * 0.042)

    def board_rect(self):
        gutter = self.gutter()
        side = min(self.width() - gutter, self.height() - gutter)
        side -= side % 8  # integral squares avoid seams between fills

        if side < 8:
            return QRectF(0.0, 0.0, 0.0, 0.0)

        return QRectF(
            gutter + (self.width() - gutter - side) / 2.0,
            (self.height() - gutter - side) / 2.0,
            float(side),
            float(side),
        )

    def square_rect(self, view_index, board):
        size = board.width() / 8.0
        return QRectF(
            board.left() + (view_index % 8) * size,
            board.top() + (view_index // 8) * size,
            size,
            size,
        )

    def square_center(self, board_index, board):
        view_index = 63 - board_index if self.flip else board_index
        return self.square_rect(view_index, board).center()

    def index_at(self, position):
        board = self.board_rect()

        if not board.contains(position) or board.width() <= 0:
            return None

        size = board.width() / 8.0
        column = int((position.x() - board.left()) / size)
        row = int((position.y() - board.top()) / size)

        if not (0 <= row < 8 and 0 <= column < 8):
            return None

        view_index = row * 8 + column
        return 63 - view_index if self.flip else view_index

    def request_target(self, target):
        if self.selected_square is None or san_rules is None:
            return False

        origin_name = san_rules.square_name(self.selected_square)
        target_name = san_rules.square_name(target)
        choices = [
            move for move in self.legal_from_selected
            if move.startswith(origin_name + target_name)
        ]

        if not choices:
            self.interactionHint.emit("Illegal move")
            return False

        self.moveRequested.emit(choices)
        self.clear_selection()
        return True

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.interactive:
            super().mousePressEvent(event)
            return

        self.setFocus(Qt.FocusReason.MouseFocusReason)
        index = self.index_at(event.position())

        if index is None:
            self.clear_selection()
            event.accept()
            return

        self.press_square = index
        self.drag_position = event.position()
        self.dragging = False

        if self.selected_square is not None and index != self.selected_square:
            if self.request_target(index):
                event.accept()
                return

        self.select_square(index)
        event.accept()

    def mouseMoveEvent(self, event):
        if (
            not self.interactive
            or self.press_square is None
            or self.selected_square != self.press_square
            or not (event.buttons() & Qt.MouseButton.LeftButton)
        ):
            super().mouseMoveEvent(event)
            return

        self.drag_position = event.position()

        if not self.dragging:
            origin = self.square_center(self.press_square, self.board_rect())
            if (
                abs(origin.x() - self.drag_position.x())
                + abs(origin.y() - self.drag_position.y())
                >= QApplication.startDragDistance()
            ):
                self.dragging = True
                self.setCursor(Qt.CursorShape.ClosedHandCursor)

        if self.dragging:
            self.update()

        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or not self.interactive:
            super().mouseReleaseEvent(event)
            return

        if self.dragging:
            target = self.index_at(event.position())

            if target is None or not self.request_target(target):
                # Retain the origin selection after an unsuccessful drag.
                if self.press_square is not None:
                    self.select_square(self.press_square)

        self.press_square = None
        self.dragging = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.update()
        event.accept()

    def keyPressEvent(self, event):
        if not self.interactive:
            super().keyPressEvent(event)
            return

        key = event.key()

        if key == Qt.Key.Key_Escape:
            self.clear_selection()
            event.accept()
            return

        if self.focus_square is None:
            owned = [
                index for index, piece in enumerate(self.grid)
                if piece != "." and (piece.isupper() == (self.side_to_move == "w"))
            ]
            self.focus_square = owned[0] if owned else 0

        view = 63 - self.focus_square if self.flip else self.focus_square
        row, column = divmod(view, 8)
        delta = {
            Qt.Key.Key_Left: (0, -1),
            Qt.Key.Key_Right: (0, 1),
            Qt.Key.Key_Up: (-1, 0),
            Qt.Key.Key_Down: (1, 0),
        }.get(key)

        if delta is not None:
            row = max(0, min(7, row + delta[0]))
            column = max(0, min(7, column + delta[1]))
            view = row * 8 + column
            self.focus_square = 63 - view if self.flip else view
            self.update()
            event.accept()
            return

        if key in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            if (
                self.selected_square is not None
                and self.focus_square != self.selected_square
                and self.request_target(self.focus_square)
            ):
                event.accept()
                return

            self.select_square(self.focus_square)
            event.accept()
            return

        super().keyPressEvent(event)

    # -- pieces -----------------------------------------------------------

    def piece_path(self, piece, size):
        """Outline of one piece, normalised into a size x size box.

        Cached per (piece, rounded size): building a QPainterPath from a glyph
        is far too slow to redo for 32 pieces every frame.
        """
        key = (piece, round(size))
        cached = self._path_cache.get(key, "miss")

        if cached != "miss":
            return cached

        font = QFont(self.piece_family)
        font.setPixelSize(max(8, int(size)))

        path = QPainterPath()
        path.addText(0.0, 0.0, font, GLYPH[piece])
        bounds = path.boundingRect()

        if bounds.isEmpty():
            self._path_cache[key] = None
            return None

        scale = min(size / bounds.width(), size / bounds.height())
        transform = QTransform()
        transform.translate(size / 2.0, size / 2.0)
        transform.scale(scale, scale)
        transform.translate(-bounds.center().x(), -bounds.center().y())

        shaped = transform.map(path)

        if len(self._path_cache) > 64:
            self._path_cache.clear()

        self._path_cache[key] = shaped
        return shaped

    def draw_arrow(self, painter, move, colour, board, width_factor):
        if len(move) < 4:
            return

        origin = square_index(move[0:2])
        target = square_index(move[2:4])

        if origin is None or target is None or origin == target:
            return

        start = self.square_center(origin, board)
        end = self.square_center(target, board)

        size = board.width() / 8.0
        shaft = size * width_factor
        head = size * 0.34

        dx = end.x() - start.x()
        dy = end.y() - start.y()
        length = math.hypot(dx, dy)

        if length < 1.0:
            return

        ux, uy = dx / length, dy / length

        # Start outside the origin square's centre and stop short of the target
        # centre, so the head sits on the square rather than burying the piece.
        start = QPointF(
            start.x() + ux * size * 0.22, start.y() + uy * size * 0.22
        )
        tip = QPointF(end.x() - ux * size * 0.10, end.y() - uy * size * 0.10)
        base = QPointF(tip.x() - ux * head, tip.y() - uy * head)

        painter.setPen(
            QPen(colour, shaft, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        )
        painter.drawLine(start, base)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(colour))
        painter.drawPolygon(
            QPolygonF(
                [
                    tip,
                    QPointF(
                        base.x() - uy * head * 0.62,
                        base.y() + ux * head * 0.62,
                    ),
                    QPointF(
                        base.x() + uy * head * 0.62,
                        base.y() - ux * head * 0.62,
                    ),
                ]
            )
        )

    # -- painting ---------------------------------------------------------

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        board = self.board_rect()

        if board.width() < 8.0:
            painter.end()
            return

        size = board.width() / 8.0
        highlights = {}

        for move, colour in (
            (self.human_move, COLOR_HUMAN),
            (self.best_move, COLOR_BEST),
        ):
            if len(move) >= 4:
                tint = QColor(colour)
                tint.setAlpha(105)

                for name in (move[0:2], move[2:4]):
                    index = square_index(name)

                    if index is not None:
                        highlights[index] = tint

        painter.setPen(Qt.PenStyle.NoPen)

        for view_index in range(64):
            board_index = 63 - view_index if self.flip else view_index
            rect = self.square_rect(view_index, board)
            light = (board_index // 8 + board_index % 8) % 2 == 0

            painter.setBrush(COLOR_LIGHT if light else COLOR_DARK)
            painter.drawRect(rect)

            tint = highlights.get(board_index)

            if tint is not None:
                painter.setBrush(tint)
                painter.drawRect(rect)

        if self.selected_square is not None:
            selected_view = (
                63 - self.selected_square if self.flip else self.selected_square
            )
            selected = QColor(COLOR_SELECTED)
            selected.setAlpha(115)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(selected)
            painter.drawRect(self.square_rect(selected_view, board))

            target_indices = {
                square_index(move[2:4]) for move in self.legal_from_selected
            }

            for target in target_indices:
                if target is None:
                    continue

                target_view = 63 - target if self.flip else target
                rect = self.square_rect(target_view, board)
                centre = rect.center()
                colour = QColor(COLOR_LEGAL)
                colour.setAlpha(205)
                painter.setBrush(colour)

                if self.grid[target] == ".":
                    painter.setPen(Qt.PenStyle.NoPen)
                    radius = size * 0.105
                    painter.drawEllipse(centre, radius, radius)
                else:
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.setPen(QPen(colour, max(2.0, size * 0.075)))
                    painter.drawEllipse(
                        rect.adjusted(size * 0.10, size * 0.10,
                                      -size * 0.10, -size * 0.10)
                    )

        # The move that was just played gets an outline rather than another
        # fill: three overlapping tints on the same square turn to mud.
        if len(self.last_move) >= 4:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(
                QPen(
                    COLOR_LAST,
                    max(1.5, size * 0.055),
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.SquareCap,
                    Qt.PenJoinStyle.MiterJoin,
                )
            )
            edge = max(1.0, size * 0.033)

            for name in (self.last_move[0:2], self.last_move[2:4]):
                index = square_index(name)

                if index is None:
                    continue

                view = 63 - index if self.flip else index
                painter.drawRect(
                    self.square_rect(view, board).adjusted(
                        edge, edge, -edge, -edge
                    )
                )

            painter.setPen(Qt.PenStyle.NoPen)

        # Coordinates in the gutter, outside the playing area.
        coord_font = QFont(QApplication.font())
        coord_font.setPixelSize(max(7, int(self.gutter() * 0.74)))
        coord_font.setBold(True)
        painter.setFont(coord_font)
        painter.setPen(COLOR_COORD)

        gutter = self.gutter()

        for offset in range(8):
            column = self.square_rect(56 + offset, board)
            painter.drawText(
                QRectF(column.left(), board.bottom(), size, gutter),
                Qt.AlignmentFlag.AlignCenter,
                chr(ord("h") - offset) if self.flip else chr(ord("a") + offset),
            )

            row = self.square_rect(offset * 8, board)
            painter.drawText(
                QRectF(board.left() - gutter, row.top(), gutter, size),
                Qt.AlignmentFlag.AlignCenter,
                str(offset + 1) if self.flip else str(8 - offset),
            )

        inset = size * 0.09
        outline = QPen(
            COLOR_PIECE_EDGE,
            max(1.0, size * 0.045),
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )

        for view_index in range(64):
            board_index = 63 - view_index if self.flip else view_index
            piece = self.grid[board_index]

            if piece == ".":
                continue

            if self.dragging and board_index == self.selected_square:
                continue

            path = self.piece_path(piece.lower(), size - inset * 2.0)

            if path is None:
                continue

            rect = self.square_rect(view_index, board)
            painter.save()
            painter.translate(rect.left() + inset, rect.top() + inset)
            painter.setPen(outline)
            painter.setBrush(
                QBrush(
                    COLOR_PIECE_WHITE if piece.isupper() else COLOR_PIECE_BLACK
                )
            )
            painter.drawPath(path)
            painter.restore()

        if self.dragging and self.selected_square is not None:
            piece = self.grid[self.selected_square]

            if piece != ".":
                path = self.piece_path(piece.lower(), size - inset * 2.0)

                if path is not None:
                    painter.save()
                    painter.translate(
                        self.drag_position.x() - size / 2.0 + inset,
                        self.drag_position.y() - size / 2.0 + inset,
                    )
                    painter.setPen(outline)
                    painter.setBrush(
                        QBrush(
                            COLOR_PIECE_WHITE
                            if piece.isupper()
                            else COLOR_PIECE_BLACK
                        )
                    )
                    painter.drawPath(path)
                    painter.restore()

        # Arrows last, so they read on top of the pieces.
        human = QColor(COLOR_HUMAN)
        human.setAlpha(205)
        best = QColor(COLOR_BEST)
        best.setAlpha(225)

        if self.human_move and self.human_move != self.best_move:
            self.draw_arrow(painter, self.human_move, human, board, 0.115)

        self.draw_arrow(painter, self.best_move, best, board, 0.145)

        if self.hasFocus() and self.focus_square is not None:
            focus_view = 63 - self.focus_square if self.flip else self.focus_square
            focus_rect = self.square_rect(focus_view, board)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(
                QPen(QColor("#f4f7fb"), max(1.5, size * 0.04),
                     Qt.PenStyle.DashLine)
            )
            painter.drawRect(focus_rect.adjusted(2.0, 2.0, -2.0, -2.0))

        painter.setPen(QPen(QColor(0, 0, 0, 90), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(board.adjusted(0.5, 0.5, -0.5, -0.5))
        painter.end()


class CandidateRow(QFrame):
    """One keyboard- and mouse-selectable MultiPV result."""

    clicked = pyqtSignal(int)
    activated = pyqtSignal(int)

    def __init__(self, rank, parent=None):
        super().__init__(parent)
        self.rank = rank
        self._suppress_release = False
        self.setObjectName("candidateRow")
        self.setProperty("selected", False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 4, 7, 4)
        layout.setSpacing(2)

        summary = QHBoxLayout()
        summary.setContentsMargins(0, 0, 0, 0)
        summary.setSpacing(7)

        self.move_label = QLabel("")
        self.move_label.setObjectName("candidateMove")
        summary.addWidget(self.move_label, 1)

        self.score_label = QLabel("")
        self.score_label.setObjectName("candidateScore")
        summary.addWidget(self.score_label)

        self.depth_label = QLabel("")
        self.depth_label.setObjectName("candidateDepth")
        summary.addWidget(self.depth_label)
        layout.addLayout(summary)

        self.pv_label = QLabel("")
        self.pv_label.setObjectName("candidatePv")
        self.pv_label.setWordWrap(True)
        self.pv_label.hide()
        layout.addWidget(self.pv_label)

    def set_selected(self, selected):
        selected = bool(selected)

        if self.property("selected") == selected:
            return

        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def set_line(self, move, score, depth, pv, expanded):
        self.move_label.setText(f"{self.rank}.  {move or '?'}")
        self.score_label.setText(score)
        self.depth_label.setText(f"d{depth}" if depth else "")
        self.pv_label.setText(pv)
        self.pv_label.setVisible(bool(expanded and pv))
        accessible = f"Candidate {self.rank}, {move or 'unknown'}, {score}"

        if pv:
            accessible += f". Principal variation {pv}"

        self.setAccessibleName(accessible)
        self.setToolTip(accessible)

    def mouseReleaseEvent(self, event):
        if self._suppress_release and event.button() == Qt.MouseButton.LeftButton:
            self._suppress_release = False
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit(self.rank - 1)
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._suppress_release = True
            self.activated.emit(self.rank - 1)
            event.accept()
            return

        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        window = self.window()
        if event.key() in {Qt.Key.Key_Up, Qt.Key.Key_Down} and hasattr(
            window, "candidate_rows"
        ):
            if not window.lines:
                event.accept()
                return
            offset = -1 if event.key() == Qt.Key.Key_Up else 1
            target = max(0, min(len(window.lines) - 1, self.rank - 1 + offset))
            window.select_candidate(target)
            window.candidate_rows[target].setFocus(Qt.FocusReason.TabFocusReason)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Left and hasattr(window, "preview_back"):
            window.preview_back()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Right and hasattr(window, "preview_forward"):
            window.preview_forward()
            event.accept()
            return
        if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
            self.clicked.emit(self.rank - 1)
            event.accept()
            return

        super().keyPressEvent(event)


class EvalBar(QWidget):
    """Horizontal white/black advantage bar, white on the left.

    Replaces the old "-2.98 (b to move)" text line. Scores arrive already
    converted to white's point of view by the host, so the bar direction is
    stable regardless of whose turn it is.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.centipawns = None
        self.mate = None
        self.depth = 0
        self.has_eval = False
        self.bound = "exact"
        self.pov = "white"
        self.side_to_move = "w"
        # The bar slides to a new evaluation instead of snapping. A deepening
        # search revises its score several times a second, and a bar that jumps
        # on every revision is genuinely hard to read.
        self.target_fraction = 0.5
        self.display_fraction = 0.5
        self.setFixedHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_eval(self, centipawns, mate, depth, has_eval, bound="exact"):
        self.centipawns = centipawns
        self.mate = mate
        self.depth = depth
        self.has_eval = has_eval
        self.bound = str(bound or "exact")

        if has_eval:
            self.target_fraction = win_fraction(centipawns, mate)

    def set_pov(self, pov, side_to_move):
        self.pov = pov if pov in {"white", "black", "side"} else "white"
        self.side_to_move = side_to_move if side_to_move in {"w", "b"} else "w"
        if self.pov == "side":
            description = (
                "Score shown from the side-to-move point of view; bar remains "
                "White versus Black."
            )
        elif self.pov == "black":
            description = (
                "Score shown from Black's point of view; bar remains White "
                "versus Black."
            )
        else:
            description = "Score shown from White's point of view."
        self.setToolTip(description)
        self.setAccessibleDescription(description)

    def advance(self):
        """Step the animation. True when the bar needs repainting."""
        delta = self.target_fraction - self.display_fraction

        if abs(delta) < 0.0015:
            if self.display_fraction != self.target_fraction:
                self.display_fraction = self.target_fraction
                return True

            return False

        self.display_fraction += delta * 0.28
        return True

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(0.0, 0.0, float(self.width()), float(self.height()))
        radius = rect.height() / 2.0

        clip = QPainterPath()
        clip.addRoundedRect(rect, radius, radius)
        painter.setClipPath(clip)

        base_font = QFont(QApplication.font())

        fraction = self.display_fraction
        split = rect.width() * fraction

        if not self.has_eval:
            # Hold the previous split, muted, rather than blanking the bar.
            # "Roughly here, recalculating" is more useful than a grey slab,
            # and the missing number makes clear it is not a live reading.
            painter.fillRect(rect, QColor("#2f2c28"))
            painter.fillRect(QRectF(0.0, 0.0, split, rect.height()),
                             QColor("#8f8b81"))
            painter.setPen(QColor(255, 255, 255, 120))
            base_font.setPixelSize(max(9, int(rect.height() * 0.60)))
            painter.setFont(base_font)
            painter.drawText(
                rect, Qt.AlignmentFlag.AlignCenter, "\u2026"
            )
            painter.end()
            return

        painter.fillRect(rect, COLOR_BAR_BLACK)
        painter.fillRect(QRectF(0.0, 0.0, split, rect.height()), COLOR_BAR_WHITE)

        # Centre reference, so a small edge reads as a small edge.
        painter.setPen(QPen(QColor(128, 128, 128, 110), 1.0))
        painter.drawLine(
            QPointF(rect.width() / 2.0, 0.0),
            QPointF(rect.width() / 2.0, rect.height()),
        )

        shown_cp, shown_mate = score_for_pov(
            self.centipawns, self.mate, self.pov, self.side_to_move
        )
        text = format_line_score(
            {"bound": self.bound}, shown_cp, shown_mate,
            self.pov, self.side_to_move,
        )
        score_font = QFont(base_font)
        score_font.setPixelSize(max(10, int(rect.height() * 0.56)))
        score_font.setBold(True)
        painter.setFont(score_font)

        pad = 7.0
        needed = QFontMetricsF(score_font).horizontalAdvance(text) + pad * 2.0
        white_leads = fraction >= 0.5

        # The number rides with the leader, and only crosses over when there is
        # no room left on that side.
        if white_leads:
            on_white = split >= needed
        else:
            on_white = (rect.width() - split) < needed

        if on_white:
            painter.setPen(QColor("#221f1b"))
            box = QRectF(pad, 0.0, max(0.0, split - pad), rect.height())
            align = Qt.AlignmentFlag.AlignLeft
        else:
            painter.setPen(QColor("#f0ece1"))
            box = QRectF(
                split, 0.0, max(0.0, rect.width() - split - pad), rect.height()
            )
            align = Qt.AlignmentFlag.AlignRight

        painter.drawText(box, align | Qt.AlignmentFlag.AlignVCenter, text)

        if self.depth > 0:
            depth_font = QFont(base_font)
            depth_font.setPixelSize(max(8, int(rect.height() * 0.42)))
            painter.setFont(depth_font)

            # Put the depth readout on the opposite end from the score.
            if on_white:
                painter.setPen(QColor(232, 227, 217, 175))
                depth_align = Qt.AlignmentFlag.AlignRight
            else:
                painter.setPen(QColor(70, 63, 52, 210))
                depth_align = Qt.AlignmentFlag.AlignLeft

            painter.drawText(
                rect.adjusted(pad, 0.0, -pad, 0.0),
                depth_align | Qt.AlignmentFlag.AlignVCenter,
                f"d{self.depth}",
            )

        painter.end()


class ReviewGraph(QWidget):
    """Compact, clickable White-POV evaluation graph."""

    selected = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.values = []
        self.current = -1
        self.setMinimumHeight(74)
        self.setMaximumHeight(110)
        self.setAccessibleName("Game evaluation graph")

    def set_values(self, values, current=-1):
        self.values = list(values)
        self.current = current
        self.update()

    def set_current(self, current):
        self.current = current
        self.update()

    @staticmethod
    def normalise(value):
        return math.tanh(max(-2000, min(2000, value)) / 400.0)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bounds = self.rect().adjusted(4, 4, -4, -4)
        painter.fillRect(bounds, QColor("#202329"))
        middle = bounds.center().y()
        painter.setPen(QPen(QColor("#59606c"), 1))
        painter.drawLine(bounds.left(), middle, bounds.right(), middle)
        if not self.values:
            painter.end()
            return
        path = QPainterPath()
        denominator = max(1, len(self.values) - 1)
        for index, value in enumerate(self.values):
            x = bounds.left() + bounds.width() * index / denominator
            y = middle - self.normalise(value) * bounds.height() * 0.43
            if index == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        painter.setPen(QPen(COLOR_BEST, 2))
        painter.drawPath(path)
        if 0 <= self.current < len(self.values):
            x = bounds.left() + bounds.width() * self.current / denominator
            painter.setPen(QPen(COLOR_LAST, 2))
            painter.drawLine(int(x), bounds.top(), int(x), bounds.bottom())
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.values:
            bounds = self.rect().adjusted(4, 4, -4, -4)
            fraction = (event.position().x() - bounds.left()) / max(1, bounds.width())
            index = round(max(0.0, min(1.0, fraction)) * (len(self.values) - 1))
            self.selected.emit(index)
            event.accept()
            return
        super().mousePressEvent(event)


class TurnDot(QWidget):
    """Whose move it is, without spending a line of text on it."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.side_to_move = "w"
        self.setFixedSize(14, 14)

    def set_side(self, side):
        self.side_to_move = side

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor("#6b7280"), 1.2))
        painter.setBrush(
            QBrush(
                COLOR_BAR_WHITE if self.side_to_move == "w" else COLOR_BAR_BLACK
            )
        )
        painter.drawEllipse(
            QRectF(1.5, 1.5, self.width() - 3.0, self.height() - 3.0)
        )
        painter.end()


class PageStack(QStackedWidget):
    """Reports the current page's size, not the largest page's.

    Stock QStackedWidget takes the maximum hint over every page it holds, which
    pinned this window's height to the tallest one -- so hiding the board for
    compact mode collapsed the page but not the window.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.currentChanged.connect(lambda _index: self.updateGeometry())

    def sizeHint(self):
        page = self.currentWidget()
        return page.sizeHint() if page is not None else super().sizeHint()

    def minimumSizeHint(self):
        page = self.currentWidget()

        if page is None:
            return super().minimumSizeHint()

        return page.minimumSizeHint()


class TitleBar(QWidget):
    """Slim header that also drags the window when it has no decorations."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(26)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton or DECORATED:
            super().mousePressEvent(event)
            return

        handle = self.window().windowHandle()

        # Hand the drag to the compositor rather than tracking the cursor
        # ourselves: manual move loops are the thing that breaks on Wayland.
        if handle is not None:
            handle.startSystemMove()
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        window = self.window()

        if hasattr(window, "toggle_compact"):
            window.toggle_compact()
            event.accept()
            return

        super().mouseDoubleClickEvent(event)


class Overlay(QWidget):
    def __init__(self):
        super().__init__()

        self.settings = QSettings(ORGANIZATION, APPLICATION)
        self.start_command_sent = False
        self.applying_settings = False
        self.local_mode = False

        self.budget_ms = 400
        self.maia_rating = 1900
        self.threads = min(4, max(1, (os.cpu_count() or 2) // 2))
        self.multipv = 3
        self.explore_budget = -1
        self.pv_display_length = 6
        self.follow_live = "notify"
        self.explanation_level = "compact"
        self.eval_pov = "white"
        self.line_expansion = "selected"
        self.show_best_arrow = True
        self.show_human_arrow = True
        self.show_played_highlight = True
        self.review_time_ms = 350
        self.review_lines = 2
        self.review_auto = False
        self.review_sensitivity = "standard"
        self.review_record = None
        self.review_results = []
        self.review_positions = []
        self.review_position_analyses = []
        self.review_job = None
        self.review_queue = None
        self.review_settings_used = None
        self.review_game_id = None
        self.review_visible_rows = []
        self.review_selected_ply = -1
        self.review_mode = "game"
        self.review_branch = []
        self.review_branch_root = ""
        self.review_position_job = None
        self.review_position_queue = None
        self.review_position_generation = 0
        self.review_position_lines = []
        self.review_store = study_store.ReviewStore() if study_store is not None else None
        self.study_auto_analyse = True
        self.study_save_evals = True
        self.current_study = None
        self.current_study_id = None
        self.study_node_id = None
        self.study_tree_refreshing = False
        self.study_annotation_loading = False
        self.study_position_job = None
        self.study_position_queue = None
        self.study_position_generation = 0
        self.study_position_lines = []
        self.study_analysis_node_id = None
        self.study_return_page = None
        self.settings_return_page = None

        # Latest known state. Frames are folded into these fields and painted
        # on a timer, so the host can publish faster than the screen refreshes
        # without ever backing up the event loop.
        self.position_seq = 0
        self.grid = ["."] * 64
        self.side_to_move = "w"
        self.flip = False
        self.best_move = ""
        self.human_move = ""
        self.last_move = ""
        self.last_san = ""
        self.fen = ""
        self.best_cp = None
        self.best_mate = None
        self.best_bound = "exact"
        self.depth = 0
        self.has_eval = False
        self.analysis_final = False
        self.lines = []
        self.analysis_fen = ""
        self.selected_line = 0
        self.status_text = ""
        self.dirty = False
        self.session_id = ""
        self.session_label = ""
        self.session_active = False
        self.recovery_action = ""
        self.state_source = ""
        self.synchronising = False

        # Analysis Lab is explicitly separate from the authoritative game.
        # ``live_snapshot`` keeps receiving the real board while ``mode`` is
        # explore; no explorer move is ever sent through the recovery FEN path.
        self.mode = "live"
        self.target_revision = 0
        self.live_revision = 0
        self.live_snapshot = None
        self.live_update_count = 0
        self.explore_live_base_revision = 0
        self.preview_moves = []
        self.preview_step = 0
        self.preview_root_fen = ""
        self.preview_root_grid = None
        self.preview_root_side = "w"
        self.preview_root_last = ""
        self.preview_root_last_san = ""
        self.explore_branch_id = None
        self.explore_node_id = None
        self.explore_root_node_id = None
        self.explore_nodes = {}
        self.explore_pending = ""
        self.explore_pending_parent = None
        self.pending_start_base = ""
        self.pending_start_path = []
        self.resume_branch_id = None
        self.resume_node_id = None

        self.piece_family = resolve_piece_font()

        self.compact = False
        self.expanded_geometry = None
        self.opacity_percent = 100

        self.setWindowTitle(f"ChessListener {APP_VERSION}")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        if not DECORATED:
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

        self.setMinimumWidth(300)

        self.frame_timer = QTimer(self)
        self.frame_timer.setInterval(FRAME_INTERVAL_MS)
        self.frame_timer.timeout.connect(self.flush_frame)

        self.status_timer = QTimer(self)
        self.status_timer.setSingleShot(True)
        self.status_timer.timeout.connect(self.clear_status)

        self.settings_timer = QTimer(self)
        self.settings_timer.setSingleShot(True)
        self.settings_timer.setInterval(SETTINGS_DEBOUNCE_MS)
        self.settings_timer.timeout.connect(self.send_settings)

        self.geometry_timer = QTimer(self)
        self.geometry_timer.setSingleShot(True)
        self.geometry_timer.setInterval(GEOMETRY_SAVE_MS)
        self.geometry_timer.timeout.connect(self.save_geometry)

        self.review_timer = QTimer(self)
        self.review_timer.setInterval(50)
        self.review_timer.timeout.connect(self.poll_review)

        self.review_position_timer = QTimer(self)
        self.review_position_timer.setInterval(50)
        self.review_position_timer.timeout.connect(self.poll_review_position)

        self.study_position_timer = QTimer(self)
        self.study_position_timer.setInterval(50)
        self.study_position_timer.timeout.connect(self.poll_study_position)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.title_bar = self.build_title_bar()
        root.addWidget(self.title_bar)

        self.stack = PageStack()
        root.addWidget(self.stack, 1)

        if not DECORATED:
            grip_row = QHBoxLayout()
            grip_row.setContentsMargins(0, 0, 2, 2)
            grip_row.addStretch(1)
            grip_row.addWidget(QSizeGrip(self))
            root.addLayout(grip_row)

        self.startup_page = self.build_startup_page()
        self.analysis_page = self.build_analysis_page()
        self.settings_page = self.build_settings_page()
        self.recovery_page = self.build_recovery_page()
        self.review_page = self.build_review_page()
        self.study_page = self.build_study_page()

        for page in (
            self.startup_page,
            self.analysis_page,
            self.settings_page,
            self.recovery_page,
            self.review_page,
            self.study_page,
        ):
            self.stack.addWidget(page)

        self.stack.setCurrentWidget(self.startup_page)

        self.apply_style()
        self.restore_saved_settings()
        self.restore_review_archive()
        self.resize(360, 410)

    # -- styling ----------------------------------------------------------

    def apply_style(self):
        # One stylesheet, set once, on the top-level widget. Nothing in the hot
        # path ever calls setStyleSheet again.
        self.setStyleSheet(
            f"""
            QWidget {{
                background: {COLOR_BG.name()};
                color: #e8ebf0;
                font-size: 13px;
            }}
            QLabel, QCheckBox {{ background: transparent; }}
            QLabel#title {{ font-size: 23px; font-weight: 700; }}
            QLabel#heading {{ font-weight: 700; }}
            QLabel#subtitle, QLabel#helper {{ color: #a4abb8; }}
            QLabel#statusInfo {{ color: #8f97a5; font-size: 12px; }}
            QLabel#statusWarn {{ color: #e0a95c; font-size: 12px; }}
            QLabel#sourceExact {{
                background: #234535; color: #9fe0b5; border-radius: 6px;
                font-size: 10px; font-weight: 700; padding: 1px 5px;
            }}
            QLabel#sourceInferred {{
                background: #4a3921; color: #e6c184; border-radius: 6px;
                font-size: 10px; font-weight: 700; padding: 1px 5px;
            }}
            QLabel#sourceManual {{
                background: #293e55; color: #9bc5ef; border-radius: 6px;
                font-size: 10px; font-weight: 700; padding: 1px 5px;
            }}
            QLabel#sourceSyncing {{
                background: #4a3024; color: #efb18d; border-radius: 6px;
                font-size: 10px; font-weight: 700; padding: 1px 5px;
            }}
            QLabel#sourceExplore {{
                background: #293e55; color: #9bc5ef; border-radius: 6px;
                font-size: 10px; font-weight: 700; padding: 1px 5px;
            }}
            QLabel#engineLine {{ color: #cfd5e0; }}
            QLabel#pvLine {{ color: #929aa8; }}
            QLabel#breadcrumb {{ color: #aeb6c4; font-size: 12px; }}
            QLabel#explanation {{
                color: #b9c1cd; background: #202329; border-radius: 7px;
                padding: 6px;
            }}
            QFrame#candidateRow {{
                background: #202329;
                border: 1px solid transparent;
                border-radius: 7px;
            }}
            QFrame#candidateRow:hover {{ background: #292d34; }}
            QFrame#candidateRow[selected="true"] {{
                background: #26384d;
                border-color: #4f8de0;
            }}
            QFrame#candidateRow:focus {{ border-color: #8bb8e8; }}
            QLabel#candidateMove {{ color: #e0e5ed; font-weight: 700; }}
            QLabel#candidateScore {{ color: #dce2eb; font-weight: 700; }}
            QLabel#candidateDepth, QLabel#candidatePv {{ color: #929aa8; }}
            QFrame#panel {{
                background: {COLOR_PANEL.name()};
                border: 1px solid #343841;
                border-radius: 10px;
            }}
            QComboBox, QSpinBox, QLineEdit, QTextEdit {{
                background: #171a1e;
                border: 1px solid #414751;
                border-radius: 7px;
                padding: 6px 4px 6px 9px;
                min-height: 20px;
            }}
            QComboBox:focus, QSpinBox:focus, QLineEdit:focus, QTextEdit:focus {{
                border-color: #6d98c4;
            }}
            QComboBox QAbstractItemView {{
                background: #171a1e;
                selection-background-color: #3f7fd0;
            }}
            QPushButton {{
                background: #3f7fd0;
                border: 0;
                border-radius: 8px;
                color: white;
                font-weight: 700;
                padding: 9px 16px;
            }}
            QPushButton:hover {{ background: #4f8de0; }}
            QPushButton:pressed {{ background: #3569ad; }}
            QPushButton#ghost {{
                background: transparent;
                border: 1px solid #414751;
                color: #cfd5e0;
                font-weight: 500;
                padding: 4px 10px;
            }}
            QPushButton#ghost:hover {{ background: #2b2f36; }}
            QPushButton#tinyGhost {{
                background: transparent;
                border: 1px solid #414751;
                border-radius: 6px;
                color: #cfd5e0;
                font-weight: 600;
                padding: 3px 7px;
            }}
            QPushButton#tinyGhost:hover {{ background: #2b2f36; }}
            QPushButton#liveBadge {{
                background: #4a3921; color: #e6c184; border: 0;
                border-radius: 6px; padding: 3px 7px; font-weight: 700;
            }}
            QPushButton#titleButton {{
                background: transparent;
                border: 0;
                border-radius: 4px;
                color: #98a0ae;
                font-size: 14px;
                font-weight: 700;
                padding: 0;
            }}
            QPushButton#titleButton:hover {{
                background: #32363e;
                color: #e8ebf0;
            }}
            QLabel#lastLine {{ color: #d8c464; }}
            QCheckBox {{ spacing: 8px; }}
            QScrollArea {{ background: transparent; border: 0; }}
            QTreeWidget {{
                background: #171a1e;
                border: 1px solid #343841;
                border-radius: 7px;
                alternate-background-color: #1d2025;
                selection-background-color: #26384d;
                padding: 3px;
            }}
            QTreeWidget::item {{ padding: 3px; }}
            """
        )

    # -- pages ------------------------------------------------------------

    def build_title_bar(self):
        bar = TitleBar()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(9, 3, 4, 3)
        layout.setSpacing(6)

        self.turn_dot = TurnDot()
        layout.addWidget(self.turn_dot)

        # Fixed height: the status line appearing and vanishing must not
        # reflow the board underneath it.
        self.status_label = QLabel("ChessListener")
        self.status_label.setObjectName("statusInfo")
        self.status_label.setFixedHeight(16)
        layout.addWidget(self.status_label, 1)

        self.source_label = QLabel("")
        self.source_label.setFixedHeight(16)
        self.source_label.hide()
        layout.addWidget(self.source_label)

        self.compact_button = self.make_title_button("\u2013", "Collapse")
        self.compact_button.clicked.connect(self.toggle_compact)
        layout.addWidget(self.compact_button)

        self.recovery_button = self.make_title_button("\u21bb", "Recovery")
        self.recovery_button.clicked.connect(self.toggle_recovery)
        self.recovery_button.setEnabled(False)
        layout.addWidget(self.recovery_button)

        self.review_button = self.make_title_button("★", "Local Game Review")
        self.review_button.clicked.connect(self.toggle_review)
        self.review_button.setEnabled(True)
        layout.addWidget(self.review_button)

        self.study_button = self.make_title_button("◆", "Saved Studies")
        self.study_button.clicked.connect(self.toggle_study)
        self.study_button.setEnabled(study_rules is not None and self.review_store is not None)
        layout.addWidget(self.study_button)

        self.settings_button = self.make_title_button("\u2261", "Settings")
        self.settings_button.clicked.connect(self.toggle_settings)
        layout.addWidget(self.settings_button)

        if not DECORATED:
            close_button = self.make_title_button("\u00d7", "Close")
            close_button.clicked.connect(self.close)
            layout.addWidget(close_button)

        self.turn_dot.hide()
        self.compact_button.hide()
        self.recovery_button.hide()
        self.review_button.hide()
        self.study_button.hide()
        self.settings_button.hide()
        return bar

    @staticmethod
    def make_title_button(text, tooltip):
        button = QPushButton(text)
        button.setObjectName("titleButton")
        button.setToolTip(tooltip)
        button.setFixedSize(20, 20)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setCursor(Qt.CursorShape.ArrowCursor)
        return button

    @staticmethod
    def make_small_button(text, tooltip):
        button = QPushButton(text)
        button.setObjectName("tinyGhost")
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        return button

    def build_startup_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(13)

        title = QLabel("ChessListener")
        title.setObjectName("title")
        layout.addWidget(title)

        subtitle = QLabel(
            "Local analysis for Chess.com boards, including bot and test games."
        )
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        panel = QFrame()
        panel.setObjectName("panel")
        grid = QGridLayout(panel)
        grid.setContentsMargins(14, 14, 14, 14)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(9)

        self.startup_budget = self.make_budget_combo()
        self.startup_maia = self.make_maia_combo()

        budget_label = QLabel("Analysis strength")
        budget_label.setObjectName("heading")
        grid.addWidget(budget_label, 0, 0)
        grid.addWidget(self.startup_budget, 0, 1)

        self.startup_budget_help = QLabel()
        self.startup_budget_help.setObjectName("helper")
        self.startup_budget_help.setWordWrap(True)
        grid.addWidget(self.startup_budget_help, 1, 0, 1, 2)

        maia_label = QLabel("Natural-move model")
        maia_label.setObjectName("heading")
        grid.addWidget(maia_label, 2, 0)
        grid.addWidget(self.startup_maia, 2, 1)

        maia_help = QLabel(
            "Maia predicts the move a human of that rating would play. It does "
            "not affect Stockfish's evaluation."
        )
        maia_help.setObjectName("helper")
        maia_help.setWordWrap(True)
        grid.addWidget(maia_help, 3, 0, 1, 2)

        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)
        self.startup_budget.setMinimumWidth(150)
        layout.addWidget(panel)

        self.startup_budget.currentIndexChanged.connect(
            self.update_startup_budget_help
        )

        self.remember_check = QCheckBox("Remember these settings")
        layout.addWidget(self.remember_check)

        note = QLabel("All of this stays adjustable while analysis is running.")
        note.setObjectName("helper")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.start_button = QPushButton("Start analysis")
        self.start_button.clicked.connect(self.start_analysis)
        layout.addWidget(self.start_button)
        layout.addStretch(1)

        return page

    def build_analysis_page(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(9, 3, 9, 8)
        root.setSpacing(6)

        self.board = BoardView(self.piece_family)
        self.board.moveRequested.connect(self.handle_board_move_request)
        self.board.interactionHint.connect(self.handle_board_hint)
        root.addWidget(self.board, 1)

        self.eval_bar = EvalBar()
        root.addWidget(self.eval_bar)

        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(9)

        self.last_label = QLabel("")
        self.last_label.setObjectName("lastLine")
        self.best_label = QLabel("")
        self.best_label.setObjectName("engineLine")
        self.human_label = QLabel("")
        self.human_label.setObjectName("engineLine")

        for widget in (self.last_label, self.best_label, self.human_label):
            widget.setFont(mono)
            root.addWidget(widget)

        # Expanded mode uses selectable rows; best_label remains as the terse
        # Stockfish summary in compact mode.
        self.best_label.hide()

        self.candidate_scroll = QScrollArea()
        self.candidate_scroll.setWidgetResizable(True)
        self.candidate_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.candidate_scroll.setMaximumHeight(176)

        candidate_host = QWidget()
        candidate_layout = QVBoxLayout(candidate_host)
        candidate_layout.setContentsMargins(0, 0, 0, 0)
        candidate_layout.setSpacing(3)
        self.candidate_rows = []

        for rank in range(1, 6):
            row = CandidateRow(rank)
            row.clicked.connect(self.select_candidate)
            row.activated.connect(self.activate_candidate)
            row.hide()
            candidate_layout.addWidget(row)
            self.candidate_rows.append(row)

        candidate_layout.addStretch(1)
        self.candidate_scroll.setWidget(candidate_host)
        root.addWidget(self.candidate_scroll)

        self.live_toolbar = QWidget()
        live_controls = QHBoxLayout(self.live_toolbar)
        live_controls.setContentsMargins(0, 0, 0, 0)
        live_controls.setSpacing(5)

        self.preview_back_button = self.make_small_button(
            "‹", "Previous PV position"
        )
        self.preview_back_button.clicked.connect(self.preview_back)
        live_controls.addWidget(self.preview_back_button)

        self.preview_label = QLabel("PV 0/0")
        self.preview_label.setObjectName("helper")
        live_controls.addWidget(self.preview_label)

        self.preview_forward_button = self.make_small_button(
            "›", "Next PV position"
        )
        self.preview_forward_button.clicked.connect(self.preview_forward)
        live_controls.addWidget(self.preview_forward_button)
        live_controls.addStretch(1)

        self.resume_button = self.make_small_button("Resume", "Resume Analysis Lab")
        self.resume_button.clicked.connect(self.resume_explore)
        self.resume_button.hide()
        live_controls.addWidget(self.resume_button)

        self.explore_button = self.make_small_button("Explore", "Explore this position")
        self.explore_button.clicked.connect(self.start_explore)
        live_controls.addWidget(self.explore_button)
        root.addWidget(self.live_toolbar)

        self.explore_toolbar = QWidget()
        explore_controls = QHBoxLayout(self.explore_toolbar)
        explore_controls.setContentsMargins(0, 0, 0, 0)
        explore_controls.setSpacing(5)

        self.root_button = self.make_small_button("Root", "Return to branch root")
        self.root_button.clicked.connect(self.explore_root)
        explore_controls.addWidget(self.root_button)

        self.undo_button = self.make_small_button("↶", "Undo one explored move")
        self.undo_button.clicked.connect(self.explore_undo)
        explore_controls.addWidget(self.undo_button)

        self.redo_button = self.make_small_button("↷", "Redo one explored move")
        self.redo_button.clicked.connect(self.explore_redo)
        explore_controls.addWidget(self.redo_button)

        self.save_lab_button = self.make_small_button(
            "Save", "Save this Analysis Lab tree as a local study"
        )
        self.save_lab_button.clicked.connect(self.capture_current_study_prompt)
        explore_controls.addWidget(self.save_lab_button)
        explore_controls.addStretch(1)

        self.live_update_button = QPushButton("")
        self.live_update_button.setObjectName("liveBadge")
        self.live_update_button.setToolTip("The real game changed; return to it")
        self.live_update_button.clicked.connect(self.go_live)
        self.live_update_button.hide()
        explore_controls.addWidget(self.live_update_button)

        self.go_live_button = self.make_small_button("Go Live", "Return to the real game")
        self.go_live_button.clicked.connect(self.go_live)
        explore_controls.addWidget(self.go_live_button)
        self.explore_toolbar.hide()
        root.addWidget(self.explore_toolbar)

        self.breadcrumb_label = QLabel("")
        self.breadcrumb_label.setObjectName("breadcrumb")
        self.breadcrumb_label.setWordWrap(True)
        self.breadcrumb_label.hide()
        root.addWidget(self.breadcrumb_label)

        self.explanation_label = QLabel("")
        self.explanation_label.setObjectName("explanation")
        self.explanation_label.setWordWrap(True)
        self.explanation_label.hide()
        root.addWidget(self.explanation_label)

        return page

    def build_review_page(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(9, 7, 9, 9)
        root.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Local Review")
        title.setObjectName("heading")
        header.addWidget(title)
        header.addStretch(1)
        self.review_library_combo = QComboBox()
        self.review_library_combo.setMinimumWidth(120)
        self.review_library_combo.setToolTip("Saved local reviews")
        self.review_library_combo.currentIndexChanged.connect(self.load_library_selection)
        self.review_delete_button = self.make_small_button("Delete", "Delete selected saved review")
        self.review_delete_button.clicked.connect(self.delete_library_selection)
        self.review_export_button = self.make_small_button("Export PGN", "Export annotated PGN")
        self.review_export_button.clicked.connect(self.export_review_pgn)
        self.review_export_button.setEnabled(False)
        self.review_import_button = self.make_small_button("Import", "Import a local PGN game or FEN position")
        import_menu = QMenu(self.review_import_button)
        import_pgn_action = import_menu.addAction("Import PGN…")
        import_pgn_action.triggered.connect(self.import_review_pgn)
        import_fen_action = import_menu.addAction("Import FEN…")
        import_fen_action.triggered.connect(self.import_review_fen)
        self.review_import_button.setMenu(import_menu)
        header.addWidget(self.review_import_button)
        header.addWidget(self.review_export_button)
        close = self.make_small_button("Done", "Return to live analysis")
        close.clicked.connect(self.close_review)
        header.addWidget(close)
        root.addLayout(header)

        library_row = QHBoxLayout()
        library_label = QLabel("Saved")
        library_label.setObjectName("helper")
        library_row.addWidget(library_label)
        library_row.addWidget(self.review_library_combo, 1)
        library_row.addWidget(self.review_delete_button)
        root.addLayout(library_row)

        self.review_board = BoardView(self.piece_family)
        self.review_board.set_interactive(False)
        self.review_board.moveRequested.connect(self.handle_review_move_request)
        root.addWidget(self.review_board, 1)

        self.review_graph = ReviewGraph()
        self.review_graph.selected.connect(self.select_review_ply)
        root.addWidget(self.review_graph)

        self.review_summary = QLabel("A verified move history is required.")
        self.review_summary.setObjectName("helper")
        self.review_summary.setWordWrap(True)
        root.addWidget(self.review_summary)

        self.review_progress = QProgressBar()
        self.review_progress.setRange(0, 1)
        self.review_progress.setValue(0)
        root.addWidget(self.review_progress)

        navigator = QHBoxLayout()
        self.review_back_button = self.make_small_button("‹", "Previous reviewed move")
        self.review_back_button.clicked.connect(self.review_back)
        navigator.addWidget(self.review_back_button)
        self.review_forward_button = self.make_small_button("›", "Next reviewed move")
        self.review_forward_button.clicked.connect(self.review_forward)
        navigator.addWidget(self.review_forward_button)
        self.review_live_button = self.make_small_button("Final", "Jump to final game position")
        self.review_live_button.clicked.connect(self.review_final)
        navigator.addWidget(self.review_live_button)
        self.review_filter = QComboBox()
        for label, value in (
            ("All moves", "all"), ("Turning points", "turning"),
            ("Inaccuracies +", "errors"), ("Blunders only", "major"),
            ("Checks & captures", "forcing"),
        ):
            self.review_filter.addItem(label, value)
        self.review_filter.currentIndexChanged.connect(self.refresh_review_timeline)
        navigator.addWidget(self.review_filter, 1)
        root.addLayout(navigator)

        controls = QHBoxLayout()
        self.review_start_button = QPushButton("Run local review")
        self.review_start_button.setObjectName("primary")
        self.review_start_button.clicked.connect(self.start_game_review)
        controls.addWidget(self.review_start_button)
        self.review_cancel_button = self.make_small_button("Cancel", "Cancel review")
        self.review_cancel_button.clicked.connect(self.cancel_game_review)
        self.review_cancel_button.setEnabled(False)
        controls.addWidget(self.review_cancel_button)
        self.review_explore_button = self.make_small_button("Explore here", "Explore this historical position locally")
        self.review_explore_button.clicked.connect(self.toggle_review_explore)
        self.review_explore_button.setEnabled(False)
        controls.addWidget(self.review_explore_button)
        self.review_undo_button = self.make_small_button("Undo", "Undo explored move")
        self.review_undo_button.clicked.connect(self.review_explore_undo)
        self.review_undo_button.hide()
        controls.addWidget(self.review_undo_button)
        root.addLayout(controls)

        self.review_moves = QListWidget()
        self.review_moves.currentRowChanged.connect(self.select_filtered_review_move)
        self.review_moves.setMaximumHeight(180)
        root.addWidget(self.review_moves)

        self.review_detail = QLabel("")
        self.review_detail.setObjectName("explanation")
        self.review_detail.setWordWrap(True)
        root.addWidget(self.review_detail)
        return page

    def build_study_page(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(9, 7, 9, 9)
        root.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Saved Studies")
        title.setObjectName("heading")
        header.addWidget(title)
        header.addStretch(1)
        self.study_capture_button = self.make_small_button(
            "Save Lab", "Save the current Analysis Lab tree"
        )
        self.study_capture_button.clicked.connect(self.capture_current_study_prompt)
        header.addWidget(self.study_capture_button)
        self.study_new_button = self.make_small_button(
            "New", "Create a study from the current position"
        )
        self.study_new_button.clicked.connect(self.create_study_prompt)
        header.addWidget(self.study_new_button)
        done = self.make_small_button("Done", "Return to analysis")
        done.clicked.connect(self.close_study)
        header.addWidget(done)
        root.addLayout(header)

        search_row = QHBoxLayout()
        self.study_search = QLineEdit()
        self.study_search.setPlaceholderText("Search titles, variations, or notes")
        self.study_search.setClearButtonEnabled(True)
        self.study_search.textChanged.connect(self.filter_study_library)
        search_row.addWidget(self.study_search, 1)
        self.study_export_button = self.make_small_button(
            "Export", "Export the full annotated variation tree as PGN"
        )
        self.study_export_button.clicked.connect(self.export_study_pgn)
        search_row.addWidget(self.study_export_button)
        self.study_delete_button = self.make_small_button(
            "Delete", "Delete this saved study"
        )
        self.study_delete_button.clicked.connect(self.delete_current_study)
        search_row.addWidget(self.study_delete_button)
        root.addLayout(search_row)

        self.study_library_combo = QComboBox()
        self.study_library_combo.setToolTip("Saved local variation trees")
        self.study_library_combo.currentIndexChanged.connect(
            self.load_study_library_selection
        )
        root.addWidget(self.study_library_combo)

        self.study_board = BoardView(self.piece_family)
        self.study_board.set_interactive(False)
        self.study_board.moveRequested.connect(self.handle_study_move_request)
        self.study_board.interactionHint.connect(self.handle_board_hint)
        root.addWidget(self.study_board, 1)

        navigator = QHBoxLayout()
        self.study_root_button = self.make_small_button("Root", "Go to study root")
        self.study_root_button.clicked.connect(self.study_go_root)
        navigator.addWidget(self.study_root_button)
        self.study_back_button = self.make_small_button("↶", "Go to parent position")
        self.study_back_button.clicked.connect(self.study_go_parent)
        navigator.addWidget(self.study_back_button)
        self.study_forward_button = self.make_small_button(
            "↷", "Go to the first continuation"
        )
        self.study_forward_button.clicked.connect(self.study_go_forward)
        navigator.addWidget(self.study_forward_button)
        navigator.addStretch(1)
        self.study_analyse_button = self.make_small_button(
            "Analyse", "Refresh this position's local Stockfish snapshot"
        )
        self.study_analyse_button.clicked.connect(self.analyse_study_node)
        navigator.addWidget(self.study_analyse_button)
        root.addLayout(navigator)

        self.study_tree = QTreeWidget()
        self.study_tree.setHeaderLabels(("Variation", "Snapshot"))
        self.study_tree.setColumnWidth(0, 235)
        self.study_tree.setAlternatingRowColors(True)
        self.study_tree.setMaximumHeight(190)
        self.study_tree.currentItemChanged.connect(self.select_study_tree_item)
        self.study_tree.itemCollapsed.connect(
            lambda item: self.set_study_item_collapsed(item, True)
        )
        self.study_tree.itemExpanded.connect(
            lambda item: self.set_study_item_collapsed(item, False)
        )
        root.addWidget(self.study_tree)

        self.study_title_edit = QLineEdit()
        self.study_title_edit.setMaxLength(120)
        self.study_title_edit.setPlaceholderText("Study title")
        root.addWidget(self.study_title_edit)

        annotation_row = QHBoxLayout()
        self.study_name_edit = QLineEdit()
        self.study_name_edit.setMaxLength(120)
        self.study_name_edit.setPlaceholderText("Variation name (optional)")
        annotation_row.addWidget(self.study_name_edit, 1)
        self.study_save_note_button = self.make_small_button(
            "Save note", "Save title, variation name, and comment"
        )
        self.study_save_note_button.clicked.connect(self.save_study_annotation)
        annotation_row.addWidget(self.study_save_note_button)
        root.addLayout(annotation_row)

        self.study_comment_edit = QTextEdit()
        self.study_comment_edit.setAcceptRichText(False)
        self.study_comment_edit.setPlaceholderText(
            "Your note about this position or variation"
        )
        self.study_comment_edit.setMaximumHeight(78)
        root.addWidget(self.study_comment_edit)

        self.study_detail = QLabel(
            "Save an Analysis Lab tree or create a study from any position."
        )
        self.study_detail.setObjectName("explanation")
        self.study_detail.setWordWrap(True)
        root.addWidget(self.study_detail)
        return page

    def build_settings_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("Settings")
        title.setObjectName("heading")
        header.addWidget(title)
        header.addStretch(1)

        reset_button = QPushButton("Reset defaults")
        reset_button.setObjectName("ghost")
        reset_button.clicked.connect(self.reset_settings_defaults)
        header.addWidget(reset_button)

        done_button = QPushButton("Done")
        done_button.setObjectName("ghost")
        done_button.clicked.connect(self.close_settings)
        header.addWidget(done_button)
        layout.addLayout(header)

        self.live_budget = self.make_budget_combo()
        self.live_maia = self.make_maia_combo()
        self.live_threads = QSpinBox()
        self.live_threads.setRange(1, max(1, os.cpu_count() or 1))
        self.live_multipv = QSpinBox()
        self.live_multipv.setRange(1, 5)

        self.live_explore_budget = QComboBox()
        self.live_explore_budget.addItem("Same as live", -1)
        for name, milliseconds, description in BUDGET_PRESETS:
            label = name if milliseconds == 0 else f"{name} · {milliseconds}ms"
            self.live_explore_budget.addItem(label, milliseconds)
            self.live_explore_budget.setItemData(
                self.live_explore_budget.count() - 1,
                description,
                Qt.ItemDataRole.ToolTipRole,
            )

        self.live_pv_length = QComboBox()
        for value, label in PV_LENGTH_CHOICES:
            self.live_pv_length.addItem(label, value)

        self.live_follow = QComboBox()
        for value, label in FOLLOW_LIVE_CHOICES:
            self.live_follow.addItem(label, value)

        self.live_explanation = QComboBox()
        for value, label in EXPLANATION_CHOICES:
            self.live_explanation.addItem(label, value)

        self.live_eval_pov = QComboBox()
        for value, label in EVAL_POV_CHOICES:
            self.live_eval_pov.addItem(label, value)

        self.live_line_expansion = QComboBox()
        for value, label in LINE_EXPANSION_CHOICES:
            self.live_line_expansion.addItem(label, value)

        self.live_opacity = QComboBox()
        for percent, label in OPACITY_CHOICES:
            self.live_opacity.addItem(label, percent)

        self.live_review_strength = QComboBox()
        for label, milliseconds in (
            ("Quick · 150ms/position", 150),
            ("Balanced · 350ms/position", 350),
            ("Deep · 800ms/position", 800),
            ("Maximum · 1800ms/position", 1800),
        ):
            self.live_review_strength.addItem(label, milliseconds)
        self.live_review_lines = QSpinBox()
        self.live_review_lines.setRange(1, 5)
        self.live_review_auto = QCheckBox("Start when a completed game ends")
        self.live_review_sensitivity = QComboBox()
        self.live_review_sensitivity.addItem("Strict", "strict")
        self.live_review_sensitivity.addItem("Standard", "standard")
        self.live_review_sensitivity.addItem("Lenient", "lenient")
        self.live_study_auto = QCheckBox("Analyse a selected study position")
        self.live_study_snapshots = QCheckBox("Save evaluation snapshots")

        arrows = QWidget()
        arrow_layout = QHBoxLayout(arrows)
        arrow_layout.setContentsMargins(0, 0, 0, 0)
        arrow_layout.setSpacing(7)
        self.live_best_arrow = QCheckBox("SF")
        self.live_human_arrow = QCheckBox("Maia")
        self.live_played_arrow = QCheckBox("Played")
        for checkbox in (
            self.live_best_arrow, self.live_human_arrow, self.live_played_arrow
        ):
            arrow_layout.addWidget(checkbox)
        arrow_layout.addStretch(1)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(2, 2, 2, 2)
        content_layout.setSpacing(8)

        def add_group(title_text, rows):
            panel = QFrame()
            panel.setObjectName("panel")
            grid = QGridLayout(panel)
            grid.setContentsMargins(11, 9, 11, 11)
            grid.setHorizontalSpacing(9)
            grid.setVerticalSpacing(7)
            heading = QLabel(title_text)
            heading.setObjectName("heading")
            grid.addWidget(heading, 0, 0, 1, 2)
            for row, (label_text, widget) in enumerate(rows, start=1):
                grid.addWidget(QLabel(label_text), row, 0)
                grid.addWidget(widget, row, 1)
            grid.setColumnStretch(1, 1)
            content_layout.addWidget(panel)

        add_group("Engine", (
            ("Live strength", self.live_budget),
            ("Explore strength", self.live_explore_budget),
            ("Stockfish threads", self.live_threads),
            ("Candidate lines", self.live_multipv),
            ("Natural model", self.live_maia),
        ))
        add_group("Analysis Lab", (
            ("PV shown", self.live_pv_length),
            ("When live moves", self.live_follow),
            ("Explanations", self.live_explanation),
            ("Expand", self.live_line_expansion),
        ))
        add_group("Local Game Review", (
            ("Review strength", self.live_review_strength),
            ("Alternatives", self.live_review_lines),
            ("Classifications", self.live_review_sensitivity),
            ("Automatic", self.live_review_auto),
        ))
        add_group("Saved Studies", (
            ("Automatic", self.live_study_auto),
            ("Evaluations", self.live_study_snapshots),
        ))
        add_group("Display", (
            ("Evaluation POV", self.live_eval_pov),
            ("Arrows", arrows),
            ("Window opacity", self.live_opacity),
        ))

        self.live_budget_help = QLabel()
        self.live_budget_help.setObjectName("helper")
        self.live_budget_help.setWordWrap(True)
        content_layout.addWidget(self.live_budget_help)

        note = QLabel(
            "Engine settings apply to the next search. Analysis Lab never "
            "changes the authoritative live game position."
        )
        note.setObjectName("helper")
        note.setWordWrap(True)
        content_layout.addWidget(note)
        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        for widget in (self.live_budget, self.live_maia, self.live_explore_budget):
            widget.currentIndexChanged.connect(self.queue_settings)
        self.live_opacity.currentIndexChanged.connect(self.apply_opacity)
        for widget in (self.live_threads, self.live_multipv):
            widget.valueChanged.connect(self.queue_settings)
        self.live_budget.currentIndexChanged.connect(self.update_live_budget_help)

        for widget in (
            self.live_pv_length,
            self.live_follow,
            self.live_explanation,
            self.live_eval_pov,
            self.live_line_expansion,
            self.live_review_strength,
            self.live_review_sensitivity,
        ):
            widget.currentIndexChanged.connect(self.apply_ui_preferences)

        self.live_review_lines.valueChanged.connect(self.apply_ui_preferences)
        self.live_review_auto.stateChanged.connect(self.apply_ui_preferences)
        self.live_study_auto.stateChanged.connect(self.apply_ui_preferences)
        self.live_study_snapshots.stateChanged.connect(self.apply_ui_preferences)

        for checkbox in (
            self.live_best_arrow, self.live_human_arrow, self.live_played_arrow
        ):
            checkbox.stateChanged.connect(self.apply_ui_preferences)

        return page

    def build_recovery_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 12, 16, 14)
        layout.setSpacing(9)

        header = QHBoxLayout()
        title = QLabel("Recovery")
        title.setObjectName("heading")
        header.addWidget(title)
        header.addStretch(1)

        done_button = QPushButton("Done")
        done_button.setObjectName("ghost")
        done_button.clicked.connect(self.close_recovery)
        header.addWidget(done_button)
        layout.addLayout(header)

        help_text = QLabel(
            "Use recovery when the visible board and overlay no longer agree. "
            "The native host validates every replacement position."
        )
        help_text.setObjectName("helper")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)

        rescan_button = QPushButton("Re-read board from Chess.com")
        rescan_button.setObjectName("ghost")
        rescan_button.clicked.connect(self.request_rescan)
        layout.addWidget(rescan_button)

        panel = QFrame()
        panel.setObjectName("panel")
        grid = QGridLayout(panel)
        grid.setContentsMargins(12, 12, 12, 12)
        grid.setHorizontalSpacing(9)
        grid.setVerticalSpacing(7)

        grid.addWidget(QLabel("Reset from visible board"), 0, 0, 1, 2)

        self.recovery_side = QComboBox()
        self.recovery_side.addItem("White to move", "w")
        self.recovery_side.addItem("Black to move", "b")
        grid.addWidget(QLabel("Side"), 1, 0)
        grid.addWidget(self.recovery_side, 1, 1)

        castling = QWidget()
        castling_layout = QHBoxLayout(castling)
        castling_layout.setContentsMargins(0, 0, 0, 0)
        castling_layout.setSpacing(7)
        self.castle_white_king = QCheckBox("K")
        self.castle_white_queen = QCheckBox("Q")
        self.castle_black_king = QCheckBox("k")
        self.castle_black_queen = QCheckBox("q")

        for checkbox in (
            self.castle_white_king,
            self.castle_white_queen,
            self.castle_black_king,
            self.castle_black_queen,
        ):
            castling_layout.addWidget(checkbox)
        castling_layout.addStretch(1)

        grid.addWidget(QLabel("Castling"), 2, 0)
        grid.addWidget(castling, 2, 1)

        self.recovery_en_passant = QLineEdit()
        self.recovery_en_passant.setMaxLength(2)
        self.recovery_en_passant.setPlaceholderText("-")
        grid.addWidget(QLabel("En-passant"), 3, 0)
        grid.addWidget(self.recovery_en_passant, 3, 1)

        counters = QWidget()
        counters_layout = QHBoxLayout(counters)
        counters_layout.setContentsMargins(0, 0, 0, 0)
        counters_layout.setSpacing(6)
        self.recovery_halfmove = QSpinBox()
        self.recovery_halfmove.setRange(0, 9999)
        self.recovery_fullmove = QSpinBox()
        self.recovery_fullmove.setRange(1, 9999)
        counters_layout.addWidget(self.recovery_halfmove)
        counters_layout.addWidget(QLabel("/"))
        counters_layout.addWidget(self.recovery_fullmove)
        grid.addWidget(QLabel("Half / fullmove"), 4, 0)
        grid.addWidget(counters, 4, 1)

        visible_button = QPushButton("Apply visible board")
        visible_button.clicked.connect(self.apply_visible_fen)
        grid.addWidget(visible_button, 5, 0, 1, 2)
        grid.setColumnStretch(1, 1)
        layout.addWidget(panel)

        self.recovery_exact_fen = QLineEdit()
        self.recovery_exact_fen.setPlaceholderText(
            "Exact six-field FEN, for example: 8/8/8/8/8/8/4K3/7k w - - 0 1"
        )
        layout.addWidget(self.recovery_exact_fen)

        exact_button = QPushButton("Apply exact FEN")
        exact_button.setObjectName("ghost")
        exact_button.clicked.connect(self.apply_exact_fen)
        layout.addWidget(exact_button)

        actions = QHBoxLayout()
        restart_button = QPushButton("Restart engines")
        restart_button.setObjectName("ghost")
        restart_button.clicked.connect(self.request_engine_restart)
        actions.addWidget(restart_button)

        stop_button = QPushButton("Stop session")
        stop_button.setObjectName("ghost")
        stop_button.clicked.connect(self.request_session_stop)
        actions.addWidget(stop_button)
        layout.addLayout(actions)
        layout.addStretch(1)

        self.recovery_controls = (
            rescan_button,
            panel,
            self.recovery_exact_fen,
            exact_button,
            restart_button,
            stop_button,
        )

        for control in self.recovery_controls:
            control.setEnabled(False)

        return page

    def make_budget_combo(self):
        combo = QComboBox()

        for name, milliseconds, description in BUDGET_PRESETS:
            label = (
                name if milliseconds == 0 else f"{name} \u00b7 {milliseconds}ms"
            )
            combo.addItem(label, milliseconds)
            combo.setItemData(
                combo.count() - 1, description, Qt.ItemDataRole.ToolTipRole
            )

        return combo

    def make_maia_combo(self):
        combo = QComboBox()

        combo.addItem("Maia Off", 0)

        for rating in MAIA_RATINGS:
            combo.addItem(f"Maia {rating}", rating)

        return combo

    @staticmethod
    def budget_description(milliseconds):
        for _name, value, description in BUDGET_PRESETS:
            if value == milliseconds:
                return description

        return ""

    def update_startup_budget_help(self):
        self.startup_budget_help.setText(
            self.budget_description(self.startup_budget.currentData())
        )

    def update_live_budget_help(self):
        self.live_budget_help.setText(
            self.budget_description(self.live_budget.currentData())
        )

    # -- persistence ------------------------------------------------------

    def restore_saved_settings(self):
        remember = setting_bool(self.settings.value("startup/remember", True), True)
        self.remember_check.setChecked(remember)

        try:
            saved_budget = int(self.settings.value("startup/budget_ms", 400))
            saved_rating = int(self.settings.value("startup/maia_rating", 1900))
            saved_threads = int(self.settings.value("startup/threads", self.threads))
            saved_multipv = int(self.settings.value("startup/multipv", 3))
            saved_explore_budget = int(
                self.settings.value("analysis_lab/budget_ms", -1)
            )
        except (TypeError, ValueError):
            saved_budget, saved_rating = 400, 1900
            saved_threads, saved_multipv = self.threads, 3
            saved_explore_budget = -1

        budget_values = {value for _name, value, _description in BUDGET_PRESETS}
        self.budget_ms = saved_budget if saved_budget in budget_values else 400
        self.maia_rating = (
            saved_rating if saved_rating == 0 or saved_rating in MAIA_RATINGS else 1900
        )
        self.threads = max(1, min(self.live_threads.maximum(), saved_threads))
        self.multipv = max(1, min(5, saved_multipv))
        self.explore_budget = (
            saved_explore_budget
            if saved_explore_budget == -1 or saved_explore_budget in budget_values
            else -1
        )

        try:
            self.pv_display_length = int(
                self.settings.value("analysis_lab/pv_length", 6)
            )
        except (TypeError, ValueError):
            self.pv_display_length = 6

        if self.pv_display_length not in {value for value, _label in PV_LENGTH_CHOICES}:
            self.pv_display_length = 6

        self.follow_live = str(
            self.settings.value("analysis_lab/follow_live", "notify")
        )
        self.explanation_level = str(
            self.settings.value("analysis_lab/explanation", "compact")
        )
        self.eval_pov = str(self.settings.value("display/eval_pov", "white"))
        self.line_expansion = str(
            self.settings.value("analysis_lab/line_expansion", "selected")
        )
        if self.follow_live not in {value for value, _label in FOLLOW_LIVE_CHOICES}:
            self.follow_live = "notify"
        if self.explanation_level not in {value for value, _label in EXPLANATION_CHOICES}:
            self.explanation_level = "compact"
        if self.eval_pov not in {value for value, _label in EVAL_POV_CHOICES}:
            self.eval_pov = "white"
        if self.line_expansion not in {value for value, _label in LINE_EXPANSION_CHOICES}:
            self.line_expansion = "selected"
        self.show_best_arrow = setting_bool(
            self.settings.value("display/arrow_stockfish", True), True
        )
        self.show_human_arrow = setting_bool(
            self.settings.value("display/arrow_maia", True), True
        )
        self.show_played_highlight = setting_bool(
            self.settings.value("display/arrow_played", True), True
        )
        try:
            self.review_time_ms = int(self.settings.value("review/time_ms", 350))
            self.review_lines = int(self.settings.value("review/lines", 2))
        except (TypeError, ValueError):
            self.review_time_ms, self.review_lines = 350, 2
        if self.review_time_ms not in {150, 350, 800, 1800}:
            self.review_time_ms = 350
        self.review_lines = max(1, min(5, self.review_lines))
        self.review_auto = setting_bool(
            self.settings.value("review/automatic", False), False
        )
        self.review_sensitivity = str(
            self.settings.value("review/sensitivity", "standard")
        )
        if self.review_sensitivity not in {"strict", "standard", "lenient"}:
            self.review_sensitivity = "standard"
        self.study_auto_analyse = setting_bool(
            self.settings.value("studies/auto_analyse", True), True
        )
        self.study_save_evals = setting_bool(
            self.settings.value("studies/save_evaluations", True), True
        )

        self.select_data(self.startup_budget, self.budget_ms, 1)
        self.select_data(self.startup_maia, self.maia_rating, self.startup_maia.count() - 1)

        self.applying_settings = True
        self.select_data(self.live_budget, self.budget_ms, 1)
        self.select_data(self.live_maia, self.maia_rating, self.live_maia.count() - 1)
        self.live_threads.setValue(self.threads)
        self.live_multipv.setValue(self.multipv)
        self.select_data(self.live_explore_budget, self.explore_budget, 0)
        self.select_data(self.live_pv_length, self.pv_display_length, 1)
        self.select_data(self.live_follow, self.follow_live, 0)
        self.select_data(self.live_explanation, self.explanation_level, 1)
        self.select_data(self.live_eval_pov, self.eval_pov, 0)
        self.select_data(self.live_line_expansion, self.line_expansion, 0)
        self.live_best_arrow.setChecked(self.show_best_arrow)
        self.live_human_arrow.setChecked(self.show_human_arrow)
        self.live_played_arrow.setChecked(self.show_played_highlight)
        self.select_data(self.live_review_strength, self.review_time_ms, 1)
        self.live_review_lines.setValue(self.review_lines)
        self.live_review_auto.setChecked(self.review_auto)
        self.select_data(self.live_review_sensitivity, self.review_sensitivity, 1)
        self.live_study_auto.setChecked(self.study_auto_analyse)
        self.live_study_snapshots.setChecked(self.study_save_evals)

        try:
            saved_opacity = int(self.settings.value("window/opacity", 100))
        except (TypeError, ValueError):
            saved_opacity = 100

        self.select_data(self.live_opacity, saved_opacity, 0)
        self.applying_settings = False
        self.apply_opacity()

        self.update_startup_budget_help()
        self.update_live_budget_help()

    @staticmethod
    def select_data(combo, value, fallback_index):
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else fallback_index)

    def save_settings(self):
        if self.remember_check.isChecked():
            self.settings.setValue("startup/remember", True)
            self.settings.setValue("startup/budget_ms", self.budget_ms)
            self.settings.setValue("startup/maia_rating", self.maia_rating)
            self.settings.setValue("startup/threads", self.threads)
            self.settings.setValue("startup/multipv", self.multipv)
        else:
            self.settings.setValue("startup/remember", False)

            for key in (
                "startup/budget_ms",
                "startup/maia_rating",
                "startup/threads",
                "startup/multipv",
            ):
                self.settings.remove(key)

        self.settings.setValue("analysis_lab/budget_ms", self.explore_budget)
        self.settings.setValue("analysis_lab/pv_length", self.pv_display_length)
        self.settings.setValue("analysis_lab/follow_live", self.follow_live)
        self.settings.setValue(
            "analysis_lab/explanation", self.explanation_level
        )
        self.settings.setValue(
            "analysis_lab/line_expansion", self.line_expansion
        )
        self.settings.setValue("review/time_ms", self.review_time_ms)
        self.settings.setValue("review/lines", self.review_lines)
        self.settings.setValue("review/automatic", self.review_auto)
        self.settings.setValue("review/sensitivity", self.review_sensitivity)
        self.settings.setValue("studies/auto_analyse", self.study_auto_analyse)
        self.settings.setValue("studies/save_evaluations", self.study_save_evals)
        self.settings.setValue("display/eval_pov", self.eval_pov)
        self.settings.setValue("display/arrow_stockfish", self.show_best_arrow)
        self.settings.setValue("display/arrow_maia", self.show_human_arrow)
        self.settings.setValue("display/arrow_played", self.show_played_highlight)

        self.settings.sync()

    # -- control protocol -------------------------------------------------

    def send_control(self, command):
        if self.local_mode:
            return
        try:
            os.write(sys.stdout.fileno(), (command + "\n").encode("ascii"))
        except OSError as error:
            print(f"overlay: control write failed: {error}", file=sys.stderr)

    def settings_payload(self):
        return (
            f"budget={self.budget_ms} maia={self.maia_rating} "
            f"threads={self.threads} multipv={self.multipv} "
            f"explore_budget={self.explore_budget}"
        )

    def start_analysis(self):
        if self.start_command_sent:
            return

        self.budget_ms = int(self.startup_budget.currentData())
        self.maia_rating = int(self.startup_maia.currentData())

        self.applying_settings = True
        self.select_data(self.live_budget, self.budget_ms, 1)
        self.select_data(self.live_maia, self.maia_rating, self.live_maia.count() - 1)
        self.applying_settings = False

        self.save_settings()
        self.start_command_sent = True

        engines = "Stockfish" if self.maia_rating == 0 else "Stockfish and Maia"
        self.set_status(f"Starting {engines}\u2026", "info", linger=False)
        self.stack.setCurrentWidget(self.analysis_page)

        for widget in (
            self.turn_dot,
            self.compact_button,
            self.recovery_button,
            self.review_button,
            self.study_button,
            self.settings_button,
        ):
            widget.show()

        saved = self.settings.value("window/geometry")
        restored = False

        if saved is not None:
            try:
                restored = self.restoreGeometry(saved)
            except (TypeError, RuntimeError):
                restored = False

        if not restored:
            self.resize(352, 500)

        self.frame_timer.start()
        self.send_control(
            f"START protocol={PROTOCOL_VERSION} ui_version={APP_VERSION} "
            + self.settings_payload()
        )

    def start_local_review_mode(self):
        """Open the review/import UI without Firefox or the native host."""
        self.local_mode = True
        self.start_command_sent = True
        self.session_active = False
        self.session_id = ""
        self.status_label.setText("Local review")
        self.settings_button.show()
        self.study_button.show()
        self.compact_button.hide()
        self.review_button.hide()
        self.recovery_button.hide()
        self.turn_dot.hide()
        self.review_button.setEnabled(True)
        self.open_review()
        saved = self.settings.value("window/geometry")
        try:
            restored = saved is not None and self.restoreGeometry(saved)
        except (TypeError, RuntimeError):
            restored = False
        if not restored:
            self.resize(390, 620)

    def queue_settings(self):
        if self.applying_settings or not self.start_command_sent:
            return

        self.settings_timer.start()

    def send_settings(self):
        self.budget_ms = int(self.live_budget.currentData())
        self.maia_rating = int(self.live_maia.currentData())
        self.threads = int(self.live_threads.value())
        self.multipv = int(self.live_multipv.value())
        self.explore_budget = int(self.live_explore_budget.currentData())

        self.save_settings()
        self.send_control("SET " + self.settings_payload())
        self.dirty = True

    def apply_ui_preferences(self):
        if self.applying_settings:
            return

        self.pv_display_length = int(self.live_pv_length.currentData())
        self.follow_live = str(self.live_follow.currentData())
        self.explanation_level = str(self.live_explanation.currentData())
        self.eval_pov = str(self.live_eval_pov.currentData())
        self.line_expansion = str(self.live_line_expansion.currentData())
        self.review_time_ms = int(self.live_review_strength.currentData())
        self.review_lines = int(self.live_review_lines.value())
        self.review_auto = self.live_review_auto.isChecked()
        self.review_sensitivity = str(self.live_review_sensitivity.currentData())
        self.study_auto_analyse = self.live_study_auto.isChecked()
        self.study_save_evals = self.live_study_snapshots.isChecked()
        self.show_best_arrow = self.live_best_arrow.isChecked()
        self.show_human_arrow = self.live_human_arrow.isChecked()
        self.show_played_highlight = self.live_played_arrow.isChecked()
        self.save_settings()
        self.refresh_candidate_rows()
        self.refresh_explanation()
        self.dirty = True

    def reset_settings_defaults(self):
        default_threads = min(4, max(1, (os.cpu_count() or 2) // 2))
        self.applying_settings = True
        self.select_data(self.live_budget, 400, 1)
        self.select_data(self.live_explore_budget, -1, 0)
        self.live_threads.setValue(default_threads)
        self.live_multipv.setValue(3)
        self.select_data(self.live_maia, 1900, self.live_maia.count() - 1)
        self.select_data(self.live_pv_length, 6, 1)
        self.select_data(self.live_follow, "notify", 0)
        self.select_data(self.live_explanation, "compact", 1)
        self.select_data(self.live_eval_pov, "white", 0)
        self.select_data(self.live_line_expansion, "selected", 0)
        self.live_best_arrow.setChecked(True)
        self.live_human_arrow.setChecked(True)
        self.live_played_arrow.setChecked(True)
        self.select_data(self.live_review_strength, 350, 1)
        self.live_review_lines.setValue(2)
        self.live_review_auto.setChecked(False)
        self.select_data(self.live_review_sensitivity, "standard", 1)
        self.live_study_auto.setChecked(True)
        self.live_study_snapshots.setChecked(True)
        self.select_data(self.live_opacity, 100, 0)
        self.select_data(self.startup_budget, 400, 1)
        self.select_data(self.startup_maia, 1900, self.startup_maia.count() - 1)
        self.applying_settings = False

        self.budget_ms = 400
        self.explore_budget = -1
        self.threads = default_threads
        self.multipv = 3
        self.maia_rating = 1900
        self.review_time_ms = 350
        self.review_lines = 2
        self.review_auto = False
        self.review_sensitivity = "standard"
        self.study_auto_analyse = True
        self.study_save_evals = True
        self.apply_ui_preferences()
        self.apply_opacity()

        if self.start_command_sent:
            self.send_settings()
        else:
            self.save_settings()

        self.set_status("Settings reset", "info", linger=True)

    def apply_opacity(self):
        self.opacity_percent = int(self.live_opacity.currentData())
        self.setWindowOpacity(self.opacity_percent / 100.0)
        self.settings.setValue("window/opacity", self.opacity_percent)

    def toggle_compact(self):
        if not self.start_command_sent:
            return

        self.compact = not self.compact

        if self.compact:
            self.expanded_geometry = self.saveGeometry()
            self.compact_button.setText("+")
            self.compact_button.setToolTip("Expand")
        else:
            self.compact_button.setText("\u2013")
            self.compact_button.setToolTip("Collapse")

        for widget in (
            self.board,
            self.last_label,
            self.candidate_scroll,
            self.live_toolbar,
            self.explore_toolbar,
            self.breadcrumb_label,
            self.explanation_label,
        ):
            widget.setVisible(not self.compact)

        self.best_label.setVisible(self.compact)

        if not self.compact:
            self.update_analysis_mode_ui()

        # The board has an Expanding policy and a 200 px floor, so the window
        # will not shrink until the layout has been recalculated without it.
        QTimer.singleShot(0, self.settle_after_compact)

    def settle_after_compact(self):
        self.analysis_page.updateGeometry()
        self.stack.updateGeometry()
        self.layout().activate()

        if self.compact:
            self.resize(self.width(), self.sizeHint().height())
        elif self.expanded_geometry is not None:
            self.restoreGeometry(self.expanded_geometry)

    def toggle_settings(self):
        if self.stack.currentWidget() is self.settings_page:
            self.close_settings()
        else:
            self.open_settings()

    def save_geometry(self):
        if self.start_command_sent and not self.compact:
            self.settings.setValue("window/geometry", self.saveGeometry())

    def note_geometry_change(self):
        if self.start_command_sent and not self.compact:
            self.geometry_timer.start()

    def moveEvent(self, event):
        super().moveEvent(event)
        self.note_geometry_change()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.note_geometry_change()

    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()

        if self.stack.currentWidget() is self.review_page:
            if key == Qt.Key.Key_Left:
                self.review_back()
                return
            if key == Qt.Key.Key_Right:
                self.review_forward()
                return
            if key == Qt.Key.Key_Home and self.review_mode == "game":
                self.select_review_ply(0)
                return
            if key == Qt.Key.Key_End:
                self.review_final()
                return
            if key == Qt.Key.Key_Escape:
                if self.review_mode == "explore":
                    self.leave_review_explore()
                    self.select_review_ply(self.review_selected_ply)
                else:
                    self.close_review()
                return

        if self.stack.currentWidget() is self.study_page:
            if key == Qt.Key.Key_Left:
                self.study_go_parent()
                return
            if key == Qt.Key.Key_Right:
                self.study_go_forward()
                return
            if key == Qt.Key.Key_Home:
                self.study_go_root()
                return
            if key == Qt.Key.Key_S and modifiers & Qt.KeyboardModifier.ControlModifier:
                self.save_study_annotation()
                return
            if key == Qt.Key.Key_Escape:
                self.close_study()
                return

        if self.mode == "explore" and modifiers & Qt.KeyboardModifier.ControlModifier:
            if key == Qt.Key.Key_Z:
                if modifiers & Qt.KeyboardModifier.ShiftModifier:
                    self.explore_redo()
                else:
                    self.explore_undo()
                return

        if self.mode == "explore" and key == Qt.Key.Key_Home:
            self.explore_root()
            return

        if self.mode == "explore" and key == Qt.Key.Key_L:
            self.go_live()
            return

        if key == Qt.Key.Key_Escape:
            if self.stack.currentWidget() is self.settings_page:
                self.close_settings()
                return
            if self.stack.currentWidget() is self.recovery_page:
                self.close_recovery()
                return

        if key == Qt.Key.Key_Space and self.start_command_sent:
            self.toggle_compact()
            return

        super().keyPressEvent(event)

    def open_settings(self):
        current = self.stack.currentWidget()
        if current is not self.settings_page:
            self.settings_return_page = current
        self.stack.setCurrentWidget(self.settings_page)

    def close_settings(self):
        # Don't make the user wait out the debounce just because they were quick.
        if self.settings_timer.isActive():
            self.settings_timer.stop()
            self.send_settings()

        target = self.settings_return_page
        if target is None or target is self.settings_page:
            target = self.review_page if self.local_mode else self.analysis_page
        self.settings_return_page = None
        self.stack.setCurrentWidget(target)

    def toggle_recovery(self):
        if self.stack.currentWidget() is self.recovery_page:
            self.close_recovery()
        else:
            self.open_recovery()

    def open_recovery(self):
        if not self.start_command_sent or not self.session_active:
            self.set_status("No active analysis session", "warn", linger=False)
            return

        self.populate_recovery_fields()
        self.stack.setCurrentWidget(self.recovery_page)

    def close_recovery(self):
        self.stack.setCurrentWidget(self.analysis_page)

    def set_recovery_enabled(self, enabled):
        self.recovery_button.setEnabled(enabled)

        for control in self.recovery_controls:
            control.setEnabled(enabled)

    def populate_recovery_fields(self):
        recovery_fen = (
            self.live_snapshot.get("fen", "")
            if self.mode == "explore" and self.live_snapshot
            else self.fen
        )
        fields = recovery_fen.split()

        if len(fields) == 6:
            _board, side, castling, en_passant, halfmove, fullmove = fields
        else:
            side, castling, en_passant, halfmove, fullmove = (
                self.side_to_move,
                "-",
                "-",
                "0",
                "1",
            )

        self.select_data(self.recovery_side, side, 0)
        self.castle_white_king.setChecked("K" in castling)
        self.castle_white_queen.setChecked("Q" in castling)
        self.castle_black_king.setChecked("k" in castling)
        self.castle_black_queen.setChecked("q" in castling)
        self.recovery_en_passant.setText(
            "" if en_passant == "-" else en_passant
        )

        try:
            self.recovery_halfmove.setValue(max(0, int(halfmove)))
            self.recovery_fullmove.setValue(max(1, int(fullmove)))
        except ValueError:
            self.recovery_halfmove.setValue(0)
            self.recovery_fullmove.setValue(1)

        self.recovery_exact_fen.setText(recovery_fen)

    def visible_board_fen(self):
        recovery_grid = (
            self.live_snapshot.get("grid", self.grid)
            if self.mode == "explore" and self.live_snapshot
            else self.grid
        )
        board = grid_to_fen_board(recovery_grid)
        side = self.recovery_side.currentData()
        castling = "".join(
            right
            for right, checkbox in (
                ("K", self.castle_white_king),
                ("Q", self.castle_white_queen),
                ("k", self.castle_black_king),
                ("q", self.castle_black_queen),
            )
            if checkbox.isChecked()
        ) or "-"
        en_passant = self.recovery_en_passant.text().strip() or "-"

        return validate_fen_input(
            f"{board} {side} {castling} {en_passant} "
            f"{self.recovery_halfmove.value()} {self.recovery_fullmove.value()}"
        )

    def scoped_control(self, command, payload=None):
        if not self.session_active or not self.session_id:
            raise ValueError("No active analysis session")

        line = f"{command} {self.session_id}"
        return line if payload is None else f"{line} {payload}"

    def begin_recovery(self, command, status, payload=None):
        try:
            control = self.scoped_control(command, payload)
        except ValueError as error:
            self.set_status(str(error), "warn", linger=False)
            return

        self.recovery_action = command.lower()
        self.send_control(control)
        self.stack.setCurrentWidget(self.analysis_page)
        self.set_status(status, "info", linger=True)
        self.dirty = True

    def request_rescan(self):
        self.begin_recovery("RESCAN", "Waiting for the visible board\u2026")

    def apply_visible_fen(self):
        try:
            fen = self.visible_board_fen()
        except ValueError as error:
            self.set_status(str(error), "warn", linger=False)
            return

        self.begin_recovery(
            "FEN", "Validating the position\u2026", payload=fen
        )

    def apply_exact_fen(self):
        try:
            fen = validate_fen_input(self.recovery_exact_fen.text())
        except ValueError as error:
            self.set_status(str(error), "warn", linger=False)
            return

        self.begin_recovery(
            "FEN", "Validating the position\u2026", payload=fen
        )

    def request_engine_restart(self):
        self.begin_recovery("RESTART", "Restarting engines\u2026")

    def request_session_stop(self):
        self.begin_recovery("STOP", "Stopping this session\u2026")

    # -- Saved Studies ---------------------------------------------------

    def toggle_study(self):
        if self.stack.currentWidget() is self.study_page:
            self.close_study()
        else:
            self.open_study()

    def open_study(self):
        current = self.stack.currentWidget()
        if current is not self.study_page:
            self.study_return_page = current
        self.stack.setCurrentWidget(self.study_page)
        self.study_capture_button.setEnabled(
            self.mode == "explore"
            and self.explore_root_node_id in self.explore_nodes
            and study_rules is not None
        )
        studies = self.populate_study_library(self.current_study_id)
        if self.current_study is None and studies:
            self.load_study(studies[0])
        elif self.current_study is not None:
            self.refresh_study_tree()
            self.select_study_node(
                self.current_study.get("selected", self.current_study.get("root")),
                analyse=False,
                persist=False,
            )

    def close_study(self):
        if self.current_study is not None:
            self.persist_current_study()
        self.cancel_study_analysis()
        target = self.study_return_page
        if (
            target is None or target is self.study_page
            or (target is self.startup_page and self.start_command_sent)
        ):
            target = self.review_page if self.local_mode else self.analysis_page
        self.study_return_page = None
        self.stack.setCurrentWidget(target)

    def populate_study_library(self, selected_id=None):
        if self.review_store is None or not hasattr(self, "study_library_combo"):
            return []
        query = self.study_search.text() if hasattr(self, "study_search") else ""
        studies = self.review_store.list_studies(query)
        self.applying_settings = True
        self.study_library_combo.clear()
        for item in studies:
            count = max(0, len(item.get("nodes", {})) - 1)
            self.study_library_combo.addItem(
                f"{item.get('title', 'Untitled study')} · {count} moves",
                item.get("id"),
            )
        if not studies:
            self.study_library_combo.addItem("No matching studies", None)
        if selected_id:
            self.select_data(self.study_library_combo, selected_id, 0)
        self.study_library_combo.setEnabled(bool(studies))
        self.study_delete_button.setEnabled(bool(studies))
        self.study_export_button.setEnabled(bool(self.current_study))
        self.applying_settings = False
        return studies

    def filter_study_library(self, _text=""):
        studies = self.populate_study_library(self.current_study_id)
        identifiers = {item.get("id") for item in studies}
        if studies and self.current_study_id not in identifiers:
            self.load_study(studies[0])

    def load_study_library_selection(self):
        if self.applying_settings or self.review_store is None:
            return
        item = self.review_store.find_study(self.study_library_combo.currentData())
        if item is not None:
            self.load_study(item)

    def load_study(self, raw):
        if study_rules is None:
            return False
        self.cancel_study_analysis()
        try:
            self.current_study = study_rules.normalise_study(raw)
        except ValueError as error:
            self.set_status(f"Could not load study: {error}", "warn", linger=False)
            return False
        self.current_study_id = self.current_study.get("id") or None
        self.study_node_id = self.current_study.get("selected", self.current_study["root"])
        self.refresh_study_tree()
        self.select_study_node(self.study_node_id, analyse=False, persist=False)
        self.study_export_button.setEnabled(True)
        return True

    def available_study_fen(self):
        if self.stack.currentWidget() is self.study_page and self.current_study:
            node = self.current_study["nodes"].get(str(self.study_node_id), {})
            if node.get("fen"):
                return node["fen"]
        if self.stack.currentWidget() is self.review_page and self.review_positions:
            index = max(0, min(self.review_selected_ply, len(self.review_positions) - 1))
            return self.review_positions[index]
        return self.fen

    def create_study_prompt(self):
        if study_rules is None or self.review_store is None:
            self.set_status("Saved studies are not installed", "warn", linger=False)
            return
        try:
            fen = study_rules.canonical_fen(self.available_study_fen())
        except ValueError as error:
            self.set_status(str(error), "warn", linger=False)
            return
        title, accepted = QInputDialog.getText(
            self, "New local study", "Study title:", text="Untitled study"
        )
        if accepted:
            self.create_study(title, fen)

    def create_study(self, title, fen):
        if study_rules is None:
            return False
        try:
            self.current_study = study_rules.new_study(title, fen)
            self.current_study_id = None
            self.study_node_id = self.current_study["root"]
            if not self.persist_current_study(refresh_library=True):
                return False
        except (OSError, ValueError) as error:
            self.set_status(f"Could not create study: {error}", "warn", linger=False)
            return False
        self.open_study()
        self.select_study_node(self.study_node_id, analyse=self.study_auto_analyse)
        self.set_status("Local study created", "info", linger=True)
        return True

    def capture_current_study_prompt(self):
        if (
            study_rules is None or self.review_store is None
            or self.mode != "explore"
            or self.explore_root_node_id not in self.explore_nodes
        ):
            self.set_status("Open Analysis Lab before saving its tree", "warn", linger=False)
            return
        suggested = self.session_label or "Analysis Lab study"
        title, accepted = QInputDialog.getText(
            self, "Save Analysis Lab", "Study title:", text=suggested
        )
        if accepted:
            self.capture_analysis_lab(title)

    def capture_analysis_lab(self, title):
        try:
            item = study_rules.from_explore_tree(
                title,
                self.explore_nodes,
                self.explore_root_node_id,
                self.explore_node_id,
                {"Site": "ChessListener Analysis Lab", "Source": self.session_label},
            )
            self.current_study = item
            self.current_study_id = None
            self.study_node_id = item["selected"]
            if not self.persist_current_study(refresh_library=True):
                return False
        except (OSError, ValueError, AttributeError) as error:
            self.set_status(f"Could not save Analysis Lab: {error}", "warn", linger=False)
            return False
        self.open_study()
        self.select_study_node(self.study_node_id, analyse=False, persist=False)
        self.set_status(
            f"Saved {len(item['nodes'])} study positions locally", "info", linger=True
        )
        return True

    def persist_current_study(self, refresh_library=False):
        if self.current_study is None or self.review_store is None or study_rules is None:
            return False
        try:
            clean = study_rules.normalise_study(self.current_study)
            identifier = self.review_store.save_study(clean)
            saved = self.review_store.find_study(identifier)
            if saved is None:
                raise ValueError("Study did not survive local validation")
            self.current_study = saved
            self.current_study_id = identifier
            self.study_node_id = self.current_study.get(
                "selected", self.current_study["root"]
            )
            if refresh_library:
                self.populate_study_library(identifier)
            return True
        except (OSError, ValueError) as error:
            self.set_status(f"Could not save study: {error}", "warn", linger=False)
            return False

    def study_node_score(self, node):
        lines = (node.get("analysis") or {}).get("lines") or []
        if not lines:
            return ""
        line = lines[0]
        side = str(node.get("fen", " w ").split()[1])
        cp, mate = score_for_pov(
            line.get("cp"), line.get("mate"), self.eval_pov, side
        )
        return format_line_score(line, cp, mate, self.eval_pov, side)

    def study_node_label(self, node_id):
        node = self.current_study["nodes"][node_id]
        if node_id == self.current_study["root"]:
            return "Root"
        parent = self.current_study["nodes"][node["parent"]]
        try:
            notation = san_rules.Board(parent["fen"]).san(node["move"])
        except (ValueError, AttributeError):
            notation = node["move"]
        return notation + (f" — {node['name']}" if node.get("name") else "")

    def refresh_study_tree(self):
        if self.current_study is None or not hasattr(self, "study_tree"):
            return
        selected = str(self.study_node_id or self.current_study["root"])
        self.study_tree_refreshing = True
        self.study_tree.clear()
        self.study_tree_items = {}

        def add(node_id, parent_item=None):
            node = self.current_study["nodes"][node_id]
            item = QTreeWidgetItem(
                [self.study_node_label(node_id), self.study_node_score(node)]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, node_id)
            if parent_item is None:
                self.study_tree.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
            self.study_tree_items[node_id] = item
            for child in node.get("children") or []:
                add(child, item)
            item.setExpanded(not node.get("collapsed", False))

        add(self.current_study["root"])
        current = self.study_tree_items.get(selected)
        if current is not None:
            self.study_tree.setCurrentItem(current)
            self.study_tree.scrollToItem(current)
        self.study_tree_refreshing = False

    def select_study_tree_item(self, item, _previous=None):
        if self.study_tree_refreshing or item is None:
            return
        node_id = item.data(0, Qt.ItemDataRole.UserRole)
        self.select_study_node(node_id, analyse=self.study_auto_analyse)

    def select_study_node(self, node_id, analyse=None, persist=False):
        if self.current_study is None:
            return False
        node_id = str(node_id)
        node = self.current_study["nodes"].get(node_id)
        if node is None:
            return False
        self.study_node_id = node_id
        self.current_study["selected"] = node_id
        try:
            grid, side = fen_to_grid(node["fen"])
        except ValueError:
            return False
        lines = (node.get("analysis") or {}).get("lines") or []
        best = (lines[0].get("pv") or [""])[0] if lines else ""
        self.study_board.set_position(grid, side, self.flip, node["fen"])
        self.study_board.set_moves(best, "", node.get("move", ""))
        self.study_board.set_interactive(True)
        self.study_position_lines = list(lines)
        self.study_annotation_loading = True
        self.study_title_edit.setText(self.current_study.get("title", ""))
        self.study_name_edit.setText(node.get("name", ""))
        self.study_comment_edit.setPlainText(node.get("comment", ""))
        self.study_annotation_loading = False
        parent = node.get("parent")
        children = node.get("children") or []
        self.study_root_button.setEnabled(node_id != self.current_study["root"])
        self.study_back_button.setEnabled(parent is not None)
        self.study_forward_button.setEnabled(bool(children))
        self.study_analyse_button.setEnabled(True)
        item = getattr(self, "study_tree_items", {}).get(node_id)
        if item is not None and self.study_tree.currentItem() is not item:
            self.study_tree_refreshing = True
            self.study_tree.setCurrentItem(item)
            self.study_tree_refreshing = False
        self.show_study_analysis()
        if persist:
            self.persist_current_study()
        if analyse is None:
            analyse = self.study_auto_analyse
        if analyse:
            self.analyse_study_node()
        return True

    def set_study_item_collapsed(self, item, collapsed):
        if self.study_tree_refreshing or self.current_study is None or item is None:
            return
        node_id = str(item.data(0, Qt.ItemDataRole.UserRole))
        node = self.current_study["nodes"].get(node_id)
        if node is not None and node.get("collapsed") != collapsed:
            node["collapsed"] = collapsed
            self.persist_current_study()

    def save_study_annotation(self):
        if self.study_annotation_loading or self.current_study is None:
            return
        node = self.current_study["nodes"].get(str(self.study_node_id))
        if node is None:
            return
        self.current_study["title"] = self.study_title_edit.text().strip() or "Untitled study"
        node["name"] = self.study_name_edit.text().strip()
        node["comment"] = self.study_comment_edit.toPlainText().strip()
        if self.persist_current_study(refresh_library=True):
            self.refresh_study_tree()
            self.set_status("Study note saved", "info", linger=True)

    def study_go_root(self):
        if self.current_study is not None:
            self.select_study_node(self.current_study["root"])

    def study_go_parent(self):
        if self.current_study is None:
            return
        node = self.current_study["nodes"].get(str(self.study_node_id), {})
        if node.get("parent") is not None:
            self.select_study_node(node["parent"])

    def study_go_forward(self):
        if self.current_study is None:
            return
        children = self.current_study["nodes"].get(
            str(self.study_node_id), {}
        ).get("children") or []
        if children:
            self.select_study_node(children[0])

    def handle_study_move_request(self, choices):
        if self.current_study is None or not choices:
            return
        if len(choices) == 1:
            self.apply_study_move(choices[0])
            return
        menu = QMenu(self)
        labels = {"q": "Queen", "r": "Rook", "b": "Bishop", "n": "Knight"}
        for move in choices:
            action = menu.addAction(labels.get(move[-1], move[-1].upper()))
            action.triggered.connect(
                lambda _checked=False, selected=move: self.apply_study_move(selected)
            )
        menu.exec(self.study_board.mapToGlobal(self.study_board.rect().center()))

    def apply_study_move(self, move):
        if self.current_study is None or study_rules is None:
            return False
        try:
            item, node_id, reused = study_rules.add_move(
                self.current_study, self.study_node_id, move
            )
        except ValueError as error:
            self.set_status(str(error), "warn", linger=True)
            return False
        self.current_study = item
        self.study_node_id = node_id
        if not self.persist_current_study(refresh_library=True):
            return False
        self.refresh_study_tree()
        self.select_study_node(
            node_id, analyse=self.study_auto_analyse, persist=False
        )
        self.set_status(
            "Opened saved continuation" if reused else "Added study variation",
            "info", linger=True,
        )
        return True

    def current_study_analysis_settings(self):
        milliseconds = self.explore_budget if self.explore_budget >= 0 else self.budget_ms
        if milliseconds == 0:
            milliseconds = 800
        return {
            "time_ms": max(25, int(milliseconds)),
            "lines": self.multipv,
            "threads": self.threads,
        }

    def cancel_study_analysis(self):
        if self.study_position_job is not None:
            self.study_position_job.cancel()
        self.study_position_timer.stop()
        self.study_position_job = None
        self.study_position_queue = None
        if hasattr(self, "study_analyse_button"):
            self.study_analyse_button.setEnabled(self.current_study is not None)

    def analyse_study_node(self):
        if self.current_study is None or review_rules is None:
            return
        node = self.current_study["nodes"].get(str(self.study_node_id))
        if node is None:
            return
        self.cancel_study_analysis()
        self.study_position_generation += 1
        self.study_analysis_node_id = str(self.study_node_id)
        self.study_position_job, self.study_position_queue = (
            review_rules.start_position_analysis(
                node["fen"], self.current_study_analysis_settings(),
                self.study_position_generation,
            )
        )
        self.study_analyse_button.setEnabled(False)
        self.study_detail.setText("Analysing this saved position locally…")
        self.study_position_timer.start()

    def poll_study_position(self):
        if self.study_position_queue is None:
            self.study_position_timer.stop()
            return
        while True:
            try:
                message = self.study_position_queue.get_nowait()
            except queue.Empty:
                break
            generation = int(message.get("generation", -1))
            if generation != self.study_position_generation:
                continue
            if message.get("type") == "position_complete":
                node_id = self.study_analysis_node_id
                node = (
                    self.current_study["nodes"].get(node_id)
                    if self.current_study is not None else None
                )
                if node is not None and message.get("fen") == node.get("fen"):
                    self.study_position_lines = list(message.get("lines") or [])
                    if self.study_save_evals:
                        node["analysis"] = {
                            "lines": self.study_position_lines,
                            "depth": max(
                                (int(line.get("depth", 0)) for line in self.study_position_lines),
                                default=0,
                            ),
                            "final": True,
                            "captured_at": int(time.time()),
                        }
                        self.persist_current_study(refresh_library=True)
                        self.refresh_study_tree()
                    self.show_study_analysis()
                self.study_position_job = None
                self.study_position_queue = None
                self.study_position_timer.stop()
                self.study_analyse_button.setEnabled(True)
                return
            elif message.get("type") == "position_error":
                self.study_detail.setText(
                    "Study analysis failed: " + str(message.get("message", "unknown error"))
                )
                self.study_position_job = None
                self.study_position_queue = None
                self.study_position_timer.stop()
                self.study_analyse_button.setEnabled(True)
                return

    def show_study_analysis(self):
        if self.current_study is None:
            return
        node = self.current_study["nodes"].get(str(self.study_node_id), {})
        lines = self.study_position_lines or (node.get("analysis") or {}).get("lines") or []
        rendered = []
        side = str(node.get("fen", " w ").split()[1])
        for line in lines[:self.multipv]:
            cp, mate = score_for_pov(
                line.get("cp"), line.get("mate"), self.eval_pov, side
            )
            score = format_line_score(line, cp, mate, self.eval_pov, side)
            try:
                continuation = san_rules.numbered_line_to_san(
                    node.get("fen", ""), line.get("pv") or [], self.display_pv_limit()
                )
            except (ValueError, AttributeError):
                continuation = " ".join(line.get("pv") or [])
            rendered.append(
                f"#{line.get('rank', len(rendered) + 1)} {score} · "
                f"d{line.get('depth', 0)} · {continuation or 'terminal position'}"
            )
        count = len(self.current_study.get("nodes", {}))
        heading = f"{count} saved positions · move pieces to add a variation"
        self.study_detail.setText(heading + ("\n" + "\n".join(rendered) if rendered else ""))

    def export_study_pgn(self):
        if self.current_study is None or study_rules is None:
            return
        safe = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in self.current_study.get("title", "study")
        ).strip("_") or "study"
        path, _selected = QFileDialog.getSaveFileName(
            self, "Export annotated study", safe + ".pgn", "PGN files (*.pgn)"
        )
        if not path:
            return
        if not path.lower().endswith(".pgn"):
            path += ".pgn"
        try:
            with open(path, "w", encoding="utf-8") as output:
                output.write(study_rules.annotated_pgn(self.current_study))
            self.set_status("Annotated study PGN exported", "info", linger=True)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Could not export study", str(error))

    def delete_current_study(self):
        if self.review_store is None or not self.current_study_id:
            return
        answer = QMessageBox.question(
            self, "Delete saved study?",
            "Delete this variation tree, its notes, and evaluation snapshots?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if self.review_store.delete_study(self.current_study_id):
            self.cancel_study_analysis()
            self.current_study = None
            self.current_study_id = None
            self.study_node_id = None
            self.study_tree.clear()
            self.study_board.set_interactive(False)
            self.study_detail.setText("Study deleted. Create or save another one locally.")
            studies = self.populate_study_library()
            if studies:
                self.load_study(studies[0])

    # -- Local Game Review ----------------------------------------------

    def toggle_review(self):
        if self.stack.currentWidget() is self.review_page:
            self.close_review()
        else:
            self.open_review()

    def open_review(self):
        self.stack.setCurrentWidget(self.review_page)
        self.populate_review_library(self.review_game_id)
        if not self.review_record:
            self.review_summary.setText(
                "Import a PGN game or a complete six-field FEN. "
                "Everything is parsed, stored, and analysed locally."
            )
            return
        if self.review_positions:
            self.select_review_ply(len(self.review_positions) - 1)
        else:
            try:
                grid, side = fen_to_grid(self.review_record["initial_fen"])
                self.review_board.set_position(
                    grid, side, self.flip, self.review_record["initial_fen"]
                )
            except ValueError:
                pass

    def close_review(self):
        self.leave_review_explore()
        if self.local_mode:
            self.close()
            return
        self.stack.setCurrentWidget(self.analysis_page)

    @staticmethod
    def build_record_positions(initial_fen, moves):
        board = san_rules.Board(initial_fen)
        positions = [board.fen()]
        for move in moves:
            board = board.apply_uci(move)
            positions.append(board.fen())
        return positions

    def apply_game_record(self, state):
        initial_fen = str(state.get("initial_fen", ""))
        raw_moves = state.get("moves", state.get("uci_moves", ""))
        moves = (
            [str(move) for move in raw_moves]
            if isinstance(raw_moves, (list, tuple))
            else [move for move in str(raw_moves).split("|") if move]
        )
        if not initial_fen or review_rules is None:
            return False
        try:
            initial_fen = (
                pgn_import.canonical_fen(initial_fen)
                if pgn_import is not None else san_rules.Board(initial_fen).fen()
            )
            positions = self.build_record_positions(initial_fen, moves)
        except (ValueError, AttributeError):
            return False
        fingerprint = (initial_fen, tuple(moves))
        old = self.review_record
        raw_metadata = state.get("metadata")
        metadata = {
            str(key): str(value)
            for key, value in raw_metadata.items()
        } if isinstance(raw_metadata, dict) else {}
        label = str(state.get("label", "")).strip() or self.session_label or (
            f"Local game · {len(moves)} plies"
        )
        if old and old.get("fingerprint") == fingerprint:
            old.update({
                "result": str(state.get("result", old.get("result", "*"))),
                "label": label or old.get("label", "Local game"),
                "metadata": metadata or old.get("metadata", {}),
                "imported": bool(state.get("imported", old.get("imported", False))),
            })
            return True
        self.review_record = {
            "initial_fen": initial_fen,
            "moves": moves,
            "result": str(state.get("result", "*")),
            "label": label,
            "metadata": metadata,
            "imported": bool(state.get("imported", False)),
            "fingerprint": fingerprint,
        }
        self.review_results = []
        self.review_positions = positions
        self.review_position_analyses = []
        self.review_game_id = None
        self.review_settings_used = None
        self.leave_review_explore()
        self.review_moves.clear()
        self.review_graph.set_values([])
        self.review_export_button.setEnabled(True)
        self.review_button.setEnabled(True)
        self.review_start_button.setEnabled(True)
        self.review_explore_button.setEnabled(bool(positions))
        self.review_selected_ply = len(positions) - 1
        self.select_review_ply(self.review_selected_ply)
        source = "Imported local record" if state.get("imported") else "Verified local record"
        self.review_summary.setText(
            f"{source} · {len(moves)} plies · "
            f"result {self.review_record['result']}. "
            "Run review when you want; live analysis remains independent."
        )
        return True

    def apply_imported_record(self, record):
        self.cancel_game_review()
        self.leave_review_explore()
        state = dict(record)
        state["imported"] = True
        if not self.apply_game_record(state):
            raise ValueError("The imported game could not be replayed")
        save_error = None
        if self.review_store is not None:
            try:
                self.review_game_id = self.review_store.save_record(self.review_record)
                self.populate_review_library(self.review_game_id)
            except (OSError, ValueError) as error:
                save_error = error
        self.open_review()
        if save_error is None:
            self.set_status(
                f"Imported {len(self.review_record['moves'])} plies locally",
                "info", linger=True,
            )
        else:
            self.set_status(
                f"Imported for this window, but could not save it: {save_error}",
                "warn", linger=False,
            )

    def import_review_pgn(self):
        if pgn_import is None:
            self.set_status("PGN importer is not installed", "warn", linger=False)
            return
        path, _selected = QFileDialog.getOpenFileName(
            self, "Import one PGN game", "", "PGN files (*.pgn);;Text files (*.txt);;All files (*)"
        )
        if not path:
            return
        try:
            self.apply_imported_record(pgn_import.parse_pgn_file(path))
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Could not import PGN", str(error))

    def import_review_fen(self):
        if pgn_import is None:
            self.set_status("FEN importer is not installed", "warn", linger=False)
            return
        text, accepted = QInputDialog.getMultiLineText(
            self, "Import FEN position", "Paste a complete six-field FEN:"
        )
        if not accepted:
            return
        try:
            fen = pgn_import.canonical_fen(text)
            side = "White" if fen.split()[1] == "w" else "Black"
            self.apply_imported_record({
                "initial_fen": fen,
                "moves": [],
                "result": "*",
                "label": f"Imported position · {side} to move",
                "metadata": {"Event": "Imported FEN"},
            })
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Could not import FEN", str(error))

    def restore_review_archive(self):
        if self.review_store is not None:
            games = self.review_store.list_games()
            if games:
                self.load_library_game(games[0])
                self.populate_review_library(games[0].get("id"))
                return
        raw = self.settings.value("review/latest", "")
        if not isinstance(raw, str) or not raw:
            return
        try:
            saved = json.loads(raw)
            record = saved["record"]
            self.apply_game_record({
                "initial_fen": record["initial_fen"],
                "uci_moves": "|".join(record["moves"]),
                "result": record.get("result", "*"),
                "label": record.get("label", "Local game"),
                "metadata": record.get("metadata") or {},
                "imported": record.get("imported", False),
            })
            self.finish_game_review({
                "reviews": saved["reviews"],
                "positions": saved["positions"],
                "position_analyses": saved.get("position_analyses") or [],
            }, persist=False)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.settings.remove("review/latest")
        self.populate_review_library(self.review_game_id)

    def populate_review_library(self, selected_id=None):
        if not hasattr(self, "review_library_combo"):
            return
        self.applying_settings = True
        self.review_library_combo.clear()
        games = self.review_store.list_games() if self.review_store is not None else []
        if games:
            for game in games:
                self.review_library_combo.addItem(str(game.get("label", "Local game")), game.get("id"))
        else:
            self.review_library_combo.addItem("No saved reviews", None)
        if selected_id:
            self.select_data(self.review_library_combo, selected_id, 0)
        self.review_library_combo.setEnabled(bool(games))
        self.review_delete_button.setEnabled(bool(games))
        self.applying_settings = False

    def load_library_game(self, game):
        reviews = game.get("reviews") or {}
        self.apply_game_record({
            "initial_fen": game["initial_fen"],
            "uci_moves": "|".join(game["moves"]),
            "result": game.get("result", "*"),
            "label": game.get("label", "Local game"),
            "metadata": game.get("metadata") or {},
            "imported": game.get("imported", False),
        })
        self.review_game_id = game.get("id")
        if not reviews:
            if self.review_positions:
                self.select_review_ply(len(self.review_positions) - 1)
            return
        cached = max(reviews.values(), key=lambda item: int(item.get("created_at", 0)))
        settings = cached.get("settings") or {}
        self.review_settings_used = settings
        self.finish_game_review({
            "reviews": cached.get("results") or [],
            "positions": cached.get("positions") or [],
            "position_analyses": cached.get("position_analyses") or [],
        }, persist=False)

    def load_library_selection(self):
        if self.applying_settings or self.review_store is None:
            return
        identifier = self.review_library_combo.currentData()
        game = self.review_store.find(identifier)
        if game is not None:
            self.cancel_game_review()
            self.leave_review_explore()
            self.load_library_game(game)

    def delete_library_selection(self):
        if self.review_store is None:
            return
        identifier = self.review_library_combo.currentData()
        if not identifier:
            return
        answer = QMessageBox.question(
            self, "Delete local review?",
            "Delete this game and all of its cached review settings?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if identifier and self.review_store.delete(identifier):
            games = self.review_store.list_games()
            if games:
                self.load_library_game(games[0])
                self.populate_review_library(games[0].get("id"))
            else:
                self.review_record = None
                self.review_results = []
                self.review_positions = []
                self.review_position_analyses = []
                self.review_moves.clear()
                self.review_graph.set_values([])
                self.review_button.setEnabled(True)
                self.review_export_button.setEnabled(False)
                self.review_explore_button.setEnabled(False)
                self.review_summary.setText(
                    "Import a PGN game or a complete six-field FEN."
                )
                self.populate_review_library()

    def persist_review_archive(self):
        if not self.review_record or not self.review_positions:
            return
        payload = json.dumps({
            "record": {
                "initial_fen": self.review_record["initial_fen"],
                "moves": self.review_record["moves"],
                "result": self.review_record.get("result", "*"),
                "label": self.review_record.get("label", "Local game"),
                "metadata": self.review_record.get("metadata") or {},
                "imported": self.review_record.get("imported", False),
            },
            "reviews": self.review_results,
            "positions": self.review_positions,
            "position_analyses": self.review_position_analyses,
        }, separators=(",", ":"))
        if len(payload.encode("utf-8")) <= 2_000_000:
            self.settings.setValue("review/latest", payload)
            self.settings.sync()

        if self.review_store is not None and self.review_settings_used is not None:
            try:
                self.review_game_id, _key = self.review_store.save_review(
                    self.review_record, self.review_settings_used,
                    self.review_results, self.review_positions,
                    self.review_position_analyses,
                )
                self.populate_review_library(self.review_game_id)
            except (OSError, ValueError) as error:
                self.set_status(f"Could not save review library: {error}", "warn", linger=False)

    def current_review_settings(self):
        return {
            "time_ms": self.review_time_ms,
            "lines": self.review_lines,
            "threads": self.threads,
            "thresholds": {
                "strict": (10, 25, 60, 120, 240),
                "standard": (15, 35, 80, 160, 300),
                "lenient": (20, 50, 110, 220, 400),
            }[self.review_sensitivity],
        }

    def start_game_review(self):
        if not self.review_record or self.review_job is not None or review_rules is None:
            return
        self.review_results = []
        self.review_position_analyses = []
        try:
            self.review_positions = self.build_record_positions(
                self.review_record["initial_fen"], self.review_record["moves"]
            )
        except (ValueError, AttributeError):
            self.review_positions = []
        self.review_moves.clear()
        settings = self.current_review_settings()
        self.review_settings_used = settings
        if self.review_store is not None and study_store is not None:
            identifier = study_store.game_id(
                self.review_record["initial_fen"], self.review_record["moves"]
            )
            cached = self.review_store.cached_review(
                identifier, study_store.settings_key(settings)
            )
            if cached is not None:
                self.review_game_id = identifier
                self.finish_game_review({
                    "reviews": cached.get("results") or [],
                    "positions": cached.get("positions") or [],
                    "position_analyses": cached.get("position_analyses") or [],
                }, persist=False)
                self.review_summary.setText(
                    self.review_summary.text() + " · loaded from local cache"
                )
                return
        self.review_job, self.review_queue = review_rules.start_review(
            self.review_record["initial_fen"],
            self.review_record["moves"], settings,
        )
        total = len(self.review_record["moves"]) + 1
        self.review_progress.setRange(0, total)
        self.review_progress.setValue(0)
        self.review_start_button.setEnabled(False)
        self.review_cancel_button.setEnabled(True)
        count = len(self.review_record["moves"])
        self.review_summary.setText(
            f"Reviewing {count} plies locally\u2026" if count
            else "Analysing the imported position locally\u2026"
        )
        self.review_timer.start()

    def cancel_game_review(self):
        if self.review_job is not None:
            self.review_job.cancel()

    def poll_review(self):
        if self.review_queue is None:
            self.review_timer.stop()
            return
        while True:
            try:
                message = self.review_queue.get_nowait()
            except queue.Empty:
                break
            kind = message.get("type")
            if kind == "progress":
                self.review_progress.setMaximum(int(message.get("total", 1)))
                self.review_progress.setValue(int(message.get("done", 0)))
            elif kind == "complete":
                self.finish_game_review(message)
            elif kind in {"cancelled", "error"}:
                text = "Review cancelled" if kind == "cancelled" else (
                    "Review failed: " + str(message.get("message", "unknown error"))
                )
                self.review_summary.setText(text)
                self.finish_review_job()
        if self.review_job is not None and not self.review_job.is_alive():
            self.finish_review_job()

    def finish_review_job(self):
        self.review_timer.stop()
        self.review_job = None
        self.review_queue = None
        self.review_start_button.setEnabled(bool(self.review_record))
        self.review_cancel_button.setEnabled(False)

    def finish_game_review(self, message, persist=True):
        self.review_results = list(message.get("reviews") or [])
        self.review_positions = list(message.get("positions") or [])
        self.review_position_analyses = list(message.get("position_analyses") or [])
        self.review_selected_ply = -1
        self.leave_review_explore()
        self.refresh_review_timeline()

        graph_values = []
        if self.review_position_analyses:
            for lines in self.review_position_analyses:
                first_score = lines[0] if isinstance(lines, list) and lines else {"cp": 0}
                graph_values.append(review_rules.score_value(**first_score))
        elif self.review_results:
            first_lines = self.review_results[0].get("lines") or []
            first_score = first_lines[0] if first_lines else {"cp": 0}
            graph_values.append(review_rules.score_value(**first_score))
            graph_values.extend(
                review_rules.score_value(**(item.get("eval_score") or {"cp": 0}))
                for item in self.review_results
            )
        self.review_graph.set_values(graph_values)

        white = [item["loss"] for item in self.review_results if int(item["ply"]) % 2]
        black = [item["loss"] for item in self.review_results if not int(item["ply"]) % 2]
        turning_points = sum(
            item["classification"] in {"Mistake", "Blunder"}
            for item in self.review_results
        )
        white_average = sum(white) / max(1, len(white)) / 100.0
        black_average = sum(black) / max(1, len(black)) / 100.0
        white_errors = sum(
            item["classification"] in {"Inaccuracy", "Mistake", "Blunder"}
            for item in self.review_results if int(item["ply"]) % 2
        )
        black_errors = sum(
            item["classification"] in {"Inaccuracy", "Mistake", "Blunder"}
            for item in self.review_results if not int(item["ply"]) % 2
        )
        if self.review_results:
            self.review_summary.setText(
                f"{len(self.review_results)} plies · {turning_points} turning point"
                f"{'s' if turning_points != 1 else ''} · "
                f"White avg loss {white_average:.2f} ({white_errors} errors) · "
                f"Black {black_average:.2f} ({black_errors} errors)."
            )
        else:
            score = ""
            if self.review_position_analyses and self.review_position_analyses[0]:
                score = " · evaluation " + review_rules.format_eval(
                    self.review_position_analyses[0][0]
                )
            self.review_summary.setText(
                "Position analysis complete" + score
                + ". Use Explore here to test continuations."
            )
        self.review_export_button.setEnabled(True)
        self.review_explore_button.setEnabled(bool(self.review_positions))
        if self.review_positions:
            self.select_review_ply(len(self.review_positions) - 1)
        if persist:
            self.persist_review_archive()
        self.finish_review_job()

    @staticmethod
    def review_item_visible(item, mode):
        classification = item.get("classification")
        if mode == "turning":
            return classification in {"Mistake", "Blunder"}
        if mode == "errors":
            return classification in {"Inaccuracy", "Mistake", "Blunder"}
        if mode == "major":
            return classification == "Blunder"
        if mode == "forcing":
            san_text = str(item.get("san", ""))
            return "x" in san_text or "+" in san_text or "#" in san_text
        return True

    def refresh_review_timeline(self):
        colors = {
            "Best": "#7fc97f", "Excellent": "#8dd3c7", "Good": "#ccebc5",
            "Inaccuracy": "#f4d35e", "Mistake": "#ef9f47", "Blunder": "#e05a5a",
        }
        self.review_moves.clear()
        self.review_visible_rows = []
        mode = str(self.review_filter.currentData() or "all")
        for index, item in enumerate(self.review_results):
            if not self.review_item_visible(item, mode):
                continue
            self.review_visible_rows.append(index)
            move_no = (int(item["ply"]) + 1) // 2
            prefix = f"{move_no}." if int(item["ply"]) % 2 else f"{move_no}..."
            row = QListWidgetItem(
                f"{prefix} {item['san']:<8}  {item['classification']:<10} "
                f"{item['loss'] / 100:.2f}"
            )
            row.setForeground(QColor(colors.get(item["classification"], "#e8ebf0")))
            self.review_moves.addItem(row)

    def select_filtered_review_move(self, visible_row):
        if 0 <= visible_row < len(self.review_visible_rows):
            self.select_review_ply(self.review_visible_rows[visible_row] + 1)

    def select_review_ply(self, ply):
        if self.review_mode == "explore":
            return
        if ply < 0 or ply >= len(self.review_positions) or not self.review_positions:
            return
        self.review_selected_ply = ply
        self.review_graph.set_current(ply)
        if ply == 0:
            fen = self.review_positions[0]
            try:
                grid, side = fen_to_grid(fen)
            except ValueError:
                return
            self.review_board.set_position(grid, side, self.flip, fen)
            self.review_board.set_moves("", "", "")
            prefix = "Imported position" if not self.review_record.get("moves") else "Starting position"
            detail = prefix + " · choose Explore here to analyse your own continuation."
            if self.review_position_analyses and self.review_position_analyses[0]:
                rendered = []
                for line in self.review_position_analyses[0][:self.review_lines]:
                    try:
                        continuation = san_rules.numbered_line_to_san(
                            fen, line.get("pv") or [], self.display_pv_limit()
                        )
                    except (ValueError, AttributeError):
                        continuation = " ".join(line.get("pv") or [])
                    rendered.append(
                        f"#{line.get('rank', len(rendered) + 1)} "
                        f"{review_rules.format_eval(line)} · d{line.get('depth', 0)}"
                        + (f" · {continuation}" if continuation else "")
                    )
                detail += "\n" + "\n".join(rendered)
            self.review_detail.setText(detail)
        elif ply <= len(self.review_results):
            self.select_review_move(ply - 1)
        else:
            fen = self.review_positions[ply]
            try:
                grid, side = fen_to_grid(fen)
                before = san_rules.Board(self.review_positions[ply - 1])
                move = self.review_record["moves"][ply - 1]
                notation = before.san(move)
            except (ValueError, AttributeError, IndexError):
                return
            self.review_board.set_position(grid, side, self.flip, fen)
            self.review_board.set_moves("", "", move)
            self.review_detail.setText(
                f"Move {ply}: {notation} · not reviewed yet. "
                "Run local review for classifications and continuations."
            )
        actual = ply - 1
        if actual in self.review_visible_rows:
            self.review_moves.blockSignals(True)
            self.review_moves.setCurrentRow(self.review_visible_rows.index(actual))
            self.review_moves.blockSignals(False)
        self.review_back_button.setEnabled(ply > 0)
        self.review_forward_button.setEnabled(ply < len(self.review_positions) - 1)

    def review_back(self):
        if self.review_mode == "explore":
            self.review_explore_undo()
        else:
            self.select_review_ply(max(0, self.review_selected_ply - 1))

    def review_forward(self):
        if self.review_mode == "game":
            self.select_review_ply(
                min(len(self.review_positions) - 1, self.review_selected_ply + 1)
            )

    def review_final(self):
        if self.review_mode == "explore":
            self.leave_review_explore()
        if self.review_positions:
            self.select_review_ply(len(self.review_positions) - 1)

    def select_review_move(self, row):
        if row < 0 or row >= len(self.review_results):
            return
        item = self.review_results[row]
        try:
            grid, side = fen_to_grid(item["fen_after"])
        except ValueError:
            return
        self.review_board.set_position(grid, side, self.flip, item["fen_after"])
        self.review_board.set_moves(item.get("best", ""), "", item.get("uci", ""))
        lines = item.get("lines") or []
        continuations = []
        for line in lines[:self.review_lines]:
            try:
                text = san_rules.numbered_line_to_san(
                    item["fen_before"], line.get("pv") or [], self.pv_display_length or 128
                )
            except (ValueError, AttributeError):
                text = " ".join(line.get("pv") or [])
            if text:
                continuations.append(f"#{line.get('rank', len(continuations)+1)} {text}")
        best_text = ""
        if item.get("best") and item["best"] != item["uci"]:
            best_text = "Stockfish preferred " + (
                name_move(item["fen_before"], [], item["best"]) or item["best"]
            ) + ". "
        detail = (
            f"{item['classification']} · {item['loss'] / 100:.2f} evaluation points lost · "
            f"position {item['eval']} · depth {item['depth']}\n"
            f"{best_text}The lines below show Stockfish's current continuation, not a guarantee."
        )
        if continuations:
            detail += "\n" + "\n".join(continuations)
        if explanation_rules is not None and lines:
            explanation_lines = []
            for line in lines:
                pv = line.get("pv") or []
                explanation_lines.append({
                    **line,
                    "move": pv[0] if pv else "",
                    "pv": " ".join(pv),
                    "bound": "exact",
                    "final": True,
                })
            explanation = explanation_rules.build_explanation(
                item["fen_before"], explanation_lines, selected_rank=1,
                display_plies=self.display_pv_limit(), level=self.explanation_level,
                eval_pov=self.eval_pov,
            )
            facts = explanation.get("facts") or []
            if facts:
                detail += "\nWhat the line shows: " + " ".join(facts[:3])
        self.review_detail.setText(detail)

    def toggle_review_explore(self):
        if self.review_mode == "explore":
            self.leave_review_explore()
            self.select_review_ply(self.review_selected_ply)
            return
        if not self.review_positions or self.review_selected_ply < 0:
            return
        self.review_mode = "explore"
        self.review_branch_root = self.review_positions[self.review_selected_ply]
        self.review_branch = []
        self.review_explore_button.setText("Return to game")
        self.review_undo_button.show()
        self.review_undo_button.setEnabled(False)
        self.review_filter.setEnabled(False)
        self.review_moves.setEnabled(False)
        self.review_graph.setEnabled(False)
        self.review_board.set_interactive(True)
        self.analyse_review_branch()

    def leave_review_explore(self):
        if self.review_position_job is not None:
            self.review_position_job.cancel()
        self.review_position_timer.stop()
        self.review_position_job = None
        self.review_position_queue = None
        self.review_mode = "game"
        self.review_branch = []
        self.review_branch_root = ""
        if hasattr(self, "review_explore_button"):
            self.review_explore_button.setText("Explore here")
            self.review_undo_button.hide()
            self.review_filter.setEnabled(True)
            self.review_moves.setEnabled(True)
            self.review_graph.setEnabled(True)
            self.review_board.set_interactive(False)

    def handle_review_move_request(self, choices):
        if self.review_mode != "explore" or not choices:
            return
        if len(choices) == 1:
            self.apply_review_explore_move(choices[0])
            return
        menu = QMenu(self)
        labels = {"q": "Queen", "r": "Rook", "b": "Bishop", "n": "Knight"}
        for move in choices:
            action = menu.addAction(labels.get(move[-1], move[-1].upper()))
            action.triggered.connect(
                lambda _checked=False, selected=move: self.apply_review_explore_move(selected)
            )
        menu.exec(self.review_board.mapToGlobal(self.review_board.rect().center()))

    def review_branch_board(self):
        board = san_rules.Board(self.review_branch_root)
        for move in self.review_branch:
            board = board.apply_uci(move)
        return board

    def apply_review_explore_move(self, move):
        if self.review_mode != "explore" or san_rules is None:
            return
        try:
            board = self.review_branch_board()
            if move not in board.legal_uci_moves():
                raise ValueError("Illegal move")
            self.review_branch.append(move)
            board = board.apply_uci(move)
            grid, side = fen_to_grid(board.fen())
        except (ValueError, AttributeError):
            self.set_status("Illegal review move", "warn", linger=True)
            return
        self.review_board.set_position(grid, side, self.flip, board.fen())
        self.review_board.set_moves("", "", move)
        self.review_undo_button.setEnabled(True)
        self.analyse_review_branch()

    def review_explore_undo(self):
        if self.review_mode != "explore" or not self.review_branch:
            return
        self.review_branch.pop()
        try:
            board = self.review_branch_board()
            grid, side = fen_to_grid(board.fen())
        except (ValueError, AttributeError):
            return
        last = self.review_branch[-1] if self.review_branch else ""
        self.review_board.set_position(grid, side, self.flip, board.fen())
        self.review_board.set_moves("", "", last)
        self.review_undo_button.setEnabled(bool(self.review_branch))
        self.analyse_review_branch()

    def analyse_review_branch(self):
        if self.review_mode != "explore" or review_rules is None:
            return
        if self.review_position_job is not None:
            self.review_position_job.cancel()
        try:
            fen = self.review_branch_board().fen()
        except (ValueError, AttributeError):
            return
        self.review_position_generation += 1
        settings = self.current_review_settings()
        self.review_position_job, self.review_position_queue = (
            review_rules.start_position_analysis(
                fen, settings, self.review_position_generation
            )
        )
        self.review_detail.setText(
            f"Exploring from reviewed ply {self.review_selected_ply} · "
            f"{len(self.review_branch)} branch moves · analysing locally\u2026"
        )
        self.review_position_timer.start()

    def poll_review_position(self):
        if self.review_position_queue is None:
            self.review_position_timer.stop()
            return
        while True:
            try:
                message = self.review_position_queue.get_nowait()
            except queue.Empty:
                break
            if (
                message.get("type") == "position_complete"
                and int(message.get("generation", -1)) == self.review_position_generation
                and self.review_mode == "explore"
            ):
                self.review_position_lines = list(message.get("lines") or [])
                self.show_review_branch_analysis()
                self.review_position_job = None
                self.review_position_queue = None
                self.review_position_timer.stop()
            elif message.get("type") == "position_error":
                self.review_detail.setText(
                    "Review exploration failed: " + str(message.get("message", "unknown error"))
                )
                self.review_position_job = None
                self.review_position_queue = None
                self.review_position_timer.stop()

    def show_review_branch_analysis(self):
        try:
            fen = self.review_branch_board().fen()
        except (ValueError, AttributeError):
            return
        lines = self.review_position_lines
        best = (lines[0].get("pv") or [""])[0] if lines else ""
        last = self.review_branch[-1] if self.review_branch else ""
        self.review_board.set_moves(best, "", last)
        rendered = []
        for line in lines[:self.review_lines]:
            try:
                continuation = san_rules.numbered_line_to_san(
                    fen, line.get("pv") or [], self.display_pv_limit()
                )
            except (ValueError, AttributeError):
                continuation = " ".join(line.get("pv") or [])
            rendered.append(
                f"#{line.get('rank', len(rendered)+1)} "
                f"{review_rules.format_eval(line)} · d{line.get('depth', 0)} · {continuation}"
            )
        self.review_detail.setText(
            f"Local branch · {len(self.review_branch)} moves from reviewed ply "
            f"{self.review_selected_ply}\n" + ("\n".join(rendered) or "Terminal position")
        )

    def export_review_pgn(self):
        if not self.review_record or review_rules is None:
            return
        path, _selected = QFileDialog.getSaveFileName(
            self, "Export annotated review", "ChessListener-review.pgn", "PGN files (*.pgn)"
        )
        if not path:
            return
        text = review_rules.annotated_pgn(
            self.review_record["initial_fen"], self.review_record["moves"],
            self.review_results, self.review_record.get("result", "*"),
            self.review_record.get("metadata") or {},
        )
        try:
            with open(path, "w", encoding="utf-8") as output:
                output.write(text)
        except OSError as error:
            self.set_status(f"Could not export PGN: {error}", "warn", linger=False)
            return
        self.set_status("Annotated PGN exported", "info", linger=True)

    # -- Analysis Lab ----------------------------------------------------

    def selected_candidate(self):
        if 0 <= self.selected_line < len(self.lines):
            line = self.lines[self.selected_line]
            return line if isinstance(line, dict) else None

        return None

    def display_pv_limit(self):
        return 128 if self.pv_display_length == 0 else self.pv_display_length

    def select_candidate(self, index):
        if not 0 <= index < len(self.lines):
            return

        if self.preview_step:
            self.cancel_preview(restore=True)

        self.selected_line = index
        self.preview_moves = pv_moves(self.lines[index])
        self.preview_step = 0
        self.preview_root_fen = self.fen
        self.preview_root_grid = list(self.grid)
        self.preview_root_side = self.side_to_move
        self.preview_root_last = self.last_move
        self.preview_root_last_san = self.last_san
        self.refresh_candidate_rows()
        self.refresh_explanation()
        self.dirty = True

    def activate_candidate(self, index):
        self.select_candidate(index)
        self.preview_forward()

    def refresh_candidate_rows(self):
        if not hasattr(self, "candidate_rows"):
            return

        root_fen = self.analysis_fen or self.fen
        limit = self.display_pv_limit()
        try:
            _root_grid, score_side = fen_to_grid(root_fen)
        except ValueError:
            score_side = self.side_to_move

        for index, row in enumerate(self.candidate_rows):
            if index >= len(self.lines) or index >= self.multipv:
                row.hide()
                continue

            line = self.lines[index]
            if not isinstance(line, dict):
                row.hide()
                continue

            moves = pv_moves(line)
            move = name_move(root_fen, self.grid, line.get("move", ""))
            cp, mate = score_for_pov(
                line.get("cp"), line.get("mate"), self.eval_pov,
                score_side,
            )
            pv = ""
            if san_rules is not None and root_fen:
                try:
                    pv = san_rules.numbered_line_to_san(root_fen, moves, limit)
                except (ValueError, AttributeError):
                    pv = ""

            expanded = self.line_expansion == "all" or index == self.selected_line
            row.set_selected(index == self.selected_line)
            row.set_line(
                move or line.get("move", ""),
                format_line_score(line, cp, mate, self.eval_pov, score_side),
                int(line.get("depth") or self.depth or 0),
                pv,
                expanded,
            )
            row.show()

        self.candidate_scroll.setVisible(bool(self.lines) and not self.compact)
        selected = self.selected_candidate()
        if not self.preview_step:
            self.preview_moves = pv_moves(selected) if selected else []
        self.update_analysis_mode_ui()

    def refresh_explanation(self):
        if not hasattr(self, "explanation_label"):
            return

        if (
            self.explanation_level == "off"
            or not self.lines
            or explanation_rules is None
        ):
            self.explanation_label.clear()
            self.explanation_label.hide()
            return

        result = explanation_rules.build_explanation(
            self.analysis_fen or self.fen,
            self.lines,
            selected_rank=self.selected_line + 1,
            display_plies=self.display_pv_limit(),
            level=self.explanation_level,
            eval_pov=self.eval_pov,
        )
        selected = self.selected_candidate() or {}
        try:
            _root_grid, root_side = fen_to_grid(self.analysis_fen or self.fen)
        except ValueError:
            root_side = self.side_to_move
        shown_cp, shown_mate = score_for_pov(
            selected.get("cp"), selected.get("mate"), self.eval_pov, root_side
        )
        score_text = format_line_score(
            selected, shown_cp, shown_mate, self.eval_pov, root_side
        )
        status_text = format_line_status(selected, self.eval_pov, root_side)
        pieces = [result.get("heading", ""), score_text or result.get("score_text", "")]
        if result.get("comparison_text"):
            pieces.append(result["comparison_text"])
        if result.get("line_text"):
            pieces.append(result["line_text"])
        if self.explanation_level == "compact":
            pieces.extend((result.get("facts") or [])[:2])
        elif self.explanation_level == "detailed":
            pieces.extend(result.get("facts") or [])
            if status_text:
                pieces.append(status_text)
        text = "\n".join(piece for piece in pieces if piece)
        self.explanation_label.setText(text)
        self.explanation_label.setVisible(bool(text) and not self.compact)

    def cancel_preview(self, restore=False):
        if restore and self.preview_step and self.preview_root_fen:
            try:
                grid, side = fen_to_grid(self.preview_root_fen)
            except ValueError:
                pass
            else:
                self.fen = self.preview_root_fen
                self.grid = grid
                self.side_to_move = side
                self.last_move = self.preview_root_last
                self.last_san = self.preview_root_last_san

        self.preview_step = 0
        self.preview_moves = []
        self.preview_root_fen = ""
        self.preview_root_grid = None
        self.preview_root_last_san = ""
        self.update_analysis_mode_ui()
        self.dirty = True

    def apply_preview_step(self):
        if san_rules is None or not self.preview_root_fen:
            return

        try:
            board = san_rules.Board(self.preview_root_fen)
            last = self.preview_root_last
            last_san = self.preview_root_last_san
            for move in self.preview_moves[: self.preview_step]:
                last_san = board.san(move)
                board = board.apply_uci(move)
                last = move
            fen = board.fen()
            grid, side = fen_to_grid(fen)
        except (ValueError, AttributeError):
            self.cancel_preview(restore=True)
            return

        self.fen = fen
        self.grid = grid
        self.side_to_move = side
        self.last_move = last
        self.last_san = last_san
        self.update_analysis_mode_ui()
        self.dirty = True

    def preview_forward(self):
        if self.mode != "live":
            return

        selected = self.selected_candidate()
        moves = pv_moves(selected) if selected else []

        if not moves:
            return

        if not self.preview_root_fen:
            self.preview_root_fen = self.fen
            self.preview_root_grid = list(self.grid)
            self.preview_root_side = self.side_to_move
            self.preview_root_last = self.last_move
            self.preview_root_last_san = self.last_san
            self.preview_moves = moves

        if self.preview_step < min(len(self.preview_moves), self.display_pv_limit()):
            self.preview_step += 1
            self.apply_preview_step()

    def preview_back(self):
        if self.mode != "live" or self.preview_step <= 0:
            return

        self.preview_step -= 1
        self.apply_preview_step()

    def start_explore(self):
        if not self.session_active or not self.session_id or self.explore_pending:
            self.set_status("No active analysis session", "warn", linger=False)
            return

        snapshot = self.live_snapshot
        base_fen = snapshot.get("fen", "") if snapshot else self.fen
        if not base_fen:
            self.set_status("No live position to explore", "warn", linger=False)
            return

        path = []
        if self.preview_step and self.preview_root_fen == base_fen:
            path = self.preview_moves[: self.preview_step]

        payload = base_fen + ("|" + ",".join(path) if path else "")
        self.pending_start_base = base_fen
        self.pending_start_path = list(path)
        self.explore_pending = "start"
        self.explore_button.setEnabled(False)
        self.send_control(f"EXPLORE_START {self.session_id} {payload}")
        self.set_status("Opening Analysis Lab\u2026", "info", linger=False)

    def resume_explore(self):
        if (
            not self.session_active or self.resume_branch_id is None
            or self.resume_node_id is None or self.explore_pending
        ):
            return
        self.explore_pending = "resume"
        self.send_control(
            f"EXPLORE_RESUME {self.session_id} "
            f"{self.resume_branch_id} {self.resume_node_id}"
        )

    def handle_board_hint(self, text):
        if text == "Illegal move":
            self.set_status(text, "warn", linger=True)

    def handle_board_move_request(self, choices):
        if self.mode != "explore" or self.explore_pending or not choices:
            return

        if len(choices) == 1:
            self.send_explore_move(choices[0])
            return

        menu = QMenu(self)
        labels = {"q": "Queen", "r": "Rook", "b": "Bishop", "n": "Knight"}
        for move in choices:
            action = menu.addAction(labels.get(move[-1], move[-1].upper()))
            action.triggered.connect(
                lambda _checked=False, selected=move: self.send_explore_move(selected)
            )
        menu.exec(self.board.mapToGlobal(self.board.rect().center()))

    def send_explore_move(self, move):
        if (
            self.explore_branch_id is None or self.explore_node_id is None
            or self.explore_pending
        ):
            return
        self.explore_pending = "move"
        self.explore_pending_parent = self.explore_node_id
        self.board.set_interactive(False)
        self.send_control(
            f"EXPLORE_MOVE {self.session_id} {self.explore_branch_id} "
            f"{self.explore_node_id} {move}"
        )
        self.set_status("Applying move\u2026", "info", linger=False)

    def send_explore_goto(self, node_id, action="goto"):
        if self.explore_branch_id is None or node_id is None or self.explore_pending:
            return
        self.explore_pending = action
        self.send_control(
            f"EXPLORE_GOTO {self.session_id} {self.explore_branch_id} {node_id}"
        )

    def explore_root(self):
        self.send_explore_goto(self.explore_root_node_id, "goto")

    def explore_undo(self):
        node = self.explore_nodes.get(self.explore_node_id, {})
        self.send_explore_goto(node.get("parent"), "goto")

    def explore_redo(self):
        node = self.explore_nodes.get(self.explore_node_id, {})
        children = node.get("children") or []
        if children:
            self.send_explore_goto(children[-1], "goto")

    def go_live(self):
        if self.mode != "explore" or self.explore_branch_id is None or self.explore_pending:
            return
        self.explore_pending = "live"
        self.send_control(
            f"EXPLORE_LIVE {self.session_id} {self.explore_branch_id}"
        )

    def apply_explore_position(self, state):
        fen = str(state.get("fen", ""))
        if not fen:
            return False
        try:
            grid, side = fen_to_grid(fen)
        except ValueError:
            return False
        self.fen = fen
        self.grid = grid
        self.side_to_move = state.get("stm", side)
        self.flip = bool(state.get("flip", self.flip))
        self.last_move = state.get("last") or ""
        self.last_san = ""
        if self.last_move:
            current = self.explore_nodes.get(self.explore_node_id, {})
            parent = self.explore_nodes.get(current.get("parent"), {})
            before_fen = parent.get("fen", "")
            try:
                before_grid, _before_side = fen_to_grid(before_fen)
            except ValueError:
                before_grid = []
            self.last_san = name_move(before_fen, before_grid, self.last_move)
        self.analysis_fen = fen
        self.clear_evaluation(clear_last=False)
        return True

    def apply_explore(self, state):
        event = str(state.get("event", ""))

        if event == "rejected":
            self.explore_pending = ""
            self.explore_pending_parent = None
            self.pending_start_base = ""
            self.pending_start_path = []
            self.explore_button.setEnabled(True)
            self.board.set_interactive(self.mode == "explore")
            self.set_status(
                str(state.get("text") or state.get("reason") or "Explore request rejected"),
                "warn", linger=False,
            )
            return

        if event == "destroyed":
            try:
                destroyed_branch = int(state.get("branch_id"))
            except (TypeError, ValueError):
                destroyed_branch = None

            if self.explore_pending == "start":
                old_branch = (
                    self.explore_branch_id
                    if self.explore_branch_id is not None
                    else self.resume_branch_id
                )
                if destroyed_branch is not None and destroyed_branch != old_branch:
                    return
                # Starting a replacement branch intentionally destroys the
                # retained old tree first. Keep the in-flight base/path so the
                # following started event can seed Explore-here ancestry.
                self.explore_branch_id = None
                self.explore_node_id = None
                self.explore_root_node_id = None
                self.explore_nodes.clear()
                self.resume_branch_id = None
                self.resume_node_id = None
                self.update_analysis_mode_ui()
                return

            known_branch = (
                self.explore_branch_id
                if self.explore_branch_id is not None
                else self.resume_branch_id
            )
            if (
                destroyed_branch is not None and known_branch is not None
                and destroyed_branch != known_branch
            ):
                return
            was_exploring = self.mode == "explore"
            self.explore_pending = ""
            self.pending_start_base = ""
            self.pending_start_path = []
            self.explore_branch_id = None
            self.explore_node_id = None
            self.explore_root_node_id = None
            self.explore_nodes.clear()
            self.resume_branch_id = None
            self.resume_node_id = None
            if was_exploring:
                self.mode = "live"
                self.restore_live_snapshot(clear_eval=True)
            self.update_analysis_mode_ui()
            return

        if event == "live":
            try:
                self.resume_branch_id = int(
                    state.get("branch_id", self.explore_branch_id)
                )
            except (TypeError, ValueError):
                return
            self.resume_node_id = self.explore_node_id
            self.mode = "live"
            self.explore_pending = ""
            self.live_update_count = 0
            self.restore_live_snapshot(clear_eval=True)
            self.update_analysis_mode_ui()
            return

        if event not in {"started", "selected"}:
            return

        branch_id = state.get("branch_id")
        node_id = state.get("node_id")
        if branch_id is None or node_id is None:
            return

        try:
            branch_id = int(branch_id)
            node_id = int(node_id)
        except (TypeError, ValueError):
            return

        pending = self.explore_pending
        self.explore_branch_id = branch_id
        self.explore_node_id = node_id
        self.resume_branch_id = self.explore_branch_id
        self.resume_node_id = self.explore_node_id
        self.mode = "explore"
        self.explore_pending = ""
        self.explore_button.setEnabled(True)

        if event == "started":
            self.explore_nodes.clear()
            self.explore_root_node_id = 0
            parent = None

            # EXPLORE_START builds path nodes transactionally as 0..N. Seed
            # the local mirror so Explore-here still has working Root/Undo and
            # an intelligible breadcrumb instead of treating N as a new root.
            if self.pending_start_base and san_rules is not None:
                try:
                    board = san_rules.Board(self.pending_start_base)
                    self.explore_nodes[0] = {
                        "parent": None, "children": [],
                        "fen": board.fen(), "last": "",
                    }
                    for index, move in enumerate(self.pending_start_path, start=1):
                        board = board.apply_uci(move)
                        self.explore_nodes[index] = {
                            "parent": index - 1, "children": [],
                            "fen": board.fen(), "last": move,
                        }
                        self.explore_nodes[index - 1]["children"].append(index)
                except (ValueError, AttributeError):
                    self.explore_nodes.clear()

            if self.explore_node_id in self.explore_nodes:
                parent = self.explore_nodes[self.explore_node_id].get("parent")
            elif self.explore_node_id == 0:
                self.explore_nodes[0] = {
                    "parent": None, "children": [],
                    "fen": self.pending_start_base, "last": "",
                }
            self.explore_live_base_revision = self.live_revision
            self.live_update_count = 0
        elif pending == "move":
            parent = self.explore_pending_parent
        else:
            parent = self.explore_nodes.get(self.explore_node_id, {}).get("parent")

        node = self.explore_nodes.setdefault(
            self.explore_node_id,
            {"parent": parent, "children": [], "fen": "", "last": ""},
        )
        if pending == "move" and parent is not None:
            parent_node = self.explore_nodes.setdefault(
                parent, {"parent": None, "children": [], "fen": "", "last": ""}
            )
            if self.explore_node_id not in parent_node["children"]:
                parent_node["children"].append(self.explore_node_id)
            node["parent"] = parent

        node["fen"] = str(state.get("fen", node.get("fen", "")))
        node["last"] = state.get("last") or node.get("last", "")
        self.explore_pending_parent = None
        self.pending_start_base = ""
        self.pending_start_path = []
        self.apply_explore_position(state)
        self.set_status("Analysis Lab", "info", linger=True)
        self.update_analysis_mode_ui()
        self.dirty = True

    def apply_live_update(self, state):
        if self.mode != "explore":
            return
        try:
            grid, side = fen_to_grid(state["fen"])
        except (KeyError, ValueError):
            return
        try:
            revision = int(state.get("live_revision", self.live_revision + 1))
        except (TypeError, ValueError):
            return
        if revision < self.live_revision:
            return
        previous = self.live_snapshot
        metadata_only = bool(
            previous
            and revision == previous.get("revision")
            and state["fen"] == previous.get("fen")
        )
        last = (
            (state.get("last") or previous.get("last", ""))
            if metadata_only else (state.get("last") or "")
        )
        last_san = previous.get("last_san", "") if metadata_only else ""
        if previous and last and not metadata_only:
            last_san = name_move(previous.get("fen", ""), previous.get("grid", []), last)
        self.live_revision = max(self.live_revision, revision)
        self.live_snapshot = {
            "fen": state["fen"], "grid": grid,
            "side": state.get("stm", side), "flip": bool(state.get("flip", self.flip)),
            "last": last, "last_san": last_san,
            "source": str(state.get("source", "")),
            "synchronising": bool(state.get("synchronising", False)),
            "revision": revision,
        }
        self.live_update_count = max(
            self.live_update_count,
            max(0, revision - self.explore_live_base_revision),
        )
        self.update_analysis_mode_ui()
        if (
            self.follow_live == "auto" and self.live_update_count > 0
            and self.mode == "explore" and not self.explore_pending
        ):
            self.go_live()

    def restore_live_snapshot(self, clear_eval=False):
        snapshot = self.live_snapshot
        if not snapshot:
            return
        self.fen = snapshot["fen"]
        self.grid = list(snapshot["grid"])
        self.side_to_move = snapshot["side"]
        self.flip = snapshot["flip"]
        self.last_move = snapshot["last"]
        self.last_san = snapshot.get("last_san", "")
        self.state_source = snapshot.get("source", "")
        self.synchronising = snapshot.get("synchronising", False)
        self.analysis_fen = self.fen
        if clear_eval:
            self.clear_evaluation(clear_last=False)
        self.dirty = True

    def update_breadcrumb(self):
        if self.mode != "explore" or self.explore_node_id is None:
            self.breadcrumb_label.hide()
            return
        path = []
        seen = set()
        node_id = self.explore_node_id
        while node_id is not None and node_id not in seen:
            seen.add(node_id)
            node = self.explore_nodes.get(node_id, {})
            if node.get("last"):
                parent = self.explore_nodes.get(node.get("parent"), {})
                before = parent.get("fen", "")
                path.append(name_move(before, [], node["last"]) or node["last"])
            node_id = node.get("parent")
        path.reverse()
        shown = path[-6:]
        prefix = "Root" + (" › …" if len(path) > 6 else "")
        self.breadcrumb_label.setText(prefix + "".join(f" › {move}" for move in shown))
        self.breadcrumb_label.setVisible(not self.compact)

    def update_analysis_mode_ui(self):
        if not hasattr(self, "live_toolbar"):
            return
        exploring = self.mode == "explore"
        self.board.set_interactive(exploring and not self.explore_pending)
        self.live_toolbar.setVisible(not exploring and not self.compact)
        self.explore_toolbar.setVisible(exploring and not self.compact)
        self.resume_button.setVisible(
            not exploring and self.resume_branch_id is not None and not self.compact
        )
        total = min(len(self.preview_moves), self.display_pv_limit())
        self.preview_label.setText(f"PV {self.preview_step}/{total}")
        self.preview_back_button.setEnabled(self.preview_step > 0)
        self.preview_forward_button.setEnabled(self.preview_step < total)
        self.explore_button.setText("Explore here" if self.preview_step else "Explore")
        live_syncing = bool(
            self.live_snapshot and self.live_snapshot.get("synchronising")
        )
        self.live_update_button.setText(
            "Live syncing" if live_syncing else f"Live +{self.live_update_count}"
        )
        self.live_update_button.setVisible(
            exploring and (self.live_update_count > 0 or live_syncing)
        )
        current = self.explore_nodes.get(self.explore_node_id, {})
        self.undo_button.setEnabled(current.get("parent") is not None and not self.explore_pending)
        self.redo_button.setEnabled(bool(current.get("children")) and not self.explore_pending)
        self.root_button.setEnabled(
            exploring and self.explore_node_id != self.explore_root_node_id
            and not self.explore_pending
        )
        self.go_live_button.setEnabled(exploring and not self.explore_pending)
        self.save_lab_button.setEnabled(
            exploring and self.explore_root_node_id in self.explore_nodes
            and not self.explore_pending and study_rules is not None
        )
        self.update_breadcrumb()
        self.update_source_badge()

    # -- status -----------------------------------------------------------

    def set_status(self, text, kind="info", linger=True):
        self.status_text = text
        self.status_label.setObjectName(
            "statusWarn" if kind == "warn" else "statusInfo"
        )

        # Re-polish rather than assign a new stylesheet: the object-name rules
        # are already loaded, this only re-evaluates which one applies.
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)
        self.status_label.setText(text)

        self.status_timer.stop()

        if text and linger and kind != "warn":
            self.status_timer.start(STATUS_LINGER_MS)

    def clear_status(self):
        fallback = (
            self.session_label if self.session_active
            else "Local review" if self.local_mode else ""
        )
        self.set_status(fallback, "info", linger=False)

    def clear_startup_notice(self):
        """Belt and braces for the notice that used to get stuck.

        The host does send a ready frame, but the notice is a UI concern: any
        evidence that analysis is running is enough to retire it, so a lost or
        late ready message can no longer leave it on screen forever.
        """
        if self.status_text.startswith("Starting"):
            self.clear_status()

    # -- incoming frames --------------------------------------------------

    def handle_message(self, state):
        kind = state.get("type", "")

        if kind == "position":
            self.apply_position(state)
        elif kind == "analysis":
            self.apply_analysis(state)
        elif kind == "ready":
            self.apply_ready(state)
        elif kind == "settings":
            self.apply_settings_echo(state)
        elif kind == "status":
            self.apply_status(state)
        elif kind == "session":
            self.apply_session(state)
        elif kind == "recovery":
            self.apply_recovery(state)
        elif kind == "orientation":
            self.apply_orientation(state)
        elif kind == "state":
            self.apply_state(state)
        elif kind == "explore":
            self.apply_explore(state)
        elif kind == "live_update":
            self.apply_live_update(state)
        elif kind == "game_record":
            self.apply_game_record(state)

    def clear_evaluation(self, clear_last=False):
        self.best_move = ""
        self.human_move = ""
        self.best_cp = None
        self.best_mate = None
        self.best_bound = "exact"
        self.depth = 0
        self.has_eval = False
        self.analysis_final = False
        self.lines = []

        if clear_last:
            self.last_move = ""
            self.last_san = ""

        if hasattr(self, "candidate_rows"):
            self.refresh_candidate_rows()
            self.refresh_explanation()
        self.dirty = True

    def clear_position(self):
        self.position_seq = 0
        self.target_revision = 0
        self.live_revision = 0
        self.grid = ["."] * 64
        self.side_to_move = "w"
        self.flip = False
        self.fen = ""
        self.state_source = ""
        self.synchronising = False
        self.update_source_badge()
        self.last_move = ""
        self.last_san = ""
        self.clear_evaluation(clear_last=True)

    def reset_analysis_lab(self):
        self.mode = "live"
        self.cancel_preview(restore=False)
        self.explore_pending = ""
        self.explore_pending_parent = None
        self.pending_start_base = ""
        self.pending_start_path = []
        self.explore_branch_id = None
        self.explore_node_id = None
        self.explore_root_node_id = None
        self.explore_nodes.clear()
        self.resume_branch_id = None
        self.resume_node_id = None
        self.live_update_count = 0
        self.live_snapshot = None
        self.update_analysis_mode_ui()

    def apply_session(self, state):
        event = state.get("event", "")

        if event == "started":
            self.cancel_game_review()
            if not self.review_results and not (
                self.review_record and self.review_record.get("imported")
            ):
                self.review_record = None
                self.review_positions = []
                self.review_position_analyses = []
                self.review_button.setEnabled(True)
            self.reset_analysis_lab()
            self.session_id = str(state.get("session_id", ""))
            self.session_label = str(state.get("label", "")).strip()
            self.session_active = True
            self.recovery_action = ""
            self.set_recovery_enabled(True)
            self.clear_position()
            self.set_status(
                self.session_label or "Analysis session ready",
                "info",
                linger=False,
            )
            return

        if event != "ended":
            return

        reason = str(state.get("reason", "ended"))
        keep_final_board = reason in {
            "game_ended",
            "game-ended",
            "game_end",
            "completed",
        }

        self.session_active = False
        self.session_id = ""
        self.session_label = ""
        self.recovery_action = ""
        self.set_recovery_enabled(False)
        self.reset_analysis_lab()
        # No delayed reconciliation can complete once its session has ended.
        # Keep the final board/source for a completed game, but never leave a
        # dead session wearing a permanent Syncing badge.
        self.synchronising = False
        self.update_source_badge()

        if self.stack.currentWidget() is self.recovery_page:
            self.stack.setCurrentWidget(self.analysis_page)

        if keep_final_board:
            self.clear_evaluation(clear_last=False)
            message = "Game ended \u2014 local review is ready"
            if self.review_record and self.review_auto:
                self.open_review()
                QTimer.singleShot(0, self.start_game_review)
        else:
            self.clear_position()
            message = "Session stopped"

        self.set_status(message, "info", linger=False)

    def apply_recovery(self, state):
        action = str(state.get("action", ""))

        if "accepted" in state or "ok" in state:
            accepted = bool(state.get("accepted", state.get("ok")))
            self.recovery_action = ""
            text = str(state.get("text", "")).strip()
            kind = str(state.get("kind", "info" if accepted else "warn"))

            if text:
                self.set_status(
                    text,
                    kind if kind in {"info", "warn"} else "info",
                    linger=accepted,
                )
            return

        self.recovery_action = action
        text = str(state.get("text", "")).strip()

        if text:
            self.set_status(text, "info", linger=True)

    def apply_orientation(self, state):
        self.flip = bool(state.get("flip"))
        self.dirty = True

    def update_source_badge(self):
        source = self.state_source if self.state_source in {
            "exact", "inferred", "manual"
        } else ""

        if self.mode == "explore":
            text, object_name = "Explore", "sourceExplore"
            tooltip = (
                "Analysis Lab is separate from the authoritative live game."
            )
        elif self.synchronising:
            text, object_name = "Syncing", "sourceSyncing"
            tooltip = (
                "The visible board does not yet match the trusted game state. "
                "ChessListener is retaining the last reliable evaluation."
            )
        elif source == "exact":
            text, object_name = "Exact", "sourceExact"
            tooltip = "Side, castling, en-passant and move counters are known."
        elif source == "manual":
            text, object_name = "Manual", "sourceManual"
            tooltip = "This state was supplied through the recovery controls."
        elif source == "inferred":
            text, object_name = "Inferred", "sourceInferred"
            tooltip = "Some hidden game-state fields were reconstructed."
        else:
            self.source_label.hide()
            return

        self.source_label.setObjectName(object_name)
        self.source_label.setText(text)
        self.source_label.setToolTip(tooltip)
        self.source_label.style().unpolish(self.source_label)
        self.source_label.style().polish(self.source_label)
        self.source_label.show()

    def apply_state(self, state):
        frame_mode = str(state.get("mode", "live"))
        if self.mode == "explore" and frame_mode != "explore":
            return

        try:
            seq = int(state.get(
                "target_revision", state.get("state_revision", state.get("seq", -1))
            ))
        except (TypeError, ValueError):
            return
        if seq >= 0 and seq < self.target_revision:
            return

        if frame_mode == "explore" and not self.explore_frame_matches(state):
            return

        source = str(state.get("source", "")).lower()
        if source in {"exact", "inferred", "manual"}:
            self.state_source = source

        self.synchronising = bool(state.get("synchronising", False))
        self.update_source_badge()

        text = str(state.get("text", "")).strip()
        if self.synchronising:
            self.set_status(
                text or "Synchronising…", "warn", linger=False
            )
        elif self.status_text.startswith("Synchron"):
            self.clear_status()

    def explore_frame_matches(self, state):
        if self.mode != "explore":
            return False
        try:
            branch = int(state.get("branch_id"))
            node = int(state.get("node_id"))
        except (TypeError, ValueError):
            return False
        return branch == self.explore_branch_id and node == self.explore_node_id

    def apply_position(self, state):
        frame_mode = str(state.get("mode", "live"))

        if frame_mode == "explore":
            if not self.explore_frame_matches(state):
                return
        elif self.mode == "explore":
            # Canonical builds use live_update here. Fail closed if an older
            # host publishes a live position while the explorer owns display.
            return

        try:
            grid, side = fen_to_grid(state["fen"])
        except (KeyError, ValueError) as error:
            print(f"overlay: {error}", file=sys.stderr)
            return

        try:
            seq = int(state.get(
                "target_revision", state.get("seq", self.target_revision + 1)
            ))
        except (TypeError, ValueError):
            return

        if seq < self.target_revision:
            return

        expected_fen = self.analysis_fen or self.fen
        if (
            seq == self.target_revision
            and self.target_revision > 0
            and expected_fen
            and state["fen"] != expected_fen
        ):
            return

        if frame_mode == "live" and self.preview_step:
            if seq == self.target_revision and state["fen"] == self.preview_root_fen:
                self.flip = bool(state.get("flip", self.flip))
                if self.live_snapshot is not None:
                    self.live_snapshot["flip"] = self.flip
                self.dirty = True
                return
            self.cancel_preview(restore=True)

        was_synchronising = self.synchronising
        source = str(state.get("source", "")).lower()
        if source in {"exact", "inferred", "manual"}:
            self.state_source = source
        self.synchronising = False
        self.update_source_badge()
        if was_synchronising and self.status_text.startswith("Synchron"):
            self.clear_status()

        # A native position is the authoritative completion of RESCAN/FEN.
        self.recovery_action = ""

        last = state.get("last") or ""
        same_position = seq == self.target_revision and state["fen"] == self.fen

        if same_position:
            # A very fast engine can publish analysis before the corresponding
            # board frame. apply_analysis() adopts that frame, so the later
            # equal-sequence position must merge metadata rather than erase the
            # evaluation that just arrived.
            if last and not self.last_move:
                self.last_move = last

            self.flip = bool(state.get("flip"))
            self.side_to_move = state.get("stm", side)
            if frame_mode == "live" and self.live_snapshot is not None:
                self.live_snapshot.update({
                    "side": self.side_to_move,
                    "flip": self.flip,
                    "last": self.last_move,
                    "source": self.state_source,
                })
            self.dirty = True
            return

        # SAN for the move just played has to be read off the board it came
        # from -- which is the one we are about to replace, so do it first.
        if last and last == self.last_move and state["fen"] == self.fen:
            # A forced re-read republishes the same authoritative position so
            # the engines run again. Keep the SAN already derived from the
            # actual pre-move board instead of trying to decode that move from
            # its post-move FEN.
            pass
        elif frame_mode == "explore" and last:
            current_node = self.explore_nodes.get(self.explore_node_id, {})
            parent_node = self.explore_nodes.get(current_node.get("parent"), {})
            before_fen = parent_node.get("fen", "")
            try:
                before_grid, _before_side = fen_to_grid(before_fen)
            except ValueError:
                before_grid = self.grid
            self.last_san = name_move(before_fen, before_grid, last)
        elif last and any(square != "." for square in self.grid):
            self.last_san = name_move(self.fen, self.grid, last)
        else:
            self.last_san = ""

        try:
            self.position_seq = int(state.get("seq", seq))
        except (TypeError, ValueError):
            return
        self.target_revision = seq
        if frame_mode == "live":
            self.live_revision = int(state.get("live_revision", seq))
        self.last_move = last
        self.fen = state["fen"]
        self.analysis_fen = self.fen
        self.grid = grid
        self.side_to_move = state.get("stm", side)
        self.flip = bool(state.get("flip"))

        # A new board invalidates the old evaluation. Showing the previous
        # eval beside a new position is worse than showing none.
        # A blanket reset here would also wipe the last move that was just
        # decoded above -- that one belongs to the new position, not the old
        # evaluation.
        self.clear_evaluation(clear_last=False)
        self.selected_line = 0

        if frame_mode == "live":
            self.mode = "live"
            self.live_snapshot = {
                "fen": self.fen,
                "grid": list(self.grid),
                "side": self.side_to_move,
                "flip": self.flip,
                "last": self.last_move,
                "last_san": self.last_san,
                "source": self.state_source,
                "synchronising": False,
                "revision": self.live_revision,
            }
        else:
            node = self.explore_nodes.setdefault(
                self.explore_node_id,
                {"parent": None, "children": [], "fen": "", "last": ""},
            )
            node["fen"] = self.fen
            node["last"] = self.last_move

        self.clear_startup_notice()
        self.update_analysis_mode_ui()
        self.dirty = True

    def apply_analysis(self, state):
        if self.recovery_action:
            return

        frame_mode = str(state.get("mode", "live"))
        if frame_mode == "explore":
            if not self.explore_frame_matches(state):
                return
        elif self.mode == "explore":
            return

        try:
            seq = int(state.get("target_revision", state.get("seq", -1)))
        except (TypeError, ValueError):
            return

        # Stale evaluation for a position the board has already left behind.
        if 0 <= seq < self.target_revision:
            return

        frame_fen = str(state.get("fen", ""))
        if (
            seq == self.target_revision
            and self.target_revision > 0
            and self.analysis_fen
            and frame_fen
            and frame_fen != self.analysis_fen
        ):
            return

        if seq > self.target_revision:
            # Evaluation arrived before its board frame; adopt the board too.
            self.apply_position(state)

            if seq > self.target_revision:
                return

        source = str(state.get("source", "")).lower()
        if source in {"exact", "inferred", "manual"}:
            self.state_source = source
            self.update_source_badge()

        best = state.get("best") or {}
        human = state.get("human") or {}

        self.best_move = best.get("move") or ""
        self.human_move = human.get("move") or ""
        self.best_cp = best.get("cp")
        self.best_mate = best.get("mate")
        self.best_bound = str(best.get("bound", "exact"))
        self.depth = int(state.get("depth") or 0)
        self.has_eval = self.best_cp is not None or self.best_mate is not None
        self.analysis_final = bool(state.get("final", False))
        raw_lines = state.get("lines") or []
        self.lines = [
            dict(line, final=bool(line.get("final", self.analysis_final)))
            for line in raw_lines
            if isinstance(line, dict)
        ]
        self.analysis_fen = str(state.get("fen", self.fen))

        if self.selected_line >= len(self.lines):
            self.selected_line = 0

        if frame_mode == "explore" and self.explore_node_id in self.explore_nodes:
            self.explore_nodes[self.explore_node_id]["analysis"] = {
                "best": best, "human": human, "lines": self.lines,
                "depth": self.depth, "final": self.analysis_final,
            }

        self.clear_startup_notice()
        self.refresh_candidate_rows()
        self.refresh_explanation()
        self.dirty = True

    def apply_ready(self, state):
        self.apply_settings_echo(state)

        if self.recovery_action in {"restart", "restart_engines"}:
            self.recovery_action = ""
            self.clear_evaluation(clear_last=False)

        missing = [
            name
            for name, ok in (
                ("Stockfish", bool(state.get("stockfish"))),
                ("Maia", self.maia_rating == 0 or bool(state.get("maia"))),
            )
            if not ok
        ]

        self.dirty = True

        if missing:
            self.set_status(
                "Not available: " + ", ".join(missing), "warn", linger=False
            )
        else:
            self.set_status("Engines ready", "info", linger=True)

    def apply_settings_echo(self, state):
        self.budget_ms = int(state.get("budget_ms", self.budget_ms))
        self.maia_rating = int(state.get("maia_rating", self.maia_rating))
        self.threads = int(state.get("threads", self.threads))
        self.multipv = int(state.get("multipv", self.multipv))
        self.explore_budget = int(
            state.get("explore_budget_ms", state.get("explore_budget", self.explore_budget))
        )

        self.applying_settings = True
        self.select_data(self.live_budget, self.budget_ms, 1)
        self.select_data(self.live_maia, self.maia_rating, self.live_maia.count() - 1)
        self.live_threads.setValue(
            max(1, min(self.live_threads.maximum(), self.threads))
        )
        self.live_multipv.setValue(max(1, min(5, self.multipv)))
        self.select_data(self.live_explore_budget, self.explore_budget, 0)
        self.applying_settings = False

    def apply_status(self, state):
        text = str(state.get("text", ""))
        kind = state.get("kind", "info")

        if text:
            self.set_status(text, kind if kind in {"info", "warn"} else "info")
        else:
            self.clear_status()

    # -- the only place board data reaches widgets -------------------------

    def flush_frame(self):
        if self.dirty:
            self.dirty = False
            self.repaint_state()

        # Runs every frame, dirty or not: the bar slides toward its target
        # after the state that set it has already been applied.
        if self.eval_bar.advance():
            self.eval_bar.update()

    def repaint_state(self):
        selected = self.selected_candidate()
        selected_move = (
            str(selected.get("move", "")) if selected is not None else self.best_move
        )

        if self.preview_step and self.preview_step < len(self.preview_moves):
            selected_move = self.preview_moves[self.preview_step]
        elif self.preview_step:
            selected_move = ""

        self.board.set_position(self.grid, self.side_to_move, self.flip, self.fen)
        self.board.set_moves(
            selected_move if self.show_best_arrow else "",
            self.human_move if self.show_human_arrow and not self.preview_step else "",
            self.last_move if self.show_played_highlight else "",
        )
        self.board.set_interactive(self.mode == "explore" and not self.explore_pending)
        self.board.update()

        self.eval_bar.set_eval(
            self.best_cp, self.best_mate, self.depth, self.has_eval,
            self.best_bound,
        )
        try:
            _root_grid, score_side = fen_to_grid(self.analysis_fen or self.fen)
        except ValueError:
            score_side = self.side_to_move
        self.eval_bar.set_pov(self.eval_pov, score_side)

        self.turn_dot.set_side(self.side_to_move)
        self.turn_dot.update()

        root_fen = self.analysis_fen or self.fen
        best_san = name_move(
            self.fen if self.preview_step else root_fen,
            self.grid,
            selected_move,
        )
        human_san = name_move(root_fen, self.grid, self.human_move)

        self.last_label.setText(
            f"{('Preview' if self.preview_step else 'Played'):<11}"
            f"{self.last_san or self.last_move or '--'}"
        )
        self.best_label.setText(f"{'Stockfish':<11}{best_san or '--'}")
        if self.maia_rating == 0:
            self.human_label.setText(f"{'Maia':<11}Off")
        else:
            agreement = "  ✓ agrees" if self.human_move and self.human_move == self.best_move else ""
            self.human_label.setText(
                f"{'Maia ' + str(self.maia_rating):<11}{human_san or '--'}{agreement}"
            )

    def closeEvent(self, event: QCloseEvent):
        if self.current_study is not None:
            self.persist_current_study()
        if self.review_job is not None:
            self.review_job.cancel()
            self.review_job.join(timeout=3.5)
        if self.review_position_job is not None:
            self.review_position_job.cancel()
            self.review_position_job.join(timeout=3.5)
        if self.study_position_job is not None:
            self.study_position_job.cancel()
            self.study_position_job.join(timeout=3.5)
        if self.compact and self.expanded_geometry is not None:
            self.settings.setValue("window/geometry", self.expanded_geometry)
        else:
            self.save_geometry()

        self.settings.sync()

        if not self.local_mode:
            try:
                command = (
                    f"QUIT {self.session_id}"
                    if self.session_active and self.session_id
                    else "QUIT"
                )
                self.send_control(command)
            except OSError:
                pass

        super().closeEvent(event)


class StdinReader:
    """Read complete JSON lines without mixing stdio buffering with Qt."""

    def __init__(self, on_line):
        self.on_line = on_line
        self.buffer = b""
        self.file_descriptor = sys.stdin.fileno()
        self.notifier = QSocketNotifier(
            self.file_descriptor,
            QSocketNotifier.Type.Read,
        )
        self.notifier.activated.connect(self.ready)

    def ready(self, _):
        try:
            chunk = os.read(self.file_descriptor, 262144)
        except OSError:
            chunk = b""

        if not chunk:
            self.notifier.setEnabled(False)
            QApplication.quit()
            return

        self.buffer += chunk

        # Every queued line is parsed, but the handler only folds them into
        # state. Painting happens once, later, on the frame timer.
        while b"\n" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\n", 1)
            raw = raw.strip()

            if not raw:
                continue

            try:
                self.on_line(json.loads(raw))
            except json.JSONDecodeError as error:
                print(f"overlay: bad JSON: {error}", file=sys.stderr)


def main():
    local_mode = any(argument in {"--local", "--local-review"} for argument in sys.argv[1:])
    qt_arguments = [
        argument for argument in sys.argv
        if argument not in {"--local", "--local-review"}
    ]
    app = QApplication(qt_arguments)
    app.setApplicationName(APP_ID)
    app.setOrganizationName(ORGANIZATION)
    app.setStyle("Fusion")
    QGuiApplication.setDesktopFileName(APP_ID)

    window = Overlay()
    reader = None
    if local_mode:
        window.start_local_review_mode()
    else:
        reader = StdinReader(window.handle_message)  # keep the notifier alive
    _ = reader
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
