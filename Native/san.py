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

    # -- helpers ----------------------------------------------------------

    def copy(self):
        clone = Board.__new__(Board)
        clone.squares = list(self.squares)
        clone.white_to_move = self.white_to_move
        clone.castling = set(self.castling)
        clone.en_passant = self.en_passant
        return clone

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

        after.squares[origin] = "."
        after.squares[target] = piece

        if kind == "p" and target == self.en_passant:
            captured_row = target // 8 + (1 if white else -1)
            after.squares[captured_row * 8 + target % 8] = "."

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

        after.white_to_move = not self.white_to_move
        return after

    def legal_moves(self):
        legal = []

        for origin, target, promotion in self.pseudo_legal():
            after = self.apply(origin, target, promotion)

            if not after.in_check(self.white_to_move):
                legal.append((origin, target, promotion))

        return legal

    # -- naming -----------------------------------------------------------

    def san(self, uci):
        """Name a UCI move in this position, or return it unchanged if it is
        not legal here (a desynchronised FEN should not raise into the UI)."""
        if not uci or len(uci) < 4:
            return uci

        try:
            origin = square_from_name(uci[0:2])
            target = square_from_name(uci[2:4])
        except (ValueError, IndexError):
            return uci

        promotion = uci[4].lower() if len(uci) > 4 else ""
        legal = self.legal_moves()

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
            origin = square_from_name(uci[0:2])
            target = square_from_name(uci[2:4])
        except (ValueError, IndexError):
            break

        board = board.apply(origin, target, uci[4].lower() if len(uci) > 4 else "")

    return output
