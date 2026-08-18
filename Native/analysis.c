#define _POSIX_C_SOURCE 200809L

#include "analysis.h"
#include "overlay.h"
#include "uci.h"

#include <limits.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

#define BASE_MAX     1024
#define FEN_MAX      128
#define MOVE_MAX     8
#define SESSION_ID_MAX 128
#define STOCKFISH_POLL_MS 20 /* how fast the fast lane notices a new board */
#define MAIA_POLL_MS      20 /* bounds supersession latency in Maia's lane */
#define PUSH_EVERY_MS     80 /* first info is immediate; later redraws cap */
#define MAIA_DEADLINE_MS 1500
#define MAIA_ABORT_MS     250
#define SOURCE_MAX         16
#define SYNC_TEXT_MAX     192

typedef enum {
    ANALYSIS_MODE_LIVE = 0,
    ANALYSIS_MODE_EXPLORE = 1
} AnalysisMode;

static UciEngine *g_stockfish;
static UciEngine *g_maia;
static Overlay   *g_overlay;
static FILE      *g_log;
static char       g_base[BASE_MAX];

/* ---- shared state, guarded by g_lock ---- */
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_wake = PTHREAD_COND_INITIALIZER;

static char          g_fen[FEN_MAX];
static char          g_last_move[MOVE_MAX];
static char          g_source[SOURCE_MAX];
static int           g_flip;
static unsigned long g_seq;        /* newest position handed in           */
static AnalysisMode  g_mode;
static unsigned long long g_branch_id;
static unsigned int  g_node_id;

/* Browser-owned live state advances even while the engines are intentionally
 * focused on an Analysis Lab node.  It is never reconstructed from a branch. */
static char          g_live_fen[FEN_MAX];
static char          g_live_last_move[MOVE_MAX];
static char          g_live_source[SOURCE_MAX];
static int           g_live_flip;
static unsigned long g_live_revision;
static int           g_live_synchronising;
static char          g_live_sync_text[SYNC_TEXT_MAX];
static unsigned long g_stockfish_taken;
static unsigned long g_maia_taken;
static OverlaySettings g_settings;
static int           g_stockfish_options_dirty;
static int           g_maia_reload;
static int           g_restart_stockfish;
static int           g_restart_maia;
static int           g_stockfish_ready;
static int           g_maia_ready;
static int           g_session_active;
static char          g_session_id[SESSION_ID_MAX + 1];
static int           g_synchronising;
static char          g_sync_text[SYNC_TEXT_MAX];
static int           g_quit;

/* Both engine lanes merge into this revision-keyed cache. A late Maia result
 * therefore republishes the already-computed Stockfish lines instead of
 * clearing them in the overlay; conversely, every later Stockfish frame keeps
 * an early Maia result. The cache is reset atomically when g_seq advances. */
typedef struct {
    unsigned long seq;
    char fen[FEN_MAX];
    char last_move[MOVE_MAX];
    char source[SOURCE_MAX];
    int flip;
    AnalysisMode mode;
    unsigned long live_revision;
    unsigned long long branch_id;
    unsigned int node_id;
    UciLine lines[UCI_LINES_MAX];
    int line_count;
    int depth;
    int stockfish_done;
    int maia_done;
    char human[MOVE_MAX];
} RevisionResult;

static RevisionResult g_result;

/* Registered before the controller thread starts and immutable afterwards. */
static AnalysisEventSink g_event_sink;
static void             *g_event_context;

/* Serialises the board-frame write with committing that position to the
 * worker. This guarantees that an analysis frame can never overtake its board
 * frame, even if a future caller publishes from more than one thread. */
static pthread_mutex_t g_publish_lock = PTHREAD_MUTEX_INITIALIZER;

static pthread_t g_stockfish_worker;
static pthread_t g_maia_worker;
static pthread_t g_controller;
static int       g_stockfish_worker_live;
static int       g_maia_worker_live;

/* Shutdown runs exactly once, and a second caller waits for the first to
 * finish rather than racing it. Both the message loop (browser pipe closed)
 * and the control thread (overlay window closed) can arrive here, and in
 * practice they often arrive together. */
static pthread_mutex_t g_stop_lock = PTHREAD_MUTEX_INITIALIZER;
static int             g_stopped;

/* Once their worker exists, each UCI process has exactly one owner. No engine
 * object or its pipe state is ever touched by the other lane. */
static void StopStockfish(void)
{
    if (g_stockfish != NULL) {
        uci_stop(g_stockfish);
        g_stockfish = NULL;
    }
}

static void StopMaia(void)
{
    if (g_maia != NULL) {
        uci_stop(g_maia);
        g_maia = NULL;
    }
}

static void Log(const char *format, ...)
{
    va_list arguments;

    if (g_log == NULL) {
        return;
    }

    va_start(arguments, format);
    vfprintf(g_log, format, arguments);
    va_end(arguments);
    fputc('\n', g_log);
    fflush(g_log);
}

static int MatchCurrentSession(const char *token, size_t length,
                               char output[SESSION_ID_MAX + 1])
{
    int matches;

    if (token == NULL || length == 0U || length > SESSION_ID_MAX) {
        return 0;
    }

    pthread_mutex_lock(&g_lock);
    matches =
        g_session_active &&
        strlen(g_session_id) == length &&
        memcmp(g_session_id, token, length) == 0;

    if (matches) {
        memcpy(output, token, length);
        output[length] = '\0';
    }

    pthread_mutex_unlock(&g_lock);
    return matches;
}

static int SessionIsInactive(void)
{
    int inactive;

    pthread_mutex_lock(&g_lock);
    inactive = !g_session_active;
    pthread_mutex_unlock(&g_lock);
    return inactive;
}

static int ParseScopedControl(const char *line, const char *command,
                              char sessionId[SESSION_ID_MAX + 1])
{
    size_t commandLength;
    const char *token;

    if (line == NULL || command == NULL) {
        return 0;
    }

    commandLength = strlen(command);

    if (strncmp(line, command, commandLength) != 0 ||
        line[commandLength] != ' ') {
        return 0;
    }

    token = line + commandLength + 1U;

    if (*token == '\0' || strchr(token, ' ') != NULL) {
        return 0;
    }

    return MatchCurrentSession(token, strlen(token), sessionId);
}

static int ParseScopedFen(const char *line,
                          char sessionId[SESSION_ID_MAX + 1],
                          const char **fen)
{
    const char *token;
    const char *space;

    if (line == NULL || fen == NULL || strncmp(line, "FEN ", 4) != 0) {
        return 0;
    }

    token = line + 4;
    space = strchr(token, ' ');

    if (space == NULL || space == token || space[1] == '\0' ||
        !MatchCurrentSession(
            token, (size_t)(space - token), sessionId)) {
        return 0;
    }

    *fen = space + 1;
    return 1;
}

static int ParseScopedPayload(const char *line, const char *command,
                              char sessionId[SESSION_ID_MAX + 1],
                              const char **payload)
{
    size_t commandLength;
    const char *token;
    const char *space;

    if (line == NULL || command == NULL || payload == NULL) {
        return 0;
    }

    commandLength = strlen(command);
    if (strncmp(line, command, commandLength) != 0 ||
        line[commandLength] != ' ') {
        return 0;
    }

    token = line + commandLength + 1U;
    space = strchr(token, ' ');
    if (space == NULL || space == token || space[1] == '\0' ||
        !MatchCurrentSession(token, (size_t)(space - token), sessionId)) {
        return 0;
    }

    *payload = space + 1;
    return 1;
}

static void EmitEvent(const char *kind, const char *name, const char *payload,
                      const char *sessionId)
{
    if (g_event_sink != NULL) {
        g_event_sink(
            kind,
            name,
            payload,
            sessionId != NULL && *sessionId != '\0' ? sessionId : NULL,
            g_event_context);
    }
}

static long NowMs(void)
{
    struct timespec ts;

    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
}

static const char *StateSource(const char *source)
{
    if (source != NULL &&
        (strcmp(source, "exact") == 0 ||
         strcmp(source, "manual") == 0 ||
         strcmp(source, "inferred") == 0)) {
        return source;
    }

    return "inferred";
}

static long MaiaDeadlineMs(void)
{
    const char *value = getenv("CHESSLISTENER_MAIA_DEADLINE_MS");
    char *end = NULL;
    long parsed;

    if (value == NULL || *value == '\0') {
        return MAIA_DEADLINE_MS;
    }

    parsed = strtol(value, &end, 10);
    if (end == value || *end != '\0') {
        return MAIA_DEADLINE_MS;
    }

    if (parsed < 100L) parsed = 100L;
    if (parsed > 10000L) parsed = 10000L;
    return parsed;
}

/* ------------------------------------------------------------ discovery -- */

static int BaseDirectory(char *output, size_t outputSize)
{
    char path[PATH_MAX];
    ssize_t length = readlink("/proc/self/exe", path, sizeof(path) - 1U);
    char *lastSlash;

    if (length <= 0) {
        return 0;
    }

    path[length] = '\0';
    lastSlash = strrchr(path, '/');

    if (lastSlash == NULL) {
        return 0;
    }

    *lastSlash = '\0';

    if (strlen(path) >= outputSize) {
        return 0;
    }

    memcpy(output, path, strlen(path) + 1U);
    return 1;
}

static const char *EnvOrDefault(const char *name, const char *fallback)
{
    const char *value = getenv(name);
    return value != NULL && *value != '\0' ? value : fallback;
}

static int Executable(const char *path)
{
    return path != NULL && access(path, X_OK) == 0;
}

static int Readable(const char *path)
{
    return path != NULL && access(path, R_OK) == 0;
}

/* ------------------------------------------------------------- engines -- */

static void StartStockfish(const OverlaySettings *settings)
{
    UciConfig config;
    const char *path = EnvOrDefault("CHESSLISTENER_STOCKFISH", NULL);

    if (path == NULL) {
        path = Executable("/usr/games/stockfish")
            ? "/usr/games/stockfish"
            : "stockfish";
    }

    memset(&config, 0, sizeof(config));
    config.exe = path;
    config.threads = settings->threads;
    config.multipv = settings->multipv;

    /* The limit fields are unused for Stockfish now: the search runs
     * "go infinite" and this module owns the deadline, which is what makes a
     * live strength change take effect without restarting anything. */
    config.limit = UCI_LIMIT_MOVETIME;
    config.limit_value = 1000;
    config.timeout_ms = 5000;
    config.startup_ms = 10000;

    g_stockfish = uci_start(&config);

    Log(
        g_stockfish != NULL
            ? "analysis: stockfish ready (%s, %d threads, multipv %d)"
            : "analysis: stockfish unavailable (%s)",
        path, settings->threads, settings->multipv);
}

static void StartMaia(int rating)
{
    UciConfig config;
    char lc0Default[PATH_MAX];
    char netDefault[PATH_MAX];
    char libraryDirectory[PATH_MAX];
    const char *lc0;
    const char *net;

    if (rating == 0) {
        Log("analysis: Maia disabled by user");
        return;
    }

    snprintf(lc0Default, sizeof(lc0Default), "%s/Engine/lc0", g_base);
    snprintf(netDefault, sizeof(netDefault),
             "%s/Engine/maia-chess/maia_weights/maia-%d.pb.gz", g_base, rating);

    lc0 = EnvOrDefault("CHESSLISTENER_LC0", lc0Default);
    net = EnvOrDefault("CHESSLISTENER_MAIA_NET", netDefault);

    if (!Executable(lc0)) {
        Log("analysis: lc0 not found at %s", lc0);
        return;
    }

    if (!Readable(net)) {
        Log("analysis: Maia weights not readable at %s", net);
        return;
    }

    snprintf(libraryDirectory, sizeof(libraryDirectory), "%s/Engine/lib", g_base);

    if (access(libraryDirectory, X_OK) == 0) {
        setenv("LD_LIBRARY_PATH", libraryDirectory, 1);
    }

    memset(&config, 0, sizeof(config));
    config.exe = lc0;
    config.weights = net;
    config.backend = getenv("CHESSLISTENER_LC0_BACKEND");
    config.threads = 1;
    config.multipv = 1;
    config.limit = UCI_LIMIT_NODES;
    config.limit_value = 1;
    config.timeout_ms = 5000;
    config.startup_ms = 60000;

    g_maia = uci_start(&config);

    Log(
        g_maia != NULL
            ? "analysis: Maia %d ready (%s)"
            : "analysis: Maia %d failed to start (%s)",
        rating, net);
}

static int StartOverlay(void)
{
    char scriptDefault[PATH_MAX];
    const char *script;

    snprintf(scriptDefault, sizeof(scriptDefault), "%s/overlay.py", g_base);
    script = EnvOrDefault("CHESSLISTENER_OVERLAY", scriptDefault);

    if (!Readable(script)) {
        Log("analysis: overlay script missing at %s", script);
        return 0;
    }

    g_overlay = overlay_start(script);

    if (g_overlay == NULL) {
        Log("analysis: overlay failed to start (%s)", script);
        return 0;
    }

    return 1;
}

static void DisableStockfish(const char *operation, int status)
{
    Log(
        "analysis: Stockfish %s failed (%d); disabling the engine",
        operation,
        status);

    StopStockfish();

    pthread_mutex_lock(&g_lock);
    g_stockfish_ready = 0;
    pthread_mutex_unlock(&g_lock);

    (void)overlay_publish_status(
        g_overlay, "warn", "Stockfish stopped after an engine error.");
}

static void DisableMaia(const char *operation, int status)
{
    Log(
        "analysis: Maia %s failed (%d); disabling the engine",
        operation,
        status);

    StopMaia();

    pthread_mutex_lock(&g_lock);
    g_maia_ready = 0;
    pthread_mutex_unlock(&g_lock);

    (void)overlay_publish_status(
        g_overlay, "warn", "Maia stopped after an engine error.");
}

/* ---------------------------------------------------------- worker lanes -- */

static void OverlayGone(void);

static void PublishReadyState(void)
{
    OverlaySettings settings;
    int stockfishReady;
    int maiaReady;
    int publish;

    pthread_mutex_lock(&g_lock);
    settings = g_settings;
    stockfishReady = g_stockfish_ready;
    maiaReady = g_maia_ready;
    publish = !g_quit;
    pthread_mutex_unlock(&g_lock);

    if (publish && overlay_publish_ready(
            g_overlay, stockfishReady, maiaReady, &settings) != 0) {
        OverlayGone();
    }
}

static int StockfishSuperseded(unsigned long mine)
{
    int superseded;

    pthread_mutex_lock(&g_lock);
    superseded =
        g_quit || !g_session_active || g_seq != mine ||
        g_stockfish_options_dirty || g_restart_stockfish;
    pthread_mutex_unlock(&g_lock);
    return superseded;
}

static int MaiaSuperseded(unsigned long mine)
{
    int superseded;

    pthread_mutex_lock(&g_lock);
    superseded =
        g_quit || !g_session_active || g_seq != mine ||
        g_maia_reload || g_restart_maia;
    pthread_mutex_unlock(&g_lock);
    return superseded;
}

/* The overlay went away. Only raise the flag here -- teardown belongs to
 * whichever thread reaches AnalysisStop. */
static void OverlayGone(void)
{
    Log("analysis: overlay closed");
    pthread_mutex_lock(&g_lock);
    g_quit = 1;
    pthread_cond_broadcast(&g_wake);
    pthread_mutex_unlock(&g_lock);
}

static void PublishAnalysis(unsigned long seq)
{
    RevisionResult snapshot;
    int current;

    /* Lifecycle frames and position commits use the same lock. Re-check after
     * taking it so an evaluation that raced a session end can never appear
     * after the frame that cleared it. */
    pthread_mutex_lock(&g_publish_lock);
    pthread_mutex_lock(&g_lock);
    current =
        g_session_active && g_seq == seq && g_result.seq == seq && !g_quit;
    if (current) {
        snapshot = g_result;
    }
    pthread_mutex_unlock(&g_lock);

    if (current &&
        overlay_publish_analysis(
            g_overlay, seq, snapshot.fen, snapshot.flip, snapshot.last_move,
            snapshot.depth, snapshot.stockfish_done,
            snapshot.line_count > 0 ? &snapshot.lines[0] : NULL,
            snapshot.human[0] != '\0' ? snapshot.human : NULL,
            snapshot.line_count > 0 ? snapshot.lines : NULL,
            snapshot.line_count, snapshot.source,
            snapshot.mode == ANALYSIS_MODE_EXPLORE ? "explore" : "live",
            snapshot.live_revision, snapshot.branch_id, snapshot.node_id) != 0) {
        pthread_mutex_unlock(&g_publish_lock);
        OverlayGone();
        return;
    }

    pthread_mutex_unlock(&g_publish_lock);
}

static int CopyStockfishLines(UciLine output[UCI_LINES_MAX], int *depth)
{
    const UciLine *lines;
    int count = 0;

    if (depth != NULL) {
        *depth = g_stockfish != NULL ? uci_depth(g_stockfish) : 0;
    }

    lines = g_stockfish != NULL ? uci_lines(g_stockfish, &count) : NULL;
    if (count < 0) count = 0;
    if (count > UCI_LINES_MAX) count = UCI_LINES_MAX;
    if (count > 0 && lines != NULL) {
        memcpy(output, lines, (size_t)count * sizeof(*output));
    }
    return count;
}

static void CommitStockfish(unsigned long seq,
                            const UciLine *lines, int count,
                            int depth, int done)
{
    int current;

    if (count < 0) count = 0;
    if (count > UCI_LINES_MAX) count = UCI_LINES_MAX;

    pthread_mutex_lock(&g_lock);
    current =
        !g_quit && g_session_active && g_seq == seq && g_result.seq == seq;
    if (current) {
        memset(g_result.lines, 0, sizeof(g_result.lines));
        if (count > 0 && lines != NULL) {
            memcpy(g_result.lines, lines,
                   (size_t)count * sizeof(g_result.lines[0]));
        }
        g_result.line_count = count;
        g_result.depth = depth;
        g_result.stockfish_done = done != 0;
    }
    pthread_mutex_unlock(&g_lock);

    if (current) {
        PublishAnalysis(seq);
    }
}

static void CommitMaia(unsigned long seq, const char *human)
{
    int current;

    pthread_mutex_lock(&g_lock);
    current =
        !g_quit && g_session_active && g_seq == seq && g_result.seq == seq;
    if (current) {
        snprintf(g_result.human, sizeof(g_result.human), "%s",
                 human != NULL ? human : "");
        g_result.maia_done = 1;
    }
    pthread_mutex_unlock(&g_lock);

    if (current) {
        PublishAnalysis(seq);
    }
}

static void AnalyseStockfish(unsigned long seq, const char *fen, long budgetMs)
{
    long started;
    long lastPush;
    int status;

    if (StockfishSuperseded(seq)) {
        return;
    }

    if (g_stockfish == NULL) {
        CommitStockfish(seq, NULL, 0, 0, 1);
        return;
    }

    status = uci_search_begin(g_stockfish, fen);

    if (status != 0) {
        DisableStockfish("search start", status);
        CommitStockfish(seq, NULL, 0, 0, 1);
        return;
    }

    started = NowMs();
    lastPush = 0L;

    for (;;) {
        int updated = 0;
        int finished = 0;
        long now;

        status = uci_search_poll(
            g_stockfish, STOCKFISH_POLL_MS, &updated, &finished);

        if (status == -4) {                       /* mate or stalemate */
            CommitStockfish(seq, NULL, 0, 0, 1);
            return;
        }

        if (status != 0) {
            UciLine lines[UCI_LINES_MAX];
            int depth;
            int count = CopyStockfishLines(lines, &depth);

            /* A protocol/read error (-1) used to fall through and immediately
             * poll again forever. Any non-terminal error makes the stream
             * unsafe to reuse; stop it and publish a final engine-less frame. */
            DisableStockfish("search poll", status);
            CommitStockfish(seq, lines, count, depth, 1);
            return;
        }

        now = NowMs();

        if (StockfishSuperseded(seq)) {
            int abortStatus;

            /* The board has moved on. Kill the search now; the outer loop
             * picks up the newer position on its next turn. */
            abortStatus = uci_search_abort(g_stockfish);

            if (abortStatus != 0) {
                DisableStockfish("search abort", abortStatus);
            }

            return;
        }

        if (finished) {
            UciLine lines[UCI_LINES_MAX];
            int depth;
            int count = CopyStockfishLines(lines, &depth);

            CommitStockfish(seq, lines, count, depth, 1);
            return;
        }

        if (budgetMs > 0L && now - started >= budgetMs) {
            UciLine lines[UCI_LINES_MAX];
            int depth;
            int count;
            int abortStatus = uci_search_abort(g_stockfish);

            count = CopyStockfishLines(lines, &depth);

            if (abortStatus != 0) {
                DisableStockfish("search deadline abort", abortStatus);
                depth = 0;
            }

            CommitStockfish(seq, lines, count, depth, 1);
            return;
        }

        if (updated && now - lastPush >= PUSH_EVERY_MS) {
            UciLine lines[UCI_LINES_MAX];
            int depth;
            int count;

            lastPush = now;
            count = CopyStockfishLines(lines, &depth);
            CommitStockfish(seq, lines, count, depth, 0);
        }
    }
}

static void AnalyseMaia(unsigned long seq, const char *fen)
{
    char human[MOVE_MAX] = "";
    long started;
    long deadlineMs = MaiaDeadlineMs();
    int status;

    if (MaiaSuperseded(seq)) {
        return;
    }

    if (g_maia == NULL) {
        return;
    }

    status = uci_analyse_begin(g_maia, fen);
    if (status != 0) {
        DisableMaia("search start", status);
        CommitMaia(seq, NULL);
        return;
    }

    started = NowMs();

    for (;;) {
        int updated = 0;
        int finished = 0;
        long now;

        status = uci_search_poll(
            g_maia, MAIA_POLL_MS, &updated, &finished);
        (void)updated;

        if (status != 0 && status != -4) {
            DisableMaia("query", status);
            CommitMaia(seq, NULL);
            return;
        }

        if (MaiaSuperseded(seq)) {
            int abortStatus = uci_search_abort_timeout(g_maia, MAIA_ABORT_MS);

            if (abortStatus != 0) {
                DisableMaia("supersession abort", abortStatus);
                pthread_mutex_lock(&g_lock);
                if (!g_quit) {
                    g_restart_maia = 1;
                    pthread_cond_broadcast(&g_wake);
                }
                pthread_mutex_unlock(&g_lock);
            }
            return;
        }

        if (status == -4) {
            CommitMaia(seq, NULL);
            return;
        }

        if (finished) {
            const UciLine *lines;
            int count = 0;

            lines = uci_lines(g_maia, &count);
            if (count > 0 && lines != NULL) {
                snprintf(human, sizeof(human), "%s", lines[0].move);
            }
            CommitMaia(seq, human);
            return;
        }

        now = NowMs();
        if (now - started >= deadlineMs) {
            int abortStatus =
                uci_search_abort_timeout(g_maia, MAIA_ABORT_MS);

            Log("analysis: Maia query exceeded %ld ms", deadlineMs);
            if (abortStatus != 0) {
                DisableMaia("deadline abort", abortStatus);
            }
            CommitMaia(seq, NULL);
            return;
        }
    }
}

static void ApplyStockfishOptions(const OverlaySettings *settings)
{
    char value[16];
    int status;

    if (g_stockfish == NULL) {
        return;
    }

    status = uci_search_abort(g_stockfish);

    if (status != 0) {
        DisableStockfish("option-change abort", status);
        return;
    }

    snprintf(value, sizeof(value), "%d", settings->threads);

    status = uci_set_option(g_stockfish, "Threads", value);

    if (status != 0) {
        DisableStockfish("Threads option", status);
        return;
    }

    status = uci_set_multipv(g_stockfish, settings->multipv);

    if (status != 0) {
        DisableStockfish("MultiPV option", status);
    }
}

static void ReloadMaia(int rating)
{
    char note[64];

    StopMaia();

    if (rating == 0) {
        pthread_mutex_lock(&g_lock);
        g_maia_ready = 0;
        pthread_mutex_unlock(&g_lock);
        PublishReadyState();
        (void)overlay_publish_status(g_overlay, "info", "Maia disabled.");
        return;
    }

    snprintf(note, sizeof(note), "Loading Maia %d\u2026", rating);
    (void)overlay_publish_status(g_overlay, "info", note);

    StartMaia(rating);

    pthread_mutex_lock(&g_lock);
    g_maia_ready = g_maia != NULL;
    pthread_mutex_unlock(&g_lock);
    PublishReadyState();

    if (g_maia == NULL) {
        snprintf(note, sizeof(note), "Maia %d unavailable.", rating);
        (void)overlay_publish_status(g_overlay, "warn", note);
    } else {
        (void)overlay_publish_status(g_overlay, "info", "");
    }
}

static void *StockfishWorkerThread(void *unused)
{
    (void)unused;

    for (;;) {
        char fen[FEN_MAX];
        unsigned long seq;
        int dirty;
        int restart;
        int active;
        AnalysisMode mode;
        OverlaySettings settings;

        pthread_mutex_lock(&g_lock);

        while (
            !g_quit &&
            g_seq == g_stockfish_taken &&
            !g_stockfish_options_dirty &&
            !g_restart_stockfish
        ) {
            pthread_cond_wait(&g_wake, &g_lock);
        }

        if (g_quit) {
            pthread_mutex_unlock(&g_lock);
            break;
        }

        dirty = g_stockfish_options_dirty;
        restart = g_restart_stockfish;
        g_stockfish_options_dirty = 0;
        g_restart_stockfish = 0;
        settings = g_settings;
        seq = g_seq;
        active = g_session_active;
        mode = g_mode;
        memcpy(fen, g_fen, sizeof(fen));
        g_stockfish_taken = seq;

        pthread_mutex_unlock(&g_lock);

        if (restart) {
            StopStockfish();
            StartStockfish(&settings);

            pthread_mutex_lock(&g_lock);
            g_stockfish_ready = g_stockfish != NULL;
            pthread_mutex_unlock(&g_lock);
            PublishReadyState();
        }

        if (dirty && !restart) {
            ApplyStockfishOptions(&settings);
        }

        if (dirty) {
            (void)overlay_publish_settings(g_overlay, &settings);
        }

        if (active && fen[0] != '\0') {
            long budget = settings.budget_ms;

            if (mode == ANALYSIS_MODE_EXPLORE &&
                settings.explore_budget_ms >= 0L) {
                budget = settings.explore_budget_ms;
            }
            AnalyseStockfish(seq, fen, budget);
        }
    }

    StopStockfish();
    return NULL;
}

static void *MaiaWorkerThread(void *unused)
{
    OverlaySettings initialSettings;

    (void)unused;

    pthread_mutex_lock(&g_lock);
    initialSettings = g_settings;
    pthread_mutex_unlock(&g_lock);

    /* lc0 and its network can take noticeably longer to initialise than
     * Stockfish. It now starts entirely off the fast lane. */
    ReloadMaia(initialSettings.maia_rating);

    for (;;) {
        char fen[FEN_MAX];
        unsigned long seq;
        int reload;
        int restart;
        int active;
        OverlaySettings settings;

        pthread_mutex_lock(&g_lock);

        while (
            !g_quit &&
            g_seq == g_maia_taken &&
            !g_maia_reload &&
            !g_restart_maia
        ) {
            pthread_cond_wait(&g_wake, &g_lock);
        }

        if (g_quit) {
            pthread_mutex_unlock(&g_lock);
            break;
        }

        reload = g_maia_reload;
        restart = g_restart_maia;
        g_maia_reload = 0;
        g_restart_maia = 0;
        settings = g_settings;
        seq = g_seq;
        active = g_session_active;
        memcpy(fen, g_fen, sizeof(fen));
        g_maia_taken = seq;

        pthread_mutex_unlock(&g_lock);

        if (reload || restart) {
            ReloadMaia(settings.maia_rating);
        }

        if (active && fen[0] != '\0') {
            AnalyseMaia(seq, fen);
        }
    }

    StopMaia();
    return NULL;
}

/* --------------------------------------------------------- control loop -- */

static void *ControlThread(void *unused)
{
    /* FEN plus a maximum-length session id must fit in one atomic line. */
    char line[1024];

    (void)unused;

    for (;;) {
        int status = overlay_read_control(g_overlay, line, sizeof(line));
        char sessionId[SESSION_ID_MAX + 1];
        const char *fenPayload = NULL;
        const char *explorePayload = NULL;

        if (status <= 0) {
            break;              /* overlay exited or the pipe broke */
        }

        if (strncmp(line, "QUIT", 4) == 0 &&
            (line[4] == '\0' || line[4] == ' ')) {
            /* This synchronous callback writes the browser event before the
             * control thread tears the process down. It is what lets the
             * extension distinguish a deliberate close from a crash. */
            if (strcmp(line, "QUIT") == 0 && SessionIsInactive()) {
                EmitEvent("event", "dismissed", NULL, NULL);
                break;
            }

            if (ParseScopedControl(line, "QUIT", sessionId)) {
                EmitEvent("event", "dismissed", NULL, sessionId);
                break;
            }

            Log("analysis: ignored unscoped or stale QUIT control");
            continue;
        }

        if (strncmp(line, "RESCAN", 6) == 0 &&
            (line[6] == '\0' || line[6] == ' ')) {
            if (!ParseScopedControl(line, "RESCAN", sessionId)) {
                Log("analysis: ignored unscoped or stale RESCAN control");
                continue;
            }
            (void)overlay_publish_recovery(
                g_overlay, "rescan", "Waiting for the visible board\u2026");
            EmitEvent("command", "rescan", NULL, sessionId);
            continue;
        }

        if (strncmp(line, "FEN", 3) == 0 &&
            (line[3] == '\0' || line[3] == ' ')) {
            if (!ParseScopedFen(line, sessionId, &fenPayload)) {
                Log("analysis: ignored unscoped, stale, or empty FEN control");
                continue;
            }
            (void)overlay_publish_recovery(
                g_overlay, "set_fen", "Validating the position\u2026");
            EmitEvent("command", "set_fen", fenPayload, sessionId);
            continue;
        }

        if (strncmp(line, "RESTART", 7) == 0 &&
            (line[7] == '\0' || line[7] == ' ')) {
            if (!ParseScopedControl(line, "RESTART", sessionId)) {
                Log("analysis: ignored unscoped or stale RESTART control");
                continue;
            }
            EmitEvent("command", "restart_engines", NULL, sessionId);
            continue;
        }

        if (strncmp(line, "STOP", 4) == 0 &&
            (line[4] == '\0' || line[4] == ' ')) {
            if (!ParseScopedControl(line, "STOP", sessionId)) {
                Log("analysis: ignored unscoped or stale STOP control");
                continue;
            }
            (void)overlay_publish_recovery(
                g_overlay, "stop_session", "Stopping this session\u2026");
            EmitEvent("command", "stop_session", NULL, sessionId);
            continue;
        }

        if (strncmp(line, "EXPLORE_START", 13) == 0 &&
            (line[13] == '\0' || line[13] == ' ')) {
            if (!ParseScopedPayload(
                    line, "EXPLORE_START", sessionId, &explorePayload)) {
                Log("analysis: ignored unscoped, stale, or empty EXPLORE_START");
                continue;
            }
            EmitEvent("command", "explore_start", explorePayload, sessionId);
            continue;
        }

        if (strncmp(line, "EXPLORE_MOVE", 12) == 0 &&
            (line[12] == '\0' || line[12] == ' ')) {
            if (!ParseScopedPayload(
                    line, "EXPLORE_MOVE", sessionId, &explorePayload)) {
                Log("analysis: ignored unscoped, stale, or empty EXPLORE_MOVE");
                continue;
            }
            EmitEvent("command", "explore_move", explorePayload, sessionId);
            continue;
        }

        if (strncmp(line, "EXPLORE_GOTO", 12) == 0 &&
            (line[12] == '\0' || line[12] == ' ')) {
            if (!ParseScopedPayload(
                    line, "EXPLORE_GOTO", sessionId, &explorePayload)) {
                Log("analysis: ignored unscoped, stale, or empty EXPLORE_GOTO");
                continue;
            }
            EmitEvent("command", "explore_goto", explorePayload, sessionId);
            continue;
        }

        if (strncmp(line, "EXPLORE_LIVE", 12) == 0 &&
            (line[12] == '\0' || line[12] == ' ')) {
            if (!ParseScopedPayload(
                    line, "EXPLORE_LIVE", sessionId, &explorePayload)) {
                Log("analysis: ignored unscoped, stale, or empty EXPLORE_LIVE");
                continue;
            }
            EmitEvent("command", "explore_live", explorePayload, sessionId);
            continue;
        }

        if (strncmp(line, "EXPLORE_RESUME", 14) == 0 &&
            (line[14] == '\0' || line[14] == ' ')) {
            if (!ParseScopedPayload(
                    line, "EXPLORE_RESUME", sessionId, &explorePayload)) {
                Log("analysis: ignored unscoped, stale, or empty EXPLORE_RESUME");
                continue;
            }
            EmitEvent("command", "explore_resume", explorePayload, sessionId);
            continue;
        }

        if (strncmp(line, "SET", 3) == 0 && (line[3] == '\0' || line[3] == ' ')) {
            OverlaySettings wanted;
            int ratingChanged;

            pthread_mutex_lock(&g_lock);
            wanted = g_settings;
            pthread_mutex_unlock(&g_lock);

            if (!overlay_parse_settings(line + 3, &wanted)) {
                continue;
            }

            pthread_mutex_lock(&g_lock);
            ratingChanged = wanted.maia_rating != g_settings.maia_rating;
            g_settings = wanted;
            g_stockfish_options_dirty = 1;

            if (ratingChanged) {
                g_maia_reload = 1;
            }

            pthread_cond_broadcast(&g_wake);
            pthread_mutex_unlock(&g_lock);

            Log("analysis: settings -> budget %ld ms, explore budget %ld ms, "
                "maia %d, threads %d, multipv %d",
                wanted.budget_ms, wanted.explore_budget_ms,
                wanted.maia_rating, wanted.threads, wanted.multipv);
        }
    }

    /* The overlay is the whole user interface. Once it is gone there is
     * nothing left to do, and the main thread is parked in a blocking read on
     * the browser's stdin that cannot be interrupted portably -- so tear the
     * engines down here and exit the process outright. */
    Log("analysis: control channel closed, shutting down");
    AnalysisStop();
    fflush(NULL);
    _exit(0);
}

/* ------------------------------------------------------------ lifecycle -- */

int AnalysisStart(FILE *logFile, AnalysisEventSink sink, void *ctx)
{
    OverlaySettings settings;
    int startupStatus;

    g_log = logFile;
    g_event_sink = sink;
    g_event_context = ctx;

    if (!BaseDirectory(g_base, sizeof(g_base))) {
        Log("analysis: cannot resolve own directory -- using \".\"");
        snprintf(g_base, sizeof(g_base), ".");
    }

    if (!StartOverlay()) {
        return 0;
    }

    startupStatus = overlay_wait_for_start(g_overlay, &settings);

    if (startupStatus <= 0) {
        int result = 0;

        if (startupStatus == OVERLAY_START_DISMISSED) {
            Log("analysis: startup window dismissed");
            EmitEvent("event", "dismissed", NULL, NULL);
        } else if (startupStatus == OVERLAY_START_CLOSED) {
            Log("analysis: startup window closed");
        } else if (startupStatus == OVERLAY_START_PROTOCOL_MISMATCH) {
            Log("analysis: incompatible overlay protocol");
            result = -1;
        } else {
            Log("analysis: invalid startup settings or IPC failure");
        }
        overlay_stop(g_overlay);
        return result;
    }

    pthread_mutex_lock(&g_lock);
    g_settings = settings;
    g_stockfish_ready = 0;
    g_maia_ready = 0;
    pthread_mutex_unlock(&g_lock);

    /* Stockfish is the latency-sensitive dependency, so establish it first.
     * Maia startup moves to its own worker below and cannot delay returning to
     * the browser message loop. */
    StartStockfish(&settings);

    pthread_mutex_lock(&g_lock);
    g_stockfish_ready = g_stockfish != NULL;
    pthread_mutex_unlock(&g_lock);

    if (
        overlay_publish_ready(
            g_overlay, g_stockfish != NULL, 0, &settings) != 0
    ) {
        Log("analysis: overlay closed during engine startup");
        AnalysisStop();
        return 0;
    }

    if (pthread_create(
            &g_stockfish_worker, NULL, StockfishWorkerThread, NULL) != 0) {
        Log("analysis: could not start Stockfish thread");
        AnalysisStop();
        return 0;
    }

    g_stockfish_worker_live = 1;

    if (pthread_create(&g_maia_worker, NULL, MaiaWorkerThread, NULL) != 0) {
        Log("analysis: could not start Maia thread; Stockfish remains usable");
        (void)overlay_publish_status(
            g_overlay, "warn", "Maia worker unavailable.");
    } else {
        g_maia_worker_live = 1;
    }

    if (pthread_create(&g_controller, NULL, ControlThread, NULL) != 0) {
        Log("analysis: could not start control thread");
        AnalysisStop();
        return 0;
    }

    return 1;
}

void AnalysisStop(void)
{
    pthread_t stockfishWorker;
    pthread_t maiaWorker;
    int joinStockfish;
    int joinMaia;

    pthread_mutex_lock(&g_stop_lock);

    if (g_stopped) {
        pthread_mutex_unlock(&g_stop_lock);
        return;
    }

    g_stopped = 1;

    pthread_mutex_lock(&g_lock);
    g_quit = 1;
    joinStockfish = g_stockfish_worker_live;
    joinMaia = g_maia_worker_live;
    stockfishWorker = g_stockfish_worker;
    maiaWorker = g_maia_worker;
    g_stockfish_worker_live = 0;
    g_maia_worker_live = 0;
    pthread_cond_broadcast(&g_wake);
    pthread_mutex_unlock(&g_lock);

    if (joinStockfish &&
        !pthread_equal(pthread_self(), stockfishWorker)) {
        pthread_join(stockfishWorker, NULL);
    } else if (!joinStockfish) {
        StopStockfish();
    }

    if (joinMaia && !pthread_equal(pthread_self(), maiaWorker)) {
        pthread_join(maiaWorker, NULL);
    } else if (!joinMaia) {
        StopMaia();
    }

    /* Kept allocated on purpose -- see the comment on overlay_stop. */
    overlay_stop(g_overlay);

    pthread_mutex_unlock(&g_stop_lock);
    (void)g_controller;
}

void AnalysisSessionStart(const char *session_id, const char *label)
{
    int publishStatus;

    if (session_id == NULL || *session_id == '\0' || g_overlay == NULL) {
        return;
    }

    pthread_mutex_lock(&g_publish_lock);

    pthread_mutex_lock(&g_lock);
    g_session_active = 1;
    snprintf(g_session_id, sizeof(g_session_id), "%s", session_id);
    g_seq += 1UL;
    g_mode = ANALYSIS_MODE_LIVE;
    g_branch_id = 0ULL;
    g_node_id = 0U;
    g_fen[0] = '\0';
    g_last_move[0] = '\0';
    snprintf(g_source, sizeof(g_source), "inferred");
    g_flip = 0;
    g_synchronising = 0;
    g_sync_text[0] = '\0';
    g_live_revision = 0UL;
    g_live_fen[0] = '\0';
    g_live_last_move[0] = '\0';
    snprintf(g_live_source, sizeof(g_live_source), "inferred");
    g_live_flip = 0;
    g_live_synchronising = 0;
    g_live_sync_text[0] = '\0';
    memset(&g_result, 0, sizeof(g_result));
    g_result.seq = g_seq;
    g_result.mode = ANALYSIS_MODE_LIVE;
    snprintf(g_result.source, sizeof(g_result.source), "%s", g_source);
    pthread_cond_broadcast(&g_wake);
    pthread_mutex_unlock(&g_lock);

    publishStatus = overlay_publish_session_start(
        g_overlay, session_id, label != NULL ? label : "");
    pthread_mutex_unlock(&g_publish_lock);

    if (publishStatus != 0) {
        OverlayGone();
    }
}

void AnalysisSessionEnd(const char *reason, const char *result)
{
    int publishStatus;

    if (g_overlay == NULL) {
        return;
    }

    pthread_mutex_lock(&g_publish_lock);

    pthread_mutex_lock(&g_lock);
    g_session_active = 0;
    g_session_id[0] = '\0';
    g_seq += 1UL;
    g_mode = ANALYSIS_MODE_LIVE;
    g_branch_id = 0ULL;
    g_node_id = 0U;
    g_fen[0] = '\0';
    g_last_move[0] = '\0';
    g_synchronising = 0;
    g_sync_text[0] = '\0';
    g_live_revision = 0UL;
    g_live_fen[0] = '\0';
    g_live_last_move[0] = '\0';
    g_live_synchronising = 0;
    g_live_sync_text[0] = '\0';
    memset(&g_result, 0, sizeof(g_result));
    g_result.seq = g_seq;
    pthread_cond_broadcast(&g_wake);
    pthread_mutex_unlock(&g_lock);

    publishStatus = overlay_publish_session_end(
        g_overlay,
        reason != NULL ? reason : "ended",
        result != NULL ? result : "*");
    pthread_mutex_unlock(&g_publish_lock);

    if (publishStatus != 0) {
        OverlayGone();
    }
}

void AnalysisUpdateGameRecord(const char *initial_fen, const char *uci_moves,
                              size_t move_count, const char *result)
{
    if (g_overlay == NULL || initial_fen == NULL || uci_moves == NULL) {
        return;
    }
    pthread_mutex_lock(&g_publish_lock);
    (void)overlay_publish_game_record(
        g_overlay, initial_fen, uci_moves, move_count, result);
    pthread_mutex_unlock(&g_publish_lock);
}

void AnalysisRestartEngines(void)
{
    int publishStatus = 0;

    pthread_mutex_lock(&g_publish_lock);

    if (g_overlay != NULL &&
        overlay_publish_recovery(
            g_overlay, "restart_engines", "Restarting engines\u2026") != 0) {
        publishStatus = -1;
    }

    pthread_mutex_lock(&g_lock);

    if (!g_quit && publishStatus == 0) {
        g_restart_stockfish = 1;
        g_restart_maia = 1;
        pthread_cond_broadcast(&g_wake);
    }

    pthread_mutex_unlock(&g_lock);
    pthread_mutex_unlock(&g_publish_lock);

    if (publishStatus != 0) {
        OverlayGone();
    }
}

void AnalysisReportRecovery(const char *action, int accepted,
                            const char *message)
{
    int active;
    int publishStatus = 0;

    if (action == NULL || *action == '\0' || g_overlay == NULL) {
        return;
    }

    pthread_mutex_lock(&g_publish_lock);
    pthread_mutex_lock(&g_lock);
    active = g_session_active;
    pthread_mutex_unlock(&g_lock);

    if (active) {
        publishStatus = overlay_publish_recovery_result(
            g_overlay,
            action,
            accepted != 0,
            message != NULL ? message : "");
    }

    pthread_mutex_unlock(&g_publish_lock);

    if (publishStatus != 0) {
        OverlayGone();
    }
}

void AnalysisUpdateOrientation(int visuallyFlipped)
{
    int active;
    int publishStatus = 0;

    pthread_mutex_lock(&g_publish_lock);
    pthread_mutex_lock(&g_lock);
    active = g_session_active;
    g_live_flip = visuallyFlipped != 0;
    g_flip = visuallyFlipped != 0;
    if (g_result.seq == g_seq) {
        g_result.flip = visuallyFlipped != 0;
    }
    pthread_mutex_unlock(&g_lock);

    if (active && g_overlay != NULL) {
        publishStatus = overlay_publish_orientation(
            g_overlay, visuallyFlipped != 0);
    }

    pthread_mutex_unlock(&g_publish_lock);

    if (publishStatus != 0) {
        OverlayGone();
    }
}

void AnalysisPublish(const char *fen, int visuallyFlipped,
                     const char *lastMove, const char *source)
{
    unsigned long seq;
    unsigned long liveRevision;
    const char *validSource = StateSource(source);
    int active;
    AnalysisMode mode;
    int publishStatus;

    /* g_overlay is written once during startup and never cleared, so reading
     * it from the message loop needs no lock. */
    if (fen == NULL || g_overlay == NULL) {
        return;
    }

    pthread_mutex_lock(&g_publish_lock);

    pthread_mutex_lock(&g_lock);
    active = g_session_active;
    mode = g_mode;
    seq = g_seq + 1UL;
    liveRevision = g_live_revision + 1UL;
    pthread_mutex_unlock(&g_lock);

    if (!active) {
        pthread_mutex_unlock(&g_publish_lock);
        return;
    }

    if (mode == ANALYSIS_MODE_LIVE) {
        /* Publish before making this target revision visible to the worker.
         * No analysis frame can overtake its matching board frame. */
        publishStatus = overlay_publish_position(
            g_overlay, seq, fen, visuallyFlipped, lastMove, validSource,
            "live", liveRevision, 0ULL, 0U);
    } else {
        /* A real move is background metadata while the user is exploring. */
        publishStatus = overlay_publish_live_update(
            g_overlay, liveRevision, fen, visuallyFlipped, lastMove,
            validSource, 0, "");
    }

    if (publishStatus == 0) {
        pthread_mutex_lock(&g_lock);
        g_live_revision = liveRevision;
        g_live_flip = visuallyFlipped != 0;
        g_live_synchronising = 0;
        g_live_sync_text[0] = '\0';
        snprintf(g_live_fen, sizeof(g_live_fen), "%s", fen);
        snprintf(g_live_last_move, sizeof(g_live_last_move), "%s",
                 lastMove != NULL ? lastMove : "");
        snprintf(g_live_source, sizeof(g_live_source), "%s", validSource);

        if (mode == ANALYSIS_MODE_LIVE) {
            g_seq = seq;
            g_flip = visuallyFlipped != 0;
            g_synchronising = 0;
            g_sync_text[0] = '\0';
            snprintf(g_fen, sizeof(g_fen), "%s", fen);
            snprintf(g_last_move, sizeof(g_last_move), "%s",
                     lastMove != NULL ? lastMove : "");
            snprintf(g_source, sizeof(g_source), "%s", validSource);
            memset(&g_result, 0, sizeof(g_result));
            g_result.seq = seq;
            g_result.flip = visuallyFlipped != 0;
            g_result.mode = ANALYSIS_MODE_LIVE;
            g_result.live_revision = liveRevision;
            snprintf(g_result.fen, sizeof(g_result.fen), "%s", fen);
            snprintf(g_result.last_move, sizeof(g_result.last_move), "%s",
                     lastMove != NULL ? lastMove : "");
            snprintf(g_result.source, sizeof(g_result.source), "%s", validSource);
            pthread_cond_broadcast(&g_wake);
        }
        pthread_mutex_unlock(&g_lock);
    }

    pthread_mutex_unlock(&g_publish_lock);

    if (publishStatus != 0) {
        OverlayGone();
    }
}

void AnalysisUpdateStateSource(const char *source)
{
    const char *validSource = StateSource(source);
    unsigned long seq;
    unsigned long liveRevision;
    char liveFen[FEN_MAX];
    char liveLast[MOVE_MAX];
    int liveFlip;
    AnalysisMode mode;
    char currentSource[SOURCE_MAX];
    int active;
    int synchronising;
    int publishStatus = 0;
    char text[SYNC_TEXT_MAX];

    pthread_mutex_lock(&g_publish_lock);
    pthread_mutex_lock(&g_lock);
    active = g_session_active && g_live_fen[0] != '\0';
    seq = g_seq;
    liveRevision = g_live_revision;
    liveFlip = g_live_flip;
    mode = g_mode;
    synchronising = g_live_synchronising;
    snprintf(g_live_source, sizeof(g_live_source), "%s", validSource);
    snprintf(liveFen, sizeof(liveFen), "%s", g_live_fen);
    snprintf(liveLast, sizeof(liveLast), "%s", g_live_last_move);
    if (mode == ANALYSIS_MODE_LIVE) {
        snprintf(g_source, sizeof(g_source), "%s", validSource);
        if (g_result.seq == seq) {
            snprintf(g_result.source, sizeof(g_result.source), "%s", validSource);
        }
    }
    snprintf(currentSource, sizeof(currentSource), "%s", validSource);
    snprintf(text, sizeof(text), "%s", g_live_sync_text);
    pthread_mutex_unlock(&g_lock);

    if (active && g_overlay != NULL) {
        publishStatus = mode == ANALYSIS_MODE_LIVE
            ? overlay_publish_state(
                  g_overlay, seq, liveRevision, currentSource,
                  synchronising, text)
            : overlay_publish_live_update(
                  g_overlay, liveRevision, liveFen, liveFlip, liveLast,
                  currentSource, synchronising, text);
    }
    pthread_mutex_unlock(&g_publish_lock);

    if (publishStatus != 0) {
        OverlayGone();
    }
}

void AnalysisSetSynchronising(int active, const char *textValue)
{
    unsigned long seq;
    unsigned long liveRevision;
    char source[SOURCE_MAX];
    char text[SYNC_TEXT_MAX];
    char liveFen[FEN_MAX];
    char liveLast[MOVE_MAX];
    int liveFlip;
    AnalysisMode mode;
    int sessionActive;
    int synchronising = active != 0;
    int publishStatus = 0;

    pthread_mutex_lock(&g_publish_lock);
    pthread_mutex_lock(&g_lock);
    sessionActive = g_session_active && g_live_fen[0] != '\0';
    seq = g_seq;
    liveRevision = g_live_revision;
    liveFlip = g_live_flip;
    mode = g_mode;
    g_live_synchronising = synchronising;
    snprintf(g_live_sync_text, sizeof(g_live_sync_text), "%s",
             textValue != NULL ? textValue : "");
    snprintf(source, sizeof(source), "%s", g_live_source);
    snprintf(text, sizeof(text), "%s", g_live_sync_text);
    snprintf(liveFen, sizeof(liveFen), "%s", g_live_fen);
    snprintf(liveLast, sizeof(liveLast), "%s", g_live_last_move);
    if (mode == ANALYSIS_MODE_LIVE) {
        g_synchronising = synchronising;
        snprintf(g_sync_text, sizeof(g_sync_text), "%s", g_live_sync_text);
    }
    pthread_mutex_unlock(&g_lock);

    if (sessionActive && g_overlay != NULL) {
        publishStatus = mode == ANALYSIS_MODE_LIVE
            ? overlay_publish_state(
                  g_overlay, seq, liveRevision, source, synchronising, text)
            : overlay_publish_live_update(
                  g_overlay, liveRevision, liveFen, liveFlip, liveLast,
                  source, synchronising, text);
    }
    pthread_mutex_unlock(&g_publish_lock);

    if (publishStatus != 0) {
        OverlayGone();
    }
}

static int SelectExploreTarget(unsigned long long branchId,
                               unsigned int nodeId, const char *fen,
                               int visuallyFlipped, const char *lastMove,
                               const char *source, const char *event,
                               const char *action)
{
    const char *validSource = StateSource(source);
    unsigned long seq;
    unsigned long liveRevision;
    int active;
    int publishStatus = 0;

    if (branchId == 0ULL || fen == NULL || *fen == '\0' || g_overlay == NULL) {
        return 0;
    }

    pthread_mutex_lock(&g_publish_lock);
    pthread_mutex_lock(&g_lock);
    active = g_session_active && g_live_fen[0] != '\0';
    seq = g_seq + 1UL;
    liveRevision = g_live_revision;
    pthread_mutex_unlock(&g_lock);

    if (active) {
        publishStatus = overlay_publish_explore(
            g_overlay, event, action, NULL, NULL, branchId, nodeId,
            fen, lastMove);
    }
    if (active && publishStatus == 0) {
        publishStatus = overlay_publish_position(
            g_overlay, seq, fen, visuallyFlipped, lastMove, validSource,
            "explore", liveRevision, branchId, nodeId);
    }

    if (active && publishStatus == 0) {
        pthread_mutex_lock(&g_lock);
        g_seq = seq;
        g_mode = ANALYSIS_MODE_EXPLORE;
        g_branch_id = branchId;
        g_node_id = nodeId;
        g_flip = visuallyFlipped != 0;
        g_synchronising = 0;
        g_sync_text[0] = '\0';
        snprintf(g_fen, sizeof(g_fen), "%s", fen);
        snprintf(g_last_move, sizeof(g_last_move), "%s",
                 lastMove != NULL ? lastMove : "");
        snprintf(g_source, sizeof(g_source), "%s", validSource);
        memset(&g_result, 0, sizeof(g_result));
        g_result.seq = seq;
        g_result.flip = visuallyFlipped != 0;
        g_result.mode = ANALYSIS_MODE_EXPLORE;
        g_result.live_revision = liveRevision;
        g_result.branch_id = branchId;
        g_result.node_id = nodeId;
        snprintf(g_result.fen, sizeof(g_result.fen), "%s", fen);
        snprintf(g_result.last_move, sizeof(g_result.last_move), "%s",
                 lastMove != NULL ? lastMove : "");
        snprintf(g_result.source, sizeof(g_result.source), "%s", validSource);
        pthread_cond_broadcast(&g_wake);
        pthread_mutex_unlock(&g_lock);
    }

    pthread_mutex_unlock(&g_publish_lock);

    if (publishStatus != 0) {
        OverlayGone();
    }
    return active && publishStatus == 0;
}

void AnalysisExploreStart(unsigned long long branchId, unsigned int nodeId,
                          const char *fen, int visuallyFlipped,
                          const char *lastMove, const char *source)
{
    (void)SelectExploreTarget(
        branchId, nodeId, fen, visuallyFlipped, lastMove, source,
        "started", "start");
}

void AnalysisExploreSelect(unsigned long long branchId, unsigned int nodeId,
                           const char *fen, int visuallyFlipped,
                           const char *lastMove, const char *source,
                           const char *action)
{
    (void)SelectExploreTarget(
        branchId, nodeId, fen, visuallyFlipped, lastMove, source,
        "selected", action != NULL ? action : "goto");
}

void AnalysisExploreLive(unsigned long long branchId)
{
    unsigned long seq;
    unsigned long liveRevision;
    char fen[FEN_MAX];
    char last[MOVE_MAX];
    char source[SOURCE_MAX];
    int flip;
    int synchronising;
    char text[SYNC_TEXT_MAX];
    int active;
    int publishStatus = 0;

    pthread_mutex_lock(&g_publish_lock);
    pthread_mutex_lock(&g_lock);
    active = g_session_active && g_live_fen[0] != '\0';
    seq = g_seq + 1UL;
    liveRevision = g_live_revision;
    flip = g_live_flip;
    synchronising = g_live_synchronising;
    snprintf(fen, sizeof(fen), "%s", g_live_fen);
    snprintf(last, sizeof(last), "%s", g_live_last_move);
    snprintf(source, sizeof(source), "%s", g_live_source);
    snprintf(text, sizeof(text), "%s", g_live_sync_text);
    pthread_mutex_unlock(&g_lock);

    if (active) {
        publishStatus = overlay_publish_explore(
            g_overlay, "live", "live", NULL, NULL,
            branchId, 0U, NULL, NULL);
    }
    if (active && publishStatus == 0) {
        publishStatus = overlay_publish_position(
            g_overlay, seq, fen, flip, last, source, "live",
            liveRevision, 0ULL, 0U);
    }
    if (active && publishStatus == 0 && synchronising) {
        publishStatus = overlay_publish_state(
            g_overlay, seq, liveRevision, source, synchronising, text);
    }

    if (active && publishStatus == 0) {
        pthread_mutex_lock(&g_lock);
        g_seq = seq;
        g_mode = ANALYSIS_MODE_LIVE;
        g_branch_id = 0ULL;
        g_node_id = 0U;
        g_flip = flip;
        g_synchronising = synchronising;
        snprintf(g_sync_text, sizeof(g_sync_text), "%s", text);
        snprintf(g_fen, sizeof(g_fen), "%s", fen);
        snprintf(g_last_move, sizeof(g_last_move), "%s", last);
        snprintf(g_source, sizeof(g_source), "%s", source);
        memset(&g_result, 0, sizeof(g_result));
        g_result.seq = seq;
        g_result.flip = flip;
        g_result.mode = ANALYSIS_MODE_LIVE;
        g_result.live_revision = liveRevision;
        snprintf(g_result.fen, sizeof(g_result.fen), "%s", fen);
        snprintf(g_result.last_move, sizeof(g_result.last_move), "%s", last);
        snprintf(g_result.source, sizeof(g_result.source), "%s", source);
        pthread_cond_broadcast(&g_wake);
        pthread_mutex_unlock(&g_lock);
    }

    pthread_mutex_unlock(&g_publish_lock);
    if (publishStatus != 0) {
        OverlayGone();
    }
}

void AnalysisExploreDestroy(unsigned long long branchId, const char *reason)
{
    int active;
    int publishStatus = 0;

    if (branchId == 0ULL || g_overlay == NULL) {
        return;
    }

    pthread_mutex_lock(&g_publish_lock);
    pthread_mutex_lock(&g_lock);
    active = g_session_active;
    if (active && g_mode == ANALYSIS_MODE_EXPLORE &&
        g_branch_id == branchId) {
        g_seq += 1UL;
        g_mode = ANALYSIS_MODE_LIVE;
        g_branch_id = 0ULL;
        g_node_id = 0U;
        g_fen[0] = '\0';
        memset(&g_result, 0, sizeof(g_result));
        g_result.seq = g_seq;
        g_result.mode = ANALYSIS_MODE_LIVE;
        pthread_cond_broadcast(&g_wake);
    }
    pthread_mutex_unlock(&g_lock);
    if (active) {
        publishStatus = overlay_publish_explore(
            g_overlay, "destroyed", NULL, reason, NULL,
            branchId, 0U, NULL, NULL);
    }
    pthread_mutex_unlock(&g_publish_lock);
    if (publishStatus != 0) {
        OverlayGone();
    }
}

void AnalysisReportExploreRejected(const char *action, const char *reason,
                                   const char *message,
                                   unsigned long long branchId,
                                   unsigned int nodeId)
{
    int active;
    int publishStatus = 0;

    if (g_overlay == NULL) {
        return;
    }

    pthread_mutex_lock(&g_publish_lock);
    pthread_mutex_lock(&g_lock);
    active = g_session_active;
    pthread_mutex_unlock(&g_lock);
    if (active) {
        publishStatus = overlay_publish_explore(
            g_overlay, "rejected", action, reason, message,
            branchId, nodeId, NULL, NULL);
    }
    pthread_mutex_unlock(&g_publish_lock);
    if (publishStatus != 0) {
        OverlayGone();
    }
}
