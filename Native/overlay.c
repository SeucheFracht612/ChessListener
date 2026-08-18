#define _POSIX_C_SOURCE 200809L

#include "overlay.h"
#include "version.h"

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define CONTROL_LINE_MAX 1024U
#define OVERLAY_WRITE_TIMEOUT_MS 500L

#define MIN_BUDGET_MS   100L
#define MAX_BUDGET_MS   10000L
#define SAME_LIVE_BUDGET -1L
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

static long monotonic_ms(void)
{
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return -1L;
    }

    return now.tv_sec * 1000L + now.tv_nsec / 1000000L;
}

static int wait_writable(int fd, long deadline)
{
    for (;;) {
        struct pollfd descriptor = {
            .fd = fd,
            .events = POLLOUT,
            .revents = 0
        };
        long now = monotonic_ms();
        long remaining;
        int result;

        if (now < 0L) {
            return -1;
        }

        remaining = deadline - now;

        if (remaining <= 0L) {
            errno = ETIMEDOUT;
            return -1;
        }

        result = poll(&descriptor, 1U, (int)remaining);

        if (result > 0 && (descriptor.revents & POLLOUT) != 0) {
            return 0;
        }

        if (result == 0) {
            errno = ETIMEDOUT;
            return -1;
        }

        if (result < 0 && errno == EINTR) {
            continue;
        }

        if (result > 0) {
            errno = EPIPE;
        }

        return -1;
    }
}

static int write_all(int fd, const char *s, size_t n)
{
    long started = monotonic_ms();
    long deadline;

    if (started < 0L) {
        return -1;
    }

    deadline = started + OVERLAY_WRITE_TIMEOUT_MS;

    while (n > 0U) {
        long now = monotonic_ms();
        ssize_t written;

        if (now < 0L || now >= deadline) {
            errno = ETIMEDOUT;
            return -1;
        }

        written = write(fd, s, n);

        if (written > 0) {
            s += written;
            n -= (size_t)written;
            continue;
        }

        if (written < 0 && errno == EINTR) {
            continue;
        }

        if (
            written < 0 &&
            (errno == EAGAIN || errno == EWOULDBLOCK) &&
            wait_writable(fd, deadline) == 0
        ) {
            continue;
        }

        if (written == 0) {
            errno = EPIPE;
        }

        return -1;
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

    {
        int flags = fcntl(to_child[1], F_GETFL);

        if (flags < 0 || fcntl(to_child[1], F_SETFL, flags | O_NONBLOCK) < 0) {
            close(to_child[0]);
            close(to_child[1]);
            close(from_child[0]);
            close(from_child[1]);
            return NULL;
        }
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
        } else if (strcmp(token, "explore_budget") == 0) {
            long milliseconds = strtol(value, NULL, 10);

            settings->explore_budget_ms =
                milliseconds == SAME_LIVE_BUDGET || milliseconds == 0L
                    ? milliseconds
                    : clamp_long(milliseconds, MIN_BUDGET_MS, MAX_BUDGET_MS);
        } else if (strcmp(token, "maia") == 0) {
            int rating = (int)strtol(value, NULL, 10);

            if (rating == 0) {
                settings->maia_rating = 0;
            } else {
                rating = clamp_int(rating, MIN_MAIA_RATING, MAX_MAIA_RATING);
                settings->maia_rating = (rating / 100) * 100;
            }
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

static int payload_protocol(const char *payload, int *protocol)
{
    char work[CONTROL_LINE_MAX];
    char *save = NULL;
    char *token;

    if (payload == NULL || protocol == NULL) {
        return 0;
    }

    snprintf(work, sizeof(work), "%s", payload);

    for (
        token = strtok_r(work, " \t", &save);
        token != NULL;
        token = strtok_r(NULL, " \t", &save)
    ) {
        char *equals = strchr(token, '=');
        char *end;
        long value;

        if (equals == NULL) {
            continue;
        }

        *equals = '\0';

        if (strcmp(token, "protocol") != 0) {
            continue;
        }

        value = strtol(equals + 1, &end, 10);

        if (end == equals + 1 || *end != '\0' || value < 0L || value > 999L) {
            return 0;
        }

        *protocol = (int)value;
        return 1;
    }

    return 0;
}

int overlay_wait_for_start(Overlay *overlay, OverlaySettings *settings)
{
    char line[CONTROL_LINE_MAX];
    int status;

    if (overlay == NULL || settings == NULL) {
        return OVERLAY_START_ERROR;
    }

    /* Sensible defaults, so a payload missing a field is still usable. */
    settings->budget_ms = 400L;
    settings->explore_budget_ms = -1L;
    settings->maia_rating = 1900;
    settings->threads = 2;
    settings->multipv = 3;

    for (;;) {
        status = overlay_read_control(overlay, line, sizeof(line));

        if (status <= 0) {
            return status;
        }

        if (strcmp(line, "QUIT") == 0) {
            return OVERLAY_START_DISMISSED;
        }

        if (strncmp(line, "START", 5) == 0 &&
            (line[5] == '\0' || line[5] == ' ')) {
            int protocol;

            if (!payload_protocol(line + 5, &protocol) ||
                protocol != CHESSLISTENER_PROTOCOL_VERSION) {
                char note[128];

                snprintf(
                    note,
                    sizeof(note),
                    "Incompatible overlay protocol; expected protocol %d.",
                    CHESSLISTENER_PROTOCOL_VERSION);
                (void)overlay_publish_status(overlay, "warn", note);
                return OVERLAY_START_PROTOCOL_MISMATCH;
            }

            return overlay_parse_settings(line + 5, settings)
                ? OVERLAY_START_OK
                : OVERLAY_START_ERROR;
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
        close(overlay->write_fd);
        overlay->write_fd = -1;

        /* A full pipe means the UI event loop is no longer consuming frames.
         * Terminating it also closes the control pipe, allowing the control
         * thread to perform the normal process-wide shutdown. */
        if (overlay->pid > 0) {
            (void)kill(overlay->pid, SIGTERM);
        }

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

static int append_json_string(char *buffer, size_t capacity, size_t *length,
                              const char *text)
{
    int written;

    if (buffer == NULL || length == NULL || *length >= capacity) {
        return -1;
    }

    if (text == NULL) {
        text = "";
    }

    written = snprintf(buffer + *length, capacity - *length, "\"");

    if (written < 0 || (size_t)written >= capacity - *length) {
        return -1;
    }
    *length += (size_t)written;

    for (const char *cursor = text; *cursor != '\0'; cursor += 1) {
        unsigned char character = (unsigned char)*cursor;

        if (character == '"' || character == '\\') {
            written = snprintf(
                buffer + *length, capacity - *length, "\\%c", character);
        } else if (character < 0x20U) {
            written = snprintf(
                buffer + *length, capacity - *length,
                "\\u%04x", (unsigned int)character);
        } else {
            written = snprintf(
                buffer + *length, capacity - *length, "%c", character);
        }

        if (written < 0 || (size_t)written >= capacity - *length) {
            return -1;
        }
        *length += (size_t)written;
    }

    written = snprintf(buffer + *length, capacity - *length, "\"");

    if (written < 0 || (size_t)written >= capacity - *length) {
        return -1;
    }
    *length += (size_t)written;
    return 0;
}

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
    UciScoreBound bound = line->bound;
    const char *boundName;

    if (flipSign) {
        if (bound == UCI_SCORE_LOWERBOUND) {
            bound = UCI_SCORE_UPPERBOUND;
        } else if (bound == UCI_SCORE_UPPERBOUND) {
            bound = UCI_SCORE_LOWERBOUND;
        }
    }

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

    boundName = bound == UCI_SCORE_LOWERBOUND
        ? "lowerbound"
        : bound == UCI_SCORE_UPPERBOUND ? "upperbound" : "exact";
    written = snprintf(
        buffer + *length, capacity - *length,
        ",\"bound\":\"%s\"", boundName);
    if (written < 0 || (size_t)written >= capacity - *length) {
        return -1;
    }
    *length += (size_t)written;
    return 0;
}

static int append_last_move(char *buffer, size_t capacity, size_t *length,
                            const char *last_move)
{
    int written;

    if (last_move != NULL && *last_move != '\0') {
        written = snprintf(
            buffer + *length, capacity - *length,
            ",\"last\":\"%s\"", last_move);
    } else {
        written = snprintf(
            buffer + *length, capacity - *length, ",\"last\":null");
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
        "\"budget_ms\":%ld,\"explore_budget_ms\":%ld,"
        "\"maia_rating\":%d,\"threads\":%d,\"multipv\":%d}\n",
        stockfish_ready ? "true" : "false",
        maia_ready ? "true" : "false",
        settings->budget_ms, settings->explore_budget_ms, settings->maia_rating,
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
        "{\"type\":\"settings\",\"budget_ms\":%ld,"
        "\"explore_budget_ms\":%ld,\"maia_rating\":%d,"
        "\"threads\":%d,\"multipv\":%d}\n",
        settings->budget_ms, settings->explore_budget_ms, settings->maia_rating,
        settings->threads, settings->multipv);

    if (written < 0 || (size_t)written >= sizeof(buffer)) {
        return -1;
    }

    return publish_buffer(overlay, buffer, (size_t)written);
}

int overlay_publish_session_start(Overlay *overlay, const char *session_id,
                                  const char *label)
{
    char buffer[4096];
    size_t length = 0U;

    if (session_id == NULL || *session_id == '\0') {
        return -1;
    }

    APPEND("{\"type\":\"session\",\"event\":\"started\","
           "\"session_id\":");
    if (append_json_string(buffer, sizeof(buffer), &length, session_id) < 0) {
        return -1;
    }
    APPEND(",\"label\":");
    if (append_json_string(buffer, sizeof(buffer), &length, label) < 0) {
        return -1;
    }
    APPEND("}\n");
    return publish_buffer(overlay, buffer, length);
}

int overlay_publish_session_end(Overlay *overlay, const char *reason)
{
    char buffer[512];
    size_t length = 0U;

    APPEND("{\"type\":\"session\",\"event\":\"ended\",\"reason\":");
    if (append_json_string(buffer, sizeof(buffer), &length, reason) < 0) {
        return -1;
    }
    APPEND("}\n");
    return publish_buffer(overlay, buffer, length);
}

int overlay_publish_game_record(Overlay *overlay, const char *initial_fen,
                                const char *uci_moves, size_t move_count,
                                const char *result)
{
    char buffer[65536];
    size_t length = 0U;

    if (initial_fen == NULL || uci_moves == NULL) {
        return -1;
    }
    APPEND("{\"type\":\"game_record\",\"initial_fen\":");
    if (append_json_string(buffer, sizeof(buffer), &length, initial_fen) < 0) {
        return -1;
    }
    APPEND(",\"uci_moves\":");
    if (append_json_string(buffer, sizeof(buffer), &length, uci_moves) < 0) {
        return -1;
    }
    APPEND(",\"move_count\":%zu,\"result\":", move_count);
    if (append_json_string(buffer, sizeof(buffer), &length,
                           result != NULL ? result : "*") < 0) {
        return -1;
    }
    APPEND("}\n");
    return publish_buffer(overlay, buffer, length);
}

int overlay_publish_recovery(Overlay *overlay, const char *action,
                             const char *text)
{
    char buffer[768];
    size_t length = 0U;

    if (action == NULL || *action == '\0') {
        return -1;
    }

    APPEND("{\"type\":\"recovery\",\"action\":");
    if (append_json_string(buffer, sizeof(buffer), &length, action) < 0) {
        return -1;
    }
    APPEND(",\"text\":");
    if (append_json_string(buffer, sizeof(buffer), &length, text) < 0) {
        return -1;
    }
    APPEND("}\n");
    return publish_buffer(overlay, buffer, length);
}

int overlay_publish_recovery_result(Overlay *overlay, const char *action,
                                    int accepted, const char *text)
{
    char buffer[1024];
    size_t length = 0U;

    if (action == NULL || *action == '\0') {
        return -1;
    }

    APPEND("{\"type\":\"recovery\",\"action\":");
    if (append_json_string(buffer, sizeof(buffer), &length, action) < 0) {
        return -1;
    }
    APPEND(",\"accepted\":%s,\"ok\":%s,\"kind\":\"%s\",\"text\":",
           accepted ? "true" : "false",
           accepted ? "true" : "false",
           accepted ? "info" : "warn");
    if (append_json_string(buffer, sizeof(buffer), &length, text) < 0) {
        return -1;
    }
    APPEND("}\n");
    return publish_buffer(overlay, buffer, length);
}

int overlay_publish_orientation(Overlay *overlay, int flip)
{
    char buffer[80];
    int written = snprintf(
        buffer, sizeof(buffer),
        "{\"type\":\"orientation\",\"flip\":%s}\n",
        flip ? "true" : "false");

    if (written < 0 || (size_t)written >= sizeof(buffer)) {
        return -1;
    }

    return publish_buffer(overlay, buffer, (size_t)written);
}

int overlay_publish_explore(Overlay *overlay, const char *event,
                            const char *action, const char *reason,
                            const char *text,
                            unsigned long long branch_id,
                            unsigned int node_id,
                            const char *fen, const char *last_move)
{
    char buffer[1536];
    size_t length = 0U;

    if (event == NULL || *event == '\0') {
        return -1;
    }

    APPEND("{\"type\":\"explore\",\"event\":");
    if (append_json_string(buffer, sizeof(buffer), &length, event) < 0) {
        return -1;
    }
    if (action != NULL && *action != '\0') {
        APPEND(",\"action\":");
        if (append_json_string(buffer, sizeof(buffer), &length, action) < 0) {
            return -1;
        }
    }
    if (reason != NULL && *reason != '\0') {
        APPEND(",\"reason\":");
        if (append_json_string(buffer, sizeof(buffer), &length, reason) < 0) {
            return -1;
        }
    }
    if (text != NULL && *text != '\0') {
        APPEND(",\"text\":");
        if (append_json_string(buffer, sizeof(buffer), &length, text) < 0) {
            return -1;
        }
    }
    if (branch_id != 0ULL) {
        APPEND(",\"branch_id\":%llu,\"node_id\":%u", branch_id, node_id);
    }
    if (fen != NULL && *fen != '\0') {
        APPEND(",\"fen\":");
        if (append_json_string(buffer, sizeof(buffer), &length, fen) < 0) {
            return -1;
        }
    }
    if (last_move != NULL && *last_move != '\0') {
        APPEND(",\"last\":");
        if (append_json_string(buffer, sizeof(buffer), &length, last_move) < 0) {
            return -1;
        }
    } else if (fen != NULL) {
        APPEND(",\"last\":null");
    }
    APPEND("}\n");
    return publish_buffer(overlay, buffer, length);
}

int overlay_publish_live_update(Overlay *overlay, unsigned long live_revision,
                                const char *fen, int flip,
                                const char *last_move, const char *source,
                                int synchronising, const char *text)
{
    char buffer[1024];
    size_t length = 0U;

    if (fen == NULL || source == NULL || *source == '\0') {
        return -1;
    }

    APPEND("{\"type\":\"live_update\",\"live_revision\":%lu,"
           "\"fen\":\"%s\",\"flip\":%s,\"stm\":\"%c\",\"source\":",
           live_revision, fen, flip ? "true" : "false",
           black_to_move(fen) ? 'b' : 'w');
    if (append_json_string(buffer, sizeof(buffer), &length, source) < 0) {
        return -1;
    }
    if (append_last_move(buffer, sizeof(buffer), &length, last_move) < 0) {
        return -1;
    }
    APPEND(",\"synchronising\":%s,\"text\":",
           synchronising ? "true" : "false");
    if (append_json_string(
            buffer, sizeof(buffer), &length, text != NULL ? text : "") < 0) {
        return -1;
    }
    APPEND("}\n");
    return publish_buffer(overlay, buffer, length);
}

int overlay_publish_state(Overlay *overlay, unsigned long seq,
                          unsigned long live_revision,
                          const char *source, int synchronising,
                          const char *text)
{
    char buffer[768];
    size_t length = 0U;

    if (source == NULL || *source == '\0') {
        return -1;
    }

    APPEND("{\"type\":\"state\",\"seq\":%lu,"
           "\"target_revision\":%lu,\"state_revision\":%lu,"
           "\"live_revision\":%lu,\"mode\":\"live\",\"source\":",
           seq, seq, seq, live_revision);
    if (append_json_string(buffer, sizeof(buffer), &length, source) < 0) {
        return -1;
    }
    APPEND(",\"synchronising\":%s,\"text\":",
           synchronising ? "true" : "false");
    if (append_json_string(
            buffer, sizeof(buffer), &length, text != NULL ? text : "") < 0) {
        return -1;
    }
    APPEND("}\n");
    return publish_buffer(overlay, buffer, length);
}

int overlay_publish_status(Overlay *overlay, const char *kind, const char *text)
{
    char buffer[512];
    size_t length = 0U;

    if (text == NULL) {
        return -1;
    }

    APPEND("{\"type\":\"status\",\"kind\":\"%s\",\"text\":",
           kind != NULL ? kind : "info");

    if (append_json_string(buffer, sizeof(buffer), &length, text) < 0) {
        return -1;
    }

    APPEND("}\n");
    return publish_buffer(overlay, buffer, length);
}

int overlay_publish_position(Overlay *overlay, unsigned long seq,
                             const char *fen, int flip, const char *last_move,
                             const char *source, const char *mode,
                             unsigned long live_revision,
                             unsigned long long branch_id,
                             unsigned int node_id)
{
    char buffer[384];
    size_t length = 0U;

    if (fen == NULL || source == NULL || *source == '\0' || mode == NULL) {
        return -1;
    }

    APPEND("{\"type\":\"position\",\"seq\":%lu,"
           "\"target_revision\":%lu,\"state_revision\":%lu,"
           "\"live_revision\":%lu,\"mode\":\"%s\","
           "\"fen\":\"%s\",\"flip\":%s,\"stm\":\"%c\",\"source\":",
           seq, seq, seq, live_revision, mode, fen, flip ? "true" : "false",
           black_to_move(fen) ? 'b' : 'w');
    if (append_json_string(buffer, sizeof(buffer), &length, source) < 0) {
        return -1;
    }
    if (append_last_move(buffer, sizeof(buffer), &length, last_move) < 0) {
        return -1;
    }
    if (strcmp(mode, "explore") == 0) {
        APPEND(",\"branch_id\":%llu,\"node_id\":%u", branch_id, node_id);
    }

    APPEND("}\n");
    return publish_buffer(overlay, buffer, length);
}

int overlay_publish_analysis(Overlay *overlay, unsigned long seq,
                             const char *fen, int flip, const char *last_move,
                             int depth, int final, const UciLine *best,
                             const char *human_move, const UciLine *lines,
                             int count, const char *source, const char *mode,
                             unsigned long live_revision,
                             unsigned long long branch_id,
                             unsigned int node_id)
{
    char buffer[4096];
    size_t length = 0U;
    int emitted = 0;
    int flipSign;

    if (overlay == NULL || fen == NULL || source == NULL || *source == '\0' ||
        mode == NULL) {
        return -1;
    }

    flipSign = black_to_move(fen);

    APPEND("{\"type\":\"analysis\",\"seq\":%lu,"
           "\"target_revision\":%lu,\"state_revision\":%lu,"
           "\"live_revision\":%lu,\"mode\":\"%s\","
           "\"fen\":\"%s\",\"flip\":%s,\"stm\":\"%c\","
           "\"depth\":%d,\"final\":%s,\"source\":",
           seq, seq, seq, live_revision, mode, fen, flip ? "true" : "false",
           flipSign ? 'b' : 'w', depth, final ? "true" : "false");
    if (append_json_string(buffer, sizeof(buffer), &length, source) < 0) {
        return -1;
    }
    if (append_last_move(buffer, sizeof(buffer), &length, last_move) < 0) {
        return -1;
    }
    if (strcmp(mode, "explore") == 0) {
        APPEND(",\"branch_id\":%llu,\"node_id\":%u", branch_id, node_id);
    }

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
