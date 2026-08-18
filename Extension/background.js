"use strict";

const NATIVE_HOST = "local.chess_listener";
const NATIVE_PROTOCOL_VERSION = 4;
const REQUIRED_CAPABILITIES = [
    "session_v2",
    "state_override",
    "streaming_analysis",
    "history_reconciliation",
    "state_revision",
    "state_source",
    "analysis_lab",
    "analysis_target"
];
const HANDSHAKE_TIMEOUT_MS = 3000;
const SESSION_STORAGE_KEY = "session_broker_v4";
const MAX_HISTORY_PLIES = 1024;
const MAX_HISTORY_BYTES = 32 * 1024;

const pages = new Map();
const retiredPageInstances = new Map();
let activeSession = null;
let nativeConnection = null;
let nativeProtocolError = null;
let automaticClaimsPaused = false;
let pendingExplicitTabId = null;
let sessionCounter = 0;
let eligibilityCounter = 0;
let initializationPromise = null;
let persistenceChain = Promise.resolve();
let restoredSessionNeedsHydration = false;

function createSessionId() {
    sessionCounter += 1;
    return `session-${Date.now().toString(36)}-${sessionCounter}`;
}

function boundedError(error, fallback) {
    const value = error?.message ?? error ?? fallback;
    return String(value ?? "Native host unavailable").slice(0, 240);
}

function serializableSession(session) {
    if (session === null) {
        return null;
    }
    return {
        session_id: session.sessionId,
        tab_id: session.tabId,
        page_instance_id: session.pageInstanceId,
        route_generation: session.routeGeneration,
        game_key: session.gameKey,
        url: session.url,
        trigger: session.trigger,
        snapshot_seq: session.snapshotSeq,
        latest_snapshot: session.latestSnapshot,
        history_seq: session.historySeq,
        latest_history: session.latestHistory,
        dismissed: session.dismissed,
        retry_used: session.retryUsed,
        native_unavailable: session.nativeUnavailable,
        last_error: session.lastError
    };
}

function persistedBrokerState() {
    return {
        format: 2,
        automatic_claims_paused: automaticClaimsPaused,
        protocol_error: nativeProtocolError?.message ?? null,
        active_session: serializableSession(activeSession)
    };
}

function persistBrokerState() {
    const state = persistedBrokerState();
    persistenceChain = persistenceChain
        .then(() =>
            browser.storage.session.set({
                [SESSION_STORAGE_KEY]: state
            })
        )
        .catch((error) => {
            console.warn(
                "[ChessListener] could not persist session state:",
                error
            );
        });
    return persistenceChain;
}

function restoreSession(saved) {
    if (
        saved === null ||
        typeof saved !== "object" ||
        typeof saved.session_id !== "string" ||
        !Number.isInteger(saved.tab_id) ||
        typeof saved.page_instance_id !== "string" ||
        !Number.isInteger(saved.route_generation) ||
        typeof saved.game_key !== "string" ||
        !Number.isInteger(saved.snapshot_seq)
    ) {
        return null;
    }
    return {
        sessionId: saved.session_id,
        tabId: saved.tab_id,
        pageInstanceId: saved.page_instance_id,
        routeGeneration: saved.route_generation,
        gameKey: saved.game_key,
        url: typeof saved.url === "string" ? saved.url : "",
        trigger: typeof saved.trigger === "string" ? saved.trigger : "restored",
        snapshotSeq: saved.snapshot_seq,
        latestSnapshot:
            saved.latest_snapshot !== null &&
            typeof saved.latest_snapshot === "object"
                ? saved.latest_snapshot
                : null,
        historySeq: Number.isInteger(saved.history_seq)
            ? saved.history_seq
            : 0,
        latestHistory:
            saved.latest_history !== null &&
            typeof saved.latest_history === "object"
                ? saved.latest_history
                : null,
        lastSentSnapshotSeq: 0,
        lastSentHistorySeq: 0,
        hydratedConnection: null,
        dismissed: saved.dismissed === true,
        // A successful event-page rehydration gets a fresh crash retry budget.
        retryUsed: false,
        nativeUnavailable: saved.native_unavailable === true,
        lastError:
            typeof saved.last_error === "string"
                ? saved.last_error.slice(0, 240)
                : null,
        queue: Promise.resolve()
    };
}

function ensureInitialized() {
    if (initializationPromise !== null) {
        return initializationPromise;
    }
    initializationPromise = (async () => {
        try {
            const stored = await browser.storage.session.get(
                SESSION_STORAGE_KEY
            );
            const state = stored?.[SESSION_STORAGE_KEY];
            if (state?.format === 2) {
                automaticClaimsPaused =
                    state.automatic_claims_paused === true;
                nativeProtocolError =
                    typeof state.protocol_error === "string"
                        ? new Error(state.protocol_error)
                        : null;
                activeSession = restoreSession(state.active_session);
                restoredSessionNeedsHydration = activeSession !== null;
            }
        } catch (error) {
            console.warn(
                "[ChessListener] could not restore session state:",
                error
            );
        }
    })();
    return initializationPromise;
}

function senderTabId(sender) {
    const tabId = sender?.tab?.id;
    return Number.isInteger(tabId) ? tabId : null;
}

function isValidPageMessage(message) {
    return (
        typeof message?.page_instance_id === "string" &&
        message.page_instance_id.length > 0 &&
        Number.isInteger(message.route_generation) &&
        message.route_generation >= 0 &&
        typeof message.game_key === "string" &&
        message.game_key.length > 0 &&
        typeof message.url === "string"
    );
}

function isValidBoard(board) {
    return (
        typeof board === "string" &&
        board.length === 64 &&
        /^[prnbqkPRNBQK.]{64}$/.test(board)
    );
}

function canonicalGameResult(value) {
    return ["*", "1-0", "0-1", "1/2-1/2"].includes(value)
        ? value
        : "*";
}

function isValidHistoryToken(token, notation) {
    if (notation === "uci") {
        return /^[a-h][1-8][a-h][1-8][qrbn]?$/.test(token);
    }
    return (
        /^(?:O-O(?:-O)?|[KQRBN](?:[a-h]|[1-8]|[a-h][1-8])?x?[a-h][1-8](?:=?[QRBN])?|[a-h](?:x[a-h])?[1-8](?:=?[QRBN])?)[+#]?$/.test(
            token
        )
    );
}

function isValidInitialFen(fen) {
    return (
        fen === undefined ||
        (typeof fen === "string" &&
            fen.length <= 128 &&
            /^[prnbqkPRNBQK1-8/]+ [wb] (?:-|(?=[KQkq])K?Q?k?q?) (?:-|[a-h][36]) \d+ \d+$/.test(
                fen
            ))
    );
}

function isValidHistoryCandidate(message) {
    if (
        !isValidBoard(message?.displayed_board) ||
        !["uci", "san"].includes(message?.history_notation) ||
        typeof message?.history_moves !== "string" ||
        message.history_moves.length === 0 ||
        message.history_moves.length > MAX_HISTORY_BYTES ||
        typeof message?.history_complete !== "boolean" ||
        ![undefined, "*", "1-0", "0-1", "1/2-1/2"].includes(
            message?.game_result
        ) ||
        !isValidInitialFen(message.initial_fen)
    ) {
        return false;
    }
    const tokens = message.history_moves.split("|");
    return (
        tokens.length > 0 &&
        tokens.length <= MAX_HISTORY_PLIES &&
        tokens.every(
            (token) =>
                token.length > 0 &&
                isValidHistoryToken(token, message.history_notation)
        )
    );
}

function getRetiredInstances(tabId) {
    let retired = retiredPageInstances.get(tabId);
    if (retired === undefined) {
        retired = new Set();
        retiredPageInstances.set(tabId, retired);
    }
    return retired;
}

function sameSessionIdentity(session, entry) {
    return (
        session !== null &&
        session.tabId === entry.tabId &&
        session.pageInstanceId === entry.pageInstanceId &&
        session.routeGeneration === entry.routeGeneration &&
        session.gameKey === entry.gameKey
    );
}

function sessionOwnsTabPage(session, tabId, pageInstanceId) {
    return (
        session !== null &&
        session.tabId === tabId &&
        session.pageInstanceId === pageInstanceId
    );
}

function publicState() {
    let status = "idle";
    if (activeSession !== null) {
        if (nativeProtocolError !== null || activeSession.nativeUnavailable) {
            status = "error";
        } else if (activeSession.dismissed) {
            status = "dismissed";
        } else if (activeSession.hydratedConnection === null) {
            status = "connecting";
        } else {
            status = "active";
        }
    } else if (automaticClaimsPaused) {
        status = "stopped";
    } else if (nativeProtocolError !== null) {
        status = "error";
    }

    return {
        extension_version: browser.runtime.getManifest().version,
        native_protocol_version: NATIVE_PROTOCOL_VERSION,
        status,
        automatic_claims_paused: automaticClaimsPaused,
        protocol_error: nativeProtocolError?.message ?? null,
        session:
            activeSession === null
                ? null
                : {
                      session_id: activeSession.sessionId,
                      tab_id: activeSession.tabId,
                      page_instance_id: activeSession.pageInstanceId,
                      route_generation: activeSession.routeGeneration,
                      game_key: activeSession.gameKey,
                      url: activeSession.url,
                      snapshot_seq: activeSession.snapshotSeq,
                      history_seq: activeSession.historySeq,
                      dismissed: activeSession.dismissed,
                      retry_used: activeSession.retryUsed,
                      last_error: activeSession.lastError
                  }
    };
}

function notifyStateChanged() {
    try {
        const pending = browser.runtime.sendMessage({
            type: "session_state_changed",
            state: publicState()
        });
        if (pending && typeof pending.catch === "function") {
            pending.catch(() => {});
        }
    } catch (_error) {
        // No extension page is necessarily open to receive this notification.
    }
    return persistBrokerState();
}

function protocolErrorFor(message) {
    const reason = String(message?.reason ?? "");
    if (
        message?.type === "error" &&
        /(protocol|hello_required|session_v[234]_required)/i.test(reason)
    ) {
        return new Error(`Native protocol error: ${reason || "unknown"}`);
    }
    return null;
}

function resetNativeConnection(connection) {
    if (nativeConnection === connection) {
        nativeConnection = null;
    }
}

function disconnectQuietly(connection, reason) {
    connection.intentionalReason = reason;
    try {
        connection.port.disconnect();
    } catch (_error) {
        // The native port may already be closed.
    }
}

function failHandshake(connection, error, disconnectReason = null) {
    if (!connection.settled) {
        connection.settled = true;
        clearTimeout(connection.timeoutId);
        resetNativeConnection(connection);
        connection.rejectReady(error);
    }

    if (disconnectReason !== null) {
        disconnectQuietly(connection, disconnectReason);
    }
}

function compatibleHello(message) {
    const capabilities = Array.isArray(message?.capabilities)
        ? message.capabilities
        : [];
    return (
        message?.type === "hello" &&
        message.ok === true &&
        message.protocol_version === NATIVE_PROTOCOL_VERSION &&
        REQUIRED_CAPABILITIES.every((name) => capabilities.includes(name))
    );
}

function handleOverlayCommand(message) {
    if (
        activeSession === null ||
        typeof message?.session_id !== "string" ||
        message.session_id !== activeSession.sessionId
    ) {
        return;
    }

    switch (message.command) {
        case "rescan":
            void handleOverlayRescan(message.session_id);
            break;
        case "set_fen":
        case "restart_engines":
        case "explore_start":
        case "explore_move":
        case "explore_goto":
        case "explore_live":
        case "explore_resume":
            void forwardSessionCommand(message.command, message.payload);
            break;
        case "stop_session":
            automaticClaimsPaused = true;
            pendingExplicitTabId = null;
            void endActiveSession("overlay_stop");
            break;
        default:
            console.warn(
                "[ChessListener] ignored unknown overlay command:",
                message.command
            );
    }
}

function handleNativeRuntimeMessage(connection, message) {
    const runtimeProtocolError = protocolErrorFor(message);
    if (runtimeProtocolError !== null) {
        nativeProtocolError = runtimeProtocolError;
        if (activeSession !== null) {
            activeSession.nativeUnavailable = true;
            activeSession.lastError = boundedError(runtimeProtocolError);
        }
        resetNativeConnection(connection);
        disconnectQuietly(connection, "protocol_error");
        console.error("[ChessListener]", runtimeProtocolError.message);
        notifyStateChanged();
        return;
    }

    if (message?.type === "overlay_event" && message.event === "dismissed") {
        if (
            activeSession !== null &&
            (message.session_id === activeSession.sessionId ||
                (message.session_id === undefined &&
                    activeSession.hydratedConnection === connection))
        ) {
            activeSession.dismissed = true;
            notifyStateChanged();
        }
        return;
    }

    if (message?.type === "overlay_command") {
        handleOverlayCommand(message);
        return;
    }

    if (message?.type === "error") {
        if (activeSession !== null) {
            activeSession.lastError = boundedError(
                message.reason ?? message.message,
                "Native host reported an error"
            );
            notifyStateChanged();
        }
        console.error("[ChessListener] native host error:", message);
    } else if (message?.ok === false || message?.accepted === false) {
        console.warn("[ChessListener] native host rejected a message:", message);
    }
}

function scheduleReconnectAfterDisconnect(connection, detail) {
    const session = activeSession;
    if (
        session === null ||
        session.hydratedConnection !== connection ||
        session.dismissed ||
        nativeProtocolError !== null
    ) {
        return;
    }

    session.hydratedConnection = null;
    if (session.retryUsed) {
        session.nativeUnavailable = true;
        session.lastError = boundedError(
            detail,
            "Native host disconnected again; automatic relaunch stopped"
        );
        notifyStateChanged();
        return;
    }

    session.retryUsed = true;
    notifyStateChanged();
    enqueueSessionTask(session, async () => {
        try {
            await hydrateAndDeliverLatest(session);
        } catch (error) {
            if (activeSession === session && nativeProtocolError === null) {
                session.nativeUnavailable = true;
                session.lastError = boundedError(
                    error,
                    "Native host reconnect failed"
                );
                console.error(
                    "[ChessListener] native reconnect failed:",
                    error
                );
                notifyStateChanged();
            }
        }
    });
}

function connectToNativeHost() {
    if (nativeProtocolError !== null) {
        return Promise.reject(nativeProtocolError);
    }
    if (nativeConnection !== null) {
        return nativeConnection.readyPromise;
    }

    let port;
    try {
        port = browser.runtime.connectNative(NATIVE_HOST);
    } catch (error) {
        return Promise.reject(error);
    }

    const connection = {
        port,
        ready: false,
        settled: false,
        intentionalReason: null,
        timeoutId: null,
        resolveReady: null,
        rejectReady: null,
        readyPromise: null
    };
    connection.readyPromise = new Promise((resolve, reject) => {
        connection.resolveReady = resolve;
        connection.rejectReady = reject;
    });
    nativeConnection = connection;

    port.onMessage.addListener((message) => {
        if (nativeConnection !== connection) {
            return;
        }

        if (!connection.ready) {
            if (!compatibleHello(message)) {
                const error = new Error(
                    "The native host uses an incompatible protocol or capabilities"
                );
                error.protocolError = true;
                nativeProtocolError = error;
                console.error("[ChessListener] incompatible native host:", message);
                failHandshake(connection, error, "protocol_error");
                notifyStateChanged();
                return;
            }

            connection.ready = true;
            connection.settled = true;
            clearTimeout(connection.timeoutId);
            console.info(
                `[ChessListener] native host ${message.host_version} ready`,
                message.capabilities
            );
            connection.resolveReady(connection);
            return;
        }

        handleNativeRuntimeMessage(connection, message);
    });

    port.onDisconnect.addListener((disconnectedPort) => {
        const detail = disconnectedPort?.error?.message;
        const wasReady = connection.ready;
        resetNativeConnection(connection);

        if (!connection.settled) {
            failHandshake(
                connection,
                new Error(detail || "The native host disconnected during startup")
            );
        }

        if (wasReady && connection.intentionalReason === null) {
            scheduleReconnectAfterDisconnect(connection, detail);
        }

        if (detail && connection.intentionalReason !== "protocol_error") {
            console.error("[ChessListener] native host error:", detail);
        }
    });

    connection.timeoutId = setTimeout(() => {
        failHandshake(
            connection,
            new Error("The native host handshake timed out"),
            "startup_timeout"
        );
    }, HANDSHAKE_TIMEOUT_MS);

    try {
        port.postMessage({
            type: "hello",
            protocol_version: NATIVE_PROTOCOL_VERSION,
            extension_version: browser.runtime.getManifest().version
        });
    } catch (error) {
        failHandshake(connection, error, "startup_post_failed");
    }

    return connection.readyPromise;
}

function makeSessionStart(session) {
    return {
        type: "session_start",
        session_id: session.sessionId,
        page_instance_id: session.pageInstanceId,
        route_generation: session.routeGeneration,
        game_key: session.gameKey,
        url: session.url,
        tab_id: session.tabId,
        trigger: session.trigger
    };
}

function makeNativeSnapshot(session, candidate, snapshotSeq) {
    return {
        type: "position_snapshot",
        session_id: session.sessionId,
        page_instance_id: session.pageInstanceId,
        route_generation: session.routeGeneration,
        game_key: session.gameKey,
        snapshot_seq: snapshotSeq,
        board: candidate.board,
        visually_flipped: candidate.visually_flipped,
        url: candidate.url,
        piece_count: candidate.piece_count,
        captured_at: candidate.captured_at,
        force: candidate.force,
        recovery: candidate.recovery
    };
}

function makeNativeHistory(session, candidate, historySeq) {
    const message = {
        type: "history_reconcile",
        session_id: session.sessionId,
        history_seq: historySeq,
        snapshot_seq: session.snapshotSeq,
        displayed_board: candidate.displayed_board,
        history_notation: candidate.history_notation,
        history_moves: candidate.history_moves,
        history_complete: candidate.history_complete,
        game_result: candidate.game_result ?? "*",
        captured_at: candidate.captured_at
    };
    if (candidate.initial_fen !== undefined) {
        message.initial_fen = candidate.initial_fen;
    }
    return message;
}

function enqueueSessionTask(session, task) {
    const result = session.queue.then(task, task);
    session.queue = result.catch(() => {});
    return result;
}

function postToConnection(connection, message) {
    try {
        connection.port.postMessage(message);
    } catch (error) {
        resetNativeConnection(connection);
        disconnectQuietly(connection, "post_failed");
        throw error;
    }
}

async function hydrateAndDeliverLatest(session) {
    if (
        activeSession !== session ||
        session.dismissed ||
        session.nativeUnavailable ||
        nativeProtocolError !== null
    ) {
        return;
    }

    const connection = await connectToNativeHost();
    if (
        activeSession !== session ||
        session.dismissed ||
        session.nativeUnavailable
    ) {
        return;
    }

    if (session.hydratedConnection !== connection) {
        postToConnection(connection, makeSessionStart(session));
        session.hydratedConnection = connection;
        session.lastSentSnapshotSeq = 0;
        session.lastSentHistorySeq = 0;
    }

    if (
        session.latestSnapshot !== null &&
        session.latestSnapshot.snapshot_seq > session.lastSentSnapshotSeq
    ) {
        postToConnection(connection, session.latestSnapshot);
        session.lastSentSnapshotSeq = session.latestSnapshot.snapshot_seq;
    }
    if (
        session.latestHistory !== null &&
        session.latestSnapshot !== null &&
        session.latestHistory.displayed_board === session.latestSnapshot.board &&
        session.latestHistory.history_seq > session.lastSentHistorySeq &&
        session.lastSentSnapshotSeq > 0
    ) {
        /* A recovery probe is intentionally transient and is not the baseline
         * replayed after a native restart. Associate history with whichever
         * identical-board snapshot this connection most recently received. */
        postToConnection(connection, {
            ...session.latestHistory,
            snapshot_seq: session.lastSentSnapshotSeq
        });
        session.lastSentHistorySeq = session.latestHistory.history_seq;
    }
    session.lastError = null;
    notifyStateChanged();
}

async function deliverWithSingleRetry(session) {
    try {
        await hydrateAndDeliverLatest(session);
    } catch (error) {
        if (
            activeSession !== session ||
            session.dismissed ||
            nativeProtocolError !== null
        ) {
            return;
        }
        if (session.retryUsed) {
            session.nativeUnavailable = true;
            session.lastError = boundedError(
                error,
                "Native host unavailable after automatic retry"
            );
            notifyStateChanged();
            throw error;
        }

        session.retryUsed = true;
        notifyStateChanged();
        try {
            await hydrateAndDeliverLatest(session);
        } catch (retryError) {
            if (activeSession === session && nativeProtocolError === null) {
                session.nativeUnavailable = true;
                session.lastError = boundedError(
                    retryError,
                    "Native host unavailable after automatic retry"
                );
                notifyStateChanged();
            }
            throw retryError;
        }
    }
}

function queueLatestSnapshot(session) {
    return enqueueSessionTask(session, () => deliverWithSingleRetry(session));
}

function queueRecoverySnapshot(session, snapshot) {
    return enqueueSessionTask(session, async () => {
        await deliverWithSingleRetry(session);
        if (
            activeSession !== session ||
            session.dismissed ||
            session.nativeUnavailable ||
            session.hydratedConnection === null ||
            snapshot.snapshot_seq <= session.lastSentSnapshotSeq
        ) {
            return;
        }
        postToConnection(session.hydratedConnection, snapshot);
        session.lastSentSnapshotSeq = snapshot.snapshot_seq;
    });
}

async function hydrateRestoredSessionIfNeeded() {
    if (!restoredSessionNeedsHydration) {
        return;
    }
    restoredSessionNeedsHydration = false;
    const session = activeSession;
    if (
        session === null ||
        session.dismissed ||
        session.nativeUnavailable ||
        nativeProtocolError !== null ||
        session.latestSnapshot === null
    ) {
        return;
    }
    try {
        await queueLatestSnapshot(session);
    } catch (error) {
        console.error(
            "[ChessListener] could not rehydrate restored session:",
            error
        );
    }
}

function rememberSnapshot(session, candidate) {
    const previousBoard = session.latestSnapshot?.board ?? null;
    session.snapshotSeq += 1;
    session.url = candidate.url;
    session.latestSnapshot = makeNativeSnapshot(
        session,
        candidate,
        session.snapshotSeq
    );
    if (previousBoard !== null && previousBoard !== candidate.board) {
        session.latestHistory = null;
    } else if (session.latestHistory !== null) {
        session.latestHistory.snapshot_seq = session.snapshotSeq;
    }
}

function rememberHistory(session, candidate) {
    session.historySeq += 1;
    session.latestHistory = makeNativeHistory(
        session,
        candidate,
        session.historySeq
    );
}

function newSessionFor(entry, trigger) {
    const session = {
        sessionId: createSessionId(),
        tabId: entry.tabId,
        pageInstanceId: entry.pageInstanceId,
        routeGeneration: entry.routeGeneration,
        gameKey: entry.gameKey,
        url: entry.url,
        trigger,
        snapshotSeq: 0,
        latestSnapshot: null,
        lastSentSnapshotSeq: 0,
        historySeq: 0,
        latestHistory: null,
        lastSentHistorySeq: 0,
        hydratedConnection: null,
        dismissed: false,
        retryUsed: false,
        nativeUnavailable: false,
        lastError: null,
        queue: Promise.resolve()
    };
    rememberSnapshot(session, entry.latestCandidate);
    if (
        entry.latestHistoryCandidate !== null &&
        entry.latestHistoryCandidate.displayed_board ===
            entry.latestCandidate.board
    ) {
        rememberHistory(session, entry.latestHistoryCandidate);
    }
    return session;
}

async function claimEntry(entry, trigger) {
    if (
        activeSession !== null ||
        entry.latestCandidate === null ||
        !entry.eligible
    ) {
        return false;
    }

    activeSession = newSessionFor(entry, trigger);
    restoredSessionNeedsHydration = false;
    await notifyStateChanged();
    try {
        await queueLatestSnapshot(activeSession);
    } catch (error) {
        console.error("[ChessListener] could not start native session:", error);
    }
    return true;
}

async function claimFirstEligible() {
    if (activeSession !== null || automaticClaimsPaused) {
        return false;
    }
    const candidates = [...pages.values()]
        .filter(
            (entry) =>
                entry.eligible &&
                entry.visible &&
                entry.latestCandidate !== null
        )
        .sort((left, right) => left.eligibleOrder - right.eligibleOrder);
    return candidates.length > 0
        ? claimEntry(candidates[0], "automatic")
        : false;
}

async function endActiveSession(reason, options = {}) {
    const session = activeSession;
    if (session === null) {
        return false;
    }

    activeSession = null;
    restoredSessionNeedsHydration = false;
    if (options.pauseAutomatic === true) {
        automaticClaimsPaused = true;
    }

    const connection = session.hydratedConnection;
    if (
        connection !== null &&
        connection.ready &&
        nativeConnection === connection &&
        nativeProtocolError === null
    ) {
        try {
            postToConnection(connection, {
                type: "session_end",
                session_id: session.sessionId,
                reason,
                game_result:
                    reason === "game_end"
                        ? canonicalGameResult(options.gameResult)
                        : "*"
            });
        } catch (error) {
            console.warn("[ChessListener] could not send session_end:", error);
        }
    }

    await notifyStateChanged();
    if (options.claimNext === true) {
        await claimFirstEligible();
    }
    return true;
}

async function preparePageEntry(tabId, message) {
    if (!isValidPageMessage(message)) {
        return { entry: null, stale: true };
    }

    const retired = getRetiredInstances(tabId);
    if (retired.has(message.page_instance_id)) {
        return { entry: null, stale: true };
    }

    const previous = pages.get(tabId) ?? null;
    const pageChanged =
        previous !== null &&
        previous.pageInstanceId !== message.page_instance_id;
    const olderGeneration =
        previous !== null &&
        !pageChanged &&
        message.route_generation < previous.routeGeneration;
    if (olderGeneration) {
        return { entry: null, stale: true };
    }

    const identityChanged =
        previous !== null &&
        (pageChanged ||
            message.route_generation > previous.routeGeneration ||
            message.game_key !== previous.gameKey);
    const restoredOwnerIdentityChanged =
        activeSession !== null &&
        activeSession.tabId === tabId &&
        (activeSession.pageInstanceId !== message.page_instance_id ||
            activeSession.routeGeneration !== message.route_generation ||
            activeSession.gameKey !== message.game_key);

    if (pageChanged) {
        retired.add(previous.pageInstanceId);
    }

    if (
        (identityChanged || restoredOwnerIdentityChanged) &&
        activeSession !== null &&
        activeSession.tabId === tabId
    ) {
        await endActiveSession("navigation");
    }

    let entry = previous;
    if (entry === null || identityChanged) {
        entry = {
            tabId,
            pageInstanceId: message.page_instance_id,
            routeGeneration: message.route_generation,
            gameKey: message.game_key,
            url: message.url,
            visible: false,
            eligible: false,
            eligibleOrder: ++eligibilityCounter,
            latestCandidate: null,
            lastIdentityKey: null,
            latestHistoryCandidate: null,
            lastHistoryFingerprint: null
        };
        pages.set(tabId, entry);
    }

    entry.url = message.url;
    return { entry, stale: false };
}

async function handlePageState(message, sender) {
    const tabId = senderTabId(sender);
    if (tabId === null) {
        return { accepted: false, reason: "tab_required" };
    }

    const { entry, stale } = await preparePageEntry(tabId, message);
    if (stale || entry === null) {
        return { accepted: false, stale: true };
    }

    const wasEligible = entry.eligible;
    entry.visible = message.visible === true;
    entry.eligible = message.eligible === true;
    if (!wasEligible && entry.eligible) {
        entry.eligibleOrder = ++eligibilityCounter;
    }

    if (
        activeSession !== null &&
        sameSessionIdentity(activeSession, entry) &&
        message.reason === "game_end"
    ) {
        await endActiveSession("game_end", {
            claimNext: true,
            gameResult: message.game_result
        });
    } else if (activeSession === null) {
        await claimFirstEligible();
    }

    return { accepted: true, owner: sameSessionIdentity(activeSession, entry) };
}

function snapshotIdentity(message) {
    return [
        message.page_instance_id,
        message.route_generation,
        message.game_key,
        message.board,
        message.visually_flipped === true ? "flipped" : "normal"
    ].join("|");
}

function historyFingerprint(message) {
    return [
        message.page_instance_id,
        message.route_generation,
        message.game_key,
        message.displayed_board,
        message.history_notation,
        message.history_moves,
        message.history_complete === true ? "complete" : "partial",
        message.initial_fen ?? "",
        message.game_result ?? "*"
    ].join("|");
}

async function handleBoardCandidate(message, sender) {
    const tabId = senderTabId(sender);
    if (
        tabId === null ||
        !isValidBoard(message?.board) ||
        typeof message?.visually_flipped !== "boolean"
    ) {
        return { accepted: false, reason: "invalid_snapshot" };
    }

    const { entry, stale } = await preparePageEntry(tabId, message);
    if (stale || entry === null) {
        return { accepted: false, stale: true };
    }

    entry.visible = message.visible === true;
    if (!entry.eligible) {
        entry.eligibleOrder = ++eligibilityCounter;
    }
    entry.eligible = true;

    const identity = snapshotIdentity(message);
    const duplicate = entry.lastIdentityKey === identity;
    const recovery = message.recovery === true;
    const candidate = {
        board: message.board,
        visually_flipped: message.visually_flipped,
        url: message.url,
        piece_count: Number.isInteger(message.piece_count)
            ? message.piece_count
            : null,
        captured_at: Number.isFinite(message.captured_at)
            ? message.captured_at
            : Date.now(),
        force: message.force === true,
        recovery
    };

    /* A recovery tick is only meaningful for a board that already travelled
     * through the normal fast path. It must neither claim a session nor become
     * the snapshot cached for a future session. */
    if (recovery && !duplicate) {
        return { accepted: false, reason: "recovery_without_snapshot" };
    }
    if (!recovery) {
        entry.latestCandidate = candidate;
        entry.lastIdentityKey = identity;
    }

    if (recovery) {
        if (
            activeSession === null ||
            !sameSessionIdentity(activeSession, entry)
        ) {
            return { accepted: true, owner: false, cached: false };
        }
        const session = activeSession;
        restoredSessionNeedsHydration = false;
        session.snapshotSeq += 1;
        const recoverySnapshot = makeNativeSnapshot(
            session,
            candidate,
            session.snapshotSeq
        );
        if (!session.dismissed && !session.nativeUnavailable) {
            try {
                await queueRecoverySnapshot(session, recoverySnapshot);
            } catch (error) {
                console.error(
                    "[ChessListener] could not forward delayed recovery:",
                    error
                );
            }
        }
        await notifyStateChanged();
        return {
            accepted: true,
            owner: activeSession === session,
            recovery: true,
            snapshot_seq: session.snapshotSeq
        };
    }

    if (pendingExplicitTabId === tabId) {
        pendingExplicitTabId = null;
        automaticClaimsPaused = false;
        if (activeSession !== null && !sameSessionIdentity(activeSession, entry)) {
            await endActiveSession("explicit_switch");
        } else if (
            activeSession !== null &&
            (activeSession.dismissed || activeSession.nativeUnavailable)
        ) {
            await endActiveSession("explicit_restart");
        }
        if (activeSession === null) {
            await claimEntry(entry, "explicit");
        }
        return { accepted: true, owner: sameSessionIdentity(activeSession, entry) };
    }

    if (activeSession === null) {
        if (!automaticClaimsPaused && entry.visible) {
            await claimEntry(entry, "automatic");
        }
        return { accepted: true, owner: sameSessionIdentity(activeSession, entry) };
    }

    if (!sameSessionIdentity(activeSession, entry)) {
        await hydrateRestoredSessionIfNeeded();
        return { accepted: true, owner: false, cached: true };
    }

    restoredSessionNeedsHydration = false;

    if (duplicate && message.force !== true) {
        return { accepted: true, owner: true, duplicate: true };
    }

    rememberSnapshot(activeSession, candidate);
    if (!activeSession.dismissed && !activeSession.nativeUnavailable) {
        try {
            await queueLatestSnapshot(activeSession);
        } catch (error) {
            console.error("[ChessListener] could not forward snapshot:", error);
        }
    }
    await notifyStateChanged();
    return {
        accepted: true,
        owner: true,
        snapshot_seq: activeSession.snapshotSeq
    };
}

async function handleHistoryCandidate(message, sender) {
    const tabId = senderTabId(sender);
    if (
        tabId === null ||
        !isValidPageMessage(message) ||
        !isValidHistoryCandidate(message)
    ) {
        return { accepted: false, reason: "invalid_history" };
    }

    /* Unlike a board candidate, history is not allowed to create/replace a
     * page entry or end a session. It can only enrich the exact owner identity
     * already established by the fast board path. */
    const entry = pages.get(tabId) ?? null;
    if (
        entry === null ||
        entry.pageInstanceId !== message.page_instance_id ||
        entry.routeGeneration !== message.route_generation ||
        entry.gameKey !== message.game_key
    ) {
        return { accepted: false, stale: true };
    }

    /* History never claims ownership. It is useful only when it describes the
     * exact board snapshot currently owned by this page and session. */
    if (
        activeSession === null ||
        !sameSessionIdentity(activeSession, entry) ||
        entry.latestCandidate === null ||
        entry.latestCandidate.board !== message.displayed_board ||
        activeSession.latestSnapshot === null ||
        activeSession.latestSnapshot.board !== message.displayed_board
    ) {
        return {
            accepted: false,
            owner: false,
            stale: true,
            reason: "history_snapshot_mismatch"
        };
    }

    const fingerprint = historyFingerprint(message);
    if (
        entry.lastHistoryFingerprint === fingerprint &&
        activeSession.latestHistory !== null &&
        activeSession.latestHistory.displayed_board === message.displayed_board
    ) {
        return {
            accepted: true,
            owner: true,
            duplicate: true,
            history_seq: activeSession.latestHistory.history_seq,
            snapshot_seq: activeSession.snapshotSeq
        };
    }

    const candidate = {
        displayed_board: message.displayed_board,
        history_notation: message.history_notation,
        history_moves: message.history_moves,
        history_complete: message.history_complete,
        game_result: message.game_result ?? "*",
        captured_at: Number.isFinite(message.captured_at)
            ? message.captured_at
            : Date.now()
    };
    if (message.initial_fen !== undefined) {
        candidate.initial_fen = message.initial_fen;
    }
    const session = activeSession;
    entry.latestHistoryCandidate = candidate;
    entry.lastHistoryFingerprint = fingerprint;
    rememberHistory(session, candidate);

    restoredSessionNeedsHydration = false;
    if (!session.dismissed && !session.nativeUnavailable) {
        try {
            await queueLatestSnapshot(session);
        } catch (error) {
            console.error(
                "[ChessListener] could not forward move history:",
                error
            );
        }
    }
    await notifyStateChanged();
    return {
        accepted: true,
        owner: activeSession === session,
        history_seq: session.historySeq,
        snapshot_seq: session.snapshotSeq
    };
}

async function handlePageUnloading(message, sender) {
    const tabId = senderTabId(sender);
    if (tabId === null || typeof message?.page_instance_id !== "string") {
        return { accepted: false };
    }
    const owned = sessionOwnsTabPage(
        activeSession,
        tabId,
        message.page_instance_id
    );
    const entry = pages.get(tabId);
    if (entry?.pageInstanceId === message.page_instance_id) {
        pages.delete(tabId);
        if (message.bfcache !== true) {
            getRetiredInstances(tabId).add(message.page_instance_id);
        }
    }
    if (owned) {
        await endActiveSession("navigation", { claimNext: true });
    }
    return { accepted: true };
}

async function sendContentCommand(tabId, command, extra = {}) {
    try {
        return await browser.tabs.sendMessage(tabId, {
            type: "content_command",
            command,
            ...extra
        });
    } catch (error) {
        console.warn(
            `[ChessListener] could not send ${command} to tab ${tabId}:`,
            error
        );
        return null;
    }
}

async function requestOwnerRescan(source) {
    if (activeSession === null) {
        return { ok: false, reason: "no_active_session", state: publicState() };
    }
    const result = await sendContentCommand(activeSession.tabId, "rescan", {
        page_instance_id: activeSession.pageInstanceId,
        route_generation: activeSession.routeGeneration,
        source
    });
    if (result?.accepted !== true) {
        return {
            ok: false,
            reason: result?.reason ?? "content_unavailable",
            state: publicState()
        };
    }
    return { ok: true, state: publicState() };
}

function rescanFailureReason(reason) {
    return [
        "game_ended",
        "no_supported_board",
        "content_unavailable"
    ].includes(reason)
        ? reason
        : "content_unavailable";
}

async function handleOverlayRescan(sessionId) {
    const session = activeSession;
    if (session === null || session.sessionId !== sessionId) {
        return;
    }

    const result = await requestOwnerRescan("overlay");
    if (result.ok) {
        return;
    }

    /* Navigation/game-end may have ended or replaced the session while the
     * content-script request was in flight. Its session_end frame already
     * clears the overlay's recovery state, so never report into a successor. */
    if (activeSession !== session || session.sessionId !== sessionId) {
        return;
    }

    await forwardSessionCommand(
        "rescan_result",
        rescanFailureReason(result.reason)
    );
}

async function forwardSessionCommand(command, payload) {
    const session = activeSession;
    if (session === null || session.dismissed) {
        return { ok: false, reason: "no_active_session" };
    }

    return enqueueSessionTask(session, async () => {
        await deliverWithSingleRetry(session);
        if (
            activeSession !== session ||
            session.hydratedConnection === null ||
            session.nativeUnavailable
        ) {
            return { ok: false, reason: "native_unavailable" };
        }
        const message = {
            type: "session_command",
            session_id: session.sessionId,
            command
        };
        if (payload !== undefined) {
            message.payload = payload;
        }
        postToConnection(session.hydratedConnection, message);
        return { ok: true };
    });
}

async function analyzeActiveBrowserTab() {
    if (nativeProtocolError !== null) {
        return {
            ok: false,
            reason: "native_protocol_error",
            state: publicState()
        };
    }
    const tabs = await browser.tabs.query({ active: true, currentWindow: true });
    const tabId = tabs.find((tab) => Number.isInteger(tab.id))?.id ?? null;
    if (tabId === null) {
        return { ok: false, reason: "no_active_tab", state: publicState() };
    }

    const entry = pages.get(tabId) ?? null;
    automaticClaimsPaused = false;
    if (
        entry !== null &&
        entry.eligible &&
        entry.latestCandidate !== null
    ) {
        if (activeSession !== null && sameSessionIdentity(activeSession, entry)) {
            if (
                activeSession.dismissed ||
                activeSession.nativeUnavailable
            ) {
                await endActiveSession("explicit_restart");
                await claimEntry(entry, "explicit");
            } else {
                await requestOwnerRescan("popup");
            }
        } else {
            if (activeSession !== null) {
                await endActiveSession("explicit_switch");
            }
            await claimEntry(entry, "explicit");
        }
        return { ok: true, state: publicState() };
    }

    pendingExplicitTabId = tabId;
    const result = await sendContentCommand(tabId, "rescan", {
        source: "popup"
    });
    if (result?.accepted !== true) {
        pendingExplicitTabId = null;
        await notifyStateChanged();
        return {
            ok: false,
            reason: result?.reason ?? "no_supported_board",
            state: publicState()
        };
    }
    await notifyStateChanged();
    return {
        ok: true,
        pending: true,
        state: publicState()
    };
}

async function handlePopupAction(action) {
    switch (action) {
        case "analyze_this_tab":
            return analyzeActiveBrowserTab();
        case "rescan":
            return requestOwnerRescan("popup");
        case "stop_session":
            pendingExplicitTabId = null;
            automaticClaimsPaused = true;
            await endActiveSession("manual_stop", { pauseAutomatic: true });
            await notifyStateChanged();
            return { ok: true, state: publicState() };
        default:
            return {
                ok: false,
                reason: "unknown_action",
                state: publicState()
            };
    }
}

async function dispatchRuntimeMessage(message, sender) {
    await ensureInitialized();
    switch (message?.type) {
        case "page_state": {
            const result = await handlePageState(message, sender);
            await hydrateRestoredSessionIfNeeded();
            return result;
        }
        case "board_candidate": {
            const result = await handleBoardCandidate(message, sender);
            await hydrateRestoredSessionIfNeeded();
            return result;
        }
        case "history_candidate": {
            const result = await handleHistoryCandidate(message, sender);
            await hydrateRestoredSessionIfNeeded();
            return result;
        }
        case "page_unloading":
            return handlePageUnloading(message, sender);
        case "popup_get_state":
            await hydrateRestoredSessionIfNeeded();
            return publicState();
        case "popup_action": {
            const result = await handlePopupAction(message.action);
            await hydrateRestoredSessionIfNeeded();
            return result;
        }
        default:
            return undefined;
    }
}

browser.runtime.onMessage.addListener((message, sender) => {
    if (
        ![
            "page_state",
            "board_candidate",
            "history_candidate",
            "page_unloading",
            "popup_get_state",
            "popup_action"
        ].includes(message?.type)
    ) {
        return undefined;
    }
    return dispatchRuntimeMessage(message, sender);
});

browser.tabs.onRemoved.addListener((tabId) => {
    void ensureInitialized().then(async () => {
        pages.delete(tabId);
        retiredPageInstances.delete(tabId);
        if (activeSession?.tabId === tabId) {
            await endActiveSession("tab_closed", { claimNext: true });
        }
    });
});

browser.tabs.onUpdated.addListener((tabId, changeInfo) => {
    void ensureInitialized().then(async () => {
        if (
            typeof changeInfo?.url === "string" &&
            activeSession?.tabId === tabId &&
            changeInfo.url !== activeSession.url
        ) {
            await endActiveSession("navigation");
        }
    });
});
