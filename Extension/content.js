"use strict";

const PIECE_TO_CHARACTER = Object.freeze({
    wp: "P",
    wn: "N",
    wb: "B",
    wr: "R",
    wq: "Q",
    wk: "K",
    bp: "p",
    bn: "n",
    bb: "b",
    br: "r",
    bq: "q",
    bk: "k"
});

const GAME_OVER_SELECTORS = [
    '[data-cy="game-over-modal"]',
    '[data-cy="game-over-dialog"]',
    "game-over-modal",
    ".game-over-modal-content",
    ".game-over-modal"
];

const INITIAL_BOARD =
    "rnbqkbnr" +
    "pppppppp" +
    "........" +
    "........" +
    "........" +
    "........" +
    "PPPPPPPP" +
    "RNBQKBNR";

function createPageInstanceId() {
    if (typeof globalThis.crypto?.randomUUID === "function") {
        return globalThis.crypto.randomUUID();
    }
    return `page-${Date.now().toString(36)}-${Math.random()
        .toString(36)
        .slice(2)}`;
}

const PAGE_INSTANCE_ID = createPageInstanceId();
let routeGeneration = 0;
let routeUrl = window.location.href;
let gameKey = buildGameKey();
let activeBoard = null;
let boardObserver = null;
let captureTimer = null;
let captureEpoch = 0;
let forceNextCapture = false;
let lastSubmittedIdentity = null;
let pointerIsDown = false;
let gameIsOver = false;
let gameOverMarker = null;

function isGamePage() {
    return /^\/(play|game)(\/|$)/.test(window.location.pathname);
}

function buildGameKey() {
    const url = new URL(window.location.href);
    return `${url.origin}${url.pathname}${url.search}#${routeGeneration}`;
}

function pageIsVisible() {
    return document.visibilityState === "visible";
}

function elementIsRendered(element) {
    if (
        element.hidden ||
        element.getAttribute("aria-hidden") === "true"
    ) {
        return false;
    }
    const style = window.getComputedStyle(element);
    if (
        style.display === "none" ||
        style.visibility === "hidden" ||
        style.visibility === "collapse" ||
        Number.parseFloat(style.opacity) === 0
    ) {
        return false;
    }
    const rectangle = element.getBoundingClientRect();
    return rectangle.width > 0 && rectangle.height > 0;
}

function detectGameOver() {
    return GAME_OVER_SELECTORS.some((selector) => {
        const element = document.querySelector(selector);
        return element !== null && elementIsRendered(element);
    });
}

function findActiveBoard() {
    if (!isGamePage()) {
        return null;
    }

    const boards = [...document.querySelectorAll("wc-chess-board")]
        .filter((board) => {
            const rectangle = board.getBoundingClientRect();
            return (
                rectangle.width >= 150 &&
                rectangle.height >= 150 &&
                rectangle.width > 0 &&
                rectangle.height > 0
            );
        })
        .sort((left, right) => {
            const leftRectangle = left.getBoundingClientRect();
            const rightRectangle = right.getBoundingClientRect();
            return (
                rightRectangle.width * rightRectangle.height -
                leftRectangle.width * leftRectangle.height
            );
        });

    return boards[0] ?? null;
}

function readPiece(element) {
    const pieceClass = [...element.classList].find((className) =>
        /^[wb][prnbqk]$/.test(className)
    );
    const squareClass = [...element.classList].find((className) =>
        /^square-[1-8][1-8]$/.test(className)
    );
    if (pieceClass === undefined || squareClass === undefined) {
        return null;
    }

    const squareMatch = /^square-([1-8])([1-8])$/.exec(squareClass);
    if (squareMatch === null) {
        return null;
    }

    const character = PIECE_TO_CHARACTER[pieceClass];
    if (character === undefined) {
        return null;
    }
    return {
        character,
        file: Number(squareMatch[1]),
        rank: Number(squareMatch[2])
    };
}

function readBoardPosition(board) {
    const squares = new Array(64).fill(".");
    let pieceCount = 0;

    for (const element of board.querySelectorAll(".piece")) {
        const piece = readPiece(element);
        if (piece === null) {
            continue;
        }

        const row = 8 - piece.rank;
        const column = piece.file - 1;
        const index = row * 8 + column;
        if (squares[index] !== ".") {
            return null;
        }

        squares[index] = piece.character;
        pieceCount += 1;
    }

    const position = squares.join("");
    if (!position.includes("K") || !position.includes("k")) {
        return null;
    }
    return { position, pieceCount };
}

function currentPageState(reason) {
    return {
        type: "page_state",
        page_instance_id: PAGE_INSTANCE_ID,
        route_generation: routeGeneration,
        game_key: gameKey,
        url: window.location.href,
        visible: pageIsVisible(),
        eligible:
            isGamePage() && activeBoard !== null && !gameIsOver,
        reason
    };
}

function sendWithoutWaiting(message) {
    try {
        const pending = browser.runtime.sendMessage(message);
        if (pending && typeof pending.catch === "function") {
            pending.catch((error) => {
                console.debug("[ChessListener] background unavailable:", error);
            });
        }
    } catch (error) {
        console.debug("[ChessListener] background unavailable:", error);
    }
}

function notifyPageState(reason) {
    sendWithoutWaiting(currentPageState(reason));
}

function invalidateCaptures() {
    captureEpoch += 1;
    clearTimeout(captureTimer);
    captureTimer = null;
}

function scheduleCapture(force = false) {
    if (force) {
        forceNextCapture = true;
    }
    if (
        activeBoard === null ||
        pointerIsDown ||
        gameIsOver ||
        !isGamePage()
    ) {
        return;
    }

    clearTimeout(captureTimer);
    const token = ++captureEpoch;
    captureTimer = setTimeout(() => {
        captureTimer = null;
        const forced = forceNextCapture;
        void captureStablePosition(token, forced);
    }, 120);
}

async function captureStablePosition(token, forced) {
    const board = activeBoard;
    const capturedGeneration = routeGeneration;
    const capturedGameKey = gameKey;
    if (
        token !== captureEpoch ||
        board === null ||
        !board.isConnected ||
        pointerIsDown ||
        gameIsOver
    ) {
        return;
    }

    const firstReading = readBoardPosition(board);
    const firstFlip = board.classList.contains("flipped");
    if (firstReading === null) {
        return;
    }

    await new Promise((resolve) => setTimeout(resolve, 75));
    if (
        token !== captureEpoch ||
        board !== activeBoard ||
        !board.isConnected ||
        pointerIsDown ||
        gameIsOver ||
        capturedGeneration !== routeGeneration ||
        capturedGameKey !== gameKey
    ) {
        return;
    }

    const secondReading = readBoardPosition(board);
    const secondFlip = board.classList.contains("flipped");
    if (
        secondReading === null ||
        firstReading.position !== secondReading.position ||
        firstFlip !== secondFlip
    ) {
        scheduleCapture(forced);
        return;
    }

    const identity = [
        PAGE_INSTANCE_ID,
        capturedGeneration,
        capturedGameKey,
        secondReading.position,
        secondFlip ? "flipped" : "normal"
    ].join("|");
    if (!forced && identity === lastSubmittedIdentity) {
        return;
    }

    const message = {
        type: "board_candidate",
        page_instance_id: PAGE_INSTANCE_ID,
        route_generation: capturedGeneration,
        game_key: capturedGameKey,
        url: window.location.href,
        visible: pageIsVisible(),
        board_id: board.id,
        board: secondReading.position,
        piece_count: secondReading.pieceCount,
        visually_flipped: secondFlip,
        captured_at: Date.now(),
        force: forced
    };

    try {
        const reply = await browser.runtime.sendMessage(message);
        if (
            token === captureEpoch &&
            capturedGeneration === routeGeneration &&
            capturedGameKey === gameKey &&
            reply?.accepted === true
        ) {
            lastSubmittedIdentity = identity;
            if (forced) {
                forceNextCapture = false;
            }
        }
    } catch (error) {
        if (token === captureEpoch) {
            console.error("[ChessListener] could not cache board:", error);
            scheduleCapture(forced);
        }
    }
}

function attachToBoard(board) {
    if (board === activeBoard) {
        return false;
    }
    invalidateCaptures();

    if (boardObserver !== null) {
        boardObserver.disconnect();
        boardObserver = null;
    }
    activeBoard = board;

    if (activeBoard !== null) {
        boardObserver = new MutationObserver(() => scheduleCapture());
        boardObserver.observe(activeBoard, {
            childList: true,
            subtree: true,
            attributes: true,
            attributeFilter: ["class"]
        });
        scheduleCapture();
    }
    return true;
}

function markGameOver() {
    const reading =
        activeBoard === null ? null : readBoardPosition(activeBoard);
    gameOverMarker = {
        url: routeUrl,
        board: activeBoard,
        position: reading?.position ?? null
    };
}

function isClearlyNewGame(boardChanged) {
    if (gameOverMarker === null) {
        return true;
    }

    if (routeUrl !== gameOverMarker.url) {
        return true;
    }

    if (boardChanged && activeBoard !== gameOverMarker.board) {
        return true;
    }

    const reading =
        activeBoard === null ? null : readBoardPosition(activeBoard);
    return (
        reading !== null &&
        reading.position === INITIAL_BOARD &&
        reading.position !== gameOverMarker.position
    );
}

function refreshContext(reason = "poll") {
    let changed = false;
    let routeChanged = false;
    if (window.location.href !== routeUrl) {
        invalidateCaptures();
        routeGeneration += 1;
        routeUrl = window.location.href;
        gameKey = buildGameKey();
        lastSubmittedIdentity = null;
        changed = true;
        routeChanged = true;
        reason = "navigation";
    }

    const boardChanged = attachToBoard(findActiveBoard());
    if (boardChanged) {
        changed = true;
        if (reason === "poll") {
            reason = activeBoard === null ? "board_missing" : "board_changed";
        }
    }

    const nowGameOver = detectGameOver();
    if (nowGameOver && !gameIsOver) {
        invalidateCaptures();
        gameIsOver = true;
        markGameOver();
        changed = true;
        reason = "game_end";
    } else if (gameIsOver && !nowGameOver && isClearlyNewGame(boardChanged)) {
        invalidateCaptures();
        gameIsOver = false;
        gameOverMarker = null;
        changed = true;

        if (!routeChanged) {
            routeGeneration += 1;
            gameKey = buildGameKey();
        }
        lastSubmittedIdentity = null;
        reason = "new_game";
    }

    if (changed || reason !== "poll") {
        notifyPageState(reason);
    }
    if (!gameIsOver && activeBoard !== null && changed) {
        scheduleCapture(reason === "navigation" || reason === "new_game");
    }
}

document.addEventListener(
    "pointerdown",
    (event) => {
        if (activeBoard !== null && activeBoard.contains(event.target)) {
            pointerIsDown = true;
            invalidateCaptures();
        }
    },
    true
);

function finishPointerInteraction() {
    if (!pointerIsDown) {
        return;
    }
    pointerIsDown = false;
    scheduleCapture();
}

document.addEventListener("pointerup", finishPointerInteraction, true);
document.addEventListener("pointercancel", finishPointerInteraction, true);

window.addEventListener("blur", () => {
    pointerIsDown = false;
    invalidateCaptures();
    if (pageIsVisible()) {
        scheduleCapture();
    }
});

document.addEventListener("visibilitychange", () => {
    pointerIsDown = false;
    invalidateCaptures();
    refreshContext("visibility");
    if (pageIsVisible()) {
        scheduleCapture();
    }
});

window.addEventListener("pagehide", (event) => {
    sendWithoutWaiting({
        type: "page_unloading",
        page_instance_id: PAGE_INSTANCE_ID,
        route_generation: routeGeneration,
        game_key: gameKey,
        url: window.location.href,
        bfcache: event.persisted === true
    });
});

window.addEventListener("pageshow", (event) => {
    if (event.persisted !== true) {
        return;
    }

    /* A page restored from Firefox's back/forward cache keeps this content
     * script and its page id. Re-announce it and force a fresh stable read;
     * the background deliberately did not retire this restorable instance. */
    invalidateCaptures();
    refreshContext("bfcache_restore");
    scheduleCapture(true);
});

browser.runtime.onMessage.addListener((message) => {
    if (message?.type !== "content_command" || message.command !== "rescan") {
        return undefined;
    }
    if (
        typeof message.page_instance_id === "string" &&
        message.page_instance_id !== PAGE_INSTANCE_ID
    ) {
        return Promise.resolve({ accepted: false, stale: true });
    }

    refreshContext("rescan");
    if (!isGamePage() || activeBoard === null || gameIsOver) {
        return Promise.resolve({
            accepted: false,
            reason: gameIsOver ? "game_ended" : "no_supported_board"
        });
    }
    scheduleCapture(true);
    return Promise.resolve({ accepted: true });
});

refreshContext("initial");
setInterval(() => refreshContext(), 500);
