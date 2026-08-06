#include <ctype.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "analysis.h"

#define MAX_MESSAGE_SIZE (1024U * 1024U)
#define LOG_PATH "/tmp/chess-listener.log"
#define BOARD_SQUARE_COUNT 64U
#define MAX_LEGAL_MOVES 256U
#define MAX_FEN_LENGTH 128U
#define MAX_RESPONSE_LENGTH 256U

#define CASTLE_WHITE_KINGSIDE  (1U << 0)
#define CASTLE_WHITE_QUEENSIDE (1U << 1)
#define CASTLE_BLACK_KINGSIDE  (1U << 2)
#define CASTLE_BLACK_QUEENSIDE (1U << 3)

#define MOVE_FLAG_CAPTURE      (1U << 0)
#define MOVE_FLAG_DOUBLE_PAWN  (1U << 1)
#define MOVE_FLAG_EN_PASSANT   (1U << 2)
#define MOVE_FLAG_CASTLING     (1U << 3)
#define MOVE_FLAG_PROMOTION    (1U << 4)

typedef enum {
    COLOR_WHITE = 0,
    COLOR_BLACK = 1
} Color;

typedef struct {
    char board[BOARD_SQUARE_COUNT];
    Color sideToMove;
    uint8_t castlingRights;
    int enPassantSquare;
    unsigned int halfmoveClock;
    unsigned int fullmoveNumber;
} Position;

typedef struct {
    int from;
    int to;
    char promotion;
    unsigned int flags;
} Move;

typedef struct {
    Move moves[MAX_LEGAL_MOVES];
    size_t count;
} MoveList;

static const char INITIAL_BOARD[BOARD_SQUARE_COUNT + 1U] =
"rnbqkbnr"
"pppppppp"
"........"
"........"
"........"
"........"
"PPPPPPPP"
"RNBQKBNR";

static int ReadExact(void *destination, size_t byteCount)
{
    if (destination == NULL && byteCount != 0U) {
        return 0;
    }

    unsigned char *writePosition = destination;
    size_t remainingBytes = byteCount;

    while (remainingBytes > 0U) {
        size_t bytesRead = fread(writePosition, 1U, remainingBytes, stdin);

        if (bytesRead == 0U) {
            return 0;
        }

        writePosition += bytesRead;
        remainingBytes -= bytesRead;
    }

    return 1;
}

static int ReadNativeMessage(char **messageOut)
{
    if (messageOut == NULL) {
        return 0;
    }

    *messageOut = NULL;

    uint32_t messageLength = 0U;

    if (!ReadExact(&messageLength, sizeof(messageLength))) {
        return 0;
    }

    if (messageLength == 0U || messageLength > MAX_MESSAGE_SIZE) {
        return 0;
    }

    char *message = malloc((size_t)messageLength + 1U);

    if (message == NULL) {
        return 0;
    }

    if (!ReadExact(message, (size_t)messageLength)) {
        free(message);
        return 0;
    }

    message[messageLength] = '\0';
    *messageOut = message;

    return 1;
}

static int WriteNativeMessage(const char *json)
{
    if (json == NULL) {
        return 0;
    }

    size_t stringLength = strlen(json);

    if (stringLength > UINT32_MAX) {
        return 0;
    }

    uint32_t messageLength = (uint32_t)stringLength;

    if (fwrite(&messageLength, sizeof(messageLength), 1U, stdout) != 1U) {
        return 0;
    }

    if (fwrite(json, 1U, messageLength, stdout) != messageLength) {
        return 0;
    }

    return fflush(stdout) == 0;
}

/*
 * Extracts only simple, unescaped ASCII string fields created by ChessListener.
 * It is intentionally not a general-purpose JSON parser.
 */
static int ExtractJsonStringField(
    const char *json,
    const char *fieldName,
    char *output,
    size_t outputSize)
{
    if (
        json == NULL ||
        fieldName == NULL ||
        output == NULL ||
        outputSize == 0U
    ) {
        return 0;
    }

    char key[64];
    int keyLength = snprintf(key, sizeof(key), "\"%s\"", fieldName);

    if (keyLength < 0 || (size_t)keyLength >= sizeof(key)) {
        return 0;
    }

    const char *cursor = strstr(json, key);

    if (cursor == NULL) {
        return 0;
    }

    cursor += (size_t)keyLength;

    while (isspace((unsigned char)*cursor) != 0) {
        cursor += 1;
    }

    if (*cursor != ':') {
        return 0;
    }

    cursor += 1;

    while (isspace((unsigned char)*cursor) != 0) {
        cursor += 1;
    }

    if (*cursor != '"') {
        return 0;
    }

    cursor += 1;

    size_t outputIndex = 0U;

    while (*cursor != '\0' && *cursor != '"') {
        if (*cursor == '\\' || outputIndex + 1U >= outputSize) {
            return 0;
        }

        output[outputIndex] = *cursor;
        outputIndex += 1U;
        cursor += 1;
    }

    if (*cursor != '"') {
        return 0;
    }

    output[outputIndex] = '\0';
    return 1;
}

/*
 * Same restricted parser, for the literals true / false.
 */
static int ExtractJsonBoolField(
    const char *json,
    const char *fieldName,
    int *valueOut)
{
    char key[64];
    int keyLength;
    const char *cursor;

    if (json == NULL || fieldName == NULL || valueOut == NULL) {
        return 0;
    }

    keyLength = snprintf(key, sizeof(key), "\"%s\"", fieldName);

    if (keyLength < 0 || (size_t)keyLength >= sizeof(key)) {
        return 0;
    }

    cursor = strstr(json, key);

    if (cursor == NULL) {
        return 0;
    }

    cursor += (size_t)keyLength;

    while (isspace((unsigned char)*cursor) != 0) {
        cursor += 1;
    }

    if (*cursor != ':') {
        return 0;
    }

    cursor += 1;

    while (isspace((unsigned char)*cursor) != 0) {
        cursor += 1;
    }

    if (strncmp(cursor, "true", 4U) == 0) {
        *valueOut = 1;
        return 1;
    }

    if (strncmp(cursor, "false", 5U) == 0) {
        *valueOut = 0;
        return 1;
    }

    return 0;
}

static int IsWhitePiece(char piece)
{
    return piece >= 'A' && piece <= 'Z';
}

static int IsBlackPiece(char piece)
{
    return piece >= 'a' && piece <= 'z';
}

static int IsPieceOfColor(char piece, Color color)
{
    return color == COLOR_WHITE ? IsWhitePiece(piece) : IsBlackPiece(piece);
}

static int IsOpponentPiece(char piece, Color color)
{
    return color == COLOR_WHITE ? IsBlackPiece(piece) : IsWhitePiece(piece);
}

static Color OppositeColor(Color color)
{
    return color == COLOR_WHITE ? COLOR_BLACK : COLOR_WHITE;
}

static int IsBoardCharacter(char character)
{
    return (
        character == '.' ||
        character == 'P' || character == 'N' || character == 'B' ||
        character == 'R' || character == 'Q' || character == 'K' ||
        character == 'p' || character == 'n' || character == 'b' ||
        character == 'r' || character == 'q' || character == 'k'
    );
}

static int ValidateBoardString(const char *board, size_t *pieceCountOut)
{
    if (board == NULL || strlen(board) != BOARD_SQUARE_COUNT) {
        return 0;
    }

    size_t pieceCount = 0U;
    size_t whitePieceCount = 0U;
    size_t blackPieceCount = 0U;
    size_t whitePawnCount = 0U;
    size_t blackPawnCount = 0U;
    size_t whiteKingCount = 0U;
    size_t blackKingCount = 0U;

    for (size_t index = 0U; index < BOARD_SQUARE_COUNT; index += 1U) {
        char character = board[index];

        if (!IsBoardCharacter(character)) {
            return 0;
        }

        if (character != '.') {
            pieceCount += 1U;

            if (IsWhitePiece(character)) {
                whitePieceCount += 1U;
            } else {
                blackPieceCount += 1U;
            }
        }

        if (character == 'P') {
            whitePawnCount += 1U;
        } else if (character == 'p') {
            blackPawnCount += 1U;
        }

        if (
            (index < 8U || index >= 56U) &&
            (character == 'P' || character == 'p')
        ) {
            return 0;
        }

        if (character == 'K') {
            whiteKingCount += 1U;
        } else if (character == 'k') {
            blackKingCount += 1U;
        }
    }

    if (whiteKingCount != 1U || blackKingCount != 1U) {
        return 0;
    }

    if (
        whitePieceCount > 16U ||
        blackPieceCount > 16U ||
        whitePawnCount > 8U ||
        blackPawnCount > 8U
    ) {
        return 0;
    }

    if (pieceCountOut != NULL) {
        *pieceCountOut = pieceCount;
    }

    return 1;
}

static int RowOf(int square)
{
    return square / 8;
}

static int FileOf(int square)
{
    return square % 8;
}

static int IsInsideBoard(int row, int file)
{
    return row >= 0 && row < 8 && file >= 0 && file < 8;
}

static int SquareFromRowFile(int row, int file)
{
    return row * 8 + file;
}

static void SquareToName(int square, char output[3])
{
    output[0] = (char)('a' + FileOf(square));
    output[1] = (char)('8' - RowOf(square));
    output[2] = '\0';
}

static void InitializePosition(Position *position)
{
    memcpy(position->board, INITIAL_BOARD, BOARD_SQUARE_COUNT);
    position->sideToMove = COLOR_WHITE;
    position->castlingRights =
    CASTLE_WHITE_KINGSIDE |
    CASTLE_WHITE_QUEENSIDE |
    CASTLE_BLACK_KINGSIDE |
    CASTLE_BLACK_QUEENSIDE;
    position->enPassantSquare = -1;
    position->halfmoveClock = 0U;
    position->fullmoveNumber = 1U;
}

static int FindKingSquare(const Position *position, Color color)
{
    char king = color == COLOR_WHITE ? 'K' : 'k';

    for (int square = 0; square < 64; square += 1) {
        if (position->board[square] == king) {
            return square;
        }
    }

    return -1;
}

static int IsSquareAttacked(
    const Position *position,
    int targetSquare,
    Color attackingColor)
{
    int targetRow = RowOf(targetSquare);
    int targetFile = FileOf(targetSquare);

    /* Pawn attacks. */
    int pawnSourceRow =
    attackingColor == COLOR_WHITE ? targetRow + 1 : targetRow - 1;
    char pawn = attackingColor == COLOR_WHITE ? 'P' : 'p';

    for (int fileOffset = -1; fileOffset <= 1; fileOffset += 2) {
        int sourceFile = targetFile + fileOffset;

        if (IsInsideBoard(pawnSourceRow, sourceFile)) {
            int sourceSquare = SquareFromRowFile(pawnSourceRow, sourceFile);

            if (position->board[sourceSquare] == pawn) {
                return 1;
            }
        }
    }

    /* Knight attacks. */
    static const int knightOffsets[8][2] = {
        {-2, -1}, {-2, 1}, {-1, -2}, {-1, 2},
        {1, -2}, {1, 2}, {2, -1}, {2, 1}
    };
    char knight = attackingColor == COLOR_WHITE ? 'N' : 'n';

    for (size_t index = 0U; index < 8U; index += 1U) {
        int sourceRow = targetRow + knightOffsets[index][0];
        int sourceFile = targetFile + knightOffsets[index][1];

        if (IsInsideBoard(sourceRow, sourceFile)) {
            int sourceSquare = SquareFromRowFile(sourceRow, sourceFile);

            if (position->board[sourceSquare] == knight) {
                return 1;
            }
        }
    }

    /* King attacks. */
    char king = attackingColor == COLOR_WHITE ? 'K' : 'k';

    for (int rowOffset = -1; rowOffset <= 1; rowOffset += 1) {
        for (int fileOffset = -1; fileOffset <= 1; fileOffset += 1) {
            if (rowOffset == 0 && fileOffset == 0) {
                continue;
            }

            int sourceRow = targetRow + rowOffset;
            int sourceFile = targetFile + fileOffset;

            if (IsInsideBoard(sourceRow, sourceFile)) {
                int sourceSquare = SquareFromRowFile(sourceRow, sourceFile);

                if (position->board[sourceSquare] == king) {
                    return 1;
                }
            }
        }
    }

    /* Sliding attacks. */
    static const int rookDirections[4][2] = {
        {-1, 0}, {1, 0}, {0, -1}, {0, 1}
    };
    static const int bishopDirections[4][2] = {
        {-1, -1}, {-1, 1}, {1, -1}, {1, 1}
    };
    char rook = attackingColor == COLOR_WHITE ? 'R' : 'r';
    char bishop = attackingColor == COLOR_WHITE ? 'B' : 'b';
    char queen = attackingColor == COLOR_WHITE ? 'Q' : 'q';

    for (size_t direction = 0U; direction < 4U; direction += 1U) {
        int row = targetRow + rookDirections[direction][0];
        int file = targetFile + rookDirections[direction][1];

        while (IsInsideBoard(row, file)) {
            char piece = position->board[SquareFromRowFile(row, file)];

            if (piece != '.') {
                if (piece == rook || piece == queen) {
                    return 1;
                }
                break;
            }

            row += rookDirections[direction][0];
            file += rookDirections[direction][1];
        }
    }

    for (size_t direction = 0U; direction < 4U; direction += 1U) {
        int row = targetRow + bishopDirections[direction][0];
        int file = targetFile + bishopDirections[direction][1];

        while (IsInsideBoard(row, file)) {
            char piece = position->board[SquareFromRowFile(row, file)];

            if (piece != '.') {
                if (piece == bishop || piece == queen) {
                    return 1;
                }
                break;
            }

            row += bishopDirections[direction][0];
            file += bishopDirections[direction][1];
        }
    }

    return 0;
}

static void AddMove(
    MoveList *moveList,
    int from,
    int to,
    unsigned int flags,
    char promotion)
{
    if (moveList->count >= MAX_LEGAL_MOVES) {
        return;
    }

    Move move;
    move.from = from;
    move.to = to;
    move.flags = flags;
    move.promotion = promotion;

    moveList->moves[moveList->count] = move;
    moveList->count += 1U;
}

static void AddPromotionMoves(
    MoveList *moveList,
    int from,
    int to,
    unsigned int flags,
    Color color)
{
    static const char whitePromotions[4] = {'Q', 'R', 'B', 'N'};
    static const char blackPromotions[4] = {'q', 'r', 'b', 'n'};
    const char *promotions =
    color == COLOR_WHITE ? whitePromotions : blackPromotions;

    for (size_t index = 0U; index < 4U; index += 1U) {
        AddMove(
            moveList,
            from,
            to,
            flags | MOVE_FLAG_PROMOTION,
            promotions[index]
        );
    }
}

static void GeneratePawnMoves(
    const Position *position,
    int from,
    MoveList *moveList)
{
    Color color = position->sideToMove;
    int row = RowOf(from);
    int file = FileOf(from);
    int direction = color == COLOR_WHITE ? -1 : 1;
    int startRow = color == COLOR_WHITE ? 6 : 1;
    int promotionRow = color == COLOR_WHITE ? 0 : 7;

    int oneStepRow = row + direction;

    if (IsInsideBoard(oneStepRow, file)) {
        int oneStep = SquareFromRowFile(oneStepRow, file);

        if (position->board[oneStep] == '.') {
            if (oneStepRow == promotionRow) {
                AddPromotionMoves(moveList, from, oneStep, 0U, color);
            } else {
                AddMove(moveList, from, oneStep, 0U, '\0');

                if (row == startRow) {
                    int twoStepRow = row + 2 * direction;
                    int twoStep = SquareFromRowFile(twoStepRow, file);

                    if (position->board[twoStep] == '.') {
                        AddMove(
                            moveList,
                            from,
                            twoStep,
                            MOVE_FLAG_DOUBLE_PAWN,
                            '\0'
                        );
                    }
                }
            }
        }
    }

    for (int fileOffset = -1; fileOffset <= 1; fileOffset += 2) {
        int targetRow = row + direction;
        int targetFile = file + fileOffset;

        if (!IsInsideBoard(targetRow, targetFile)) {
            continue;
        }

        int target = SquareFromRowFile(targetRow, targetFile);
        char targetPiece = position->board[target];

        if (IsOpponentPiece(targetPiece, color)) {
            if (targetRow == promotionRow) {
                AddPromotionMoves(
                    moveList,
                    from,
                    target,
                    MOVE_FLAG_CAPTURE,
                    color
                );
            } else {
                AddMove(
                    moveList,
                    from,
                    target,
                    MOVE_FLAG_CAPTURE,
                    '\0'
                );
            }
        } else if (target == position->enPassantSquare) {
            AddMove(
                moveList,
                from,
                target,
                MOVE_FLAG_CAPTURE | MOVE_FLAG_EN_PASSANT,
                '\0'
            );
        }
    }
}

static void GenerateKnightMoves(
    const Position *position,
    int from,
    MoveList *moveList)
{
    static const int offsets[8][2] = {
        {-2, -1}, {-2, 1}, {-1, -2}, {-1, 2},
        {1, -2}, {1, 2}, {2, -1}, {2, 1}
    };
    int row = RowOf(from);
    int file = FileOf(from);
    Color color = position->sideToMove;

    for (size_t index = 0U; index < 8U; index += 1U) {
        int targetRow = row + offsets[index][0];
        int targetFile = file + offsets[index][1];

        if (!IsInsideBoard(targetRow, targetFile)) {
            continue;
        }

        int target = SquareFromRowFile(targetRow, targetFile);
        char targetPiece = position->board[target];

        if (!IsPieceOfColor(targetPiece, color)) {
            unsigned int flags =
            IsOpponentPiece(targetPiece, color) ? MOVE_FLAG_CAPTURE : 0U;
            AddMove(moveList, from, target, flags, '\0');
        }
    }
}

static void GenerateSlidingMoves(
    const Position *position,
    int from,
    const int directions[][2],
    size_t directionCount,
    MoveList *moveList)
{
    int row = RowOf(from);
    int file = FileOf(from);
    Color color = position->sideToMove;

    for (size_t direction = 0U; direction < directionCount; direction += 1U) {
        int targetRow = row + directions[direction][0];
        int targetFile = file + directions[direction][1];

        while (IsInsideBoard(targetRow, targetFile)) {
            int target = SquareFromRowFile(targetRow, targetFile);
            char targetPiece = position->board[target];

            if (targetPiece == '.') {
                AddMove(moveList, from, target, 0U, '\0');
            } else {
                if (IsOpponentPiece(targetPiece, color)) {
                    AddMove(
                        moveList,
                        from,
                        target,
                        MOVE_FLAG_CAPTURE,
                        '\0'
                    );
                }
                break;
            }

            targetRow += directions[direction][0];
            targetFile += directions[direction][1];
        }
    }
}

static void ApplyMoveUnchecked(
    const Position *source,
    Move move,
    Position *destination)
{
    *destination = *source;

    char movingPiece = destination->board[move.from];
    char capturedPiece = destination->board[move.to];
    Color movingColor = source->sideToMove;
    int isPawnMove = movingPiece == 'P' || movingPiece == 'p';
    int isCapture = (move.flags & MOVE_FLAG_CAPTURE) != 0U;

    destination->board[move.from] = '.';

    if ((move.flags & MOVE_FLAG_EN_PASSANT) != 0U) {
        int capturedPawnSquare =
        movingColor == COLOR_WHITE ? move.to + 8 : move.to - 8;
        capturedPiece = destination->board[capturedPawnSquare];
        destination->board[capturedPawnSquare] = '.';
    }

    destination->board[move.to] =
    (move.flags & MOVE_FLAG_PROMOTION) != 0U
    ? move.promotion
    : movingPiece;

    if ((move.flags & MOVE_FLAG_CASTLING) != 0U) {
        if (movingColor == COLOR_WHITE && move.to == 62) {
            destination->board[63] = '.';
            destination->board[61] = 'R';
        } else if (movingColor == COLOR_WHITE && move.to == 58) {
            destination->board[56] = '.';
            destination->board[59] = 'R';
        } else if (movingColor == COLOR_BLACK && move.to == 6) {
            destination->board[7] = '.';
            destination->board[5] = 'r';
        } else if (movingColor == COLOR_BLACK && move.to == 2) {
            destination->board[0] = '.';
            destination->board[3] = 'r';
        }
    }

    if (movingPiece == 'K') {
        destination->castlingRights &= (uint8_t)~(
            CASTLE_WHITE_KINGSIDE | CASTLE_WHITE_QUEENSIDE
        );
    } else if (movingPiece == 'k') {
        destination->castlingRights &= (uint8_t)~(
            CASTLE_BLACK_KINGSIDE | CASTLE_BLACK_QUEENSIDE
        );
    } else if (movingPiece == 'R') {
        if (move.from == 63) {
            destination->castlingRights &= (uint8_t)~CASTLE_WHITE_KINGSIDE;
        } else if (move.from == 56) {
            destination->castlingRights &= (uint8_t)~CASTLE_WHITE_QUEENSIDE;
        }
    } else if (movingPiece == 'r') {
        if (move.from == 7) {
            destination->castlingRights &= (uint8_t)~CASTLE_BLACK_KINGSIDE;
        } else if (move.from == 0) {
            destination->castlingRights &= (uint8_t)~CASTLE_BLACK_QUEENSIDE;
        }
    }

    if (capturedPiece == 'R') {
        if (move.to == 63) {
            destination->castlingRights &= (uint8_t)~CASTLE_WHITE_KINGSIDE;
        } else if (move.to == 56) {
            destination->castlingRights &= (uint8_t)~CASTLE_WHITE_QUEENSIDE;
        }
    } else if (capturedPiece == 'r') {
        if (move.to == 7) {
            destination->castlingRights &= (uint8_t)~CASTLE_BLACK_KINGSIDE;
        } else if (move.to == 0) {
            destination->castlingRights &= (uint8_t)~CASTLE_BLACK_QUEENSIDE;
        }
    }

    destination->enPassantSquare = -1;

    if ((move.flags & MOVE_FLAG_DOUBLE_PAWN) != 0U) {
        destination->enPassantSquare = (move.from + move.to) / 2;
    }

    if (isPawnMove || isCapture) {
        destination->halfmoveClock = 0U;
    } else {
        destination->halfmoveClock += 1U;
    }

    if (movingColor == COLOR_BLACK) {
        destination->fullmoveNumber += 1U;
    }

    destination->sideToMove = OppositeColor(movingColor);
}

static int IsKingSafeAfterMove(const Position *position, Move move)
{
    Position result;
    Color movingColor = position->sideToMove;
    ApplyMoveUnchecked(position, move, &result);

    int kingSquare = FindKingSquare(&result, movingColor);

    return (
        kingSquare >= 0 &&
        !IsSquareAttacked(&result, kingSquare, OppositeColor(movingColor))
    );
}

static int IsCastlingTransitSafe(
    const Position *position,
    int kingFrom,
    int transitSquare)
{
    Position transit = *position;
    char king = transit.board[kingFrom];
    transit.board[kingFrom] = '.';
    transit.board[transitSquare] = king;

    return !IsSquareAttacked(
        &transit,
        transitSquare,
        OppositeColor(position->sideToMove)
    );
}

static void GenerateKingMoves(
    const Position *position,
    int from,
    MoveList *moveList)
{
    int row = RowOf(from);
    int file = FileOf(from);
    Color color = position->sideToMove;

    for (int rowOffset = -1; rowOffset <= 1; rowOffset += 1) {
        for (int fileOffset = -1; fileOffset <= 1; fileOffset += 1) {
            if (rowOffset == 0 && fileOffset == 0) {
                continue;
            }

            int targetRow = row + rowOffset;
            int targetFile = file + fileOffset;

            if (!IsInsideBoard(targetRow, targetFile)) {
                continue;
            }

            int target = SquareFromRowFile(targetRow, targetFile);
            char targetPiece = position->board[target];

            if (!IsPieceOfColor(targetPiece, color)) {
                unsigned int flags =
                IsOpponentPiece(targetPiece, color) ? MOVE_FLAG_CAPTURE : 0U;
                AddMove(moveList, from, target, flags, '\0');
            }
        }
    }

    Color opponent = OppositeColor(color);

    if (IsSquareAttacked(position, from, opponent)) {
        return;
    }

    if (color == COLOR_WHITE && from == 60 && position->board[60] == 'K') {
        if (
            (position->castlingRights & CASTLE_WHITE_KINGSIDE) != 0U &&
            position->board[61] == '.' &&
            position->board[62] == '.' &&
            position->board[63] == 'R' &&
            IsCastlingTransitSafe(position, 60, 61)
        ) {
            AddMove(
                moveList,
                60,
                62,
                MOVE_FLAG_CASTLING,
                '\0'
            );
        }

        if (
            (position->castlingRights & CASTLE_WHITE_QUEENSIDE) != 0U &&
            position->board[59] == '.' &&
            position->board[58] == '.' &&
            position->board[57] == '.' &&
            position->board[56] == 'R' &&
            IsCastlingTransitSafe(position, 60, 59)
        ) {
            AddMove(
                moveList,
                60,
                58,
                MOVE_FLAG_CASTLING,
                '\0'
            );
        }
    } else if (
        color == COLOR_BLACK &&
        from == 4 &&
        position->board[4] == 'k'
    ) {
        if (
            (position->castlingRights & CASTLE_BLACK_KINGSIDE) != 0U &&
            position->board[5] == '.' &&
            position->board[6] == '.' &&
            position->board[7] == 'r' &&
            IsCastlingTransitSafe(position, 4, 5)
        ) {
            AddMove(
                moveList,
                4,
                6,
                MOVE_FLAG_CASTLING,
                '\0'
            );
        }

        if (
            (position->castlingRights & CASTLE_BLACK_QUEENSIDE) != 0U &&
            position->board[3] == '.' &&
            position->board[2] == '.' &&
            position->board[1] == '.' &&
            position->board[0] == 'r' &&
            IsCastlingTransitSafe(position, 4, 3)
        ) {
            AddMove(
                moveList,
                4,
                2,
                MOVE_FLAG_CASTLING,
                '\0'
            );
        }
    }
}

static void GeneratePseudoLegalMoves(
    const Position *position,
    MoveList *moveList)
{
    static const int rookDirections[4][2] = {
        {-1, 0}, {1, 0}, {0, -1}, {0, 1}
    };
    static const int bishopDirections[4][2] = {
        {-1, -1}, {-1, 1}, {1, -1}, {1, 1}
    };
    static const int queenDirections[8][2] = {
        {-1, 0}, {1, 0}, {0, -1}, {0, 1},
        {-1, -1}, {-1, 1}, {1, -1}, {1, 1}
    };

    moveList->count = 0U;

    for (int from = 0; from < 64; from += 1) {
        char piece = position->board[from];

        if (!IsPieceOfColor(piece, position->sideToMove)) {
            continue;
        }

        switch ((char)tolower((unsigned char)piece)) {
            case 'p':
                GeneratePawnMoves(position, from, moveList);
                break;
            case 'n':
                GenerateKnightMoves(position, from, moveList);
                break;
            case 'b':
                GenerateSlidingMoves(
                    position,
                    from,
                    bishopDirections,
                    4U,
                    moveList
                );
                break;
            case 'r':
                GenerateSlidingMoves(
                    position,
                    from,
                    rookDirections,
                    4U,
                    moveList
                );
                break;
            case 'q':
                GenerateSlidingMoves(
                    position,
                    from,
                    queenDirections,
                    8U,
                    moveList
                );
                break;
            case 'k':
                GenerateKingMoves(position, from, moveList);
                break;
            default:
                break;
        }
    }
}

static void GenerateLegalMoves(const Position *position, MoveList *legalMoves)
{
    MoveList pseudoMoves;
    GeneratePseudoLegalMoves(position, &pseudoMoves);
    legalMoves->count = 0U;

    for (size_t index = 0U; index < pseudoMoves.count; index += 1U) {
        Move move = pseudoMoves.moves[index];

        if (IsKingSafeAfterMove(position, move)) {
            AddMove(
                legalMoves,
                move.from,
                move.to,
                move.flags,
                move.promotion
            );
        }
    }
}

static int BoardsEqual(const char left[64], const char right[64])
{
    return memcmp(left, right, BOARD_SQUARE_COUNT) == 0;
}

static int FindMatchingMove(
    const Position *position,
    const char observedBoard[64],
    Move *matchingMoveOut,
    Position *resultOut,
    size_t *matchCountOut)
{
    MoveList legalMoves;
    GenerateLegalMoves(position, &legalMoves);

    size_t matchCount = 0U;
    Move matchingMove = {0};
    Position matchingPosition = {0};

    for (size_t index = 0U; index < legalMoves.count; index += 1U) {
        Position candidate;
        ApplyMoveUnchecked(position, legalMoves.moves[index], &candidate);

        if (BoardsEqual(candidate.board, observedBoard)) {
            matchingMove = legalMoves.moves[index];
            matchingPosition = candidate;
            matchCount += 1U;
        }
    }

    if (matchCountOut != NULL) {
        *matchCountOut = matchCount;
    }

    if (matchCount != 1U) {
        return 0;
    }

    if (matchingMoveOut != NULL) {
        *matchingMoveOut = matchingMove;
    }

    if (resultOut != NULL) {
        *resultOut = matchingPosition;
    }

    return 1;
}

/*
 * Joining a game in progress.
 *
 * A single board frame cannot tell us whose turn it is, so we wait for a
 * second frame: the difference between two consecutive frames reveals which
 * colour just moved, and therefore who is to move now. Castling rights are a
 * best guess from piece placement (wrong only if a king or rook left its home
 * square and returned, which barely affects an evaluation). En passant is
 * recoverable exactly, because a double pawn push is visible in the diff.
 */
static int InferMovedColor(
    const char previousBoard[64],
    const char observedBoard[64],
    Color *movedColorOut)
{
    int whiteAppeared = 0;
    int blackAppeared = 0;

    for (int square = 0; square < (int)BOARD_SQUARE_COUNT; square += 1) {
        const char before = previousBoard[square];
        const char now = observedBoard[square];

        if (now == '.' || now == before) {
            continue;
        }

        if (IsWhitePiece(now)) {
            if (!IsWhitePiece(before)) {
                whiteAppeared += 1;
            }
        } else if (!IsBlackPiece(before)) {
            blackAppeared += 1;
        }
    }

    /*
     * Exactly one side should have pieces on newly occupied squares. If both
     * do we missed a frame and cannot tell the two moves apart, so refuse.
     */
    if (whiteAppeared > 0 && blackAppeared == 0) {
        *movedColorOut = COLOR_WHITE;
        return 1;
    }

    if (blackAppeared > 0 && whiteAppeared == 0) {
        *movedColorOut = COLOR_BLACK;
        return 1;
    }

    return 0;
}

static uint8_t GuessCastlingRights(const char board[64])
{
    uint8_t rights = 0U;

    /* index 60 = e1, 63 = h1, 56 = a1, 4 = e8, 7 = h8, 0 = a8 */
    if (board[60] == 'K') {
        if (board[63] == 'R') {
            rights |= CASTLE_WHITE_KINGSIDE;
        }
        if (board[56] == 'R') {
            rights |= CASTLE_WHITE_QUEENSIDE;
        }
    }

    if (board[4] == 'k') {
        if (board[7] == 'r') {
            rights |= CASTLE_BLACK_KINGSIDE;
        }
        if (board[0] == 'r') {
            rights |= CASTLE_BLACK_QUEENSIDE;
        }
    }

    return rights;
}

static int DetectEnPassantSquare(
    const char previousBoard[64],
    const char observedBoard[64],
    Color movedColor)
{
    if (movedColor == COLOR_WHITE) {
        /* rank 4 occupies indices 32..39; the pawn came from rank 2 */
        for (int square = 32; square < 40; square += 1) {
            if (
                observedBoard[square] == 'P' &&
                previousBoard[square] == '.' &&
                previousBoard[square + 16] == 'P' &&
                observedBoard[square + 16] == '.'
            ) {
                return square + 8;
            }
        }

        return -1;
    }

    /* rank 5 occupies indices 24..31; the pawn came from rank 7 */
    for (int square = 24; square < 32; square += 1) {
        if (
            observedBoard[square] == 'p' &&
            previousBoard[square] == '.' &&
            previousBoard[square - 16] == 'p' &&
            observedBoard[square - 16] == '.'
        ) {
            return square - 8;
        }
    }

    return -1;
}

static void AdoptPosition(
    Position *position,
    const char previousBoard[64],
    const char observedBoard[64],
    Color movedColor)
{
    memcpy(position->board, observedBoard, BOARD_SQUARE_COUNT);
    position->sideToMove = OppositeColor(movedColor);
    position->castlingRights = GuessCastlingRights(observedBoard);
    position->enPassantSquare =
    DetectEnPassantSquare(previousBoard, observedBoard, movedColor);

    /*
     * Unknowable from the board alone. Neither figure changes an evaluation
     * in any way that matters for an overlay.
     */
    position->halfmoveClock = 0U;
    position->fullmoveNumber = 1U;
}

static int AppendCharacter(
    char *output,
    size_t outputSize,
    size_t *offset,
    char character)
{
    if (*offset + 1U >= outputSize) {
        return 0;
    }

    output[*offset] = character;
    *offset += 1U;
    output[*offset] = '\0';
    return 1;
}

static int AppendText(
    char *output,
    size_t outputSize,
    size_t *offset,
    const char *text)
{
    size_t textLength = strlen(text);

    if (*offset + textLength >= outputSize) {
        return 0;
    }

    memcpy(output + *offset, text, textLength);
    *offset += textLength;
    output[*offset] = '\0';
    return 1;
}

static int PositionToFen(
    const Position *position,
    char output[MAX_FEN_LENGTH])
{
    size_t offset = 0U;
    output[0] = '\0';

    for (int row = 0; row < 8; row += 1) {
        int emptyCount = 0;

        for (int file = 0; file < 8; file += 1) {
            char piece = position->board[SquareFromRowFile(row, file)];

            if (piece == '.') {
                emptyCount += 1;
                continue;
            }

            if (emptyCount > 0) {
                if (!AppendCharacter(
                    output,
                    MAX_FEN_LENGTH,
                    &offset,
                    (char)('0' + emptyCount))) {
                    return 0;
                    }
                    emptyCount = 0;
            }

            if (!AppendCharacter(output, MAX_FEN_LENGTH, &offset, piece)) {
                return 0;
            }
        }

        if (emptyCount > 0) {
            if (!AppendCharacter(
                output,
                MAX_FEN_LENGTH,
                &offset,
                (char)('0' + emptyCount))) {
                return 0;
                }
        }

        if (row != 7) {
            if (!AppendCharacter(output, MAX_FEN_LENGTH, &offset, '/')) {
                return 0;
            }
        }
    }

    if (!AppendText(
        output,
        MAX_FEN_LENGTH,
        &offset,
        position->sideToMove == COLOR_WHITE ? " w " : " b ")) {
        return 0;
        }

        if (position->castlingRights == 0U) {
            if (!AppendCharacter(output, MAX_FEN_LENGTH, &offset, '-')) {
                return 0;
            }
        } else {
            if (
                (position->castlingRights & CASTLE_WHITE_KINGSIDE) != 0U &&
                !AppendCharacter(output, MAX_FEN_LENGTH, &offset, 'K')) {
                return 0;
                }
                if (
                    (position->castlingRights & CASTLE_WHITE_QUEENSIDE) != 0U &&
                    !AppendCharacter(output, MAX_FEN_LENGTH, &offset, 'Q')) {
                    return 0;
                    }
                    if (
                        (position->castlingRights & CASTLE_BLACK_KINGSIDE) != 0U &&
                        !AppendCharacter(output, MAX_FEN_LENGTH, &offset, 'k')) {
                        return 0;
                        }
                        if (
                            (position->castlingRights & CASTLE_BLACK_QUEENSIDE) != 0U &&
                            !AppendCharacter(output, MAX_FEN_LENGTH, &offset, 'q')) {
                            return 0;
                            }
        }

        if (!AppendCharacter(output, MAX_FEN_LENGTH, &offset, ' ')) {
            return 0;
        }

        if (position->enPassantSquare < 0) {
            if (!AppendCharacter(output, MAX_FEN_LENGTH, &offset, '-')) {
                return 0;
            }
        } else {
            char squareName[3];
            SquareToName(position->enPassantSquare, squareName);

            if (!AppendText(output, MAX_FEN_LENGTH, &offset, squareName)) {
                return 0;
            }
        }

        int written = snprintf(
            output + offset,
            MAX_FEN_LENGTH - offset,
            " %u %u",
            position->halfmoveClock,
            position->fullmoveNumber
        );

        return written >= 0 && (size_t)written < MAX_FEN_LENGTH - offset;
}

static void MoveToUci(Move move, char output[6])
{
    char from[3];
    char to[3];
    SquareToName(move.from, from);
    SquareToName(move.to, to);

    output[0] = from[0];
    output[1] = from[1];
    output[2] = to[0];
    output[3] = to[1];

    if ((move.flags & MOVE_FLAG_PROMOTION) != 0U) {
        output[4] = (char)tolower((unsigned char)move.promotion);
        output[5] = '\0';
    } else {
        output[4] = '\0';
    }
}

static void LogBoard(FILE *logFile, const char board[64], size_t pieceCount)
{
    fprintf(logFile, "Observed position (%zu pieces):\n", pieceCount);

    for (size_t row = 0U; row < 8U; row += 1U) {
        size_t rowStart = row * 8U;
        fprintf(logFile, "  %.*s\n", 8, board + rowStart);
    }
}

static void LogBoardDifference(
    FILE *logFile,
    const char previous[64],
    const char observed[64])
{
    fprintf(logFile, "Changed squares:\n");

    for (int square = 0; square < 64; square += 1) {
        if (previous[square] != observed[square]) {
            char squareName[3];
            SquareToName(square, squareName);
            fprintf(
                logFile,
                "  %s: %c -> %c\n",
                squareName,
                previous[square],
                observed[square]
            );
        }
    }
}

static int WriteResponse(
    char response[MAX_RESPONSE_LENGTH],
    int accepted,
    const char *reason,
    const char *fen,
    const char *uci)
{
    int written;

    if (fen != NULL && uci != NULL) {
        written = snprintf(
            response,
            MAX_RESPONSE_LENGTH,
            "{\"ok\":true,\"accepted\":%s,\"reason\":\"%s\","
            "\"uci\":\"%s\",\"fen\":\"%s\"}",
            accepted ? "true" : "false",
            reason,
            uci,
            fen
        );
    } else if (fen != NULL) {
        written = snprintf(
            response,
            MAX_RESPONSE_LENGTH,
            "{\"ok\":true,\"accepted\":%s,\"reason\":\"%s\","
            "\"fen\":\"%s\"}",
            accepted ? "true" : "false",
            reason,
            fen
        );
    } else {
        written = snprintf(
            response,
            MAX_RESPONSE_LENGTH,
            "{\"ok\":true,\"accepted\":%s,\"reason\":\"%s\"}",
            accepted ? "true" : "false",
            reason
        );
    }

    return written >= 0 && (size_t)written < MAX_RESPONSE_LENGTH;
}

/*
 * Holds the first frame seen when we join a game already in progress. See
 * InferMovedColor: two frames are needed before a position can be adopted.
 */
static char pendingBoard[BOARD_SQUARE_COUNT];
static int hasPendingBoard = 0;

static void HandleMessage(
    const char *message,
    FILE *logFile,
    Position *position,
    int *hasPosition,
    char response[MAX_RESPONSE_LENGTH])
{
    char type[32];
    int visuallyFlipped = 0;

    if (!ExtractJsonStringField(message, "type", type, sizeof(type))) {
        fprintf(logFile, "Rejected message without a valid type field.\n");
        fflush(logFile);
        (void)WriteResponse(response, 0, "invalid_type", NULL, NULL);
        return;
    }

    if (strcmp(type, "position_snapshot") != 0) {
        fprintf(logFile, "Ignored message type: %s\n", type);
        fflush(logFile);
        (void)WriteResponse(response, 0, "ignored_type", NULL, NULL);
        return;
    }

    char boardString[BOARD_SQUARE_COUNT + 1U];

    if (!ExtractJsonStringField(
        message,
        "board",
        boardString,
        sizeof(boardString))) {
        fprintf(logFile, "Rejected position without a valid board field.\n");
    fflush(logFile);
    (void)WriteResponse(response, 0, "invalid_board_field", NULL, NULL);
    return;
        }

        (void)ExtractJsonBoolField(message, "visually_flipped", &visuallyFlipped);

        size_t pieceCount = 0U;

        if (!ValidateBoardString(boardString, &pieceCount)) {
            fprintf(logFile, "Rejected invalid board: %s\n", boardString);
            fflush(logFile);
            (void)WriteResponse(response, 0, "invalid_board", NULL, NULL);
            return;
        }

        if (
            *hasPosition &&
            memcmp(position->board, boardString, BOARD_SQUARE_COUNT) == 0
        ) {
            fprintf(logFile, "Ignored duplicate position.\n");
            fflush(logFile);
            (void)WriteResponse(response, 0, "duplicate", NULL, NULL);
            return;
        }

        if (memcmp(boardString, INITIAL_BOARD, BOARD_SQUARE_COUNT) == 0) {
            InitializePosition(position);
            *hasPosition = 1;

            char fen[MAX_FEN_LENGTH];

            if (!PositionToFen(position, fen)) {
                (void)WriteResponse(response, 0, "fen_error", NULL, NULL);
                return;
            }

            hasPendingBoard = 0;

            fprintf(logFile, "\n=== New standard game ===\n");
            LogBoard(logFile, position->board, pieceCount);
            fprintf(logFile, "FEN: %s\n", fen);
            fflush(logFile);

            AnalysisPublish(fen, visuallyFlipped);

            (void)WriteResponse(response, 1, "game_started", fen, NULL);
            return;
        }

        if (!*hasPosition) {
            Color movedColor;

            /*
             * Spectating starts mid-game, so refusing everything but the
             * initial position would mean never locking on. Adopt instead,
             * once a second frame tells us whose turn it is.
             */
            if (
                hasPendingBoard &&
                InferMovedColor(pendingBoard, boardString, &movedColor)
            ) {
                char fen[MAX_FEN_LENGTH];

                AdoptPosition(position, pendingBoard, boardString, movedColor);
                *hasPosition = 1;
                hasPendingBoard = 0;

                if (!PositionToFen(position, fen)) {
                    (void)WriteResponse(response, 0, "fen_error", NULL, NULL);
                    return;
                }

                fprintf(logFile, "\n=== Adopted game in progress ===\n");
                LogBoard(logFile, position->board, pieceCount);
                fprintf(
                    logFile,
                    "FEN: %s (castling rights are a best guess)\n",
                    fen
                );
                fflush(logFile);

                AnalysisPublish(fen, visuallyFlipped);

                (void)WriteResponse(response, 1, "game_adopted", fen, NULL);
                return;
            }

            memcpy(pendingBoard, boardString, BOARD_SQUARE_COUNT);
            hasPendingBoard = 1;

            fprintf(
                logFile,
                "Holding first frame of a game in progress; "
                "waiting for one more move to establish the side to move.\n"
            );
            LogBoard(logFile, boardString, pieceCount);
            fflush(logFile);
            (void)WriteResponse(response, 0, "waiting_for_second_frame", NULL, NULL);
            return;
        }

        Move matchingMove;
        Position result;
        size_t matchCount = 0U;

        if (!FindMatchingMove(
            position,
            boardString,
            &matchingMove,
            &result,
            &matchCount)) {
            fprintf(
                logFile,
                "Rejected transition: %zu legal moves matched the observed board.\n",
                matchCount
            );
        LogBoardDifference(logFile, position->board, boardString);
        LogBoard(logFile, boardString, pieceCount);
        fflush(logFile);
        (void)WriteResponse(
            response,
            0,
            matchCount == 0U ? "no_legal_move_match" : "ambiguous_move",
            NULL,
            NULL
        );
        return;
            }

            *position = result;

            char fen[MAX_FEN_LENGTH];
            char uci[6];
            MoveToUci(matchingMove, uci);

            if (!PositionToFen(position, fen)) {
                (void)WriteResponse(response, 0, "fen_error", NULL, NULL);
                return;
            }

            fprintf(logFile, "\nMove: %s\n", uci);
            LogBoard(logFile, position->board, pieceCount);
            fprintf(logFile, "FEN: %s\n", fen);
            fflush(logFile);

            AnalysisPublish(fen, visuallyFlipped);

            (void)WriteResponse(response, 1, "move_recorded", fen, uci);
}

int main(void)
{
    const char *debug = getenv("CHESSLISTENER_DEBUG");
    const char *logPath =
        debug != NULL && strcmp(debug, "1") == 0
            ? LOG_PATH
            : "/dev/null";
    FILE *logFile = fopen(logPath, "a");

    if (logFile == NULL) {
        return EXIT_FAILURE;
    }

    /*
     * Unbuffered stdin. This used to be load bearing: analysis.c polled this
     * descriptor to spot a queued position, and poll() cannot see bytes stdio
     * has already pulled into a FILE buffer. That check is gone -- the engine
     * runs on its own thread now and the loop below never stalls -- but
     * reading straight from the pipe is still the honest thing to do here.
     */
    setvbuf(stdin, NULL, _IONBF, 0);

    if (!AnalysisStart(logFile)) {
        fclose(logFile);
        return EXIT_SUCCESS;
    }

    Position position = {0};
    int hasPosition = 0;

    for (;;) {
        char *message = NULL;

        if (!ReadNativeMessage(&message)) {
            break;
        }

        char response[MAX_RESPONSE_LENGTH];
        HandleMessage(message, logFile, &position, &hasPosition, response);

        if (!WriteNativeMessage(response)) {
            free(message);
            break;
        }

        free(message);
    }

    AnalysisStop();

    fclose(logFile);
    return EXIT_SUCCESS;
}

