"use strict";

const popupRoot = document.querySelector("#popup-root");
const statusBadge = document.querySelector("#status-badge");
const sessionLabel = document.querySelector("#session-label");
const sessionDetail = document.querySelector("#session-detail");
const errorPanel = document.querySelector("#error-panel");
const errorTitle = document.querySelector("#error-title");
const errorBox = document.querySelector("#error");
const errorDetails = document.querySelector("#error-details");
const errorTechnical = document.querySelector("#error-technical");
const versionLabel = document.querySelector("#version");
const analyzeButton = document.querySelector("#analyze");
const stopButton = document.querySelector("#stop");

let busyAction = null;
let busyText = "";
let currentState = null;
let activeTabId = null;
let stateGeneration = 0;

const REASON_COPY = {
    no_supported_board: {
        title: "No supported board found",
        message:
            "Open a live game, bot game, or analysis board on Chess.com, then try again."
    },
    game_ended: {
        title: "This game has finished",
        message:
            "Use the finished-game panel to save or review it, or start another game."
    },
    content_unavailable: {
        title: "The Chess.com tab is not ready",
        message:
            "Reload the tab, wait for the board to appear, then try again."
    },
    no_active_session: {
        title: "No analysis session is running",
        message: "Open a Chess.com board and choose Start analysis."
    },
    no_active_tab: {
        title: "Firefox has no active tab",
        message: "Select a Chess.com tab, then open ChessListener again."
    },
    native_unavailable: {
        title: "The local analyzer is unavailable",
        message:
            "Choose Reopen overlay. If this keeps happening, rerun the ChessListener installer."
    },
    native_protocol_error: {
        title: "The extension and local app do not match",
        message: "Update or reinstall both ChessListener parts together.",
        technical: true
    },
    unknown_action: {
        title: "That action is not available",
        message: "Close this popup, reopen it, and try again.",
        technical: true
    }
};

function statusText(status) {
    return {
        idle: "Idle",
        stopped: "Paused",
        connecting: "Connecting",
        active: "Active",
        dismissed: "Closed",
        error: "Needs attention"
    }[status] ?? "Needs attention";
}

function sessionName(urlText) {
    try {
        const url = new URL(urlText);
        const path = url.pathname.toLowerCase();
        if (path.includes("/play/computer")) {
            return "Computer game · Chess.com";
        }
        if (path.includes("/analysis")) {
            return "Analysis board · Chess.com";
        }
        if (path.includes("/game/") || path.includes("/play/online")) {
            return "Live game · Chess.com";
        }
        return url.hostname === "www.chess.com"
            ? "Chess.com board"
            : url.hostname || "Chess.com board";
    } catch (_error) {
        return "Chess.com board";
    }
}

function ownsActiveTab(state = currentState) {
    return (
        Number.isInteger(activeTabId) &&
        Number.isInteger(state?.session?.tab_id) &&
        activeTabId === state.session.tab_id
    );
}

function primaryContext(state = currentState) {
    const status = state?.status ?? "error";
    const session = state?.session ?? null;
    if (session === null) {
        return {
            action: "analyze_this_tab",
            label: "Start analysis",
            busy: "Starting…"
        };
    }
    if (Number.isInteger(activeTabId) && !ownsActiveTab(state)) {
        return {
            action: "analyze_this_tab",
            label: "Switch to this tab",
            busy: "Switching…"
        };
    }
    if (
        status === "dismissed" ||
        status === "error" ||
        session.dismissed === true ||
        session.native_unavailable === true
    ) {
        return {
            action: "analyze_this_tab",
            label: "Reopen overlay",
            busy: "Reopening…"
        };
    }
    return {
        action: "rescan",
        label: "Refresh this board",
        busy: "Refreshing…"
    };
}

function busyLabel(action) {
    return busyText || (
        action === "stop_session" ? "Stopping…" : primaryContext().busy
    );
}

function normalizedReason(reason) {
    return String(reason ?? "").trim();
}

function errorCopy(reason) {
    const raw = normalizedReason(reason);
    if (!raw) {
        return null;
    }
    const direct = REASON_COPY[raw];
    if (direct !== undefined) {
        return {
            ...direct,
            detail: direct.technical === true ? raw : ""
        };
    }
    if (/protocol|hello_required|session_v\d+_required/i.test(raw)) {
        return {
            ...REASON_COPY.native_protocol_error,
            detail: raw
        };
    }
    if (
        /native messaging host|native application|host.*not found|specified native/i.test(raw)
    ) {
        return {
            title: "ChessListener is not installed for Firefox",
            message:
                "Run the ChessListener installer, fully restart Firefox, and try again.",
            detail: raw
        };
    }
    if (/process exited|disconnect|closed|broken pipe/i.test(raw)) {
        return {
            title: "The local analyzer stopped",
            message:
                "Choose Reopen overlay. If it stops again, check the local ChessListener log.",
            detail: raw
        };
    }
    return {
        title: "Analysis could not continue",
        message: "Try again. Technical details are available below.",
        detail: raw
    };
}

function showError(reason) {
    const copy = errorCopy(reason);
    if (copy === null) {
        errorPanel.hidden = true;
        errorTitle.textContent = "";
        errorBox.textContent = "";
        errorDetails.hidden = true;
        errorDetails.open = false;
        errorTechnical.textContent = "";
        return;
    }
    errorTitle.textContent = copy.title;
    errorBox.textContent = copy.message;
    errorTechnical.textContent = copy.detail;
    errorDetails.hidden = copy.detail.length === 0;
    // Populate the live alert before exposing it so assistive technology
    // announces one complete, actionable message instead of an empty panel.
    errorPanel.hidden = false;
}

function stateError(state) {
    return state?.protocol_error ?? state?.session?.last_error ?? "";
}

function render(state) {
    currentState = state;
    const status = state?.status ?? "error";
    const session = state?.session ?? null;
    const isBusy = busyAction !== null;

    popupRoot.setAttribute("aria-busy", String(isBusy));
    statusBadge.dataset.status = isBusy ? "busy" : status;
    statusBadge.textContent = isBusy
        ? busyLabel(busyAction)
        : statusText(status);

    if (session === null) {
        sessionLabel.textContent =
            status === "stopped" ? "Automatic start paused" : "No active board";
        sessionDetail.textContent =
            status === "stopped"
                ? "Start analysis when you are ready to use ChessListener again."
                : "Open a Chess.com board to begin.";
    } else {
        sessionLabel.textContent = sessionName(session.url);
        if (Number.isInteger(activeTabId) && !ownsActiveTab(state)) {
            sessionDetail.textContent =
                "Another Firefox tab owns the current analysis session.";
        } else if (status === "dismissed") {
            sessionDetail.textContent =
                "Board session retained · overlay closed";
        } else if (status === "connecting") {
            sessionDetail.textContent = "Starting the local analyzer…";
        } else if (status === "error") {
            sessionDetail.textContent =
                "Board session retained · local analyzer needs attention";
        } else {
            sessionDetail.textContent =
                "Position synced · overlay running locally";
        }
    }

    versionLabel.textContent = `v${state?.extension_version ?? "0.9.5"}`;
    const primary = primaryContext(state);
    analyzeButton.textContent = isBusy && busyAction !== "stop_session"
        ? busyLabel(busyAction)
        : primary.label;
    analyzeButton.disabled = isBusy;
    stopButton.textContent = isBusy && busyAction === "stop_session"
        ? "Stopping…"
        : "Stop session";
    stopButton.hidden = session === null;
    stopButton.disabled = isBusy || session === null;
    showError(stateError(state));
}

async function queryActiveTab() {
    try {
        const tabs = await browser.tabs.query({ active: true, currentWindow: true });
        return tabs.find((tab) => Number.isInteger(tab.id))?.id ?? null;
    } catch (_error) {
        return null;
    }
}

async function refresh() {
    const generation = ++stateGeneration;
    try {
        const [state, tabId] = await Promise.all([
            browser.runtime.sendMessage({ type: "popup_get_state" }),
            queryActiveTab()
        ]);
        if (generation !== stateGeneration) {
            return;
        }
        activeTabId = tabId;
        render(state);
    } catch (error) {
        if (generation !== stateGeneration) {
            return;
        }
        showError(error?.message ?? String(error));
    }
}

async function runAction(action) {
    const generation = ++stateGeneration;
    busyAction = action;
    busyText = action === "stop_session" ? "Stopping…" : primaryContext().busy;
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
                ? normalizedReason(result.reason ?? "Action failed")
                : "";
        const [state, tabId] = await Promise.all([
            result?.state !== undefined
                ? Promise.resolve(result.state)
                : browser.runtime.sendMessage({ type: "popup_get_state" }),
            queryActiveTab()
        ]);
        if (generation === stateGeneration) {
            activeTabId = tabId;
            currentState = state;
        }
    } catch (error) {
        actionError = error?.message ?? String(error);
    } finally {
        busyAction = null;
        busyText = "";
        if (currentState !== null) {
            render(currentState);
        }
        if (actionError && generation === stateGeneration) {
            showError(actionError);
        }
    }
}

analyzeButton.addEventListener("click", () => {
    void runAction(primaryContext().action);
});
stopButton.addEventListener("click", () => {
    void runAction("stop_session");
});

browser.runtime.onMessage.addListener((message) => {
    if (message?.type === "session_state_changed" && message.state !== undefined) {
        const generation = ++stateGeneration;
        currentState = message.state;
        void queryActiveTab().then((tabId) => {
            if (generation !== stateGeneration) {
                return;
            }
            activeTabId = tabId;
            render(message.state);
        });
    }
});

void refresh();
