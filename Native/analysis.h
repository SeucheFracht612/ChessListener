/* analysis.h -- owns the engines and the overlay.
 *
 * Threading model (this is the whole point of the module):
 *
 *   caller thread   AnalysisPublish() only records the wanted position and
 *                   paints the board. It never waits for an engine, so a
 *                   premove burst is absorbed at DOM speed.
 *   Stockfish lane  Streams the engine immediately on the newest revision.
 *   Maia lane       Runs independently with a finite deadline. Its later
 *                   result merges into the same revision-keyed cache, so it
 *                   can never delay or erase Stockfish output.
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
#include <stddef.h>

/* Overlay actions are delivered from analysis.c's control thread. The sink
 * must consume payload and session_id before it returns; their backing
 * buffers are reused after the callback. session_id is captured when the
 * complete control line arrives, so a concurrent session switch cannot
 * retarget a queued action. kind is "command" for user actions and "event"
 * for lifecycle notices such as an intentional dismissal. */
typedef void (*AnalysisEventSink)(
    const char *kind,
    const char *name,
    const char *payload,
    const char *session_id,
    void *ctx);

/* Opens the startup window, waits for the user's settings, then starts the
 * engines and the worker threads. Returns 1 when analysis is ready, 0 when
 * startup was cancelled/unavailable, or -1 for an incompatible UI protocol. */
int AnalysisStart(FILE *logFile, AnalysisEventSink sink, void *ctx);

void AnalysisStop(void);

/* Begin/end an explicitly owned browser game. Starting a session invalidates
 * every result from the previous generation. Ending one cancels outstanding
 * work; the overlay decides whether to retain the final board from reason. */
void AnalysisSessionStart(const char *session_id, const char *label);
void AnalysisSessionEnd(const char *reason, const char *result);

/* Publish the latest legally verified full game record for local review. */
void AnalysisUpdateGameRecord(const char *initial_fen, const char *uci_moves,
                              size_t move_count, const char *result);

/* Restart both UCI processes on their owner thread using the current settings.
 * The current position is analysed again after a successful restart. */
void AnalysisRestartEngines(void);

/* Complete a recovery request after native validation. accepted controls the
 * info/warn presentation; a rejection never changes the overlay board. */
void AnalysisReportRecovery(const char *action, int accepted,
                            const char *message);

/* Reorient the existing board without creating a new position or search. */
void AnalysisUpdateOrientation(int visually_flipped);

/* Metadata-only state updates. They retain the current revision and cached
 * engine output, and therefore never restart either engine. source is one of
 * "exact", "inferred", or "manual". */
void AnalysisUpdateStateSource(const char *source);
void AnalysisSetSynchronising(int active, const char *text);

/* Analysis Lab selects a validated, session-scoped branch as the target of
 * the existing engine pair.  The authoritative live state continues to be
 * recorded by AnalysisPublish while a branch is selected. */
void AnalysisExploreStart(unsigned long long branch_id, unsigned int node_id,
                          const char *fen, int visually_flipped,
                          const char *last_move, const char *source);
void AnalysisExploreSelect(unsigned long long branch_id, unsigned int node_id,
                           const char *fen, int visually_flipped,
                           const char *last_move, const char *source,
                           const char *action);
void AnalysisExploreLive(unsigned long long branch_id);
void AnalysisExploreDestroy(unsigned long long branch_id, const char *reason);
void AnalysisReportExploreRejected(const char *action, const char *reason,
                                   const char *message,
                                   unsigned long long branch_id,
                                   unsigned int node_id);

/* Record a new position. lastMove is UCI or NULL when unknown. Returns
 * immediately: the board is painted now, the evaluation follows on the
 * independent engine lanes. */
void AnalysisPublish(const char *fen, int visuallyFlipped,
                     const char *lastMove, const char *source);

#endif /* ANALYSIS_H */
