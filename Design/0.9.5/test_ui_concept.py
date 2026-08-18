#!/usr/bin/env python3
"""Structural regression checks for the 0.9.5 interactive UI concept."""

from __future__ import annotations

import math
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path


HERE = Path(__file__).resolve().parent
HTML_PATH = HERE / "ui-concept.html"


class ConceptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.views: list[str] = []
        self.options: list[tuple[str, str]] = []
        self.boards: list[dict[str, str]] = []
        self._in_view_select = False
        self._option_value: str | None = None
        self._option_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        classes = set(values.get("class", "").split())
        if tag == "select" and values.get("id") == "cl-view-select":
            self._in_view_select = True
        elif tag == "option" and self._in_view_select:
            self._option_value = values.get("value", "")
            self._option_text = []
        if "screen" in classes and values.get("data-view"):
            self.views.append(values["data-view"])
        if "chess-board" in classes:
            self.boards.append(values)

    def handle_data(self, data: str) -> None:
        if self._option_value is not None:
            self._option_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._option_value is not None:
            self.options.append((self._option_value, "".join(self._option_text).strip()))
            self._option_value = None
            self._option_text = []
        elif tag == "select" and self._in_view_select:
            self._in_view_select = False


def expanded_rank_width(rank: str) -> int:
    return sum(int(character) if character.isdigit() else 1 for character in rank)


def arrow_segment(move: str, kind: str) -> tuple[float, float, float, float]:
    files = "abcdefgh"
    from_x = (files.index(move[0]) + 0.5) * 12.5
    from_y = (8 - int(move[1]) + 0.5) * 12.5
    to_x = (files.index(move[2]) + 0.5) * 12.5
    to_y = (8 - int(move[3]) + 0.5) * 12.5
    dx, dy = to_x - from_x, to_y - from_y
    distance = max(1.0, math.hypot(dx, dy))
    ux, uy = dx / distance, dy / distance
    side = 0.75 if kind == "maia" else -0.75
    offset_x, offset_y = -uy * side, ux * side
    return (
        from_x + ux * 2.4 + offset_x,
        from_y + uy * 2.4 + offset_y,
        to_x - ux * 4.6 + offset_x,
        to_y - uy * 4.6 + offset_y,
    )


def main() -> None:
    html = HTML_PATH.read_text(encoding="utf-8")
    parser = ConceptParser()
    parser.feed(html)

    option_views = [value for value, _ in parser.options]
    assert parser.views == option_views, (parser.views, option_views)
    assert len(parser.views) == 10
    assert {"live", "postgame", "compact", "lab", "review", "studies", "settings", "recovery", "startup", "popup"} == set(parser.views)

    for board in parser.boards:
        ranks = board["data-fen"].split()[0].split("/")
        assert len(ranks) == 8, board["data-fen"]
        assert all(expanded_rank_width(rank) == 8 for rank in ranks), board["data-fen"]
        arrows = board.get("data-arrows", "")
        for entry in filter(None, arrows.split(",")):
            move, _, kind = entry.partition(":")
            assert re.fullmatch(r"[a-h][1-8][a-h][1-8]", move), entry
            segment = arrow_segment(move, kind)
            assert all(0.0 <= coordinate <= 100.0 for coordinate in segment), segment
            assert segment[:2] != segment[2:]

    assert 'data-piece-style="outline"' in html
    assert '<option value="outline" selected>Outline set</option>' in html
    assert '<option value="solid">Solid silhouettes</option>' in html
    assert 'k: "♔", q: "♕", r: "♖", b: "♗", n: "♘", p: "♙"' in html
    assert 'k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟"' in html
    assert "rank-label" not in html and "file-label" not in html
    assert "rank-axis" in html and "file-axis" in html
    assert "arrow-halo" in html and "toX - ux * 4.6" in html

    postgame = re.search(r'<section class="screen" data-view="postgame".*?</section>', html, re.S)
    assert postgame
    for label in ("Save game", "Run local review", "Explore final position", "Export PGN"):
        assert label in postgame.group(0)
    assert html.count("cl-auto-save-games") >= 4
    assert "Automatically save completed games" in html
    assert "Analyse" not in html and "Analysing" not in html

    node_program = r"""
const fs = require('fs');
const html = fs.readFileSync(process.argv[1], 'utf8');
const scripts = Array.from(html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g), m => m[1]);
const app = scripts.find(code => code.includes('var root = document.getElementById("cl095")'));
if (!app) throw new Error('app script not found');
new Function(app);
"""
    subprocess.run(["node", "-e", node_program, str(HTML_PATH)], check=True)
    print(f"PASS: {len(parser.views)} screens, {len(parser.boards)} boards, piece styles, arrows, and post-game flow")


if __name__ == "__main__":
    main()
