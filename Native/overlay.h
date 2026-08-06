/* overlay.h -- spawn the Qt overlay and exchange small control/data messages.
 *
 * Publishing is thread safe: the message-loop thread pushes board frames while
 * the engine thread pushes evaluations, and both serialise on one lock.
 * The control channel (overlay -> host) stays open for the whole session so
 * the settings panel can change engine strength live.
 */
#ifndef OVERLAY_H
#define OVERLAY_H

#include <stddef.h>

#include "uci.h"

typedef struct Overlay Overlay;

typedef struct {
    long budget_ms;      /* Stockfish time per position; 0 = keep thinking */
    int  maia_rating;
    int  threads;
    int  multipv;
} OverlaySettings;

/* python3 is looked up on PATH; script must be an absolute path. */
Overlay *overlay_start(const char *script);

/* Blocks until the startup window either starts analysis or closes.
 * Returns 1 with validated settings, 0 if the window was closed, or -1 for a
 * malformed control message / IPC failure. */
int overlay_wait_for_start(Overlay *o, OverlaySettings *settings);

/* Reads one control line (blocking). Returns 1 on a line, 0 on EOF (the
 * overlay exited), -1 on error. Call from the control thread only. */
int overlay_read_control(Overlay *o, char *line, size_t size);

/* Parses "budget=400 maia=1900 threads=2 multipv=3" from a SET/START payload
 * into settings, clamping to supported ranges. Returns 1 on success. */
int overlay_parse_settings(const char *payload, OverlaySettings *settings);

/* Tell the UI that engine startup has finished. */
int overlay_publish_ready(Overlay *o, int stockfish_ready, int maia_ready,
                          const OverlaySettings *settings);

/* One-line status note, shown transiently in the UI. kind: "info" | "warn". */
int overlay_publish_status(Overlay *o, const char *kind, const char *text);

/* Echo the settings actually in force after a live change. */
int overlay_publish_settings(Overlay *o, const OverlaySettings *settings);

/* Board-only frame: paints immediately, carries no evaluation. This is what
 * keeps the overlay in step with a premove burst. */
int overlay_publish_position(Overlay *o, unsigned long seq, const char *fen,
                             int flip);

/* Evaluation frame for the position identified by seq. best/human may be NULL,
 * lines may be NULL with n = 0. Scores are white's POV. Returns 0, or -1 if
 * the overlay has gone away -- treat that as "user closed the window". */
int overlay_publish_analysis(Overlay *o, unsigned long seq, const char *fen,
                             int flip, int depth, int final,
                             const UciLine *best, const char *human_move,
                             const UciLine *lines, int n);

void overlay_stop(Overlay *o);

#endif /* OVERLAY_H */
