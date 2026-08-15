#!/usr/bin/env python3
"""ChessListener startup window and always-on-top analysis overlay.

The native host sends one JSON object per line on stdin. This process reserves
stdout for a small control protocol:

    START protocol=2 ui_version=0.3.0 budget=400 maia=1900 threads=2 multipv=3
    SET   budget=900 maia=1600 threads=4 multipv=3
    RESCAN <session-id>
    FEN <session-id> rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1
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

try:
    import san as san_rules
except ImportError:  # pragma: no cover - overlay must still run without it
    san_rules = None

from PyQt6.QtCore import QPointF, QRectF, QSettings, QSocketNotifier, Qt, QTimer
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
    QPushButton,
    QSizeGrip,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

APP_ID = "chess-overlay"
ORGANIZATION = "ChessListener"
APPLICATION = "ChessListener"
APP_VERSION = "0.3.0"
PROTOCOL_VERSION = 2

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

    def __init__(self, piece_family, parent=None):
        super().__init__(parent)
        self.piece_family = piece_family
        self.grid = ["."] * 64
        self.flip = False
        self.best_move = ""
        self.human_move = ""
        self.last_move = ""
        self.side_to_move = "w"
        self._path_cache = {}
        self.setMinimumSize(200, 200)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )

    def set_position(self, grid, side_to_move, flip):
        self.grid = grid
        self.side_to_move = side_to_move
        self.flip = flip

    def set_moves(self, best_move, human_move, last_move):
        self.best_move = best_move or ""
        self.human_move = human_move or ""
        self.last_move = last_move or ""

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

        # Arrows last, so they read on top of the pieces.
        human = QColor(COLOR_HUMAN)
        human.setAlpha(205)
        best = QColor(COLOR_BEST)
        best.setAlpha(225)

        if self.human_move and self.human_move != self.best_move:
            self.draw_arrow(painter, self.human_move, human, board, 0.115)

        self.draw_arrow(painter, self.best_move, best, board, 0.145)

        painter.setPen(QPen(QColor(0, 0, 0, 90), 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(board.adjusted(0.5, 0.5, -0.5, -0.5))
        painter.end()


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
        # The bar slides to a new evaluation instead of snapping. A deepening
        # search revises its score several times a second, and a bar that jumps
        # on every revision is genuinely hard to read.
        self.target_fraction = 0.5
        self.display_fraction = 0.5
        self.setFixedHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_eval(self, centipawns, mate, depth, has_eval):
        self.centipawns = centipawns
        self.mate = mate
        self.depth = depth
        self.has_eval = has_eval

        if has_eval:
            self.target_fraction = win_fraction(centipawns, mate)

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

        text = format_score(self.centipawns, self.mate)
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

        self.budget_ms = 400
        self.maia_rating = 1900
        self.threads = min(4, max(1, (os.cpu_count() or 2) // 2))
        self.multipv = 3

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
        self.depth = 0
        self.has_eval = False
        self.lines = []
        self.status_text = ""
        self.dirty = False
        self.session_id = ""
        self.session_label = ""
        self.session_active = False
        self.recovery_action = ""

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

        for page in (
            self.startup_page,
            self.analysis_page,
            self.settings_page,
            self.recovery_page,
        ):
            self.stack.addWidget(page)

        self.stack.setCurrentWidget(self.startup_page)

        self.apply_style()
        self.restore_saved_settings()
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
            QLabel#engineLine {{ color: #cfd5e0; }}
            QLabel#pvLine {{ color: #929aa8; }}
            QFrame#panel {{
                background: {COLOR_PANEL.name()};
                border: 1px solid #343841;
                border-radius: 10px;
            }}
            QComboBox, QSpinBox, QLineEdit {{
                background: #171a1e;
                border: 1px solid #414751;
                border-radius: 7px;
                padding: 6px 4px 6px 9px;
                min-height: 20px;
            }}
            QComboBox:focus, QSpinBox:focus, QLineEdit:focus {{
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

        self.compact_button = self.make_title_button("\u2013", "Collapse")
        self.compact_button.clicked.connect(self.toggle_compact)
        layout.addWidget(self.compact_button)

        self.recovery_button = self.make_title_button("\u21bb", "Recovery")
        self.recovery_button.clicked.connect(self.toggle_recovery)
        self.recovery_button.setEnabled(False)
        layout.addWidget(self.recovery_button)

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
        root.addWidget(self.board, 1)

        self.eval_bar = EvalBar()
        root.addWidget(self.eval_bar)

        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(9)

        # Three aligned rows: what just happened, what the engine wants, what a
        # human of the chosen rating would play. Padded with a monospace font
        # rather than a grid layout so the values line up without the labels
        # jumping every time a move name changes width.
        self.last_label = QLabel("")
        self.last_label.setObjectName("lastLine")
        self.best_label = QLabel("")
        self.best_label.setObjectName("engineLine")
        self.human_label = QLabel("")
        self.human_label.setObjectName("engineLine")
        self.pv_label = QLabel("")
        self.pv_label.setObjectName("pvLine")
        self.pv_label.setWordWrap(True)

        for widget in (
            self.last_label,
            self.best_label,
            self.human_label,
            self.pv_label,
        ):
            widget.setFont(mono)
            root.addWidget(widget)

        return page

    def build_settings_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(11)

        header = QHBoxLayout()
        title = QLabel("Settings")
        title.setObjectName("heading")
        header.addWidget(title)
        header.addStretch(1)

        done_button = QPushButton("Done")
        done_button.setObjectName("ghost")
        done_button.clicked.connect(self.close_settings)
        header.addWidget(done_button)
        layout.addLayout(header)

        panel = QFrame()
        panel.setObjectName("panel")
        grid = QGridLayout(panel)
        grid.setContentsMargins(13, 13, 13, 13)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(9)

        self.live_budget = self.make_budget_combo()
        self.live_maia = self.make_maia_combo()

        self.live_threads = QSpinBox()
        self.live_threads.setRange(1, max(1, os.cpu_count() or 1))

        self.live_opacity = QComboBox()

        for percent, label in OPACITY_CHOICES:
            self.live_opacity.addItem(label, percent)

        self.live_multipv = QSpinBox()
        self.live_multipv.setRange(1, 5)

        rows = (
            ("Analysis strength", self.live_budget),
            ("Stockfish threads", self.live_threads),
            ("Candidate lines", self.live_multipv),
            ("Natural-move model", self.live_maia),
            ("Window opacity", self.live_opacity),
        )

        for row, (label_text, widget) in enumerate(rows):
            grid.addWidget(QLabel(label_text), row, 0)
            grid.addWidget(widget, row, 1)

        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)
        self.live_budget.setMinimumWidth(150)
        layout.addWidget(panel)

        self.live_budget_help = QLabel()
        self.live_budget_help.setObjectName("helper")
        self.live_budget_help.setWordWrap(True)
        layout.addWidget(self.live_budget_help)

        note = QLabel(
            "Strength, CPU and line count take effect on the next search. "
            "Changing the Maia rating reloads its network, which takes a few "
            "seconds. Double-click the header to collapse the window."
        )
        note.setObjectName("helper")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)

        for widget in (self.live_budget, self.live_maia):
            widget.currentIndexChanged.connect(self.queue_settings)

        # Opacity is ours alone; the host never needs to hear about it.
        self.live_opacity.currentIndexChanged.connect(self.apply_opacity)

        for widget in (self.live_threads, self.live_multipv):
            widget.valueChanged.connect(self.queue_settings)

        self.live_budget.currentIndexChanged.connect(self.update_live_budget_help)

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
        except (TypeError, ValueError):
            saved_budget, saved_rating = 400, 1900
            saved_threads, saved_multipv = self.threads, 3

        self.budget_ms = saved_budget
        self.maia_rating = saved_rating
        self.threads = max(1, min(self.live_threads.maximum(), saved_threads))
        self.multipv = max(1, min(5, saved_multipv))

        self.select_data(self.startup_budget, saved_budget, 1)
        self.select_data(self.startup_maia, saved_rating, len(MAIA_RATINGS) - 1)

        self.applying_settings = True
        self.select_data(self.live_budget, saved_budget, 1)
        self.select_data(self.live_maia, saved_rating, len(MAIA_RATINGS) - 1)
        self.live_threads.setValue(self.threads)
        self.live_multipv.setValue(self.multipv)
        self.applying_settings = False

        try:
            saved_opacity = int(self.settings.value("window/opacity", 100))
        except (TypeError, ValueError):
            saved_opacity = 100

        self.select_data(self.live_opacity, saved_opacity, 0)
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

        self.settings.sync()

    # -- control protocol -------------------------------------------------

    def send_control(self, command):
        try:
            os.write(sys.stdout.fileno(), (command + "\n").encode("ascii"))
        except OSError as error:
            print(f"overlay: control write failed: {error}", file=sys.stderr)

    def settings_payload(self):
        return (
            f"budget={self.budget_ms} maia={self.maia_rating} "
            f"threads={self.threads} multipv={self.multipv}"
        )

    def start_analysis(self):
        if self.start_command_sent:
            return

        self.budget_ms = int(self.startup_budget.currentData())
        self.maia_rating = int(self.startup_maia.currentData())

        self.applying_settings = True
        self.select_data(self.live_budget, self.budget_ms, 1)
        self.select_data(self.live_maia, self.maia_rating, len(MAIA_RATINGS) - 1)
        self.applying_settings = False

        self.save_settings()
        self.start_command_sent = True

        self.set_status("Starting Stockfish and Maia\u2026", "info", linger=False)
        self.stack.setCurrentWidget(self.analysis_page)

        for widget in (
            self.turn_dot,
            self.compact_button,
            self.recovery_button,
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

    def queue_settings(self):
        if self.applying_settings or not self.start_command_sent:
            return

        self.settings_timer.start()

    def send_settings(self):
        self.budget_ms = int(self.live_budget.currentData())
        self.maia_rating = int(self.live_maia.currentData())
        self.threads = int(self.live_threads.value())
        self.multipv = int(self.live_multipv.value())

        self.save_settings()
        self.send_control("SET " + self.settings_payload())
        self.dirty = True

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

        for widget in (self.board, self.last_label, self.pv_label):
            widget.setVisible(not self.compact)

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
        self.stack.setCurrentWidget(self.settings_page)

    def close_settings(self):
        # Don't make the user wait out the debounce just because they were quick.
        if self.settings_timer.isActive():
            self.settings_timer.stop()
            self.send_settings()

        self.stack.setCurrentWidget(self.analysis_page)

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
        fields = self.fen.split()

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

        self.recovery_exact_fen.setText(self.fen)

    def visible_board_fen(self):
        board = grid_to_fen_board(self.grid)
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
        fallback = self.session_label if self.session_active else ""
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

    def clear_evaluation(self, clear_last=False):
        self.best_move = ""
        self.human_move = ""
        self.best_cp = None
        self.best_mate = None
        self.depth = 0
        self.has_eval = False
        self.lines = []

        if clear_last:
            self.last_move = ""
            self.last_san = ""

        self.dirty = True

    def clear_position(self):
        self.position_seq = 0
        self.grid = ["."] * 64
        self.side_to_move = "w"
        self.flip = False
        self.fen = ""
        self.last_move = ""
        self.last_san = ""
        self.clear_evaluation(clear_last=True)

    def apply_session(self, state):
        event = state.get("event", "")

        if event == "started":
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

        if self.stack.currentWidget() is self.recovery_page:
            self.stack.setCurrentWidget(self.analysis_page)

        if keep_final_board:
            self.clear_evaluation(clear_last=False)
            message = "Game ended \u2014 analysis stopped"
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

    def apply_position(self, state):
        try:
            grid, side = fen_to_grid(state["fen"])
        except (KeyError, ValueError) as error:
            print(f"overlay: {error}", file=sys.stderr)
            return

        seq = int(state.get("seq", self.position_seq + 1))

        if seq < self.position_seq:
            return

        # A native position is the authoritative completion of RESCAN/FEN.
        self.recovery_action = ""

        last = state.get("last") or ""
        same_position = seq == self.position_seq and state["fen"] == self.fen

        if same_position:
            # A very fast engine can publish analysis before the corresponding
            # board frame. apply_analysis() adopts that frame, so the later
            # equal-sequence position must merge metadata rather than erase the
            # evaluation that just arrived.
            if last and not self.last_move:
                self.last_move = last

            self.flip = bool(state.get("flip"))
            self.side_to_move = state.get("stm", side)
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
        elif last and any(square != "." for square in self.grid):
            self.last_san = name_move(self.fen, self.grid, last)
        else:
            self.last_san = ""

        self.position_seq = seq
        self.last_move = last
        self.fen = state["fen"]
        self.grid = grid
        self.side_to_move = state.get("stm", side)
        self.flip = bool(state.get("flip"))

        # A new board invalidates the old evaluation. Showing the previous
        # eval beside a new position is worse than showing none.
        # A blanket reset here would also wipe the last move that was just
        # decoded above -- that one belongs to the new position, not the old
        # evaluation.
        self.clear_evaluation(clear_last=False)

        self.clear_startup_notice()
        self.dirty = True

    def apply_analysis(self, state):
        if self.recovery_action:
            return

        seq = int(state.get("seq", -1))

        # Stale evaluation for a position the board has already left behind.
        if 0 <= seq < self.position_seq:
            return

        if seq > self.position_seq:
            # Evaluation arrived before its board frame; adopt the board too.
            self.apply_position(state)

        best = state.get("best") or {}
        human = state.get("human") or {}

        self.best_move = best.get("move") or ""
        self.human_move = human.get("move") or ""
        self.best_cp = best.get("cp")
        self.best_mate = best.get("mate")
        self.depth = int(state.get("depth") or 0)
        self.has_eval = self.best_cp is not None or self.best_mate is not None
        self.lines = state.get("lines") or []

        self.clear_startup_notice()
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
                ("Maia", bool(state.get("maia"))),
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

        self.applying_settings = True
        self.select_data(self.live_budget, self.budget_ms, 1)
        self.select_data(self.live_maia, self.maia_rating, len(MAIA_RATINGS) - 1)
        self.live_threads.setValue(
            max(1, min(self.live_threads.maximum(), self.threads))
        )
        self.live_multipv.setValue(max(1, min(5, self.multipv)))
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
        self.board.set_position(self.grid, self.side_to_move, self.flip)
        self.board.set_moves(self.best_move, self.human_move, self.last_move)
        self.board.update()

        self.eval_bar.set_eval(
            self.best_cp, self.best_mate, self.depth, self.has_eval
        )

        self.turn_dot.set_side(self.side_to_move)
        self.turn_dot.update()

        best_san = name_move(self.fen, self.grid, self.best_move)
        human_san = name_move(self.fen, self.grid, self.human_move)

        self.last_label.setText(
            f"{'Played':<11}{self.last_san or self.last_move or '--'}"
        )
        self.best_label.setText(f"{'Stockfish':<11}{best_san or '--'}")
        self.human_label.setText(
            f"{'Maia ' + str(self.maia_rating):<11}{human_san or '--'}"
        )

        if self.lines and len(self.lines) > 1:
            # Skip the first line: it is already on the Stockfish row.
            self.pv_label.setText(
                "   ".join(
                    f"{name_move(self.fen, self.grid, line.get('move', '')) or '?'} "
                    f"{format_score(line.get('cp'), line.get('mate'))}"
                    for line in self.lines[1:4]
                )
            )
        else:
            self.pv_label.setText("")

    def closeEvent(self, event: QCloseEvent):
        if self.compact and self.expanded_geometry is not None:
            self.settings.setValue("window/geometry", self.expanded_geometry)
        else:
            self.save_geometry()

        self.settings.sync()

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
    app = QApplication(sys.argv)
    app.setApplicationName(APP_ID)
    app.setOrganizationName(ORGANIZATION)
    app.setStyle("Fusion")
    QGuiApplication.setDesktopFileName(APP_ID)

    window = Overlay()
    reader = StdinReader(window.handle_message)  # keep the notifier alive
    _ = reader
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
