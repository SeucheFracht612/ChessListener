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

let activeBoard = null;
let boardObserver = null;
let captureTimer = null;
let lastSentPosition = null;
let pointerIsDown = false;

console.log("[ChessListener] content script loaded.");

function IsGamePage() {
    return (
        window.location.pathname.startsWith("/play/") ||
        window.location.pathname.startsWith("/game/")
    );
}

function FindActiveBoard() {
    if (!IsGamePage()) {
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

        const leftArea =
        leftRectangle.width * leftRectangle.height;

        const rightArea =
        rightRectangle.width * rightRectangle.height;

        return rightArea - leftArea;
    });

    return boards[0] ?? null;
}

function ReadPiece(element) {
    const pieceClass = [...element.classList].find((className) =>
    /^[wb][prnbqk]$/.test(className)
    );

    const squareClass = [...element.classList].find((className) =>
    /^square-[1-8][1-8]$/.test(className)
    );

    if (pieceClass === undefined || squareClass === undefined) {
        return null;
    }

    const squareMatch =
    /^square-([1-8])([1-8])$/.exec(squareClass);

    if (squareMatch === null) {
        return null;
    }

    const file = Number(squareMatch[1]);
    const rank = Number(squareMatch[2]);
    const character = PIECE_TO_CHARACTER[pieceClass];

    if (character === undefined) {
        return null;
    }

    return {
        character,
        file,
        rank
    };
}

function ReadBoardPosition(board) {
    const squares = new Array(64).fill(".");
    let pieceCount = 0;

    for (const element of board.querySelectorAll(".piece")) {
        const piece = ReadPiece(element);

        /*
         * Chess.com may temporarily create auxiliary piece elements
         * during animations or dragging. Ignore elements without both
         * a valid piece code and a valid square.
         */
        if (piece === null) {
            continue;
        }

        /*
         * Our array starts at a8:
         *
         * index 0  = a8
         * index 7  = h8
         * index 56 = a1
         * index 63 = h1
         */
        const row = 8 - piece.rank;
        const column = piece.file - 1;
        const index = row * 8 + column;

        /*
         * Two pieces cannot occupy the same square. Seeing this means
         * Chess.com is currently between DOM states.
         */
        if (squares[index] !== ".") {
            return null;
        }

        squares[index] = piece.character;
        pieceCount += 1;
    }

    const position = squares.join("");

    /*
     * Reject clearly incomplete animation states.
     */
    if (!position.includes("K") || !position.includes("k")) {
        return null;
    }

    return {
        position,
        pieceCount
    };
}

function FormatBoard(position) {
    return position.match(/.{8}/g)?.join("\n") ?? position;
}

function ScheduleCapture() {
    if (activeBoard === null || pointerIsDown) {
        return;
    }

    clearTimeout(captureTimer);

    captureTimer = setTimeout(() => {
        CaptureStablePosition();
    }, 120);
}

async function CaptureStablePosition() {
    const board = activeBoard;

    if (board === null || !board.isConnected || pointerIsDown) {
        return;
    }

    const firstReading = ReadBoardPosition(board);

    if (firstReading === null) {
        return;
    }

    /*
     * Read twice to avoid capturing halfway through castling,
     * promotion, capture animations, or DOM replacement.
     */
    await new Promise((resolve) => {
        setTimeout(resolve, 75);
    });

    if (
        board !== activeBoard ||
        !board.isConnected ||
        pointerIsDown
    ) {
        return;
    }

    const secondReading = ReadBoardPosition(board);

    if (
        secondReading === null ||
        firstReading.position !== secondReading.position
    ) {
        ScheduleCapture();
        return;
    }

    if (secondReading.position === lastSentPosition) {
        return;
    }

    const message = {
        type: "position_snapshot",
        url: window.location.href,
        captured_at: Date.now(),
        board_id: board.id,
        board: secondReading.position,
        piece_count: secondReading.pieceCount,
        visually_flipped:
        board.classList.contains("flipped")
    };

    try {
        console.log(
            "[ChessListener] sending position:\n" +
            FormatBoard(message.board)
        );

        const response =
        await browser.runtime.sendMessage(message);

        console.log(
            "[ChessListener] background acknowledged position:",
            response
        );

        /*
         * Only mark it as sent after successful communication.
         */
        lastSentPosition = secondReading.position;
    } catch (error) {
        console.error(
            "[ChessListener] could not send position:",
            error
        );
    }
}

function AttachToBoard(board) {
    if (board === activeBoard) {
        return;
    }

    if (boardObserver !== null) {
        boardObserver.disconnect();
        boardObserver = null;
    }

    activeBoard = board;
    lastSentPosition = null;

    if (activeBoard === null) {
        console.log("[ChessListener] no active game board found.");
        return;
    }

    console.log(
        `[ChessListener] attached to board #${activeBoard.id}`
    );

    boardObserver = new MutationObserver(() => {
        ScheduleCapture();
    });

    boardObserver.observe(activeBoard, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ["class"]
    });

    ScheduleCapture();
}

function RefreshActiveBoard() {
    const board = FindActiveBoard();

    if (board !== activeBoard) {
        AttachToBoard(board);
    }
}

/*
 * Avoid recording an incomplete state while the user is dragging
 * a piece around the board.
 */
document.addEventListener(
    "pointerdown",
    (event) => {
        if (
            activeBoard !== null &&
            activeBoard.contains(event.target)
        ) {
            pointerIsDown = true;
            clearTimeout(captureTimer);
        }
    },
    true
);

document.addEventListener(
    "pointerup",
    () => {
        if (!pointerIsDown) {
            return;
        }

        pointerIsDown = false;
        ScheduleCapture();
    },
    true
);

/*
 * Chess.com uses client-side navigation and may replace the entire
 * board without reloading the page.
 */
RefreshActiveBoard();
setInterval(RefreshActiveBoard, 1000);
