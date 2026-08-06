#!/usr/bin/env python3
"""ChessListener startup window and always-on-top analysis overlay.

The native host sends one JSON object per line on stdin. This process reserves
stdout for a small control protocol:

    START budget=400 maia=1900 threads=2 multipv=3
    SET   budget=900 maia=1600 threads=4 multipv=3
    QUIT

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
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

APP_ID = "chess-overlay"
ORGANIZATION = "ChessListener"
APPLICATION = "ChessListener"

FRAME_INTERVAL_MS = 16
SETTINGS_DEBOUNCE_MS = 300
STATUS_LINGER_MS = 4000

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


def fen_to_grid(fen):
    """Return (64-square a8..h1 grid, side to move)."""
    parts = fen.split()
    rows = parts[0].split("/")

    if len(rows) != 8:
        raise ValueError("bad FEN board field")

    grid = []

    for row in rows:
        square_count = 0

        for character in row:
            if character.isdigit():
                empty_count = int(character)
                grid.extend("." * empty_count)
                square_count += empty_count
            else:
                grid.append(character)
                square_count += 1

        if square_count != 8:
            raise ValueError("bad FEN rank: " + row)

    side = parts[1] if len(parts) > 1 else "w"
    return grid, side


def square_index(name):
    """Convert e4 to an index in an a8..h1 grid."""
    if not name or len(name) < 2:
        return None

    file_index = ord(name[0]) - ord("a")
    rank_index = ord(name[1]) - ord("1")

    if not (0 <= file_index < 8 and 0 <= rank_index < 8):
        return None

    return (7 - rank_index) * 8 + file_index


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

    def set_moves(self, best_move, human_move):
        self.best_move = best_move or ""
        self.human_move = human_move or ""

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
        self.setFixedHeight(26)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_eval(self, centipawns, mate, depth, has_eval):
        self.centipawns = centipawns
        self.mate = mate
        self.depth = depth
        self.has_eval = has_eval

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = QRectF(0.0, 0.0, float(self.width()), float(self.height()))
        radius = rect.height() / 2.0

        clip = QPainterPath()
        clip.addRoundedRect(rect, radius, radius)
        painter.setClipPath(clip)

        base_font = QFont(QApplication.font())

        if not self.has_eval:
            painter.fillRect(rect, QColor("#383c44"))
            painter.setPen(QColor("#98a0ae"))
            base_font.setPixelSize(max(9, int(rect.height() * 0.50)))
            painter.setFont(base_font)
            painter.drawText(
                rect, Qt.AlignmentFlag.AlignCenter, "thinking\u2026"
            )
            painter.end()
            return

        fraction = win_fraction(self.centipawns, self.mate)
        split = rect.width() * fraction

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
        self.best_cp = None
        self.best_mate = None
        self.depth = 0
        self.has_eval = False
        self.lines = []
        self.status_text = ""
        self.dirty = False

        self.piece_family = resolve_piece_font()

        self.setWindowTitle("ChessListener")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setMinimumWidth(320)

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

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        self.stack = QStackedWidget()
        root.addWidget(self.stack)

        self.startup_page = self.build_startup_page()
        self.analysis_page = self.build_analysis_page()
        self.settings_page = self.build_settings_page()

        for page in (self.startup_page, self.analysis_page, self.settings_page):
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
            QComboBox, QSpinBox {{
                background: #171a1e;
                border: 1px solid #414751;
                border-radius: 7px;
                padding: 6px 4px 6px 9px;
                min-height: 20px;
            }}
            QComboBox:focus, QSpinBox:focus {{ border-color: #6d98c4; }}
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
            QCheckBox {{ spacing: 8px; }}
            """
        )

    # -- pages ------------------------------------------------------------

    def build_startup_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(13)

        title = QLabel("ChessListener")
        title.setObjectName("title")
        layout.addWidget(title)

        subtitle = QLabel(
            "Live spectator analysis for games you are watching on Chess.com."
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
        root.setContentsMargins(9, 8, 9, 9)
        root.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(7)

        self.turn_dot = TurnDot()
        header.addWidget(self.turn_dot)

        # Fixed height: the status line appearing and vanishing must not reflow
        # the board underneath it.
        self.status_label = QLabel("")
        self.status_label.setObjectName("statusInfo")
        self.status_label.setFixedHeight(16)
        header.addWidget(self.status_label, 1)

        self.settings_button = QPushButton("Settings")
        self.settings_button.setObjectName("ghost")
        self.settings_button.clicked.connect(self.open_settings)
        header.addWidget(self.settings_button)

        root.addLayout(header)

        self.board = BoardView(self.piece_family)
        root.addWidget(self.board, 1)

        self.eval_bar = EvalBar()
        root.addWidget(self.eval_bar)

        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSize(9)

        self.best_label = QLabel("Stockfish  --")
        self.best_label.setObjectName("engineLine")
        self.human_label = QLabel("Maia  --")
        self.human_label.setObjectName("engineLine")
        self.pv_label = QLabel("")
        self.pv_label.setObjectName("pvLine")
        self.pv_label.setWordWrap(True)

        for widget in (self.best_label, self.human_label, self.pv_label):
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
        
        self.live_multipv = QSpinBox()
        self.live_multipv.setRange(1, 5)
        
        rows = (
            ("Analysis strength", self.live_budget),
            ("Stockfish threads", self.live_threads),
            ("Candidate lines", self.live_multipv),
            ("Natural-move model", self.live_maia),
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
            "seconds."
        )
        note.setObjectName("helper")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)

        for widget in (self.live_budget, self.live_maia):
            widget.currentIndexChanged.connect(self.queue_settings)

        for widget in (self.live_threads, self.live_multipv):
            widget.valueChanged.connect(self.queue_settings)

        self.live_budget.currentIndexChanged.connect(self.update_live_budget_help)

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
        self.human_label.setText(f"Maia {self.maia_rating}  --")
        self.stack.setCurrentWidget(self.analysis_page)
        self.resize(352, 492)
        self.frame_timer.start()
        self.send_control("START " + self.settings_payload())

    def queue_settings(self):
        if self.applying_settings or not self.start_command_sent:
            return

        self.settings_timer.start()

    def send_settings(self):
        self.budget_ms = int(self.live_budget.currentData())
        self.maia_rating = int(self.live_maia.currentData())
        self.threads = int(self.live_threads.value())
        self.multipv = int(self.live_multipv.value())

        self.human_label.setText(f"Maia {self.maia_rating}  --")
        self.save_settings()
        self.send_control("SET " + self.settings_payload())

    def open_settings(self):
        self.stack.setCurrentWidget(self.settings_page)

    def close_settings(self):
        # Don't make the user wait out the debounce just because they were quick.
        if self.settings_timer.isActive():
            self.settings_timer.stop()
            self.send_settings()

        self.stack.setCurrentWidget(self.analysis_page)

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
        self.set_status("", "info", linger=False)

    def clear_startup_notice(self):
        """Belt and braces for the notice that used to get stuck.

        The host does send a ready frame, but the notice is a UI concern: any
        evidence that analysis is running is enough to retire it, so a lost or
        late ready message can no longer leave it on screen forever.
        """
        if self.status_text.startswith("Starting"):
            self.set_status("", "info", linger=False)

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

    def apply_position(self, state):
        try:
            grid, side = fen_to_grid(state["fen"])
        except (KeyError, ValueError) as error:
            print(f"overlay: {error}", file=sys.stderr)
            return

        seq = int(state.get("seq", self.position_seq + 1))

        if seq < self.position_seq:
            return

        self.position_seq = seq
        self.grid = grid
        self.side_to_move = state.get("stm", side)
        self.flip = bool(state.get("flip"))

        # A new board invalidates the old evaluation. Showing the previous
        # eval beside a new position is worse than showing none.
        self.best_move = ""
        self.human_move = ""
        self.best_cp = None
        self.best_mate = None
        self.depth = 0
        self.has_eval = False
        self.lines = []

        self.clear_startup_notice()
        self.dirty = True

    def apply_analysis(self, state):
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

        missing = [
            name
            for name, ok in (
                ("Stockfish", bool(state.get("stockfish"))),
                ("Maia", bool(state.get("maia"))),
            )
            if not ok
        ]

        self.human_label.setText(f"Maia {self.maia_rating}  --")

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
        self.set_status(text, kind if kind in {"info", "warn"} else "info")

    # -- the only place board data reaches widgets -------------------------

    def flush_frame(self):
        if not self.dirty:
            return

        self.dirty = False

        self.board.set_position(self.grid, self.side_to_move, self.flip)
        self.board.set_moves(self.best_move, self.human_move)
        self.board.update()

        self.eval_bar.set_eval(
            self.best_cp, self.best_mate, self.depth, self.has_eval
        )
        self.eval_bar.update()

        self.turn_dot.set_side(self.side_to_move)
        self.turn_dot.update()

        self.best_label.setText(f"Stockfish  {self.best_move or '--'}")
        self.human_label.setText(
            f"Maia {self.maia_rating}  {self.human_move or '--'}"
        )

        if self.lines:
            self.pv_label.setText(
                "   ".join(
                    f"{line.get('move', '?')} "
                    f"{format_score(line.get('cp'), line.get('mate'))}"
                    for line in self.lines[:4]
                )
            )
        else:
            self.pv_label.setText("")

    def closeEvent(self, event: QCloseEvent):
        try:
            self.send_control("QUIT")
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
