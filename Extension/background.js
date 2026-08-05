"use strict";

const NATIVE_HOST = "local.chess_listener";

let nativePort = null;

console.log("[ChessListener] background script loaded.");

function connectToNativeHost() {
    if (nativePort !== null) {
        return nativePort;
    }

    console.log(`[ChessListener] connecting to ${NATIVE_HOST}`);

    const port = browser.runtime.connectNative(NATIVE_HOST);
    nativePort = port;

    port.onMessage.addListener((message) => {
        console.log("[ChessListener] native host replied:", message);
    });

    port.onDisconnect.addListener((disconnectedPort) => {
        if (disconnectedPort.error) {
            console.error(
                "[ChessListener] native host error:",
                disconnectedPort.error.message
            );
        } else {
            console.log("[ChessListener] native host disconnected normally.");
        }

        if (nativePort === disconnectedPort) {
            nativePort = null;
        }
    });

    return port;
}

browser.runtime.onMessage.addListener((message, sender) => {
    console.log("[ChessListener] received content message:", message);

    const acceptedMessageTypes = new Set([
        "test_snapshot",
        "position_snapshot"
    ]);

    if (!acceptedMessageTypes.has(message?.type)) {
        return undefined;
    }

    try {
        const port = connectToNativeHost();

        port.postMessage({
            ...message,
            tab_id: sender.tab?.id ?? null
        });

        return Promise.resolve({
            accepted: true
        });
    } catch (error) {
        console.error(
            "[ChessListener] failed to connect to native host:",
            error
        );

        return Promise.reject(error);
    }
});
