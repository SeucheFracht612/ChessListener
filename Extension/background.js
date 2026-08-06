"use strict";

const NATIVE_HOST = "local.chess_listener";
let nativePort = null;

function connectToNativeHost() {
    if (nativePort !== null) {
        return nativePort;
    }

    const port = browser.runtime.connectNative(NATIVE_HOST);
    nativePort = port;

    /* Replies currently only confirm that the native host accepted a frame.
     * The overlay owns all user-visible state, so there is nothing to print. */
    port.onMessage.addListener(() => {});

    port.onDisconnect.addListener((disconnectedPort) => {
        if (nativePort === disconnectedPort) {
            nativePort = null;
        }

        /* Keep normal disconnects silent. Actual connection failures still
         * appear once, which is useful without spamming every board update. */
        if (disconnectedPort.error) {
            console.error(
                "[ChessListener] native host error:",
                disconnectedPort.error.message
            );
        }
    });

    return port;
}

browser.runtime.onMessage.addListener((message, sender) => {
    if (message?.type !== "position_snapshot") {
        return undefined;
    }

    try {
        const port = connectToNativeHost();

        port.postMessage({
            ...message,
            tab_id: sender.tab?.id ?? null
        });

        return Promise.resolve({ accepted: true });
    } catch (error) {
        console.error(
            "[ChessListener] failed to connect to native host:",
            error
        );
        return Promise.reject(error);
    }
});
