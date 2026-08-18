#!/usr/bin/env python3
"""Validated local variation trees and annotated PGN export.

Studies are UI-owned data.  They never replace the native host's authoritative
live position and they never feed Chess.com.  Keeping this model dependency
free (apart from ChessListener's legal board helper) also makes saved studies
available in ``overlay.py --local`` mode.
"""

import copy
import re
import time

import san
import pgn_import


STANDARD_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
MAX_STUDY_NODES = 512
MAX_TITLE = 120
MAX_NAME = 120
MAX_COMMENT = 4000
MAX_PV_PLIES = 32
MAX_LINES = 5
UCI_RE = re.compile(r"^[a-h][1-8][a-h][1-8][qrbn]?$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def canonical_fen(value):
    if not isinstance(value, str) or len(value) > 256:
        raise ValueError("Study position must be a complete six-field FEN")
    try:
        return pgn_import.canonical_fen(value)
    except pgn_import.ImportError as error:
        raise ValueError(str(error)) from error


def _bounded_text(value, maximum):
    return str(value or "").replace("\x00", "")[:maximum]


def _normalise_line(raw, root_fen, fallback_rank):
    if not isinstance(raw, dict):
        return None
    raw_pv = raw.get("pv") or []
    moves = raw_pv.split() if isinstance(raw_pv, str) else list(raw_pv)
    board = san.Board(root_fen)
    legal_pv = []
    for move in moves[:MAX_PV_PLIES]:
        move = str(move).lower()
        if not UCI_RE.fullmatch(move):
            break
        try:
            board = board.apply_uci(move)
        except ValueError:
            break
        legal_pv.append(move)
    cp = raw.get("cp")
    mate = raw.get("mate")
    try:
        cp = None if cp is None else max(-1_000_000, min(1_000_000, int(cp)))
        mate = None if mate is None else max(-999, min(999, int(mate)))
        rank = max(1, min(MAX_LINES, int(raw.get("rank", fallback_rank))))
        depth = max(0, min(1000, int(raw.get("depth", 0))))
    except (TypeError, ValueError):
        return None
    if cp is None and mate is None:
        return None
    bound = str(raw.get("bound", "exact")).lower()
    if bound not in {"exact", "lowerbound", "upperbound"}:
        bound = "exact"
    return {
        "rank": rank,
        "depth": depth,
        "cp": cp,
        "mate": mate,
        "bound": bound,
        "pv": legal_pv,
    }


def normalise_analysis(raw, fen):
    if not isinstance(raw, dict):
        return {}
    lines = []
    for index, line in enumerate(raw.get("lines") or [], start=1):
        clean = _normalise_line(line, fen, index)
        if clean is not None:
            lines.append(clean)
        if len(lines) >= MAX_LINES:
            break
    if not lines and isinstance(raw.get("best"), dict):
        best = dict(raw["best"])
        best.setdefault("depth", raw.get("depth", 0))
        clean = _normalise_line(best, fen, 1)
        if clean is not None:
            lines.append(clean)
    if not lines:
        return {}
    try:
        captured_at = max(0, int(raw.get("captured_at", time.time())))
    except (TypeError, ValueError):
        captured_at = int(time.time())
    return {
        "depth": max(line["depth"] for line in lines),
        "final": bool(raw.get("final", False)),
        "captured_at": captured_at,
        "lines": lines,
    }


def new_study(title, root_fen, metadata=None):
    root_fen = canonical_fen(root_fen)
    return {
        "id": "",
        "title": _bounded_text(title, MAX_TITLE) or "Untitled study",
        "root_fen": root_fen,
        "root": "0",
        "selected": "0",
        "metadata": {
            _bounded_text(key, 64): _bounded_text(value, 1024)
            for key, value in list((metadata or {}).items())[:32]
            if _bounded_text(key, 64)
        },
        "nodes": {
            "0": {
                "parent": None,
                "children": [],
                "move": "",
                "fen": root_fen,
                "name": "",
                "comment": "",
                "collapsed": False,
                "analysis": {},
            }
        },
    }


def normalise_study(raw):
    """Return a bounded, legally replayed copy or raise ``ValueError``.

    FENs stored on child nodes are verified against their parent and move.
    This makes a corrupt JSON file unable to smuggle a mismatched board into
    the explorer or its exported PGN.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("nodes"), dict):
        raise ValueError("Invalid saved study")
    if len(raw["nodes"]) < 1 or len(raw["nodes"]) > MAX_STUDY_NODES:
        raise ValueError(f"A study must contain 1 to {MAX_STUDY_NODES} nodes")
    root_fen = canonical_fen(raw.get("root_fen", ""))
    root_id = str(raw.get("root", "0"))
    if not root_id.isdigit() or root_id not in raw["nodes"]:
        raise ValueError("Study root is missing")

    clean = new_study(raw.get("title"), root_fen, raw.get("metadata"))
    identifier = str(raw.get("id", ""))
    clean["id"] = identifier if IDENTIFIER_RE.fullmatch(identifier) else ""
    clean["root"] = root_id
    clean["nodes"] = {}
    visiting = set()
    visited = set()

    def visit(node_id, parent_id, board):
        if node_id in visiting:
            raise ValueError("Study contains a variation cycle")
        if node_id in visited:
            raise ValueError("A study node has more than one parent")
        if not node_id.isdigit():
            raise ValueError("Study node identifiers must be numeric")
        raw_node = raw["nodes"].get(node_id)
        if not isinstance(raw_node, dict):
            raise ValueError("Study node is missing")
        actual_parent = raw_node.get("parent")
        actual_parent = None if actual_parent is None else str(actual_parent)
        if actual_parent != parent_id:
            raise ValueError("Study parent link is inconsistent")
        move = str(raw_node.get("move", "")).lower()
        expected = board
        if parent_id is None:
            if move:
                raise ValueError("Study root cannot contain a move")
        else:
            if not UCI_RE.fullmatch(move):
                raise ValueError("Study contains an invalid move")
            try:
                expected = board.apply_uci(move)
            except ValueError as error:
                raise ValueError("Study contains an illegal move") from error
        expected_fen = expected.fen()
        stored_fen = canonical_fen(raw_node.get("fen", expected_fen))
        if stored_fen != expected_fen:
            raise ValueError("Study node FEN does not match its variation")
        children = raw_node.get("children") or []
        if not isinstance(children, list) or len(children) > MAX_STUDY_NODES:
            raise ValueError("Invalid study child list")
        children = [str(child) for child in children]
        if len(set(children)) != len(children):
            raise ValueError("Study child list contains duplicates")
        clean["nodes"][node_id] = {
            "parent": parent_id,
            "children": children,
            "move": move,
            "fen": expected_fen,
            "name": _bounded_text(raw_node.get("name"), MAX_NAME),
            "comment": _bounded_text(raw_node.get("comment"), MAX_COMMENT),
            "collapsed": bool(raw_node.get("collapsed", False)),
            "analysis": normalise_analysis(raw_node.get("analysis"), expected_fen),
        }
        visiting.add(node_id)
        for child_id in children:
            visit(child_id, node_id, expected)
        visiting.remove(node_id)
        visited.add(node_id)

    visit(root_id, None, san.Board(root_fen))
    if len(visited) != len(raw["nodes"]):
        raise ValueError("Study contains unreachable nodes")
    selected = str(raw.get("selected", root_id))
    clean["selected"] = selected if selected in clean["nodes"] else root_id
    return clean


def from_explore_tree(title, nodes, root_id, selected_id, metadata=None):
    if root_id not in nodes:
        raise ValueError("Analysis Lab has no complete root to save")
    if nodes[root_id].get("parent") is not None:
        raise ValueError("Analysis Lab root has a parent")
    mapping = {}
    ordered = []

    def collect(old_id):
        if old_id in mapping:
            return
        node = nodes.get(old_id)
        if not isinstance(node, dict):
            raise ValueError("Analysis Lab tree is incomplete")
        mapping[old_id] = str(len(mapping))
        ordered.append(old_id)
        for child in node.get("children") or []:
            collect(child)

    collect(root_id)
    if len(mapping) != len(nodes):
        raise ValueError("Analysis Lab tree contains unreachable positions")
    root_fen = canonical_fen(nodes[root_id].get("fen", ""))
    result = new_study(title, root_fen, metadata)
    result["nodes"] = {}
    result["root"] = mapping[root_id]
    for old_id in ordered:
        raw_node = nodes[old_id]
        node_id = mapping[old_id]
        parent = raw_node.get("parent")
        result["nodes"][node_id] = {
            "parent": mapping.get(parent),
            "children": [mapping[child] for child in raw_node.get("children") or []],
            "move": str(raw_node.get("last", "")),
            "fen": str(raw_node.get("fen", "")),
            "name": str(raw_node.get("name", "")),
            "comment": str(raw_node.get("comment", "")),
            "collapsed": bool(raw_node.get("collapsed", False)),
            "analysis": copy.deepcopy(raw_node.get("analysis") or {}),
        }
    result["selected"] = mapping.get(selected_id, result["root"])
    return normalise_study(result)


def add_move(raw, parent_id, move):
    result = normalise_study(raw)
    parent_id = str(parent_id)
    if parent_id not in result["nodes"]:
        raise ValueError("Unknown study position")
    move = str(move).lower()
    board = san.Board(result["nodes"][parent_id]["fen"])
    try:
        after = board.apply_uci(move)
    except ValueError as error:
        raise ValueError("Illegal study move") from error
    for child_id in result["nodes"][parent_id]["children"]:
        if result["nodes"][child_id]["move"] == move:
            result["selected"] = child_id
            return result, child_id, True
    if len(result["nodes"]) >= MAX_STUDY_NODES:
        raise ValueError(f"Study is limited to {MAX_STUDY_NODES} positions")
    node_id = str(max((int(key) for key in result["nodes"]), default=-1) + 1)
    result["nodes"][node_id] = {
        "parent": parent_id,
        "children": [],
        "move": move,
        "fen": after.fen(),
        "name": "",
        "comment": "",
        "collapsed": False,
        "analysis": {},
    }
    result["nodes"][parent_id]["children"].append(node_id)
    result["selected"] = node_id
    return result, node_id, False


def path_to_node(raw, node_id):
    study = normalise_study(raw)
    node_id = str(node_id)
    if node_id not in study["nodes"]:
        raise ValueError("Unknown study position")
    output = []
    while node_id != study["root"]:
        node = study["nodes"][node_id]
        output.append(node["move"])
        node_id = node["parent"]
    output.reverse()
    return output


def _header_value(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _score_text(line):
    if line.get("mate") is not None:
        return f"#{int(line['mate'])}"
    cp = int(line.get("cp") or 0)
    prefix = {"lowerbound": "≥", "upperbound": "≤"}.get(line.get("bound"), "")
    return f"{prefix}{cp / 100:+.2f}"


def _node_comment(node):
    parts = []
    if node.get("name"):
        parts.append("Variation: " + node["name"])
    if node.get("comment"):
        parts.append(node["comment"])
    analysis = node.get("analysis") or {}
    lines = analysis.get("lines") or []
    if lines:
        line = lines[0]
        if line.get("bound", "exact") == "exact":
            value = f"#{int(line['mate'])}" if line.get("mate") is not None else f"{int(line.get('cp') or 0) / 100:.2f}"
            parts.append(f"[%eval {value}]")
        else:
            parts.append("eval " + _score_text(line))
        parts.append(f"[%depth {int(line.get('depth', analysis.get('depth', 0)))}]")
    return "; ".join(parts).replace("{", "(").replace("}", ")").replace("\n", " ")


def annotated_pgn(raw):
    study = normalise_study(raw)
    nodes = study["nodes"]

    def move_token(board, node):
        prefix = f"{board.fullmove_number}." if board.white_to_move else f"{board.fullmove_number}..."
        notation = board.san(node["move"])
        comment = _node_comment(node)
        tokens = [prefix, notation]
        if comment:
            tokens.append("{" + comment + "}")
        return tokens, board.apply_uci(node["move"])

    def render_choice(node_id, board):
        tokens, after = move_token(board, nodes[node_id])
        tokens.extend(render_position(node_id, after))
        return tokens

    def render_position(parent_id, board):
        children = nodes[parent_id]["children"]
        if not children:
            return []
        main = children[0]
        tokens, after = move_token(board, nodes[main])
        for alternative in children[1:]:
            tokens.append("(")
            tokens.extend(render_choice(alternative, board))
            tokens.append(")")
        tokens.extend(render_position(main, after))
        return tokens

    metadata = study.get("metadata") or {}
    result = metadata.get("Result", "*")
    if result not in {"1-0", "0-1", "1/2-1/2", "*"}:
        result = "*"
    headers = [
        f'[Event "{_header_value(study["title"])}"]',
        f'[Site "{_header_value(metadata.get("Site", "Local"))}"]',
        f'[Result "{result}"]',
    ]
    if study["root_fen"] != STANDARD_FEN:
        headers.extend(("[SetUp \"1\"]", f'[FEN "{study["root_fen"]}"]'))
    tokens = render_position(study["root"], san.Board(study["root_fen"]))
    return "\n".join(headers) + "\n\n" + " ".join(tokens + [result]) + "\n"
