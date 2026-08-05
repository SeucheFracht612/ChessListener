/* overlay.h -- spawn the Qt overlay as a child and feed it JSON lines. */
#ifndef OVERLAY_H
#define OVERLAY_H

#include "uci.h"

typedef struct Overlay Overlay;

/* python3 is looked up on PATH; script must be an absolute path. */
Overlay *overlay_start(const char *script);

/* best/human may be NULL. alts may be NULL (n = 0). Returns 0, or -1 if the
 * overlay has gone away -- treat that as "user closed the window", not fatal. */
int overlay_publish(Overlay *o, const char *fen, int flip,
                    const UciLine *best, const char *human_move,
                    const UciLine *alts, int n);

void overlay_stop(Overlay *o);

#endif /* OVERLAY_H */
