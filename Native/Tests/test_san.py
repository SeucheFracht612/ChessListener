"""SAN conversion and move-generation checks.

SAN is only as trustworthy as the move generator underneath it -- an engine's
best move gets named wrong if legality or disambiguation is off -- so this runs
perft alongside the naming cases.

    python3 Tests/test_san.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import san

START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
ITAL  = "r1bqk2r/pppp1ppp/2n2n2/2b1p3/2B1P3/5N2/PPPP1PPP/RNBQ1RK1 b kq - 5 5"

cases = [
    (START, "e2e4", "e4"),
    (START, "g1f3", "Nf3"),
    (START, "b1c3", "Nc3"),
    (ITAL,  "c6d4", "Nd4"),
    (ITAL,  "e8g8", "O-O"),
    (ITAL,  "c5f2", "Bxf2+"),
    # two rooks on the first rank -> file disambiguation
    ("4k3/8/8/8/8/8/8/R6R w - - 0 1", "a1d1", "Rad1"),
    ("4k3/8/8/8/8/8/8/R6R w - - 0 1", "h1d1", "Rhd1"),
    # two rooks on the same file -> rank disambiguation
    ("4k3/8/8/8/8/R7/8/R7 w - - 0 1", "a1a2", "R1a2"),
    ("4k3/8/8/8/8/R7/8/R7 w - - 0 1", "a3a2", "R3a2"),
    # three queens, needs both file and rank
    ("4k3/8/8/8/Q2Q4/8/8/Q6K w - - 0 1", "a4d1", "Qa4d1"),
    # promotion, with and without capture
    ("8/P6k/8/8/8/8/8/K7 w - - 0 1", "a7a8q", "a8=Q"),
    ("1n5k/P7/8/8/8/8/8/K7 w - - 0 1", "a7b8n", "axb8=N"),
    # en passant
    ("7k/8/8/3pP3/8/8/8/K7 w - d6 0 1", "e5d6", "exd6"),
    # pawn capture
    ("rnbqkbnr/ppp1pppp/8/3p4/4P3/8/PPPP1PPP/RNBQKBNR w KQkq d6 0 2", "e4d5", "exd5"),
    # queenside castling both colours
    ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1c1", "O-O-O"),
    ("r3k2r/8/8/8/8/8/8/R3K2R b KQkq - 0 1", "e8c8", "O-O-O"),
    # mate
    ("6k1/5ppp/8/8/8/8/8/R6K w - - 0 1", "a1a8", "Ra8#"),
    # check but not mate
    ("8/8/8/3k4/8/3K1Q2/8/8 w - - 0 1", "f3f5", "Qf5+"),
    # pinned knight cannot move, so no disambiguation from it
    ("k7/8/8/b7/8/2N3N1/8/4K3 w - - 0 1", "g3e4", "Ne4"),
    # illegal input must pass through untouched, not raise
    (START, "e2e5", "e2e5"),
    (START, "zz99", "zz99"),
]

bad = 0
for fen, uci, want in cases:
    got = san.Board(fen).san(uci)
    flag = "ok " if got == want else "BAD"
    if got != want: bad += 1
    print(f"{flag} {uci:<6} -> {got:<8} (want {want})")

# perft-style sanity: legal move counts from known positions
counts = [
    (START, 20),
    ("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", 26),
]
for fen, want in counts:
    got = len(san.Board(fen).legal_moves())
    flag = "ok " if got == want else "BAD"
    if got != want: bad += 1
    print(f"{flag} legal moves {got} (want {want})  {fen[:30]}")

pv = san.line_to_san(ITAL, "c6d4 f3d4 c5d4 c2c3".split())
print("pv:", pv)
if pv != ["Nd4", "Nxd4", "Bxd4", "c3"]:
    bad += 1
    print("BAD pv")



def perft(board, depth):
    if depth == 0:
        return 1

    moves = board.legal_moves()

    if depth == 1:
        return len(moves)

    return sum(
        perft(board.apply(origin, target, promotion), depth - 1)
        for origin, target, promotion in moves
    )


# Standard perft positions. Kiwipete in particular exercises castling, pins and
# en passant all at once, which is where a hand-written generator usually fails.
KIWIPETE = "r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1"

for fen, depth, want in (
    (START, 1, 20),
    (START, 2, 400),
    (START, 3, 8902),
    (KIWIPETE, 1, 48),
    (KIWIPETE, 2, 2039),
    ("8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1", 3, 2812),
    ("r3k2r/Pppp1ppp/1b3nbN/nP6/BBP1P3/q4N2/Pp1P2PP/R2Q1RK1 w kq - 0 1", 2, 264),
):
    got = perft(san.Board(fen), depth)
    flag = "ok " if got == want else "BAD"

    if got != want:
        bad += 1

    print(f"{flag} perft({depth}) = {got:<6} (want {want})  {fen[:34]}")

print("FAILURES:", bad)
sys.exit(1 if bad else 0)
