"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function element() {
    const listeners = new Map();
    return {
        textContent: "",
        hidden: false,
        disabled: false,
        dataset: {},
        addEventListener(type, listener) {
            listeners.set(type, listener);
        },
        click() {
            listeners.get("click")?.();
        }
    };
}

async function flushTasks() {
    for (let index = 0; index < 4; index += 1) {
        await Promise.resolve();
    }
    await new Promise((resolve) => setImmediate(resolve));
}

async function main() {
    const ids = [
        "status-badge",
        "session-label",
        "session-detail",
        "error",
        "version",
        "analyze",
        "rescan",
        "stop"
    ];
    const elements = new Map(ids.map((id) => [`#${id}`, element()]));
    let runtimeListener = null;
    let actionResult = null;

    const idleState = {
        extension_version: "0.9.0",
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
            runtime: {
                sendMessage(message) {
                    if (message.type === "popup_get_state") {
                        return Promise.resolve(idleState);
                    }
                    assert.equal(message.type, "popup_action");
                    return Promise.resolve(actionResult);
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
    assert.equal(elements.get("#rescan").disabled, true);

    actionResult = {
        ok: false,
        reason: "no_supported_board",
        state: idleState
    };
    elements.get("#analyze").click();
    await flushTasks();
    assert.equal(elements.get("#error").hidden, false);
    assert.equal(elements.get("#error").textContent, "no_supported_board");
    assert.equal(elements.get("#analyze").disabled, false);

    const activeState = {
        ...idleState,
        status: "active",
        session: {
            tab_id: 7,
            snapshot_seq: 4,
            url: "https://www.chess.com/play/computer",
            last_error: null
        }
    };
    runtimeListener({ type: "session_state_changed", state: activeState });
    assert.equal(elements.get("#rescan").disabled, false);
    assert.equal(elements.get("#stop").disabled, false);

    runtimeListener({
        type: "session_state_changed",
        state: { ...activeState, status: "dismissed" }
    });
    assert.equal(elements.get("#rescan").disabled, true);
    assert.equal(elements.get("#stop").disabled, false);

    console.log("PASS popup state, action errors, and recovery controls");
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
