#define _POSIX_C_SOURCE 200809L

#include "analysis.h"
#include "overlay.h"
#include "uci.h"

#include <limits.h>
#include <stdarg.h>
#include <poll.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

/* Base directory is capped well below PATH_MAX so appending a known suffix
 * provably cannot truncate. */
#define BASE_MAX 1024

#define ALT_LINES 3

static UciEngine *g_stockfish;
static UciEngine *g_maia;
static Overlay   *g_overlay;
static FILE      *g_log;

/* ------------------------------------------------------------- helpers -- */

static void Log(const char *fmt, ...)
{
    va_list ap;
    if (g_log == NULL) {
        return;
    }
    va_start(ap, fmt);
    vfprintf(g_log, fmt, ap);
    va_end(ap);
    fputc('\n', g_log);
    fflush(g_log);
}

/* The browser picks our working directory, so relative paths are useless.
 * Resolve everything against the binary's own location instead. */
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
    return (value != NULL && *value != '\0') ? value : fallback;
}

static int Executable(const char *path)
{
    return path != NULL && access(path, X_OK) == 0;
}

static int Readable(const char *path)
{
    return path != NULL && access(path, R_OK) == 0;
}

/* Non-zero if another native message is already waiting. stdin is unbuffered
 * (see main), so poll() sees everything -- with stdio buffering enabled this
 * check would miss bytes already sitting in the FILE buffer. */
static int InputPending(void)
{
    struct pollfd probe;
    probe.fd = STDIN_FILENO;
    probe.events = POLLIN;
    probe.revents = 0;
    return poll(&probe, 1, 0) > 0;
}

/* --------------------------------------------------------------- start -- */

static void StartStockfish(void)
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
    config.threads = 2;
    config.multipv = ALT_LINES;
    /* movetime, not depth: depth 18 is 80 ms in one position and 4 s in
     * another, which makes a live overlay feel broken. */
    config.limit = UCI_LIMIT_MOVETIME;
    config.limit_value = 300;
    config.timeout_ms = 5000;
    config.startup_ms = 10000;

    g_stockfish = uci_start(&config);
    Log(g_stockfish != NULL
    ? "analysis: stockfish ready (%s)"
    : "analysis: stockfish unavailable (%s) -- evaluations disabled",
        path);
}

static void StartMaia(const char base[BASE_MAX])
{
    UciConfig config;
    char lc0Default[PATH_MAX];
    char netDefault[PATH_MAX];
    char libraryDirectory[PATH_MAX];
    const char *lc0;
    const char *net;

    snprintf(lc0Default, sizeof(lc0Default), "%s/Engine/lc0", base);
    snprintf(netDefault, sizeof(netDefault),
             "%s/Engine/maia-chess/maia_weights/maia-1500.pb.gz", base);

    lc0 = EnvOrDefault("CHESSLISTENER_LC0", lc0Default);
    net = EnvOrDefault("CHESSLISTENER_MAIA_NET", netDefault);

    if (!Executable(lc0)) {
        Log("analysis: lc0 not found at %s -- human-move prediction disabled",
            lc0);
        return;
    }

    if (!Readable(net)) {
        Log("analysis: Maia weights not readable at %s -- disabled", net);
        return;
    }

    /* Release tarballs ship their backend .so files beside the binary. */
    snprintf(libraryDirectory, sizeof(libraryDirectory), "%s/Engine/lib", base);
    if (access(libraryDirectory, X_OK) == 0) {
        setenv("LD_LIBRARY_PATH", libraryDirectory, 1);
    }

    memset(&config, 0, sizeof(config));
    config.exe = lc0;
    config.weights = net;
    /* Leave the backend unset by default: lc0 autodetects whatever it was
     * actually compiled with. Forcing a name that wasn't built in makes lc0
     * fail at load time. Override with CHESSLISTENER_LC0_BACKEND if needed. */
    config.backend = getenv("CHESSLISTENER_LC0_BACKEND");
    config.threads = 1;
    config.multipv = 1;
    /* Maia is only human-like at exactly one node: that reads the policy
     * head and does no search at all. More nodes gives weak Leela, not a
     * 1500-rated human. */
    config.limit = UCI_LIMIT_NODES;
    config.limit_value = 1;
    config.timeout_ms = 5000;
    config.startup_ms = 60000;

    g_maia = uci_start(&config);
    Log(g_maia != NULL
    ? "analysis: maia ready (%s)"
    : "analysis: maia failed to start (%s)",
        net);
}

static void StartOverlay(const char base[BASE_MAX])
{
    char scriptDefault[PATH_MAX];
    const char *script;
    const char *disabled = getenv("CHESSLISTENER_NO_OVERLAY");

    if (disabled != NULL && strcmp(disabled, "1") == 0) {
        Log("analysis: overlay disabled by environment");
        return;
    }

    snprintf(scriptDefault, sizeof(scriptDefault), "%s/overlay.py", base);
    script = EnvOrDefault("CHESSLISTENER_OVERLAY", scriptDefault);

    if (!Readable(script)) {
        Log("analysis: overlay script missing at %s", script);
        return;
    }

    g_overlay = overlay_start(script);
    Log(g_overlay != NULL
    ? "analysis: overlay started (%s)"
    : "analysis: overlay failed to start (%s)",
        script);
}

void AnalysisStart(FILE *logFile)
{
    char base[BASE_MAX];

    g_log = logFile;

    if (!BaseDirectory(base, sizeof(base))) {
        Log("analysis: cannot resolve own directory -- using \".\"");
        snprintf(base, sizeof(base), ".");
    }

    Log("analysis: base directory %s", base);

    StartStockfish();
    StartMaia(base);
    StartOverlay(base);
}

void AnalysisStop(void)
{
    if (g_overlay != NULL) {
        overlay_stop(g_overlay);
        g_overlay = NULL;
    }
    if (g_stockfish != NULL) {
        uci_stop(g_stockfish);
        g_stockfish = NULL;
    }
    if (g_maia != NULL) {
        uci_stop(g_maia);
        g_maia = NULL;
    }
}

/* ------------------------------------------------------------- publish -- */

void AnalysisPublish(const char *fen, int visuallyFlipped)
{
    UciLine lines[ALT_LINES];
    char humanMove[8];
    int lineCount = 0;
    int haveHuman = 0;

    if (fen == NULL || g_overlay == NULL) {
        return;
    }

    /* In blitz, board updates can arrive faster than we can analyse. Showing
     * an evaluation for a position two moves stale is worse than showing
     * nothing, so drop this one and let the newer message win. */
    if (InputPending()) {
        Log("analysis: skipped stale position");
        return;
    }

    memset(lines, 0, sizeof(lines));
    humanMove[0] = '\0';

    if (g_stockfish != NULL) {
        lineCount = uci_analyse(g_stockfish, fen, lines, ALT_LINES);

        if (lineCount == -4) {
            Log("analysis: terminal position, nothing to evaluate");
            (void)overlay_publish(g_overlay, fen, visuallyFlipped,
                                  NULL, NULL, NULL, 0);
            return;
        }

        if (lineCount < 0) {
            Log("analysis: stockfish query failed (%d)", lineCount);
            /* -2 means the child is gone; stop asking it. */
            if (lineCount == -2) {
                uci_stop(g_stockfish);
                g_stockfish = NULL;
            }
            lineCount = 0;
        }
    }

    if (g_maia != NULL) {
        int status = uci_bestmove(g_maia, fen, humanMove, sizeof(humanMove));

        if (status == 0) {
            haveHuman = 1;
        } else {
            Log("analysis: maia query failed (%d)", status);
            if (status == -2) {
                uci_stop(g_maia);
                g_maia = NULL;
            }
        }
    }

    if (overlay_publish(g_overlay, fen, visuallyFlipped,
        lineCount > 0 ? &lines[0] : NULL,
        haveHuman ? humanMove : NULL,
        lineCount > 0 ? lines : NULL,
        lineCount > 0 ? lineCount : 0) != 0) {
        Log("analysis: overlay closed");
    overlay_stop(g_overlay);
    g_overlay = NULL;
        }
}
