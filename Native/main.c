#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <ctype.h>
#include <inttypes.h>
#include <limits.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "analysis.h"
#include "version.h"

#define MAX_MESSAGE_SIZE (1024U * 1024U)
#define LOG_PATH "/tmp/chess-listener.log"
#define BOARD_SQUARE_COUNT 64U
#define MAX_LEGAL_MOVES 256U
#define MAX_FEN_LENGTH 128U
#define MAX_RESPONSE_LENGTH 1024U
#define MAX_SESSION_ID_LENGTH 128U
#define MAX_SESSION_LABEL_LENGTH 256U
#define MAX_EVENT_LENGTH 1024U
#define MAX_HISTORY_TEXT_LENGTH (32U * 1024U)
#define MAX_HISTORY_MOVES 1024U
#define MAX_HISTORY_TOKEN_LENGTH 64U
#define MAX_STATE_SOURCE_LENGTH 8U

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

#define MAX_EXPLORER_NODES 256U
#define MAX_EXPLORER_START_PLIES 32U

typedef struct {
    Position position;
    unsigned int parent;
    char uci[6];
} ExplorerNode;

typedef struct {
    int active;
    int targeting;
    uint64_t id;
    unsigned int selectedNode;
    unsigned int nodeCount;
    char baseSource[MAX_STATE_SOURCE_LENGTH + 1U];
    ExplorerNode nodes[MAX_EXPLORER_NODES];
} ExplorerBranch;

typedef struct {
    int active;
    char id[MAX_SESSION_ID_LENGTH + 1U];
    Position position;
    int hasPosition;
    char lastMove[6];
    char pendingBoard[BOARD_SQUARE_COUNT];
    int hasPendingBoard;
    int pendingIsMismatch;
    int pendingRecoveryFailed;
    uint64_t pendingSnapshotSequence;
    uint64_t lastSnapshotSequence;
    int hasSnapshotSequence;
    uint64_t lastHistorySequence;
    int hasHistorySequence;
    char stateSource[MAX_STATE_SOURCE_LENGTH + 1U];
    int visuallyFlipped;
    int hasOrientation;
    ExplorerBranch explorer;
} SessionState;

/* Firefox native-messaging frames may originate on the browser thread or on
 * analysis.c's overlay-control thread. Header and payload must be one atomic
 * critical section or concurrent writes corrupt the byte stream. */
static pthread_mutex_t nativeOutputLock = PTHREAD_MUTEX_INITIALIZER;

static const char INITIAL_BOARD[BOARD_SQUARE_COUNT + 1U] =
"rnbqkbnr"
"pppppppp"
"........"
"........"
"........"
"........"
"PPPPPPPP"
"RNBQKBNR";

/* Kept below JavaScript's exact-integer ceiling for JSON consumers.  A native
 * host owns one browser connection, so process-local monotonic IDs are ample. */
static uint64_t nextExplorerBranchId = 1U;

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
    int result = 0;

    if (json == NULL) {
        return 0;
    }

    size_t stringLength = strlen(json);

    if (stringLength > UINT32_MAX) {
        return 0;
    }

    uint32_t messageLength = (uint32_t)stringLength;

    if (pthread_mutex_lock(&nativeOutputLock) != 0) {
        return 0;
    }

    if (fwrite(&messageLength, sizeof(messageLength), 1U, stdout) != 1U) {
        goto done;
    }

    if (fwrite(json, 1U, messageLength, stdout) != messageLength) {
        goto done;
    }

    result = fflush(stdout) == 0;

done:
    (void)pthread_mutex_unlock(&nativeOutputLock);
    return result;
}

static int AppendJsonEscaped(
    char *output,
    size_t outputSize,
    size_t *offset,
    const char *text)
{
    static const char hex[] = "0123456789abcdef";

    if (output == NULL || offset == NULL || text == NULL) {
        return 0;
    }

    for (const unsigned char *cursor = (const unsigned char *)text;
         *cursor != '\0';
         cursor += 1) {
        unsigned char character = *cursor;

        if (character == '"' || character == '\\') {
            if (*offset + 2U >= outputSize) {
                return 0;
            }
            output[(*offset)++] = '\\';
            output[(*offset)++] = (char)character;
        } else if (character < 0x20U) {
            if (*offset + 6U >= outputSize) {
                return 0;
            }
            output[(*offset)++] = '\\';
            output[(*offset)++] = 'u';
            output[(*offset)++] = '0';
            output[(*offset)++] = '0';
            output[(*offset)++] = hex[character >> 4U];
            output[(*offset)++] = hex[character & 0x0fU];
        } else {
            if (*offset + 1U >= outputSize) {
                return 0;
            }
            output[(*offset)++] = (char)character;
        }
    }

    output[*offset] = '\0';
    return 1;
}

static void ForwardAnalysisEvent(
    const char *kind,
    const char *name,
    const char *payload,
    const char *sessionId,
    void *context)
{
    char message[MAX_EVENT_LENGTH];
    size_t offset;
    int written;

    (void)context;

    if (kind == NULL || name == NULL) {
        return;
    }

    if (strcmp(kind, "command") == 0) {
        if (strcmp(name, "rescan") != 0 &&
            strcmp(name, "set_fen") != 0 &&
            strcmp(name, "restart_engines") != 0 &&
            strcmp(name, "stop_session") != 0 &&
            strcmp(name, "explore_start") != 0 &&
            strcmp(name, "explore_move") != 0 &&
            strcmp(name, "explore_goto") != 0 &&
            strcmp(name, "explore_live") != 0 &&
            strcmp(name, "explore_resume") != 0) {
            return;
        }
        if (sessionId == NULL || *sessionId == '\0') {
            return;
        }

        written = snprintf(
            message,
            sizeof(message),
            "{\"type\":\"overlay_command\",\"command\":\"%s\"",
            name);
    } else if (strcmp(kind, "event") == 0) {
        if (strcmp(name, "dismissed") != 0) {
            return;
        }

        written = snprintf(
            message,
            sizeof(message),
            "{\"type\":\"overlay_event\",\"event\":\"%s\"",
            name);
    } else {
        return;
    }

    if (written < 0 || (size_t)written >= sizeof(message)) {
        return;
    }
    offset = (size_t)written;

    if (sessionId != NULL && *sessionId != '\0') {
        static const char prefix[] = ",\"session_id\":\"";
        size_t prefixLength = sizeof(prefix) - 1U;

        if (offset + prefixLength >= sizeof(message)) {
            return;
        }
        memcpy(message + offset, prefix, prefixLength);
        offset += prefixLength;
        message[offset] = '\0';

        if (!AppendJsonEscaped(message, sizeof(message), &offset, sessionId) ||
            offset + 2U >= sizeof(message)) {
            return;
        }
        message[offset++] = '"';
        message[offset] = '\0';
    }

    if (payload != NULL) {
        static const char prefix[] = ",\"payload\":\"";
        size_t prefixLength = sizeof(prefix) - 1U;

        if (offset + prefixLength >= sizeof(message)) {
            return;
        }
        memcpy(message + offset, prefix, prefixLength);
        offset += prefixLength;
        message[offset] = '\0';

        if (!AppendJsonEscaped(message, sizeof(message), &offset, payload) ||
            offset + 2U >= sizeof(message)) {
            return;
        }
        message[offset++] = '"';
    }

    if (offset + 1U >= sizeof(message)) {
        return;
    }
    message[offset++] = '}';
    message[offset] = '\0';

    (void)WriteNativeMessage(message);
}

/* Locate a restricted JSON object's field value. Continue past matching text
 * in string values (for example a game key literally equal to "force") and
 * accept only a quoted name followed by a colon. */
static const char *FindJsonFieldValue(
    const char *json,
    const char *fieldName)
{
    char key[64];
    int keyLength;
    const char *cursor;

    if (json == NULL || fieldName == NULL) {
        return NULL;
    }

    keyLength = snprintf(key, sizeof(key), "\"%s\"", fieldName);
    if (keyLength < 0 || (size_t)keyLength >= sizeof(key)) {
        return NULL;
    }

    cursor = json;
    while ((cursor = strstr(cursor, key)) != NULL) {
        const char *before = cursor;
        const char *value = cursor + (size_t)keyLength;

        while (before > json &&
               isspace((unsigned char)before[-1]) != 0) {
            before -= 1;
        }

        if (before == json || (before[-1] != '{' && before[-1] != ',')) {
            cursor += (size_t)keyLength;
            continue;
        }

        while (isspace((unsigned char)*value) != 0) {
            value += 1;
        }
        if (*value == ':') {
            value += 1;
            while (isspace((unsigned char)*value) != 0) {
                value += 1;
            }
            return value;
        }

        cursor += (size_t)keyLength;
    }

    return NULL;
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

    const char *cursor = FindJsonFieldValue(json, fieldName);

    if (cursor == NULL) {
        return 0;
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
    const char *cursor;

    if (json == NULL || fieldName == NULL || valueOut == NULL) {
        return 0;
    }

    cursor = FindJsonFieldValue(json, fieldName);
    if (cursor == NULL) {
        return 0;
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

/* Same restricted parser, for a base-10 integer field. */
static int ExtractJsonIntField(
    const char *json,
    const char *fieldName,
    int *valueOut)
{
    const char *cursor;
    char *end;
    long value;

    if (json == NULL || fieldName == NULL || valueOut == NULL) {
        return 0;
    }

    cursor = FindJsonFieldValue(json, fieldName);
    if (cursor == NULL) {
        return 0;
    }

    value = strtol(cursor, &end, 10);

    if (end == cursor || value < 0L || value > 999L) {
        return 0;
    }

    while (isspace((unsigned char)*end) != 0) {
        end += 1;
    }

    if (*end != ',' && *end != '}') {
        return 0;
    }

    *valueOut = (int)value;
    return 1;
}

/* Same restricted parser, for a non-negative 64-bit sequence number. */
static int ExtractJsonUint64Field(
    const char *json,
    const char *fieldName,
    uint64_t *valueOut)
{
    const char *cursor;
    char *end;
    unsigned long long value;

    if (json == NULL || fieldName == NULL || valueOut == NULL) {
        return 0;
    }

    cursor = FindJsonFieldValue(json, fieldName);
    if (cursor == NULL) {
        return 0;
    }
    if (*cursor == '-' || *cursor == '+') {
        return 0;
    }

    errno = 0;
    value = strtoull(cursor, &end, 10);
    if (end == cursor || errno == ERANGE || value > UINT64_MAX) {
        return 0;
    }

    while (isspace((unsigned char)*end) != 0) {
        end += 1;
    }
    if (*end != ',' && *end != '}') {
        return 0;
    }

    *valueOut = (uint64_t)value;
    return 1;
}

static int IsValidSessionId(const char *sessionId)
{
    size_t length;

    if (sessionId == NULL) {
        return 0;
    }

    length = strlen(sessionId);
    if (length == 0U || length > MAX_SESSION_ID_LENGTH) {
        return 0;
    }

    for (size_t index = 0U; index < length; index += 1U) {
        unsigned char character = (unsigned char)sessionId[index];
        if (isalnum(character) == 0 && character != '-' && character != '_' &&
            character != '.' && character != ':') {
            return 0;
        }
    }

    return 1;
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

static int ParseFenUnsigned(
    const char **cursorInOut,
    unsigned int minimum,
    unsigned int *valueOut,
    int finalField)
{
    const char *cursor;
    char *end;
    unsigned long value;

    if (cursorInOut == NULL || *cursorInOut == NULL || valueOut == NULL) {
        return 0;
    }

    cursor = *cursorInOut;
    if (isdigit((unsigned char)*cursor) == 0) {
        return 0;
    }

    errno = 0;
    value = strtoul(cursor, &end, 10);
    if (end == cursor || errno == ERANGE || value > UINT_MAX ||
        value < minimum) {
        return 0;
    }

    if (finalField) {
        if (*end != '\0') {
            return 0;
        }
    } else {
        if (*end != ' ') {
            return 0;
        }
        end += 1;
    }

    *valueOut = (unsigned int)value;
    *cursorInOut = end;
    return 1;
}

/* Parse the complete six-field FEN used by the authoritative state override.
 * In addition to syntax, reject structurally impossible boards, castling
 * rights without their home king/rook, and en-passant targets that cannot
 * have resulted from the immediately preceding double pawn move. */
static int PositionFromFen(const char *fen, Position *positionOut)
{
    Position position;
    const char *cursor;
    char validationBoard[BOARD_SQUARE_COUNT + 1U];

    if (fen == NULL || positionOut == NULL || *fen == '\0' ||
        strlen(fen) >= MAX_FEN_LENGTH) {
        return 0;
    }

    memset(&position, 0, sizeof(position));
    memset(position.board, '.', sizeof(position.board));
    position.enPassantSquare = -1;
    cursor = fen;

    for (int row = 0; row < 8; row += 1) {
        int file = 0;

        while (*cursor != '\0' && *cursor != '/' && *cursor != ' ') {
            if (*cursor >= '1' && *cursor <= '8') {
                file += *cursor - '0';
                if (file > 8) {
                    return 0;
                }
            } else if (strchr("PNBRQKpnbrqk", *cursor) != NULL) {
                if (file >= 8) {
                    return 0;
                }
                position.board[SquareFromRowFile(row, file)] = *cursor;
                file += 1;
            } else {
                return 0;
            }
            cursor += 1;
        }

        if (file != 8) {
            return 0;
        }

        if (row < 7) {
            if (*cursor != '/') {
                return 0;
            }
        } else if (*cursor != ' ') {
            return 0;
        }
        cursor += 1;
    }

    if (cursor[0] == 'w' && cursor[1] == ' ') {
        position.sideToMove = COLOR_WHITE;
    } else if (cursor[0] == 'b' && cursor[1] == ' ') {
        position.sideToMove = COLOR_BLACK;
    } else {
        return 0;
    }
    cursor += 2;

    if (*cursor == '-') {
        cursor += 1;
        if (*cursor != ' ') {
            return 0;
        }
    } else {
        int sawRight = 0;

        while (*cursor != '\0' && *cursor != ' ') {
            uint8_t right;

            switch (*cursor) {
                case 'K': right = CASTLE_WHITE_KINGSIDE; break;
                case 'Q': right = CASTLE_WHITE_QUEENSIDE; break;
                case 'k': right = CASTLE_BLACK_KINGSIDE; break;
                case 'q': right = CASTLE_BLACK_QUEENSIDE; break;
                default: return 0;
            }

            if ((position.castlingRights & right) != 0U) {
                return 0;
            }
            position.castlingRights |= right;
            sawRight = 1;
            cursor += 1;
        }

        if (!sawRight || *cursor != ' ') {
            return 0;
        }
    }
    cursor += 1;

    if (*cursor == '-') {
        position.enPassantSquare = -1;
        cursor += 1;
        if (*cursor != ' ') {
            return 0;
        }
    } else {
        char fileName = cursor[0];
        char rankName = cursor[1];
        int row;
        int file;

        if (fileName < 'a' || fileName > 'h' || rankName == '\0' ||
            (rankName != '3' && rankName != '6') || cursor[2] != ' ') {
            return 0;
        }

        row = '8' - rankName;
        file = fileName - 'a';
        position.enPassantSquare = SquareFromRowFile(row, file);
        cursor += 2;
    }
    cursor += 1;

    memcpy(validationBoard, position.board, BOARD_SQUARE_COUNT);
    validationBoard[BOARD_SQUARE_COUNT] = '\0';

    if (!ParseFenUnsigned(&cursor, 0U, &position.halfmoveClock, 0) ||
        !ParseFenUnsigned(&cursor, 1U, &position.fullmoveNumber, 1) ||
        !ValidateBoardString(validationBoard, NULL)) {
        return 0;
    }

    if (((position.castlingRights & CASTLE_WHITE_KINGSIDE) != 0U &&
         (position.board[60] != 'K' || position.board[63] != 'R')) ||
        ((position.castlingRights & CASTLE_WHITE_QUEENSIDE) != 0U &&
         (position.board[60] != 'K' || position.board[56] != 'R')) ||
        ((position.castlingRights & CASTLE_BLACK_KINGSIDE) != 0U &&
         (position.board[4] != 'k' || position.board[7] != 'r')) ||
        ((position.castlingRights & CASTLE_BLACK_QUEENSIDE) != 0U &&
         (position.board[4] != 'k' || position.board[0] != 'r'))) {
        return 0;
    }

    if (position.enPassantSquare >= 0) {
        int square = position.enPassantSquare;
        char rank = (char)('8' - RowOf(square));

        if (position.board[square] != '.') {
            return 0;
        }

        if (position.sideToMove == COLOR_WHITE) {
            if (rank != '6' || square + 8 >= (int)BOARD_SQUARE_COUNT ||
                square - 8 < 0 || position.board[square + 8] != 'p' ||
                position.board[square - 8] != '.') {
                return 0;
            }
        } else if (rank != '3' || square - 8 < 0 ||
                   square + 8 >= (int)BOARD_SQUARE_COUNT ||
                   position.board[square - 8] != 'P' ||
                   position.board[square + 8] != '.') {
            return 0;
        }
    }

    *positionOut = position;
    return 1;
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

/*
 * Recovering from skipped snapshots.
 *
 * content.js debounces and double-reads before it sends, so a fast sequence --
 * a premove chain, or just a strong player blitzing a forced line -- arrives as
 * ONE board frame that is several plies ahead. Matching a single move against it
 * fails, and because the tracker then keeps its stale position, every later
 * frame fails too: one skipped snapshot used to kill the rest of the game.
 *
 * This is deliberately a delayed recovery path, never part of ordinary move
 * capture. Search shallowest first and label its result inferred: two legal
 * paths can land on the same visible pieces while disagreeing about hidden
 * state such as en-passant or the halfmove clock.
 */
#define MAX_CATCHUP_PLIES 6
#define CATCHUP_NODE_BUDGET 100000UL
#define CATCHUP_DEADLINE_NS (50ULL * 1000ULL * 1000ULL)

#define MAX_SQUARES_PER_PLY 4

static uint64_t MonotonicNanoseconds(void)
{
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return 0U;
    }

    return (uint64_t)now.tv_sec * 1000000000ULL + (uint64_t)now.tv_nsec;
}

static int CatchupLimitReached(
    const unsigned long *nodeBudget,
    uint64_t deadlineNanoseconds)
{
    uint64_t now;

    if (nodeBudget == NULL || *nodeBudget == 0UL) {
        return 1;
    }

    now = MonotonicNanoseconds();
    return now == 0U || now >= deadlineNanoseconds;
}

static int OutOfReach(
    const char current[64],
    const char observed[64],
    int pliesRemaining)
{
    int differing = 0;
    int allowance = pliesRemaining * MAX_SQUARES_PER_PLY;

    for (size_t square = 0U; square < BOARD_SQUARE_COUNT; square += 1U) {
        if (current[square] != observed[square]) {
            differing += 1;

            if (differing > allowance) {
                return 1;
            }
        }
    }

    return 0;
}

static int SearchMoveSequence(
    const Position *position,
    const char observedBoard[64],
    int pliesRemaining,
    unsigned long *nodeBudget,
    uint64_t deadlineNanoseconds,
    Move *lastMoveOut,
    Position *resultOut)
{
    MoveList legalMoves;

    if (pliesRemaining <= 0) {
        return 0;
    }

    GenerateLegalMoves(position, &legalMoves);

    for (size_t index = 0U; index < legalMoves.count; index += 1U) {
        Position candidate;

        if (CatchupLimitReached(nodeBudget, deadlineNanoseconds)) {
            return 0;
        }

        *nodeBudget -= 1UL;

        ApplyMoveUnchecked(position, legalMoves.moves[index], &candidate);

        if (
            pliesRemaining > 1 &&
            OutOfReach(candidate.board, observedBoard, pliesRemaining - 1)
        ) {
            continue;
        }

        if (BoardsEqual(candidate.board, observedBoard)) {
            /* Only a match at the exact target depth counts. A shallower hit
             * was already found by an earlier iteration of the deepening loop,
             * so reaching here at depth > 1 means the board must still differ
             * one ply from the end. */
            if (pliesRemaining == 1) {
                if (lastMoveOut != NULL) {
                    *lastMoveOut = legalMoves.moves[index];
                }

                if (resultOut != NULL) {
                    *resultOut = candidate;
                }

                return 1;
            }

            continue;
        }

        if (
            SearchMoveSequence(
                &candidate,
                observedBoard,
                pliesRemaining - 1,
                nodeBudget,
                deadlineNanoseconds,
                lastMoveOut,
                resultOut)
        ) {
            return 1;
        }
    }

    return 0;
}

/*
 * Returns the number of plies that were played, 0 if the board is out of reach.
 * pliesOut of 1 is the ordinary case; more means snapshots were skipped.
 */
static int FindMoveSequence(
    const Position *position,
    const char observedBoard[64],
    Move *lastMoveOut,
    Position *resultOut,
    int *pliesOut)
{
    unsigned long budget = CATCHUP_NODE_BUDGET;
    uint64_t started = MonotonicNanoseconds();
    uint64_t deadline;

    if (started == 0U || UINT64_MAX - started < CATCHUP_DEADLINE_NS) {
        return 0;
    }
    deadline = started + CATCHUP_DEADLINE_NS;

    for (int plies = 2; plies <= MAX_CATCHUP_PLIES; plies += 1) {
        if (OutOfReach(position->board, observedBoard, plies)) {
            continue;
        }

        if (CatchupLimitReached(&budget, deadline)) {
            return 0;
        }

        if (
            SearchMoveSequence(
                position,
                observedBoard,
                plies,
                &budget,
                deadline,
                lastMoveOut,
                resultOut)
        ) {
            if (pliesOut != NULL) {
                *pliesOut = plies;
            }

            return 1;
        }
    }

    return 0;
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

/*
 * A bounded recovery can fail simply because the trusted position is now too
 * far behind. Do not adopt that unproven board on its own: its side to move
 * is unknowable. If a later board arrives, however, one legal transition
 * between the two consecutive observations can establish the mover. Accept
 * it only when exactly one candidate side has exactly one matching move.
 *
 * Hidden state remains a best effort here (as for joining a game in progress)
 * and the result is therefore published as inferred. Exact DOM history can
 * still replace it transactionally afterward.
 */
static int InferUniquePendingTransition(
    const char previousBoard[64],
    const char observedBoard[64],
    Move *matchingMoveOut,
    Position *resultOut)
{
    int matchingMoverCount = 0;
    Move matchingMove = {0};
    Position matchingPosition = {0};

    for (int color = (int)COLOR_WHITE;
         color <= (int)COLOR_BLACK;
         color += 1) {
        Position candidate = {0};
        Move candidateMove = {0};
        Position candidateResult = {0};
        size_t matchCount = 0U;

        memcpy(candidate.board, previousBoard, BOARD_SQUARE_COUNT);
        candidate.sideToMove = (Color)color;
        candidate.castlingRights = GuessCastlingRights(previousBoard);
        candidate.enPassantSquare = -1;
        candidate.halfmoveClock = 0U;
        candidate.fullmoveNumber = 1U;

        if (!FindMatchingMove(
                &candidate,
                observedBoard,
                &candidateMove,
                &candidateResult,
                &matchCount)) {
            /* More than one legal move for either candidate is ambiguous,
             * even when the other color has no match. */
            if (matchCount > 0U) {
                return 0;
            }
            continue;
        }

        matchingMoverCount += 1;
        matchingMove = candidateMove;
        matchingPosition = candidateResult;
    }

    if (matchingMoverCount != 1) {
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

static int SquareFromName(const char name[2])
{
    if (name == NULL || name[0] < 'a' || name[0] > 'h' ||
        name[1] < '1' || name[1] > '8') {
        return -1;
    }

    return SquareFromRowFile('8' - name[1], name[0] - 'a');
}

static int MatchUciHistoryMove(
    const Position *position,
    const char *token,
    Move *moveOut)
{
    size_t length;
    int from;
    int to;
    char promotion = '\0';
    MoveList legalMoves;
    size_t matchCount = 0U;
    Move match = {0};

    if (position == NULL || token == NULL || moveOut == NULL) {
        return 0;
    }

    length = strlen(token);
    if (length != 4U && length != 5U) {
        return 0;
    }

    from = SquareFromName(token);
    to = SquareFromName(token + 2);
    if (from < 0 || to < 0) {
        return 0;
    }

    if (length == 5U) {
        promotion = (char)tolower((unsigned char)token[4]);
        if (strchr("qrbn", promotion) == NULL) {
            return 0;
        }
    }

    GenerateLegalMoves(position, &legalMoves);
    for (size_t index = 0U; index < legalMoves.count; index += 1U) {
        Move candidate = legalMoves.moves[index];
        char candidatePromotion = (candidate.flags & MOVE_FLAG_PROMOTION) != 0U
            ? (char)tolower((unsigned char)candidate.promotion)
            : '\0';

        if (candidate.from == from && candidate.to == to &&
            candidatePromotion == promotion) {
            match = candidate;
            matchCount += 1U;
        }
    }

    if (matchCount != 1U) {
        return 0;
    }

    *moveOut = match;
    return 1;
}

static int EndsWith(const char *text, size_t length, const char *suffix)
{
    size_t suffixLength = strlen(suffix);

    return length >= suffixLength &&
        memcmp(text + length - suffixLength, suffix, suffixLength) == 0;
}

/* Chess.com supplies SAN text rather than a FEN in several board variants.
 * Only notation that can be proved against exactly one generated legal move
 * is accepted. Decorations do not affect move identity and are stripped; all
 * chess state still comes from applying the matched legal move. */
static int NormaliseSanToken(
    const char *token,
    char output[MAX_HISTORY_TOKEN_LENGTH + 1U])
{
    size_t offset = 0U;
    size_t length;
    int changed;

    if (token == NULL || output == NULL) {
        return 0;
    }

    for (const unsigned char *cursor = (const unsigned char *)token;
         *cursor != '\0'; cursor += 1) {
        if (isspace(*cursor) != 0) {
            continue;
        }
        if (*cursor < 0x20U || *cursor > 0x7eU ||
            offset >= MAX_HISTORY_TOKEN_LENGTH) {
            return 0;
        }
        output[offset++] = *cursor == '0' || *cursor == 'o'
            ? 'O'
            : (char)*cursor;
    }
    output[offset] = '\0';

    do {
        changed = 0;
        while (offset > 0U &&
               (output[offset - 1U] == '+' || output[offset - 1U] == '#' ||
                output[offset - 1U] == '!' || output[offset - 1U] == '?')) {
            output[--offset] = '\0';
            changed = 1;
        }

        length = offset;
        if (EndsWith(output, length, "e.p.")) {
            offset -= 4U;
            output[offset] = '\0';
            changed = 1;
        } else if (EndsWith(output, length, "ep")) {
            offset -= 2U;
            output[offset] = '\0';
            changed = 1;
        }
    } while (changed);

    return offset > 0U;
}

static int MatchSanHistoryMove(
    const Position *position,
    const char *rawToken,
    Move *moveOut)
{
    char token[MAX_HISTORY_TOKEN_LENGTH + 1U];
    size_t length;
    int castleSide = 0;
    char pieceType = 'P';
    char promotion = '\0';
    int target = -1;
    int capture = 0;
    int fromFile = -1;
    int fromRank = -1;
    size_t prefixStart = 0U;
    size_t prefixEnd;
    MoveList legalMoves;
    Move match = {0};
    size_t matchCount = 0U;

    if (position == NULL || rawToken == NULL || moveOut == NULL ||
        !NormaliseSanToken(rawToken, token)) {
        return 0;
    }

    length = strlen(token);
    if (strcmp(token, "O-O") == 0) {
        castleSide = 1;
    } else if (strcmp(token, "O-O-O") == 0) {
        castleSide = -1;
    } else {
        if (length >= 2U && token[length - 2U] == '=' &&
            strchr("QRBNqrbn", token[length - 1U]) != NULL) {
            promotion = (char)toupper((unsigned char)token[length - 1U]);
            token[length - 2U] = '\0';
            length -= 2U;
        } else if (length >= 3U &&
                   strchr("QRBNqrbn", token[length - 1U]) != NULL &&
                   token[length - 3U] >= 'a' && token[length - 3U] <= 'h' &&
                   token[length - 2U] >= '1' && token[length - 2U] <= '8') {
            promotion = (char)toupper((unsigned char)token[length - 1U]);
            token[length - 1U] = '\0';
            length -= 1U;
        }

        if (length < 2U) {
            return 0;
        }

        target = SquareFromName(token + length - 2U);
        if (target < 0) {
            return 0;
        }
        prefixEnd = length - 2U;

        if (prefixEnd > 0U && strchr("KQRBN", token[0]) != NULL) {
            pieceType = token[0];
            prefixStart = 1U;
        }

        for (size_t index = prefixStart; index < prefixEnd; index += 1U) {
            char character = token[index];

            if (character == 'x') {
                if (capture) {
                    return 0;
                }
                capture = 1;
            } else if (character >= 'a' && character <= 'h') {
                if (fromFile >= 0) {
                    return 0;
                }
                fromFile = character - 'a';
            } else if (character >= '1' && character <= '8') {
                if (fromRank >= 0) {
                    return 0;
                }
                fromRank = character - '0';
            } else {
                return 0;
            }
        }

        if (pieceType == 'P') {
            if ((capture && fromFile < 0) || (!capture && prefixEnd != 0U)) {
                return 0;
            }
        }
    }

    GenerateLegalMoves(position, &legalMoves);
    for (size_t index = 0U; index < legalMoves.count; index += 1U) {
        Move candidate = legalMoves.moves[index];
        char movingPiece = (char)toupper(
            (unsigned char)position->board[candidate.from]);
        char candidatePromotion =
            (candidate.flags & MOVE_FLAG_PROMOTION) != 0U
                ? (char)toupper((unsigned char)candidate.promotion)
                : '\0';
        int candidateCapture =
            (candidate.flags & MOVE_FLAG_CAPTURE) != 0U;

        if (castleSide != 0) {
            int targetFile = castleSide > 0 ? 6 : 2;
            if ((candidate.flags & MOVE_FLAG_CASTLING) == 0U ||
                FileOf(candidate.to) != targetFile) {
                continue;
            }
        } else if (movingPiece != pieceType || candidate.to != target ||
                   candidateCapture != capture ||
                   candidatePromotion != promotion ||
                   (fromFile >= 0 && FileOf(candidate.from) != fromFile) ||
                   (fromRank >= 0 && 8 - RowOf(candidate.from) != fromRank)) {
            continue;
        }

        match = candidate;
        matchCount += 1U;
    }

    if (matchCount != 1U) {
        return 0;
    }

    *moveOut = match;
    return 1;
}

static int ReplayHistory(
    char *historyMoves,
    const char *notation,
    const char *initialFen,
    Position *positionOut,
    char lastMoveOut[6],
    size_t *moveCountOut,
    char *canonicalMovesOut,
    size_t canonicalMovesSize)
{
    Position replay;
    char *cursor;
    size_t moveCount = 0U;

    if (historyMoves == NULL || notation == NULL || positionOut == NULL ||
        lastMoveOut == NULL) {
        return 0;
    }

    if (initialFen != NULL) {
        if (!PositionFromFen(initialFen, &replay)) {
            return 0;
        }
    } else {
        InitializePosition(&replay);
    }
    lastMoveOut[0] = '\0';
    if (canonicalMovesOut != NULL && canonicalMovesSize > 0U) {
        canonicalMovesOut[0] = '\0';
    }

    if (historyMoves[0] == '\0') {
        *positionOut = replay;
        if (moveCountOut != NULL) {
            *moveCountOut = 0U;
        }
        return 1;
    }

    cursor = historyMoves;
    for (;;) {
        char *delimiter = strchr(cursor, '|');
        char *end = delimiter != NULL ? delimiter : cursor + strlen(cursor);
        Move move;
        Position next;

        while (cursor < end && isspace((unsigned char)*cursor) != 0) {
            cursor += 1;
        }
        while (end > cursor && isspace((unsigned char)end[-1]) != 0) {
            end -= 1;
        }

        if (cursor == end || (size_t)(end - cursor) > MAX_HISTORY_TOKEN_LENGTH ||
            moveCount >= MAX_HISTORY_MOVES) {
            return 0;
        }
        *end = '\0';

        if (strcmp(notation, "uci") == 0) {
            if (!MatchUciHistoryMove(&replay, cursor, &move)) {
                return 0;
            }
        } else if (strcmp(notation, "san") == 0) {
            if (!MatchSanHistoryMove(&replay, cursor, &move)) {
                return 0;
            }
        } else {
            return 0;
        }

        ApplyMoveUnchecked(&replay, move, &next);
        replay = next;
        MoveToUci(move, lastMoveOut);
        if (canonicalMovesOut != NULL && canonicalMovesSize > 0U) {
            size_t used = strlen(canonicalMovesOut);
            size_t needed = strlen(lastMoveOut) + (used > 0U ? 1U : 0U);
            if (used + needed + 1U > canonicalMovesSize) {
                return 0;
            }
            if (used > 0U) {
                canonicalMovesOut[used++] = '|';
                canonicalMovesOut[used] = '\0';
            }
            (void)snprintf(canonicalMovesOut + used,
                           canonicalMovesSize - used, "%s", lastMoveOut);
        }
        moveCount += 1U;

        if (delimiter == NULL) {
            break;
        }
        cursor = delimiter + 1;
    }

    *positionOut = replay;
    if (moveCountOut != NULL) {
        *moveCountOut = moveCount;
    }
    return 1;
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

enum {
    HELLO_REJECTED = -1,
    HELLO_NOT_RECEIVED = 0,
    HELLO_ACCEPTED = 1
};

/* Validate the browser/host protocol before launching the overlay or engines.
 * This keeps incompatible components from appearing to work while silently
 * disagreeing about message fields. */
static int HandleHello(
    const char *message,
    FILE *logFile,
    char response[MAX_RESPONSE_LENGTH])
{
    char type[32];
    int protocol;
    int written;

    if (!ExtractJsonStringField(message, "type", type, sizeof(type)) ||
        strcmp(type, "hello") != 0) {
        written = snprintf(
            response,
            MAX_RESPONSE_LENGTH,
            "{\"type\":\"error\",\"ok\":false,"
            "\"reason\":\"hello_required\",\"protocol_version\":%d,"
            "\"host_version\":\"%s\"}",
            CHESSLISTENER_PROTOCOL_VERSION,
            CHESSLISTENER_HOST_VERSION);

        fprintf(logFile, "Rejected message before protocol hello.\n");
        fflush(logFile);
        return written >= 0 && (size_t)written < MAX_RESPONSE_LENGTH
            ? HELLO_NOT_RECEIVED
            : HELLO_REJECTED;
    }

    if (!ExtractJsonIntField(message, "protocol_version", &protocol) ||
        protocol != CHESSLISTENER_PROTOCOL_VERSION) {
        written = snprintf(
            response,
            MAX_RESPONSE_LENGTH,
            "{\"type\":\"hello\",\"ok\":false,"
            "\"reason\":\"incompatible_protocol\","
            "\"protocol_version\":%d,\"host_version\":\"%s\"}",
            CHESSLISTENER_PROTOCOL_VERSION,
            CHESSLISTENER_HOST_VERSION);

        fprintf(
            logFile,
            "Rejected incompatible protocol hello; host requires protocol %d.\n",
            CHESSLISTENER_PROTOCOL_VERSION);
        fflush(logFile);
        return HELLO_REJECTED;
    }

    written = snprintf(
        response,
        MAX_RESPONSE_LENGTH,
        "{\"type\":\"hello\",\"ok\":true,\"protocol_version\":%d,"
        "\"host_version\":\"%s\",\"capabilities\":["
        "\"session_v2\",\"state_override\",\"streaming_analysis\","
        "\"last_move\",\"history_reconciliation\","
        "\"state_revision\",\"state_source\","
        "\"analysis_lab\",\"analysis_target\",\"local_game_review\","
        "\"review_explorer\",\"review_library\"]}",
        CHESSLISTENER_PROTOCOL_VERSION,
        CHESSLISTENER_HOST_VERSION);

    if (written < 0 || (size_t)written >= MAX_RESPONSE_LENGTH) {
        return HELLO_REJECTED;
    }

    fprintf(
        logFile,
        "Protocol %d hello accepted (host %s).\n",
        CHESSLISTENER_PROTOCOL_VERSION,
        CHESSLISTENER_HOST_VERSION);
    fflush(logFile);
    return HELLO_ACCEPTED;
}

static void ResetSessionTracking(SessionState *session)
{
    memset(&session->position, 0, sizeof(session->position));
    session->hasPosition = 0;
    session->lastMove[0] = '\0';
    memset(session->pendingBoard, 0, sizeof(session->pendingBoard));
    session->hasPendingBoard = 0;
    session->pendingIsMismatch = 0;
    session->pendingRecoveryFailed = 0;
    session->pendingSnapshotSequence = 0U;
    session->lastSnapshotSequence = 0U;
    session->hasSnapshotSequence = 0;
    session->lastHistorySequence = 0U;
    session->hasHistorySequence = 0;
    (void)snprintf(
        session->stateSource,
        sizeof(session->stateSource),
        "inferred");
    session->visuallyFlipped = 0;
    session->hasOrientation = 0;
    memset(&session->explorer, 0, sizeof(session->explorer));
}

static int RequireMatchingSession(
    const char *message,
    const SessionState *session,
    char response[MAX_RESPONSE_LENGTH])
{
    char sessionId[MAX_SESSION_ID_LENGTH + 1U];

    if (!session->active) {
        (void)WriteResponse(response, 0, "session_required", NULL, NULL);
        return 0;
    }

    if (!ExtractJsonStringField(
            message, "session_id", sessionId, sizeof(sessionId)) ||
        !IsValidSessionId(sessionId)) {
        (void)WriteResponse(response, 0, "invalid_session_id", NULL, NULL);
        return 0;
    }

    if (strcmp(sessionId, session->id) != 0) {
        (void)WriteResponse(response, 0, "session_mismatch", NULL, NULL);
        return 0;
    }

    return 1;
}

static void HandleSessionStart(
    const char *message,
    FILE *logFile,
    SessionState *session,
    char response[MAX_RESPONSE_LENGTH])
{
    char sessionId[MAX_SESSION_ID_LENGTH + 1U];
    char label[MAX_SESSION_LABEL_LENGTH + 1U];

    if (!ExtractJsonStringField(
            message, "session_id", sessionId, sizeof(sessionId)) ||
        !IsValidSessionId(sessionId)) {
        (void)WriteResponse(response, 0, "invalid_session_id", NULL, NULL);
        return;
    }

    if (!ExtractJsonStringField(message, "game_key", label, sizeof(label)) &&
        !ExtractJsonStringField(message, "url", label, sizeof(label))) {
        (void)snprintf(label, sizeof(label), "%s", sessionId);
    }

    if (session->active) {
        if (session->explorer.active) {
            AnalysisExploreDestroy(session->explorer.id, "session_replaced");
        }
        AnalysisSessionEnd("replaced", "*");
    }

    memset(session, 0, sizeof(*session));
    session->active = 1;
    (void)snprintf(session->id, sizeof(session->id), "%s", sessionId);
    ResetSessionTracking(session);
    AnalysisSessionStart(session->id, label);

    fprintf(logFile, "\n=== Session started: %s (%s) ===\n", session->id, label);
    fflush(logFile);
    (void)WriteResponse(response, 1, "session_started", NULL, NULL);
}

static void HandlePositionSnapshot(
    const char *message,
    FILE *logFile,
    SessionState *session,
    char response[MAX_RESPONSE_LENGTH])
{
    int visuallyFlipped;
    int force = 0;
    int recovery = 0;
    uint64_t snapshotSequence;
    char boardString[BOARD_SQUARE_COUNT + 1U];

    if (!RequireMatchingSession(message, session, response)) {
        return;
    }

    if (!ExtractJsonUint64Field(
            message, "snapshot_seq", &snapshotSequence)) {
        (void)WriteResponse(response, 0, "invalid_snapshot_seq", NULL, NULL);
        return;
    }

    if (session->hasSnapshotSequence &&
        snapshotSequence <= session->lastSnapshotSequence) {
        (void)WriteResponse(response, 0, "stale_snapshot", NULL, NULL);
        return;
    }

    /* Even a malformed snapshot consumes its sequence number. Otherwise a
     * delayed, older-but-well-formed frame could roll the session backwards. */
    session->lastSnapshotSequence = snapshotSequence;
    session->hasSnapshotSequence = 1;

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

    if (!ExtractJsonBoolField(
            message, "visually_flipped", &visuallyFlipped)) {
        (void)WriteResponse(response, 0, "invalid_orientation", NULL, NULL);
        return;
    }

    if (FindJsonFieldValue(message, "force") != NULL &&
        !ExtractJsonBoolField(message, "force", &force)) {
        (void)WriteResponse(response, 0, "invalid_force", NULL, NULL);
        return;
    }

    if (FindJsonFieldValue(message, "recovery") != NULL &&
        !ExtractJsonBoolField(message, "recovery", &recovery)) {
        (void)WriteResponse(response, 0, "invalid_recovery", NULL, NULL);
        return;
    }

    size_t pieceCount = 0U;

    if (!ValidateBoardString(boardString, &pieceCount)) {
        fprintf(logFile, "Rejected invalid board: %s\n", boardString);
        fflush(logFile);
        (void)WriteResponse(response, 0, "invalid_board", NULL, NULL);
        return;
    }

    int orientationChanged =
        !session->hasOrientation ||
        session->visuallyFlipped != visuallyFlipped;
    session->visuallyFlipped = visuallyFlipped;
    session->hasOrientation = 1;

    if (orientationChanged) {
        AnalysisUpdateOrientation(visuallyFlipped);
    }

    if (
        session->hasPosition &&
        memcmp(
            session->position.board,
            boardString,
            BOARD_SQUARE_COUNT) == 0
    ) {
        if (session->pendingIsMismatch) {
            memset(session->pendingBoard, 0, sizeof(session->pendingBoard));
            session->hasPendingBoard = 0;
            session->pendingIsMismatch = 0;
            session->pendingRecoveryFailed = 0;
            session->pendingSnapshotSequence = 0U;
            AnalysisSetSynchronising(0, NULL);
        }

        if (force) {
            char fen[MAX_FEN_LENGTH];

            if (!PositionToFen(&session->position, fen)) {
                (void)WriteResponse(response, 0, "fen_error", NULL, NULL);
                return;
            }

            AnalysisPublish(
                fen,
                visuallyFlipped,
                session->lastMove[0] != '\0' ? session->lastMove : NULL,
                session->stateSource);
            fprintf(logFile, "Refreshed forced duplicate position.\n");
            fflush(logFile);
            (void)WriteResponse(
                response, 1, "position_refreshed", fen, NULL);
            return;
        }

        fprintf(
            logFile,
            orientationChanged
                ? "Updated orientation for duplicate position.\n"
                : "Ignored duplicate position.\n");
        fflush(logFile);
        (void)WriteResponse(
            response,
            orientationChanged,
            orientationChanged ? "orientation_updated" : "duplicate",
            NULL,
            NULL);
        return;
    }

    if (!session->hasPosition &&
        memcmp(boardString, INITIAL_BOARD, BOARD_SQUARE_COUNT) == 0) {
        char fen[MAX_FEN_LENGTH];

        InitializePosition(&session->position);
        session->hasPosition = 1;
        session->lastMove[0] = '\0';
        (void)snprintf(
            session->stateSource,
            sizeof(session->stateSource),
            "exact");

        if (!PositionToFen(&session->position, fen)) {
            (void)WriteResponse(response, 0, "fen_error", NULL, NULL);
            return;
        }

        session->hasPendingBoard = 0;
        session->pendingIsMismatch = 0;
        session->pendingRecoveryFailed = 0;
        session->pendingSnapshotSequence = 0U;
        AnalysisSetSynchronising(0, NULL);

        fprintf(logFile, "\n=== New standard game ===\n");
        LogBoard(logFile, session->position.board, pieceCount);
        fprintf(logFile, "FEN: %s\n", fen);
        fflush(logFile);

        AnalysisPublish(fen, visuallyFlipped, NULL, session->stateSource);

        (void)WriteResponse(response, 1, "game_started", fen, NULL);
        return;
    }

    if (!session->hasPosition) {
        Color movedColor;

        /*
         * Observation can start mid-game, so refusing everything but the
         * initial position would mean never locking on. Adopt instead,
         * once a second frame tells us whose turn it is.
         */
        if (
            session->hasPendingBoard &&
            InferMovedColor(session->pendingBoard, boardString, &movedColor)
        ) {
            char fen[MAX_FEN_LENGTH];

            AdoptPosition(
                &session->position,
                session->pendingBoard,
                boardString,
                movedColor);
            session->hasPosition = 1;
            session->hasPendingBoard = 0;
            session->pendingIsMismatch = 0;
            session->pendingRecoveryFailed = 0;
            session->pendingSnapshotSequence = 0U;
            session->lastMove[0] = '\0';
            (void)snprintf(
                session->stateSource,
                sizeof(session->stateSource),
                "inferred");

            if (!PositionToFen(&session->position, fen)) {
                (void)WriteResponse(response, 0, "fen_error", NULL, NULL);
                return;
            }

            fprintf(logFile, "\n=== Adopted game in progress ===\n");
            LogBoard(logFile, session->position.board, pieceCount);
            fprintf(
                logFile,
                "FEN: %s (castling rights are a best guess)\n",
                fen
            );
            fflush(logFile);

            AnalysisSetSynchronising(0, NULL);
            AnalysisPublish(
                fen, visuallyFlipped, NULL, session->stateSource);

            (void)WriteResponse(response, 1, "game_adopted", fen, NULL);
            return;
        }

        memcpy(session->pendingBoard, boardString, BOARD_SQUARE_COUNT);
        session->hasPendingBoard = 1;
        session->pendingIsMismatch = 0;
        session->pendingRecoveryFailed = 0;
        session->pendingSnapshotSequence = snapshotSequence;

        fprintf(
            logFile,
            "Holding first frame of a game in progress; "
            "waiting for one more move to establish the side to move.\n"
        );
        LogBoard(logFile, boardString, pieceCount);
        fflush(logFile);
        (void)WriteResponse(
            response, 0, "waiting_for_second_frame", NULL, NULL);
        return;
    }

    if (session->pendingIsMismatch && !recovery) {
        /* Once continuity is broken, a later layout that happens to be one
         * legal ply from the old trusted position is not proof that every
         * omitted move was undone. After an explicit bounded recovery has
         * failed, one uniquely legal move between consecutive observed boards
         * is enough to resume inferred tracking without adopting an idle or
         * ambiguous board. Exact history remains the preferred authority. */
        Move inferredMove;
        Position inferredResult;

        if (session->pendingRecoveryFailed &&
            session->hasPendingBoard &&
            InferUniquePendingTransition(
                session->pendingBoard,
                boardString,
                &inferredMove,
                &inferredResult)) {
            char fen[MAX_FEN_LENGTH];
            char uci[6];

            if (!PositionToFen(&inferredResult, fen)) {
                (void)WriteResponse(response, 0, "fen_error", NULL, NULL);
                return;
            }

            MoveToUci(inferredMove, uci);
            session->position = inferredResult;
            (void)snprintf(
                session->lastMove, sizeof(session->lastMove), "%s", uci);
            (void)snprintf(
                session->stateSource,
                sizeof(session->stateSource),
                "inferred");
            memset(session->pendingBoard, 0, sizeof(session->pendingBoard));
            session->hasPendingBoard = 0;
            session->pendingIsMismatch = 0;
            session->pendingRecoveryFailed = 0;
            session->pendingSnapshotSequence = 0U;
            AnalysisSetSynchronising(0, NULL);

            fprintf(
                logFile,
                "\nConsecutive-board fallback recovered inferred move: %s\n",
                uci);
            LogBoard(logFile, session->position.board, pieceCount);
            fprintf(logFile, "FEN: %s\n", fen);
            fflush(logFile);

            AnalysisPublish(
                fen, visuallyFlipped, uci, session->stateSource);
            (void)WriteResponse(response, 1, "move_recorded", fen, uci);
            return;
        }

        memcpy(session->pendingBoard, boardString, BOARD_SQUARE_COUNT);
        session->hasPendingBoard = 1;
        session->pendingSnapshotSequence = snapshotSequence;
        AnalysisSetSynchronising(1, "Synchronising\342\200\246");
        (void)WriteResponse(response, 0, "synchronising", NULL, NULL);
        return;
    }

    Move matchingMove;
    Position result;
    size_t matchCount = 0U;
    int plies = 1;
    int recoveredFromMismatch = session->pendingIsMismatch;

    if (!FindMatchingMove(
        &session->position,
        boardString,
        &matchingMove,
        &result,
        &matchCount)) {
        /* Ordinary board capture stops here. It must never pay for a tree
         * search: keep showing the last trustworthy evaluation while the DOM
         * history reconciler gets a chance to provide exact state. */
        if (!recovery) {
            fprintf(
                logFile,
                "Transition needs reconciliation: %zu legal one-ply moves "
                "matched. Holding trusted state.\n",
                matchCount);
            LogBoardDifference(
                logFile, session->position.board, boardString);
            LogBoard(logFile, boardString, pieceCount);
            fflush(logFile);

            memcpy(session->pendingBoard, boardString, BOARD_SQUARE_COUNT);
            session->hasPendingBoard = 1;
            session->pendingIsMismatch = 1;
            session->pendingRecoveryFailed = 0;
            session->pendingSnapshotSequence = snapshotSequence;
            AnalysisSetSynchronising(1, "Synchronising\342\200\246");

            (void)WriteResponse(response, 0, "synchronising", NULL, NULL);
            return;
        }

        /* Only the explicitly delayed recovery message may use bounded DFS.
         * One node budget and one monotonic deadline are shared by every
         * iterative-deepening depth. */
        memcpy(session->pendingBoard, boardString, BOARD_SQUARE_COUNT);
        session->hasPendingBoard = 1;
        session->pendingIsMismatch = 1;
        session->pendingSnapshotSequence = snapshotSequence;

        if (matchCount == 0U &&
            FindMoveSequence(
                &session->position,
                boardString,
                &matchingMove,
                &result,
                &plies)) {
            fprintf(
                logFile,
                "\nDelayed recovery caught up %d plies.\n",
                plies
            );
            fflush(logFile);
        } else {
            fprintf(
                logFile,
                "Delayed recovery did not prove a path: %zu single moves "
                "matched, bounded search through %d plies failed.\n",
                matchCount,
                MAX_CATCHUP_PLIES
            );
            LogBoardDifference(
                logFile, session->position.board, boardString);
            LogBoard(logFile, boardString, pieceCount);
            fflush(logFile);

            AnalysisSetSynchronising(1, "Synchronising\342\200\246");
            session->pendingRecoveryFailed = 1;
            (void)WriteResponse(
                response, 0, "recovery_pending", NULL, NULL);
            return;
        }
    }

    session->position = result;

    if (recovery && recoveredFromMismatch) {
        (void)snprintf(
            session->stateSource,
            sizeof(session->stateSource),
            "inferred");
    }
    memset(session->pendingBoard, 0, sizeof(session->pendingBoard));
    session->hasPendingBoard = 0;
    session->pendingIsMismatch = 0;
    session->pendingRecoveryFailed = 0;
    session->pendingSnapshotSequence = 0U;
    AnalysisSetSynchronising(0, NULL);

    char fen[MAX_FEN_LENGTH];
    char uci[6];
    MoveToUci(matchingMove, uci);
    (void)snprintf(session->lastMove, sizeof(session->lastMove), "%s", uci);

    if (!PositionToFen(&session->position, fen)) {
        (void)WriteResponse(response, 0, "fen_error", NULL, NULL);
        return;
    }

    fprintf(logFile, "\nMove: %s\n", uci);
    LogBoard(logFile, session->position.board, pieceCount);
    fprintf(logFile, "FEN: %s\n", fen);
    fflush(logFile);

    AnalysisPublish(fen, visuallyFlipped, uci, session->stateSource);

    (void)WriteResponse(response, 1, "move_recorded", fen, uci);
}

static void HandleHistoryReconcile(
    const char *message,
    FILE *logFile,
    SessionState *session,
    char response[MAX_RESPONSE_LENGTH])
{
    uint64_t historySequence;
    uint64_t snapshotSequence;
    int historyComplete;
    char displayedBoard[BOARD_SQUARE_COUNT + 1U];
    char notation[8];
    char historyMoves[MAX_HISTORY_TEXT_LENGTH + 1U];
    char initialFen[MAX_FEN_LENGTH];
    const char *initialFenValue = NULL;
    Position replay;
    char replayFen[MAX_FEN_LENGTH];
    char currentFen[MAX_FEN_LENGTH];
    char lastMove[6];
    char canonicalMoves[MAX_HISTORY_TEXT_LENGTH + 1U];
    char canonicalInitialFen[MAX_FEN_LENGTH];
    char gameResult[16] = "*";
    size_t moveCount = 0U;
    int associated = 0;
    int sameFen = 0;

    if (!RequireMatchingSession(message, session, response)) {
        return;
    }

    if (!ExtractJsonUint64Field(message, "history_seq", &historySequence)) {
        (void)WriteResponse(response, 0, "invalid_history_seq", NULL, NULL);
        return;
    }
    if (session->hasHistorySequence &&
        historySequence <= session->lastHistorySequence) {
        (void)WriteResponse(response, 0, "stale_history", NULL, NULL);
        return;
    }

    if (!ExtractJsonUint64Field(message, "snapshot_seq", &snapshotSequence) ||
        !ExtractJsonBoolField(
            message, "history_complete", &historyComplete) ||
        !historyComplete ||
        !ExtractJsonStringField(
            message,
            "displayed_board",
            displayedBoard,
            sizeof(displayedBoard)) ||
        !ValidateBoardString(displayedBoard, NULL) ||
        !ExtractJsonStringField(
            message, "history_notation", notation, sizeof(notation)) ||
        (strcmp(notation, "uci") != 0 && strcmp(notation, "san") != 0) ||
        !ExtractJsonStringField(
            message,
            "history_moves",
            historyMoves,
            sizeof(historyMoves))) {
        (void)WriteResponse(response, 0, "invalid_history", NULL, NULL);
        return;
    }

    if (FindJsonFieldValue(message, "initial_fen") != NULL) {
        if (!ExtractJsonStringField(
                message, "initial_fen", initialFen, sizeof(initialFen))) {
            (void)WriteResponse(
                response, 0, "invalid_initial_fen", NULL, NULL);
            return;
        }
        initialFenValue = initialFen;
    }
    if (FindJsonFieldValue(message, "game_result") != NULL) {
        if (!ExtractJsonStringField(
                message, "game_result", gameResult, sizeof(gameResult)) ||
            (strcmp(gameResult, "*") != 0 &&
             strcmp(gameResult, "1-0") != 0 &&
             strcmp(gameResult, "0-1") != 0 &&
             strcmp(gameResult, "1/2-1/2") != 0)) {
            (void)WriteResponse(response, 0, "invalid_game_result", NULL, NULL);
            return;
        }
    }

    /* A history result is useful only for the exact DOM snapshot it was
     * captured beside. Never let a slow replay roll a newer board backward. */
    if (!session->hasSnapshotSequence ||
        snapshotSequence != session->lastSnapshotSequence) {
        (void)WriteResponse(
            response, 0, "history_snapshot_mismatch", NULL, NULL);
        return;
    }

    if (session->hasPosition &&
        BoardsEqual(session->position.board, displayedBoard)) {
        associated = 1;
    }
    if (session->hasPendingBoard &&
        session->pendingSnapshotSequence == snapshotSequence &&
        BoardsEqual(session->pendingBoard, displayedBoard)) {
        associated = 1;
    }
    if (!associated) {
        (void)WriteResponse(
            response, 0, "history_board_mismatch", NULL, NULL);
        return;
    }

    /* From here onward replay is transactional: no session or overlay state
     * changes unless every token is legal and the final pieces match. */
    if (!ReplayHistory(
            historyMoves,
            notation,
            initialFenValue,
            &replay,
            lastMove,
            &moveCount,
            canonicalMoves,
            sizeof(canonicalMoves)) ||
        !BoardsEqual(replay.board, displayedBoard) ||
        !PositionToFen(&replay, replayFen)) {
        fprintf(
            logFile,
            "Rejected history %llu: illegal/incomplete replay or final "
            "board mismatch.\n",
            (unsigned long long)historySequence);
        fflush(logFile);
        (void)WriteResponse(response, 0, "history_replay_failed", NULL, NULL);
        return;
    }

    session->lastHistorySequence = historySequence;
    session->hasHistorySequence = 1;

    if (session->hasPosition &&
        PositionToFen(&session->position, currentFen) &&
        strcmp(currentFen, replayFen) == 0) {
        sameFen = 1;
    }

    session->position = replay;
    session->hasPosition = 1;
    (void)snprintf(session->lastMove, sizeof(session->lastMove), "%s", lastMove);
    (void)snprintf(
        session->stateSource,
        sizeof(session->stateSource),
        "exact");
    memset(session->pendingBoard, 0, sizeof(session->pendingBoard));
    session->hasPendingBoard = 0;
    session->pendingIsMismatch = 0;
    session->pendingRecoveryFailed = 0;
    session->pendingSnapshotSequence = 0U;
    AnalysisSetSynchronising(0, NULL);

    if (initialFenValue != NULL) {
        (void)snprintf(canonicalInitialFen, sizeof(canonicalInitialFen),
                       "%s", initialFenValue);
    } else {
        Position initialPosition;
        InitializePosition(&initialPosition);
        if (!PositionToFen(&initialPosition, canonicalInitialFen)) {
            (void)snprintf(canonicalInitialFen, sizeof(canonicalInitialFen),
                           "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/"
                           "RNBQKBNR w KQkq - 0 1");
        }
    }
    AnalysisUpdateGameRecord(
        canonicalInitialFen, canonicalMoves, moveCount, gameResult);

    if (sameFen) {
        /* Provenance changed, but chess state did not. Preserve the running
         * engine revision and its accumulated depth. */
        AnalysisUpdateStateSource(session->stateSource);
        fprintf(
            logFile,
            "History %llu confirmed exact state (%zu plies) without "
            "restarting analysis.\n",
            (unsigned long long)historySequence,
            moveCount);
        fflush(logFile);
        (void)WriteResponse(
            response, 1, "history_confirmed", replayFen, NULL);
        return;
    }

    fprintf(
        logFile,
        "History %llu reconciled exact state (%zu plies): %s\n",
        (unsigned long long)historySequence,
        moveCount,
        replayFen);
    fflush(logFile);
    AnalysisPublish(
        replayFen,
        session->hasOrientation ? session->visuallyFlipped : 0,
        lastMove[0] != '\0' ? lastMove : NULL,
        session->stateSource);
    (void)WriteResponse(
        response,
        1,
        "history_reconciled",
        replayFen,
        lastMove[0] != '\0' ? lastMove : NULL);
}

static uint64_t AllocateExplorerBranchId(void)
{
    uint64_t id = nextExplorerBranchId;

    nextExplorerBranchId += 1U;
    if (nextExplorerBranchId == 0U ||
        nextExplorerBranchId > UINT64_C(9007199254740991)) {
        nextExplorerBranchId = 1U;
    }
    return id;
}

static int ParseUnsignedToken(const char **cursorInOut, uint64_t maximum,
                              int allowZero, uint64_t *valueOut,
                              int finalToken)
{
    const char *cursor;
    char *end;
    unsigned long long value;

    if (cursorInOut == NULL || *cursorInOut == NULL || valueOut == NULL) {
        return 0;
    }
    cursor = *cursorInOut;
    if (*cursor == '\0' || isdigit((unsigned char)*cursor) == 0) {
        return 0;
    }

    errno = 0;
    value = strtoull(cursor, &end, 10);
    if (end == cursor || errno == ERANGE || (!allowZero && value == 0ULL) ||
        value > maximum) {
        return 0;
    }
    if (finalToken) {
        if (*end != '\0') return 0;
    } else {
        if (*end != ' ' || end[1] == '\0') return 0;
        end += 1;
    }
    *cursorInOut = end;
    *valueOut = (uint64_t)value;
    return 1;
}

static int ParseExplorerBranchNode(const char *payload, uint64_t *branchId,
                                   unsigned int *nodeId)
{
    const char *cursor = payload;
    uint64_t node;

    return ParseUnsignedToken(
               &cursor, UINT64_C(9007199254740991), 0, branchId, 0) &&
        ParseUnsignedToken(
               &cursor, MAX_EXPLORER_NODES - 1U, 1, &node, 1) &&
        ((*nodeId = (unsigned int)node), 1);
}

static int ParseExplorerMovePayload(const char *payload, uint64_t *branchId,
                                    unsigned int *nodeId, char uci[6])
{
    const char *cursor = payload;
    uint64_t node;
    size_t length;

    if (!ParseUnsignedToken(
            &cursor, UINT64_C(9007199254740991), 0, branchId, 0) ||
        !ParseUnsignedToken(
            &cursor, MAX_EXPLORER_NODES - 1U, 1, &node, 0)) {
        return 0;
    }
    length = strlen(cursor);
    if ((length != 4U && length != 5U) || length >= 6U) {
        return 0;
    }
    memcpy(uci, cursor, length + 1U);
    *nodeId = (unsigned int)node;
    return 1;
}

static int WriteExploreResponse(char response[MAX_RESPONSE_LENGTH],
                                const char *reason, uint64_t branchId,
                                unsigned int nodeId, const char *fen,
                                const char *lastMove)
{
    int written = snprintf(
        response, MAX_RESPONSE_LENGTH,
        "{\"ok\":true,\"accepted\":true,\"reason\":\"%s\","
        "\"branch_id\":%" PRIu64 ",\"node_id\":%u%s%s%s%s%s}",
        reason, branchId, nodeId,
        fen != NULL ? ",\"fen\":\"" : "",
        fen != NULL ? fen : "",
        fen != NULL ? "\"" : "",
        lastMove != NULL && *lastMove != '\0' ? ",\"last\":\"" : "",
        lastMove != NULL && *lastMove != '\0' ? lastMove : "");

    if (written < 0 || (size_t)written >= MAX_RESPONSE_LENGTH) {
        return 0;
    }
    if (lastMove != NULL && *lastMove != '\0') {
        size_t length = (size_t)written;
        if (length < 1U || response[length - 1U] != '}' ||
            length + 1U >= MAX_RESPONSE_LENGTH) {
            return 0;
        }
        response[length - 1U] = '\"';
        response[length] = '}';
        response[length + 1U] = '\0';
    }
    return 1;
}

static void RejectExplorer(char response[MAX_RESPONSE_LENGTH],
                           const char *action, const char *reason,
                           const char *text, uint64_t branchId,
                           unsigned int nodeId)
{
    AnalysisReportExploreRejected(
        action, reason, text, (unsigned long long)branchId, nodeId);
    (void)WriteResponse(response, 0, reason, NULL, NULL);
}

static int ExplorerNodeFen(const ExplorerBranch *branch, unsigned int nodeId,
                           char fen[MAX_FEN_LENGTH])
{
    return branch != NULL && nodeId < branch->nodeCount &&
        PositionToFen(&branch->nodes[nodeId].position, fen);
}

static void HandleExploreStart(const char *message, FILE *logFile,
                               SessionState *session,
                               char response[MAX_RESPONSE_LENGTH])
{
    char payload[MAX_FEN_LENGTH + MAX_EXPLORER_START_PLIES * 6U + 2U];
    char currentFen[MAX_FEN_LENGTH];
    char finalFen[MAX_FEN_LENGTH];
    char *path;
    Position base;
    ExplorerBranch candidate;
    unsigned int parent = 0U;

    if (!session->hasPosition ||
        !ExtractJsonStringField(message, "payload", payload, sizeof(payload))) {
        RejectExplorer(response, "start", "no_live_position",
                       "A trustworthy live position is required.", 0U, 0U);
        return;
    }

    path = strchr(payload, '|');
    if (path != NULL) {
        *path = '\0';
        path += 1;
        if (strchr(path, '|') != NULL) {
            RejectExplorer(response, "start", "invalid_path",
                           "The requested line is malformed.", 0U, 0U);
            return;
        }
    }

    if (!PositionFromFen(payload, &base) ||
        !PositionToFen(&base, finalFen) || strcmp(payload, finalFen) != 0) {
        RejectExplorer(response, "start", "invalid_fen",
                       "The analysis position is not a valid canonical FEN.",
                       0U, 0U);
        return;
    }
    if (!PositionToFen(&session->position, currentFen) ||
        strcmp(finalFen, currentFen) != 0) {
        RejectExplorer(response, "start", "stale_base",
                       "The live position changed; return Live and try again.",
                       0U, 0U);
        return;
    }

    memset(&candidate, 0, sizeof(candidate));
    candidate.active = 1;
    candidate.targeting = 1;
    candidate.nodeCount = 1U;
    candidate.nodes[0].position = base;
    snprintf(candidate.baseSource, sizeof(candidate.baseSource), "%s",
             session->stateSource);

    if (path != NULL && *path != '\0') {
        char *cursor = path;
        unsigned int plyCount = 0U;

        while (*cursor != '\0') {
            char *comma = strchr(cursor, ',');
            char token[6];
            size_t length = comma != NULL
                ? (size_t)(comma - cursor) : strlen(cursor);
            Move move;
            Position next;

            if (++plyCount > MAX_EXPLORER_START_PLIES ||
                (length != 4U && length != 5U) || length >= sizeof(token)) {
                RejectExplorer(response, "start", "invalid_path",
                               "The requested line is too long or malformed.",
                               0U, 0U);
                return;
            }
            memcpy(token, cursor, length);
            token[length] = '\0';
            if (!MatchUciHistoryMove(
                    &candidate.nodes[parent].position, token, &move)) {
                RejectExplorer(response, "start", "illegal_move",
                               "The requested line contains an illegal move.",
                               0U, 0U);
                return;
            }
            ApplyMoveUnchecked(&candidate.nodes[parent].position, move, &next);
            candidate.nodes[candidate.nodeCount].position = next;
            candidate.nodes[candidate.nodeCount].parent = parent;
            MoveToUci(move, candidate.nodes[candidate.nodeCount].uci);
            parent = candidate.nodeCount;
            candidate.nodeCount += 1U;
            if (comma == NULL) break;
            cursor = comma + 1;
            if (*cursor == '\0') {
                RejectExplorer(response, "start", "invalid_path",
                               "The requested line is malformed.", 0U, 0U);
                return;
            }
        }
    } else if (path != NULL && *path == '\0') {
        RejectExplorer(response, "start", "invalid_path",
                       "The requested line is empty.", 0U, 0U);
        return;
    }

    candidate.id = AllocateExplorerBranchId();
    candidate.selectedNode = parent;
    if (!ExplorerNodeFen(&candidate, parent, finalFen)) {
        RejectExplorer(response, "start", "invalid_position",
                       "The branch position could not be encoded.", 0U, 0U);
        return;
    }

    if (session->explorer.active) {
        AnalysisExploreDestroy(session->explorer.id, "replaced");
    }
    session->explorer = candidate;
    AnalysisExploreStart(
        candidate.id, parent, finalFen,
        session->hasOrientation ? session->visuallyFlipped : 0,
        parent != 0U ? candidate.nodes[parent].uci : NULL,
        candidate.baseSource);
    fprintf(logFile, "Explorer branch %" PRIu64 " started at node %u.\n",
            candidate.id, parent);
    fflush(logFile);
    (void)WriteExploreResponse(
        response, "explore_started", candidate.id, parent, finalFen,
        parent != 0U ? candidate.nodes[parent].uci : NULL);
}

static int ValidateExplorerSelection(SessionState *session, uint64_t branchId,
                                     unsigned int nodeId,
                                     char response[MAX_RESPONSE_LENGTH],
                                     const char *action)
{
    if (!session->explorer.active || session->explorer.id != branchId) {
        RejectExplorer(response, action, "stale_branch",
                       "That analysis branch is no longer active.",
                       branchId, nodeId);
        return 0;
    }
    if (nodeId >= session->explorer.nodeCount) {
        RejectExplorer(response, action, "unknown_node",
                       "That analysis position no longer exists.",
                       branchId, nodeId);
        return 0;
    }
    return 1;
}

static void HandleExploreMove(const char *message, FILE *logFile,
                              SessionState *session,
                              char response[MAX_RESPONSE_LENGTH])
{
    char payload[96];
    char requested[6];
    char canonical[6];
    char fen[MAX_FEN_LENGTH];
    uint64_t branchId = 0U;
    unsigned int parent = 0U;
    unsigned int selected = 0U;
    Move move;
    Position next;
    ExplorerBranch *branch = &session->explorer;

    if (!ExtractJsonStringField(message, "payload", payload, sizeof(payload)) ||
        !ParseExplorerMovePayload(payload, &branchId, &parent, requested)) {
        RejectExplorer(response, "move", "invalid_payload",
                       "Expected branch, node and a UCI move.", 0U, 0U);
        return;
    }
    if (!ValidateExplorerSelection(
            session, branchId, parent, response, "move")) return;
    if (!MatchUciHistoryMove(&branch->nodes[parent].position, requested, &move)) {
        RejectExplorer(response, "move", "illegal_move",
                       "That move is not legal in this position.",
                       branchId, parent);
        return;
    }
    MoveToUci(move, canonical);
    for (unsigned int index = 1U; index < branch->nodeCount; index += 1U) {
        if (branch->nodes[index].parent == parent &&
            strcmp(branch->nodes[index].uci, canonical) == 0) {
            selected = index;
            break;
        }
    }
    if (selected == 0U) {
        if (branch->nodeCount >= MAX_EXPLORER_NODES) {
            RejectExplorer(response, "move", "branch_full",
                           "This branch has reached its 256-position limit.",
                           branchId, parent);
            return;
        }
        ApplyMoveUnchecked(&branch->nodes[parent].position, move, &next);
        selected = branch->nodeCount++;
        branch->nodes[selected].position = next;
        branch->nodes[selected].parent = parent;
        snprintf(branch->nodes[selected].uci,
                 sizeof(branch->nodes[selected].uci), "%s", canonical);
    }
    branch->selectedNode = selected;
    branch->targeting = 1;
    (void)ExplorerNodeFen(branch, selected, fen);
    AnalysisExploreSelect(
        branchId, selected, fen,
        session->hasOrientation ? session->visuallyFlipped : 0,
        branch->nodes[selected].uci, branch->baseSource, "move");
    fprintf(logFile, "Explorer branch %" PRIu64 " selected node %u via %s.\n",
            branchId, selected, canonical);
    fflush(logFile);
    (void)WriteExploreResponse(response, "explore_move_applied", branchId,
                               selected, fen, canonical);
}

static void HandleExploreSelect(const char *message, SessionState *session,
                                char response[MAX_RESPONSE_LENGTH],
                                const char *action)
{
    char payload[80];
    char fen[MAX_FEN_LENGTH];
    uint64_t branchId = 0U;
    unsigned int nodeId = 0U;
    ExplorerBranch *branch = &session->explorer;

    if (!ExtractJsonStringField(message, "payload", payload, sizeof(payload)) ||
        !ParseExplorerBranchNode(payload, &branchId, &nodeId)) {
        RejectExplorer(response, action, "invalid_payload",
                       "Expected a branch and node number.", 0U, 0U);
        return;
    }
    if (!ValidateExplorerSelection(
            session, branchId, nodeId, response, action)) return;
    (void)ExplorerNodeFen(branch, nodeId, fen);
    branch->selectedNode = nodeId;
    branch->targeting = 1;
    AnalysisExploreSelect(
        branchId, nodeId, fen,
        session->hasOrientation ? session->visuallyFlipped : 0,
        nodeId != 0U ? branch->nodes[nodeId].uci : NULL,
        branch->baseSource, action);
    (void)WriteExploreResponse(
        response,
        strcmp(action, "resume") == 0
            ? "explore_resumed" : "explore_position_selected",
        branchId, nodeId, fen,
        nodeId != 0U ? branch->nodes[nodeId].uci : NULL);
}

static void HandleExploreLive(const char *message, SessionState *session,
                              char response[MAX_RESPONSE_LENGTH])
{
    char payload[48];
    const char *cursor;
    uint64_t branchId = 0U;

    if (!ExtractJsonStringField(message, "payload", payload, sizeof(payload))) {
        RejectExplorer(response, "live", "invalid_payload",
                       "Expected an analysis branch number.", 0U, 0U);
        return;
    }
    cursor = payload;
    if (!ParseUnsignedToken(
            &cursor, UINT64_C(9007199254740991), 0, &branchId, 1) ||
        !ValidateExplorerSelection(
            session, branchId, session->explorer.selectedNode,
            response, "live")) return;

    session->explorer.targeting = 0;
    AnalysisExploreLive(branchId);
    (void)WriteExploreResponse(
        response, "explore_live", branchId,
        session->explorer.selectedNode, NULL, NULL);
}

static void HandleSessionCommand(
    const char *message,
    FILE *logFile,
    SessionState *session,
    char response[MAX_RESPONSE_LENGTH])
{
    char command[32];

    if (!RequireMatchingSession(message, session, response)) {
        return;
    }

    if (!ExtractJsonStringField(
            message, "command", command, sizeof(command))) {
        (void)WriteResponse(response, 0, "invalid_command", NULL, NULL);
        return;
    }

    if (strcmp(command, "explore_start") == 0) {
        HandleExploreStart(message, logFile, session, response);
        return;
    }
    if (strcmp(command, "explore_move") == 0) {
        HandleExploreMove(message, logFile, session, response);
        return;
    }
    if (strcmp(command, "explore_goto") == 0) {
        HandleExploreSelect(message, session, response, "goto");
        return;
    }
    if (strcmp(command, "explore_live") == 0) {
        HandleExploreLive(message, session, response);
        return;
    }
    if (strcmp(command, "explore_resume") == 0) {
        HandleExploreSelect(message, session, response, "resume");
        return;
    }

    if (strcmp(command, "set_fen") == 0) {
        char suppliedFen[MAX_FEN_LENGTH];
        char canonicalFen[MAX_FEN_LENGTH];
        Position authoritative;

        if (!ExtractJsonStringField(
                message, "payload", suppliedFen, sizeof(suppliedFen)) ||
            !PositionFromFen(suppliedFen, &authoritative) ||
            !PositionToFen(&authoritative, canonicalFen)) {
            AnalysisReportRecovery(
                "set_fen",
                0,
                "Invalid FEN; check the board and all six fields.");
            (void)WriteResponse(response, 0, "invalid_fen", NULL, NULL);
            return;
        }

        session->position = authoritative;
        session->hasPosition = 1;
        session->lastMove[0] = '\0';
        memset(session->pendingBoard, 0, sizeof(session->pendingBoard));
        session->hasPendingBoard = 0;
        session->pendingIsMismatch = 0;
        session->pendingRecoveryFailed = 0;
        session->pendingSnapshotSequence = 0U;
        (void)snprintf(
            session->stateSource,
            sizeof(session->stateSource),
            "manual");
        AnalysisSetSynchronising(0, NULL);

        fprintf(
            logFile,
            "Authoritative FEN applied to session %s: %s\n",
            session->id,
            canonicalFen);
        fflush(logFile);
        AnalysisPublish(
            canonicalFen,
            session->hasOrientation ? session->visuallyFlipped : 0,
            NULL,
            session->stateSource);
        AnalysisReportRecovery(
            "set_fen", 1, "Authoritative position applied.");
        (void)WriteResponse(
            response, 1, "fen_applied", canonicalFen, NULL);
        return;
    }

    if (strcmp(command, "restart_engines") == 0) {
        AnalysisRestartEngines();
        fprintf(logFile, "Engine restart requested for session %s.\n", session->id);
        fflush(logFile);
        (void)WriteResponse(response, 1, "engines_restarted", NULL, NULL);
        return;
    }

    if (strcmp(command, "rescan_result") == 0) {
        char result[32];
        const char *messageText;

        if (!ExtractJsonStringField(
                message, "payload", result, sizeof(result))) {
            (void)WriteResponse(
                response, 0, "invalid_rescan_result", NULL, NULL);
            return;
        }

        if (strcmp(result, "game_ended") == 0) {
            messageText =
                "The Chess.com game has ended; there is no active board "
                "to re-read.";
        } else if (strcmp(result, "no_supported_board") == 0) {
            messageText =
                "No supported Chess.com board is visible in the owning tab.";
        } else if (strcmp(result, "content_unavailable") == 0) {
            messageText =
                "The owning Chess.com tab is unavailable; reload it and "
                "try again.";
        } else {
            (void)WriteResponse(
                response, 0, "invalid_rescan_result", NULL, NULL);
            return;
        }

        AnalysisReportRecovery("rescan", 0, messageText);
        fprintf(
            logFile,
            "Board re-read failed for session %s: %s.\n",
            session->id,
            result);
        fflush(logFile);
        (void)WriteResponse(response, 1, "rescan_failed", NULL, NULL);
        return;
    }

    (void)WriteResponse(response, 0, "unknown_command", NULL, NULL);
}

static void HandleSessionEnd(
    const char *message,
    FILE *logFile,
    SessionState *session,
    char response[MAX_RESPONSE_LENGTH])
{
    char reason[64];
    char gameResult[16] = "*";

    if (!RequireMatchingSession(message, session, response)) {
        return;
    }

    if (!ExtractJsonStringField(message, "reason", reason, sizeof(reason))) {
        (void)snprintf(reason, sizeof(reason), "browser_session_end");
    }
    if (FindJsonFieldValue(message, "game_result") != NULL &&
        (!ExtractJsonStringField(
            message, "game_result", gameResult, sizeof(gameResult)) ||
         (strcmp(gameResult, "*") != 0 &&
          strcmp(gameResult, "1-0") != 0 &&
          strcmp(gameResult, "0-1") != 0 &&
          strcmp(gameResult, "1/2-1/2") != 0))) {
        (void)WriteResponse(response, 0, "invalid_game_result", NULL, NULL);
        return;
    }
    if (strcmp(reason, "game_end") != 0) {
        (void)snprintf(gameResult, sizeof(gameResult), "*");
    }

    fprintf(logFile, "=== Session ended: %s (%s) ===\n", session->id, reason);
    fflush(logFile);
    if (session->explorer.active) {
        AnalysisExploreDestroy(session->explorer.id, reason);
    }
    AnalysisSessionEnd(reason, gameResult);
    memset(session, 0, sizeof(*session));
    (void)WriteResponse(response, 1, "session_ended", NULL, NULL);
}

static void HandleMessage(
    const char *message,
    FILE *logFile,
    SessionState *session,
    char response[MAX_RESPONSE_LENGTH])
{
    char type[32];

    if (!ExtractJsonStringField(message, "type", type, sizeof(type))) {
        fprintf(logFile, "Rejected message without a valid type field.\n");
        fflush(logFile);
        (void)WriteResponse(response, 0, "invalid_type", NULL, NULL);
    } else if (strcmp(type, "session_start") == 0) {
        HandleSessionStart(message, logFile, session, response);
    } else if (strcmp(type, "position_snapshot") == 0) {
        HandlePositionSnapshot(message, logFile, session, response);
    } else if (strcmp(type, "history_reconcile") == 0) {
        HandleHistoryReconcile(message, logFile, session, response);
    } else if (strcmp(type, "session_command") == 0) {
        HandleSessionCommand(message, logFile, session, response);
    } else if (strcmp(type, "session_end") == 0) {
        HandleSessionEnd(message, logFile, session, response);
    } else {
        fprintf(logFile, "Ignored message type: %s\n", type);
        fflush(logFile);
        (void)WriteResponse(response, 0, "ignored_type", NULL, NULL);
    }
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

    /* Complete the native-messaging handshake before opening any UI or engine.
     * A stale extension therefore gets a precise response and a clean exit
     * instead of launching an incompatible overlay. */
    for (;;) {
        char *message = NULL;
        char response[MAX_RESPONSE_LENGTH];
        int helloStatus;

        if (!ReadNativeMessage(&message)) {
            fclose(logFile);
            return EXIT_SUCCESS;
        }

        helloStatus = HandleHello(message, logFile, response);
        free(message);

        if (!WriteNativeMessage(response)) {
            fclose(logFile);
            return EXIT_FAILURE;
        }

        if (helloStatus == HELLO_REJECTED) {
            fclose(logFile);
            return EXIT_FAILURE;
        }

        if (helloStatus == HELLO_ACCEPTED) {
            break;
        }
    }

    int analysisStatus = AnalysisStart(logFile, ForwardAnalysisEvent, NULL);

    if (analysisStatus <= 0) {
        if (analysisStatus < 0) {
            char response[MAX_RESPONSE_LENGTH];
            int written = snprintf(
                response,
                sizeof(response),
                "{\"type\":\"error\",\"ok\":false,"
                "\"reason\":\"incompatible_overlay_protocol\","
                "\"protocol_version\":%d,\"host_version\":\"%s\"}",
                CHESSLISTENER_PROTOCOL_VERSION,
                CHESSLISTENER_HOST_VERSION);

            if (written >= 0 && (size_t)written < sizeof(response)) {
                (void)WriteNativeMessage(response);
            }
        }

        fclose(logFile);
        return analysisStatus < 0 ? EXIT_FAILURE : EXIT_SUCCESS;
    }

    SessionState session = {0};

    for (;;) {
        char *message = NULL;

        if (!ReadNativeMessage(&message)) {
            break;
        }

        char response[MAX_RESPONSE_LENGTH];
        HandleMessage(message, logFile, &session, response);

        if (!WriteNativeMessage(response)) {
            free(message);
            break;
        }

        free(message);
    }

    if (session.active) {
        if (session.explorer.active) {
            AnalysisExploreDestroy(
                session.explorer.id, "browser_disconnected");
        }
        AnalysisSessionEnd("browser_disconnected", "*");
    }
    AnalysisStop();

    fclose(logFile);
    return EXIT_SUCCESS;
}
