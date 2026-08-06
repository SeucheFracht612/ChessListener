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
#define POLL_SLICE_MS 40    /* how fast we notice a new position           */
#define PUSH_EVERY_MS 120   /* how often a deepening search redraws the bar */

static UciEngine *g_stockfish;
static UciEngine *g_maia;
static Overlay   *g_overlay;
static FILE      *g_log;
static char       g_base[BASE_MAX];

/* ---- shared state, guarded by g_lock ---- */
static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t  g_wake = PTHREAD_COND_INITIALIZER;

static char          g_fen[FEN_MAX];
static int           g_flip;
static unsigned long g_seq;        /* newest position handed in           */
static unsigned long g_taken;      /* newest position the worker has taken */
static OverlaySettings g_settings;
static int           g_options_dirty;  /* threads / multipv / budget changed */
static int           g_maia_reload;    /* rating changed, lc0 must restart   */
static int           g_quit;

static pthread_t g_worker;
static pthread_t g_controller;
static int       g_worker_live;

/* Shutdown runs exactly once, and a second caller waits for the first to
 * finish rather than racing it. Both the message loop (browser pipe closed)
 * and the control thread (overlay window closed) can arrive here, and in
 * practice they often arrive together. */
static pthread_mutex_t g_stop_lock = PTHREAD_MUTEX_INITIALIZER;
static int             g_stopped;

/* g_stockfish and g_maia belong to the worker thread once it exists: it is the
 * only thread that starts, restarts, or stops them. AnalysisStop joins the
 * worker and lets it do the teardown, which is what removed the last of the
 * shutdown races. */
static void StopEngines(void)
{
    if (g_stockfish != NULL) {
        uci_stop(g_stockfish);
        g_stockfish = NULL;
    }

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

static long NowMs(void)
{
    struct timespec ts;

    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000L + ts.tv_nsec / 1000000L;
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

/* ---------------------------------------------------------- worker loop -- */

/* True when the worker should drop what it is doing: a newer position, a
 * settings change, or shutdown. */
static int Superseded(unsigned long mine)
{
    int superseded;

    pthread_mutex_lock(&g_lock);
    superseded = g_quit || g_seq != mine || g_options_dirty || g_maia_reload;
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

static void PublishAnalysis(unsigned long seq, const char *fen, int flip,
                            int depth, int final, const char *human)
{
    const UciLine *lines = NULL;
    int count = 0;

    if (g_stockfish != NULL) {
        lines = uci_lines(g_stockfish, &count);
    }

    if (
        overlay_publish_analysis(
            g_overlay, seq, fen, flip, depth, final,
            count > 0 ? &lines[0] : NULL,
            (human != NULL && *human != '\0') ? human : NULL,
            count > 0 ? lines : NULL,
            count) != 0
    ) {
        OverlayGone();
    }
}

static void AnalysePosition(unsigned long seq, const char *fen, int flip,
                            long budgetMs)
{
    char human[8];
    long started;
    long lastPush;
    int status;

    human[0] = '\0';

    /* Maia first: one network evaluation, tens of milliseconds, and the
     * "what would a human play" line is the part that reads as instant. */
    if (g_maia != NULL) {
        status = uci_bestmove(g_maia, fen, human, sizeof(human));

        if (status != 0) {
            human[0] = '\0';
            Log("analysis: Maia query failed (%d)", status);

            if (status == -2) {
                uci_stop(g_maia);
                g_maia = NULL;
                (void)overlay_publish_status(g_overlay, "warn",
                                             "Maia stopped responding.");
            }
        }
    }

    if (g_stockfish == NULL) {
        PublishAnalysis(seq, fen, flip, 0, 1, human);
        return;
    }

    if (uci_search_begin(g_stockfish, fen) != 0) {
        Log("analysis: stockfish search failed to start");
        uci_stop(g_stockfish);
        g_stockfish = NULL;
        (void)overlay_publish_status(g_overlay, "warn",
                                     "Stockfish stopped responding.");
        PublishAnalysis(seq, fen, flip, 0, 1, human);
        return;
    }

    started = NowMs();
    lastPush = 0L;

    for (;;) {
        int updated = 0;
        int finished = 0;
        long now;

        status = uci_search_poll(g_stockfish, POLL_SLICE_MS, &updated, &finished);

        if (status == -4) {                       /* mate or stalemate */
            PublishAnalysis(seq, fen, flip, 0, 1, human);
            return;
        }

        if (status == -2) {
            Log("analysis: stockfish died mid-search");
            uci_stop(g_stockfish);
            g_stockfish = NULL;
            (void)overlay_publish_status(g_overlay, "warn",
                                         "Stockfish stopped responding.");
            PublishAnalysis(seq, fen, flip, 0, 1, human);
            return;
        }

        now = NowMs();

        if (Superseded(seq)) {
            /* The board has moved on. Kill the search now; the outer loop
             * picks up the newer position on its next turn. */
            (void)uci_search_abort(g_stockfish);
            return;
        }

        if (finished) {
            PublishAnalysis(seq, fen, flip, uci_depth(g_stockfish), 1, human);
            return;
        }

        if (budgetMs > 0L && now - started >= budgetMs) {
            (void)uci_search_abort(g_stockfish);
            PublishAnalysis(seq, fen, flip, uci_depth(g_stockfish), 1, human);
            return;
        }

        if (updated && now - lastPush >= PUSH_EVERY_MS) {
            lastPush = now;
            PublishAnalysis(seq, fen, flip, uci_depth(g_stockfish), 0, human);
        }
    }
}

static void ApplyStockfishOptions(const OverlaySettings *settings)
{
    char value[16];

    if (g_stockfish == NULL) {
        return;
    }

    (void)uci_search_abort(g_stockfish);

    snprintf(value, sizeof(value), "%d", settings->threads);

    if (uci_set_option(g_stockfish, "Threads", value) != 0) {
        Log("analysis: could not set Threads=%d", settings->threads);
    }

    if (uci_set_multipv(g_stockfish, settings->multipv) != 0) {
        Log("analysis: could not set MultiPV=%d", settings->multipv);
    }
}

static void ReloadMaia(int rating)
{
    char note[64];

    snprintf(note, sizeof(note), "Loading Maia %d\u2026", rating);
    (void)overlay_publish_status(g_overlay, "info", note);

    if (g_maia != NULL) {
        uci_stop(g_maia);
        g_maia = NULL;
    }

    StartMaia(rating);

    if (g_maia == NULL) {
        snprintf(note, sizeof(note), "Maia %d unavailable.", rating);
        (void)overlay_publish_status(g_overlay, "warn", note);
    } else {
        (void)overlay_publish_status(g_overlay, "info", "");
    }
}

static void *WorkerThread(void *unused)
{
    (void)unused;

    for (;;) {
        char fen[FEN_MAX];
        unsigned long seq;
        int flip;
        int reload;
        int dirty;
        OverlaySettings settings;

        pthread_mutex_lock(&g_lock);

        while (
            !g_quit &&
            g_seq == g_taken &&
            !g_options_dirty &&
            !g_maia_reload
        ) {
            pthread_cond_wait(&g_wake, &g_lock);
        }

        if (g_quit) {
            pthread_mutex_unlock(&g_lock);
            break;
        }

        reload = g_maia_reload;
        dirty = g_options_dirty;
        g_maia_reload = 0;
        g_options_dirty = 0;
        settings = g_settings;
        seq = g_seq;
        flip = g_flip;
        memcpy(fen, g_fen, sizeof(fen));
        g_taken = seq;

        pthread_mutex_unlock(&g_lock);

        if (reload) {
            ReloadMaia(settings.maia_rating);
        }

        if (dirty) {
            ApplyStockfishOptions(&settings);
            (void)overlay_publish_settings(g_overlay, &settings);
        }

        if (fen[0] != '\0') {
            AnalysePosition(seq, fen, flip, settings.budget_ms);
        }
    }

    StopEngines();
    return NULL;
}

/* --------------------------------------------------------- control loop -- */

static void *ControlThread(void *unused)
{
    char line[256];

    (void)unused;

    for (;;) {
        int status = overlay_read_control(g_overlay, line, sizeof(line));

        if (status <= 0) {
            break;              /* overlay exited or the pipe broke */
        }

        if (strcmp(line, "QUIT") == 0) {
            break;
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
            g_options_dirty = 1;

            if (ratingChanged) {
                g_maia_reload = 1;
            }

            pthread_cond_broadcast(&g_wake);
            pthread_mutex_unlock(&g_lock);

            Log("analysis: settings -> budget %ld ms, maia %d, threads %d, "
                "multipv %d",
                wanted.budget_ms, wanted.maia_rating, wanted.threads,
                wanted.multipv);
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

int AnalysisStart(FILE *logFile)
{
    OverlaySettings settings;
    int startupStatus;

    g_log = logFile;

    if (!BaseDirectory(g_base, sizeof(g_base))) {
        Log("analysis: cannot resolve own directory -- using \".\"");
        snprintf(g_base, sizeof(g_base), ".");
    }

    if (!StartOverlay()) {
        return 0;
    }

    startupStatus = overlay_wait_for_start(g_overlay, &settings);

    if (startupStatus <= 0) {
        Log(
            startupStatus == 0
                ? "analysis: startup window closed"
                : "analysis: invalid startup settings");
        overlay_stop(g_overlay);
        return 0;
    }

    StartStockfish(&settings);
    StartMaia(settings.maia_rating);

    pthread_mutex_lock(&g_lock);
    g_settings = settings;
    pthread_mutex_unlock(&g_lock);

    if (
        overlay_publish_ready(
            g_overlay, g_stockfish != NULL, g_maia != NULL, &settings) != 0
    ) {
        Log("analysis: overlay closed during engine startup");
        AnalysisStop();
        return 0;
    }

    if (pthread_create(&g_worker, NULL, WorkerThread, NULL) != 0) {
        Log("analysis: could not start engine thread");
        AnalysisStop();
        return 0;
    }

    g_worker_live = 1;

    if (pthread_create(&g_controller, NULL, ControlThread, NULL) != 0) {
        Log("analysis: could not start control thread");
        AnalysisStop();
        return 0;
    }

    return 1;
}

void AnalysisStop(void)
{
    pthread_t worker;
    int joinWorker;

    pthread_mutex_lock(&g_stop_lock);

    if (g_stopped) {
        pthread_mutex_unlock(&g_stop_lock);
        return;
    }

    g_stopped = 1;

    pthread_mutex_lock(&g_lock);
    g_quit = 1;
    joinWorker = g_worker_live;
    worker = g_worker;
    g_worker_live = 0;
    pthread_cond_broadcast(&g_wake);
    pthread_mutex_unlock(&g_lock);

    if (joinWorker && !pthread_equal(pthread_self(), worker)) {
        /* Returning from the join means the worker has already stopped both
         * engines, so nothing here touches them. */
        pthread_join(worker, NULL);
    } else if (!joinWorker) {
        /* Failed startup: there is no worker, so the engines are still ours. */
        StopEngines();
    }

    /* Kept allocated on purpose -- see the comment on overlay_stop. */
    overlay_stop(g_overlay);

    pthread_mutex_unlock(&g_stop_lock);
    (void)g_controller;
}

void AnalysisPublish(const char *fen, int visuallyFlipped)
{
    unsigned long seq;

    /* g_overlay is written once during startup and never cleared, so reading
     * it from the message loop needs no lock. */
    if (fen == NULL || g_overlay == NULL) {
        return;
    }

    pthread_mutex_lock(&g_lock);
    g_seq += 1UL;
    seq = g_seq;
    g_flip = visuallyFlipped;
    snprintf(g_fen, sizeof(g_fen), "%s", fen);
    pthread_cond_broadcast(&g_wake);
    pthread_mutex_unlock(&g_lock);

    /* Paint the new board straight away. The evaluation for this seq lands
     * later; the overlay ignores any evaluation older than the board it is
     * showing. */
    if (overlay_publish_position(g_overlay, seq, fen, visuallyFlipped) != 0) {
        OverlayGone();
    }
}
