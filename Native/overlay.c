#define _POSIX_C_SOURCE 200809L

#include "overlay.h"

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define CONTROL_LINE_MAX 256U

#define MIN_BUDGET_MS   100L
#define MAX_BUDGET_MS   10000L
#define MIN_MAIA_RATING 1100
#define MAX_MAIA_RATING 1900
#define MIN_THREADS     1
#define MAX_THREADS     32
#define MIN_MULTIPV     1
#define MAX_MULTIPV     UCI_LINES_MAX

struct Overlay {
    pid_t pid;
    int write_fd;   /* host -> overlay stdin  */
    int control_fd; /* overlay stdout -> host */
    int dead;
    pthread_mutex_t write_lock;
};

static int write_all(int fd, const char *s, size_t n)
{
    while (n > 0U) {
        ssize_t written = write(fd, s, n);

        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }

        s += written;
        n -= (size_t)written;
    }

    return 0;
}

static void redirect_child_stderr(void)
{
    const char *debug = getenv("CHESSLISTENER_DEBUG");
    const char *path =
        debug != NULL && strcmp(debug, "1") == 0
            ? "/tmp/chess-listener-overlay.log"
            : "/dev/null";
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);

    if (fd >= 0) {
        (void)dup2(fd, STDERR_FILENO);
        if (fd != STDERR_FILENO) {
            close(fd);
        }
    }
}

Overlay *overlay_start(const char *script)
{
    int to_child[2];
    int from_child[2];
    Overlay *overlay;

    if (script == NULL) {
        return NULL;
    }

    signal(SIGPIPE, SIG_IGN);

    if (pipe(to_child) < 0) {
        return NULL;
    }

    if (pipe(from_child) < 0) {
        close(to_child[0]);
        close(to_child[1]);
        return NULL;
    }

    overlay = calloc(1U, sizeof(*overlay));

    if (overlay == NULL) {
        close(to_child[0]);
        close(to_child[1]);
        close(from_child[0]);
        close(from_child[1]);
        return NULL;
    }

    if (pthread_mutex_init(&overlay->write_lock, NULL) != 0) {
        free(overlay);
        close(to_child[0]);
        close(to_child[1]);
        close(from_child[0]);
        close(from_child[1]);
        return NULL;
    }

    overlay->pid = fork();

    if (overlay->pid < 0) {
        pthread_mutex_destroy(&overlay->write_lock);
        free(overlay);
        close(to_child[0]);
        close(to_child[1]);
        close(from_child[0]);
        close(from_child[1]);
        return NULL;
    }

    if (overlay->pid == 0) {
        (void)dup2(to_child[0], STDIN_FILENO);
        (void)dup2(from_child[1], STDOUT_FILENO);

        close(to_child[0]);
        close(to_child[1]);
        close(from_child[0]);
        close(from_child[1]);

        /* stdout is now a private control pipe, never the browser's native-
         * messaging stream. Keep Qt/Python diagnostics out of the web console
         * by sending stderr to a local file. */
        redirect_child_stderr();

        execlp("python3", "python3", "-u", script, (char *)NULL);
        _exit(127);
    }

    close(to_child[0]);
    close(from_child[1]);
    overlay->write_fd = to_child[1];
    overlay->control_fd = from_child[0];
    return overlay;
}

/* ------------------------------------------------------------- control -- */

int overlay_read_control(Overlay *overlay, char *line, size_t size)
{
    size_t length = 0U;

    if (overlay == NULL || line == NULL || size < 2U) {
        return -1;
    }

    for (;;) {
        char character;
        ssize_t bytes_read = read(overlay->control_fd, &character, 1U);

        if (bytes_read == 0) {
            return 0;
        }

        if (bytes_read < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }

        if (character == '\n') {
            line[length] = '\0';
            return 1;
        }

        if (character == '\r') {
            continue;
        }

        if (length + 1U >= size) {
            /* Overlong line: drop it rather than desynchronising the stream. */
            length = 0U;
            continue;
        }

        line[length] = character;
        length += 1U;
    }
}

static long clamp_long(long value, long low, long high)
{
    if (value < low) return low;
    if (value > high) return high;
    return value;
}

static int clamp_int(int value, int low, int high)
{
    if (value < low) return low;
    if (value > high) return high;
    return value;
}

int overlay_parse_settings(const char *payload, OverlaySettings *settings)
{
    char work[CONTROL_LINE_MAX];
    char *save = NULL;
    char *token;

    if (payload == NULL || settings == NULL) {
        return 0;
    }

    snprintf(work, sizeof(work), "%s", payload);

    for (
        token = strtok_r(work, " \t", &save);
        token != NULL;
        token = strtok_r(NULL, " \t", &save)
    ) {
        char *equals = strchr(token, '=');
        const char *value;

        if (equals == NULL) {
            continue;
        }

        *equals = '\0';
        value = equals + 1;

        if (strcmp(token, "budget") == 0) {
            long milliseconds = strtol(value, NULL, 10);

            /* 0 is legal and means "never stop until the position changes". */
            settings->budget_ms =
                milliseconds == 0L
                    ? 0L
                    : clamp_long(milliseconds, MIN_BUDGET_MS, MAX_BUDGET_MS);
        } else if (strcmp(token, "maia") == 0) {
            int rating = (int)strtol(value, NULL, 10);

            rating = clamp_int(rating, MIN_MAIA_RATING, MAX_MAIA_RATING);
            settings->maia_rating = (rating / 100) * 100;
        } else if (strcmp(token, "threads") == 0) {
            settings->threads =
                clamp_int((int)strtol(value, NULL, 10), MIN_THREADS, MAX_THREADS);
        } else if (strcmp(token, "multipv") == 0) {
            settings->multipv =
                clamp_int((int)strtol(value, NULL, 10), MIN_MULTIPV, MAX_MULTIPV);
        }
    }

    return 1;
}

int overlay_wait_for_start(Overlay *overlay, OverlaySettings *settings)
{
    char line[CONTROL_LINE_MAX];
    int status;

    if (overlay == NULL || settings == NULL) {
        return -1;
    }

    /* Sensible defaults, so a payload missing a field is still usable. */
    settings->budget_ms = 400L;
    settings->maia_rating = 1900;
    settings->threads = 2;
    settings->multipv = 3;

    for (;;) {
        status = overlay_read_control(overlay, line, sizeof(line));

        if (status <= 0) {
            return status;
        }

        if (strcmp(line, "QUIT") == 0) {
            return 0;
        }

        if (strncmp(line, "START", 5) == 0 &&
            (line[5] == '\0' || line[5] == ' ')) {
            return overlay_parse_settings(line + 5, settings) ? 1 : -1;
        }

        /* Anything else before START is noise; keep waiting rather than
         * failing the whole session on one stray line. */
    }
}

/* ------------------------------------------------------------- publish -- */

static int publish_buffer(Overlay *overlay, const char *buffer, size_t length)
{
    int result;

    if (overlay == NULL) {
        return -1;
    }

    pthread_mutex_lock(&overlay->write_lock);

    if (overlay->dead) {
        result = -1;
    } else if (write_all(overlay->write_fd, buffer, length) < 0) {
        overlay->dead = 1;
        result = -1;
    } else {
        result = 0;
    }

    pthread_mutex_unlock(&overlay->write_lock);
    return result;
}

#define APPEND(...) do {                                                \
        int _written = snprintf(                                        \
            buffer + length, sizeof(buffer) - length, __VA_ARGS__);     \
        if (                                                            \
            _written < 0 ||                                             \
            (size_t)_written >= sizeof(buffer) - length                 \
        ) {                                                             \
            return -1;                                                  \
        }                                                               \
        length += (size_t)_written;                                     \
    } while (0)

static int black_to_move(const char *fen)
{
    const char *p = fen + strcspn(fen, " ");

    while (*p == ' ') {
        p += 1;
    }

    return *p == 'b';
}

/* Emits "cp":N or "mate":N already converted to white's point of view, so the
 * UI never has to know whose turn it is to draw the bar in the right
 * direction. */
static int append_score_white(char *buffer, size_t capacity, size_t *length,
                              const UciLine *line, int flipSign)
{
    int written;

    if (line->has_mate) {
        written = snprintf(buffer + *length, capacity - *length,
                           "\"mate\":%d", flipSign ? -line->mate : line->mate);
    } else if (line->has_cp) {
        written = snprintf(buffer + *length, capacity - *length,
                           "\"cp\":%d", flipSign ? -line->cp : line->cp);
    } else {
        written = snprintf(buffer + *length, capacity - *length, "\"cp\":null");
    }

    if (written < 0 || (size_t)written >= capacity - *length) {
        return -1;
    }

    *length += (size_t)written;
    return 0;
}

int overlay_publish_ready(Overlay *overlay, int stockfish_ready, int maia_ready,
                          const OverlaySettings *settings)
{
    char buffer[320];
    int written;

    if (settings == NULL) {
        return -1;
    }

    written = snprintf(
        buffer, sizeof(buffer),
        "{\"type\":\"ready\",\"stockfish\":%s,\"maia\":%s,"
        "\"budget_ms\":%ld,\"maia_rating\":%d,\"threads\":%d,\"multipv\":%d}\n",
        stockfish_ready ? "true" : "false",
        maia_ready ? "true" : "false",
        settings->budget_ms, settings->maia_rating,
        settings->threads, settings->multipv);

    if (written < 0 || (size_t)written >= sizeof(buffer)) {
        return -1;
    }

    return publish_buffer(overlay, buffer, (size_t)written);
}

int overlay_publish_settings(Overlay *overlay, const OverlaySettings *settings)
{
    char buffer[256];
    int written;

    if (settings == NULL) {
        return -1;
    }

    written = snprintf(
        buffer, sizeof(buffer),
        "{\"type\":\"settings\",\"budget_ms\":%ld,\"maia_rating\":%d,"
        "\"threads\":%d,\"multipv\":%d}\n",
        settings->budget_ms, settings->maia_rating,
        settings->threads, settings->multipv);

    if (written < 0 || (size_t)written >= sizeof(buffer)) {
        return -1;
    }

    return publish_buffer(overlay, buffer, (size_t)written);
}

int overlay_publish_status(Overlay *overlay, const char *kind, const char *text)
{
    char buffer[512];
    size_t length = 0U;

    if (text == NULL) {
        return -1;
    }

    APPEND("{\"type\":\"status\",\"kind\":\"%s\",\"text\":\"",
           kind != NULL ? kind : "info");

    /* The only strings we ever send here are our own, but escaping quotes and
     * backslashes costs nothing and keeps a stray engine path from producing
     * invalid JSON. */
    for (const char *p = text; *p != '\0'; p += 1) {
        if (*p == '"' || *p == '\\') {
            APPEND("\\%c", *p);
        } else if ((unsigned char)*p >= 0x20U) {
            APPEND("%c", *p);
        }
    }

    APPEND("\"}\n");
    return publish_buffer(overlay, buffer, length);
}

int overlay_publish_position(Overlay *overlay, unsigned long seq,
                             const char *fen, int flip)
{
    char buffer[256];
    int written;

    if (fen == NULL) {
        return -1;
    }

    written = snprintf(
        buffer, sizeof(buffer),
        "{\"type\":\"position\",\"seq\":%lu,\"fen\":\"%s\",\"flip\":%s,"
        "\"stm\":\"%c\"}\n",
        seq, fen, flip ? "true" : "false",
        black_to_move(fen) ? 'b' : 'w');

    if (written < 0 || (size_t)written >= sizeof(buffer)) {
        return -1;
    }

    return publish_buffer(overlay, buffer, (size_t)written);
}

int overlay_publish_analysis(Overlay *overlay, unsigned long seq,
                             const char *fen, int flip, int depth, int final,
                             const UciLine *best, const char *human_move,
                             const UciLine *lines, int count)
{
    char buffer[4096];
    size_t length = 0U;
    int emitted = 0;
    int flipSign;

    if (overlay == NULL || fen == NULL) {
        return -1;
    }

    flipSign = black_to_move(fen);

    APPEND("{\"type\":\"analysis\",\"seq\":%lu,\"fen\":\"%s\",\"flip\":%s,"
           "\"stm\":\"%c\",\"depth\":%d,\"final\":%s",
           seq, fen, flip ? "true" : "false",
           flipSign ? 'b' : 'w', depth, final ? "true" : "false");

    if (best != NULL && best->move[0] != '\0') {
        APPEND(",\"best\":{\"move\":\"%s\",\"pv\":\"%s\",", best->move, best->pv);

        if (append_score_white(buffer, sizeof(buffer), &length, best, flipSign) < 0) {
            return -1;
        }

        APPEND("}");
    }

    if (human_move != NULL && *human_move != '\0') {
        APPEND(",\"human\":{\"move\":\"%s\"}", human_move);
    }

    if (lines != NULL && count > 0) {
        APPEND(",\"lines\":[");

        for (int index = 0; index < count; index += 1) {
            if (lines[index].move[0] == '\0') {
                continue;
            }

            APPEND("%s{\"move\":\"%s\",\"depth\":%d,\"pv\":\"%s\",",
                   emitted > 0 ? "," : "",
                   lines[index].move, lines[index].depth, lines[index].pv);

            if (
                append_score_white(
                    buffer, sizeof(buffer), &length, &lines[index], flipSign) < 0
            ) {
                return -1;
            }

            APPEND("}");
            emitted += 1;
        }

        APPEND("]");
    }

    APPEND("}\n");
    return publish_buffer(overlay, buffer, length);
}

#undef APPEND

/* Deliberately does NOT free the struct or close the control descriptor.
 *
 * Other threads legitimately hold this pointer -- the message loop publishes
 * board frames, the control thread is parked in read() on control_fd -- and
 * there is no cheap way to prove they have all let go. Freeing here bought a
 * use-after-free; closing control_fd under a blocked reader bought a
 * descriptor-reuse race. Both were found by ThreadSanitizer.
 *
 * Instead the struct is marked dead and outlives the shutdown: publishes now
 * fail with -1, the child is reaped, and the one small allocation plus one
 * descriptor are reclaimed by process exit. This host is one process per
 * browser session, so that costs nothing. */
void overlay_stop(Overlay *overlay)
{
    int status;

    if (overlay == NULL) {
        return;
    }

    pthread_mutex_lock(&overlay->write_lock);

    if (overlay->dead) {
        pthread_mutex_unlock(&overlay->write_lock);
        return;
    }

    overlay->dead = 1;

    if (overlay->write_fd > 0) {
        close(overlay->write_fd);
        overlay->write_fd = -1;
    }

    pthread_mutex_unlock(&overlay->write_lock);

    if (overlay->pid > 0) {
        for (int attempt = 0; attempt < 50; attempt += 1) {
            pid_t result = waitpid(overlay->pid, &status, WNOHANG);

            if (result == overlay->pid || result < 0) {
                overlay->pid = 0;
                break;
            }

            nanosleep(&(struct timespec){ .tv_nsec = 20000000L }, NULL);
        }

        if (overlay->pid > 0) {
            kill(overlay->pid, SIGTERM);
            waitpid(overlay->pid, &status, 0);
        }
    }
}
