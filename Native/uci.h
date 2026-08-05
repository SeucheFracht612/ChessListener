/* uci.h -- persistent UCI client. Works for lc0+Maia and for Stockfish.
 * Replaces maia.h; delete maia.c/maia.h once you've switched over. */
#ifndef UCI_H
#define UCI_H

#include <stddef.h>

typedef struct UciEngine UciEngine;

typedef enum {
    UCI_LIMIT_NODES,     /* Maia: 1                     */
    UCI_LIMIT_DEPTH,     /* Stockfish: 16-20            */
    UCI_LIMIT_MOVETIME   /* Stockfish: ms, for overlays */
} UciLimit;

typedef struct {
    const char *exe;         /* absolute path to lc0 or stockfish        */
    const char *weights;     /* lc0 only; NULL for Stockfish             */
    const char *backend;     /* lc0 only; "eigen", "blas", NULL = auto   */
    int      threads;
    UciLimit limit;
    long     limit_value;
    int      multipv;        /* 1 = single line                          */
    int      timeout_ms;
    int      startup_ms;
} UciConfig;

#define UCI_PV_MAX 512

typedef struct {
    int  multipv;            /* 1-based rank                             */
    int  depth;
    int  has_cp,   cp;       /* centipawns, side-to-move POV             */
    int  has_mate, mate;     /* mate in N; negative = being mated        */
    char move[8];            /* first move of the line, UCI notation     */
    char pv[UCI_PV_MAX];     /* full line, space separated               */
} UciLine;

/* 0 = ok, -1 = io/protocol, -2 = engine died, -3 = timeout,
 * -4 = no legal move (terminal position). */

UciEngine *uci_start(const UciConfig *cfg);

/* Fills lines[0..n-1] ranked best first. Returns the count, or negative on
 * error. lines[0].move always matches the engine's own bestmove. */
int uci_analyse(UciEngine *e, const char *fen, UciLine *lines, int max);

int uci_bestmove(UciEngine *e, const char *fen, char *out, size_t outsz);

/* Convert a side-to-move score to white's POV, for a stable overlay. */
int uci_cp_white(const UciLine *l, const char *fen);

void uci_stop(UciEngine *e);

#endif /* UCI_H */
