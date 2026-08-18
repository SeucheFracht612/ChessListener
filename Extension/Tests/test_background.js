"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const BOARD_A = `K${".".repeat(62)}k`;
const BOARD_B = `K${".".repeat(30)}Q${".".repeat(31)}k`;
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

function loadBackground(options = {}) {
    const ports = [];
    const logs = [];
    const runtimeMessages = [];
    const tabMessages = [];
    let runtimeListener = null;
    let tabRemovedListener = null;
    let tabUpdatedListener = null;
    let activeTabId = 1;
    const storageData = options.storageData ?? {};

    function clone(value) {
        return value === undefined
            ? undefined
            : JSON.parse(JSON.stringify(value));
    }

    function makePort() {
        let messageListener = null;
        let disconnectListener = null;
        const port = {
            posted: [],
            disconnected: false,
            error: null,
            postMessage(message) {
                this.posted.push(message);
            },
            disconnect() {
                this.disconnected = true;
            },
            onMessage: {
                addListener(listener) {
                    messageListener = listener;
                }
            },
            onDisconnect: {
                addListener(listener) {
                    disconnectListener = listener;
                }
            },
            emitMessage(message) {
                assert.equal(typeof messageListener, "function");
                messageListener(message);
            },
            emitDisconnect(detail = null) {
                assert.equal(typeof disconnectListener, "function");
                this.error = detail === null ? null : { message: detail };
                disconnectListener(this);
            }
        };
        ports.push(port);
        return port;
    }

    const context = {
        browser: {
            runtime: {
                connectNative(name) {
                    assert.equal(name, "local.chess_listener");
                    return makePort();
                },
                getManifest() {
                    return { version: "0.9.0" };
                },
                sendMessage(message) {
                    runtimeMessages.push(message);
                    return Promise.resolve(undefined);
                },
                onMessage: {
                    addListener(listener) {
                        runtimeListener = listener;
                    }
                }
            },
            storage: {
                session: {
                    get(key) {
                        return Promise.resolve({ [key]: clone(storageData[key]) });
                    },
                    set(values) {
                        for (const [key, value] of Object.entries(values)) {
                            storageData[key] = clone(value);
                        }
                        return Promise.resolve();
                    }
                }
            },
            tabs: {
                query(query) {
                    assert.equal(query.active, true);
                    assert.equal(query.currentWindow, true);
                    return Promise.resolve([{ id: activeTabId }]);
                },
                sendMessage(tabId, message) {
                    tabMessages.push({ tabId, message });
                    const reply =
                        typeof options.contentReply === "function"
                            ? options.contentReply(tabId, message)
                            : options.contentReply ?? { accepted: true };
                    return Promise.resolve(reply);
                },
                onRemoved: {
                    addListener(listener) {
                        tabRemovedListener = listener;
                    }
                },
                onUpdated: {
                    addListener(listener) {
                        tabUpdatedListener = listener;
                    }
                }
            }
        },
        console: {
            info(...values) {
                logs.push(["info", ...values]);
            },
            warn(...values) {
                logs.push(["warn", ...values]);
            },
            error(...values) {
                logs.push(["error", ...values]);
            }
        },
        Date,
        Map,
        Set,
        Promise,
        Error,
        Number,
        String,
        setTimeout,
        clearTimeout
    };

    const script = fs.readFileSync(
        path.join(__dirname, "..", "background.js"),
        "utf8"
    );
    vm.runInNewContext(script, context, { filename: "background.js" });
    assert.equal(typeof runtimeListener, "function");
    assert.equal(typeof tabRemovedListener, "function");
    assert.equal(typeof tabUpdatedListener, "function");

    return {
        ports,
        logs,
        runtimeMessages,
        tabMessages,
        send(message, tabId = null) {
            return runtimeListener(
                message,
                tabId === null ? {} : { tab: { id: tabId } }
            );
        },
        setActiveTab(tabId) {
            activeTabId = tabId;
        },
        removeTab(tabId) {
            tabRemovedListener(tabId, { isWindowClosing: false });
        },
        updateTab(tabId, url) {
            tabUpdatedListener(tabId, { url }, { id: tabId, url });
        }
    };
}

function candidate(tab, options = {}) {
    const generation = options.generation ?? 0;
    const page = options.page ?? `page-${tab}`;
    const game = options.game ?? `game-${tab}-${generation}`;
    return {
        type: "board_candidate",
        page_instance_id: page,
        route_generation: generation,
        game_key: game,
        url: options.url ?? `https://www.chess.com/game/live/${game}`,
        visible: options.visible ?? true,
        board: options.board ?? BOARD_A,
        piece_count: options.pieceCount ?? 2,
        visually_flipped: options.flipped ?? false,
        captured_at: options.capturedAt ?? 1000 + generation,
        force: options.force ?? false,
        recovery: options.recovery ?? false
    };
}

function historyCandidate(tab, options = {}) {
    const base = candidate(tab, options);
    return {
        type: "history_candidate",
        page_instance_id: base.page_instance_id,
        route_generation: base.route_generation,
        game_key: base.game_key,
        url: base.url,
        visible: base.visible,
        displayed_board: options.displayedBoard ?? base.board,
        history_notation: options.notation ?? "uci",
        history_moves: options.moves ?? "e2e4|e7e5",
        history_complete: options.complete ?? true,
        captured_at: options.capturedAt ?? 2000 + base.route_generation,
        ...(options.initialFen === undefined
            ? {}
            : { initial_fen: options.initialFen })
    };
}

function pageState(tab, options = {}) {
    const base = candidate(tab, options);
    return {
        type: "page_state",
        page_instance_id: base.page_instance_id,
        route_generation: base.route_generation,
        game_key: base.game_key,
        url: base.url,
        visible: base.visible,
        eligible: options.eligible ?? true,
        reason: options.reason ?? "visibility"
    };
}

function hello(port, overrides = {}) {
    port.emitMessage({
        type: "hello",
        ok: true,
        protocol_version: 4,
        host_version: "0.9.0",
        capabilities: REQUIRED_CAPABILITIES,
        ...overrides
    });
}

function nativeMessages(port, type = null) {
    return port.posted.filter(
        (message) => type === null || message.type === type
    );
}

async function flushTasks() {
    for (let index = 0; index < 5; index += 1) {
        await Promise.resolve();
    }
    await new Promise((resolve) => setImmediate(resolve));
}

async function startFirstSession(harness, tabId = 1, options = {}) {
    const pending = harness.send(candidate(tabId, options), tabId);
    await flushTasks();
    assert.equal(harness.ports.length, 1);
    assert.equal(harness.ports[0].posted[0].type, "hello");
    assert.equal(harness.ports[0].posted[0].protocol_version, 4);
    assert.equal(harness.ports[0].posted[0].extension_version, "0.9.0");
    hello(harness.ports[0]);
    const reply = await pending;
    assert.equal(reply.accepted, true);
    assert.equal(reply.owner, true);
    const messages = harness.ports[0].posted;
    assert.equal(messages[1].type, "session_start");
    assert.equal(messages[2].type, "position_snapshot");
    assert.equal(messages[2].session_id, messages[1].session_id);
    assert.equal(messages[2].snapshot_seq, 1);
    assert.equal(messages[2].page_instance_id, options.page ?? `page-${tabId}`);
    return messages[1].session_id;
}

async function testOwnershipSwitchNavigationAndClose() {
    const harness = loadBackground();
    const firstSessionId = await startFirstSession(harness, 1);
    const port = harness.ports[0];

    const cached = await harness.send(candidate(2), 2);
    assert.equal(cached.owner, false);
    assert.equal(cached.cached, true);
    assert.equal(port.posted.length, 3, "a second tab must not steal ownership");

    const duplicate = await harness.send(candidate(1), 1);
    assert.equal(duplicate.duplicate, true);
    assert.equal(port.posted.length, 3);

    const orientation = await harness.send(
        candidate(1, { flipped: true }),
        1
    );
    assert.equal(orientation.snapshot_seq, 2);
    assert.equal(port.posted.at(-1).type, "position_snapshot");
    assert.equal(port.posted.at(-1).visually_flipped, true);
    assert.equal(port.posted.at(-1).snapshot_seq, 2);

    const forced = await harness.send(
        candidate(1, { flipped: true, force: true }),
        1
    );
    assert.equal(forced.snapshot_seq, 3);
    assert.equal(port.posted.at(-1).force, true);

    harness.setActiveTab(2);
    const switched = await harness.send({
        type: "popup_action",
        action: "analyze_this_tab"
    });
    assert.equal(switched.ok, true);
    const switchTail = port.posted.slice(-3);
    assert.deepEqual(
        switchTail.map((message) => message.type),
        ["session_end", "session_start", "position_snapshot"]
    );
    assert.equal(switchTail[0].session_id, firstSessionId);
    assert.equal(switchTail[0].reason, "explicit_switch");
    const secondSessionId = switchTail[1].session_id;
    assert.notEqual(secondSessionId, firstSessionId);
    assert.equal(switchTail[2].session_id, secondSessionId);
    assert.equal(switchTail[2].snapshot_seq, 1);

    const navigationStart = port.posted.length;
    const navigated = await harness.send(
        candidate(2, {
            generation: 1,
            game: "game-2-new",
            url: "https://www.chess.com/play/computer"
        }),
        2
    );
    assert.equal(navigated.owner, true);
    const navigationMessages = port.posted.slice(navigationStart);
    assert.deepEqual(
        navigationMessages.map((message) => message.type),
        ["session_end", "session_start", "position_snapshot"]
    );
    assert.equal(navigationMessages[0].reason, "navigation");
    assert.equal(navigationMessages[0].session_id, secondSessionId);
    assert.equal(navigationMessages[2].route_generation, 1);
    assert.equal(navigationMessages[2].snapshot_seq, 1);

    const staleCount = port.posted.length;
    const stale = await harness.send(candidate(2, { generation: 0 }), 2);
    assert.equal(stale.stale, true);
    assert.equal(port.posted.length, staleCount);

    await harness.send(pageState(1, { visible: false }), 1);
    const activeId = navigationMessages[1].session_id;
    harness.removeTab(2);
    await flushTasks();
    assert.equal(port.posted.at(-1).type, "session_end");
    assert.equal(port.posted.at(-1).session_id, activeId);
    assert.equal(port.posted.at(-1).reason, "tab_closed");
    assert.equal(
        nativeMessages(port, "session_start").length,
        3,
        "a hidden cached tab must not claim after owner close"
    );
}

async function testDismissalAndOverlayCommandIsolation() {
    const harness = loadBackground();
    const sessionId = await startFirstSession(harness, 7, {
        game: "bot-game",
        url: "https://www.chess.com/play/computer"
    });
    const port = harness.ports[0];

    port.emitMessage({
        type: "overlay_command",
        command: "rescan",
        session_id: "delayed-old-session"
    });
    port.emitMessage({ type: "overlay_command", command: "rescan" });
    await flushTasks();
    assert.equal(harness.tabMessages.length, 0);

    port.emitMessage({
        type: "overlay_command",
        command: "rescan",
        session_id: sessionId
    });
    await flushTasks();
    assert.equal(harness.tabMessages.length, 1);
    assert.equal(harness.tabMessages[0].tabId, 7);
    assert.equal(harness.tabMessages[0].message.command, "rescan");

    port.emitMessage({
        type: "overlay_command",
        command: "set_fen",
        session_id: sessionId,
        payload: "8/8/8/8/8/8/4K3/7k w - - 0 1"
    });
    await flushTasks();
    const setFen = port.posted.at(-1);
    assert.equal(setFen.type, "session_command");
    assert.equal(setFen.session_id, sessionId);
    assert.equal(setFen.command, "set_fen");

    port.emitMessage({
        type: "overlay_command",
        command: "restart_engines",
        session_id: sessionId
    });
    await flushTasks();
    const restartEngines = port.posted.at(-1);
    assert.equal(restartEngines.type, "session_command");
    assert.equal(restartEngines.session_id, sessionId);
    assert.equal(restartEngines.command, "restart_engines");

    const explorationCommands = [
        ["explore_start", "8/8/8/8/8/8/4K3/7k w - - 0 1"],
        ["explore_move", "7 3 e2e4"],
        ["explore_goto", "7 2"],
        ["explore_live", "7"],
        ["explore_resume", "7 3"]
    ];
    for (const [command, payload] of explorationCommands) {
        const beforeStale = port.posted.length;
        port.emitMessage({
            type: "overlay_command",
            command,
            session_id: "delayed-old-session",
            payload
        });
        await flushTasks();
        assert.equal(
            port.posted.length,
            beforeStale,
            `${command} from a stale session must not be forwarded`
        );

        port.emitMessage({
            type: "overlay_command",
            command,
            session_id: sessionId,
            payload
        });
        await flushTasks();
        const forwarded = port.posted.at(-1);
        assert.equal(forwarded.type, "session_command");
        assert.equal(forwarded.session_id, sessionId);
        assert.equal(forwarded.command, command);
        assert.equal(forwarded.payload, payload);
    }

    port.emitMessage({
        type: "overlay_event",
        event: "dismissed",
        session_id: "delayed-old-session"
    });
    let state = await harness.send({ type: "popup_get_state" });
    assert.equal(state.status, "active");

    // Startup-close may omit the id, but only on this session's live port.
    port.emitMessage({ type: "overlay_event", event: "dismissed" });
    state = await harness.send({ type: "popup_get_state" });
    assert.equal(state.status, "dismissed");
    assert.equal(state.session.dismissed, true);

    const beforeDismissedSnapshot = port.posted.length;
    const cachedAfterDismiss = await harness.send(
        candidate(7, {
            board: BOARD_B,
            game: "bot-game",
            url: "https://www.chess.com/play/computer"
        }),
        7
    );
    assert.equal(cachedAfterDismiss.owner, true);
    assert.equal(port.posted.length, beforeDismissedSnapshot);

    port.emitDisconnect();
    await flushTasks();
    assert.equal(
        harness.ports.length,
        1,
        "dismissal must remain sticky across native disconnect"
    );
}

async function testOverlayStopUsesExactSession() {
    const harness = loadBackground();
    const sessionId = await startFirstSession(harness, 8);
    const port = harness.ports[0];
    const before = port.posted.length;
    port.emitMessage({
        type: "overlay_command",
        command: "stop_session",
        session_id: "old-session"
    });
    await flushTasks();
    assert.equal(port.posted.length, before);

    port.emitMessage({
        type: "overlay_command",
        command: "stop_session",
        session_id: sessionId
    });
    await flushTasks();
    assert.equal(port.posted.at(-1).type, "session_end");
    assert.equal(port.posted.at(-1).session_id, sessionId);
    assert.equal(port.posted.at(-1).reason, "overlay_stop");
    const state = await harness.send({ type: "popup_get_state" });
    assert.equal(state.status, "stopped");
}

async function testOverlayRescanFailureFeedback() {
    const cases = [
        {
            reason: "game_ended",
            contentReply: { accepted: false, reason: "game_ended" }
        },
        {
            reason: "no_supported_board",
            contentReply: {
                accepted: false,
                reason: "no_supported_board"
            }
        },
        {
            reason: "content_unavailable",
            contentReply() {
                return Promise.reject(new Error("content script went away"));
            }
        }
    ];

    for (const scenario of cases) {
        const harness = loadBackground({
            contentReply: scenario.contentReply
        });
        const sessionId = await startFirstSession(harness, 18);
        const port = harness.ports[0];
        port.emitMessage({
            type: "overlay_command",
            command: "rescan",
            session_id: sessionId
        });
        await flushTasks();

        const feedback = nativeMessages(port, "session_command").filter(
            (message) => message.command === "rescan_result"
        );
        assert.equal(feedback.length, 1, scenario.reason);
        assert.equal(feedback[0].session_id, sessionId);
        assert.equal(feedback[0].payload, scenario.reason);
    }
}

async function testPageUnloadCannotReclaimItself() {
    const harness = loadBackground();
    const sessionId = await startFirstSession(harness, 6);
    const port = harness.ports[0];
    const beforeUnload = port.posted.length;
    await harness.send(
        {
            type: "page_unloading",
            page_instance_id: "page-6",
            route_generation: 0,
            game_key: "game-6-0",
            url: "https://www.chess.com/game/live/game-6-0"
        },
        6
    );
    const unloadMessages = port.posted.slice(beforeUnload);
    assert.deepEqual(
        unloadMessages.map((message) => message.type),
        ["session_end"]
    );
    assert.equal(unloadMessages[0].session_id, sessionId);
    assert.equal(unloadMessages[0].reason, "navigation");
    const state = await harness.send({ type: "popup_get_state" });
    assert.equal(state.status, "idle");
    assert.equal(state.session, null);

    const stale = await harness.send(candidate(6), 6);
    assert.equal(stale.accepted, false);
    assert.equal(stale.stale, true);
}

async function testBfcachePageCanReturn() {
    const harness = loadBackground();
    const firstSessionId = await startFirstSession(harness, 19);
    const port = harness.ports[0];

    await harness.send(
        {
            type: "page_unloading",
            page_instance_id: "page-19",
            route_generation: 0,
            game_key: "game-19-0",
            url: "https://www.chess.com/game/live/game-19-0",
            bfcache: true
        },
        19
    );
    assert.equal(nativeMessages(port, "session_end").at(-1).session_id,
        firstSessionId);

    const restoredState = await harness.send(
        pageState(19, { reason: "bfcache_restore" }),
        19
    );
    assert.equal(restoredState.accepted, true);
    assert.equal(restoredState.stale, undefined);

    const restoredBoard = await harness.send(
        candidate(19, { force: true }),
        19
    );
    assert.equal(restoredBoard.accepted, true);
    assert.equal(restoredBoard.owner, true);
    const secondSessionId = nativeMessages(port, "session_start").at(-1)
        .session_id;
    assert.notEqual(secondSessionId, firstSessionId);
}

async function testUnexpectedDisconnectRehydratesOnce() {
    const harness = loadBackground();
    const sessionId = await startFirstSession(harness, 4);
    const firstPort = harness.ports[0];
    const latest = nativeMessages(firstPort, "position_snapshot").at(-1);

    firstPort.emitDisconnect("native process exited");
    await flushTasks();
    assert.equal(harness.ports.length, 2);
    const retryPort = harness.ports[1];
    assert.equal(retryPort.posted[0].type, "hello");
    hello(retryPort);
    await flushTasks();
    assert.deepEqual(
        retryPort.posted.slice(1).map((message) => message.type),
        ["session_start", "position_snapshot"]
    );
    assert.equal(retryPort.posted[1].session_id, sessionId);
    assert.equal(retryPort.posted[2].session_id, sessionId);
    assert.equal(retryPort.posted[2].snapshot_seq, latest.snapshot_seq);
    assert.equal(retryPort.posted[2].board, latest.board);

    retryPort.emitDisconnect("native process exited again");
    await flushTasks();
    assert.equal(harness.ports.length, 2, "only one relaunch is allowed");
    const state = await harness.send({ type: "popup_get_state" });
    assert.equal(state.status, "error");
    assert.equal(state.session.retry_used, true);
    assert.match(state.session.last_error, /native process exited again/);

    harness.setActiveTab(4);
    const manualRestartPending = harness.send({
        type: "popup_action",
        action: "analyze_this_tab"
    });
    await flushTasks();
    assert.equal(harness.ports.length, 3);
    hello(harness.ports[2]);
    const manualRestart = await manualRestartPending;
    assert.equal(manualRestart.ok, true);
    assert.equal(manualRestart.state.status, "active");
    assert.notEqual(manualRestart.state.session.session_id, sessionId);
    assert.equal(manualRestart.state.session.last_error, null);
}

async function testProtocolFailureNeverRetries() {
    const harness = loadBackground();
    const pending = harness.send(candidate(9), 9);
    await flushTasks();
    const port = harness.ports[0];
    hello(port, { capabilities: ["session_v2", "streaming_analysis"] });
    const reply = await pending;
    assert.equal(reply.accepted, true);
    assert.equal(port.disconnected, true);
    port.emitDisconnect("incompatible protocol");
    await flushTasks();
    assert.equal(harness.ports.length, 1);
    const state = await harness.send({ type: "popup_get_state" });
    assert.equal(state.status, "error");
    assert.match(state.protocol_error, /incompatible protocol/i);
}

async function testEventPageRestartPersistence() {
    const storageData = {};
    const first = loadBackground({ storageData });
    const sessionId = await startFirstSession(first, 12, {
        game: "persistent-game"
    });
    const latest = nativeMessages(first.ports[0], "position_snapshot").at(-1);
    await flushTasks();
    assert.equal(
        storageData.session_broker_v4.active_session.session_id,
        sessionId
    );

    const restored = loadBackground({ storageData });
    const statePending = restored.send({ type: "popup_get_state" });
    await flushTasks();
    assert.equal(restored.ports.length, 1);
    hello(restored.ports[0]);
    const restoredState = await statePending;
    assert.equal(restoredState.session.session_id, sessionId);
    assert.deepEqual(
        restored.ports[0].posted.slice(1).map((message) => message.type),
        ["session_start", "position_snapshot"]
    );
    assert.equal(restored.ports[0].posted[2].session_id, sessionId);
    assert.equal(restored.ports[0].posted[2].snapshot_seq, latest.snapshot_seq);
    assert.equal(restored.ports[0].posted[2].board, latest.board);

    restored.ports[0].emitMessage({
        type: "overlay_event",
        event: "dismissed",
        session_id: sessionId
    });
    await flushTasks();

    const dismissedRestart = loadBackground({ storageData });
    const dismissedState = await dismissedRestart.send({
        type: "popup_get_state"
    });
    assert.equal(dismissedRestart.ports.length, 0);
    assert.equal(dismissedState.status, "dismissed");
    assert.equal(dismissedState.session.session_id, sessionId);

    const stopped = await dismissedRestart.send({
        type: "popup_action",
        action: "stop_session"
    });
    assert.equal(stopped.state.status, "stopped");
    await flushTasks();

    const stoppedRestart = loadBackground({ storageData });
    const stoppedState = await stoppedRestart.send({ type: "popup_get_state" });
    assert.equal(stoppedRestart.ports.length, 0);
    assert.equal(stoppedState.status, "stopped");
    assert.equal(stoppedState.session, null);
}

async function testManualStopDoesNotImmediatelyReclaim() {
    const harness = loadBackground();
    await startFirstSession(harness, 3);
    const port = harness.ports[0];

    const stopped = await harness.send({
        type: "popup_action",
        action: "stop_session"
    });
    assert.equal(stopped.state.status, "stopped");
    assert.equal(port.posted.at(-1).reason, "manual_stop");

    const afterStop = port.posted.length;
    const update = await harness.send(
        candidate(3, { board: BOARD_B }),
        3
    );
    assert.equal(update.owner, false);
    assert.equal(port.posted.length, afterStop);

    harness.setActiveTab(3);
    const restart = await harness.send({
        type: "popup_action",
        action: "analyze_this_tab"
    });
    assert.equal(restart.ok, true);
    assert.equal(restart.state.status, "active");
    assert.equal(port.posted.at(-2).type, "session_start");
    assert.equal(port.posted.at(-1).type, "position_snapshot");
}

async function testAnalyzeRejectsUnsupportedTab() {
    const harness = loadBackground({
        contentReply: {
            accepted: false,
            reason: "no_supported_board"
        }
    });
    harness.setActiveTab(99);
    const result = await harness.send({
        type: "popup_action",
        action: "analyze_this_tab"
    });
    assert.equal(result.ok, false);
    assert.equal(result.reason, "no_supported_board");
    assert.equal(result.state.status, "idle");
    assert.equal(harness.ports.length, 0);
}

async function testHistoryAssociationRecoveryAndReconnectOrdering() {
    const storageData = {};
    const harness = loadBackground({ storageData });
    const sessionId = await startFirstSession(harness, 21, {
        game: "history-game"
    });
    const firstPort = harness.ports[0];
    const firstSnapshot = nativeMessages(firstPort, "position_snapshot")[0];
    assert.equal(firstSnapshot.snapshot_seq, 1);
    assert.equal(firstSnapshot.recovery, false);

    const beforeNormalDuplicate = firstPort.posted.length;
    const normalDuplicate = await harness.send(
        candidate(21, { game: "history-game" }),
        21
    );
    assert.equal(normalDuplicate.duplicate, true);
    assert.equal(firstPort.posted.length, beforeNormalDuplicate);

    const recovery = await harness.send(
        candidate(21, { game: "history-game", recovery: true }),
        21
    );
    assert.equal(recovery.accepted, true);
    assert.equal(recovery.recovery, true);
    assert.equal(recovery.snapshot_seq, 2);
    const recoveryFrame = nativeMessages(firstPort, "position_snapshot").at(-1);
    assert.equal(recoveryFrame.snapshot_seq, 2);
    assert.equal(recoveryFrame.recovery, true);

    const exact = await harness.send(
        historyCandidate(21, {
            game: "history-game",
            initialFen:
                "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        }),
        21
    );
    assert.equal(exact.accepted, true);
    assert.equal(exact.history_seq, 1);
    assert.equal(exact.snapshot_seq, 2);
    const firstHistory = nativeMessages(firstPort, "history_reconcile").at(-1);
    assert.deepEqual(
        {
            type: firstHistory.type,
            session_id: firstHistory.session_id,
            history_seq: firstHistory.history_seq,
            snapshot_seq: firstHistory.snapshot_seq,
            displayed_board: firstHistory.displayed_board,
            history_notation: firstHistory.history_notation,
            history_moves: firstHistory.history_moves,
            history_complete: firstHistory.history_complete
        },
        {
            type: "history_reconcile",
            session_id: sessionId,
            history_seq: 1,
            snapshot_seq: 2,
            displayed_board: BOARD_A,
            history_notation: "uci",
            history_moves: "e2e4|e7e5",
            history_complete: true
        }
    );

    const beforeHistoryDuplicate = firstPort.posted.length;
    const duplicateHistory = await harness.send(
        historyCandidate(21, { game: "history-game" }),
        21
    );
    assert.equal(duplicateHistory.duplicate, undefined);
    /* The initial_fen difference is a meaningful full-fingerprint rewrite. */
    assert.equal(duplicateHistory.history_seq, 2);
    const exactDuplicate = await harness.send(
        historyCandidate(21, { game: "history-game" }),
        21
    );
    assert.equal(exactDuplicate.duplicate, true);
    assert.equal(firstPort.posted.length, beforeHistoryDuplicate + 1);

    const rewrite = await harness.send(
        historyCandidate(21, {
            game: "history-game",
            notation: "san",
            moves: "e4|c5",
            complete: true
        }),
        21
    );
    assert.equal(rewrite.history_seq, 3);
    assert.equal(
        nativeMessages(firstPort, "history_reconcile").at(-1).history_moves,
        "e4|c5"
    );

    const staleCount = firstPort.posted.length;
    const staleHistory = await harness.send(
        historyCandidate(21, {
            game: "history-game",
            displayedBoard: BOARD_B,
            moves: "d2d4"
        }),
        21
    );
    assert.equal(staleHistory.accepted, false);
    assert.equal(staleHistory.reason, "history_snapshot_mismatch");
    assert.equal(firstPort.posted.length, staleCount);

    const sessionEndsBeforeForeignHistory = nativeMessages(
        firstPort,
        "session_end"
    ).length;
    const foreignIdentityHistory = await harness.send(
        historyCandidate(21, {
            generation: 1,
            game: "history-game-new",
            moves: "d2d4"
        }),
        21
    );
    assert.equal(foreignIdentityHistory.accepted, false);
    assert.equal(foreignIdentityHistory.stale, true);
    assert.equal(
        nativeMessages(firstPort, "session_end").length,
        sessionEndsBeforeForeignHistory,
        "history alone must never replace the owner identity"
    );

    await flushTasks();
    assert.equal(
        storageData.session_broker_v4.active_session.latest_history.history_moves,
        "e4|c5"
    );

    const restored = loadBackground({ storageData });
    const restoredStatePending = restored.send({ type: "popup_get_state" });
    await flushTasks();
    assert.equal(restored.ports.length, 1);
    hello(restored.ports[0]);
    const restoredState = await restoredStatePending;
    assert.equal(restoredState.session.session_id, sessionId);
    assert.deepEqual(
        restored.ports[0].posted.slice(1).map((message) => message.type),
        ["session_start", "position_snapshot", "history_reconcile"]
    );
    assert.equal(restored.ports[0].posted[2].snapshot_seq, 1);
    assert.equal(restored.ports[0].posted[2].recovery, false);
    assert.equal(restored.ports[0].posted[3].snapshot_seq, 1);

    firstPort.emitDisconnect("restart after history");
    await flushTasks();
    assert.equal(harness.ports.length, 2);
    const retryPort = harness.ports[1];
    hello(retryPort);
    await flushTasks();
    assert.deepEqual(
        retryPort.posted.slice(1).map((message) => message.type),
        ["session_start", "position_snapshot", "history_reconcile"]
    );
    assert.equal(retryPort.posted[1].session_id, sessionId);
    assert.equal(retryPort.posted[2].snapshot_seq, 1);
    assert.equal(retryPort.posted[2].recovery, false);
    assert.equal(retryPort.posted[3].snapshot_seq, 1);
    assert.equal(retryPort.posted[3].history_moves, "e4|c5");

    const otherTab = await harness.send(
        historyCandidate(22, { game: "other-game" }),
        22
    );
    assert.equal(otherTab.accepted, false);
    assert.equal(otherTab.stale, true);
}

async function testCrashAfterRecoveryReplaysOrdinaryBaseline() {
    const harness = loadBackground();
    const sessionId = await startFirstSession(harness, 23, {
        game: "recovery-crash"
    });
    const firstPort = harness.ports[0];
    const recovery = await harness.send(
        candidate(23, { game: "recovery-crash", recovery: true }),
        23
    );
    assert.equal(recovery.snapshot_seq, 2);
    assert.equal(
        nativeMessages(firstPort, "position_snapshot").at(-1).recovery,
        true
    );

    firstPort.emitDisconnect("crash after recovery");
    await flushTasks();
    const retryPort = harness.ports[1];
    hello(retryPort);
    await flushTasks();
    assert.deepEqual(
        retryPort.posted.slice(1).map((message) => message.type),
        ["session_start", "position_snapshot"]
    );
    assert.equal(retryPort.posted[1].session_id, sessionId);
    assert.equal(retryPort.posted[2].snapshot_seq, 1);
    assert.equal(retryPort.posted[2].recovery, false);

    const next = await harness.send(
        candidate(23, { game: "recovery-crash", board: BOARD_B }),
        23
    );
    assert.equal(next.snapshot_seq, 3);
    assert.equal(retryPort.posted.at(-1).snapshot_seq, 3);
    assert.equal(retryPort.posted.at(-1).recovery, false);
}

async function main() {
    await testOwnershipSwitchNavigationAndClose();
    await testDismissalAndOverlayCommandIsolation();
    await testOverlayStopUsesExactSession();
    await testOverlayRescanFailureFeedback();
    await testPageUnloadCannotReclaimItself();
    await testBfcachePageCanReturn();
    await testUnexpectedDisconnectRehydratesOnce();
    await testProtocolFailureNeverRetries();
    await testEventPageRestartPersistence();
    await testManualStopDoesNotImmediatelyReclaim();
    await testAnalyzeRejectsUnsupportedTab();
    await testHistoryAssociationRecoveryAndReconnectOrdering();
    await testCrashAfterRecoveryReplaysOrdinaryBaseline();
    console.log(
        "PASS ownership, recovery ordering, history association, persistence, and reconnect"
    );
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
