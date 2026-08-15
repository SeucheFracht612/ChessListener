"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function piece(...classes) {
    return { classList: classes };
}

function loadContent() {
    const runtimeMessages = [];
    const timers = new Map();
    const documentListeners = new Map();
    const windowListeners = new Map();
    let timerId = 0;
    let runtimeListener = null;
    let modal = null;

    const board = {
        id: "board",
        isConnected: true,
        classList: {
            contains(name) {
                return name === "flipped" ? false : false;
            }
        },
        contains() {
            return true;
        },
        querySelectorAll(selector) {
            assert.equal(selector, ".piece");
            return [piece("wk", "square-11"), piece("bk", "square-88")];
        },
        getBoundingClientRect() {
            return { width: 640, height: 640 };
        }
    };

    const document = {
        visibilityState: "visible",
        querySelector() {
            return modal;
        },
        querySelectorAll(selector) {
            assert.equal(selector, "wc-chess-board");
            return [board];
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
        MutationObserver: class {
            observe() {}
            disconnect() {}
        },
        URL,
        Date,
        Math,
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
        documentListeners,
        windowListeners,
        runtimeListener: () => runtimeListener,
        setModal(value) {
            modal = value;
        }
    };
}

function modal(options = {}) {
    return {
        hidden: options.hidden ?? false,
        getAttribute(name) {
            return name === "aria-hidden" ? options.ariaHidden ?? null : null;
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

async function main() {
    const harness = loadContent();
    const { context } = harness;

    assert.equal(vm.runInContext("isGamePage()", context), true);
    assert.equal(typeof harness.documentListeners.get("pointercancel"), "function");
    assert.equal(
        typeof harness.documentListeners.get("visibilitychange"),
        "function"
    );
    assert.equal(typeof harness.windowListeners.get("blur"), "function");
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
    harness.setModal(modal());
    assert.equal(vm.runInContext("detectGameOver()", context), true);

    harness.runtimeMessages.length = 0;
    harness.timers.clear();
    vm.runInContext("refreshContext()", context);
    assert.equal(vm.runInContext("gameIsOver", context), true);
    assert.equal(harness.runtimeMessages.at(-1).reason, "game_end");

    // Closing a result dialog is not a new game. The ended state stays sticky
    // until the route or actual board identity changes.
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
    assert.equal(
        harness.runtimeMessages.some(
            (message) =>
                message.type === "page_state" &&
                message.reason === "bfcache_restore"
        ),
        true
    );
    assert.equal(vm.runInContext("forceNextCapture", context), true);
    assert.equal(
        [...harness.timers.values()].some((timer) => timer.delay === 120),
        true
    );

    harness.runtimeMessages.length = 0;
    harness.timers.clear();
    vm.runInContext(
        "activeBoard = testBoard; gameIsOver = false; pointerIsDown = false; " +
            "forceNextCapture = true;",
        context
    );
    const pendingCapture = vm.runInContext(
        "captureStablePosition(captureEpoch, true)",
        context
    );
    await Promise.resolve();
    const stableReadDelay = [...harness.timers.values()].find(
        (timer) => timer.delay === 75
    );
    assert.notEqual(stableReadDelay, undefined);

    // A navigation/mutation/visibility invalidation during the stable-read
    // delay must prevent this old asynchronous capture from being submitted.
    vm.runInContext("invalidateCaptures()", context);
    stableReadDelay.callback();
    await pendingCapture;
    assert.equal(
        harness.runtimeMessages.some(
            (message) => message.type === "board_candidate"
        ),
        false
    );
    assert.equal(
        vm.runInContext("forceNextCapture", context),
        true,
        "a stale read must not consume an explicit re-read request"
    );

    context.window.location.href = "https://www.chess.com/home";
    context.window.location.pathname = "/home";
    const unsupported = await harness.runtimeListener()({
        type: "content_command",
        command: "rescan"
    });
    assert.equal(unsupported.accepted, false);
    assert.equal(unsupported.reason, "no_supported_board");

    console.log(
        "PASS content lifecycle, pointer recovery, and stale capture guards"
    );
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
