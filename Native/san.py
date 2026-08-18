"""UCI to SAN conversion for the overlay.

The engines speak UCI ("c6d4"), which is the wrong notation for something you
glance at while watching a game -- "Nxd4+" is read directly, "c6d4" has to be
translated. Doing that properly needs legal move generation, so this module
carries a small one.

Squares are indexed a8..h1, matching the grid the overlay already builds from a
FEN: index 0 is a8, 7 is h8, 56 is a1, 63 is h1.

Deliberately separate from overlay.py so it can be tested on its own, and kept
dependency free. overlay.py treats a failed import as "fall back to UCI".
"""

KNIGHT_STEPS = ((-2, -1), (-2, 1), (-1, -2), (-1, 2), (1, -2), (1, 2), (2, -1), (2, 1))
KING_STEPS = ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
BISHOP_RAYS = ((-1, -1), (-1, 1), (1, -1), (1, 1))
ROOK_RAYS = ((-1, 0), (1, 0), (0, -1), (0, 1))

PIECE_LETTER = {"n": "N", "b": "B", "r": "R", "q": "Q", "k": "K"}


def square_name(index):
    return chr(ord("a") + index % 8) + str(8 - index // 8)


def square_from_name(name):
    return (8 - int(name[1])) * 8 + (ord(name[0]) - ord("a"))


class Board:
    """A position, just complete enough to generate legal moves and name them."""

    def __init__(self, fen):
        fields = fen.split()
        rows = fields[0].split("/")

        if len(rows) != 8:
            raise ValueError("bad FEN board field")

        squares = []

        for row in rows:
            for character in row:
                if character.isdigit():
                    squares.extend("." * int(character))
                else:
                    squares.append(character)

        if len(squares) != 64:
            raise ValueError("bad FEN board field")

        self.squares = squares
        self.white_to_move = (fields[1] if len(fields) > 1 else "w") == "w"

        rights = fields[2] if len(fields) > 2 else "-"
        self.castling = set() if rights == "-" else set(rights)

        target = fields[3] if len(fields) > 3 else "-"
        self.en_passant = None if target == "-" else square_from_name(target)

        try:
            self.halfmove_clock = int(fields[4]) if len(fields) > 4 else 0
            self.fullmove_number = int(fields[5]) if len(fields) > 5 else 1
        except ValueError as error:
            raise ValueError("bad FEN move counters") from error

        if self.halfmove_clock < 0 or self.fullmove_number < 1:
            raise ValueError("bad FEN move counters")

    # -- helpers ----------------------------------------------------------

    def copy(self):
        clone = Board.__new__(Board)
        clone.squares = list(self.squares)
        clone.white_to_move = self.white_to_move
        clone.castling = set(self.castling)
        clone.en_passant = self.en_passant
        clone.halfmove_clock = self.halfmove_clock
        clone.fullmove_number = self.fullmove_number
        return clone

    def fen(self):
        """Return a complete six-field FEN for this position.

        Analysis Lab positions are deliberately transported as ordinary FENs,
        so retaining the counters here avoids turning an exploratory move into
        a lossy board-only reconstruction.
        """
        rows = []

        for start in range(0, 64, 8):
            row = []
            empty = 0

            for piece in self.squares[start : start + 8]:
                if piece == ".":
                    empty += 1
                    continue

                if empty:
                    row.append(str(empty))
                    empty = 0
                row.append(piece)

            if empty:
                row.append(str(empty))

            rows.append("".join(row))

        board = "/".join(rows)
        side = "w" if self.white_to_move else "b"
        castling = "".join(
            right for right in "KQkq" if right in self.castling
        ) or "-"
        en_passant = (
            "-" if self.en_passant is None else square_name(self.en_passant)
        )
        return (
            f"{board} {side} {castling} {en_passant} "
            f"{self.halfmove_clock} {self.fullmove_number}"
        )

    @staticmethod
    def is_white(piece):
        return piece != "." and piece.isupper()

    def own(self, piece):
        return piece != "." and (piece.isupper() == self.white_to_move)

    def enemy(self, piece):
        return piece != "." and (piece.isupper() != self.white_to_move)

    def king_square(self, white):
        wanted = "K" if white else "k"

        for index, piece in enumerate(self.squares):
            if piece == wanted:
                return index

        return None

    def attacked(self, index, by_white):
        """Is `index` attacked by the given side?"""
        row, column = divmod(index, 8)

        # Pawns. White pawns move toward row 0, so they attack from below.
        pawn_row = row + 1 if by_white else row - 1
        pawn = "P" if by_white else "p"

        if 0 <= pawn_row < 8:
            for delta in (-1, 1):
                file_index = column + delta

                if 0 <= file_index < 8 and self.squares[pawn_row * 8 + file_index] == pawn:
                    return True

        for steps, letters in (
            (KNIGHT_STEPS, "N"),
            (KING_STEPS, "K"),
        ):
            wanted = letters if by_white else letters.lower()

            for row_step, column_step in steps:
                target_row, target_column = row + row_step, column + column_step

                if 0 <= target_row < 8 and 0 <= target_column < 8:
                    if self.squares[target_row * 8 + target_column] == wanted:
                        return True

        for rays, sliders in ((BISHOP_RAYS, "BQ"), (ROOK_RAYS, "RQ")):
            wanted = sliders if by_white else sliders.lower()

            for row_step, column_step in rays:
                target_row, target_column = row + row_step, column + column_step

                while 0 <= target_row < 8 and 0 <= target_column < 8:
                    piece = self.squares[target_row * 8 + target_column]

                    if piece != ".":
                        if piece in wanted:
                            return True
                        break

                    target_row += row_step
                    target_column += column_step

        return False

    def in_check(self, white):
        king = self.king_square(white)
        return king is not None and self.attacked(king, not white)

    # -- move generation --------------------------------------------------

    def pseudo_legal(self):
        """(origin, target, promotion) triples, ignoring king safety."""
        moves = []
        white = self.white_to_move

        for origin, piece in enumerate(self.squares):
            if not self.own(piece):
                continue

            row, column = divmod(origin, 8)
            kind = piece.lower()

            if kind == "p":
                self._pawn_moves(moves, origin, row, column, white)
            elif kind == "n":
                self._step_moves(moves, origin, row, column, KNIGHT_STEPS)
            elif kind == "k":
                self._step_moves(moves, origin, row, column, KING_STEPS)
                self._castling_moves(moves, origin, white)
            else:
                rays = (
                    BISHOP_RAYS
                    if kind == "b"
                    else ROOK_RAYS
                    if kind == "r"
                    else BISHOP_RAYS + ROOK_RAYS
                )
                self._ray_moves(moves, origin, row, column, rays)

        return moves

    def _pawn_moves(self, moves, origin, row, column, white):
        step = -1 if white else 1
        start_row = 6 if white else 1
        last_row = 0 if white else 7

        ahead_row = row + step

        if 0 <= ahead_row < 8:
            ahead = ahead_row * 8 + column

            if self.squares[ahead] == ".":
                self._push_pawn(moves, origin, ahead, ahead_row == last_row)

                double_row = row + step * 2

                if row == start_row:
                    double = double_row * 8 + column

                    if self.squares[double] == ".":
                        moves.append((origin, double, ""))

            for delta in (-1, 1):
                capture_column = column + delta

                if not 0 <= capture_column < 8:
                    continue

                target = ahead_row * 8 + capture_column

                if self.enemy(self.squares[target]) or target == self.en_passant:
                    self._push_pawn(moves, origin, target, ahead_row == last_row)

    @staticmethod
    def _push_pawn(moves, origin, target, promoting):
        if promoting:
            for piece in "qrbn":
                moves.append((origin, target, piece))
        else:
            moves.append((origin, target, ""))

    def _step_moves(self, moves, origin, row, column, steps):
        for row_step, column_step in steps:
            target_row, target_column = row + row_step, column + column_step

            if not (0 <= target_row < 8 and 0 <= target_column < 8):
                continue

            target = target_row * 8 + target_column

            if not self.own(self.squares[target]):
                moves.append((origin, target, ""))

    def _ray_moves(self, moves, origin, row, column, rays):
        for row_step, column_step in rays:
            target_row, target_column = row + row_step, column + column_step

            while 0 <= target_row < 8 and 0 <= target_column < 8:
                target = target_row * 8 + target_column
                piece = self.squares[target]

                if self.own(piece):
                    break

                moves.append((origin, target, ""))

                if piece != ".":
                    break

                target_row += row_step
                target_column += column_step

    def _castling_moves(self, moves, origin, white):
        home = 60 if white else 4

        if origin != home:
            return

        if self.in_check(white):
            return

        options = (
            (("K", 61, 62, (61, 62)), ("Q", 59, 58, (59, 58, 57)))
            if white
            else (("k", 5, 6, (5, 6)), ("q", 3, 2, (3, 2, 1)))
        )

        for right, transit, destination, empties in options:
            if right not in self.castling:
                continue

            if any(self.squares[index] != "." for index in empties):
                continue

            # The transit square must also be safe; the destination is checked
            # by the ordinary legality filter.
            if self.attacked(transit, not white):
                continue

            moves.append((origin, destination, ""))

    def apply(self, origin, target, promotion):
        after = self.copy()
        piece = after.squares[origin]
        kind = piece.lower()
        white = piece.isupper()
        capture = after.squares[target] != "."

        after.squares[origin] = "."
        after.squares[target] = piece

        if kind == "p" and target == self.en_passant:
            captured_row = target // 8 + (1 if white else -1)
            after.squares[captured_row * 8 + target % 8] = "."
            capture = True

        if promotion:
            after.squares[target] = promotion.upper() if white else promotion

        if kind == "k" and abs(target - origin) == 2:
            if target > origin:  # kingside
                after.squares[target + 1] = "."
                after.squares[target - 1] = "R" if white else "r"
            else:
                after.squares[target - 2] = "."
                after.squares[target + 1] = "R" if white else "r"

        for right, square in (("K", 63), ("Q", 56), ("k", 7), ("q", 0)):
            if after.squares[square] != ("R" if right.isupper() else "r"):
                after.castling.discard(right)

        if kind == "k":
            for right in ("KQ" if white else "kq"):
                after.castling.discard(right)

        after.en_passant = None

        if kind == "p" and abs(target - origin) == 16:
            after.en_passant = (origin + target) // 2

        after.halfmove_clock = (
            0 if kind == "p" or capture else self.halfmove_clock + 1
        )
        after.fullmove_number = self.fullmove_number + (
            0 if self.white_to_move else 1
        )
        after.white_to_move = not self.white_to_move
        return after

    def legal_uci_moves(self, origin=None):
        """Return legal moves in UCI notation, optionally from one square."""
        output = []

        for move_origin, target, promotion in self.legal_moves():
            if origin is not None and move_origin != origin:
                continue

            output.append(
                square_name(move_origin) + square_name(target) + promotion
            )

        return output

    def apply_uci(self, uci, require_legal=True):
        """Apply one UCI move and return the resulting board.

        ``require_legal`` exists for callers that already obtained a move from
        ``legal_moves``. Interactive UI paths should retain the default and
        fail closed before asking the native host to validate the same move.
        """
        if not isinstance(uci, str) or len(uci) not in {4, 5}:
            raise ValueError("bad UCI move")

        try:
            origin = square_from_name(uci[0:2])
            target = square_from_name(uci[2:4])
        except (ValueError, IndexError):
            raise ValueError("bad UCI move") from None

        promotion = uci[4].lower() if len(uci) == 5 else ""

        if promotion and promotion not in "qrbn":
            raise ValueError("bad UCI promotion")

        if require_legal and (origin, target, promotion) not in self.legal_moves():
            raise ValueError("illegal move")

        return self.apply(origin, target, promotion)

    def legal_moves(self):
        legal = []

        for origin, target, promotion in self.pseudo_legal():
            after = self.apply(origin, target, promotion)

            if not after.in_check(self.white_to_move):
                legal.append((origin, target, promotion))

        return legal

    # -- naming -----------------------------------------------------------

    def san(self, uci, legal=None):
        """Name a UCI move in this position, or return it unchanged if it is
        not legal here (a desynchronised FEN should not raise into the UI).

        Importers may pass one precomputed ``legal_moves()`` list while
        matching a SAN token against every candidate. Ordinary UI callers can
        omit it and retain the original one-shot behaviour.
        """
        if not uci or len(uci) < 4:
            return uci

        try:
            origin = square_from_name(uci[0:2])
            target = square_from_name(uci[2:4])
        except (ValueError, IndexError):
            return uci

        promotion = uci[4].lower() if len(uci) > 4 else ""
        legal = self.legal_moves() if legal is None else legal

        if (origin, target, promotion) not in legal:
            return uci

        piece = self.squares[origin]
        kind = piece.lower()
        captured = self.squares[target] != "."

        if kind == "k" and abs(target - origin) == 2:
            text = "O-O" if target > origin else "O-O-O"
        elif kind == "p":
            if captured or target == self.en_passant:
                text = f"{uci[0]}x{square_name(target)}"
            else:
                text = square_name(target)

            if promotion:
                text += "=" + promotion.upper()
        else:
            # Disambiguate against other pieces of the same type that could
            # legally reach the same square.
            rivals = [
                other_origin
                for other_origin, other_target, other_promotion in legal
                if other_target == target
                and other_origin != origin
                and self.squares[other_origin] == piece
            ]

            hint = ""

            if rivals:
                same_file = any(other % 8 == origin % 8 for other in rivals)
                same_rank = any(other // 8 == origin // 8 for other in rivals)

                if not same_file:
                    hint = uci[0]
                elif not same_rank:
                    hint = uci[1]
                else:
                    hint = uci[0:2]

            text = (
                PIECE_LETTER[kind]
                + hint
                + ("x" if captured else "")
                + square_name(target)
            )

        after = self.apply(origin, target, promotion)

        if after.in_check(after.white_to_move):
            text += "#" if not after.legal_moves() else "+"

        return text


def line_to_san(fen, moves, limit=6):
    """Render a UCI principal variation as SAN, stopping at the first move that
    does not fit the position (a truncated or stale pv should degrade, not
    raise)."""
    try:
        board = Board(fen)
    except ValueError:
        return []

    output = []

    for uci in moves[:limit]:
        text = board.san(uci)

        if text == uci and len(uci) >= 4:
            # san() returns the input unchanged when the move is not legal.
            break

        output.append(text)

        try:
            board = board.apply_uci(uci)
        except ValueError:
            break

    return output


def numbered_line_to_san(fen, moves, limit=6):
    """Render a UCI line with move numbers, suitable for a compact PV row.

    A black-to-move root starts with the conventional ``18...`` marker. The
    function shares ``line_to_san``'s fail-closed behaviour: a malformed or
    stale continuation is shown only up to its first invalid move.
    """
    try:
        board = Board(fen)
    except ValueError:
        return ""

    if limit is None:
        selected = moves
    else:
        selected = moves[: max(0, int(limit))]

    output = []

    for index, uci in enumerate(selected):
        text = board.san(uci)

        if text == uci and len(uci) >= 4:
            break

        if board.white_to_move:
            output.append(f"{board.fullmove_number}. {text}")
        elif index == 0:
            output.append(f"{board.fullmove_number}... {text}")
        else:
            output.append(text)

        try:
            board = board.apply_uci(uci)
        except ValueError:
            break

    return " ".join(output)
