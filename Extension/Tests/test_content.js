"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function piece(...classes) {
    return { classList: classes };
}

function moveNode(
    attributes = {},
    textContent = "",
    descendants = [],
    options = {}
) {
    return {
        attributes,
        textContent,
        classList: options.classes ?? [],
        getAttribute(name) {
            return Object.hasOwn(this.attributes, name)
                ? this.attributes[name]
                : null;
        },
        querySelectorAll(selector) {
            if (selector === "*") {
                return descendants;
            }
            const match = /^\[([^\]]+)\]$/.exec(selector);
            return match === null
                ? []
                : descendants.filter(
                      (node) => node.getAttribute(match[1]) !== null
                  );
        },
        matches(selector) {
            const match = /^\[([^\]]+)\]$/.exec(selector);
            return match !== null && this.getAttribute(match[1]) !== null;
        }
    };
}

function moveRow(number, moves) {
    return {
        textContent: `${number}.${moves
            .map((move) => move.textContent)
            .join("")}`,
        classList: ["move-list-row"],
        getAttribute(name) {
            return name === "data-whole-move-number" ? String(number) : null;
        },
        querySelectorAll(selector) {
            if (selector.includes("move-number")) {
                return [];
            }
            return selector.includes("move") || selector.includes("data-san")
                ? moves
                : [];
        },
        matches() {
            return false;
        }
    };
}

function moveList(nodes, attributes = {}, options = {}) {
    return {
        isConnected: true,
        hidden: options.hidden ?? false,
        attributes,
        textContent:
            options.textContent ?? nodes.map((node) => node.textContent).join(" "),
        computedStyle: {
            display: options.display ?? "block",
            visibility: options.visibility ?? "visible",
            opacity: options.opacity ?? "1"
        },
        getAttribute(name) {
            if (name === "aria-hidden") {
                return options.ariaHidden ?? null;
            }
            return Object.hasOwn(this.attributes, name)
                ? this.attributes[name]
                : null;
        },
        matches() {
            return false;
        },
        getBoundingClientRect() {
            return {
                width: options.width ?? 320,
                height: options.height ?? 480
            };
        },
        querySelectorAll(selector) {
            if (selector.includes("move-list-row")) {
                return options.rows ?? [];
            }
            if (selector === "[data-ply]") {
                return nodes.filter(
                    (node) => node.getAttribute("data-ply") !== null
                );
            }
            const attribute = /^\[([^\]]+)\]$/.exec(selector);
            if (attribute !== null) {
                return nodes.filter(
                    (node) => node.getAttribute(attribute[1]) !== null
                );
            }
            if (selector.includes(",")) {
                return nodes.filter(
                    (node) =>
                        node.getAttribute("data-san") !== null ||
                        node.getAttribute("data-move") !== null ||
                        node.textContent.length > 0
                );
            }
            return [];
        }
    };
}

function modal(options = {}) {
    return {
        hidden: options.hidden ?? false,
        textContent: options.textContent ?? "",
        getAttribute(name) {
            if (name === "aria-hidden") {
                return options.ariaHidden ?? null;
            }
            return options.attributes?.[name] ?? null;
        },
        computedStyle: {
            display: options.display ?? "block",
            visibility: options.visibility ?? "visible",
            opacity: options.opacity ?? "1"
        },
        getBoundingClientRect() {
            return {
                width: options.width ?? 320,
                height: options.height ?? 180
            };
        }
    };
}

function loadContent() {
    const runtimeMessages = [];
    const timers = new Map();
    const documentListeners = new Map();
    const windowListeners = new Map();
    const observers = [];
    const historyContainers = new Map();
    let timerId = 0;
    let runtimeListener = null;
    let gameOverModal = null;
    let boardPieces = [piece("wk", "square-11"), piece("bk", "square-88")];
    let boardFlipped = false;

    const board = {
        id: "board",
        isConnected: true,
        classList: {
            contains(name) {
                return name === "flipped" && boardFlipped;
            }
        },
        contains() {
            return true;
        },
        querySelectorAll(selector) {
            assert.equal(selector, ".piece");
            return boardPieces;
        },
        getBoundingClientRect() {
            return { width: 640, height: 640 };
        }
    };

    const gameOverSelectors = new Set([
        '[data-cy="game-over-modal"]',
        '[data-cy="game-over-dialog"]',
        "game-over-modal",
        ".game-over-modal-content",
        ".game-over-modal"
    ]);
    const document = {
        visibilityState: "visible",
        querySelector(selector) {
            if (gameOverSelectors.has(selector)) {
                return gameOverModal;
            }
            const containers = historyContainers.get(selector) ?? [];
            return containers[0] ?? null;
        },
        querySelectorAll(selector) {
            if (selector === "wc-chess-board") {
                return [board];
            }
            return historyContainers.get(selector) ?? [];
        },
        addEventListener(type, listener) {
            documentListeners.set(type, listener);
        }
    };
    const window = {
        location: {
            href: "https://www.chess.com/play/computer",
            pathname: "/play/computer"
        },
        addEventListener(type, listener) {
            windowListeners.set(type, listener);
        },
        getComputedStyle(element) {
            return element.computedStyle;
        }
    };

    class TestMutationObserver {
        constructor(callback) {
            this.callback = callback;
            this.target = null;
            this.options = null;
            this.disconnected = false;
            observers.push(this);
        }
        observe(target, options) {
            this.target = target;
            this.options = options;
        }
        disconnect() {
            this.disconnected = true;
        }
    }

    const context = vm.createContext({
        browser: {
            runtime: {
                sendMessage(message) {
                    runtimeMessages.push(message);
                    return Promise.resolve({ accepted: true });
                },
                onMessage: {
                    addListener(listener) {
                        runtimeListener = listener;
                    }
                }
            }
        },
        document,
        window,
        MutationObserver: TestMutationObserver,
        URL,
        Date,
        Math,
        Map,
        Promise,
        console: {
            debug() {},
            error() {}
        },
        setTimeout(callback, delay) {
            timerId += 1;
            timers.set(timerId, { callback, delay });
            return timerId;
        },
        clearTimeout(id) {
            timers.delete(id);
        },
        setInterval() {
            return 1;
        },
        testBoard: board
    });

    const script = fs.readFileSync(
        path.join(__dirname, "..", "content.js"),
        "utf8"
    );
    vm.runInContext(script, context, { filename: "content.js" });

    return {
        context,
        runtimeMessages,
        timers,
        observers,
        documentListeners,
        windowListeners,
        runtimeListener: () => runtimeListener,
        setModal(value) {
            gameOverModal = value;
        },
        setPieces(value) {
            boardPieces = value;
        },
        setHistory(selector, container) {
            historyContainers.clear();
            if (selector !== null) {
                historyContainers.set(
                    selector,
                    Array.isArray(container) ? container : [container]
                );
            }
        },
        runTimer(delay) {
            const found = [...timers.entries()].find(
                ([, timer]) => timer.delay === delay
            );
            assert.notEqual(found, undefined, `missing ${delay}ms timer`);
            timers.delete(found[0]);
            found[1].callback();
        }
    };
}

async function flushTasks() {
    for (let index = 0; index < 8; index += 1) {
        await Promise.resolve();
    }
}

function historyResult(context, container) {
    context.testHistoryContainer = container;
    return vm.runInContext("readMoveHistory(testHistoryContainer)", context);
}

async function testLifecycleAndFastStableCapture() {
    const harness = loadContent();
    const { context } = harness;

    assert.equal(vm.runInContext("isGamePage()", context), true);
    assert.equal(typeof harness.documentListeners.get("pointercancel"), "function");
    assert.equal(typeof harness.windowListeners.get("pagehide"), "function");
    assert.equal(typeof harness.windowListeners.get("pageshow"), "function");

    harness.setModal(modal({ hidden: true }));
    assert.equal(vm.runInContext("detectGameOver()", context), false);
    harness.setModal(modal({ ariaHidden: "true" }));
    assert.equal(vm.runInContext("detectGameOver()", context), false);
    harness.setModal(modal({ display: "none" }));
    assert.equal(vm.runInContext("detectGameOver()", context), false);
    harness.setModal(modal({ width: 0, height: 0 }));
    assert.equal(vm.runInContext("detectGameOver()", context), false);
    harness.setHistory(
        "wc-simple-move-list",
        moveList([
            moveNode({ "data-ply": "1", "data-san": "e4" }, "e4"),
            moveNode({ "data-ply": "2", "data-san": "e5" }, "e5")
        ])
    );
    harness.setModal(modal({ textContent: "Game over 1-0" }));
    assert.equal(vm.runInContext("detectGameOver()", context), true);
    assert.equal(vm.runInContext("readVisibleGameResult()", context), "1-0");

    harness.runtimeMessages.length = 0;
    harness.timers.clear();
    vm.runInContext("refreshContext()", context);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(vm.runInContext("gameIsOver", context), true);
    assert.equal(harness.runtimeMessages.at(-1).reason, "game_end");
    assert.equal(harness.runtimeMessages.at(-1).game_result, "1-0");
    assert.equal(
        harness.runtimeMessages.find(
            (message) => message.type === "history_candidate"
        )?.game_result,
        "1-0"
    );

    harness.setModal(null);
    vm.runInContext("refreshContext()", context);
    assert.equal(vm.runInContext("gameIsOver", context), true);
    assert.equal(
        harness.runtimeMessages.some((message) => message.reason === "new_game"),
        false
    );

    context.window.location.href = "https://www.chess.com/game/live/new-game";
    context.window.location.pathname = "/game/live/new-game";
    vm.runInContext("refreshContext()", context);
    assert.equal(vm.runInContext("gameIsOver", context), false);
    assert.equal(harness.runtimeMessages.at(-1).reason, "new_game");

    harness.runtimeMessages.length = 0;
    harness.timers.clear();
    harness.windowListeners.get("pagehide")({ persisted: true });
    assert.equal(harness.runtimeMessages.at(-1).type, "page_unloading");
    assert.equal(harness.runtimeMessages.at(-1).bfcache, true);
    harness.windowListeners.get("pageshow")({ persisted: true });
    assert.equal(vm.runInContext("forceNextCapture", context), true);
    assert.equal(
        [...harness.timers.values()].some((timer) => timer.delay === 32),
        true
    );

    harness.runtimeMessages.length = 0;
    harness.timers.clear();
    vm.runInContext(
        "activeBoard = testBoard; gameIsOver = false; pointerIsDown = false; " +
            "forceNextCapture = true; scheduleCapture(true);",
        context
    );
    harness.runTimer(32);
    await flushTasks();
    assert.equal(
        harness.runtimeMessages.some((message) => message.type === "board_candidate"),
        false,
        "the first read must not be submitted"
    );
    harness.runTimer(24);
    await flushTasks();
    const normal = harness.runtimeMessages.find(
        (message) => message.type === "board_candidate"
    );
    assert.notEqual(normal, undefined);
    assert.equal(normal.recovery, undefined);
    assert.equal(
        [...harness.timers.values()].some((timer) => timer.delay === 700),
        true,
        "recovery must be delayed well beyond the normal path"
    );

    const oldRecovery = [...harness.timers.entries()].find(
        ([, timer]) => timer.delay === 700
    );
    assert.notEqual(oldRecovery, undefined);
    harness.timers.delete(oldRecovery[0]);
    const messagesBeforeRecovery = harness.runtimeMessages.length;
    const boardObserver = harness.observers.find(
        (observer) => observer.target === context.testBoard && !observer.disconnected
    );
    assert.notEqual(boardObserver, undefined);
    boardObserver.callback([{ type: "attributes", attributeName: "class" }]);
    oldRecovery[1].callback();
    await flushTasks();
    assert.equal(
        harness.runtimeMessages.length,
        messagesBeforeRecovery + 1,
        "a harmless post-capture class mutation must not cancel recovery"
    );
    assert.equal(harness.runtimeMessages.at(-1).recovery, true);

    // Invalidation during the confirming read prevents stale submission.
    harness.runtimeMessages.length = 0;
    harness.timers.clear();
    vm.runInContext("forceNextCapture = true; scheduleCapture(true)", context);
    harness.runTimer(32);
    await flushTasks();
    const confirm = [...harness.timers.entries()].find(
        ([, timer]) => timer.delay === 24
    );
    assert.notEqual(confirm, undefined);
    vm.runInContext("invalidateCaptures()", context);
    confirm[1].callback();
    await flushTasks();
    assert.equal(
        harness.runtimeMessages.some((message) => message.type === "board_candidate"),
        false
    );
    assert.equal(vm.runInContext("forceNextCapture", context), true);

    // A genuinely changing board also retries instead of forwarding a mixed read.
    harness.timers.clear();
    const pending = vm.runInContext(
        "captureStablePosition(captureEpoch, true)",
        context
    );
    await flushTasks();
    harness.setPieces([
        piece("wk", "square-11"),
        piece("wq", "square-44"),
        piece("bk", "square-88")
    ]);
    harness.runTimer(24);
    await pending;
    assert.equal(
        [...harness.timers.values()].some((timer) => timer.delay === 32),
        true
    );

    context.window.location.href = "https://www.chess.com/home";
    context.window.location.pathname = "/home";
    const unsupported = await harness.runtimeListener()({
        type: "content_command",
        command: "rescan"
    });
    assert.equal(unsupported.accepted, false);
    assert.equal(unsupported.reason, "no_supported_board");
}

async function testGameEndResultWithoutHistory() {
    const harness = loadContent();
    const { context } = harness;

    harness.setHistory(null, null);
    harness.setModal(modal({ textContent: "Black wins 0-1" }));
    harness.runtimeMessages.length = 0;
    harness.timers.clear();
    vm.runInContext("refreshContext()", context);
    await flushTasks();

    assert.equal(
        harness.runtimeMessages.some(
            (message) => message.type === "history_candidate"
        ),
        false
    );
    const endState = harness.runtimeMessages.find(
        (message) => message.type === "page_state" && message.reason === "game_end"
    );
    assert.notEqual(endState, undefined);
    assert.equal(endState.game_result, "0-1");

    const malformed = vm.runInContext(
        'currentPageState("game_end", "White won")',
        context
    );
    assert.equal(malformed.game_result, "*");
    const ordinary = vm.runInContext(
        'currentPageState("visibility", "1-0")',
        context
    );
    assert.equal(Object.hasOwn(ordinary, "game_result"), false);
}

function testHistoryAdaptersAndNormalization() {
    const harness = loadContent();
    const { context } = harness;
    const selectors = [
        "wc-simple-move-list",
        "wc-move-list",
        "vertical-move-list",
        '[data-cy="move-list"]',
        '[data-testid="move-list"]'
    ];
    for (const selector of selectors) {
        const container = moveList([
            moveNode({ "data-ply": "1", "data-uci": "e2e4" })
        ]);
        harness.setHistory(selector, container);
        context.expectedHistoryContainer = container;
        assert.equal(
            vm.runInContext(
                "findHistoryContainer() === expectedHistoryContainer",
                context
            ),
            true,
            selector
        );
    }
    const hiddenContainer = moveList([], {}, { display: "none" });
    const visibleContainer = moveList([
        moveNode({ "data-ply": "1", "data-san": "e4" })
    ]);
    harness.setHistory("wc-simple-move-list", [
        hiddenContainer,
        visibleContainer
    ]);
    context.expectedHistoryContainer = visibleContainer;
    assert.equal(
        vm.runInContext(
            "findHistoryContainer() === expectedHistoryContainer",
            context
        ),
        true,
        "the visible game list wins over a hidden duplicate"
    );

    let result = historyResult(
        context,
        moveList([
            moveNode({ "data-ply": "1", "data-uci": "E2E4" }),
            moveNode({ "data-ply": "2", "data-uci": "e7e5" })
        ])
    );
    assert.deepEqual(JSON.parse(JSON.stringify(result)), {
        notation: "uci",
        moves: "e2e4|e7e5",
        complete: true,
        initialFen: null,
        result: "*"
    });

    result = historyResult(
        context,
        moveList(
            [
                moveNode({ "data-ply": "0", "data-san": "\u2658f3?!" }),
                moveNode({ "data-ply": "1", "data-san": "0-0" })
            ],
            {
                "data-initial-fen":
                    "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
            }
        )
    );
    assert.equal(result.notation, "san");
    assert.equal(result.moves, "Nf3|O-O");
    assert.equal(result.complete, true);
    assert.match(result.initialFen, /^rnbqkbnr/);

    const nestedUci = moveNode(
        { "data-ply": "1" },
        "ignored",
        [moveNode({ "data-uci": "g1f3" })]
    );
    result = historyResult(context, moveList([nestedUci]));
    assert.equal(result.notation, "uci");
    assert.equal(result.moves, "g1f3");
    assert.equal(result.complete, true);

    result = historyResult(
        context,
        moveList([
            moveNode({ "data-san": "e4" }, "e4"),
            moveNode({ "data-san": "e5" }, "e5")
        ])
    );
    assert.equal(result.moves, "e4|e5");
    assert.equal(result.complete, false, "unnumbered DOM must stay partial");

    result = historyResult(
        context,
        moveList([
            moveNode({ "data-ply": "2", "data-uci": "e2e4" }),
            moveNode({ "data-ply": "3", "data-uci": "e7e5" })
        ])
    );
    assert.equal(result.complete, false, "a virtualized tail is not complete");

    result = historyResult(
        context,
        moveList(
            [],
            {},
            { textContent: "1. e4 e5 2. \u2658f3 Nc6 3. Bb5 a6 1-0" }
        )
    );
    assert.equal(result.notation, "san");
    assert.equal(result.moves, "e4|e5|Nf3|Nc6|Bb5|a6");
    assert.equal(result.result, "1-0");
    assert.equal(
        result.complete,
        true,
        "strict full container text beginning at move 1 is authoritative"
    );
    assert.equal(
        historyResult(
            context,
            moveList(
                [],
                {},
                { textContent: "1. e4 e5 (2. Nf3 Nc6)" }
            )
        ),
        null,
        "variation text is never mistaken for the game history"
    );

    /* Bot games use paired data-whole-move-number rows. Their data-ply move
     * children render piece letters through icon-font PUA glyphs, so raw
     * textContent looks like a pawn move unless the icon class is restored. */
    const iconMove = (ply, text, pieceName, color) =>
        moveNode(
            { "data-ply": String(ply) },
            `\ue000${text}`,
            [
                moveNode({}, "", [], {
                    classes: ["icon-font-chess", `${pieceName}-${color}`]
                })
            ],
            { classes: [color] }
        );
    const botMoves = [
        moveNode({ "data-ply": "0" }, "e4", [], { classes: ["white"] }),
        iconMove(1, "f6", "knight", "black"),
        iconMove(2, "g4", "queen", "white"),
        iconMove(3, "xg4", "knight", "black"),
        iconMove(4, "f3", "knight", "white"),
        moveNode({ "data-ply": "5" }, "d5", [], { classes: ["black"] }),
        moveNode({ "data-ply": "6" }, "d4", [], { classes: ["white"] }),
        iconMove(7, "e3", "knight", "black"),
        iconMove(8, "c3", "knight", "white"),
        moveNode({ "data-ply": "9" }, "dxe4", [], { classes: ["black"] })
    ];
    result = historyResult(
        context,
        moveList(botMoves, {}, {
            rows: [
                moveRow(1, botMoves.slice(0, 2)),
                moveRow(2, botMoves.slice(2, 4)),
                moveRow(3, botMoves.slice(4, 6)),
                moveRow(4, botMoves.slice(6, 8)),
                moveRow(5, botMoves.slice(8, 10))
            ],
            textContent:
                "1.e4\ue000f62.\ue000g4\ue000xg4" +
                "3.\ue000f3d54.d4\ue000e35.\ue000c3dxe4"
        })
    );
    assert.equal(result.notation, "san");
    assert.equal(
        result.moves,
        "e4|Nf6|Qg4|Nxg4|Nf3|d5|d4|Ne3|Nc3|dxe4"
    );
    assert.equal(result.complete, true);

    const oversized = Array.from({ length: 1025 }, (_, index) =>
        moveNode({ "data-ply": String(index), "data-uci": "e2e4" })
    );
    assert.equal(historyResult(context, moveList(oversized)), null);
}

async function testIndependentHistoryRewriteAndDelayedRecovery() {
    const harness = loadContent();
    const { context } = harness;
    const nodes = [
        moveNode({ "data-ply": "1", "data-san": "e4" }, "e4"),
        moveNode({ "data-ply": "2", "data-san": "e5" }, "e5")
    ];
    const container = moveList(nodes);
    harness.setHistory("wc-simple-move-list", container);
    vm.runInContext("refreshContext('fixture')", context);
    harness.runtimeMessages.length = 0;
    harness.timers.clear();

    vm.runInContext("scheduleCapture(true)", context);
    harness.runTimer(32);
    await flushTasks();
    harness.runTimer(24);
    await flushTasks();
    assert.deepEqual(
        harness.runtimeMessages.map((message) => message.type),
        ["board_candidate"]
    );

    harness.runTimer(280);
    await flushTasks();
    const firstHistory = harness.runtimeMessages.at(-1);
    assert.equal(firstHistory.type, "history_candidate");
    assert.equal(firstHistory.history_moves, "e4|e5");
    assert.equal(firstHistory.history_complete, true);

    harness.runTimer(700);
    await flushTasks();
    const recovery = harness.runtimeMessages.at(-1);
    assert.equal(recovery.type, "board_candidate");
    assert.equal(recovery.recovery, true);
    assert.deepEqual(
        harness.runtimeMessages.map((message) => message.type),
        ["board_candidate", "history_candidate", "board_candidate"]
    );

    nodes.pop();
    const historyObserver = harness.observers.find(
        (observer) => observer.target === container && !observer.disconnected
    );
    assert.notEqual(historyObserver, undefined);
    historyObserver.callback([]);
    harness.runTimer(280);
    await flushTasks();
    const rewritten = harness.runtimeMessages.at(-1);
    assert.equal(rewritten.type, "history_candidate");
    assert.equal(rewritten.history_moves, "e4");

    historyObserver.callback([]);
    harness.runTimer(280);
    await flushTasks();
    assert.equal(
        harness.runtimeMessages.filter(
            (message) => message.type === "history_candidate"
        ).length,
        2,
        "an identical history fingerprint is suppressed"
    );

    harness.setPieces([
        piece("wk", "square-11"),
        piece("wq", "square-44"),
        piece("bk", "square-88")
    ]);
    historyObserver.callback([]);
    harness.runTimer(280);
    await flushTasks();
    assert.equal(
        harness.runtimeMessages.filter(
            (message) => message.type === "history_candidate"
        ).length,
        2
    );
}

async function main() {
    await testLifecycleAndFastStableCapture();
    await testGameEndResultWithoutHistory();
    testHistoryAdaptersAndNormalization();
    await testIndependentHistoryRewriteAndDelayedRecovery();
    console.log(
        "PASS fast capture, stale guards, history adapters, rewrites, and delayed recovery"
    );
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
