"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function element() {
    const listeners = new Map();
    const attributes = new Map();
    return {
        textContent: "",
        hidden: false,
        disabled: false,
        open: false,
        dataset: {},
        setAttribute(name, value) {
            attributes.set(name, String(value));
        },
        getAttribute(name) {
            return attributes.get(name) ?? null;
        },
        addEventListener(type, listener) {
            listeners.set(type, listener);
        },
        click() {
            listeners.get("click")?.();
        }
    };
}

function deferred() {
    let resolve;
    const promise = new Promise((onResolve) => {
        resolve = onResolve;
    });
    return { promise, resolve };
}

async function flushTasks() {
    for (let index = 0; index < 6; index += 1) {
        await Promise.resolve();
    }
    await new Promise((resolve) => setImmediate(resolve));
}

function cssToken(css, name) {
    const match = css.match(new RegExp(`--${name}:\\s*(#[0-9a-f]{6})`, "i"));
    assert.ok(match, `missing CSS token --${name}`);
    return match[1];
}

function relativeLuminance(hex) {
    const channels = [1, 3, 5].map((offset) =>
        Number.parseInt(hex.slice(offset, offset + 2), 16) / 255
    ).map((value) =>
        value <= 0.04045
            ? value / 12.92
            : ((value + 0.055) / 1.055) ** 2.4
    );
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
}

function contrastRatio(first, second) {
    const values = [relativeLuminance(first), relativeLuminance(second)]
        .sort((a, b) => b - a);
    return (values[0] + 0.05) / (values[1] + 0.05);
}

async function main() {
    const ids = [
        "popup-root",
        "status-badge",
        "session-label",
        "session-detail",
        "error-panel",
        "error-title",
        "error",
        "error-details",
        "error-technical",
        "version",
        "analyze",
        "stop"
    ];
    const elements = new Map(ids.map((id) => [`#${id}`, element()]));
    let runtimeListener = null;
    let activeTab = 7;
    let tabQueryResponse = null;
    let actionResponse = null;
    const sentActions = [];

    const idleState = {
        extension_version: "0.9.5",
        native_protocol_version: 4,
        status: "idle",
        protocol_error: null,
        session: null
    };

    const context = vm.createContext({
        document: {
            querySelector(selector) {
                return elements.get(selector) ?? null;
            }
        },
        browser: {
            tabs: {
                query(query) {
                    assert.deepEqual(
                        { ...query },
                        { active: true, currentWindow: true }
                    );
                    const response = tabQueryResponse;
                    tabQueryResponse = null;
                    return response?.promise ?? Promise.resolve([{ id: activeTab }]);
                }
            },
            runtime: {
                sendMessage(message) {
                    if (message.type === "popup_get_state") {
                        return Promise.resolve(idleState);
                    }
                    assert.equal(message.type, "popup_action");
                    sentActions.push(message.action);
                    if (actionResponse?.promise !== undefined) {
                        return actionResponse.promise;
                    }
                    return Promise.resolve(actionResponse);
                },
                onMessage: {
                    addListener(listener) {
                        runtimeListener = listener;
                    }
                }
            }
        },
        URL,
        Promise,
        String,
        console
    });

    const script = fs.readFileSync(
        path.join(__dirname, "..", "popup.js"),
        "utf8"
    );
    vm.runInContext(script, context, { filename: "popup.js" });
    await flushTasks();

    assert.equal(elements.get("#status-badge").textContent, "Idle");
    assert.equal(elements.get("#analyze").textContent, "Start analysis");
    assert.equal(elements.get("#stop").disabled, true);
    assert.equal(elements.get("#stop").hidden, true);
    assert.equal(elements.get("#popup-root").getAttribute("aria-busy"), "false");
    assert.equal(elements.get("#version").textContent, "v0.9.5");

    // Busy is a visible interaction state, not only a disabled control.
    actionResponse = deferred();
    elements.get("#analyze").click();
    assert.equal(elements.get("#status-badge").textContent, "Starting…");
    assert.equal(elements.get("#status-badge").dataset.status, "busy");
    assert.equal(elements.get("#analyze").textContent, "Starting…");
    assert.equal(elements.get("#analyze").disabled, true);
    assert.equal(elements.get("#popup-root").getAttribute("aria-busy"), "true");
    actionResponse.resolve({
        ok: false,
        reason: "no_supported_board",
        state: idleState
    });
    await flushTasks();
    assert.deepEqual(sentActions, ["analyze_this_tab"]);
    assert.equal(elements.get("#error-panel").hidden, false);
    assert.equal(elements.get("#error-title").textContent, "No supported board found");
    assert.match(elements.get("#error").textContent, /live game, bot game/i);
    assert.doesNotMatch(elements.get("#error").textContent, /no_supported_board/);
    assert.equal(elements.get("#error-details").hidden, true);
    assert.equal(elements.get("#analyze").disabled, false);
    assert.equal(elements.get("#popup-root").getAttribute("aria-busy"), "false");

    const activeState = {
        ...idleState,
        status: "active",
        session: {
            tab_id: 7,
            snapshot_seq: 4,
            url: "https://www.chess.com/play/computer/Martin-Bot",
            last_error: null
        }
    };
    runtimeListener({
        type: "session_state_changed",
        state: { ...activeState, status: "connecting" }
    });
    await flushTasks();
    assert.equal(elements.get("#status-badge").textContent, "Connecting");
    assert.equal(elements.get("#session-detail").textContent, "Starting the local analyzer…");

    runtimeListener({ type: "session_state_changed", state: activeState });
    await flushTasks();
    assert.equal(elements.get("#status-badge").textContent, "Active");
    assert.equal(elements.get("#analyze").textContent, "Refresh this board");
    assert.equal(elements.get("#session-label").textContent, "Computer game · Chess.com");
    assert.equal(
        elements.get("#session-detail").textContent,
        "Position synced · overlay running locally"
    );
    assert.doesNotMatch(elements.get("#session-detail").textContent, /Tab|Snapshot|Protocol/);
    assert.equal(elements.get("#stop").disabled, false);
    assert.equal(elements.get("#stop").hidden, false);

    // An older active-tab lookup must not roll the popup back to stale state.
    const oldQuery = deferred();
    tabQueryResponse = oldQuery;
    runtimeListener({ type: "session_state_changed", state: activeState });
    const newQuery = deferred();
    tabQueryResponse = newQuery;
    runtimeListener({
        type: "session_state_changed",
        state: { ...activeState, status: "dismissed" }
    });
    newQuery.resolve([{ id: 7 }]);
    await flushTasks();
    assert.equal(elements.get("#status-badge").textContent, "Closed");
    assert.equal(elements.get("#analyze").textContent, "Reopen overlay");
    oldQuery.resolve([{ id: 19 }]);
    await flushTasks();
    assert.equal(elements.get("#status-badge").textContent, "Closed");
    assert.equal(elements.get("#analyze").textContent, "Reopen overlay");

    const endedCopy = vm.runInContext('errorCopy("game_ended")', context);
    assert.match(endedCopy.message, /finished-game panel/i);
    assert.doesNotMatch(endedCopy.message, /reopen/i);

    runtimeListener({ type: "session_state_changed", state: activeState });
    await flushTasks();

    actionResponse = deferred();
    elements.get("#analyze").click();
    assert.equal(elements.get("#status-badge").textContent, "Refreshing…");
    assert.equal(elements.get("#analyze").textContent, "Refreshing…");
    // A background state push must not rename an operation already underway.
    runtimeListener({
        type: "session_state_changed",
        state: { ...activeState, status: "dismissed" }
    });
    await flushTasks();
    assert.equal(elements.get("#status-badge").textContent, "Refreshing…");
    assert.equal(elements.get("#analyze").textContent, "Refreshing…");
    actionResponse.resolve({ ok: true, state: activeState });
    await flushTasks();

    activeTab = 19;
    runtimeListener({ type: "session_state_changed", state: activeState });
    await flushTasks();
    assert.equal(elements.get("#analyze").textContent, "Switch to this tab");
    assert.match(elements.get("#session-detail").textContent, /another Firefox tab/i);
    actionResponse = deferred();
    elements.get("#analyze").click();
    assert.equal(elements.get("#status-badge").textContent, "Switching…");
    assert.equal(elements.get("#analyze").textContent, "Switching…");
    actionResponse.resolve({ ok: true, state: activeState });
    await flushTasks();

    activeTab = 7;
    const dismissedState = {
        ...activeState,
        status: "dismissed"
    };
    runtimeListener({
        type: "session_state_changed",
        state: dismissedState
    });
    await flushTasks();
    assert.equal(elements.get("#status-badge").textContent, "Closed");
    assert.equal(elements.get("#analyze").textContent, "Reopen overlay");
    actionResponse = deferred();
    elements.get("#analyze").click();
    assert.equal(elements.get("#status-badge").textContent, "Reopening…");
    assert.equal(elements.get("#analyze").textContent, "Reopening…");
    actionResponse.resolve({ ok: true, state: dismissedState });
    await flushTasks();

    const nativeErrorState = {
        ...activeState,
        status: "error",
        session: {
            ...activeState.session,
            last_error: "native process exited again"
        }
    };
    runtimeListener({ type: "session_state_changed", state: nativeErrorState });
    await flushTasks();
    assert.equal(elements.get("#analyze").textContent, "Reopen overlay");
    assert.equal(elements.get("#error-title").textContent, "The local analyzer stopped");
    assert.equal(elements.get("#error-details").hidden, false);
    assert.equal(
        elements.get("#error-technical").textContent,
        "native process exited again"
    );

    const protocolState = {
        ...idleState,
        status: "error",
        protocol_error: "Native protocol error: incompatible protocol 3"
    };
    runtimeListener({ type: "session_state_changed", state: protocolState });
    await flushTasks();
    assert.match(elements.get("#error-title").textContent, /do not match/i);
    assert.match(elements.get("#error").textContent, /reinstall/i);
    assert.equal(elements.get("#error-details").hidden, false);
    assert.match(elements.get("#error-technical").textContent, /protocol 3/i);

    // Stopping also has an explicit busy label and ends in a truthful paused state.
    runtimeListener({ type: "session_state_changed", state: activeState });
    await flushTasks();
    actionResponse = deferred();
    elements.get("#stop").click();
    assert.equal(elements.get("#status-badge").textContent, "Stopping…");
    assert.equal(elements.get("#stop").textContent, "Stopping…");
    assert.equal(elements.get("#popup-root").getAttribute("aria-busy"), "true");
    actionResponse.resolve({
        ok: true,
        state: { ...idleState, status: "stopped", automatic_claims_paused: true }
    });
    await flushTasks();
    assert.deepEqual(sentActions, [
        "analyze_this_tab",
        "rescan",
        "analyze_this_tab",
        "analyze_this_tab",
        "stop_session"
    ]);
    assert.equal(elements.get("#status-badge").textContent, "Paused");
    assert.equal(elements.get("#analyze").textContent, "Start analysis");
    assert.equal(elements.get("#stop").disabled, true);
    assert.equal(elements.get("#stop").hidden, true);
    assert.equal(elements.get("#error-panel").hidden, true);

    const html = fs.readFileSync(
        path.join(__dirname, "..", "popup.html"),
        "utf8"
    );
    const css = fs.readFileSync(
        path.join(__dirname, "..", "popup.css"),
        "utf8"
    );
    assert.match(html, /id="popup-root" aria-busy="false"/);
    assert.match(html, /id="error-panel"[^>]+role="alert"/);
    assert.match(html, /id="status-badge"[\s\S]+aria-live="polite"/);
    assert.doesNotMatch(html, /Protocol 4/);
    assert.match(css, /button\s*\{[\s\S]*?min-height:\s*40px/);
    assert.match(css, /button:focus-visible/);
    assert.ok(
        contrastRatio(cssToken(css, "paper-550"), cssToken(css, "ink-950")) >= 4.5,
        "quiet footer/status text must retain 4.5:1 contrast"
    );
    assert.ok(
        contrastRatio(cssToken(css, "paper-300"), cssToken(css, "ink-850")) >= 4.5,
        "ordinary control text must retain 4.5:1 contrast"
    );

    console.log(
        "PASS popup contextual actions, actionable errors, busy states, and accessibility"
    );
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
