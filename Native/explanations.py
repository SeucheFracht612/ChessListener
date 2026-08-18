"""Deterministic, position-grounded text for the Analysis Lab.

Stockfish supplies variations, not reasons.  This module therefore describes
only facts that can be proven by replaying a principal variation: legal SAN,
checks, captures, special moves, forced replies and the material which is
actually present at the displayed horizon.  It deliberately does not infer
plans, pressure, king safety or other strategic intent.

The public function is dependency-free apart from :mod:`san` and never raises
for data received from the engine/UI boundary.  Engine scores are expected in
White's point of view, matching the native host protocol.
"""

import re

try:
    import san as san_rules
except ImportError:  # Allow ``from Native import explanations`` in tools.
    from . import san as san_rules


HEADING = "What the line shows"
DEFAULT_DISPLAY_PLIES = 6
MAX_DISPLAY_PLIES = 256

_UCI_MOVE = re.compile(r"^[a-h][1-8][a-h][1-8][qrbn]?$", re.ASCII)
_PIECE_NAMES = {
    "p": "pawn",
    "n": "knight",
    "b": "bishop",
    "r": "rook",
    "q": "queen",
    "k": "king",
}
_INVENTORY_ORDER = ("k", "q", "r", "b", "n", "p")


def _empty_result(selected_rank=1):
    return {
        "heading": HEADING,
        "pv_san": [],
        "line_text": "",
        "score_text": "",
        "comparison_text": "",
        "status_text": "",
        "facts": [],
        "truncated": False,
        "has_more": False,
        "selected_rank": selected_rank,
        "move_san": "",
        "perspective": "white",
        "horizon_inventory": {"white": {}, "black": {}},
    }


def _strict_board(fen):
    """Return a san.Board only for a structurally credible chess FEN."""
    if not isinstance(fen, str):
        return None

    fields = fen.strip().split()
    if not 2 <= len(fields) <= 6 or fields[1] not in {"w", "b"}:
        return None

    rows = fields[0].split("/")
    if len(rows) != 8:
        return None

    squares = []
    for row in rows:
        width = 0
        for character in row:
            if character in "12345678":
                width += int(character)
                squares.extend("." * int(character))
            elif character in "PNBRQKpnbrqk":
                width += 1
                squares.append(character)
            else:
                return None
        if width != 8:
            return None

    if len(squares) != 64 or squares.count("K") != 1 or squares.count("k") != 1:
        return None

    castling = fields[2] if len(fields) > 2 else "-"
    if castling != "-":
        if any(character not in "KQkq" for character in castling):
            return None
        if len(castling) != len(set(castling)):
            return None

        # san.Board assumes a rook is present when a castling right exists.
        # Reject inconsistent rights rather than letting replay create a rook.
        required = {
            "K": ((60, "K"), (63, "R")),
            "Q": ((60, "K"), (56, "R")),
            "k": ((4, "k"), (7, "r")),
            "q": ((4, "k"), (0, "r")),
        }
        for right in castling:
            if any(squares[index] != piece for index, piece in required[right]):
                return None

    en_passant = fields[3] if len(fields) > 3 else "-"
    if en_passant != "-" and not (
        len(en_passant) == 2
        and en_passant[0] in "abcdefgh"
        and en_passant[1] in "36"
    ):
        return None

    if len(fields) > 4:
        try:
            if int(fields[4]) < 0:
                return None
        except (TypeError, ValueError):
            return None
    if len(fields) > 5:
        try:
            if int(fields[5]) < 1:
                return None
        except (TypeError, ValueError):
            return None

    try:
        return san_rules.Board(" ".join(fields))
    except (AttributeError, IndexError, TypeError, ValueError):
        return None


def _positive_rank(value, fallback):
    try:
        rank = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return rank if rank > 0 else fallback


def _normalise_lines(lines):
    if not isinstance(lines, (list, tuple)):
        return []

    normalised = []
    for index, raw in enumerate(lines):
        if not isinstance(raw, dict):
            continue

        item = dict(raw)
        item["_rank"] = _positive_rank(raw.get("rank"), index + 1)
        normalised.append(item)
    return normalised


def _choose_line(lines, selected_rank):
    for line in lines:
        if line["_rank"] == selected_rank:
            return line
    return None


def _line_moves(line):
    """Return (tokens, already_truncated) without inventing a continuation."""
    move = line.get("move")
    move = move.strip().lower() if isinstance(move, str) else ""

    pv = line.get("pv")
    if isinstance(pv, str):
        tokens = pv.split()
    elif isinstance(pv, (list, tuple)):
        tokens = list(pv)
    elif pv is None:
        tokens = []
    else:
        tokens = [pv]

    truncated = False
    if len(tokens) > MAX_DISPLAY_PLIES:
        tokens = tokens[:MAX_DISPLAY_PLIES]
        truncated = True

    # The native protocol says ``move`` is the first move of ``pv``.  A
    # disagreement means frames were mixed or input was malformed.  Showing
    # the independently named candidate is useful; attaching the other PV is
    # not safe.
    if move:
        if not tokens:
            tokens = [move]
        elif not isinstance(tokens[0], str) or tokens[0].strip().lower() != move:
            tokens = [move]
            truncated = True
    elif tokens and isinstance(tokens[0], str):
        move = tokens[0].strip().lower()

    return tokens, truncated


def _parse_uci(token):
    if not isinstance(token, str):
        return None

    move = token.strip().lower()
    if not _UCI_MOVE.fullmatch(move):
        return None

    try:
        origin = san_rules.square_from_name(move[0:2])
        target = san_rules.square_from_name(move[2:4])
    except (IndexError, TypeError, ValueError):
        return None

    return move, origin, target, move[4:5]


def _inventory(board):
    result = {
        "white": {name: 0 for name in _PIECE_NAMES.values()},
        "black": {name: 0 for name in _PIECE_NAMES.values()},
    }
    for piece in board.squares:
        if piece == ".":
            continue
        side = "white" if piece.isupper() else "black"
        name = _PIECE_NAMES.get(piece.lower())
        if name is not None:
            result[side][name] += 1
    return result


def _article_piece(piece):
    name = _PIECE_NAMES.get(piece.lower(), "piece")
    return ("an " if name[0] in "aeiou" else "a ") + name


def _replay(board, tokens, display_plies, pre_truncated=False):
    sans = []
    events = []
    truncated = bool(pre_truncated)
    previous = None

    try:
        wanted = int(display_plies)
    except (TypeError, ValueError, OverflowError):
        wanted = DEFAULT_DISPLAY_PLIES
    wanted = max(0, min(MAX_DISPLAY_PLIES, wanted))

    visible = tokens[:wanted]
    has_more = len(tokens) > wanted

    for index, token in enumerate(visible):
        parsed = _parse_uci(token)
        if parsed is None:
            truncated = True
            break

        move, origin, target, promotion = parsed
        try:
            legal = board.legal_moves()
        except (AttributeError, IndexError, TypeError, ValueError):
            truncated = True
            break

        if (origin, target, promotion) not in legal:
            truncated = True
            break

        moving = board.squares[origin]
        captured = board.squares[target]
        en_passant = (
            moving.lower() == "p"
            and board.en_passant == target
            and captured == "."
            and origin % 8 != target % 8
        )

        if en_passant:
            captured_index = target + (8 if moving.isupper() else -8)
            expected = "p" if moving.isupper() else "P"
            if not 0 <= captured_index < 64 or board.squares[captured_index] != expected:
                truncated = True
                break
            captured = expected

        # san.Board's compact move generator is intentionally permissive
        # about malformed positions.  Never replay a king capture.
        if captured.lower() == "k":
            truncated = True
            break

        notation = board.san(move)
        if not notation or notation == move:
            truncated = True
            break

        is_capture = captured != "."
        is_recapture = bool(
            is_capture
            and previous is not None
            and previous["capture"]
            and previous["target"] == target
        )
        castle = moving.lower() == "k" and abs(target - origin) == 2

        try:
            after = board.apply(origin, target, promotion)
        except (AttributeError, IndexError, TypeError, ValueError):
            truncated = True
            break

        # Applying a purported legal move must preserve both kings.
        if after.squares.count("K") != 1 or after.squares.count("k") != 1:
            truncated = True
            break

        events.append(
            {
                "index": index,
                "san": notation,
                "target": target,
                "square": san_rules.square_name(target),
                "capture": is_capture,
                "captured": captured,
                "recapture": is_recapture,
                "en_passant": en_passant,
                "castle": "kingside" if castle and target > origin else
                          "queenside" if castle else "",
                "promotion": promotion,
                "only_legal": len(legal) == 1,
                "check": notation.endswith("+"),
                "mate": notation.endswith("#"),
            }
        )
        sans.append(notation)
        previous = events[-1]
        board = after

    return {
        "board": board,
        "sans": sans,
        "events": events,
        "truncated": truncated,
        "has_more": has_more,
    }


def _normalise_level(level):
    value = str(level or "compact").strip().lower()
    if value in {"off", "none", "0"}:
        return "off"
    if value in {"detailed", "detail", "full"}:
        return "detailed"
    return "compact"


def _perspective(eval_pov, board):
    value = str(eval_pov or "white").strip().lower().replace("_", "-")
    if value in {"black", "b"}:
        return "black", -1
    if value in {"side", "side-to-move", "stm", "turn"}:
        return ("white", 1) if board.white_to_move else ("black", -1)
    return "white", 1


def _integer(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _score(line, sign):
    mate = _integer(line.get("mate"))
    if mate is not None:
        return "mate", mate * sign
    cp = _integer(line.get("cp"))
    if cp is not None:
        return "cp", cp * sign
    return None, None


def _format_score(score):
    kind, value = score
    if kind == "mate":
        if value == 0:
            return "#"
        return f"#{value}"
    if kind == "cp":
        return f"{value / 100:+.2f}"
    return ""


def _depth(line):
    value = _integer(line.get("depth"))
    return value if value is not None and value > 0 else None


def _bound_label(line):
    if line.get("lowerbound") is True:
        return "Lower bound"
    if line.get("upperbound") is True:
        return "Upper bound"

    value = line.get("bound")
    if value is None or value is False:
        return ""
    if value is True:
        return "Bound"

    text = str(value).strip().lower().replace("_", " ").replace("-", " ")
    if text in {"", "none", "exact", "unbounded", "false"}:
        return ""
    if text == "lowerbound":
        text = "lower bound"
    elif text == "upperbound":
        text = "upper bound"
    return text.capitalize() if text else "Bound"


def _final_state(line):
    value = line.get("final")
    return value if isinstance(value, bool) else None


def _status(line):
    parts = []
    bound = _bound_label(line)
    final = _final_state(line)
    depth = _depth(line)

    if bound:
        parts.append(bound)
    elif final is True:
        parts.append("Final")
    elif final is False:
        parts.append("Searching")

    if depth is not None:
        parts.append(f"depth {depth}")
    return " · ".join(parts)


def _first_san(board, line):
    tokens, _ = _line_moves(line)
    replay = _replay(board.copy(), tokens, 1)
    return replay["sans"][0] if replay["sans"] else ""


def _comparison(board, best, selected, sign, perspective_name):
    if best is selected or selected["_rank"] == best["_rank"]:
        return "Stockfish's first-ranked candidate."

    # A numeric comparison only has meaning when both scores describe the
    # same kind of value at the same completed search state and neither is a
    # bound.  In particular, centipawns must never be subtracted from mate.
    if _bound_label(best) or _bound_label(selected):
        return ""
    if _depth(best) is None or _depth(best) != _depth(selected):
        return ""
    if _final_state(best) != _final_state(selected):
        return ""

    best_score = _score(best, sign)
    selected_score = _score(selected, sign)
    if best_score[0] is None or best_score[0] != selected_score[0]:
        return ""

    best_name = _first_san(board, best)
    reference = "line 1" + (f" ({best_name})" if best_name else "")
    perspective = perspective_name.capitalize()

    if best_score[0] == "mate":
        return (
            f"{reference.capitalize()} is {_format_score(best_score)}; "
            f"the selected line is {_format_score(selected_score)} from "
            f"{perspective}'s perspective."
        )

    difference = selected_score[1] - best_score[1]
    if difference == 0:
        return (
            f"The selected line has the same displayed evaluation as "
            f"{reference} from {perspective}'s perspective."
        )

    direction = "higher" if difference > 0 else "lower"
    return (
        f"Compared with {reference}, the evaluation is "
        f"{abs(difference) / 100:.2f} pawns {direction} from "
        f"{perspective}'s perspective."
    )


def _plural(count, singular):
    return singular if count == 1 else singular + "s"


def _inventory_phrase(inventory):
    parts = []
    for symbol in _INVENTORY_ORDER:
        name = _PIECE_NAMES[symbol]
        count = inventory.get(name, 0)
        if count:
            parts.append(f"{count} {_plural(count, name)}")
    if not parts:
        return "no pieces"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _facts(events, root_inventory, horizon_inventory, level):
    if level == "off" or not events:
        return []

    mate_facts = []
    forced_facts = []
    special_facts = []
    check_facts = []
    capture_facts = []

    for event in events:
        notation = event["san"]
        if event["mate"]:
            mate_facts.append(f"{notation} ends the displayed line in checkmate.")
        elif event["check"]:
            check_facts.append(f"{notation} gives check.")

        if event["only_legal"]:
            if event["index"] == 0:
                forced_facts.append(
                    f"{notation} is the only legal move in the root position."
                )
            else:
                previous = events[event["index"] - 1]["san"]
                forced_facts.append(
                    f"After {previous}, {notation} is the only legal reply."
                )

        if event["promotion"]:
            special_facts.append(
                f"{notation} promotes the pawn to {_article_piece(event['promotion'])}."
            )
        elif event["castle"]:
            special_facts.append(f"{notation} castles {event['castle']}.")
        elif event["en_passant"]:
            special_facts.append(f"{notation} captures a pawn en passant.")
        elif event["recapture"]:
            captured = _article_piece(event["captured"])
            special_facts.append(
                f"{notation} immediately recaptures {captured} on {event['square']}."
            )
        elif event["capture"]:
            capture_facts.append(
                f"{notation} captures {_article_piece(event['captured'])}."
            )

    candidates = mate_facts + forced_facts + special_facts + check_facts + capture_facts

    if level == "detailed" and horizon_inventory != root_inventory:
        candidates.append(
            "At the displayed horizon, White has "
            + _inventory_phrase(horizon_inventory["white"])
            + "; Black has "
            + _inventory_phrase(horizon_inventory["black"])
            + "."
        )

    maximum = 3 if level == "detailed" else 2
    if not candidates:
        return ["No simple tactical feature was detected in the displayed continuation."]
    return candidates[:maximum]


def build_explanation(
    root_fen,
    lines,
    selected_rank=1,
    display_plies=DEFAULT_DISPLAY_PLIES,
    level="compact",
    eval_pov="white",
):
    """Build UI-ready, factual text for one engine candidate.

    ``lines`` contains ranked dictionaries with ``move``, ``pv``, ``cp`` or
    ``mate``, ``depth`` and optional ``final``/``bound`` fields.  ``pv`` may be
    a whitespace string (the native protocol form) or a sequence.  Scores are
    White-POV. ``eval_pov`` accepts ``white``, ``black`` or ``side`` (the side
    to move in ``root_fen``).

    Bad FENs, missing candidates and malformed PV tails degrade to empty or
    legally truncated output and never escape as exceptions.
    """
    rank = _positive_rank(selected_rank, 1)
    result = _empty_result(rank)

    try:
        board = _strict_board(root_fen)
        normalised = _normalise_lines(lines)
        selected = _choose_line(normalised, rank)
        if board is None or selected is None:
            return result

        best = _choose_line(normalised, 1) or normalised[0]
        perspective_name, sign = _perspective(eval_pov, board)
        detail_level = _normalise_level(level)
        tokens, pre_truncated = _line_moves(selected)
        root_inventory = _inventory(board)
        replay = _replay(
            board.copy(), tokens, display_plies, pre_truncated=pre_truncated
        )
        horizon_inventory = _inventory(replay["board"])

        result.update(
            {
                "pv_san": replay["sans"],
                "line_text": " ".join(replay["sans"]),
                "score_text": _format_score(_score(selected, sign)),
                "comparison_text": "" if detail_level == "off" else _comparison(
                    board, best, selected, sign, perspective_name
                ),
                "status_text": _status(selected),
                "facts": _facts(
                    replay["events"], root_inventory, horizon_inventory, detail_level
                ),
                "truncated": replay["truncated"],
                "has_more": replay["has_more"],
                "move_san": replay["sans"][0] if replay["sans"] else "",
                "perspective": perspective_name,
                "horizon_inventory": horizon_inventory,
            }
        )
        return result
    except Exception:
        # This is a presentation boundary fed by asynchronous engine data.
        # An explanation must never be able to take down the overlay.
        return result


__all__ = ["build_explanation"]
