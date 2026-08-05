#!/usr/bin/env python3
"""Always-on-top board + analysis overlay.

Reads one JSON object per line on stdin, e.g.

  {"fen": "rnbq...  b KQkq - 3 3",
   "flip": false,
   "best":  {"move": "c6d4", "cp": -38, "mate": null, "pv": "c6d4 f3d4 e5d4"},
   "human": {"move": "g8f6", "cp": -14},
   "lines": [{"move":"c6d4","cp":-38},{"move":"g8f6","cp":-58}]}

Every field except "fen" is optional. Unknown fields are ignored, so the C
host can grow the protocol without breaking the UI.
"""

import json
import os
import sys

from PyQt6.QtCore import Qt, QSocketNotifier
from PyQt6.QtGui import QFont, QGuiApplication
from PyQt6.QtWidgets import (
    QApplication, QFrame, QGridLayout, QLabel, QVBoxLayout, QWidget,
)

APP_ID = "chess-overlay"          # KWin matches window rules on this
SQUARE = 34

LIGHT, DARK = "#b9a68b", "#7d6b52"
HL_BEST, HL_HUMAN = "#4a7fb5", "#b57a3a"

# Filled glyphs for BOTH colours, tinted via text colour. The outline glyphs
# (U+2654..) disappear against light squares in most fonts.
GLYPH = {"k": "\u265a", "q": "\u265b", "r": "\u265c",
         "b": "\u265d", "n": "\u265e", "p": "\u265f"}


def fen_to_grid(fen):
    """-> (list of 64 chars indexed a8..h1, side_to_move)."""
    parts = fen.split()
    rows = parts[0].split("/")
    if len(rows) != 8:
        raise ValueError("bad FEN board field")
    grid = []
    for row in rows:
        n = 0
        for ch in row:
            if ch.isdigit():
                grid.extend("." * int(ch))
                n += int(ch)
            else:
                grid.append(ch)
                n += 1
        if n != 8:
            raise ValueError("bad FEN rank: " + row)
    side = parts[1] if len(parts) > 1 else "w"
    return grid, side


def sq_index(name):
    """'e4' -> index into the a8..h1 grid, or None."""
    if not name or len(name) < 2:
        return None
    f, r = ord(name[0]) - ord("a"), ord(name[1]) - ord("1")
    if not (0 <= f < 8 and 0 <= r < 8):
        return None
    return (7 - r) * 8 + f


def fmt_score(cp, mate):
    if mate is not None:
        return f"#{'' if mate > 0 else '-'}{abs(mate)}"
    if cp is None:
        return "--"
    return f"{cp / 100:+.2f}"


class Overlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Analysis")
        # Honoured on X11, ignored on Wayland -- the KWin rule is what
        # actually pins this. Harmless to ask for both.
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        board = QFrame()
        self.grid = QGridLayout(board)
        self.grid.setSpacing(0)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.cells = []
        font = QFont()
        font.setPointSize(int(SQUARE * 0.62))
        for i in range(64):
            lab = QLabel("")
            lab.setFixedSize(SQUARE, SQUARE)
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lab.setFont(font)
            self.grid.addWidget(lab, i // 8, i % 8)
            self.cells.append(lab)
        root.addWidget(board)

        self.eval_lab = QLabel("--")
        f = QFont(); f.setPointSize(15); f.setBold(True)
        self.eval_lab.setFont(f)
        root.addWidget(self.eval_lab)

        self.best_lab = QLabel("engine  --")
        self.human_lab = QLabel("human   --")
        self.pv_lab = QLabel("")
        self.pv_lab.setWordWrap(True)
        mono = QFont("monospace"); mono.setPointSize(9)
        for w in (self.best_lab, self.human_lab, self.pv_lab):
            w.setFont(mono)
            root.addWidget(w)

        self.render_state({"fen": "8/8/8/8/8/8/8/8 w - - 0 1"})

    # -- rendering ------------------------------------------------------
    def render_state(self, st):
        try:
            grid, side = fen_to_grid(st["fen"])
        except (KeyError, ValueError) as exc:
            print(f"overlay: {exc}", file=sys.stderr)
            return

        flip = bool(st.get("flip"))
        best = st.get("best") or {}
        human = st.get("human") or {}

        hl = {}
        for key, colour in (("best", HL_BEST), ("human", HL_HUMAN)):
            mv = (st.get(key) or {}).get("move") or ""
            for name in (mv[0:2], mv[2:4]):
                idx = sq_index(name)
                if idx is not None:
                    hl.setdefault(idx, colour)

        for view in range(64):
            idx = 63 - view if flip else view
            piece = grid[idx]
            lab = self.cells[view]
            base = LIGHT if (idx // 8 + idx % 8) % 2 == 0 else DARK
            bg = hl.get(idx, base)
            fg = "#f8f8f8" if piece.isupper() else "#101010"
            lab.setText(GLYPH.get(piece.lower(), ""))
            lab.setStyleSheet(f"background:{bg}; color:{fg};")

        cp, mate = best.get("cp"), best.get("mate")
        # Scores arrive from the side-to-move's POV; show white's POV so the
        # number doesn't flip sign every half-move.
        if side == "b" and cp is not None:
            cp = -cp
        if side == "b" and mate is not None:
            mate = -mate
        self.eval_lab.setText(f"{fmt_score(cp, mate)}   ({side} to move)")

        self.best_lab.setText(f"engine  {best.get('move', '--'):<6}")
        self.human_lab.setText(f"human   {human.get('move', '--'):<6}")

        alts = st.get("lines") or []
        self.pv_lab.setText("  ".join(
            f"{l.get('move','?')} {fmt_score(l.get('cp'), l.get('mate'))}"
            for l in alts[:3]
        ) or best.get("pv", ""))


class StdinReader:
    """Line-assembling reader. os.read + our own buffer, because a
    QSocketNotifier tells us the fd is ready, not that a full line arrived."""

    def __init__(self, on_line):
        self.on_line = on_line
        self.buf = b""
        self.fd = sys.stdin.fileno()
        self.note = QSocketNotifier(self.fd, QSocketNotifier.Type.Read)
        self.note.activated.connect(self.ready)

    def ready(self, _):
        try:
            chunk = os.read(self.fd, 65536)
        except OSError:
            chunk = b""
        if not chunk:                      # host closed the pipe
            self.note.setEnabled(False)
            QApplication.quit()
            return
        self.buf += chunk
        while b"\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n", 1)
            raw = raw.strip()
            if not raw:
                continue
            try:
                self.on_line(json.loads(raw))
            except json.JSONDecodeError as exc:
                print(f"overlay: bad JSON: {exc}", file=sys.stderr)


def main():
    app = QApplication(sys.argv)
    # Both are needed: WM_CLASS on X11, app_id on Wayland. The KWin rule
    # keys off this, so don't change it casually.
    app.setApplicationName(APP_ID)
    QGuiApplication.setDesktopFileName(APP_ID)

    win = Overlay()
    reader = StdinReader(win.render_state)   # noqa: F841  (keep alive)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
