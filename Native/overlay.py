#!/usr/bin/env python3
"""ChessListener startup window and always-on-top analysis overlay.

The native host sends one JSON object per line on stdin. This process reserves
stdout for a small control protocol:

    START protocol=4 ui_version=0.9.5 budget=400 maia=1900 threads=2 multipv=3
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
    QPoint,
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
    QAbstractButton,
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
    QSplitter,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QToolTip,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)


class ScrollSafeComboBox(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):
        if not self.hasFocus():
            event.ignore()
        else:
            super().wheelEvent(event)

class ScrollSafeSpinBox(QSpinBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event):
        if not self.hasFocus():
            event.ignore()
        else:
            super().wheelEvent(event)

APP_ID = "chess-overlay"
ORGANIZATION = "ChessListener"
APPLICATION = "ChessListener"
APP_VERSION = "0.9.5"
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

# 0.9.5 "Analyst's Desk" tokens.  The warm board remains the visual anchor;
# the surrounding shell is deliberately matte and quiet so source colours do
# actual information work instead of becoming decoration.
COLOR_LIGHT = QColor("#e8d5b4")
COLOR_DARK = QColor("#987652")
COLOR_PIECE_WHITE = QColor("#faf7ef")
COLOR_PIECE_BLACK = QColor("#1e201c")
COLOR_PIECE_EDGE = QColor("#151714")
COLOR_BEST = QColor("#5b8fc9")
COLOR_HUMAN = QColor("#d4914f")
COLOR_LAST = QColor("#d4b84f")
COLOR_COORD = QColor("#7f887e")
COLOR_LEGAL = QColor("#7da9d2")
COLOR_SELECTED = QColor("#5b8fc9")

COLOR_BG = QColor("#151714")
COLOR_RAISED = QColor("#1c1f1c")
COLOR_CONTROL = QColor("#242824")
COLOR_PANEL = COLOR_RAISED
COLOR_LINE = QColor("#3a4039")
COLOR_LINE_STRONG = QColor("#535b52")
COLOR_TEXT = QColor("#f0ede5")
COLOR_TEXT_SECONDARY = QColor("#aeb5ac")
COLOR_TEXT_QUIET = QColor("#7f887e")
COLOR_SUCCESS = QColor("#7db58f")
COLOR_WARNING = QColor("#d8a35b")
COLOR_DANGER = QColor("#d36b70")
COLOR_FOCUS = QColor("#8eb9e5")
COLOR_BAR_WHITE = QColor("#eee8da")
COLOR_BAR_BLACK = QColor("#24241f")

# Both modes deliberately use one glyph family for both colours.  Unicode's
# white chess glyphs are the outline family and the black glyphs are the solid
# family; colour then distinguishes sides.  Mixing the two families is never
# allowed because it makes one side look visually heavier than the other.
GLYPH_SOLID = {
    "k": "\u265a",
    "q": "\u265b",
    "r": "\u265c",
    "b": "\u265d",
    "n": "\u265e",
    "p": "\u265f",
}

GLYPH_OUTLINE = {
    "k": "\u2654",
    "q": "\u2655",
    "r": "\u2656",
    "b": "\u2657",
    "n": "\u2658",
    "p": "\u2659",
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
WORKSPACE_ORIENTATION_CHOICES = (
    ("follow", "Follow live board"),
    ("white", "White at bottom"),
    ("black", "Black at bottom"),
)


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

        glyphs = tuple(GLYPH_SOLID.values()) + tuple(GLYPH_OUTLINE.values())
        if all(raw.supportsCharacter(ord(glyph)) for glyph in glyphs):
            return family

    return QFont().family()


class BrandMark(QWidget):
    """Small four-square ChessListener mark used in the native title bar."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self.setAccessibleName("ChessListener")
        self.setToolTip("ChessListener")

    def paintEvent(self, _event):
        painter = QPainter(self)
        bounds = self.rect().adjusted(1, 1, -1, -1)
        half_w = bounds.width() // 2
        half_h = bounds.height() // 2
        cells = (
            (QRectF(bounds.left(), bounds.top(), half_w, half_h), COLOR_LIGHT),
            (QRectF(bounds.left() + half_w, bounds.top(),
                    bounds.width() - half_w, half_h), COLOR_DARK),
            (QRectF(bounds.left(), bounds.top() + half_h, half_w,
                    bounds.height() - half_h), COLOR_DARK),
            (QRectF(bounds.left() + half_w, bounds.top() + half_h,
                    bounds.width() - half_w, bounds.height() - half_h), COLOR_LIGHT),
        )
        painter.setPen(Qt.PenStyle.NoPen)
        for rect, colour in cells:
            painter.fillRect(rect, colour)
        painter.setPen(QPen(COLOR_LINE_STRONG, 1))
        painter.drawRect(QRectF(bounds))
        font = QFont(QApplication.font())
        font.setPixelSize(6)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(COLOR_BG)
        painter.drawText(cells[0][0], Qt.AlignmentFlag.AlignCenter, "C")
        painter.drawText(cells[3][0], Qt.AlignmentFlag.AlignCenter, "L")
        painter.end()


class TitleAction(QAbstractButton):
    """Accessible, consistently sized line icon for title-bar actions."""

    def __init__(self, icon_name, accessible_name, parent=None):
        super().__init__(parent)
        self.icon_name = icon_name
        self.setAccessibleName(accessible_name)
        self.setToolTip(accessible_name)
        self.setFixedSize(32, 32)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_icon(self, icon_name, accessible_name=None):
        self.icon_name = icon_name
        if accessible_name:
            self.setAccessibleName(accessible_name)
            self.setToolTip(accessible_name)
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        if self.isDown() or self.underMouse():
            painter.setBrush(COLOR_CONTROL.darker(112) if self.isDown() else COLOR_CONTROL)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(rect, 4, 4)
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(COLOR_FOCUS, 2))
            painter.drawRoundedRect(rect, 4, 4)

        colour = COLOR_TEXT_SECONDARY if self.isEnabled() else COLOR_TEXT_QUIET
        painter.setPen(QPen(colour, 1.7, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap,
                            Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        c = rect.center()
        left, right = c.x() - 7, c.x() + 7
        top, bottom = c.y() - 7, c.y() + 7

        if self.icon_name in {"collapse", "expand"}:
            painter.drawLine(QPointF(left, c.y()), QPointF(right, c.y()))
            if self.icon_name == "expand":
                painter.drawLine(QPointF(c.x(), top), QPointF(c.x(), bottom))
        elif self.icon_name == "close":
            painter.drawLine(QPointF(left + 1, top + 1), QPointF(right - 1, bottom - 1))
            painter.drawLine(QPointF(right - 1, top + 1), QPointF(left + 1, bottom - 1))
        elif self.icon_name == "more":
            painter.setBrush(colour)
            painter.setPen(Qt.PenStyle.NoPen)
            for offset in (-5, 0, 5):
                painter.drawEllipse(QPointF(c.x() + offset, c.y()), 1.4, 1.4)
        elif self.icon_name == "settings":
            for y, knob in ((top + 2, c.x() - 3), (c.y(), c.x() + 4),
                            (bottom - 2, c.x() - 1)):
                painter.drawLine(QPointF(left, y), QPointF(right, y))
                painter.setBrush(COLOR_RAISED)
                painter.drawEllipse(QPointF(knob, y), 2.2, 2.2)
                painter.setBrush(Qt.BrushStyle.NoBrush)
        elif self.icon_name == "recovery":
            arc_rect = QRectF(c.x() - 6, c.y() - 6, 12, 12)
            painter.drawArc(arc_rect, 30 * 16, 285 * 16)
            painter.drawLine(QPointF(right - 1, top + 3), QPointF(right - 1, top + 8))
            painter.drawLine(QPointF(right - 1, top + 3), QPointF(right - 6, top + 3))
        elif self.icon_name == "review":
            page = QRectF(c.x() - 5.5, c.y() - 7, 11, 14)
            painter.drawRoundedRect(page, 1.5, 1.5)
            for offset in (-3, 0, 3):
                painter.drawLine(QPointF(c.x() - 3, c.y() + offset),
                                 QPointF(c.x() + (0 if offset == 3 else 3), c.y() + offset))
        elif self.icon_name == "study":
            path = QPainterPath()
            path.moveTo(c.x() - 6, top)
            path.lineTo(c.x(), top + 2)
            path.lineTo(c.x() + 6, top)
            path.lineTo(c.x() + 6, bottom)
            path.lineTo(c.x(), bottom - 2)
            path.lineTo(c.x() - 6, bottom)
            path.closeSubpath()
            painter.drawPath(path)
            painter.drawLine(QPointF(c.x(), top + 2), QPointF(c.x(), bottom - 2))
        painter.end()


class DisclosurePanel(QFrame):
    """Keyboard-operable progressive-disclosure section."""

    def __init__(self, title, expanded=False, parent=None):
        super().__init__(parent)
        self.title = title
        self.setObjectName("disclosurePanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.toggle = QPushButton()
        self.toggle.setObjectName("disclosure")
        self.toggle.setCheckable(True)
        self.toggle.setChecked(bool(expanded))
        self.toggle.setAccessibleName(f"{title} settings")
        self.toggle.clicked.connect(self._apply_state)
        layout.addWidget(self.toggle)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(8, 3, 8, 10)
        self.body_layout.setSpacing(8)
        layout.addWidget(self.body)
        self._apply_state()

    def _apply_state(self):
        expanded = self.toggle.isChecked()
        # QPushButton treats ampersands as mnemonic markers. Keep the visible
        # section name literal while the accessible name remains unescaped.
        visible_title = self.title.replace("&", "&&")
        self.toggle.setText(
            ("\u25be  " if expanded else "\u25b8  ") + visible_title
        )
        self.toggle.setAccessibleDescription("Expanded" if expanded else "Collapsed")
        self.body.setVisible(expanded)

    def set_expanded(self, expanded):
        self.toggle.setChecked(bool(expanded))
        self._apply_state()


class WorkspaceScrollArea(QScrollArea):
    """Keep an entire nested editor visible when a workspace is scrolled."""

    def ensureWidgetVisible(self, child, xmargin=50, ymargin=50):
        # QScrollArea can stop after exposing only the leading edge of a child
        # nested inside a splitter. Finish with the smallest adjustment needed
        # to expose the complete editor on short/narrow screens.
        super().ensureWidgetVisible(child, xmargin, ymargin)
        if child is None or not child.isVisible():
            return
        viewport = self.viewport()
        margin = max(0, ymargin)
        top = child.mapTo(viewport, QPoint(0, 0)).y()
        bottom = top + child.height()
        bar = self.verticalScrollBar()
        if child.height() + 2 * margin > viewport.height():
            bar.setValue(bar.value() + top - margin)
        elif bottom > viewport.height() - margin:
            bar.setValue(bar.value() + bottom - (viewport.height() - margin))
        elif top < margin:
            bar.setValue(bar.value() + top - margin)


def labelled_field(label_text, control, help_text=""):
    """Return a responsive vertical label/control row with a real buddy."""
    row = QWidget()
    layout = QVBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(3)
    label = QLabel(label_text)
    label.setObjectName("settingName")
    label.setBuddy(control)
    layout.addWidget(label)
    if help_text:
        help_label = QLabel(help_text)
        help_label.setObjectName("settingHelp")
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        control.setAccessibleDescription(help_text)
    layout.addWidget(control)
    return row


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
        self.piece_style = "outline"
        self.show_coordinates = True
        self.follow_flip = False
        self.orientation_override = None
        # The live page sometimes adds Preview/Lab context below the board.
        # A slightly softer floor prevents those states from forcing a small
        # overlay taller than requested; Review/Studies install their own
        # width-matched minimum in update_workspace_constraints().
        self.setMinimumSize(150, 150)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Analysis chess board")

    def set_display_preferences(self, piece_style="outline", coordinates=True):
        style = piece_style if piece_style in {"outline", "solid"} else "outline"
        changed = style != self.piece_style or bool(coordinates) != self.show_coordinates
        self.piece_style = style
        self.show_coordinates = bool(coordinates)
        if changed:
            self.updateGeometry()
            self.update()

    def set_orientation_mode(self, mode="follow"):
        override = {"white": False, "black": True}.get(str(mode))
        changed = override != self.orientation_override
        self.orientation_override = override
        self.flip = self.follow_flip if override is None else override
        if changed:
            self.clear_selection()
            self.update()

    def set_position(self, grid, side_to_move, flip, fen=""):
        changed = fen and fen != self.fen
        self.grid = list(grid)
        self.side_to_move = side_to_move
        self.follow_flip = bool(flip)
        self.flip = (
            self.follow_flip
            if self.orientation_override is None
            else self.orientation_override
        )
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
        if not self.show_coordinates:
            return 0.0
        return max(10.0, min(self.width(), self.height()) * 0.042)

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
        key = (self.piece_style, piece, round(size))
        cached = self._path_cache.get(key, "miss")

        if cached != "miss":
            return cached

        font = QFont(self.piece_family)
        font.setPixelSize(max(8, int(size)))

        path = QPainterPath()
        glyphs = GLYPH_OUTLINE if self.piece_style == "outline" else GLYPH_SOLID
        path.addText(0.0, 0.0, font, glyphs[piece])
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

    def draw_piece_path(self, painter, path, piece, size):
        """Paint one side-coloured piece without changing visual weight.

        The Unicode outline glyphs are already paths made of thin strokes.
        Giving those paths a high-contrast foreground edge visually reverses
        the sides at small sizes (black looks parchment and white looks ink).
        Draw a restrained contrast halo *behind* the path, then paint every
        foreground pixel in the actual side colour instead.
        """
        side_colour = QColor(
            COLOR_PIECE_WHITE if piece.isupper() else COLOR_PIECE_BLACK
        )
        if self.piece_style == "outline":
            halo = QColor(COLOR_BG if piece.isupper() else "#eee3d0")
            halo.setAlpha(145 if piece.isupper() else 105)
            painter.setBrush(QBrush(halo))
            painter.setPen(QPen(
                halo, max(1.8, size * 0.052),
                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            ))
            painter.drawPath(path)
            painter.setBrush(QBrush(side_colour))
            painter.setPen(QPen(
                side_colour, max(0.75, size * 0.017),
                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            ))
            painter.drawPath(path)
            return

        edge_colour = QColor(
            COLOR_PIECE_EDGE if piece.isupper() else "#eee3d0"
        )
        if not piece.isupper():
            edge_colour.setAlpha(55)
        painter.setPen(QPen(
            edge_colour, max(0.9, size * 0.038),
            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        ))
        painter.setBrush(QBrush(side_colour))
        painter.drawPath(path)

    def draw_arrow(self, painter, move, colour, board, marker_kind,
                   lateral_offset=0.0):
        """Draw a restrained arrow that leaves both involved pieces readable.

        Stockfish and Maia share exactly the same geometry.  A square/diamond
        origin marker and the colour carry source identity; a dark halo keeps
        the shaft legible on both board colours.
        """
        if len(move) < 4:
            return

        origin = square_index(move[0:2])
        target = square_index(move[2:4])

        if origin is None or target is None or origin == target:
            return

        start = self.square_center(origin, board)
        end = self.square_center(target, board)

        size = board.width() / 8.0
        shaft = max(2.4, size * 0.085)
        head = size * 0.265

        dx = end.x() - start.x()
        dy = end.y() - start.y()
        length = math.hypot(dx, dy)

        if length < 1.0:
            return

        ux, uy = dx / length, dy / length
        px, py = -uy, ux

        # Start beyond the origin-piece centre and stop well before the target
        # centre.  Parallel sources shift a few pixels in opposite directions.
        start = QPointF(
            start.x() + ux * size * 0.29 + px * size * lateral_offset,
            start.y() + uy * size * 0.29 + py * size * lateral_offset,
        )
        tip = QPointF(
            end.x() - ux * size * 0.30 + px * size * lateral_offset,
            end.y() - uy * size * 0.30 + py * size * lateral_offset,
        )
        base = QPointF(tip.x() - ux * head, tip.y() - uy * head)
        wing_a = QPointF(base.x() - uy * head * 0.52,
                         base.y() + ux * head * 0.52)
        wing_b = QPointF(base.x() + uy * head * 0.52,
                         base.y() - ux * head * 0.52)
        head_polygon = QPolygonF([tip, wing_a, wing_b])

        halo = QColor(COLOR_BG)
        halo.setAlpha(150)
        painter.setPen(QPen(halo, shaft + max(2.3, size * 0.05),
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(start, base)
        halo_path = QPainterPath()
        halo_path.addPolygon(head_polygon)
        painter.setPen(QPen(halo, max(2.0, size * 0.05),
                            Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap,
                            Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(halo)
        painter.drawPath(halo_path)

        painter.setPen(QPen(colour, shaft, Qt.PenStyle.SolidLine,
                            Qt.PenCapStyle.RoundCap))
        painter.drawLine(start, base)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(colour))
        painter.drawPolygon(head_polygon)

        marker_size = max(3.2, size * 0.115)
        painter.setPen(QPen(halo, max(1.0, size * 0.025)))
        painter.setBrush(colour)
        if marker_kind == "maia":
            painter.drawPolygon(QPolygonF([
                QPointF(start.x(), start.y() - marker_size),
                QPointF(start.x() + marker_size, start.y()),
                QPointF(start.x(), start.y() + marker_size),
                QPointF(start.x() - marker_size, start.y()),
            ]))
        else:
            painter.drawRect(QRectF(start.x() - marker_size * 0.72,
                                    start.y() - marker_size * 0.72,
                                    marker_size * 1.44, marker_size * 1.44))

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

        # Coordinates remain outside the playing grid and can be hidden for
        # very small overlays without changing the eight-by-eight geometry.
        if self.show_coordinates:
            coord_font = QFont(QApplication.font())
            coord_font.setPixelSize(max(8, int(self.gutter() * 0.76)))
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
            self.draw_piece_path(painter, path, piece, size)
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
                    self.draw_piece_path(painter, path, piece, size)
                    painter.restore()

        # Arrows last, so they read on top of the pieces.
        human = QColor(COLOR_HUMAN)
        human.setAlpha(205)
        best = QColor(COLOR_BEST)
        best.setAlpha(225)

        shared_origin = (
            len(self.human_move) >= 4 and len(self.best_move) >= 4
            and self.human_move[:2] == self.best_move[:2]
            and self.human_move != self.best_move
        )
        if self.human_move and self.human_move != self.best_move:
            self.draw_arrow(
                painter, self.human_move, human, board, "maia",
                0.065 if shared_origin else 0.0,
            )

        self.draw_arrow(
            painter, self.best_move, best, board, "stockfish",
            -0.065 if shared_origin else 0.0,
        )

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
        self.reduced_motion = False
        self.setFixedHeight(24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Position evaluation")

    def set_reduced_motion(self, reduced):
        self.reduced_motion = bool(reduced)
        if self.reduced_motion:
            self.display_fraction = self.target_fraction
            self.update()

    def set_eval(self, centipawns, mate, depth, has_eval, bound="exact"):
        self.centipawns = centipawns
        self.mate = mate
        self.depth = depth
        self.has_eval = has_eval
        self.bound = str(bound or "exact")

        if has_eval:
            self.target_fraction = win_fraction(centipawns, mate)
            if self.reduced_motion:
                self.display_fraction = self.target_fraction
        shown = format_line_score(
            {"bound": self.bound}, centipawns, mate, "white", "w"
        ) if has_eval else "Analyzing"
        depth_text = f", depth {depth}" if depth else ""
        self.setAccessibleDescription(f"White point of view {shown}{depth_text}")

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
        if self.reduced_motion:
            if self.display_fraction != self.target_fraction:
                self.display_fraction = self.target_fraction
                return True
            return False
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
        radius = 4.0

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
        self.points = []
        self.current = -1
        self.hovered = -1
        self.setMinimumHeight(74)
        self.setMaximumHeight(110)
        self.setAccessibleName("Game evaluation graph")
        self.setAccessibleDescription(
            "White is above the centre line and Black is below. Use Left, "
            "Right, Home, or End to choose a position."
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)

    def set_values(self, values, current=-1, points=None):
        self.values = list(values)
        self.points = list(points or [])
        self.current = current
        self.hovered = -1
        self.update()

    def point_description(self, index):
        if not 0 <= index < len(self.values):
            return ""
        score = self.values[index] / 100.0
        point = self.points[index] if index < len(self.points) else {}
        if index == 0:
            return f"Initial position · evaluation {score:+.2f}"
        san = str(point.get("san", "") or f"position {index}")
        classification = str(point.get("classification", "") or "Unclassified")
        try:
            loss = float(point.get("loss", 0)) / 100.0
        except (TypeError, ValueError):
            loss = 0.0
        return (
            f"Move {index}: {san} · {classification} · "
            f"evaluation {score:+.2f} · loss {loss:.2f}"
        )

    def set_current(self, current):
        self.current = current
        if 0 <= current < len(self.values):
            self.setAccessibleDescription(
                "Selected " + self.point_description(current)
                + ". Use Left or Right to move."
            )
        self.update()

    @staticmethod
    def normalise(value):
        return math.tanh(max(-2000, min(2000, value)) / 400.0)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bounds = self.rect().adjusted(4, 4, -4, -4)
        top_half = QRectF(bounds.left(), bounds.top(), bounds.width(), bounds.height() / 2)
        bottom_half = QRectF(bounds.left(), bounds.center().y(), bounds.width(), bounds.height() / 2)
        painter.fillRect(top_half, QColor("#222622"))
        painter.fillRect(bottom_half, QColor("#191b19"))
        middle = bounds.center().y()
        painter.setPen(QPen(COLOR_LINE_STRONG, 1))
        painter.drawLine(bounds.left(), middle, bounds.right(), middle)
        label_font = QFont(QApplication.font())
        label_font.setPixelSize(9)
        painter.setFont(label_font)
        painter.setPen(COLOR_TEXT_QUIET)
        painter.drawText(bounds.adjusted(5, 2, -5, -2),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, "WHITE")
        painter.drawText(bounds.adjusted(5, 2, -5, -2),
                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom, "BLACK")
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
            value = self.values[self.current] / 100.0
            painter.setPen(COLOR_TEXT)
            painter.drawText(bounds.adjusted(5, 2, -5, -2),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop,
                             f"{value:+.2f}")
        if 0 <= self.hovered < len(self.values) and self.hovered != self.current:
            x = bounds.left() + bounds.width() * self.hovered / denominator
            hover_pen = QPen(COLOR_TEXT_QUIET, 1)
            hover_pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(hover_pen)
            painter.drawLine(int(x), bounds.top(), int(x), bounds.bottom())
        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(COLOR_FOCUS, 2))
            painter.drawRect(bounds.adjusted(1, 1, -1, -1))
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.values:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            bounds = self.rect().adjusted(4, 4, -4, -4)
            fraction = (event.position().x() - bounds.left()) / max(1, bounds.width())
            index = round(max(0.0, min(1.0, fraction)) * (len(self.values) - 1))
            self.selected.emit(index)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not self.values:
            super().mouseMoveEvent(event)
            return
        bounds = self.rect().adjusted(4, 4, -4, -4)
        fraction = (event.position().x() - bounds.left()) / max(1, bounds.width())
        index = round(max(0.0, min(1.0, fraction)) * (len(self.values) - 1))
        description = self.point_description(index)
        if index != self.hovered:
            self.hovered = index
            self.setToolTip(description)
            self.setAccessibleDescription(
                "Hovering " + description + ". Click to select this position."
            )
            self.update()
        QToolTip.showText(event.globalPosition().toPoint(), description, self)
        event.accept()

    def leaveEvent(self, event):
        self.hovered = -1
        QToolTip.hideText()
        if 0 <= self.current < len(self.values):
            self.set_current(self.current)
        self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if not self.values:
            super().keyPressEvent(event)
            return
        key = event.key()
        current = self.current if 0 <= self.current < len(self.values) else 0
        if key == Qt.Key.Key_Left:
            current = max(0, current - 1)
        elif key == Qt.Key.Key_Right:
            current = min(len(self.values) - 1, current + 1)
        elif key == Qt.Key.Key_Home:
            current = 0
        elif key == Qt.Key.Key_End:
            current = len(self.values) - 1
        else:
            super().keyPressEvent(event)
            return
        self.selected.emit(current)
        event.accept()


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
        self.setMinimumHeight(42)
        self.setObjectName("titleBar")

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

        if (
            hasattr(window, "can_toggle_compact")
            and window.can_toggle_compact()
        ):
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
        self.piece_style = "outline"
        self.show_coordinates = True
        self.workspace_orientation = "follow"
        self.always_on_top = True
        self.remember_compact = False
        self.reduced_motion = False
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
        self.review_generation = 0
        self.review_active_identity = None
        self.review_cancelling = False
        self.review_job_record = None
        self.review_job_settings = None
        self.review_settings_used = None
        self.review_game_id = None
        self.completed_game_id = None
        self.completed_game_saved = False
        self.live_game_record = None
        self.completed_game_record = None
        self.auto_save_completed = False
        self.review_visible_rows = []
        self.review_selected_ply = -1
        self.review_mode = "game"
        self.review_branch = []
        self.review_branch_root = ""
        self.review_position_job = None
        self.review_position_queue = None
        self.review_position_generation = 0
        self.review_position_lines = []
        self.review_store = None
        self.review_store_error = ""
        if study_store is not None:
            try:
                self.review_store = study_store.ReviewStore()
            except (OSError, ValueError) as error:
                # Migration can fail closed before the UI exists (for example,
                # when a data directory aliases the managed runtime).  Live
                # analysis remains usable; library actions explain the block.
                self.review_store_error = str(error)
        self.study_auto_analyse = True
        self.study_save_evals = True
        self.current_study = None
        self.current_study_id = None
        self.study_node_id = None
        self.study_tree_refreshing = False
        self.study_annotation_loading = False
        self.study_dirty = False
        self.study_save_failed = False
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
        self.postgame_visible = False
        self.settings_undo_snapshot = None
        self.workspace_geometry = {}

        self.setWindowTitle(f"ChessListener {APP_VERSION}")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.always_on_top)

        if not DECORATED:
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)

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

        self.study_autosave_timer = QTimer(self)
        self.study_autosave_timer.setSingleShot(True)
        self.study_autosave_timer.setInterval(650)
        self.study_autosave_timer.timeout.connect(self.flush_study_edits)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.title_bar = self.build_title_bar()
        root.addWidget(self.title_bar)

        self.stack = PageStack()
        self.stack.currentChanged.connect(self.update_page_chrome)
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

        # Annotation text is local-first: copy it into the in-memory study on
        # every keystroke and atomically flush after a short idle period.
        self.study_title_edit.textChanged.connect(self.queue_study_autosave)
        self.study_name_edit.textChanged.connect(self.queue_study_autosave)
        self.study_comment_edit.textChanged.connect(self.queue_study_autosave)

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
        if self.review_store_error:
            self.report_library_error(self.review_store_error)
        else:
            self.restore_review_archive()
        self.update_page_chrome()
        self.resize(360, 410)

    # -- styling ----------------------------------------------------------

    def apply_style(self):
        # One stylesheet, set once, on the top-level widget. Nothing in the hot
        # path ever calls setStyleSheet again.
        self.setStyleSheet(
            f"""
            QWidget {{
                background: {COLOR_BG.name()};
                color: {COLOR_TEXT.name()};
                font-size: 13px;
            }}
            QWidget#titleBar {{
                background: {COLOR_RAISED.name()};
                border-bottom: 1px solid {COLOR_LINE.name()};
            }}
            QLabel, QCheckBox {{ background: transparent; }}
            QLabel#title {{ font-size: 22px; font-weight: 600; }}
            QLabel#heading {{ font-size: 14px; font-weight: 600; }}
            QLabel#pageTitle {{ color: {COLOR_TEXT.name()}; font-size: 12px; font-weight: 600; }}
            QLabel#subtitle, QLabel#helper, QLabel#settingHelp {{
                color: {COLOR_TEXT_SECONDARY.name()};
            }}
            QLabel#settingHelp {{ font-size: 11px; }}
            QLabel#settingName {{ color: {COLOR_TEXT.name()}; font-weight: 500; }}
            QLabel#eyebrow {{
                color: {COLOR_TEXT_QUIET.name()}; font-size: 10px;
                font-weight: 600;
            }}
            QLabel#statusInfo {{ color: {COLOR_TEXT_QUIET.name()}; font-size: 10px; }}
            QLabel#statusWarn {{ color: {COLOR_WARNING.name()}; font-size: 10px; }}
            QLabel#sourceExact {{
                background: #23392b; color: #a8d8b6; border: 1px solid #476b52;
                border-radius: 3px; font-size: 9px; font-weight: 600; padding: 1px 5px;
            }}
            QLabel#sourceInferred {{
                background: #423b22; color: #e7d181; border: 1px solid #6e6337;
                border-radius: 3px; font-size: 9px; font-weight: 600; padding: 1px 5px;
            }}
            QLabel#sourceManual {{
                background: #22364a; color: #c8def3; border: 1px solid #3f6282;
                border-radius: 3px; font-size: 9px; font-weight: 600; padding: 1px 5px;
            }}
            QLabel#sourceSyncing {{
                background: #473321; color: #efbf89; border: 1px solid #765838;
                border-radius: 3px; font-size: 9px; font-weight: 600; padding: 1px 5px;
            }}
            QLabel#sourceExplore {{
                background: #473321; color: #efbf89; border: 1px solid #765838;
                border-radius: 3px; font-size: 9px; font-weight: 600; padding: 1px 5px;
            }}
            QLabel#sourcePreview {{
                background: #423b22; color: #e7d181; border: 1px solid #6e6337;
                border-radius: 3px; font-size: 9px; font-weight: 600; padding: 1px 5px;
            }}
            QLabel#sourceFinal {{
                background: #242824; color: #cbd0c8; border: 1px solid #535b52;
                border-radius: 3px; font-size: 9px; font-weight: 600; padding: 1px 5px;
            }}
            QLabel#engineLine {{ color: {COLOR_TEXT_SECONDARY.name()}; }}
            QLabel#pvLine {{ color: {COLOR_TEXT_QUIET.name()}; }}
            QLabel#breadcrumb {{ color: {COLOR_TEXT_SECONDARY.name()}; font-size: 11px; }}
            QLabel#explanation {{
                color: {COLOR_TEXT_SECONDARY.name()}; background: transparent;
                border-left: 2px solid {COLOR_BEST.name()}; padding: 2px 2px 2px 9px;
            }}
            QLabel#analysisState {{ color: {COLOR_TEXT_QUIET.name()}; font-size: 11px; }}
            QLabel#recoveryError {{
                color: #efa7aa; background: #2b1d1f; border-left: 2px solid {COLOR_DANGER.name()};
                padding: 6px 8px;
            }}
            QLabel#saveState {{ color: {COLOR_TEXT_QUIET.name()}; font-size: 11px; }}
            QLabel#saveState[savePending="true"] {{ color: {COLOR_WARNING.name()}; }}
            QLabel#saveState[saveFailed="true"] {{
                color: #efa7aa; border-left: 2px solid {COLOR_DANGER.name()};
                padding-left: 7px;
            }}
            QFrame#contextRibbon {{
                background: #2c241a; border: 1px solid #5b472f;
                border-radius: 4px;
            }}
            QFrame#postGamePanel {{
                background: transparent; border-top: 1px solid {COLOR_LINE.name()};
                border-bottom: 1px solid {COLOR_LINE.name()};
            }}
            QFrame#candidateRow {{
                background: transparent; border: 0;
                border-bottom: 1px solid {COLOR_LINE.name()};
            }}
            QFrame#candidateRow:hover {{ background: {COLOR_RAISED.name()}; }}
            QFrame#candidateRow[selected="true"] {{
                background: #22364a; border-left: 2px solid {COLOR_BEST.name()};
                border-bottom: 1px solid {COLOR_LINE.name()};
            }}
            QFrame#candidateRow:focus {{ border: 2px solid {COLOR_FOCUS.name()}; }}
            QLabel#candidateMove, QLabel#candidateScore {{ color: {COLOR_TEXT.name()}; font-weight: 600; }}
            QLabel#candidateDepth, QLabel#candidatePv {{ color: {COLOR_TEXT_QUIET.name()}; }}
            QFrame#panel {{
                background: {COLOR_PANEL.name()};
                border: 1px solid {COLOR_LINE.name()}; border-radius: 8px;
            }}
            QFrame#disclosurePanel {{
                background: transparent; border-top: 1px solid {COLOR_LINE.name()};
            }}
            QComboBox, QSpinBox, ScrollSafeComboBox, ScrollSafeSpinBox, QLineEdit, QTextEdit {{
                background: #181b18; border: 1px solid {COLOR_LINE_STRONG.name()};
                border-radius: 5px; padding: 5px 6px 5px 8px; min-height: 24px;
            }}
            QComboBox:focus, QSpinBox:focus, ScrollSafeComboBox:focus, ScrollSafeSpinBox:focus, QLineEdit:focus, QTextEdit:focus {{
                border: 2px solid {COLOR_FOCUS.name()};
            }}
            QLineEdit[invalid="true"] {{
                border: 2px solid {COLOR_DANGER.name()}; background: #261b1c;
            }}
            QComboBox QAbstractItemView {{
                background: #181b18; border: 1px solid {COLOR_LINE_STRONG.name()};
                selection-background-color: #22364a;
            }}
            QPushButton {{
                background: {COLOR_CONTROL.name()}; border: 1px solid {COLOR_LINE_STRONG.name()};
                border-radius: 5px; color: {COLOR_TEXT_SECONDARY.name()};
                font-weight: 500; padding: 6px 10px; min-height: 24px;
            }}
            QPushButton:hover {{ background: #2b302b; color: {COLOR_TEXT.name()}; border-color: #687266; }}
            QPushButton:pressed {{ background: #1a1d1a; }}
            QPushButton:focus {{ border: 2px solid {COLOR_FOCUS.name()}; }}
            QPushButton:disabled {{ color: #6f776e; border-color: #343934; background: #1a1d1a; }}
            QPushButton#primary {{
                background: {COLOR_BEST.name()}; border-color: {COLOR_BEST.name()};
                color: #101820; font-weight: 600; min-height: 26px;
            }}
            QPushButton#primary:hover {{ background: #6b9bd1; }}
            QPushButton#primary:disabled {{
                color: #777f77; border-color: #3b423b; background: #242824;
            }}
            QPushButton#ghost {{ background: transparent; }}
            QPushButton#tinyGhost {{ background: transparent; padding: 4px 7px; }}
            QPushButton#danger {{
                background: transparent; border-color: #7b4247; color: #efa7aa;
            }}
            QPushButton#liveBadge {{
                background: #473321; color: #efbf89; border: 1px solid #765838;
                border-radius: 4px; padding: 3px 7px; font-weight: 600;
            }}
            QPushButton#disclosure {{
                background: transparent; border: 0; border-radius: 0;
                color: {COLOR_TEXT_SECONDARY.name()}; text-align: left;
                padding: 9px 2px; min-height: 20px;
            }}
            QPushButton#disclosure:hover {{ background: {COLOR_RAISED.name()}; color: {COLOR_TEXT.name()}; }}
            QLabel#lastLine {{ color: {COLOR_LAST.name()}; }}
            QCheckBox {{ spacing: 8px; color: {COLOR_TEXT_SECONDARY.name()}; min-height: 24px; }}
            QCheckBox::indicator {{ width: 15px; height: 15px; border: 1px solid {COLOR_LINE_STRONG.name()}; border-radius: 3px; background: #181b18; }}
            QCheckBox::indicator:checked {{ background: {COLOR_BEST.name()}; border-color: {COLOR_BEST.name()}; }}
            QCheckBox:focus {{ color: {COLOR_TEXT.name()}; }}
            QScrollArea {{ background: transparent; border: 0; }}
            QScrollBar:vertical {{ background: transparent; width: 9px; margin: 1px; }}
            QScrollBar::handle:vertical {{ background: #4b534a; min-height: 24px; border-radius: 4px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar:horizontal {{ height: 0px; }}
            QProgressBar {{
                background: #181b18; border: 1px solid {COLOR_LINE.name()};
                border-radius: 3px; text-align: center; color: {COLOR_TEXT_SECONDARY.name()};
                min-height: 17px;
            }}
            QProgressBar::chunk {{ background: {COLOR_BEST.name()}; border-radius: 2px; }}
            QListWidget, QTreeWidget {{
                background: #181b18; border: 1px solid {COLOR_LINE.name()};
                border-radius: 5px; alternate-background-color: #1c1f1c;
                selection-background-color: #22364a; padding: 2px;
            }}
            QListWidget::item, QTreeWidget::item {{ padding: 5px; border-bottom: 1px solid #2e332e; }}
            QListWidget::item:selected, QTreeWidget::item:selected {{ border-left: 2px solid {COLOR_BEST.name()}; }}
            QHeaderView::section {{ background: #1c1f1c; color: {COLOR_TEXT_QUIET.name()}; border: 0; border-bottom: 1px solid {COLOR_LINE.name()}; padding: 5px; }}
            QSplitter::handle {{ background: {COLOR_LINE.name()}; width: 1px; height: 1px; }}
            """
        )

    # -- pages ------------------------------------------------------------

    def build_title_bar(self):
        bar = TitleBar()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(8, 4, 4, 4)
        layout.setSpacing(4)

        self.brand_mark = BrandMark()
        layout.addWidget(self.brand_mark)

        title_copy = QWidget()
        title_layout = QVBoxLayout(title_copy)
        title_layout.setContentsMargins(2, 0, 2, 0)
        title_layout.setSpacing(0)
        self.page_title_label = QLabel("ChessListener")
        self.page_title_label.setObjectName("pageTitle")
        self.page_title_label.setMinimumHeight(14)
        self.page_title_label.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )
        title_layout.addWidget(self.page_title_label)
        self.status_label = QLabel("")
        self.status_label.setObjectName("statusInfo")
        self.status_label.setMinimumHeight(11)
        self.status_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        title_layout.addWidget(self.status_label)
        layout.addWidget(title_copy, 1)

        self.source_label = QLabel("")
        self.source_label.setMinimumHeight(16)
        self.source_label.hide()
        layout.addWidget(self.source_label)

        self.turn_dot = TurnDot()
        layout.addWidget(self.turn_dot)

        self.compact_button = self.make_title_button("collapse", "Compact live analysis")
        self.compact_button.clicked.connect(self.toggle_compact)
        layout.addWidget(self.compact_button)

        self.recovery_button = self.make_title_button("recovery", "Open Recovery")
        self.recovery_button.clicked.connect(self.toggle_recovery)
        self.recovery_button.setEnabled(False)
        layout.addWidget(self.recovery_button)

        self.review_button = self.make_title_button("review", "Open Local Review")
        self.review_button.clicked.connect(self.toggle_review)
        self.review_button.setEnabled(True)
        layout.addWidget(self.review_button)

        self.study_button = self.make_title_button("study", "Open Saved Studies")
        self.study_button.clicked.connect(self.toggle_study)
        self.study_button.setEnabled(study_rules is not None and self.review_store is not None)
        layout.addWidget(self.study_button)

        self.settings_button = self.make_title_button("settings", "Open Settings")
        self.settings_button.clicked.connect(self.toggle_settings)
        layout.addWidget(self.settings_button)

        self.overflow_button = self.make_title_button("more", "More destinations")
        self.overflow_button.clicked.connect(self.show_title_overflow)
        layout.addWidget(self.overflow_button)

        self.close_button = None
        if not DECORATED:
            self.close_button = self.make_title_button("close", "Close ChessListener")
            self.close_button.clicked.connect(self.close)
            layout.addWidget(self.close_button)

        self.turn_dot.hide()
        self.compact_button.hide()
        self.recovery_button.hide()
        self.review_button.hide()
        self.study_button.hide()
        self.settings_button.hide()
        self.overflow_button.hide()
        return bar

    @staticmethod
    def make_title_button(icon_name, tooltip):
        return TitleAction(icon_name, tooltip)

    def show_title_overflow(self):
        menu = QMenu(self)
        page = self.stack.currentWidget() if hasattr(self, "stack") else None
        if page is not self.recovery_page and self.session_active:
            action = menu.addAction("Recovery")
            action.setEnabled(self.recovery_button.isEnabled())
            action.triggered.connect(self.open_recovery)
        if page is not self.review_page:
            action = menu.addAction("Local Review")
            action.triggered.connect(self.open_review)
        if page is not self.study_page:
            action = menu.addAction("Saved Studies")
            action.setEnabled(self.study_button.isEnabled())
            action.triggered.connect(self.open_study)
        if page is not self.settings_page:
            action = menu.addAction("Settings")
            action.triggered.connect(self.open_settings)
        menu.exec(self.overflow_button.mapToGlobal(self.overflow_button.rect().bottomRight()))

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
        layout.setContentsMargins(18, 17, 18, 17)
        layout.setSpacing(10)

        eyebrow = QLabel("LOCAL CHESS ANALYSIS")
        eyebrow.setObjectName("eyebrow")
        layout.addWidget(eyebrow)

        title = QLabel("ChessListener")
        title.setObjectName("title")
        layout.addWidget(title)

        subtitle = QLabel(
            "A small analysis desk for the Chess.com board in front of you. "
            "Games and studies stay on this computer."
        )
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        panel = QFrame()
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(12, 11, 12, 12)
        panel_layout.setSpacing(7)

        self.startup_budget = self.make_budget_combo()
        self.startup_maia = self.make_maia_combo()

        budget_label = QLabel("Analysis strength")
        budget_label.setObjectName("heading")
        budget_label.setBuddy(self.startup_budget)
        panel_layout.addWidget(budget_label)

        self.startup_budget_help = QLabel()
        self.startup_budget_help.setObjectName("helper")
        self.startup_budget_help.setWordWrap(True)
        panel_layout.addWidget(self.startup_budget_help)
        panel_layout.addWidget(self.startup_budget)

        maia_label = QLabel("Maia human-move model")
        maia_label.setObjectName("heading")
        maia_label.setBuddy(self.startup_maia)
        panel_layout.addSpacing(5)
        panel_layout.addWidget(maia_label)

        maia_help = QLabel(
            "Optional. Maia predicts what a player near the chosen rating might "
            "play; Stockfish's evaluation remains independent."
        )
        maia_help.setObjectName("helper")
        maia_help.setWordWrap(True)
        panel_layout.addWidget(maia_help)
        panel_layout.addWidget(self.startup_maia)
        layout.addWidget(panel)

        self.startup_budget.currentIndexChanged.connect(
            self.update_startup_budget_help
        )

        self.remember_check = QCheckBox("Remember these settings")
        self.remember_check.setAccessibleDescription(
            "Store the two startup choices locally for future sessions."
        )
        layout.addWidget(self.remember_check)

        note = QLabel("All of this stays adjustable while analysis is running.")
        note.setObjectName("helper")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.start_button = QPushButton("Start analysis")
        self.start_button.setObjectName("primary")
        self.start_button.setMinimumHeight(40)
        self.start_button.setAccessibleDescription(
            "Start the local engines and begin reading the current browser board."
        )
        self.start_button.clicked.connect(self.start_analysis)
        layout.addWidget(self.start_button)
        layout.addStretch(1)

        return page

    def build_analysis_page(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(9, 3, 9, 8)
        root.setSpacing(6)

        self.analysis_context = QFrame()
        self.analysis_context.setObjectName("contextRibbon")
        context_layout = QHBoxLayout(self.analysis_context)
        context_layout.setContentsMargins(8, 5, 8, 5)
        context_layout.setSpacing(6)
        self.analysis_context_label = QLabel("")
        self.analysis_context_label.setObjectName("helper")
        self.analysis_context_label.setWordWrap(True)
        context_layout.addWidget(self.analysis_context_label, 1)
        self.analysis_context.hide()
        root.addWidget(self.analysis_context)

        self.board = BoardView(self.piece_family)
        self.board.moveRequested.connect(self.handle_board_move_request)
        self.board.interactionHint.connect(self.handle_board_hint)
        root.addWidget(self.board, 1)

        self.game_finished_panel = QFrame()
        self.game_finished_panel.setObjectName("postGamePanel")
        postgame = QVBoxLayout(self.game_finished_panel)
        postgame.setContentsMargins(1, 10, 1, 10)
        postgame.setSpacing(7)
        self.game_finished_title = QLabel("Game finished")
        self.game_finished_title.setObjectName("heading")
        postgame.addWidget(self.game_finished_title)
        self.game_finished_detail = QLabel(
            "Keep the final board here, then choose what you want to do locally."
        )
        self.game_finished_detail.setObjectName("helper")
        self.game_finished_detail.setWordWrap(True)
        postgame.addWidget(self.game_finished_detail)
        postgame_actions = QGridLayout()
        postgame_actions.setContentsMargins(0, 0, 0, 0)
        postgame_actions.setSpacing(6)
        self.postgame_save_button = QPushButton("Save game")
        self.postgame_save_button.setObjectName("primary")
        self.postgame_save_button.setMinimumHeight(40)
        self.postgame_save_button.setAccessibleDescription(
            "Save this completed game once in the local ChessListener library."
        )
        self.postgame_save_button.clicked.connect(self.postgame_save_clicked)
        postgame_actions.addWidget(self.postgame_save_button, 0, 0, 1, 2)
        self.postgame_review_button = QPushButton("Run local review")
        self.postgame_review_button.clicked.connect(
            lambda: self.open_review_for_completed_game(run=True)
        )
        postgame_actions.addWidget(self.postgame_review_button, 1, 0)
        self.postgame_explore_button = QPushButton("Explore final position")
        self.postgame_explore_button.clicked.connect(self.explore_completed_game)
        postgame_actions.addWidget(self.postgame_explore_button, 1, 1)
        self.postgame_export_button = QPushButton("Export PGN")
        self.postgame_export_button.clicked.connect(self.export_completed_game)
        postgame_actions.addWidget(self.postgame_export_button, 2, 0, 1, 2)
        postgame_actions.setColumnStretch(0, 1)
        postgame_actions.setColumnStretch(1, 1)
        postgame.addLayout(postgame_actions)
        self.postgame_auto_save = QCheckBox("Automatically save completed games")
        self.postgame_auto_save.setAccessibleDescription(
            "Save future completed games locally without starting a review."
        )
        self.postgame_auto_save.stateChanged.connect(self.set_auto_save_completed)
        postgame.addWidget(self.postgame_auto_save)
        self.game_finished_panel.hide()
        root.addWidget(self.game_finished_panel)

        self.eval_bar = EvalBar()
        root.addWidget(self.eval_bar)

        self.analysis_state_label = QLabel("Waiting for a board position")
        self.analysis_state_label.setObjectName("analysisState")
        self.analysis_state_label.setAccessibleName("Analysis status")
        root.addWidget(self.analysis_state_label)

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
        # Waiting, streaming, and complete states share one allocation. Rows
        # scroll inside it instead of stealing 100+ px from the board when the
        # first engine result arrives.
        self.candidate_scroll.setFixedHeight(142)

        candidate_host = QWidget()
        candidate_layout = QVBoxLayout(candidate_host)
        candidate_layout.setContentsMargins(0, 0, 0, 0)
        candidate_layout.setSpacing(3)
        self.candidate_placeholder = QLabel(
            "Candidate lines will appear here as soon as Stockfish has a result."
        )
        self.candidate_placeholder.setObjectName("helper")
        self.candidate_placeholder.setWordWrap(True)
        self.candidate_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.candidate_placeholder.setMinimumHeight(50)
        candidate_layout.addWidget(self.candidate_placeholder)
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

        self.preview_label = QLabel("Line 0/0")
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
        explore_controls = QGridLayout(self.explore_toolbar)
        explore_controls.setContentsMargins(0, 0, 0, 0)
        explore_controls.setSpacing(5)
        self.explore_controls = explore_controls
        self.explore_controls_stacked = False

        self.root_button = self.make_small_button("Root", "Return to branch root")
        self.root_button.clicked.connect(self.explore_root)
        explore_controls.addWidget(self.root_button, 0, 0)

        self.undo_button = self.make_small_button("Undo", "Undo one explored move")
        self.undo_button.clicked.connect(self.explore_undo)
        explore_controls.addWidget(self.undo_button, 0, 1)

        self.redo_button = self.make_small_button("Redo", "Redo one explored move")
        self.redo_button.clicked.connect(self.explore_redo)
        explore_controls.addWidget(self.redo_button, 0, 2)

        self.save_lab_button = self.make_small_button(
            "Save locally", "Save this Analysis Lab tree as a local study"
        )
        self.save_lab_button.clicked.connect(self.capture_current_study_prompt)
        explore_controls.addWidget(self.save_lab_button, 0, 3)
        explore_controls.setColumnStretch(4, 1)

        self.live_update_button = QPushButton("")
        self.live_update_button.setObjectName("liveBadge")
        self.live_update_button.setToolTip("The real game changed; return to it")
        self.live_update_button.clicked.connect(self.go_live)
        self.live_update_button.hide()
        explore_controls.addWidget(self.live_update_button, 0, 5)

        self.go_live_button = self.make_small_button("Go Live", "Return to the real game")
        self.go_live_button.clicked.connect(self.go_live)
        explore_controls.addWidget(self.go_live_button, 0, 6)
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
        self.explanation_scroll = QScrollArea()
        self.explanation_scroll.setWidgetResizable(True)
        self.explanation_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.explanation_scroll.setFixedHeight(116)
        self.explanation_scroll.setWidget(self.explanation_label)
        self.explanation_scroll.hide()
        root.addWidget(self.explanation_scroll)

        self.analysis_regular_widgets = (
            self.eval_bar, self.analysis_state_label, self.last_label,
            self.best_label, self.human_label, self.candidate_scroll,
            self.live_toolbar, self.explore_toolbar, self.breadcrumb_label,
            self.explanation_scroll,
        )

        return page

    def build_review_page(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(10, 8, 10, 10)
        root.setSpacing(8)

        header = QGridLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self.review_library_combo = ScrollSafeComboBox()
        self.review_library_combo.setAccessibleName("Saved local game")
        self.review_library_combo.setToolTip("Saved local reviews")
        self.review_library_combo.currentIndexChanged.connect(self.load_library_selection)
        header.addWidget(self.review_library_combo, 0, 0, 1, 2)

        self.review_import_button = self.make_small_button(
            "Import", "Import a local PGN game or FEN position"
        )
        import_menu = QMenu(self.review_import_button)
        import_pgn_action = import_menu.addAction("Import PGN…")
        import_pgn_action.triggered.connect(self.import_review_pgn)
        import_fen_action = import_menu.addAction("Import FEN…")
        import_fen_action.triggered.connect(self.import_review_fen)
        self.review_import_button.setMenu(import_menu)
        header.addWidget(self.review_import_button, 0, 2)
        self.review_close_button = self.make_small_button("Back", "Return to live analysis")
        self.review_close_button.clicked.connect(self.close_review)
        header.addWidget(self.review_close_button, 0, 3)

        self.review_delete_button = self.make_small_button(
            "Delete", "Delete selected saved review"
        )
        self.review_delete_button.setObjectName("danger")
        self.review_delete_button.clicked.connect(self.delete_library_selection)
        self.review_export_button = self.make_small_button(
            "Export PGN", "Export annotated PGN"
        )
        self.review_export_button.clicked.connect(self.export_review_pgn)
        self.review_export_button.setEnabled(False)
        header.addWidget(self.review_export_button, 1, 0)
        header.addWidget(self.review_delete_button, 1, 1)
        header.setColumnStretch(0, 1)
        header.setColumnStretch(1, 1)
        root.addLayout(header)

        self.review_empty_panel = QFrame()
        self.review_empty_panel.setObjectName("panel")
        empty_layout = QVBoxLayout(self.review_empty_panel)
        empty_layout.setContentsMargins(16, 15, 16, 15)
        empty_title = QLabel("Review a game locally")
        empty_title.setObjectName("heading")
        empty_layout.addWidget(empty_title)
        empty_text = QLabel(
            "Import a PGN or complete FEN, or choose a saved game. The board, "
            "graph, explanations, and review stay on this computer."
        )
        empty_text.setObjectName("helper")
        empty_text.setWordWrap(True)
        empty_layout.addWidget(empty_text)
        root.addWidget(self.review_empty_panel)

        self.review_workspace = QSplitter(Qt.Orientation.Vertical)
        self.review_workspace.setChildrenCollapsible(False)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 3, 0)
        left_layout.setSpacing(6)

        self.review_board = BoardView(self.piece_family)
        self.review_board.set_interactive(False)
        self.review_board.moveRequested.connect(self.handle_review_move_request)
        left_layout.addWidget(self.review_board, 1)

        navigator = QHBoxLayout()
        self.review_back_button = self.make_small_button("Back", "Previous reviewed move")
        self.review_back_button.clicked.connect(self.review_back)
        navigator.addWidget(self.review_back_button)
        self.review_forward_button = self.make_small_button("Next", "Next reviewed move")
        self.review_forward_button.clicked.connect(self.review_forward)
        navigator.addWidget(self.review_forward_button)
        self.review_live_button = self.make_small_button("Final position", "Jump to final game position")
        self.review_live_button.clicked.connect(self.review_final)
        navigator.addWidget(self.review_live_button)
        navigator.addStretch(1)
        left_layout.addLayout(navigator)

        self.review_graph = ReviewGraph()
        self.review_graph.selected.connect(self.select_review_ply)
        left_layout.addWidget(self.review_graph)
        self.review_workspace.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(3, 0, 0, 0)
        right_layout.setSpacing(7)

        self.review_summary = QLabel("A verified move history is required.")
        self.review_summary.setObjectName("helper")
        self.review_summary.setWordWrap(True)
        right_layout.addWidget(self.review_summary)

        self.review_progress = QProgressBar()
        self.review_progress.setRange(0, 1)
        self.review_progress.setValue(0)
        self.review_progress.setAccessibleName("Local review progress")
        self.review_progress.hide()
        right_layout.addWidget(self.review_progress)

        self.review_filter = ScrollSafeComboBox()
        for label, value in (
            ("All moves", "all"), ("Turning points", "turning"),
            ("Inaccuracies +", "errors"), ("Blunders only", "major"),
            ("Checks & captures", "forcing"),
        ):
            self.review_filter.addItem(label, value)
        self.review_filter.currentIndexChanged.connect(self.refresh_review_timeline)
        self.review_filter.setAccessibleName("Move filter")
        right_layout.addWidget(self.review_filter)

        controls = QGridLayout()
        controls.setSpacing(6)
        self.review_start_button = QPushButton("Run local review")
        self.review_start_button.setObjectName("primary")
        self.review_start_button.clicked.connect(self.start_game_review)
        controls.addWidget(self.review_start_button, 0, 0, 1, 2)
        self.review_cancel_button = self.make_small_button("Cancel", "Cancel review")
        self.review_cancel_button.clicked.connect(self.cancel_game_review)
        self.review_cancel_button.setEnabled(False)
        controls.addWidget(self.review_cancel_button, 1, 0)
        self.review_explore_button = self.make_small_button(
            "Explore here", "Explore this historical position locally"
        )
        self.review_explore_button.clicked.connect(self.toggle_review_explore)
        self.review_explore_button.setEnabled(False)
        controls.addWidget(self.review_explore_button, 1, 1)
        self.review_undo_button = self.make_small_button("Undo", "Undo explored move")
        self.review_undo_button.clicked.connect(self.review_explore_undo)
        self.review_undo_button.hide()
        controls.addWidget(self.review_undo_button, 2, 0, 1, 2)
        controls.setColumnStretch(0, 1)
        controls.setColumnStretch(1, 1)
        right_layout.addLayout(controls)

        self.review_moves = QListWidget()
        self.review_moves.setAccessibleName("Reviewed moves")
        self.review_moves.currentRowChanged.connect(self.select_filtered_review_move)
        right_layout.addWidget(self.review_moves, 1)

        self.review_detail = QLabel("")
        self.review_detail.setObjectName("explanation")
        self.review_detail.setWordWrap(True)
        right_layout.addWidget(self.review_detail)
        self.review_workspace.addWidget(right)
        self.review_workspace.setStretchFactor(0, 3)
        self.review_workspace.setStretchFactor(1, 2)
        # The stacked workspace is deliberately taller than a small overlay.
        # Scroll it rather than forcing the top-level window to grow or
        # squeezing navigation across the board's first rank.
        self.review_workspace_scroll = WorkspaceScrollArea()
        self.review_workspace_scroll.setWidgetResizable(True)
        self.review_workspace_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.review_workspace_scroll.setWidget(self.review_workspace)
        root.addWidget(self.review_workspace_scroll, 1)
        return page

    def build_study_page(self):
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(10, 8, 10, 10)
        root.setSpacing(8)

        header = QGridLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self.study_search = QLineEdit()
        self.study_search.setPlaceholderText("Search titles, branches, notes")
        self.study_search.setAccessibleName("Search saved studies")
        self.study_search.setClearButtonEnabled(True)
        self.study_search.textChanged.connect(self.filter_study_library)
        header.addWidget(self.study_search, 0, 0, 1, 3)

        self.study_library_combo = ScrollSafeComboBox()
        self.study_library_combo.setAccessibleName("Saved study")
        self.study_library_combo.setToolTip("Saved local variation trees")
        self.study_library_combo.currentIndexChanged.connect(
            self.load_study_library_selection
        )
        header.addWidget(self.study_library_combo, 1, 0, 1, 2)
        self.study_capture_button = self.make_small_button(
            "Save Lab", "Save the current Analysis Lab tree"
        )
        self.study_capture_button.clicked.connect(self.capture_current_study_prompt)
        header.addWidget(self.study_capture_button, 1, 2)
        self.study_new_button = self.make_small_button(
            "New study", "Create a study from the current position"
        )
        self.study_new_button.clicked.connect(self.create_study_prompt)
        header.addWidget(self.study_new_button, 2, 0)
        self.study_export_button = self.make_small_button(
            "Export PGN", "Export the full annotated variation tree as PGN"
        )
        self.study_export_button.clicked.connect(self.export_study_pgn)
        header.addWidget(self.study_export_button, 2, 1)
        self.study_delete_button = self.make_small_button(
            "Delete", "Delete this saved study"
        )
        self.study_delete_button.setObjectName("danger")
        self.study_delete_button.clicked.connect(self.delete_current_study)
        header.addWidget(self.study_delete_button, 2, 2)
        header.setColumnStretch(0, 1)
        header.setColumnStretch(1, 1)
        root.addLayout(header)

        self.study_empty_panel = QFrame()
        self.study_empty_panel.setObjectName("panel")
        study_empty_layout = QVBoxLayout(self.study_empty_panel)
        study_empty_layout.setContentsMargins(16, 15, 16, 15)
        study_empty_title = QLabel("Build a local analysis notebook")
        study_empty_title.setObjectName("heading")
        study_empty_layout.addWidget(study_empty_title)
        study_empty_text = QLabel(
            "Create a study from a position or save an Analysis Lab branch. "
            "Titles, branches, evaluations, and notes remain local."
        )
        study_empty_text.setObjectName("helper")
        study_empty_text.setWordWrap(True)
        study_empty_layout.addWidget(study_empty_text)
        root.addWidget(self.study_empty_panel)

        self.study_workspace = QSplitter(Qt.Orientation.Vertical)
        self.study_workspace.setChildrenCollapsible(False)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 3, 0)
        left_layout.setSpacing(6)

        self.study_board = BoardView(self.piece_family)
        self.study_board.set_interactive(False)
        self.study_board.moveRequested.connect(self.handle_study_move_request)
        self.study_board.interactionHint.connect(self.handle_board_hint)
        left_layout.addWidget(self.study_board, 1)

        navigator = QGridLayout()
        navigator.setContentsMargins(0, 0, 0, 0)
        navigator.setSpacing(6)
        self.study_root_button = self.make_small_button("Root", "Go to study root")
        self.study_root_button.clicked.connect(self.study_go_root)
        navigator.addWidget(self.study_root_button, 0, 0)
        self.study_back_button = self.make_small_button("Parent", "Go to parent position")
        self.study_back_button.clicked.connect(self.study_go_parent)
        navigator.addWidget(self.study_back_button, 0, 1)
        self.study_forward_button = self.make_small_button(
            "Continue", "Go to the first continuation"
        )
        self.study_forward_button.clicked.connect(self.study_go_forward)
        navigator.addWidget(self.study_forward_button, 1, 0)
        self.study_analyse_button = self.make_small_button(
            "Analyze position", "Refresh this position's local Stockfish snapshot"
        )
        self.study_analyse_button.clicked.connect(self.analyse_study_node)
        navigator.addWidget(self.study_analyse_button, 1, 1)
        navigator.setColumnStretch(0, 1)
        navigator.setColumnStretch(1, 1)
        left_layout.addLayout(navigator)
        self.study_workspace.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(3, 0, 0, 0)
        right_layout.setSpacing(7)

        self.study_tree = QTreeWidget()
        self.study_tree.setHeaderLabels(("Variation", "Snapshot"))
        self.study_tree.setAccessibleName("Study variation tree")
        self.study_tree.setColumnWidth(0, 250)
        self.study_tree.setAlternatingRowColors(True)
        self.study_tree.currentItemChanged.connect(self.select_study_tree_item)
        self.study_tree.itemCollapsed.connect(
            lambda item: self.set_study_item_collapsed(item, True)
        )
        self.study_tree.itemExpanded.connect(
            lambda item: self.set_study_item_collapsed(item, False)
        )
        right_layout.addWidget(self.study_tree, 1)

        self.study_title_edit = QLineEdit()
        self.study_title_edit.setMaxLength(120)
        self.study_title_edit.setAccessibleName("Study title")
        right_layout.addWidget(labelled_field("Study title", self.study_title_edit))

        self.study_name_edit = QLineEdit()
        self.study_name_edit.setMaxLength(120)
        self.study_name_edit.setAccessibleName("Selected branch name")
        right_layout.addWidget(labelled_field(
            "Selected branch name", self.study_name_edit, "Optional"
        ))

        self.study_comment_edit = QTextEdit()
        self.study_comment_edit.setAcceptRichText(False)
        self.study_comment_edit.setAccessibleName("Position note")
        self.study_comment_edit.setMaximumHeight(92)
        right_layout.addWidget(labelled_field("Position note", self.study_comment_edit))

        save_row = QHBoxLayout()
        self.study_save_state_label = QLabel("Saved locally")
        self.study_save_state_label.setObjectName("saveState")
        self.study_save_state_label.setAccessibleName("Study save status")
        self.study_save_state_label.setWordWrap(True)
        self.study_save_state_label.setMinimumWidth(0)
        self.study_save_state_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        save_row.addWidget(self.study_save_state_label, 1)
        self.study_save_note_button = self.make_small_button(
            "Save now", "Save title, branch name, and position note now"
        )
        self.study_save_note_button.clicked.connect(self.save_study_annotation)
        save_row.addWidget(self.study_save_note_button)
        right_layout.addLayout(save_row)

        self.study_detail = QLabel(
            "Save an Analysis Lab tree or create a study from any position."
        )
        self.study_detail.setObjectName("explanation")
        self.study_detail.setWordWrap(True)
        right_layout.addWidget(self.study_detail)
        self.study_workspace.addWidget(right)
        self.study_workspace.setStretchFactor(0, 3)
        self.study_workspace.setStretchFactor(1, 2)
        self.study_workspace_scroll = WorkspaceScrollArea()
        self.study_workspace_scroll.setWidgetResizable(True)
        self.study_workspace_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.study_workspace_scroll.setWidget(self.study_workspace)
        root.addWidget(self.study_workspace_scroll, 1)
        return page

    def build_settings_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        lead = QLabel("Everyday choices first; specialist controls stay available below.")
        lead.setObjectName("helper")
        lead.setWordWrap(True)
        layout.addWidget(lead)

        # Copy gets the full width at every size. This avoids the explanatory
        # sentence collapsing into a tall word-column beside the actions.
        header = QHBoxLayout()
        header.addStretch(1)

        self.reset_settings_button = QPushButton("Reset defaults")
        self.reset_settings_button.setObjectName("ghost")
        self.reset_settings_button.clicked.connect(self.reset_settings_defaults)
        header.addWidget(self.reset_settings_button)

        self.settings_close_button = QPushButton("Live")
        self.settings_close_button.setObjectName("ghost")
        self.settings_close_button.clicked.connect(self.close_settings)
        header.addWidget(self.settings_close_button)
        layout.addLayout(header)

        self.live_budget = self.make_budget_combo()
        self.live_maia = self.make_maia_combo()
        self.live_threads = ScrollSafeSpinBox()
        self.live_threads.setRange(1, max(1, os.cpu_count() or 1))
        self.live_multipv = ScrollSafeSpinBox()
        self.live_multipv.setRange(1, 5)

        self.live_explore_budget = ScrollSafeComboBox()
        self.live_explore_budget.addItem("Same as live", -1)
        for name, milliseconds, description in BUDGET_PRESETS:
            label = name if milliseconds == 0 else f"{name} · {milliseconds}ms"
            self.live_explore_budget.addItem(label, milliseconds)
            self.live_explore_budget.setItemData(
                self.live_explore_budget.count() - 1,
                description,
                Qt.ItemDataRole.ToolTipRole,
            )

        self.live_pv_length = ScrollSafeComboBox()
        for value, label in PV_LENGTH_CHOICES:
            self.live_pv_length.addItem(label, value)

        self.live_follow = ScrollSafeComboBox()
        for value, label in FOLLOW_LIVE_CHOICES:
            self.live_follow.addItem(label, value)

        self.live_explanation = ScrollSafeComboBox()
        for value, label in EXPLANATION_CHOICES:
            self.live_explanation.addItem(label, value)

        self.live_eval_pov = ScrollSafeComboBox()
        for value, label in EVAL_POV_CHOICES:
            self.live_eval_pov.addItem(label, value)

        self.live_line_expansion = ScrollSafeComboBox()
        for value, label in LINE_EXPANSION_CHOICES:
            self.live_line_expansion.addItem(label, value)

        self.live_opacity = ScrollSafeComboBox()
        for percent, label in OPACITY_CHOICES:
            self.live_opacity.addItem(label, percent)

        self.live_review_strength = ScrollSafeComboBox()
        for label, milliseconds in (
            ("Quick · 150ms/position", 150),
            ("Balanced · 350ms/position", 350),
            ("Deep · 800ms/position", 800),
            ("Maximum · 1800ms/position", 1800),
        ):
            self.live_review_strength.addItem(label, milliseconds)
        self.live_review_lines = ScrollSafeSpinBox()
        self.live_review_lines.setRange(1, 5)
        self.live_auto_save_completed = QCheckBox(
            "Auto-save completed games"
        )
        self.live_review_auto = QCheckBox(
            "Auto-review completed games"
        )
        self.live_review_sensitivity = ScrollSafeComboBox()
        self.live_review_sensitivity.addItem("Strict", "strict")
        self.live_review_sensitivity.addItem("Standard", "standard")
        self.live_review_sensitivity.addItem("Lenient", "lenient")
        self.live_study_auto = QCheckBox("Analyze selected study positions")
        self.live_study_snapshots = QCheckBox("Save study evaluation snapshots")

        self.live_piece_style = ScrollSafeComboBox()
        self.live_piece_style.addItem("Outline set", "outline")
        self.live_piece_style.addItem("Solid silhouettes", "solid")
        self.live_workspace_orientation = ScrollSafeComboBox()
        for value, label in WORKSPACE_ORIENTATION_CHOICES:
            self.live_workspace_orientation.addItem(label, value)
        self.live_coordinates = QCheckBox("Show board coordinates")
        self.live_best_arrow = QCheckBox("Stockfish arrow · blue square")
        self.live_human_arrow = QCheckBox("Maia arrow · amber diamond")
        self.live_played_arrow = QCheckBox("Played-move highlight · ochre")
        self.live_always_on_top = QCheckBox("Keep live overlay above the browser")
        self.live_remember_compact = QCheckBox("Remember compact mode")
        self.live_reduced_motion = QCheckBox("Reduce evaluation motion")

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(2, 2, 3, 2)
        content_layout.setSpacing(0)

        def add_open_group(title_text, rows):
            panel = QFrame()
            panel.setObjectName("panel")
            panel_layout = QVBoxLayout(panel)
            panel_layout.setContentsMargins(10, 9, 10, 11)
            panel_layout.setSpacing(8)
            heading = QLabel(title_text)
            heading.setObjectName("heading")
            panel_layout.addWidget(heading)
            for label_text, help_text, widget in rows:
                panel_layout.addWidget(labelled_field(label_text, widget, help_text))
            content_layout.addWidget(panel)

        def add_disclosure(title_text, rows, checks=(), expanded=False):
            panel = DisclosurePanel(title_text, expanded=expanded)
            for label_text, help_text, widget in rows:
                panel.body_layout.addWidget(labelled_field(label_text, widget, help_text))
            for checkbox in checks:
                panel.body_layout.addWidget(checkbox)
            content_layout.addWidget(panel)
            return panel

        self.live_budget_help = QLabel()
        self.live_budget_help.setObjectName("settingHelp")
        self.live_budget_help.setWordWrap(True)

        everyday = QFrame()
        everyday.setObjectName("panel")
        everyday_layout = QVBoxLayout(everyday)
        everyday_layout.setContentsMargins(10, 9, 10, 11)
        everyday_layout.setSpacing(8)
        heading = QLabel("Everyday analysis")
        heading.setObjectName("heading")
        everyday_layout.addWidget(heading)
        everyday_layout.addWidget(labelled_field(
            "Live strength", self.live_budget,
            "Time spent after each browser-board update."
        ))
        everyday_layout.addWidget(self.live_budget_help)
        everyday_layout.addWidget(labelled_field(
            "Candidate lines", self.live_multipv,
            "More alternatives use more space and engine time."
        ))
        everyday_layout.addWidget(labelled_field(
            "Maia human-move model", self.live_maia,
            "Optional human-like prediction; Stockfish is unchanged."
        ))
        everyday_layout.addWidget(labelled_field(
            "When the game advances", self.live_follow,
            "Choose what happens while you are exploring a private line."
        ))
        everyday_layout.addWidget(labelled_field(
            "Move explanations", self.live_explanation,
            "Controls the reasoning shown below a selected line."
        ))
        content_layout.addWidget(everyday)

        self.settings_lab_group = add_disclosure("Analysis Lab", (
            ("Explore strength", "Can be deeper than live search.", self.live_explore_budget),
            ("Continuation length", "Number of plies shown in a candidate line.", self.live_pv_length),
            ("Expanded candidate lines", "Show only the active line or every line.", self.live_line_expansion),
        ))
        self.settings_review_group = add_disclosure(
            "Local Review & Saved Studies", (
                ("Review strength", "Time spent on each historical position.", self.live_review_strength),
                ("Review alternatives", "Stockfish lines stored per position.", self.live_review_lines),
                ("Classification sensitivity", "How readily evaluation loss becomes an error.", self.live_review_sensitivity),
            ), (
                self.live_auto_save_completed, self.live_review_auto,
                self.live_study_auto, self.live_study_snapshots,
            )
        )
        self.settings_display_group = add_disclosure(
            "Board & Display", (
                ("Evaluation perspective", "Changes score signs, not the board bar direction.", self.live_eval_pov),
                ("Piece style", "Applied consistently to both colors and every board.", self.live_piece_style),
                ("Review and study orientation", "Override only the two local workspaces; Live and Analysis Lab still follow the browser.", self.live_workspace_orientation),
                ("Window opacity", "Applies immediately to the whole overlay.", self.live_opacity),
            ), (
                self.live_best_arrow, self.live_human_arrow,
                self.live_played_arrow, self.live_coordinates,
                self.live_always_on_top, self.live_remember_compact,
                self.live_reduced_motion,
            )
        )
        self.settings_engine_group = add_disclosure("Advanced engine", (
            ("Stockfish threads", "Higher values compete more with the browser.", self.live_threads),
        ))

        note = QLabel(
            "Engine changes apply to the next search. Display and explanation "
            "changes apply immediately. Analysis Lab never changes the live game."
        )
        note.setObjectName("helper")
        note.setWordWrap(True)
        content_layout.addSpacing(8)
        content_layout.addWidget(note)
        content_layout.addStretch(1)

        scroll = QScrollArea()
        self.settings_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        feedback = QHBoxLayout()
        self.settings_feedback_label = QLabel("")
        self.settings_feedback_label.setObjectName("saveState")
        self.settings_feedback_label.setAccessibleName("Settings status")
        feedback.addWidget(self.settings_feedback_label, 1)
        self.settings_undo_button = QPushButton("Undo")
        self.settings_undo_button.setObjectName("ghost")
        self.settings_undo_button.clicked.connect(self.undo_settings_reset)
        self.settings_undo_button.hide()
        feedback.addWidget(self.settings_undo_button)
        layout.addLayout(feedback)

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
            self.live_piece_style,
            self.live_workspace_orientation,
        ):
            widget.currentIndexChanged.connect(self.apply_ui_preferences)

        self.live_review_lines.valueChanged.connect(self.apply_ui_preferences)
        self.live_auto_save_completed.stateChanged.connect(self.apply_ui_preferences)
        self.live_review_auto.stateChanged.connect(self.apply_ui_preferences)
        self.live_study_auto.stateChanged.connect(self.apply_ui_preferences)
        self.live_study_snapshots.stateChanged.connect(self.apply_ui_preferences)

        for checkbox in (
            self.live_best_arrow, self.live_human_arrow, self.live_played_arrow,
            self.live_coordinates, self.live_always_on_top,
            self.live_remember_compact, self.live_reduced_motion,
        ):
            checkbox.stateChanged.connect(self.apply_ui_preferences)

        return page

    def build_recovery_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(8)

        help_text = QLabel(
            "Use this when the Chess.com board and ChessListener no longer "
            "show the same position."
        )
        help_text.setObjectName("helper")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        header = QHBoxLayout()
        header.addStretch(1)
        self.recovery_close_button = QPushButton("Live")
        self.recovery_close_button.setObjectName("ghost")
        self.recovery_close_button.clicked.connect(self.close_recovery)
        header.addWidget(self.recovery_close_button)
        layout.addLayout(header)

        scroll = QScrollArea()
        self.recovery_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 3, 0)
        body_layout.setSpacing(9)

        primary = QFrame()
        primary.setObjectName("panel")
        primary_layout = QVBoxLayout(primary)
        primary_layout.setContentsMargins(12, 11, 12, 12)
        primary_layout.setSpacing(6)
        primary_title = QLabel("Try this first")
        primary_title.setObjectName("heading")
        primary_layout.addWidget(primary_title)
        primary_help = QLabel(
            "ChessListener will read the visible board again and retain your "
            "current engine settings."
        )
        primary_help.setObjectName("helper")
        primary_help.setWordWrap(True)
        primary_layout.addWidget(primary_help)
        rescan_button = QPushButton("Re-read visible board")
        rescan_button.setObjectName("primary")
        rescan_button.setMinimumHeight(40)
        rescan_button.setAccessibleDescription(
            "Ask the extension to scan the visible Chess.com board again."
        )
        rescan_button.clicked.connect(self.request_rescan)
        primary_layout.addWidget(rescan_button)
        body_layout.addWidget(primary)

        restart_row = QVBoxLayout()
        restart_button = QPushButton("Restart engines")
        restart_button.setObjectName("ghost")
        restart_button.setToolTip("Restart Stockfish and Maia without changing the position")
        restart_button.clicked.connect(self.request_engine_restart)
        restart_row.addWidget(restart_button)
        restart_note = QLabel("Position stays unchanged")
        restart_note.setObjectName("helper")
        restart_row.addWidget(restart_note)
        body_layout.addLayout(restart_row)

        self.recovery_error_label = QLabel("")
        self.recovery_error_label.setObjectName("recoveryError")
        self.recovery_error_label.setWordWrap(True)
        self.recovery_error_label.setAccessibleName("Recovery error")
        self.recovery_error_label.hide()
        body_layout.addWidget(self.recovery_error_label)

        self.recovery_advanced = DisclosurePanel("Advanced manual repair")
        advanced = self.recovery_advanced.body_layout
        advanced_help = QLabel(
            "Use this only when a re-read cannot reconstruct turn, castling, "
            "or en-passant rights. Every position is validated before use."
        )
        advanced_help.setObjectName("helper")
        advanced_help.setWordWrap(True)
        advanced.addWidget(advanced_help)

        self.recovery_side = ScrollSafeComboBox()
        self.recovery_side.addItem("White to move", "w")
        self.recovery_side.addItem("Black to move", "b")
        advanced.addWidget(labelled_field("Side to move", self.recovery_side))

        castling = QWidget()
        castling_layout = QGridLayout(castling)
        castling_layout.setContentsMargins(0, 0, 0, 0)
        castling_layout.setSpacing(7)
        self.castle_white_king = QCheckBox("White O-O")
        self.castle_white_queen = QCheckBox("White O-O-O")
        self.castle_black_king = QCheckBox("Black O-O")
        self.castle_black_queen = QCheckBox("Black O-O-O")
        castling_layout.addWidget(self.castle_white_king, 0, 0)
        castling_layout.addWidget(self.castle_white_queen, 1, 0)
        castling_layout.addWidget(self.castle_black_king, 2, 0)
        castling_layout.addWidget(self.castle_black_queen, 3, 0)
        advanced.addWidget(labelled_field("Castling rights", castling))

        self.recovery_en_passant = QLineEdit()
        self.recovery_en_passant.setMaxLength(2)
        self.recovery_en_passant.setPlaceholderText("—")
        advanced.addWidget(labelled_field(
            "En-passant square", self.recovery_en_passant,
            "Leave empty when no en-passant capture is available."
        ))
        self.recovery_en_passant_error = QLabel("")
        self.recovery_en_passant_error.setObjectName("recoveryError")
        self.recovery_en_passant_error.setWordWrap(True)
        self.recovery_en_passant_error.setAccessibleName(
            "En-passant validation error"
        )
        self.recovery_en_passant_error.hide()
        advanced.addWidget(self.recovery_en_passant_error)

        counters = QWidget()
        counters_layout = QGridLayout(counters)
        counters_layout.setContentsMargins(0, 0, 0, 0)
        counters_layout.setSpacing(6)
        self.recovery_halfmove = ScrollSafeSpinBox()
        self.recovery_halfmove.setRange(0, 9999)
        self.recovery_halfmove.setAccessibleName("Halfmove clock")
        self.recovery_fullmove = ScrollSafeSpinBox()
        self.recovery_fullmove.setRange(1, 9999)
        self.recovery_fullmove.setAccessibleName("Fullmove number")
        half_label = QLabel("Halfmove clock")
        half_label.setBuddy(self.recovery_halfmove)
        full_label = QLabel("Fullmove number")
        full_label.setBuddy(self.recovery_fullmove)
        counters_layout.addWidget(half_label, 0, 0)
        counters_layout.addWidget(self.recovery_halfmove, 1, 0)
        counters_layout.addWidget(full_label, 2, 0)
        counters_layout.addWidget(self.recovery_fullmove, 3, 0)
        advanced.addWidget(counters)

        visible_button = QPushButton("Validate and apply visible board")
        visible_button.clicked.connect(self.apply_visible_fen)
        advanced.addWidget(visible_button)

        self.recovery_exact_fen = QLineEdit()
        self.recovery_exact_fen.setPlaceholderText(
            "8/8/8/8/8/8/4K3/7k w - - 0 1"
        )
        self.recovery_exact_fen.setAccessibleName("Exact six-field FEN")
        advanced.addWidget(labelled_field(
            "Or enter exact six-field FEN", self.recovery_exact_fen,
            "Board, turn, castling, en-passant, halfmove, and fullmove fields."
        ))
        self.recovery_fen_error = QLabel("")
        self.recovery_fen_error.setObjectName("recoveryError")
        self.recovery_fen_error.setWordWrap(True)
        self.recovery_fen_error.setAccessibleName("FEN validation error")
        self.recovery_fen_error.hide()
        advanced.addWidget(self.recovery_fen_error)

        exact_button = QPushButton("Validate and apply FEN")
        exact_button.setObjectName("ghost")
        exact_button.clicked.connect(self.apply_exact_fen)
        advanced.addWidget(exact_button)
        body_layout.addWidget(self.recovery_advanced)

        stop_button = QPushButton("Stop session")
        stop_button.setObjectName("danger")
        stop_button.setToolTip("Close this overlay and native analysis session")
        stop_button.clicked.connect(self.request_session_stop)
        stop_row = QVBoxLayout()
        stop_row.addWidget(stop_button)
        stop_note = QLabel("Closes the overlay and native session")
        stop_note.setObjectName("helper")
        stop_note.setWordWrap(True)
        stop_row.addWidget(stop_note)
        body_layout.addLayout(stop_row)
        body_layout.addStretch(1)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        self.recovery_controls = (
            rescan_button,
            self.recovery_advanced,
            self.recovery_exact_fen,
            exact_button,
            restart_button,
            stop_button,
        )

        for control in self.recovery_controls:
            control.setEnabled(False)

        return page

    def make_budget_combo(self):
        combo = ScrollSafeComboBox()

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
        combo = ScrollSafeComboBox()

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
        self.piece_style = str(
            self.settings.value("display/piece_style", "outline")
        )
        if self.piece_style not in {"outline", "solid"}:
            self.piece_style = "outline"
        self.workspace_orientation = str(
            self.settings.value("display/workspace_orientation", "follow")
        )
        if self.workspace_orientation not in {
            value for value, _label in WORKSPACE_ORIENTATION_CHOICES
        }:
            self.workspace_orientation = "follow"
        self.show_coordinates = setting_bool(
            self.settings.value("display/coordinates", True), True
        )
        self.always_on_top = setting_bool(
            self.settings.value("window/always_on_top", True), True
        )
        self.remember_compact = setting_bool(
            self.settings.value("window/remember_compact", False), False
        )
        self.reduced_motion = setting_bool(
            self.settings.value("display/reduced_motion", False), False
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
        self.auto_save_completed = setting_bool(
            self.settings.value("review/auto_save_completed", False), False
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
        self.select_data(self.live_piece_style, self.piece_style, 0)
        self.select_data(
            self.live_workspace_orientation, self.workspace_orientation, 0
        )
        self.live_coordinates.setChecked(self.show_coordinates)
        self.live_always_on_top.setChecked(self.always_on_top)
        self.live_remember_compact.setChecked(self.remember_compact)
        self.live_reduced_motion.setChecked(self.reduced_motion)
        self.select_data(self.live_review_strength, self.review_time_ms, 1)
        self.live_review_lines.setValue(self.review_lines)
        self.live_auto_save_completed.setChecked(self.auto_save_completed)
        self.live_review_auto.setChecked(self.review_auto)
        self.select_data(self.live_review_sensitivity, self.review_sensitivity, 1)
        self.live_study_auto.setChecked(self.study_auto_analyse)
        self.live_study_snapshots.setChecked(self.study_save_evals)

        try:
            saved_opacity = int(self.settings.value("window/opacity", 100))
        except (TypeError, ValueError):
            saved_opacity = 100

        self.select_data(self.live_opacity, saved_opacity, 0)
        self.postgame_auto_save.setChecked(self.auto_save_completed)
        self.applying_settings = False
        self.apply_opacity()
        self.apply_window_preferences(initial=True)
        self.apply_board_preferences()

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
        self.settings.setValue("review/auto_save_completed", self.auto_save_completed)
        self.settings.setValue("review/sensitivity", self.review_sensitivity)
        self.settings.setValue("studies/auto_analyse", self.study_auto_analyse)
        self.settings.setValue("studies/save_evaluations", self.study_save_evals)
        self.settings.setValue("display/eval_pov", self.eval_pov)
        self.settings.setValue("display/arrow_stockfish", self.show_best_arrow)
        self.settings.setValue("display/arrow_maia", self.show_human_arrow)
        self.settings.setValue("display/arrow_played", self.show_played_highlight)
        self.settings.setValue("display/piece_style", self.piece_style)
        self.settings.setValue(
            "display/workspace_orientation", self.workspace_orientation
        )
        self.settings.setValue("display/coordinates", self.show_coordinates)
        self.settings.setValue("display/reduced_motion", self.reduced_motion)
        self.settings.setValue("window/always_on_top", self.always_on_top)
        self.settings.setValue("window/remember_compact", self.remember_compact)
        if self.remember_compact:
            self.settings.setValue("window/compact", self.compact)
        else:
            self.settings.remove("window/compact")

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
        self.hide_game_finished_panel()

        engines = "Stockfish" if self.maia_rating == 0 else "Stockfish and Maia"
        self.set_status(f"Starting {engines}\u2026", "info", linger=False)
        self.stack.setCurrentWidget(self.analysis_page)

        saved = self.settings.value("window/geometry")
        restored = False

        if saved is not None:
            try:
                restored = self.restoreGeometry(saved)
            except (TypeError, RuntimeError):
                restored = False

        if not restored:
            self.resize(360, 620)

        self.update_page_chrome()

        if self.remember_compact and setting_bool(
            self.settings.value("window/compact", False), False
        ):
            QTimer.singleShot(0, self.toggle_compact)

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
        self.status_label.setText("Stored only on this computer")
        self.review_button.setEnabled(True)
        self.open_review()
        saved = self.settings.value("window/review_geometry")
        try:
            restored = saved is not None and self.restoreGeometry(saved)
        except (TypeError, RuntimeError):
            restored = False
        if not restored:
            self.resize(820, 700)
        self.update_page_chrome()

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
        self.auto_save_completed = self.live_auto_save_completed.isChecked()
        self.review_auto = self.live_review_auto.isChecked()
        self.review_sensitivity = str(self.live_review_sensitivity.currentData())
        self.study_auto_analyse = self.live_study_auto.isChecked()
        self.study_save_evals = self.live_study_snapshots.isChecked()
        self.show_best_arrow = self.live_best_arrow.isChecked()
        self.show_human_arrow = self.live_human_arrow.isChecked()
        self.show_played_highlight = self.live_played_arrow.isChecked()
        self.piece_style = str(self.live_piece_style.currentData() or "outline")
        self.workspace_orientation = str(
            self.live_workspace_orientation.currentData() or "follow"
        )
        self.show_coordinates = self.live_coordinates.isChecked()
        self.always_on_top = self.live_always_on_top.isChecked()
        self.remember_compact = self.live_remember_compact.isChecked()
        self.reduced_motion = self.live_reduced_motion.isChecked()
        self.postgame_auto_save.blockSignals(True)
        self.postgame_auto_save.setChecked(self.auto_save_completed)
        self.postgame_auto_save.blockSignals(False)
        self.save_settings()
        self.apply_board_preferences()
        self.apply_window_preferences()
        self.refresh_candidate_rows()
        self.refresh_explanation()
        self.dirty = True

    def reset_settings_defaults(self):
        self.settings_undo_snapshot = self.capture_settings_snapshot()
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
        self.select_data(self.live_piece_style, "outline", 0)
        self.select_data(self.live_workspace_orientation, "follow", 0)
        self.live_coordinates.setChecked(True)
        self.live_always_on_top.setChecked(True)
        self.live_remember_compact.setChecked(False)
        self.live_reduced_motion.setChecked(False)
        self.select_data(self.live_review_strength, 350, 1)
        self.live_review_lines.setValue(2)
        self.live_auto_save_completed.setChecked(False)
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

        self.settings_feedback_label.setText("Defaults restored")
        self.settings_undo_button.show()
        self.set_status("Defaults restored", "info", linger=True)

    def apply_opacity(self):
        self.opacity_percent = int(self.live_opacity.currentData())
        self.setWindowOpacity(self.opacity_percent / 100.0)
        self.settings.setValue("window/opacity", self.opacity_percent)

    def apply_board_preferences(self):
        for name in ("board", "review_board", "study_board"):
            board = getattr(self, name, None)
            if board is not None:
                board.set_display_preferences(self.piece_style, self.show_coordinates)
        for name in ("review_board", "study_board"):
            board = getattr(self, name, None)
            if board is not None:
                board.set_orientation_mode(self.workspace_orientation)
        if hasattr(self, "eval_bar"):
            self.eval_bar.set_reduced_motion(self.reduced_motion)

    def apply_window_preferences(self, initial=False):
        current = bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        if current == self.always_on_top:
            return
        was_visible = self.isVisible()
        old_position = self.pos()
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, self.always_on_top)
        if was_visible and not initial:
            self.show()
            self.move(old_position)

    def set_auto_save_completed(self, _state=None):
        if self.applying_settings:
            return
        sender = self.sender()
        checked = (
            self.postgame_auto_save.isChecked()
            if sender is self.postgame_auto_save
            else self.live_auto_save_completed.isChecked()
        )
        self.applying_settings = True
        self.auto_save_completed = checked
        self.postgame_auto_save.setChecked(checked)
        self.live_auto_save_completed.setChecked(checked)
        self.applying_settings = False
        self.save_settings()
        self.set_status(
            "Completed games will save locally" if checked
            else "Automatic game saving is off",
            "info", linger=True,
        )

    def capture_settings_snapshot(self):
        return {
            "budget": self.live_budget.currentData(),
            "explore_budget": self.live_explore_budget.currentData(),
            "threads": self.live_threads.value(),
            "multipv": self.live_multipv.value(),
            "maia": self.live_maia.currentData(),
            "pv_length": self.live_pv_length.currentData(),
            "follow": self.live_follow.currentData(),
            "explanation": self.live_explanation.currentData(),
            "pov": self.live_eval_pov.currentData(),
            "expansion": self.live_line_expansion.currentData(),
            "best_arrow": self.live_best_arrow.isChecked(),
            "human_arrow": self.live_human_arrow.isChecked(),
            "played": self.live_played_arrow.isChecked(),
            "piece_style": self.live_piece_style.currentData(),
            "workspace_orientation": self.live_workspace_orientation.currentData(),
            "coordinates": self.live_coordinates.isChecked(),
            "always_on_top": self.live_always_on_top.isChecked(),
            "remember_compact": self.live_remember_compact.isChecked(),
            "reduced_motion": self.live_reduced_motion.isChecked(),
            "review_strength": self.live_review_strength.currentData(),
            "review_lines": self.live_review_lines.value(),
            "auto_save": self.live_auto_save_completed.isChecked(),
            "auto_review": self.live_review_auto.isChecked(),
            "sensitivity": self.live_review_sensitivity.currentData(),
            "study_auto": self.live_study_auto.isChecked(),
            "study_snapshots": self.live_study_snapshots.isChecked(),
            "opacity": self.live_opacity.currentData(),
        }

    def restore_settings_snapshot(self, snapshot):
        if not snapshot:
            return
        self.applying_settings = True
        for combo, key, fallback in (
            (self.live_budget, "budget", 1),
            (self.live_explore_budget, "explore_budget", 0),
            (self.live_maia, "maia", 0),
            (self.live_pv_length, "pv_length", 1),
            (self.live_follow, "follow", 0),
            (self.live_explanation, "explanation", 1),
            (self.live_eval_pov, "pov", 0),
            (self.live_line_expansion, "expansion", 0),
            (self.live_piece_style, "piece_style", 0),
            (self.live_workspace_orientation, "workspace_orientation", 0),
            (self.live_review_strength, "review_strength", 1),
            (self.live_review_sensitivity, "sensitivity", 1),
            (self.live_opacity, "opacity", 0),
        ):
            self.select_data(combo, snapshot.get(key), fallback)
        self.live_threads.setValue(int(snapshot.get("threads", self.threads)))
        self.live_multipv.setValue(int(snapshot.get("multipv", self.multipv)))
        self.live_review_lines.setValue(int(snapshot.get("review_lines", 2)))
        for checkbox, key in (
            (self.live_best_arrow, "best_arrow"),
            (self.live_human_arrow, "human_arrow"),
            (self.live_played_arrow, "played"),
            (self.live_coordinates, "coordinates"),
            (self.live_always_on_top, "always_on_top"),
            (self.live_remember_compact, "remember_compact"),
            (self.live_reduced_motion, "reduced_motion"),
            (self.live_auto_save_completed, "auto_save"),
            (self.live_review_auto, "auto_review"),
            (self.live_study_auto, "study_auto"),
            (self.live_study_snapshots, "study_snapshots"),
        ):
            checkbox.setChecked(bool(snapshot.get(key, False)))
        self.applying_settings = False
        self.apply_ui_preferences()
        self.apply_opacity()
        self.send_settings() if self.start_command_sent else self.save_settings()

    def undo_settings_reset(self):
        snapshot = self.settings_undo_snapshot
        self.settings_undo_snapshot = None
        self.settings_undo_button.hide()
        self.restore_settings_snapshot(snapshot)
        self.settings_feedback_label.setText("Previous settings restored")
        self.set_status("Previous settings restored", "info", linger=True)

    def page_title(self):
        if not hasattr(self, "stack"):
            return "ChessListener"
        page = self.stack.currentWidget()
        if page is self.startup_page:
            return "ChessListener"
        if page is self.settings_page:
            return "Settings"
        if page is self.recovery_page:
            return "Recovery"
        if page is self.review_page:
            return "Local Review"
        if page is self.study_page:
            return "Saved Studies"
        if page is self.analysis_page:
            if self.postgame_visible:
                return "Game Finished"
            if self.mode == "explore":
                return "Analysis Lab"
            if self.preview_step:
                return "Preview"
            return "Live Analysis"
        return "ChessListener"

    def update_page_chrome(self, _index=None):
        if not hasattr(self, "stack") or not hasattr(self, "page_title_label"):
            return
        page = self.stack.currentWidget()
        self.page_title_label.setText(self.page_title())
        self.page_title_label.setToolTip(self.page_title())
        self.update_source_badge()
        started = self.start_command_sent
        analysis = page is self.analysis_page
        authoritative_turn = (
            analysis and not self.preview_step and not self.postgame_visible
            and bool(self.fen)
        )
        self.turn_dot.setVisible(authoritative_turn and not self.compact)
        self.turn_dot.setAccessibleName(
            "White to move" if self.side_to_move == "w" else "Black to move"
        )

        # Page-aware destinations.  At small widths secondary destinations
        # collapse into a labelled overflow menu instead of truncating titles.
        width = max(self.width(), self.minimumWidth())
        exploring = analysis and self.mode == "explore"
        if hasattr(self, "candidate_scroll"):
            self.candidate_scroll.setFixedHeight(
                110 if exploring and width < 520 else 142
            )
        if hasattr(self, "explanation_scroll"):
            if exploring and width < 380:
                explanation_height = 64
            else:
                explanation_height = 82 if width < 380 else 116
            self.explanation_scroll.setFixedHeight(explanation_height)
        if hasattr(self, "explore_controls"):
            self.update_explore_toolbar_layout(width < 520)
        if analysis and not self.postgame_visible:
            show_context = bool(
                width >= 380 and not self.compact
                and (self.mode == "explore" or self.preview_step)
            )
            self.analysis_context.setVisible(show_context)
            self.breadcrumb_label.setVisible(bool(
                not self.compact and self.mode == "explore"
                and self.breadcrumb_label.text()
            ))
        direct_all = width >= 470
        medium = 380 <= width < 470
        show_shell = started and page is not self.startup_page
        show_compact = show_shell and analysis and not self.postgame_visible
        show_recovery = bool(
            show_shell and page is not self.recovery_page and self.session_active
            and (direct_all or medium)
        )
        show_review = bool(
            show_shell and page is not self.review_page
            and (direct_all or (medium and self.postgame_visible))
        )
        show_study = bool(
            show_shell and page is not self.study_page and direct_all
        )
        show_settings = bool(
            show_shell and page is not self.settings_page and (direct_all or medium)
        )
        self.compact_button.setVisible(show_compact)
        self.recovery_button.setVisible(show_recovery)
        self.review_button.setVisible(show_review)
        self.study_button.setVisible(show_study)
        self.settings_button.setVisible(show_settings)
        hidden_destination = show_shell and any((
            page is not self.review_page and not show_review,
            page is not self.study_page and not show_study,
            page is not self.settings_page and not show_settings,
            self.session_active and page is not self.recovery_page
            and not show_recovery,
        ))
        self.overflow_button.setVisible(hidden_destination)

        # Fixed breakpoints are not sufficient once system text is scaled.
        # Reclaim optional action slots until the page identity fits in full;
        # every hidden destination remains available in the overflow menu.
        self.page_title_label.setMinimumWidth(0)
        title_need = self.page_title_label.sizeHint().width() + 3

        def required_titlebar_width():
            widgets = [self.brand_mark, self.page_title_label]
            for widget in (
                self.source_label,
                self.turn_dot,
                self.compact_button,
                self.recovery_button,
                self.review_button,
                self.study_button,
                self.settings_button,
                self.overflow_button,
                self.close_button,
            ):
                if widget is not None and not widget.isHidden():
                    widgets.append(widget)
            margins = self.title_bar.layout().contentsMargins()
            return (
                margins.left() + margins.right()
                + self.title_bar.layout().spacing() * max(0, len(widgets) - 1)
                + title_need
                + sum(widget.sizeHint().width() for widget in widgets if widget is not self.page_title_label)
            )

        for button in (
            self.study_button,
            self.recovery_button,
            self.settings_button,
            self.review_button,
        ):
            if required_titlebar_width() <= width:
                break
            if not button.isHidden():
                button.hide()
                hidden_destination = True
                self.overflow_button.show()
        self.page_title_label.setMinimumWidth(title_need)
        self.title_bar.layout().invalidate()
        self.title_bar.layout().activate()
        if page is self.settings_page:
            self.settings_close_button.setText("Close" if self.local_mode else "Back")
        if page is self.recovery_page:
            self.recovery_close_button.setText("Back")
        if page is self.review_page:
            self.review_close_button.setText("Close" if self.local_mode else "Live")

        wide_workspace = width >= 700
        orientation = (
            Qt.Orientation.Horizontal if wide_workspace else Qt.Orientation.Vertical
        )
        if hasattr(self, "review_workspace"):
            self.review_workspace.setOrientation(orientation)
        if hasattr(self, "study_workspace"):
            self.study_workspace.setOrientation(orientation)
        self.update_workspace_constraints(wide_workspace)
        self.update_analysis_board_constraint()

    def update_explore_toolbar_layout(self, stacked):
        """Wrap Analysis Lab actions before their labels become cryptic."""
        stacked = bool(stacked)
        if self.explore_controls_stacked == stacked:
            return
        self.explore_controls_stacked = stacked
        layout = self.explore_controls
        widgets = (
            self.root_button, self.undo_button, self.redo_button,
            self.save_lab_button, self.live_update_button, self.go_live_button,
        )
        while layout.count():
            layout.takeAt(0)
        for column in range(7):
            layout.setColumnStretch(column, 0)
        if stacked:
            for column, widget in enumerate(widgets[:3]):
                layout.addWidget(widget, 0, column)
                layout.setColumnStretch(column, 1)
            for column, widget in enumerate(widgets[3:]):
                layout.addWidget(widget, 1, column)
        else:
            for column, widget in enumerate(widgets[:4]):
                layout.addWidget(widget, 0, column)
            layout.setColumnStretch(4, 1)
            layout.addWidget(self.live_update_button, 0, 5)
            layout.addWidget(self.go_live_button, 0, 6)

    def update_workspace_constraints(self, wide_workspace):
        """Keep narrow workspace boards square and let their page scroll."""
        if not hasattr(self, "review_board"):
            return
        if wide_workspace:
            minimum, maximum = 200, 16777215
            self.review_workspace.setMinimumHeight(0)
            self.study_workspace.setMinimumHeight(0)
        else:
            horizontal_reserve = 54 if self.width() < 340 else 34
            side = max(200, min(430, self.width() - horizontal_reserve))
            minimum = maximum = side
            # A QSplitter can compress child layouts while it lives in a
            # resizable scroll area. Give the stacked desk its honest content
            # height so controls never paint through one another; the outer
            # page owns scrolling.
            self.review_workspace.setMinimumHeight(side + 500)
            self.study_workspace.setMinimumHeight(side + 520)
        for board in (self.review_board, self.study_board):
            board.setMinimumHeight(minimum)
            board.setMaximumHeight(maximum)
            board.updateGeometry()
        self.review_workspace.updateGeometry()
        self.study_workspace.updateGeometry()

    def update_analysis_board_constraint(self):
        if not hasattr(self, "board"):
            return
        if self.postgame_visible:
            side = max(200, self.analysis_page.width() - 18)
            self.board.setMaximumHeight(side)
        else:
            self.board.setMaximumHeight(16777215)
        self.board.updateGeometry()

    def can_toggle_compact(self):
        return (
            self.start_command_sent
            and self.stack.currentWidget() is self.analysis_page
            and self.mode == "live"
            and self.preview_step == 0
            and not self.postgame_visible
        )

    def enter_workspace(self, kind):
        if not self.isVisible():
            self.update_page_chrome()
            return
        key = f"window/{kind}_geometry"
        saved = self.settings.value(key)
        restored = False
        if saved is not None:
            try:
                restored = self.restoreGeometry(saved)
            except (TypeError, RuntimeError):
                restored = False
        if not restored:
            self.resize(max(760, self.width()), max(680, self.height()))
        self.update_page_chrome()

    def leave_workspace(self, target):
        if target is self.analysis_page:
            kind = "geometry"
        elif target is self.review_page:
            kind = "review_geometry"
        elif target is self.study_page:
            kind = "study_geometry"
        else:
            kind = None

        if kind and self.isVisible():
            saved = self.settings.value(f"window/{kind}")
            if saved is not None:
                try:
                    self.restoreGeometry(saved)
                except (TypeError, RuntimeError):
                    pass
        self.update_page_chrome()

    def update_review_surface(self):
        if not hasattr(self, "review_workspace"):
            return
        has_record = bool(self.review_record)
        self.review_empty_panel.setVisible(not has_record)
        self.review_workspace.setVisible(has_record)
        self.review_workspace_scroll.setVisible(has_record)
        self.review_start_button.setEnabled(
            has_record and self.review_job is None and not self.review_cancelling
        )

    def update_study_surface(self):
        if not hasattr(self, "study_workspace"):
            return
        has_study = bool(self.current_study)
        self.study_empty_panel.setVisible(not has_study)
        self.study_workspace.setVisible(has_study)
        self.study_workspace_scroll.setVisible(has_study)

    def postgame_save_clicked(self):
        if self.save_completed_game(automatic=False):
            self.update_game_finished_save_state(True)

    def update_game_finished_save_state(self, saved):
        self.completed_game_saved = bool(saved)
        record = self.completed_game_record or {}
        available = bool(record)
        position_only = bool(record.get("position_only", False))
        self.postgame_save_button.setText(
            ("Saved final position" if position_only else "Saved locally")
            if saved else
            ("Save final position" if position_only else "Save game")
        )
        self.postgame_save_button.setEnabled(not saved and available)
        self.postgame_save_button.setAccessibleDescription(
            "This completed game is saved once in the local library."
            if saved else
            "Save this completed game once in the local ChessListener library."
        )

    def show_game_finished_panel(self):
        if self.compact:
            self.toggle_compact()
        self.postgame_visible = True
        self.game_finished_panel.show()
        for widget in self.analysis_regular_widgets:
            widget.hide()
        self.board.show()
        self.analysis_context.hide()
        record = self.completed_game_record or {}
        available = bool(record)
        result = str(record.get("result", "*") or "*")
        label = str(record.get("label", "")).strip()
        history_complete = bool(record.get("history_complete", False))
        position_only = bool(record.get("position_only", False))
        source = str(record.get("source", "")).strip()
        self.game_finished_title.setText(
            "Game finished" + (f" · {result}" if result != "*" else "")
        )
        if not available:
            detail = (
                "No valid current position was captured, so there is nothing "
                "safe to save or review."
            )
        elif position_only:
            source_name = source if source in {"exact", "inferred", "manual"} else "verified"
            detail = (
                f"Move history unavailable. Final {source_name} position captured; "
                "review and export start from that position."
            )
        elif history_complete:
            detail = "Complete verified move history is available."
        else:
            detail = (
                "A verified current record is available, but its move history "
                "is incomplete."
            )
        self.game_finished_detail.setText(
            (label + ".\n" if label and label.rstrip(".") not in detail else "")
            + detail
        )
        self.postgame_review_button.setEnabled(available)
        self.postgame_explore_button.setEnabled(available)
        self.postgame_export_button.setEnabled(available)
        self.postgame_auto_save.blockSignals(True)
        self.postgame_auto_save.setChecked(self.auto_save_completed)
        self.postgame_auto_save.blockSignals(False)
        self.update_game_finished_save_state(self.completed_game_saved)
        self.stack.setCurrentWidget(self.analysis_page)
        self.update_analysis_board_constraint()
        self.update_page_chrome()
        self.analysis_page.updateGeometry()

    def hide_game_finished_panel(self):
        if not hasattr(self, "game_finished_panel"):
            return
        self.postgame_visible = False
        self.game_finished_panel.hide()
        self.update_analysis_board_constraint()
        self.board.setVisible(not self.compact)
        self.eval_bar.setVisible(True)
        self.analysis_state_label.setVisible(not self.compact)
        self.last_label.setVisible(True)
        self.human_label.setVisible(True)
        self.best_label.setVisible(self.compact)
        self.candidate_scroll.setVisible(not self.compact)
        self.update_analysis_mode_ui()
        self.refresh_explanation()
        self.update_page_chrome()

    def toggle_compact(self):
        if not self.can_toggle_compact():
            return

        self.compact = not self.compact

        if self.compact:
            self.expanded_geometry = self.saveGeometry()
            self.compact_button.set_icon("expand", "Expand live analysis")
        else:
            self.compact_button.set_icon("collapse", "Compact live analysis")

        for widget in (
            self.board,
            self.analysis_context,
            self.analysis_state_label,
            self.candidate_scroll,
            self.live_toolbar,
            self.explore_toolbar,
            self.breadcrumb_label,
            self.explanation_scroll,
        ):
            widget.setVisible(not self.compact)

        self.last_label.show()
        self.human_label.show()
        self.best_label.setVisible(self.compact)

        if not self.compact:
            self.update_analysis_mode_ui()
            self.refresh_explanation()

        if self.remember_compact:
            self.settings.setValue("window/compact", self.compact)
        self.update_page_chrome()

        # The board has an Expanding policy and a size floor, so the window
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
        if not self.start_command_sent or self.compact:
            return
        page = self.stack.currentWidget()
        if page is self.review_page:
            key = "window/review_geometry"
        elif page is self.study_page:
            key = "window/study_geometry"
        else:
            key = "window/geometry"
        self.settings.setValue(key, self.saveGeometry())

    def note_geometry_change(self):
        if self.start_command_sent and not self.compact:
            self.geometry_timer.start()

    def moveEvent(self, event):
        super().moveEvent(event)
        self.note_geometry_change()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.note_geometry_change()
        self.update_page_chrome()

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

        if key == Qt.Key.Key_Space and self.can_toggle_compact():
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
        self.clear_recovery_error()
        self.recovery_advanced.set_expanded(False)
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

    def clear_recovery_error(self):
        if not hasattr(self, "recovery_error_label"):
            return
        for label in (
            self.recovery_error_label,
            getattr(self, "recovery_en_passant_error", None),
            getattr(self, "recovery_fen_error", None),
        ):
            if label is not None:
                label.clear()
                label.hide()
        for field in (self.recovery_exact_fen, self.recovery_en_passant):
            field.setProperty("invalid", False)
            field.style().unpolish(field)
            field.style().polish(field)

    def show_recovery_error(self, text, field=None):
        self.recovery_advanced.set_expanded(True)
        if field is self.recovery_en_passant:
            label = self.recovery_en_passant_error
        elif field is self.recovery_exact_fen:
            label = self.recovery_fen_error
        else:
            label = self.recovery_error_label
        label.setText(str(text))
        label.show()
        if field is not None:
            field.setProperty("invalid", True)
            field.style().unpolish(field)
            field.style().polish(field)
            field.setFocus(Qt.FocusReason.OtherFocusReason)
            if isinstance(field, QLineEdit):
                field.selectAll()
        QTimer.singleShot(
            0, lambda target=label: self.recovery_scroll.ensureWidgetVisible(
                target, 12, 24
            )
        )

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
        self.clear_recovery_error()
        self.begin_recovery("RESCAN", "Waiting for the visible board\u2026")

    def apply_visible_fen(self):
        self.clear_recovery_error()
        try:
            fen = self.visible_board_fen()
        except ValueError as error:
            self.show_recovery_error(error, self.recovery_en_passant)
            self.set_status(str(error), "warn", linger=False)
            return

        self.begin_recovery(
            "FEN", "Validating the position\u2026", payload=fen
        )

    def apply_exact_fen(self):
        self.clear_recovery_error()
        try:
            fen = validate_fen_input(self.recovery_exact_fen.text())
        except ValueError as error:
            self.show_recovery_error(error, self.recovery_exact_fen)
            self.set_status(str(error), "warn", linger=False)
            return

        self.begin_recovery(
            "FEN", "Validating the position\u2026", payload=fen
        )

    def request_engine_restart(self):
        self.begin_recovery("RESTART", "Restarting engines\u2026")

    def request_session_stop(self):
        self.begin_recovery("STOP", "Stopping this session\u2026")

    # -- Local library safety --------------------------------------------

    def report_library_error(self, error):
        """Keep analysis usable while making a preserved archive failure clear."""
        self.review_store_error = str(error)
        message = (
            "Local library unavailable — no saved data was changed. "
            f"{self.review_store_error}"
        )
        self.set_status(message, "warn", linger=False)
        self.status_label.setToolTip(message)
        for name in ("review_library_combo", "study_library_combo"):
            combo = getattr(self, name, None)
            if combo is not None:
                combo.blockSignals(True)
                combo.clear()
                combo.addItem("Library unavailable — file preserved", None)
                combo.setEnabled(False)
                combo.setToolTip(message)
                combo.blockSignals(False)
        for name in ("review_delete_button", "study_delete_button"):
            button = getattr(self, name, None)
            if button is not None:
                button.setEnabled(False)
                button.setToolTip(message)

    # -- Saved Studies ---------------------------------------------------

    def toggle_study(self):
        if self.stack.currentWidget() is self.study_page:
            self.close_study()
        else:
            self.open_study()

    def open_study(self):
        current = self.stack.currentWidget()
        if current is not self.study_page:
            self.save_geometry()
            self.study_return_page = current
        self.stack.setCurrentWidget(self.study_page)
        self.enter_workspace("study")
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
        self.update_study_surface()

    def close_study(self):
        if self.current_study is not None:
            had_edits = self.study_dirty
            if not self.flush_study_edits():
                return False
            if not had_edits and not self.persist_current_study():
                self.set_study_save_state(
                    "Save failed — study remains open. Try again.", failed=True
                )
                return False
        self.cancel_study_analysis()
        target = self.study_return_page
        if (
            target is None or target is self.study_page
            or (target is self.startup_page and self.start_command_sent)
        ):
            target = self.review_page if self.local_mode else self.analysis_page
        self.study_return_page = None
        self.save_geometry()
        self.stack.setCurrentWidget(target)
        self.leave_workspace(target)
        return True

    def populate_study_library(self, selected_id=None):
        if self.review_store is None or not hasattr(self, "study_library_combo"):
            return []
        query = self.study_search.text() if hasattr(self, "study_search") else ""
        try:
            studies = self.review_store.list_studies(query)
        except (OSError, ValueError) as error:
            self.report_library_error(error)
            return []
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
        self.update_study_surface()
        return studies

    def filter_study_library(self, _text=""):
        if self.current_study is not None and not self.flush_study_edits():
            return
        studies = self.populate_study_library(self.current_study_id)
        identifiers = {item.get("id") for item in studies}
        if studies and self.current_study_id not in identifiers:
            self.load_study(studies[0])

    def load_study_library_selection(self):
        if self.applying_settings or self.review_store is None:
            return
        selected_id = self.study_library_combo.currentData()
        if selected_id and str(selected_id) == str(self.current_study_id or ""):
            return
        try:
            item = self.review_store.find_study(selected_id)
        except (OSError, ValueError) as error:
            self.report_library_error(error)
            return
        if item is not None:
            if not self.load_study(item):
                self.applying_settings = True
                self.select_data(
                    self.study_library_combo, self.current_study_id, 0
                )
                self.applying_settings = False
        elif self.current_study_id:
            self.applying_settings = True
            self.select_data(self.study_library_combo, self.current_study_id, 0)
            self.applying_settings = False

    def load_study(self, raw):
        if study_rules is None:
            return False
        incoming_id = str(raw.get("id") or "") if isinstance(raw, dict) else ""
        if self.current_study is not None:
            if not self.flush_study_edits():
                return False
            if incoming_id and incoming_id == str(self.current_study_id or ""):
                return True
        self.cancel_study_analysis()
        try:
            self.current_study = study_rules.normalise_study(raw)
        except ValueError as error:
            self.set_status(f"Could not load study: {error}", "warn", linger=False)
            return False
        self.current_study_id = self.current_study.get("id") or None
        self.study_node_id = self.current_study.get("selected", self.current_study["root"])
        self.study_dirty = False
        self.study_save_failed = False
        self.refresh_study_tree()
        self.select_study_node(self.study_node_id, analyse=False, persist=False)
        self.study_export_button.setEnabled(True)
        self.update_study_surface()
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
        if self.current_study is not None and not self.flush_study_edits():
            return False
        try:
            item = study_rules.new_study(title, fen)
            # The previous study may still have a position worker running.
            # Do not let its queued result attach to a new tree that happens
            # to reuse the same root id (and possibly the same FEN).
            self.cancel_study_analysis()
            self.current_study = item
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
        if self.current_study is not None and not self.flush_study_edits():
            return False
        try:
            item = study_rules.from_explore_tree(
                title,
                self.explore_nodes,
                self.explore_root_node_id,
                self.explore_node_id,
                {"Site": "ChessListener Analysis Lab", "Source": self.session_label},
            )
            # As with creating a blank study, replacing the active tree must
            # invalidate any worker that still belongs to the previous tree.
            self.cancel_study_analysis()
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
        if self.current_study is None or study_rules is None:
            return False
        if self.review_store is None:
            if self.review_store_error:
                self.report_library_error(self.review_store_error)
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

    def set_study_save_state(self, text, failed=False):
        """Expose durable annotation persistence state beside the editor."""
        self.study_save_failed = bool(failed)
        label = getattr(self, "study_save_state_label", None)
        if label is not None:
            label.setText(str(text))
            label.setProperty("saveFailed", bool(failed))
            label.setProperty(
                "savePending", str(text).strip().lower().startswith("saving")
            )
            # A dynamic property can affect the production stylesheet.
            label.style().unpolish(label)
            label.style().polish(label)
        button = getattr(self, "study_save_note_button", None)
        if button is not None:
            button.setToolTip(str(text))

    def confirm_discard_failed_study_on_close(self):
        """Offer an explicit escape after a close-time disk failure.

        Keeping the editor open is the safe/default path.  The destructive
        choice is deliberately worded and never inferred from a generic close
        button or timeout.
        """
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Warning)
        dialog.setWindowTitle("Study was not saved")
        dialog.setText("ChessListener could not write this study to local storage.")
        dialog.setInformativeText(
            "Keep editing to retry, or explicitly discard the unsaved changes "
            "and close ChessListener."
        )
        keep_button = dialog.addButton(
            "Keep editing", QMessageBox.ButtonRole.RejectRole
        )
        discard_button = dialog.addButton(
            "Discard unsaved edits and close",
            QMessageBox.ButtonRole.DestructiveRole,
        )
        dialog.setDefaultButton(keep_button)
        dialog.setEscapeButton(keep_button)
        dialog.exec()
        return dialog.clickedButton() is discard_button

    def sync_study_edits_to_model(self):
        """Copy the visible fields before any navigation can replace them."""
        if self.study_annotation_loading or self.current_study is None:
            return False
        node = self.current_study["nodes"].get(str(self.study_node_id))
        if node is None:
            return False
        self.current_study["title"] = (
            self.study_title_edit.text().strip() or "Untitled study"
        )
        node["name"] = self.study_name_edit.text().strip()
        node["comment"] = self.study_comment_edit.toPlainText().strip()
        return True

    def queue_study_autosave(self, *_args):
        """Retain edits immediately and debounce the atomic disk write."""
        if self.study_annotation_loading or self.current_study is None:
            return
        if not self.sync_study_edits_to_model():
            return
        self.study_dirty = True
        self.set_study_save_state("Saving…")
        self.study_autosave_timer.start()

    def flush_study_edits(self, refresh_library=True, announce=False):
        """Atomically save visible annotations, blocking navigation on error."""
        self.study_autosave_timer.stop()
        if self.current_study is None:
            self.study_dirty = False
            return True
        if not self.sync_study_edits_to_model():
            return not self.study_dirty
        if not self.study_dirty:
            return True
        if not self.persist_current_study(refresh_library=refresh_library):
            # The in-memory object and widgets still contain the user's text.
            # Navigation callers respect False and leave the editor in place.
            self.study_dirty = True
            self.set_study_save_state(
                "Save failed — edits retained here. Try again.", failed=True
            )
            return False
        self.study_dirty = False
        self.set_study_save_state("Saved locally")
        self.refresh_study_tree()
        if announce:
            self.set_status("Study saved locally", "info", linger=True)
        return True

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

    def select_study_tree_item(self, item, previous=None):
        if self.study_tree_refreshing or item is None:
            return
        node_id = item.data(0, Qt.ItemDataRole.UserRole)
        if self.select_study_node(node_id, analyse=self.study_auto_analyse):
            return
        # QTreeWidget changes its highlight before emitting currentItemChanged.
        # A failed flush must put that highlight back on the model/editor node.
        restore = previous or getattr(self, "study_tree_items", {}).get(
            str(self.study_node_id)
        )
        if restore is not None:
            self.study_tree_refreshing = True
            self.study_tree.setCurrentItem(restore)
            self.study_tree.scrollToItem(restore)
            self.study_tree_refreshing = False

    def select_study_node(self, node_id, analyse=None, persist=False):
        if self.current_study is None:
            return False
        node_id = str(node_id)
        if (
            self.study_node_id is not None
            and node_id != str(self.study_node_id)
            and not self.flush_study_edits()
        ):
            return False
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
        self.study_dirty = False
        self.set_study_save_state("Saved locally")
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
        if not self.flush_study_edits():
            return
        node_id = str(item.data(0, Qt.ItemDataRole.UserRole))
        node = self.current_study["nodes"].get(node_id)
        if node is not None and node.get("collapsed") != collapsed:
            node["collapsed"] = collapsed
            self.persist_current_study()

    def save_study_annotation(self):
        if self.study_annotation_loading or self.current_study is None:
            return
        self.study_dirty = True
        self.flush_study_edits(refresh_library=True, announce=True)

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
        if not self.flush_study_edits():
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
                    completed_lines = list(message.get("lines") or [])
                    if self.study_save_evals:
                        node["analysis"] = {
                            "lines": completed_lines,
                            "depth": max(
                                (int(line.get("depth", 0)) for line in completed_lines),
                                default=0,
                            ),
                            "final": True,
                            "captured_at": int(time.time()),
                        }
                        self.persist_current_study(refresh_library=True)
                        self.refresh_study_tree()
                    # A manual search may finish after the user has selected
                    # another node.  Preserve its snapshot on the node it was
                    # requested for, but never paint those lines onto the
                    # newly selected position.
                    if str(self.study_node_id) == str(node_id):
                        self.study_position_lines = completed_lines
                        self.show_study_analysis()
                self.study_position_job = None
                self.study_position_queue = None
                self.study_position_timer.stop()
                self.study_analyse_button.setEnabled(True)
                return
            elif message.get("type") == "position_error":
                if str(self.study_node_id) == str(self.study_analysis_node_id):
                    self.study_detail.setText(
                        "Study analysis failed: "
                        + str(message.get("message", "unknown error"))
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
        if not self.flush_study_edits():
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
        try:
            deleted = self.review_store.delete_study(self.current_study_id)
        except (OSError, ValueError) as error:
            self.report_library_error(error)
            return
        if deleted:
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
        if self.stack.currentWidget() is not self.review_page:
            self.save_geometry()
        self.stack.setCurrentWidget(self.review_page)
        self.enter_workspace("review")
        self.populate_review_library(self.review_game_id)
        self.update_review_surface()
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

    def review_record_identity(self, record=None):
        record = record or self.review_record
        if not record:
            return ""
        if study_store is not None:
            return study_store.record_id(record)
        return str(record.get("session_id") or "") + "\n" + str(
            record.get("initial_fen") or ""
        ) + "\n" + "|".join(record.get("moves") or [])

    @staticmethod
    def copy_game_record(record):
        if not record:
            return None
        copied = dict(record)
        copied["moves"] = list(record.get("moves") or [])
        copied["metadata"] = dict(record.get("metadata") or {})
        return copied

    @staticmethod
    def canonical_position_fen(raw_fen):
        try:
            return (
                pgn_import.canonical_fen(str(raw_fen or ""))
                if pgn_import is not None
                else san_rules.Board(str(raw_fen or "")).fen()
            )
        except (ValueError, AttributeError):
            return ""

    def authoritative_live_state(self):
        """Return the live truth even while a hypothetical board is shown."""
        snapshot = self.live_snapshot if isinstance(self.live_snapshot, dict) else {}
        if self.mode == "explore" and snapshot.get("fen"):
            return {
                "fen": str(snapshot.get("fen") or ""),
                "source": str(snapshot.get("source") or ""),
                "flip": bool(snapshot.get("flip", self.flip)),
            }
        if self.preview_step and self.preview_root_fen:
            return {
                "fen": str(self.preview_root_fen),
                "source": str(self.state_source or ""),
                "flip": bool(self.flip),
            }
        return {
            "fen": str(self.fen or ""),
            "source": str(self.state_source or ""),
            "flip": bool(self.flip),
        }

    def game_record_final_fen(self, record):
        if not record:
            return ""
        try:
            positions = self.build_record_positions(
                record["initial_fen"], record.get("moves") or []
            )
        except (KeyError, ValueError, AttributeError):
            return ""
        return self.canonical_position_fen(positions[-1] if positions else "")

    def position_only_record_for_session(self, session_identifier, end_state=None):
        """Return an honest one-position record for a session without history.

        Preview and Analysis Lab can temporarily replace the main board with a
        hypothetical position.  In those modes the last native live snapshot,
        rather than that hypothetical board, is the authoritative fallback.
        Invalid or absent FENs fail closed so an older selected library game can
        never leak into post-game actions.
        """
        session_identifier = str(session_identifier or "").strip()
        if not session_identifier:
            return None
        live_state = self.authoritative_live_state()
        initial_fen = self.canonical_position_fen(live_state.get("fen"))
        source = str(live_state.get("source") or "")
        if not initial_fen:
            return None
        if source not in {"exact", "inferred", "manual"}:
            source = ""
        end_state = end_state if isinstance(end_state, dict) else {}
        result = str(end_state.get("result", "*") or "*").strip()
        if result not in {"1-0", "0-1", "1/2-1/2", "*"}:
            result = "*"
        metadata = {
            "Event": self.session_label or "ChessListener live game",
            "Site": "ChessListener local capture",
        }
        if source:
            metadata["Source"] = source
        return {
            "initial_fen": initial_fen,
            "moves": [],
            "result": result,
            "label": "Final position only",
            "metadata": metadata,
            "imported": False,
            "completed": True,
            "history_complete": False,
            "position_only": True,
            "source": source,
            "session_id": session_identifier,
        }

    def review_settings_identity(self, settings):
        if study_store is not None:
            return study_store.settings_key(settings)
        return json.dumps(settings, sort_keys=True, separators=(",", ":"))

    def make_review_identity(self, settings):
        self.review_generation += 1
        return {
            "generation": self.review_generation,
            "game": self.review_record_identity(),
            "settings": self.review_settings_identity(settings),
        }

    def review_message_is_current(self, message):
        identity = message.get("review_identity")
        if not isinstance(identity, dict) or identity != self.review_active_identity:
            return False
        if self.review_cancelling or not self.review_record:
            return False
        if identity.get("game") != self.review_record_identity():
            return False
        settings = self.review_job_settings or self.review_settings_used or {}
        return identity.get("settings") == self.review_settings_identity(settings)

    def set_review_transition_controls(self, enabled):
        games_available = False
        if enabled and self.review_store is not None:
            try:
                games_available = bool(self.review_store.list_games())
            except (OSError, ValueError) as error:
                self.report_library_error(error)
        combo = getattr(self, "review_library_combo", None)
        delete = getattr(self, "review_delete_button", None)
        importer = getattr(self, "review_import_button", None)
        if combo is not None:
            combo.setEnabled(games_available)
        if delete is not None:
            delete.setEnabled(games_available)
        if importer is not None:
            importer.setEnabled(bool(enabled))

    def invalidate_review_job(self, message="Cancelling review…"):
        """Cancel a worker and make every already-queued result stale."""
        if self.review_job is None:
            self.review_active_identity = None
            return False
        self.review_generation += 1
        self.review_active_identity = None
        self.review_cancelling = True
        self.review_job.cancel()
        self.review_summary.setText(message)
        self.review_cancel_button.setText("Cancelling…")
        self.review_cancel_button.setEnabled(False)
        self.review_start_button.setEnabled(False)
        self.review_start_button.setText("Cancelling…")
        self.set_review_transition_controls(False)
        return True

    def save_completed_game(self, automatic=False):
        """Idempotently persist the best honest record available at game end."""
        if self.review_store is None:
            if self.review_store_error:
                self.report_library_error(self.review_store_error)
            else:
                self.set_status(
                    "Local library support is unavailable; the game was not saved",
                    "warn", linger=False,
                )
            return False
        record = self.copy_game_record(self.completed_game_record)
        if not record:
            self.set_status(
                "No verified history was captured for this completed game",
                "warn", linger=False,
            )
            return False
        record["completed"] = True
        # A native game_record is emitted only after exact legal replay.  If a
        # future partial-history source is added, preserve its explicit False.
        record["history_complete"] = bool(record.get("history_complete", False))
        try:
            identifier, created = self.review_store.save_completed_game(record)
        except (OSError, ValueError) as error:
            self.completed_game_saved = False
            self.set_status(f"Could not save completed game: {error}", "warn", linger=False)
            return False
        self.completed_game_id = identifier
        self.completed_game_saved = True
        self.completed_game_record["completed"] = True
        review_matches_completed = bool(
            self.review_record
            and self.review_record_identity(self.review_record)
            == self.review_record_identity(self.completed_game_record)
        )
        if review_matches_completed:
            self.review_record["completed"] = True
            self.review_game_id = identifier
        # Saving a new completed fallback must not make an older imported
        # review appear selected. Keep the visible model and combo/delete id
        # paired; the dedicated post-game Review action switches atomically.
        self.populate_review_library(
            identifier if review_matches_completed else self.review_game_id
        )
        callback = getattr(self, "update_game_finished_save_state", None)
        if callable(callback):
            callback(True)
        if automatic:
            self.set_status("Completed game saved locally", "info", linger=True)
        elif created:
            self.set_status("Game saved locally", "info", linger=True)
        else:
            self.set_status("Game is already saved locally", "info", linger=True)
        return True

    def open_review_for_completed_game(self, run=False):
        if not self.completed_game_record:
            self.set_status("No completed game is available", "warn", linger=False)
            return False
        record = self.copy_game_record(self.completed_game_record)
        record["_completed_record"] = True
        if not self.apply_game_record(record):
            self.set_status("The completed game record is no longer valid", "warn", linger=False)
            return False
        if self.completed_game_saved and self.completed_game_id:
            self.review_game_id = self.completed_game_id
        self.open_review()
        if self.review_positions:
            self.select_review_ply(len(self.review_positions) - 1)
        if run:
            QTimer.singleShot(0, self.start_game_review)
        return True

    def explore_completed_game(self):
        if not self.open_review_for_completed_game(run=False):
            return False
        if not self.review_positions:
            return False
        self.select_review_ply(len(self.review_positions) - 1)
        if self.review_mode != "explore":
            self.toggle_review_explore()
        return self.review_mode == "explore"

    def export_completed_game(self):
        if not self.completed_game_record:
            self.set_status("No completed game is available", "warn", linger=False)
            return False
        record = self.copy_game_record(self.completed_game_record)
        record["_completed_record"] = True
        if not self.apply_game_record(record):
            return False
        self.export_review_pgn()
        return True

    def close_review(self):
        self.leave_review_explore()
        if self.local_mode:
            self.close()
            return
        self.save_geometry()
        self.stack.setCurrentWidget(self.analysis_page)
        self.leave_workspace(self.analysis_page)

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
        imported = bool(state.get("imported", False))
        library_record = bool(state.get("_library_record", False))
        completed_record = bool(state.get("_completed_record", False))
        session_identifier = str(state.get("session_id", "")).strip()
        if not imported and not session_identifier:
            session_identifier = self.session_id
        bind_to_live_session = bool(
            self.session_active
            and not imported
            and not library_record
            and not completed_record
            and session_identifier
            and session_identifier == self.session_id
        )
        fingerprint = (session_identifier, initial_fen, tuple(moves))
        old = self.review_record
        raw_metadata = state.get("metadata")
        metadata = {
            str(key): str(value)
            for key, value in raw_metadata.items()
        } if isinstance(raw_metadata, dict) else {}
        label = str(state.get("label", "")).strip() or self.session_label or (
            f"Local game · {len(moves)} plies"
        )
        result = str(
            state.get("result", old.get("result", "*") if old else "*") or "*"
        ).strip()
        if result not in {"1-0", "0-1", "1/2-1/2", "*"}:
            result = "*"
        if old and old.get("fingerprint") == fingerprint:
            old.update({
                "result": result,
                "label": label or old.get("label", "Local game"),
                "metadata": metadata or old.get("metadata", {}),
                "imported": bool(state.get("imported", old.get("imported", False))),
                "completed": bool(state.get("completed", old.get("completed", False))),
                "history_complete": bool(
                    state.get("history_complete", old.get("history_complete", True))
                ),
                "position_only": bool(
                    state.get("position_only", old.get("position_only", False))
                ),
                "source": str(state.get("source", old.get("source", ""))),
            })
            if bind_to_live_session:
                self.live_game_record = self.copy_game_record(old)
            self.update_review_surface()
            return True
        self.invalidate_review_job("Cancelling review for the previous game…")
        self.review_record = {
            "initial_fen": initial_fen,
            "moves": moves,
            "result": result,
            "label": label,
            "metadata": metadata,
            "imported": bool(state.get("imported", False)),
            "completed": bool(state.get("completed", False)),
            "history_complete": bool(state.get("history_complete", True)),
            "position_only": bool(state.get("position_only", False)),
            "source": str(state.get("source", "")),
            "session_id": session_identifier,
            "fingerprint": fingerprint,
        }
        self.review_results = []
        self.review_positions = positions
        self.review_position_analyses = []
        self.review_game_id = None
        if not completed_record:
            self.completed_game_id = None
            self.completed_game_saved = False
        self.review_settings_used = None
        self.leave_review_explore()
        self.review_moves.clear()
        self.review_graph.set_values([])
        self.review_export_button.setEnabled(True)
        self.review_button.setEnabled(True)
        self.review_start_button.setEnabled(self.review_job is None)
        self.review_explore_button.setEnabled(bool(positions))
        self.review_selected_ply = len(positions) - 1
        self.select_review_ply(self.review_selected_ply)
        source = "Imported local record" if state.get("imported") else "Verified local record"
        self.review_summary.setText(
            f"{source} · {len(moves)} plies · "
            f"result {self.review_record['result']}. "
            "Run review when you want; live analysis remains independent."
        )
        if bind_to_live_session:
            self.live_game_record = self.copy_game_record(self.review_record)
        self.update_review_surface()
        return True

    def apply_imported_record(self, record):
        self.invalidate_review_job("Cancelling review before import…")
        self.leave_review_explore()
        state = dict(record)
        state["imported"] = True
        if not self.apply_game_record(state):
            raise ValueError("The imported game could not be replayed")
        save_error = self.review_store_error or (
            "Local library support is unavailable"
            if self.review_store is None else None
        )
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

    def retire_legacy_review_archive(self):
        """Remove the pre-library QSettings duplicate after durable migration.

        Schema-2 JSON is the authoritative game/review store.  Keeping the old
        ``review/latest`` snapshot after a successful JSON write makes a game
        that the user deletes from the library reappear on the next startup.
        """
        if self.settings.contains("review/latest"):
            self.settings.remove("review/latest")
            self.settings.sync()

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
            try:
                library_present = self.review_store.archive_present()
                games = self.review_store.list_games()
            except (OSError, ValueError) as error:
                # An existing JSON library remains authoritative even when it
                # is malformed or unreadable.  Never hide that failure by
                # resurrecting the stale pre-library QSettings snapshot.
                self.report_library_error(error)
                return
            if games:
                self.load_library_game(games[0])
                self.populate_review_library(games[0].get("id"))
                self.retire_legacy_review_archive()
                return
            if library_present:
                # A valid empty JSON archive can be the result of deliberate
                # deletion.  Retire the obsolete duplicate instead of making
                # that deleted game reappear.
                self.retire_legacy_review_archive()
                self.populate_review_library()
                return
        raw = self.settings.value("review/latest", "")
        if not isinstance(raw, str) or not raw:
            return
        try:
            saved = json.loads(raw)
            record = saved["record"]
            restored = self.apply_game_record({
                "initial_fen": record["initial_fen"],
                "uci_moves": "|".join(record["moves"]),
                "result": record.get("result", "*"),
                "label": record.get("label", "Local game"),
                "metadata": record.get("metadata") or {},
                "imported": record.get("imported", False),
                "completed": record.get("completed", False),
                "history_complete": record.get("history_complete", False),
                "position_only": record.get("position_only", False),
                "source": record.get("source", ""),
                "session_id": record.get("session_id", ""),
                "_library_record": True,
            })
            if not restored:
                raise ValueError("legacy review record is invalid")
            self.finish_game_review({
                "reviews": saved["reviews"],
                "positions": saved["positions"],
                "position_analyses": saved.get("position_analyses") or [],
            }, persist=False)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.settings.remove("review/latest")
            self.settings.sync()
            return

        # One-time migration from releases that predated the schema-2 JSON
        # library.  The old snapshot did not record the engine settings that
        # produced its cached lines, so migrate the validated game record but
        # do not mislabel those lines as a cache for today's settings.
        if self.review_store is not None and self.review_record is not None:
            try:
                self.review_game_id = self.review_store.save_record(
                    self.review_record
                )
            except (OSError, ValueError) as error:
                self.set_status(
                    f"Legacy review restored, but could not migrate it: {error}",
                    "warn", linger=False,
                )
            else:
                self.retire_legacy_review_archive()
        self.populate_review_library(self.review_game_id)

    def populate_review_library(self, selected_id=None):
        if not hasattr(self, "review_library_combo"):
            return
        if self.review_store is None and self.review_store_error:
            self.report_library_error(self.review_store_error)
            return []
        self.applying_settings = True
        self.review_library_combo.clear()
        try:
            games = self.review_store.list_games() if self.review_store is not None else []
        except (OSError, ValueError) as error:
            self.applying_settings = False
            self.report_library_error(error)
            return []
        current_unsaved = bool(self.review_record and not self.review_game_id)
        if current_unsaved:
            label = str(self.review_record.get("label", "Current verified game"))
            self.review_library_combo.addItem(
                f"{label} · not saved", "__current_unsaved__"
            )
        if games:
            for game in games:
                self.review_library_combo.addItem(str(game.get("label", "Local game")), game.get("id"))
        elif not current_unsaved:
            self.review_library_combo.addItem("No saved reviews", None)
        if selected_id:
            self.select_data(self.review_library_combo, selected_id, 0)
        self.review_library_combo.setEnabled(bool(games or current_unsaved))
        self.review_delete_button.setEnabled(bool(games))
        self.applying_settings = False
        return games

    def load_library_game(self, game):
        reviews = game.get("reviews") or {}
        self.apply_game_record({
            "initial_fen": game["initial_fen"],
            "uci_moves": "|".join(game["moves"]),
            "result": game.get("result", "*"),
            "label": game.get("label", "Local game"),
            "metadata": game.get("metadata") or {},
            "imported": game.get("imported", False),
            "completed": game.get("completed", False),
            "history_complete": game.get("history_complete", False),
            "position_only": game.get("position_only", False),
            "source": game.get("source", ""),
            "session_id": game.get("session_id", ""),
            "_library_record": True,
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
        try:
            game = self.review_store.find(identifier)
        except (OSError, ValueError) as error:
            self.report_library_error(error)
            return
        if game is not None:
            self.invalidate_review_job("Cancelling review before switching games…")
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
        self.invalidate_review_job("Cancelling review before deletion…")
        try:
            deleted = bool(identifier and self.review_store.delete(identifier))
            games = self.review_store.list_games() if deleted else []
        except (OSError, ValueError) as error:
            self.report_library_error(error)
            return
        if deleted:
            self.retire_legacy_review_archive()
            if str(identifier) == str(self.completed_game_id or ""):
                self.completed_game_id = None
                self.completed_game_saved = False
                self.update_game_finished_save_state(False)
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
                self.update_review_surface()

    def persist_review_archive(self):
        if not self.review_record or not self.review_positions:
            return
        if self.review_store is not None and self.review_settings_used is not None:
            try:
                self.review_game_id, _key = self.review_store.save_review(
                    self.review_record, self.review_settings_used,
                    self.review_results, self.review_positions,
                    self.review_position_analyses,
                )
                self.populate_review_library(self.review_game_id)
                self.retire_legacy_review_archive()
            except (OSError, ValueError) as error:
                self.set_status(f"Could not save review library: {error}", "warn", linger=False)
        elif self.review_store is None and self.review_store_error:
            self.report_library_error(self.review_store_error)

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
        self.review_settings_used = dict(settings)
        if self.review_store is not None and study_store is not None:
            identifier = study_store.record_id(self.review_record)
            try:
                cached = self.review_store.cached_review(
                    identifier, study_store.settings_key(settings)
                )
            except (OSError, ValueError) as error:
                self.report_library_error(error)
                cached = None
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
        identity = self.make_review_identity(settings)
        self.review_active_identity = identity
        self.review_cancelling = False
        self.review_job_record = {
            key: value for key, value in self.review_record.items()
            if key != "fingerprint"
        }
        self.review_job_settings = dict(settings)
        self.review_job, self.review_queue = review_rules.start_review(
            self.review_record["initial_fen"],
            self.review_record["moves"], settings, identity,
        )
        total = len(self.review_record["moves"]) + 1
        self.review_progress.setRange(0, total)
        self.review_progress.setValue(0)
        self.review_progress.show()
        self.review_start_button.setEnabled(False)
        self.review_start_button.setText("Reviewing…")
        self.review_cancel_button.setEnabled(True)
        self.review_cancel_button.setText("Cancel")
        self.set_review_transition_controls(False)
        count = len(self.review_record["moves"])
        self.review_summary.setText(
            f"Reviewing {count} plies locally\u2026" if count
            else "Analysing the imported position locally\u2026"
        )
        self.review_timer.start()

    def cancel_game_review(self):
        self.invalidate_review_job()

    def poll_review(self):
        if self.review_queue is None:
            self.review_timer.stop()
            return
        output = self.review_queue
        while True:
            try:
                message = output.get_nowait()
            except queue.Empty:
                break
            if not self.review_message_is_current(message):
                continue
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
                self.review_cancelling = kind == "cancelled"
        if self.review_job is not None and not self.review_job.is_alive():
            self.finish_review_job()

    def finish_review_job(self):
        self.review_timer.stop()
        self.review_job = None
        self.review_queue = None
        self.review_active_identity = None
        self.review_cancelling = False
        self.review_job_record = None
        self.review_job_settings = None
        self.review_start_button.setEnabled(bool(self.review_record))
        self.review_start_button.setText("Run local review")
        self.review_cancel_button.setEnabled(False)
        self.review_cancel_button.setText("Cancel")
        self.review_progress.hide()
        self.set_review_transition_controls(True)

    def finish_game_review(self, message, persist=True):
        if "review_identity" in message and not self.review_message_is_current(message):
            return False
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
        point_by_ply = {
            int(item.get("ply", 0)): item
            for item in self.review_results
            if isinstance(item, dict)
        }
        graph_points = [
            {
                "ply": index,
                "san": str(point_by_ply.get(index, {}).get("san", "")),
                "classification": str(
                    point_by_ply.get(index, {}).get("classification", "")
                ),
                "loss": point_by_ply.get(index, {}).get("loss", 0),
            }
            for index in range(len(graph_values))
        ]
        self.review_graph.set_values(graph_values, points=graph_points)

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
        if (
            self.review_job is None
            or "review_identity" in message
            or not self.review_cancelling
        ):
            self.finish_review_job()
        return True

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

        self.candidate_placeholder.setVisible(not bool(self.lines))
        self.candidate_scroll.setVisible(not self.compact and not self.postgame_visible)
        selected = self.selected_candidate()
        if not self.preview_step:
            self.preview_moves = pv_moves(selected) if selected else []
        self.update_analysis_mode_ui()

    def refresh_explanation(self):
        if not hasattr(self, "explanation_label"):
            return

        if self.explanation_level == "off" or explanation_rules is None:
            self.explanation_label.clear()
            self.explanation_label.hide()
            self.explanation_scroll.hide()
            return
        if not self.lines:
            self.explanation_label.setText(
                "Move reasoning will appear here with the first completed line."
            )
            visible = not self.compact and not self.postgame_visible
            self.explanation_label.setVisible(visible)
            self.explanation_scroll.setVisible(visible)
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
        visible = bool(text) and not self.compact and not self.postgame_visible
        self.explanation_label.setVisible(visible)
        self.explanation_scroll.setVisible(visible)

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
        narrow = self.width() < 380
        visible_moves = 2 if narrow else 6
        shown = path[-visible_moves:]
        prefix = "Root" + (" › …" if len(path) > visible_moves else "")
        self.breadcrumb_label.setText(prefix + "".join(f" › {move}" for move in shown))
        self.breadcrumb_label.setWordWrap(not narrow)
        self.breadcrumb_label.setVisible(not self.compact)

    def update_analysis_mode_ui(self):
        if not hasattr(self, "live_toolbar"):
            return
        if self.postgame_visible:
            self.update_page_chrome()
            return
        exploring = self.mode == "explore"
        self.board.set_interactive(exploring and not self.explore_pending)
        self.live_toolbar.setVisible(not exploring and not self.compact)
        self.explore_toolbar.setVisible(exploring and not self.compact)
        self.resume_button.setVisible(
            not exploring and self.resume_branch_id is not None and not self.compact
        )
        total = min(len(self.preview_moves), self.display_pv_limit())
        self.preview_label.setText(f"Line {self.preview_step}/{total}")
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
        if exploring:
            self.analysis_context_label.setText(
                "Private branch · the real game remains unchanged"
            )
        elif self.preview_step:
            self.analysis_context_label.setText(
                "Hypothetical line · evaluation remains at the root position"
            )
        self.analysis_context.setVisible(
            (exploring or self.preview_step > 0) and not self.compact
        )
        self.update_breadcrumb()
        self.update_analysis_state_label()
        self.update_page_chrome()
        self.update_source_badge()

    def update_analysis_state_label(self):
        if not hasattr(self, "analysis_state_label"):
            return
        if self.postgame_visible:
            text = ""
        elif self.synchronising:
            text = "Synchronizing board state"
        elif self.explore_pending:
            text = "Applying Analysis Lab change"
        elif not self.fen:
            text = "Waiting for a board position"
        elif not self.has_eval:
            text = "Analyzing" + (f" · depth {self.depth}" if self.depth else "")
        elif self.analysis_final:
            text = f"Completed · depth {self.depth}" if self.depth else "Completed"
        else:
            text = f"Analyzing · depth {self.depth}" if self.depth else "Analyzing"
        self.analysis_state_label.setText(text)
        self.analysis_state_label.setAccessibleDescription(text)

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

    def restore_completed_game_position(self):
        """Put the verified ending position back on the visible live board.

        Preview and Analysis Lab deliberately replace ``self.fen`` with a
        hypothetical position.  At game end the completed record has already
        been bound to the ending live session, so replay that record before
        clearing transient workspace state.  This keeps the saved/reviewed
        truth and the board the player sees identical.
        """
        record = self.completed_game_record
        if not record:
            return False
        try:
            positions = self.build_record_positions(
                record["initial_fen"], record.get("moves") or []
            )
            final_fen = positions[-1]
            grid, side = fen_to_grid(final_fen)
        except (KeyError, ValueError, AttributeError, IndexError):
            return False

        authoritative_state = self.authoritative_live_state()
        self.fen = final_fen
        self.grid = grid
        self.side_to_move = side
        self.analysis_fen = final_fen
        self.flip = bool(authoritative_state.get("flip", self.flip))
        self.state_source = str(
            record.get("source")
            or authoritative_state.get("source")
            or self.state_source
        )
        moves = list(record.get("moves") or [])
        self.last_move = moves[-1] if moves else ""
        self.last_san = ""
        if moves and len(positions) >= 2:
            try:
                previous_grid, _previous_side = fen_to_grid(positions[-2])
                self.last_san = name_move(positions[-2], previous_grid, moves[-1])
            except ValueError:
                pass
        self.board.set_position(self.grid, self.side_to_move, self.flip, self.fen)
        self.board.set_moves("", "", self.last_move)
        self.dirty = True
        return True

    def apply_session(self, state):
        event = state.get("event", "")

        if event == "started":
            self.cancel_game_review()
            hide_finished = getattr(self, "hide_game_finished_panel", None)
            if callable(hide_finished):
                hide_finished()
            self.completed_game_id = None
            self.completed_game_saved = False
            self.live_game_record = None
            self.completed_game_record = None
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

        # Native/content recovery may repeat an end notification.  The first
        # one consumes the active session and establishes the post-game record;
        # later copies must not clear or duplicate that completed state.
        if not self.session_active:
            return

        reason = str(state.get("reason", "ended"))
        keep_final_board = reason in {
            "game_ended",
            "game-ended",
            "game_end",
            "completed",
        }
        ending_session_id = self.session_id
        live_record = self.copy_game_record(self.live_game_record)
        authoritative_final_fen = self.canonical_position_fen(
            self.authoritative_live_state().get("fen")
        )
        record_final_fen = self.game_record_final_fen(live_record)
        record_matches_session = bool(
            live_record
            and ending_session_id
            and str(live_record.get("session_id") or "") == ending_session_id
            and authoritative_final_fen
            and record_final_fen == authoritative_final_fen
        )
        ended_result = str(state.get("result", "*") or "*").strip()
        if ended_result not in {"1-0", "0-1", "1/2-1/2", "*"}:
            ended_result = "*"
        if (
            record_matches_session and ended_result != "*"
            and str(live_record.get("result", "*") or "*") == "*"
        ):
            live_record["result"] = ended_result
        self.completed_game_record = None
        if keep_final_board:
            self.completed_game_record = (
                live_record if record_matches_session else
                self.position_only_record_for_session(ending_session_id, state)
            )

        if keep_final_board:
            self.restore_completed_game_position()

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
            message_kind = "info"
            message = (
                "Game ended — save or review it locally"
                if self.completed_game_record
                else "Game ended — verified move history was unavailable"
            )
            if self.completed_game_record:
                self.completed_game_record["completed"] = True
            if self.completed_game_record and self.auto_save_completed:
                if self.save_completed_game(automatic=True):
                    message = "Game ended — saved locally"
                else:
                    # save_completed_game() provides the useful failure
                    # reason; do not immediately cover it with a neutral end
                    # notification.
                    message = self.status_text or "Game ended — automatic save failed"
                    message_kind = "warn"
            show_finished = getattr(self, "show_game_finished_panel", None)
            if callable(show_finished):
                show_finished()
            if self.completed_game_record and self.review_auto:
                self.open_review_for_completed_game(run=True)
        else:
            self.completed_game_record = None
            self.clear_position()
            message = "Session stopped"
            message_kind = "info"
            hide_finished = getattr(self, "hide_game_finished_panel", None)
            if callable(hide_finished):
                hide_finished()

        self.set_status(message, message_kind, linger=False)

    def apply_recovery(self, state):
        action = str(state.get("action", ""))

        if "accepted" in state or "ok" in state:
            accepted = bool(state.get("accepted", state.get("ok")))
            self.recovery_action = ""
            text = str(state.get("text", "")).strip()
            kind = str(state.get("kind", "info" if accepted else "warn"))

            if accepted:
                self.clear_recovery_error()
            else:
                # A rejected manual repair returns to the same populated form;
                # never make the user reconstruct the attempted FEN again.
                self.stack.setCurrentWidget(self.recovery_page)
                field = self.recovery_exact_fen if action.lower() == "fen" else None
                self.show_recovery_error(
                    text or "The native host rejected this recovery request.",
                    field,
                )

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
        if (
            not hasattr(self, "stack")
            or self.stack.currentWidget() is not self.analysis_page
        ):
            self.source_label.hide()
            return
        source = self.state_source if self.state_source in {
            "exact", "inferred", "manual"
        } else ""

        if self.preview_step:
            text, object_name = "Preview", "sourcePreview"
            tooltip = (
                "Hypothetical continuation. The evaluation still belongs to "
                "the root live position."
            )
        elif self.mode == "explore":
            text, object_name = "Lab", "sourceExplore"
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
        self.source_label.setAccessibleName(f"Position source: {text}")
        self.source_label.setAccessibleDescription(tooltip)
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
            had_pending_edits = self.study_dirty
            if not self.flush_study_edits(refresh_library=False):
                self.set_study_save_state(
                    "Save failed — window remains open. Try again.", failed=True
                )
                if not self.confirm_discard_failed_study_on_close():
                    event.ignore()
                    return
                self.study_autosave_timer.stop()
            # Annotation edits were written by flush_study_edits().  With no
            # dirty editor fields, still take a final snapshot so structural
            # changes (branch order/collapse/selection) receive the same
            # close-safety guarantee.
            if not had_pending_edits and not self.persist_current_study():
                self.study_dirty = True
                self.set_study_save_state(
                    "Save failed — window remains open. Try again.", failed=True
                )
                if not self.confirm_discard_failed_study_on_close():
                    event.ignore()
                    return
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
