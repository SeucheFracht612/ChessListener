/* analysis.h -- owns the engines and the overlay.
 *
 * Deliberately fail-soft: if Stockfish, lc0 or PyQt are missing, every call
 * here degrades to a no-op and the host keeps recording games as before.
 * A missing engine must never break position logging.
 *
 * Paths are taken from the environment when set, otherwise resolved relative
 * to the host binary (NOT the working directory, which the browser chooses):
 *
 *   CHESSLISTENER_STOCKFISH   default: /usr/games/stockfish, then stockfish
 *   CHESSLISTENER_LC0         default: <dir>/Engine/lc0
 *   CHESSLISTENER_MAIA_NET    default: <dir>/Engine/maia-chess/maia_weights/
 *                                      maia-1500.pb.gz
 *   CHESSLISTENER_OVERLAY     default: <dir>/overlay.py
 *   CHESSLISTENER_NO_OVERLAY  set to 1 to run headless
 */
#ifndef ANALYSIS_H
#define ANALYSIS_H

#include <stdio.h>

void AnalysisStart(FILE *logFile);
void AnalysisStop(void);

/* Analyse a FEN and push the result to the overlay. No-op if unavailable.
 * Silently skips work when a newer message is already queued, so the overlay
 * never falls behind the live board. */
void AnalysisPublish(const char *fen, int visuallyFlipped);

#endif /* ANALYSIS_H */
