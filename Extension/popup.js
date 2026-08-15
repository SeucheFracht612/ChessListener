"use strict";

const statusBadge = document.querySelector("#status-badge");
const sessionLabel = document.querySelector("#session-label");
const sessionDetail = document.querySelector("#session-detail");
const errorBox = document.querySelector("#error");
const versionLabel = document.querySelector("#version");
const analyzeButton = document.querySelector("#analyze");
const rescanButton = document.querySelector("#rescan");
const stopButton = document.querySelector("#stop");

let busy = false;
let currentState = null;

function statusText(status) {
    return {
        idle: "Idle",
        stopped: "Stopped",
        connecting: "Connecting",
        active: "Active",
        dismissed: "Dismissed",
        error: "Error"
    }[status] ?? "Unknown";
}

function compactLocation(urlText) {
    try {
        const url = new URL(urlText);
        return `${url.hostname}${url.pathname}`;
    } catch (_error) {
        return urlText || "Chess.com board";
    }
}

function showError(message) {
    errorBox.textContent = message;
    errorBox.hidden = message.length === 0;
}

function render(state) {
    currentState = state;
    const status = state?.status ?? "error";
    statusBadge.dataset.status = status;
    statusBadge.textContent = statusText(status);

    if (state?.session === null || state?.session === undefined) {
        sessionLabel.textContent =
            status === "stopped" ? "Automatic start paused" : "No active board";
        sessionDetail.textContent =
            status === "stopped"
                ? "Choose Analyze this tab to start again."
                : "Open a Chess.com game or press Analyze this tab.";
    } else {
        sessionLabel.textContent = compactLocation(state.session.url);
        if (status === "dismissed") {
            sessionDetail.textContent =
                "Overlay closed · Analyze this tab to reopen";
        } else {
            sessionDetail.textContent =
                `Tab ${state.session.tab_id} · Snapshot ${state.session.snapshot_seq}`;
        }
    }

    versionLabel.textContent =
        `v${state?.extension_version ?? "0.3.0"} · Protocol ` +
        `${state?.native_protocol_version ?? 2}`;
    analyzeButton.disabled = busy;
    rescanButton.disabled =
        busy || !["active", "connecting"].includes(status);
    stopButton.disabled = busy || state?.session == null;
    showError(state?.protocol_error ?? state?.session?.last_error ?? "");
}

async function refresh() {
    try {
        const state = await browser.runtime.sendMessage({
            type: "popup_get_state"
        });
        render(state);
    } catch (error) {
        showError(error?.message ?? String(error));
    }
}

async function runAction(action) {
    busy = true;
    let actionError = "";
    showError("");
    if (currentState !== null) {
        render(currentState);
    }
    try {
        const result = await browser.runtime.sendMessage({
            type: "popup_action",
            action
        });
        actionError =
            result?.ok === false
                ? String(result.reason ?? "Action failed")
                : "";
        if (result?.state !== undefined) {
            render(result.state);
        } else {
            await refresh();
        }
    } catch (error) {
        actionError = error?.message ?? String(error);
    } finally {
        busy = false;
        if (currentState !== null) {
            render(currentState);
        }
        if (actionError) {
            showError(actionError);
        }
    }
}

analyzeButton.addEventListener("click", () => {
    void runAction("analyze_this_tab");
});
rescanButton.addEventListener("click", () => {
    void runAction("rescan");
});
stopButton.addEventListener("click", () => {
    void runAction("stop_session");
});

browser.runtime.onMessage.addListener((message) => {
    if (message?.type === "session_state_changed" && message.state !== undefined) {
        render(message.state);
    }
});

void refresh();
