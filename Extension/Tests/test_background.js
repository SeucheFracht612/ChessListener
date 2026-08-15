"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

function loadBackground(options = {}) {
    const posted = [];
    const logs = [];
    let runtimeListener = null;
    let nativeMessageListener = null;
    let nativeDisconnectListener = null;

    const port = {
        disconnected: false,
        error: null,
        postMessage(message) {
            posted.push(message);
        },
        disconnect() {
            this.disconnected = true;
        },
        onMessage: {
            addListener(listener) {
                nativeMessageListener = listener;
            }
        },
        onDisconnect: {
            addListener(listener) {
                nativeDisconnectListener = listener;
            }
        }
    };

    const context = {
        browser: {
            runtime: {
                connectNative(name) {
                    assert.equal(name, "local.chess_listener");
                    return port;
                },
                getManifest() {
                    return { version: "0.2.1" };
                },
                onMessage: {
                    addListener(listener) {
                        runtimeListener = listener;
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
        Promise,
        Error,
        setTimeout: options.setTimeout ?? setTimeout,
        clearTimeout: options.clearTimeout ?? clearTimeout
    };

    const script = fs.readFileSync(
        path.join(__dirname, "..", "background.js"),
        "utf8"
    );
    vm.runInNewContext(script, context, { filename: "background.js" });

    return {
        posted,
        logs,
        port,
        runtimeListener: () => runtimeListener,
        nativeMessageListener: () => nativeMessageListener,
        nativeDisconnectListener: () => nativeDisconnectListener
    };
}

async function main() {
    const harness = loadBackground();
    const listener = harness.runtimeListener();

    assert.equal(typeof listener, "function");

    const replyPromise = listener(
        {
            type: "position_snapshot",
            board: ".".repeat(64),
            visually_flipped: false
        },
        { tab: { id: 42 } }
    );

    // A board must remain queued until the native host proves compatibility.
    assert.equal(harness.posted.length, 1);
    assert.equal(harness.posted[0].type, "hello");
    assert.equal(harness.posted[0].protocol_version, 1);
    assert.equal(harness.posted[0].extension_version, "0.2.1");

    harness.nativeMessageListener()({
        type: "hello",
        ok: true,
        protocol_version: 1,
        host_version: "0.2.1",
        capabilities: ["position_snapshot", "last_move", "streaming_analysis"]
    });

    const reply = await replyPromise;
    assert.equal(reply.accepted, true);
    assert.equal(harness.posted.length, 2);
    assert.equal(harness.posted[1].type, "position_snapshot");
    assert.equal(harness.posted[1].tab_id, 42);
    assert.equal(harness.port.disconnected, false);
    assert.equal(harness.logs[0][0], "info");

    harness.nativeMessageListener()({
        type: "error",
        ok: false,
        reason: "incompatible_overlay_protocol"
    });
    assert.equal(harness.logs[harness.logs.length - 1][0], "error");

    harness.nativeDisconnectListener()(harness.port);

    const incompatible = loadBackground();
    const incompatiblePending = incompatible.runtimeListener()(
        { type: "position_snapshot", board: ".".repeat(64) },
        { tab: { id: 7 } }
    );
    const incompatibleRejected = assert.rejects(
        incompatiblePending,
        /incompatible protocol/
    );
    incompatible.nativeMessageListener()({
        type: "hello",
        ok: true,
        protocol_version: 1,
        host_version: "0.2.1",
        capabilities: ["position_snapshot"]
    });
    await incompatibleRejected;
    assert.equal(incompatible.port.disconnected, true);
    assert.equal(incompatible.posted.length, 1);
    assert.equal(incompatible.logs[0][0], "error");

    const disconnected = loadBackground();
    const disconnectedPending = disconnected.runtimeListener()(
        { type: "position_snapshot", board: ".".repeat(64) },
        { tab: { id: 9 } }
    );
    const disconnectedRejected = assert.rejects(
        disconnectedPending,
        /native host closed/
    );
    disconnected.port.error = { message: "native host closed" };
    disconnected.nativeDisconnectListener()(disconnected.port);
    await disconnectedRejected;
    assert.equal(disconnected.posted.length, 1);

    const timedOut = loadBackground({
        setTimeout(callback) {
            return setTimeout(callback, 0);
        }
    });
    const timeoutPending = timedOut.runtimeListener()(
        { type: "position_snapshot", board: ".".repeat(64) },
        { tab: { id: 11 } }
    );
    await assert.rejects(timeoutPending, /handshake timed out/);
    assert.equal(timedOut.port.disconnected, true);
    assert.equal(timedOut.posted.length, 1);

    console.log(
        "PASS background handshake queue, mismatch, disconnect, and timeout"
    );
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
