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

/* Keep the observed-board path short. History is deliberately collected on a
 * different clock, and expensive native recovery is requested only after the
 * same stable board has remained current for a while. */
const CAPTURE_DEBOUNCE_MS = 32;
const STABLE_CONFIRM_MS = 24;
const HISTORY_DEBOUNCE_MS = 280;
const RECOVERY_DELAY_MS = 700;
const MAX_HISTORY_PLIES = 1024;
const MAX_HISTORY_BYTES = 32 * 1024;

const HISTORY_CONTAINER_SELECTORS = [
    "wc-simple-move-list",
    "wc-move-list",
    "vertical-move-list",
    '[data-cy="move-list"]',
    '[data-testid="move-list"]'
];

const HISTORY_TOKEN_SELECTORS = [
    "[data-ply]",
    "[data-san]",
    "[data-move]",
    ".move-text-component",
    '[data-cy="move"]',
    '[data-testid="move"]'
];

const HISTORY_ROW_SELECTORS = [
    "[data-whole-move-number]",
    ".move-list-row",
    "[data-move-number]",
    '[data-cy="move-list-row"]',
    '[data-testid="move-list-row"]'
];

const HISTORY_MOVE_NUMBER_SELECTORS = [
    ".move-list-row-number",
    ".move-number",
    '[data-cy="move-number"]',
    '[data-testid="move-number"]'
];

const FIGURINE_TO_SAN = Object.freeze({
    "\u2654": "K",
    "\u2655": "Q",
    "\u2656": "R",
    "\u2657": "B",
    "\u2658": "N",
    "\u2659": "",
    "\u265a": "K",
    "\u265b": "Q",
    "\u265c": "R",
    "\u265d": "B",
    "\u265e": "N",
    "\u265f": ""
});

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
let recoveryTimer = null;
let captureEpoch = 0;
let forceNextCapture = false;
let lastSubmittedIdentity = null;
let lastStableSnapshot = null;
let activeHistoryContainer = null;
let historyObserver = null;
let historyTimer = null;
let historyEpoch = 0;
let forceNextHistoryCapture = false;
let lastSubmittedHistoryFingerprint = null;
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
        style?.display === "none" ||
        style?.visibility === "hidden" ||
        style?.visibility === "collapse" ||
        Number.parseFloat(style?.opacity) === 0
    ) {
        return false;
    }
    const rectangle = element.getBoundingClientRect?.();
    if (rectangle === undefined) {
        return true;
    }
    return rectangle.width > 0 && rectangle.height > 0;
}

function detectGameOver() {
    return GAME_OVER_SELECTORS.some((selector) => {
        const element = document.querySelector(selector);
        return element !== null && elementIsRendered(element);
    });
}

function canonicalGameResult(value) {
    if (typeof value !== "string") {
        return null;
    }
    const normalized = value.replace(/\u00bd/g, "1/2").trim();
    const match = /(?:^|\s)(1-0|0-1|1\/2-1\/2)(?:\s|$)/.exec(normalized);
    return match === null ? null : match[1];
}

function readVisibleGameResult() {
    for (const selector of GAME_OVER_SELECTORS) {
        const element = document.querySelector(selector);
        if (element === null || !elementIsRendered(element)) {
            continue;
        }
        for (const attribute of ["data-result", "data-game-result"]) {
            const result = canonicalGameResult(element.getAttribute?.(attribute));
            if (result !== null) {
                return result;
            }
        }
        const result = canonicalGameResult(element.textContent ?? "");
        if (result !== null) {
            return result;
        }
    }
    return "*";
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

function findHistoryContainer() {
    if (!isGamePage()) {
        return null;
    }
    for (const selector of HISTORY_CONTAINER_SELECTORS) {
        const containers = [...document.querySelectorAll(selector)];
        const visible = containers.find((container) =>
            elementIsRendered(container)
        );
        if (visible !== undefined) {
            return visible;
        }
    }
    return null;
}

function nodesIncludingSelf(container, selector) {
    const nodes = [...container.querySelectorAll(selector)];
    if (
        typeof container.matches === "function" &&
        container.matches(selector)
    ) {
        nodes.unshift(container);
    }
    return nodes;
}

function readNodeAttribute(node, attribute) {
    const direct = node.getAttribute?.(attribute);
    if (typeof direct === "string" && direct.trim().length > 0) {
        return direct.trim();
    }
    const descendants = [...(node.querySelectorAll?.(`[${attribute}]`) ?? [])];
    if (descendants.length !== 1) {
        return null;
    }
    const nested = descendants[0].getAttribute?.(attribute);
    return typeof nested === "string" && nested.trim().length > 0
        ? nested.trim()
        : null;
}

function normalizeUciToken(value) {
    const token = String(value ?? "").trim().toLowerCase();
    return /^[a-h][1-8][a-h][1-8][qrbn]?$/.test(token) ? token : null;
}

function normalizeSanCharacters(value) {
    return String(value ?? "")
        .replace(/[\u2654-\u265f]/g, (figurine) => FIGURINE_TO_SAN[figurine])
        .replace(/\u00a0/g, " ")
        .replace(/\u2026/g, "...")
        .replace(/[\u2010-\u2015\u2212]/g, "-")
        .replace(/0-0-0/gi, "O-O-O")
        .replace(/0-0/gi, "O-O");
}

function normalizeSanToken(value) {
    let token = normalizeSanCharacters(value).trim();
    token = token.replace(/^\d+\.(?:\.\.)?/, "");
    token = token.replace(/^\.\.\./, "");
    token = token.replace(/\$\d+$/g, "");
    token = token.replace(/[!?]+$/g, "");
    token = token.replace(/(?:e\.?p\.?)$/i, "");
    token = token.trim();

    if (
        token.length === 0 ||
        ["1-0", "0-1", "1/2-1/2", "*"].includes(token)
    ) {
        return null;
    }

    const castle = /^O-O(?:-O)?[+#]?$/.test(token);
    const pieceMove = /^[KQRBN](?:[a-h]|[1-8]|[a-h][1-8])?x?[a-h][1-8](?:=?[QRBN])?[+#]?$/.test(
        token
    );
    const pawnMove = /^[a-h](?:x[a-h])?[1-8](?:=?[QRBN])?[+#]?$/.test(
        token
    );
    return castle || pieceMove || pawnMove ? token : null;
}

function sanTokensFromText(value) {
    const chunks = normalizeSanCharacters(value)
        .replace(/(\d+)\.(\.\.)?/g, " $&")
        .trim()
        .split(/\s+/)
        .filter((chunk) => chunk.length > 0);
    const tokens = [];
    for (let chunk of chunks) {
        if (/^\d+\.(?:\.\.)?$/.test(chunk) || /^\.\.\.$/.test(chunk)) {
            continue;
        }
        chunk = chunk.replace(/^\d+\.(?:\.\.)?/, "");
        if (chunk.length === 0 || /^\$\d+$/.test(chunk)) {
            continue;
        }
        if (["1-0", "0-1", "1/2-1/2", "*"].includes(chunk)) {
            continue;
        }
        const token = normalizeSanToken(chunk);
        if (token === null) {
            return null;
        }
        tokens.push(token);
    }
    return tokens;
}

function normalizedAttributeToken(value, attribute) {
    if (attribute === "data-uci") {
        const token = normalizeUciToken(value);
        return token === null ? null : { notation: "uci", token };
    }

    if (attribute === "data-move") {
        const uci = normalizeUciToken(value);
        if (uci !== null) {
            return { notation: "uci", token: uci };
        }
    }
    const san = normalizeSanToken(value);
    return san === null ? null : { notation: "san", token: san };
}

function pieceLetterFromDescriptor(value) {
    const rawDescriptor = String(value ?? "").trim();
    const figurine = FIGURINE_TO_SAN[rawDescriptor];
    if (typeof figurine === "string" && figurine.length === 1) {
        return figurine;
    }
    const descriptor = rawDescriptor.toLowerCase();
    if (descriptor.length === 0) {
        return null;
    }
    if (/(?:^|[-_\s])knight(?:$|[-_\s])/.test(descriptor)) {
        return "N";
    }
    if (/(?:^|[-_\s])bishop(?:$|[-_\s])/.test(descriptor)) {
        return "B";
    }
    if (/(?:^|[-_\s])rook(?:$|[-_\s])/.test(descriptor)) {
        return "R";
    }
    if (/(?:^|[-_\s])queen(?:$|[-_\s])/.test(descriptor)) {
        return "Q";
    }
    if (/(?:^|[-_\s])king(?:$|[-_\s])/.test(descriptor)) {
        return "K";
    }
    const compact = descriptor.replace(/[^a-z]/g, "");
    const piece = /^(?:white|black|w|b)?([nbrqk])(?:white|black|w|b)?$/.exec(
        compact
    );
    return piece === null ? null : piece[1].toUpperCase();
}

function pieceLetterFromMoveNode(node) {
    const elements = [node, ...(node.querySelectorAll?.("*") ?? [])];
    for (const element of elements) {
        for (const attribute of [
            "data-figurine",
            "data-piece",
            "data-piece-type",
            "aria-label",
            "title"
        ]) {
            const letter = pieceLetterFromDescriptor(
                element.getAttribute?.(attribute)
            );
            if (letter !== null) {
                return letter;
            }
        }
        for (const className of element.classList ?? []) {
            const letter = pieceLetterFromDescriptor(className);
            if (letter !== null) {
                return letter;
            }
        }
    }
    return null;
}

function normalizedMoveNodeToken(node) {
    for (const attribute of ["data-uci", "data-san", "data-move"]) {
        const value = readNodeAttribute(node, attribute);
        if (value !== null) {
            const normalized = normalizedAttributeToken(value, attribute);
            if (normalized !== null) {
                return normalized;
            }
        }
    }

    const compactText = normalizeSanCharacters(node.textContent ?? "")
        .replace(/[\u200b-\u200f\u2060\ufe00-\ufe0f\ufeff\ue000-\uf8ff]/g, "")
        .replace(/\s+/g, "")
        .trim();
    if (compactText.length === 0) {
        return null;
    }

    const figurine = pieceLetterFromMoveNode(node);
    if (
        figurine !== null &&
        !compactText.startsWith(figurine) &&
        !/^O-O/.test(compactText)
    ) {
        const withFigurine = normalizeSanToken(`${figurine}${compactText}`);
        if (withFigurine !== null) {
            return { notation: "san", token: withFigurine };
        }
    }

    const san = normalizeSanToken(compactText);
    return san === null ? null : { notation: "san", token: san };
}

function readHistoryRowNumber(row) {
    for (const attribute of [
        "data-whole-move-number",
        "data-move-number"
    ]) {
        const direct = row.getAttribute?.(attribute);
        if (/^\d+$/.test(String(direct ?? ""))) {
            return Number(direct);
        }
    }

    const numberNodes = [
        ...(row.querySelectorAll?.(
            HISTORY_MOVE_NUMBER_SELECTORS.join(",")
        ) ?? [])
    ];
    for (const node of numberNodes) {
        const match = /^\s*(\d+)\s*[.:]/.exec(node.textContent ?? "");
        if (match !== null) {
            return Number(match[1]);
        }
    }

    const rowMatch = /^\s*(\d+)\s*[.:]/.exec(row.textContent ?? "");
    return rowMatch === null ? null : Number(rowMatch[1]);
}

function collectPairedRowHistory(container) {
    const rows = nodesIncludingSelf(
        container,
        HISTORY_ROW_SELECTORS.join(",")
    );
    if (rows.length === 0 || rows.length > MAX_HISTORY_PLIES) {
        return null;
    }

    const tokens = [];
    let notation = null;
    for (let rowIndex = 0; rowIndex < rows.length; rowIndex += 1) {
        const row = rows[rowIndex];
        if (readHistoryRowNumber(row) !== rowIndex + 1) {
            return null;
        }
        const moveNodes = [
            ...(row.querySelectorAll?.(HISTORY_TOKEN_SELECTORS.join(",")) ?? [])
        ].filter((node) => {
            return (
                String(node.textContent ?? "").trim().length > 0 ||
                ["data-uci", "data-san", "data-move"].some(
                    (attribute) => readNodeAttribute(node, attribute) !== null
                )
            );
        });
        if (
            moveNodes.length < 1 ||
            moveNodes.length > 2 ||
            (rowIndex + 1 < rows.length && moveNodes.length !== 2)
        ) {
            return null;
        }

        for (const moveNode of moveNodes) {
            const normalized = normalizedMoveNodeToken(moveNode);
            if (
                normalized === null ||
                (notation !== null && notation !== normalized.notation)
            ) {
                return null;
            }
            notation = normalized.notation;
            tokens.push(normalized.token);
            if (tokens.length > MAX_HISTORY_PLIES) {
                return null;
            }
        }
    }

    return { notation, tokens, complete: true };
}

function collectContiguousPlyHistory(container, attribute) {
    const plyNodes = nodesIncludingSelf(container, "[data-ply]");
    if (plyNodes.length === 0 || plyNodes.length > MAX_HISTORY_PLIES) {
        return null;
    }

    const byPly = new Map();
    let notation = null;
    for (const node of plyNodes) {
        const plyValue = node.getAttribute?.("data-ply");
        if (!/^\d+$/.test(String(plyValue ?? ""))) {
            return null;
        }
        const ply = Number(plyValue);
        if (!Number.isInteger(ply) || ply < 0 || ply > MAX_HISTORY_PLIES) {
            return null;
        }

        let normalized;
        if (attribute === null) {
            normalized = normalizedMoveNodeToken(node);
        } else {
            normalized = normalizedAttributeToken(
                readNodeAttribute(node, attribute),
                attribute
            );
        }
        if (normalized === null) {
            return null;
        }
        if (notation !== null && notation !== normalized.notation) {
            return null;
        }
        notation = normalized.notation;

        const previous = byPly.get(ply);
        if (previous !== undefined && previous !== normalized.token) {
            return null;
        }
        byPly.set(ply, normalized.token);
    }

    const plies = [...byPly.keys()].sort((left, right) => left - right);
    if (plies.length === 0 || ![0, 1].includes(plies[0])) {
        return null;
    }
    const firstPly = plies[0];
    for (let index = 0; index < plies.length; index += 1) {
        if (plies[index] !== firstPly + index) {
            return null;
        }
    }
    return {
        notation,
        tokens: plies.map((ply) => byPly.get(ply)),
        complete: true
    };
}

function collectLooseAttributeHistory(container, attribute) {
    const nodes = nodesIncludingSelf(container, `[${attribute}]`);
    if (nodes.length === 0 || nodes.length > MAX_HISTORY_PLIES) {
        return null;
    }
    const tokens = [];
    let notation = null;
    for (const node of nodes) {
        const normalized = normalizedAttributeToken(
            node.getAttribute?.(attribute),
            attribute
        );
        if (
            normalized === null ||
            (notation !== null && notation !== normalized.notation)
        ) {
            return null;
        }
        notation = normalized.notation;
        tokens.push(normalized.token);
    }
    return { notation, tokens, complete: false };
}

function collectLooseSanHistory(container) {
    const nodes = nodesIncludingSelf(
        container,
        HISTORY_TOKEN_SELECTORS.join(",")
    );
    if (nodes.length === 0) {
        return null;
    }
    const tokens = [];
    for (const node of nodes) {
        const parsed = sanTokensFromText(node.textContent ?? "");
        if (parsed === null || parsed.length === 0) {
            return null;
        }
        tokens.push(...parsed);
        if (tokens.length > MAX_HISTORY_PLIES) {
            return null;
        }
    }
    return { notation: "san", tokens, complete: false };
}

function collectNumberedContainerTextHistory(container) {
    const text = normalizeSanCharacters(container.textContent ?? "").trim();
    if (
        !/^1\.(?!\.)/.test(text) ||
        /[()[\]{}]/.test(text) ||
        text.length > MAX_HISTORY_BYTES
    ) {
        return null;
    }

    const chunks = text
        .replace(/(\d+)\.(\.\.)?/g, " $& ")
        .trim()
        .split(/\s+/)
        .filter((chunk) => chunk.length > 0);
    const tokens = [];
    let fullMove = 1;
    let side = "white";
    let ended = false;
    let result = null;

    for (const chunk of chunks) {
        const moveNumber = /^(\d+)\.(\.\.)?$/.exec(chunk);
        if (moveNumber !== null) {
            const number = Number(moveNumber[1]);
            const markerSide = moveNumber[2] === ".." ? "black" : "white";
            if (ended || number !== fullMove || markerSide !== side) {
                return null;
            }
            continue;
        }

        if (["1-0", "0-1", "1/2-1/2", "*"].includes(chunk)) {
            ended = true;
            result = chunk;
            continue;
        }
        if (ended || /^\$\d+$/.test(chunk)) {
            return null;
        }

        const token = normalizeSanToken(chunk);
        if (token === null) {
            return null;
        }
        tokens.push(token);
        if (tokens.length > MAX_HISTORY_PLIES) {
            return null;
        }
        if (side === "white") {
            side = "black";
        } else {
            side = "white";
            fullMove += 1;
        }
    }

    return tokens.length === 0
        ? null
        : { notation: "san", tokens, complete: true, result };
}

function readInitialFen(container) {
    for (const attribute of ["data-initial-fen", "data-start-fen"]) {
        const value = container.getAttribute?.(attribute);
        if (typeof value !== "string") {
            continue;
        }
        const fen = value.trim().replace(/\s+/g, " ");
        if (
            fen.length <= 128 &&
            /^[prnbqkPRNBQK1-8/]+ [wb] (?:-|(?=[KQkq])K?Q?k?q?) (?:-|[a-h][36]) \d+ \d+$/.test(
                fen
            )
        ) {
            return fen;
        }
    }
    return null;
}

function readMoveHistory(container) {
    const attempts = [
        () => collectContiguousPlyHistory(container, "data-uci"),
        () => collectContiguousPlyHistory(container, "data-san"),
        () => collectContiguousPlyHistory(container, "data-move"),
        () => collectContiguousPlyHistory(container, null),
        () => collectPairedRowHistory(container),
        () => collectNumberedContainerTextHistory(container),
        () => collectLooseAttributeHistory(container, "data-uci"),
        () => collectLooseAttributeHistory(container, "data-san"),
        () => collectLooseAttributeHistory(container, "data-move"),
        () => collectLooseSanHistory(container)
    ];

    let history = null;
    for (const attempt of attempts) {
        history = attempt();
        if (history !== null) {
            break;
        }
    }
    if (history === null || history.tokens.length === 0) {
        return null;
    }

    const moves = history.tokens.join("|");
    if (moves.length > MAX_HISTORY_BYTES) {
        return null;
    }
    return {
        notation: history.notation,
        moves,
        complete: history.complete,
        initialFen: readInitialFen(container),
        result: history.result ?? readVisibleGameResult()
    };
}

function currentPageState(reason, gameResult = undefined) {
    const state = {
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
    if (reason === "game_end") {
        state.game_result = canonicalGameResult(gameResult) ?? "*";
    }
    return state;
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
    clearTimeout(recoveryTimer);
    recoveryTimer = null;
}

function invalidateHistoryCapture() {
    historyEpoch += 1;
    clearTimeout(historyTimer);
    historyTimer = null;
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
    }, CAPTURE_DEBOUNCE_MS);
}

function scheduleHistoryCapture(force = false) {
    if (force) {
        forceNextHistoryCapture = true;
    }
    if (
        activeBoard === null ||
        activeHistoryContainer === null ||
        pointerIsDown ||
        gameIsOver ||
        !isGamePage()
    ) {
        return;
    }

    clearTimeout(historyTimer);
    const token = ++historyEpoch;
    historyTimer = setTimeout(() => {
        historyTimer = null;
        const forced = forceNextHistoryCapture;
        void captureHistory(token, forced);
    }, HISTORY_DEBOUNCE_MS);
}

function scheduleDelayedRecovery(snapshot) {
    clearTimeout(recoveryTimer);
    recoveryTimer = setTimeout(() => {
        recoveryTimer = null;
        void sendDelayedRecovery(snapshot);
    }, RECOVERY_DELAY_MS);
}

async function sendDelayedRecovery(snapshot) {
    const board = activeBoard;
    if (
        snapshot !== lastStableSnapshot ||
        board === null ||
        !board.isConnected ||
        pointerIsDown ||
        gameIsOver ||
        snapshot.routeGeneration !== routeGeneration ||
        snapshot.gameKey !== gameKey
    ) {
        return;
    }

    const reading = readBoardPosition(board);
    const flipped = board.classList.contains("flipped");
    if (
        reading === null ||
        reading.position !== snapshot.board ||
        flipped !== snapshot.visuallyFlipped
    ) {
        return;
    }

    sendWithoutWaiting({
        type: "board_candidate",
        page_instance_id: PAGE_INSTANCE_ID,
        route_generation: snapshot.routeGeneration,
        game_key: snapshot.gameKey,
        url: window.location.href,
        visible: pageIsVisible(),
        board_id: board.id,
        board: reading.position,
        piece_count: reading.pieceCount,
        visually_flipped: flipped,
        captured_at: Date.now(),
        force: false,
        recovery: true
    });
}

async function captureHistory(token, forced) {
    const board = activeBoard;
    const container = activeHistoryContainer;
    const snapshot = lastStableSnapshot;
    if (
        token !== historyEpoch ||
        board === null ||
        container === null ||
        !board.isConnected ||
        container.isConnected === false ||
        pointerIsDown ||
        gameIsOver ||
        snapshot === null ||
        snapshot.routeGeneration !== routeGeneration ||
        snapshot.gameKey !== gameKey
    ) {
        return;
    }

    const reading = readBoardPosition(board);
    const flipped = board.classList.contains("flipped");
    if (
        reading === null ||
        reading.position !== snapshot.board ||
        flipped !== snapshot.visuallyFlipped
    ) {
        return;
    }

    const history = readMoveHistory(container);
    if (history === null) {
        return;
    }
    const fingerprint = [
        PAGE_INSTANCE_ID,
        routeGeneration,
        gameKey,
        reading.position,
        history.notation,
        history.moves,
        history.complete ? "complete" : "partial",
        history.initialFen ?? "",
        history.result
    ].join("|");
    if (!forced && fingerprint === lastSubmittedHistoryFingerprint) {
        return;
    }

    const message = {
        type: "history_candidate",
        page_instance_id: PAGE_INSTANCE_ID,
        route_generation: routeGeneration,
        game_key: gameKey,
        url: window.location.href,
        visible: pageIsVisible(),
        displayed_board: reading.position,
        history_notation: history.notation,
        history_moves: history.moves,
        history_complete: history.complete,
        game_result: history.result,
        captured_at: Date.now()
    };
    if (history.initialFen !== null) {
        message.initial_fen = history.initialFen;
    }

    try {
        const reply = await browser.runtime.sendMessage(message);
        if (
            token === historyEpoch &&
            snapshot === lastStableSnapshot &&
            reply?.accepted === true
        ) {
            lastSubmittedHistoryFingerprint = fingerprint;
            if (forced) {
                forceNextHistoryCapture = false;
            }
        }
    } catch (error) {
        if (token === historyEpoch) {
            console.debug(
                "[ChessListener] could not cache move history:",
                error
            );
            scheduleHistoryCapture(forced);
        }
    }
}

async function submitFinalGameState() {
    /* Capture this before awaiting either native-message round trip. The game
     * over surface can be replaced while the final board/history is in flight. */
    const finalGameResult = readVisibleGameResult();
    const board = activeBoard;
    const container = activeHistoryContainer;
    if (board !== null && board.isConnected && !pointerIsDown) {
        const reading = readBoardPosition(board);
        if (reading !== null) {
            const snapshotMessage = {
                type: "board_candidate",
                page_instance_id: PAGE_INSTANCE_ID,
                route_generation: routeGeneration,
                game_key: gameKey,
                url: window.location.href,
                visible: pageIsVisible(),
                board_id: board.id,
                board: reading.position,
                piece_count: reading.pieceCount,
                visually_flipped: board.classList.contains("flipped"),
                captured_at: Date.now(),
                force: true,
                recovery: false
            };
            try {
                const snapshotReply = await browser.runtime.sendMessage(snapshotMessage);
                if (snapshotReply?.accepted === true && container !== null) {
                    const history = readMoveHistory(container);
                    if (history !== null && history.complete) {
                        const historyMessage = {
                            type: "history_candidate",
                            page_instance_id: PAGE_INSTANCE_ID,
                            route_generation: routeGeneration,
                            game_key: gameKey,
                            url: window.location.href,
                            visible: pageIsVisible(),
                            displayed_board: reading.position,
                            history_notation: history.notation,
                            history_moves: history.moves,
                            history_complete: true,
                            game_result: history.result,
                            captured_at: Date.now()
                        };
                        if (history.initialFen !== null) {
                            historyMessage.initial_fen = history.initialFen;
                        }
                        await browser.runtime.sendMessage(historyMessage);
                    }
                }
            } catch (error) {
                console.debug("[ChessListener] final game capture failed:", error);
            }
        }
    }
    sendWithoutWaiting(currentPageState("game_end", finalGameResult));
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

    await new Promise((resolve) => setTimeout(resolve, STABLE_CONFIRM_MS));
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
            const snapshot = {
                routeGeneration: capturedGeneration,
                gameKey: capturedGameKey,
                board: secondReading.position,
                visuallyFlipped: secondFlip
            };
            lastStableSnapshot = snapshot;
            if (forced) {
                forceNextCapture = false;
            }
            scheduleHistoryCapture(forced);
            scheduleDelayedRecovery(snapshot);
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
    lastStableSnapshot = null;

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

function attachToHistory(container) {
    if (container === activeHistoryContainer) {
        return false;
    }
    invalidateHistoryCapture();
    if (historyObserver !== null) {
        historyObserver.disconnect();
        historyObserver = null;
    }
    activeHistoryContainer = container;
    lastSubmittedHistoryFingerprint = null;

    if (activeHistoryContainer !== null) {
        historyObserver = new MutationObserver(() => scheduleHistoryCapture());
        historyObserver.observe(activeHistoryContainer, {
            childList: true,
            subtree: true,
            characterData: true,
            attributes: true,
            attributeFilter: [
                "data-ply",
                "data-uci",
                "data-san",
                "data-move",
                "data-initial-fen",
                "data-start-fen"
            ]
        });
        scheduleHistoryCapture();
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
    let deferredGameEnd = false;
    if (window.location.href !== routeUrl) {
        invalidateCaptures();
        invalidateHistoryCapture();
        routeGeneration += 1;
        routeUrl = window.location.href;
        gameKey = buildGameKey();
        lastSubmittedIdentity = null;
        lastStableSnapshot = null;
        lastSubmittedHistoryFingerprint = null;
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

    const historyChanged = attachToHistory(findHistoryContainer());
    if (historyChanged) {
        changed = true;
    }

    const nowGameOver = detectGameOver();
    if (nowGameOver && !gameIsOver) {
        invalidateCaptures();
        invalidateHistoryCapture();
        gameIsOver = true;
        markGameOver();
        changed = true;
        reason = "game_end";
        deferredGameEnd = true;
    } else if (gameIsOver && !nowGameOver && isClearlyNewGame(boardChanged)) {
        invalidateCaptures();
        invalidateHistoryCapture();
        gameIsOver = false;
        gameOverMarker = null;
        changed = true;

        if (!routeChanged) {
            routeGeneration += 1;
            gameKey = buildGameKey();
        }
        lastSubmittedIdentity = null;
        lastStableSnapshot = null;
        lastSubmittedHistoryFingerprint = null;
        reason = "new_game";
    }

    if (deferredGameEnd) {
        void submitFinalGameState();
    } else if (changed || reason !== "poll") {
        notifyPageState(reason);
    }
    if (!gameIsOver && activeBoard !== null && changed) {
        scheduleCapture(reason === "navigation" || reason === "new_game");
    }
    if (
        !gameIsOver &&
        activeBoard !== null &&
        activeHistoryContainer !== null &&
        changed
    ) {
        scheduleHistoryCapture(reason === "navigation" || reason === "new_game");
    }
}

document.addEventListener(
    "pointerdown",
    (event) => {
        if (activeBoard !== null && activeBoard.contains(event.target)) {
            pointerIsDown = true;
            invalidateCaptures();
            invalidateHistoryCapture();
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
    scheduleHistoryCapture();
}

document.addEventListener("pointerup", finishPointerInteraction, true);
document.addEventListener("pointercancel", finishPointerInteraction, true);

window.addEventListener("blur", () => {
    pointerIsDown = false;
    invalidateCaptures();
    invalidateHistoryCapture();
    if (pageIsVisible()) {
        scheduleCapture();
        scheduleHistoryCapture();
    }
});

document.addEventListener("visibilitychange", () => {
    pointerIsDown = false;
    invalidateCaptures();
    invalidateHistoryCapture();
    refreshContext("visibility");
    if (pageIsVisible()) {
        scheduleCapture();
        scheduleHistoryCapture();
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
    invalidateHistoryCapture();
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
