#define _POSIX_C_SOURCE 200809L

#include "overlay.h"

#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifndef _DEFAULT_SOURCE
#endif

struct Overlay {
    pid_t pid;
    int   fd;        /* write end of the pipe to the overlay's stdin */
    int   dead;
};

static int write_all(int fd, const char *s, size_t n)
{
    while (n) {
        ssize_t w = write(fd, s, n);
        if (w < 0) { if (errno == EINTR) continue; return -1; }
        s += w; n -= (size_t)w;
    }
    return 0;
}

Overlay *overlay_start(const char *script)
{
    int p[2];
    Overlay *o;

    if (!script) return NULL;
    signal(SIGPIPE, SIG_IGN);
    if (pipe(p) < 0) return NULL;

    o = calloc(1, sizeof *o);
    if (!o) { close(p[0]); close(p[1]); return NULL; }

    o->pid = fork();
    if (o->pid < 0) { free(o); close(p[0]); close(p[1]); return NULL; }

    if (o->pid == 0) {
        dup2(p[0], STDIN_FILENO);
        close(p[0]); close(p[1]);
        /* CRITICAL: our stdout is the native-messaging channel. A child that
         * inherits it can corrupt the framing with a single stray print, and
         * Qt does print to stdout on occasion. Send the child's stdout to our
         * stderr so its output stays visible in the browser log but can never
         * reach the protocol stream. */
        dup2(STDERR_FILENO, STDOUT_FILENO);
        execlp("python3", "python3", "-u", script, (char *)NULL);
        _exit(127);
    }

    close(p[0]);
    o->fd = p[1];
    return o;
}

/* Every value we emit is either an integer or a chess token (FEN, UCI move),
 * none of which can contain a quote or backslash, so plain snprintf is safe
 * here. If you ever add free text (player names, comments), escape it. */
static int append_score(char *buf, size_t cap, size_t *len, const UciLine *l)
{
    int n;
    if (l->has_mate)
        n = snprintf(buf + *len, cap - *len, "\"mate\":%d", l->mate);
    else if (l->has_cp)
        n = snprintf(buf + *len, cap - *len, "\"cp\":%d", l->cp);
    else
        n = snprintf(buf + *len, cap - *len, "\"cp\":null");
    if (n < 0 || (size_t)n >= cap - *len) return -1;
    *len += (size_t)n;
    return 0;
}

#define APPEND(...) do {                                        \
        int _n = snprintf(buf + len, sizeof buf - len, __VA_ARGS__); \
        if (_n < 0 || (size_t)_n >= sizeof buf - len) return -1; \
        len += (size_t)_n;                                      \
    } while (0)

int overlay_publish(Overlay *o, const char *fen, int flip,
                    const UciLine *best, const char *human_move,
                    const UciLine *alts, int n)
{
    char buf[4096];
    size_t len = 0;

    if (!o || o->dead || !fen) return -1;

    APPEND("{\"fen\":\"%s\",\"flip\":%s", fen, flip ? "true" : "false");

    if (best) {
        APPEND(",\"best\":{\"move\":\"%s\",\"pv\":\"%s\",", best->move, best->pv);
        if (append_score(buf, sizeof buf, &len, best) < 0) return -1;
        APPEND("}");
    }
    if (human_move && *human_move)
        APPEND(",\"human\":{\"move\":\"%s\"}", human_move);

    if (alts && n > 0) {
        APPEND(",\"lines\":[");
        for (int i = 0; i < n; i++) {
            if (!alts[i].move[0]) continue;
            APPEND("%s{\"move\":\"%s\",", i ? "," : "", alts[i].move);
            if (append_score(buf, sizeof buf, &len, &alts[i]) < 0) return -1;
            APPEND("}");
        }
        APPEND("]");
    }
    APPEND("}\n");

    if (write_all(o->fd, buf, len) < 0) { o->dead = 1; return -1; }
    return 0;
}

#undef APPEND

void overlay_stop(Overlay *o)
{
    int status;
    if (!o) return;
    if (o->fd > 0) close(o->fd);      /* EOF makes the overlay quit itself */
    if (o->pid > 0) {
        for (int i = 0; i < 50; i++) {
            pid_t r = waitpid(o->pid, &status, WNOHANG);
            if (r == o->pid || r < 0) { o->pid = 0; break; }
            nanosleep(&(struct timespec){ .tv_nsec = 20000000L }, NULL);
        }
        if (o->pid > 0) { kill(o->pid, SIGTERM); waitpid(o->pid, &status, 0); }
    }
    free(o);
}
