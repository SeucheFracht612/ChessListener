/* uci.h -- persistent UCI client. Works for lc0+Maia and for Stockfish.
 *
 * Two usage modes:
 *
 *   Blocking, one shot (Maia: "go nodes 1", finishes in milliseconds):
 *       uci_analyse() / uci_bestmove()
 *
 *   Streaming, interruptible (Stockfish: "go infinite", read info lines as
 *   they arrive, abort the moment the board moves on):
 *       uci_search_begin() -> uci_search_poll() ... -> uci_search_abort()
 *
 * The streaming mode is what lets the overlay follow a premove burst: no
 * search ever pins the caller for longer than one poll timeout.
 */
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
#define UCI_LINES_MAX 5

typedef enum {
    UCI_SCORE_EXACT = 0,
    UCI_SCORE_LOWERBOUND = 1,
    UCI_SCORE_UPPERBOUND = 2
} UciScoreBound;

typedef struct {
    int  multipv;            /* 1-based rank                             */
    int  depth;
    int  has_cp,   cp;       /* centipawns, side-to-move POV             */
    int  has_mate, mate;     /* mate in N; negative = being mated        */
    UciScoreBound bound;      /* UCI lowerbound/upperbound, else exact    */
    char move[8];            /* first move of the line, UCI notation     */
    char pv[UCI_PV_MAX];     /* full line, space separated               */
} UciLine;

/* 0 = ok, -1 = io/protocol, -2 = engine died, -3 = timeout,
 * -4 = no legal move (terminal position). */

UciEngine *uci_start(const UciConfig *cfg);

/* ---- one-shot analysis (Maia) ---- */

/* Fills lines[0..n-1] ranked best first. Returns the count, or negative on
 * error. lines[0].move always matches the engine's own bestmove. */
int uci_analyse(UciEngine *e, const char *fen, UciLine *lines, int max);

int uci_bestmove(UciEngine *e, const char *fen, char *out, size_t outsz);

/* Interruptible form of the engine's configured one-shot search. It uses the
 * limit from UciConfig (normally "nodes 1" for Maia), while the caller owns
 * the wall-clock deadline by polling in short slices. The result is exposed
 * through uci_lines() once uci_search_poll() reports finished. */
int uci_analyse_begin(UciEngine *e, const char *fen);

/* ---- streaming, interruptible (Stockfish) ---- */

/* Clears the accumulator and issues "position fen ... / go infinite".
 * Returns 0, or -2 if the engine has gone away. */
int uci_search_begin(UciEngine *e, const char *fen);

/* Consumes engine output for at most timeout_ms total (plus scheduling
 * granularity), with a finite line budget for perpetually readable pipes.
 *   *updated  set when at least one ranked line changed
 *   *finished set when the engine emitted bestmove (search is over)
 * Returns 0, -1 protocol, -2 engine died, -4 terminal position.
 * A quiet engine is not an error: 0 with *updated == 0. */
int uci_search_poll(UciEngine *e, int timeout_ms, int *updated, int *finished);

/* Sends "stop" and consumes up to and including bestmove, so the engine is
 * clean for the next "position". Safe to call when no search is running. */
int uci_search_abort(UciEngine *e);

/* As above, but bound the drain-after-stop wait. A timeout leaves the process
 * unsafe to reuse; callers should uci_stop() it. */
int uci_search_abort_timeout(UciEngine *e, int timeout_ms);

/* Ranked lines accumulated by the current (or last) search. */
const UciLine *uci_lines(const UciEngine *e, int *count);

/* Highest depth reported by the current search, 0 if none yet. */
int uci_depth(const UciEngine *e);

/* setoption. Only safe between searches -- call uci_search_abort() first. */
int uci_set_option(UciEngine *e, const char *name, const char *value);
int uci_set_multipv(UciEngine *e, int multipv);

/* Convert a side-to-move score to white's POV, for a stable overlay. */
int uci_cp_white(const UciLine *l, const char *fen);

void uci_stop(UciEngine *e);

#endif /* UCI_H */
