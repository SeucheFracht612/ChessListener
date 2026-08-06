/* analysis.h -- owns the engines and the overlay.
 *
 * Threading model (this is the whole point of the module):
 *
 *   caller thread   AnalysisPublish() only records the wanted position and
 *                   paints the board. It never waits for an engine, so a
 *                   premove burst is absorbed at DOM speed.
 *   engine thread   Runs Maia once, then streams Stockfish on the newest
 *                   position, aborting the search the instant a newer one
 *                   arrives. Latest position always wins; intermediate ones
 *                   are dropped rather than queued.
 *   control thread  Reads the overlay's control pipe so the settings panel can
 *                   change strength while analysis is running.
 *
 * Paths are taken from the environment when set, otherwise resolved relative
 * to the host binary (NOT the working directory, which the browser chooses):
 *
 *   CHESSLISTENER_STOCKFISH   default: /usr/games/stockfish, then stockfish
 *   CHESSLISTENER_LC0         default: <dir>/Engine/lc0
 *   CHESSLISTENER_MAIA_NET    optional override for the selected Maia net
 *   CHESSLISTENER_OVERLAY     default: <dir>/overlay.py
 */
#ifndef ANALYSIS_H
#define ANALYSIS_H

#include <stdio.h>

/* Opens the startup window, waits for the user's settings, then starts the
 * engines and the worker threads. Returns 1 when analysis is ready, 0 when
 * startup was cancelled or the overlay could not be started. */
int AnalysisStart(FILE *logFile);

void AnalysisStop(void);

/* Record a new position. Returns immediately: the board is painted now, the
 * evaluation follows on the engine thread. */
void AnalysisPublish(const char *fen, int visuallyFlipped);

#endif /* ANALYSIS_H */
