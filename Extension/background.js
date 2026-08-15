"use strict";

const NATIVE_HOST = "local.chess_listener";
const PROTOCOL_VERSION = 1;
const REQUIRED_CAPABILITIES = ["position_snapshot", "last_move"];
const HANDSHAKE_TIMEOUT_MS = 3000;
let nativePort = null;
let nativeReadyPromise = null;

function resetNativeConnection(port) {
    if (nativePort === port) {
        nativePort = null;
        nativeReadyPromise = null;
    }
}

function connectToNativeHost() {
    if (nativePort !== null && nativeReadyPromise !== null) {
        return nativeReadyPromise;
    }

    const port = browser.runtime.connectNative(NATIVE_HOST);
    nativePort = port;
    let resolveReady;
    let rejectReady;
    let settled = false;
    let timeoutId = null;

    const ready = new Promise((resolve, reject) => {
        resolveReady = resolve;
        rejectReady = reject;
    });
    nativeReadyPromise = ready;

    function disconnectQuietly() {
        try {
            port.disconnect();
        } catch (_error) {
            // The native port may already have closed itself.
        }
    }

    function failHandshake(error, disconnect = false) {
        if (!settled) {
            settled = true;
            clearTimeout(timeoutId);
            resetNativeConnection(port);
            rejectReady(error);
        }

        if (disconnect) {
            disconnectQuietly();
        }
    }

    function completeHandshake(message) {
        if (settled) {
            return;
        }

        settled = true;
        clearTimeout(timeoutId);
        console.info(
            `[ChessListener] native host ${message.host_version} ready`,
            message.capabilities
        );
        resolveReady(port);
    }

    port.onMessage.addListener((message) => {
        if (port !== nativePort) {
            return;
        }

        if (!settled) {
            const capabilities = Array.isArray(message?.capabilities)
                ? message.capabilities
                : [];
            const compatible =
                message?.type === "hello" &&
                message.ok === true &&
                message.protocol_version === PROTOCOL_VERSION &&
                REQUIRED_CAPABILITIES.every(
                    (name) => capabilities.includes(name)
                );

            if (!compatible) {
                console.error(
                    "[ChessListener] incompatible native host:",
                    message
                );
                failHandshake(
                    new Error("The native host uses an incompatible protocol"),
                    true
                );
                return;
            }

            completeHandshake({ ...message, capabilities });
            return;
        }

        if (message?.type === "error") {
            console.error("[ChessListener] native host error:", message);
        } else if (message?.ok === false || message?.accepted === false) {
            console.warn(
                "[ChessListener] native host rejected a message:",
                message
            );
        }
    });

    port.onDisconnect.addListener((disconnectedPort) => {
        const detail = disconnectedPort?.error?.message;

        if (!settled) {
            failHandshake(
                new Error(detail || "The native host disconnected during startup")
            );
        } else {
            resetNativeConnection(port);
        }

        /* Keep normal disconnects silent. Actual connection failures still
         * appear once, which is useful without spamming every board update. */
        if (detail) {
            console.error(
                "[ChessListener] native host error:",
                detail
            );
        }
    });

    timeoutId = setTimeout(() => {
        failHandshake(
            new Error("The native host handshake timed out"),
            true
        );
    }, HANDSHAKE_TIMEOUT_MS);

    try {
        port.postMessage({
            type: "hello",
            protocol_version: PROTOCOL_VERSION,
            extension_version: browser.runtime.getManifest().version
        });
    } catch (error) {
        failHandshake(error, true);
    }

    return ready;
}

async function forwardPosition(message, sender) {
    try {
        const port = await connectToNativeHost();

        port.postMessage({
            ...message,
            tab_id: sender.tab?.id ?? null
        });

        return { accepted: true };
    } catch (error) {
        console.error(
            "[ChessListener] failed to connect to native host:",
            error
        );
        throw error;
    }
}

browser.runtime.onMessage.addListener((message, sender) => {
    if (message?.type !== "position_snapshot") {
        return undefined;
    }

    return forwardPosition(message, sender);
});
