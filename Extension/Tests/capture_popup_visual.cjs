#!/usr/bin/env node
"use strict";

// Optional real-browser companion to Native/Tests/test_visual_ui.py. It uses
// deterministic WebExtension API stubs, never opens a network connection, and
// cleanly skips when Playwright or its Chromium runtime is unavailable.

const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");
const { pathToFileURL } = require("node:url");

const outputDir = process.env.CHESSLISTENER_VISUAL_OUTPUT?.trim();
if (!outputDir) {
    console.log("SKIP popup visual matrix: CHESSLISTENER_VISUAL_OUTPUT is unset");
    process.exit(0);
}

fs.mkdirSync(outputDir, { recursive: true });
const manifestPath = path.join(outputDir, "popup-visual-manifest.json");
try {
    fs.unlinkSync(manifestPath);
} catch (error) {
    if (error.code !== "ENOENT") {
        throw error;
    }
}

let chromium;
try {
    ({ chromium } = require("playwright"));
} catch (_error) {
    console.log("SKIP popup visual matrix: Playwright is not installed");
    process.exit(0);
}

const configuredExecutable =
    process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE?.trim() || "";
const executablePath = configuredExecutable || chromium.executablePath();
if (!executablePath || !fs.existsSync(executablePath)) {
    console.log(
        `SKIP popup visual matrix: Chromium runtime not found at ${executablePath || "(unset)"}`
    );
    process.exit(0);
}

const extensionDir = path.resolve(__dirname, "..");
const popupUrl = pathToFileURL(path.join(extensionDir, "popup.html")).href;
const baseState = {
    extension_version: "0.9.5",
    native_protocol_version: 4,
    status: "idle",
    protocol_error: null,
    session: null
};
const ownerSession = {
    tab_id: 7,
    snapshot_seq: 18,
    url: "https://www.chess.com/play/computer/Martin-Bot",
    last_error: null
};

const scenarios = [
    {
        name: "popup-idle",
        description: "No current board and one clear start action.",
        state: baseState,
        expect: "Start analysis"
    },
    {
        name: "popup-owner",
        description: "The active Firefox tab owns a healthy local session.",
        state: { ...baseState, status: "active", session: ownerSession },
        expect: "Refresh this board"
    },
    {
        name: "popup-connecting",
        description: "The owned board is starting its local analyzer.",
        state: { ...baseState, status: "connecting", session: ownerSession },
        expect: "Refresh this board"
    },
    {
        name: "popup-switch",
        description: "A different active tab can deliberately take ownership.",
        state: {
            ...baseState,
            status: "active",
            session: { ...ownerSession, tab_id: 19 }
        },
        expect: "Switch to this tab"
    },
    {
        name: "popup-dismissed",
        description: "The board remains owned after the overlay was closed.",
        state: { ...baseState, status: "dismissed", session: ownerSession },
        expect: "Reopen overlay"
    },
    {
        name: "popup-error",
        description: "Native failure translated into recovery copy and details.",
        state: {
            ...baseState,
            status: "error",
            session: {
                ...ownerSession,
                last_error: "native process exited again"
            }
        },
        expect: "Reopen overlay"
    },
    {
        name: "popup-busy",
        description: "Refresh remains visibly busy while its response is pending.",
        state: { ...baseState, status: "active", session: ownerSession },
        expect: "Refreshing…",
        busy: true
    }
];

const viewports = [
    { name: "normal", width: 320, height: 620, zoom: 1 },
    { name: "large-text", width: 448, height: 868, zoom: 1.4 }
];

function sha256(filename) {
    return crypto.createHash("sha256").update(fs.readFileSync(filename)).digest("hex");
}

async function installBrowserStub(page, state, busy) {
    await page.addInitScript(({ initialState, keepActionPending }) => {
        const listeners = [];
        const never = new Promise(() => {});
        Object.defineProperty(globalThis, "browser", {
            configurable: false,
            value: {
                tabs: {
                    query: async () => [{ id: 7 }]
                },
                runtime: {
                    sendMessage: async (message) => {
                        if (message?.type === "popup_get_state") {
                            return initialState;
                        }
                        if (
                            keepActionPending &&
                            message?.type === "popup_action"
                        ) {
                            return never;
                        }
                        return { ok: true, state: initialState };
                    },
                    onMessage: {
                        addListener: (listener) => listeners.push(listener)
                    }
                }
            }
        });
    }, { initialState: state, keepActionPending: busy });
}

async function capture(browser, scenario, viewport) {
    const context = await browser.newContext({
        viewport: { width: viewport.width, height: viewport.height },
        deviceScaleFactor: 1,
        colorScheme: "dark",
        reducedMotion: "reduce"
    });
    const page = await context.newPage();
    const fatal = [];
    const warnings = [];
    try {
        await installBrowserStub(page, scenario.state, scenario.busy === true);
        await page.goto(popupUrl, { waitUntil: "load" });
        await page.locator("#analyze").waitFor({ state: "visible" });
        if (viewport.zoom !== 1) {
            await page.evaluate((zoom) => {
                document.body.style.zoom = String(zoom);
            }, viewport.zoom);
        }
        if (scenario.busy) {
            await page.locator("#analyze").click();
            await page.locator('#popup-root[aria-busy="true"]').waitFor();
        }
        const actualLabel = (await page.locator("#analyze").textContent())?.trim();
        if (actualLabel !== scenario.expect) {
            fatal.push(
                `primary action expected ${JSON.stringify(scenario.expect)}, got ${JSON.stringify(actualLabel)}`
            );
        }
        const structure = await page.evaluate(() => {
            const root = document.querySelector("#popup-root");
            const body = document.body;
            const buttons = [...document.querySelectorAll("button:not([hidden])")];
            const clipped = [...document.querySelectorAll(
                "h1, #status-badge, #session-label, #session-detail, #error-title, #error, button, footer span"
            )].filter((element) => (
                element.scrollWidth > element.clientWidth + 1 ||
                element.scrollHeight > element.clientHeight + 1
            )).map((element) => element.id || element.tagName.toLowerCase());
            return {
                busy: root.getAttribute("aria-busy"),
                horizontalOverflow: body.scrollWidth > body.clientWidth + 1,
                clipped,
                unnamedButtons: buttons.filter((button) => (
                    !(button.textContent || "").trim() &&
                    !(button.getAttribute("aria-label") || "").trim()
                )).length,
                shortButtons: buttons.filter((button) => (
                    button.getBoundingClientRect().height < 40
                )).length,
                fontHeight: Number.parseFloat(
                    getComputedStyle(document.querySelector("#session-label")).lineHeight
                ) || 0
            };
        });
        if (structure.horizontalOverflow) {
            fatal.push("popup has horizontal page overflow");
        }
        if (structure.clipped.length) {
            fatal.push(`popup text clips: ${structure.clipped.join(", ")}`);
        }
        if (structure.unnamedButtons) {
            fatal.push(`${structure.unnamedButtons} visible button(s) have no name`);
        }
        if (structure.shortButtons) {
            fatal.push(`${structure.shortButtons} action target(s) are below 40px`);
        }
        if (scenario.busy && structure.busy !== "true") {
            fatal.push("busy popup does not expose aria-busy=true");
        }

        const filename = `${scenario.name}--${viewport.name}.png`;
        const outputPath = path.join(outputDir, filename);
        const body = page.locator("body");
        await body.screenshot({ path: outputPath, animations: "disabled" });
        const box = await body.boundingBox();
        return {
            scenario: scenario.name,
            description: scenario.description,
            viewport: viewport.name,
            requested_size: [viewport.width, viewport.height],
            actual_size: [Math.round(box?.width || 0), Math.round(box?.height || 0)],
            page: "Firefox popup",
            file: filename,
            sha256: sha256(outputPath),
            visible_controls: await page.locator("button:not([hidden])").count(),
            font_pixel_height: structure.fontHeight * viewport.zoom,
            live_board_geometry: null,
            fatal,
            warnings,
            baseline_diff: null
        };
    } finally {
        await context.close();
    }
}

(async () => {
    let browser;
    try {
        browser = await chromium.launch({
            executablePath,
            headless: true,
            args: ["--disable-gpu", "--font-render-hinting=none"]
        });
    } catch (error) {
        console.log(
            `SKIP popup visual matrix: Chromium could not start (${error.message})`
        );
        return;
    }
    const entries = [];
    try {
        for (const scenario of scenarios) {
            for (const viewport of viewports) {
                entries.push(await capture(browser, scenario, viewport));
            }
        }
    } finally {
        await browser.close();
    }

    for (const scenario of scenarios) {
        const normal = entries.find((entry) => (
            entry.scenario === scenario.name && entry.viewport === "normal"
        ));
        const enlarged = entries.find((entry) => (
            entry.scenario === scenario.name && entry.viewport === "large-text"
        ));
        if (normal && enlarged) {
            if (enlarged.sha256 === normal.sha256) {
                enlarged.fatal.push("large-text popup is pixel-identical to normal");
            }
            if (enlarged.font_pixel_height <= normal.font_pixel_height) {
                enlarged.fatal.push("large-text popup font did not increase");
            }
        }
    }

    fs.writeFileSync(
        manifestPath,
        `${JSON.stringify({ schema: 1, entries }, null, 2)}\n`,
        "utf8"
    );
    console.log(`Popup visual renders: ${entries.length} PNGs in ${outputDir}`);
})().catch((error) => {
    console.error(`Popup visual matrix failed: ${error.stack || error}`);
    process.exitCode = 1;
});
