#define _POSIX_C_SOURCE 200809L

#include "uci.h"

#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define UCI_BUFSZ  65536
#define UCI_LINESZ 8192

struct UciEngine {
    pid_t    pid;
    int      in_fd, out_fd;
    UciLimit limit;
    long     limit_value;
    int      multipv;
    int      timeout_ms;
    char     buf[UCI_BUFSZ];
    size_t   len;
};

/* We own the read buffer rather than using fdopen()+getline(), because stdio's
 * buffer is invisible to poll() -- mixing the two gives you timeouts that
 * never fire while a complete line sits unread in the FILE. */

/* ------------------------------------------------------------------ io -- */

static int write_all(int fd, const char *s, size_t n)
{
    while (n) {
        ssize_t w = write(fd, s, n);
        if (w < 0) { if (errno == EINTR) continue; return -1; }
        s += w; n -= (size_t)w;
    }
    return 0;
}

static int send_cmd(UciEngine *e, const char *fmt, ...)
{
    char line[UCI_LINESZ];
    va_list ap;
    int n;

    va_start(ap, fmt);
    n = vsnprintf(line, sizeof line - 2, fmt, ap);
    va_end(ap);
    if (n < 0 || (size_t)n >= sizeof line - 2) return -1;

    line[n++] = '\n';
    line[n]   = '\0';
    return write_all(e->in_fd, line, (size_t)n);
}

static int rd_line(UciEngine *e, char *line, size_t linesz, int timeout_ms)
{
    for (;;) {
        char *nl = memchr(e->buf, '\n', e->len);
        if (nl) {
            size_t n    = (size_t)(nl - e->buf);
            size_t copy = n < linesz - 1 ? n : linesz - 1;
            memcpy(line, e->buf, copy);
            line[copy] = '\0';
            if (copy && line[copy - 1] == '\r') line[copy - 1] = '\0';
            memmove(e->buf, nl + 1, e->len - n - 1);
            e->len -= n + 1;
            return 0;
        }
        if (e->len == sizeof e->buf) { e->len = 0; continue; }

        struct pollfd p = { .fd = e->out_fd, .events = POLLIN };
        int r = poll(&p, 1, timeout_ms);
        if (r == 0) return -3;
        if (r < 0) { if (errno == EINTR) continue; return -1; }

        ssize_t got = read(e->out_fd, e->buf + e->len, sizeof e->buf - e->len);
        if (got == 0) return -2;
        if (got < 0) { if (errno == EINTR) continue; return -1; }
        e->len += (size_t)got;
    }
}

static int first_word_is(const char *line, const char *tok)
{
    size_t n = strlen(tok);
    return strncmp(line, tok, n) == 0 && (line[n] == '\0' || line[n] == ' ');
}

static int wait_for(UciEngine *e, const char *tok,
                    char *line, size_t linesz, int timeout_ms)
{
    for (;;) {
        int r = rd_line(e, line, linesz, timeout_ms);
        if (r != 0) return r;
        if (first_word_is(line, tok)) return 0;
    }
}

/* --------------------------------------------------------- info parsing -- */

/* An info line looks like:
 *   info depth 18 seldepth 24 multipv 2 score cp -34 nodes 1.2M pv e2e4 e7e5
 * Order is not guaranteed, "info string ..." must be ignored, and the score
 * may carry a trailing lowerbound/upperbound token. */
static void parse_info(const char *src, UciLine *lines, int max, int *seen)
{
    char work[UCI_LINESZ];
    char *pv = NULL, *save, *tok;
    UciLine tmp;
    int slot;

    if (strncmp(src, "info", 4) != 0) return;
    if (first_word_is(src + 5, "string")) return;

    snprintf(work, sizeof work, "%s", src);

    /* Split the pv off first: it runs to end of line, so it can't be walked
     * token-by-token alongside the numeric fields. */
    {
        char *p = strstr(work, " pv ");
        if (p) { *p = '\0'; pv = p + 4; }
    }

    memset(&tmp, 0, sizeof tmp);
    tmp.multipv = 1;

    for (tok = strtok_r(work, " ", &save); tok; tok = strtok_r(NULL, " ", &save)) {
        if (!strcmp(tok, "depth")) {
            char *v = strtok_r(NULL, " ", &save);
            if (v) tmp.depth = atoi(v);
        } else if (!strcmp(tok, "multipv")) {
            char *v = strtok_r(NULL, " ", &save);
            if (v) tmp.multipv = atoi(v);
        } else if (!strcmp(tok, "score")) {
            char *kind = strtok_r(NULL, " ", &save);
            char *v    = kind ? strtok_r(NULL, " ", &save) : NULL;
            if (kind && v) {
                if (!strcmp(kind, "cp"))        { tmp.has_cp = 1;   tmp.cp = atoi(v); }
                else if (!strcmp(kind, "mate")) { tmp.has_mate = 1; tmp.mate = atoi(v); }
            }
        }
    }

    /* A line with no pv carries no move -- e.g. "info depth 1 currmove ...".
     * Nothing to record. */
    if (!pv || !*pv) return;

    snprintf(tmp.pv, sizeof tmp.pv, "%s", pv);
    {
        size_t n = strcspn(tmp.pv, " ");
        if (n == 0 || n >= sizeof tmp.move) return;
        memcpy(tmp.move, tmp.pv, n);
        tmp.move[n] = '\0';
    }

    slot = tmp.multipv - 1;
    if (slot < 0 || slot >= max) return;

    /* Deeper output supersedes shallower for the same rank. */
    if (lines[slot].move[0] && lines[slot].depth > tmp.depth) return;
    lines[slot] = tmp;
    if (tmp.multipv > *seen) *seen = tmp.multipv;
}

/* --------------------------------------------------------------- start -- */

UciEngine *uci_start(const UciConfig *cfg)
{
    int to_child[2], from_child[2];
    UciEngine *e;
    char line[UCI_LINESZ], wopt[UCI_LINESZ];
    int boot;

    if (!cfg || !cfg->exe) return NULL;

    signal(SIGPIPE, SIG_IGN);

    if (pipe(to_child) < 0) return NULL;
    if (pipe(from_child) < 0) {
        close(to_child[0]); close(to_child[1]);
        return NULL;
    }

    e = calloc(1, sizeof *e);
    if (!e) {
        close(to_child[0]); close(to_child[1]);
        close(from_child[0]); close(from_child[1]);
        return NULL;
    }

    e->pid = fork();
    if (e->pid < 0) {
        free(e);
        close(to_child[0]); close(to_child[1]);
        close(from_child[0]); close(from_child[1]);
        return NULL;
    }

    if (e->pid == 0) {
        dup2(to_child[0], STDIN_FILENO);
        dup2(from_child[1], STDOUT_FILENO);
        close(to_child[0]);  close(to_child[1]);
        close(from_child[0]); close(from_child[1]);
        if (cfg->weights) {
            snprintf(wopt, sizeof wopt, "--weights=%s", cfg->weights);
            execlp(cfg->exe, cfg->exe, wopt, (char *)NULL);
        } else {
            execlp(cfg->exe, cfg->exe, (char *)NULL);
        }
        _exit(127);
    }

    close(to_child[0]);
    close(from_child[1]);
    e->in_fd      = to_child[1];
    e->out_fd     = from_child[0];
    e->limit      = cfg->limit;
    e->limit_value = cfg->limit_value > 0 ? cfg->limit_value : 1;
    e->multipv    = cfg->multipv > 0 ? cfg->multipv : 1;
    e->timeout_ms = cfg->timeout_ms > 0 ? cfg->timeout_ms : 10000;

    boot = cfg->startup_ms > 0 ? cfg->startup_ms : 60000;

    if (send_cmd(e, "uci") < 0) goto fail;
    if (wait_for(e, "uciok", line, sizeof line, boot) != 0) goto fail;

    send_cmd(e, "setoption name Threads value %d",
             cfg->threads > 0 ? cfg->threads : 1);
    if (e->multipv > 1)
        send_cmd(e, "setoption name MultiPV value %d", e->multipv);

    if (cfg->weights) {          /* lc0: one eval, no batching or prefetch */
        send_cmd(e, "setoption name MinibatchSize value 1");
        send_cmd(e, "setoption name MaxPrefetch value 0");
        if (cfg->backend)
            send_cmd(e, "setoption name Backend value %s", cfg->backend);
    }

    if (send_cmd(e, "isready") < 0) goto fail;
    if (wait_for(e, "readyok", line, sizeof line, boot) != 0) goto fail;

    return e;

fail:
    uci_stop(e);
    return NULL;
}

/* ------------------------------------------------------------- analyse -- */

int uci_analyse(UciEngine *e, const char *fen, UciLine *lines, int max)
{
    char line[UCI_LINESZ];
    char best[8] = { 0 };
    int seen = 0, r;

    if (!e || !fen || !lines || max < 1) return -1;

    memset(lines, 0, (size_t)max * sizeof *lines);

    if (send_cmd(e, "position fen %s", fen) < 0) return -2;

    switch (e->limit) {
    case UCI_LIMIT_NODES:    r = send_cmd(e, "go nodes %ld",    e->limit_value); break;
    case UCI_LIMIT_DEPTH:    r = send_cmd(e, "go depth %ld",    e->limit_value); break;
    case UCI_LIMIT_MOVETIME: r = send_cmd(e, "go movetime %ld", e->limit_value); break;
    default: return -1;
    }
    if (r < 0) return -2;

    for (;;) {
        r = rd_line(e, line, sizeof line, e->timeout_ms);
        if (r != 0) return r;

        if (first_word_is(line, "bestmove")) {
            const char *p = line + strlen("bestmove");
            size_t n;
            while (*p == ' ') p++;
            n = strcspn(p, " ");
            if (n == 6 && !strncmp(p, "(none)", 6)) return -4;
            if (n > 0 && n < sizeof best) { memcpy(best, p, n); best[n] = '\0'; }
            break;
        }
        parse_info(line, lines, max, &seen);
    }

    if (!best[0]) return -1;

    /* Trust bestmove over the info stream: if they disagree (aborted search,
     * or an engine that emits no pv at all), make slot 0 authoritative. */
    if (seen == 0 || strcmp(lines[0].move, best) != 0) {
        for (int i = 0; i < seen; i++) {
            if (!strcmp(lines[i].move, best) && i != 0) {
                UciLine t = lines[0]; lines[0] = lines[i]; lines[i] = t;
                break;
            }
        }
        if (strcmp(lines[0].move, best) != 0) {
            memset(&lines[0], 0, sizeof lines[0]);
            lines[0].multipv = 1;
            snprintf(lines[0].move, sizeof lines[0].move, "%s", best);
            snprintf(lines[0].pv,   sizeof lines[0].pv,   "%s", best);
            if (seen == 0) seen = 1;
        }
    }

    return seen ? seen : 1;
}

int uci_bestmove(UciEngine *e, const char *fen, char *out, size_t outsz)
{
    UciLine l[1];
    int n = uci_analyse(e, fen, l, 1);
    if (n < 0) return n;
    if (outsz <= strlen(l[0].move)) return -1;
    memcpy(out, l[0].move, strlen(l[0].move) + 1);
    return 0;
}

int uci_cp_white(const UciLine *l, const char *fen)
{
    const char *p = fen + strcspn(fen, " ");
    while (*p == ' ') p++;
    return (*p == 'b') ? -l->cp : l->cp;
}

/* ---------------------------------------------------------------- stop -- */

void uci_stop(UciEngine *e)
{
    int status;
    if (!e) return;

    if (e->in_fd > 0) {
        send_cmd(e, "quit");
        close(e->in_fd);
    }
    if (e->pid > 0) {
        for (int i = 0; i < 50; i++) {
            pid_t r = waitpid(e->pid, &status, WNOHANG);
            if (r == e->pid || r < 0) { e->pid = 0; break; }
            nanosleep(&(struct timespec){ .tv_nsec = 20000000L }, NULL);
        }
        if (e->pid > 0) {
            kill(e->pid, SIGKILL);
            waitpid(e->pid, &status, 0);
        }
    }
    if (e->out_fd > 0) close(e->out_fd);
    free(e);
}
