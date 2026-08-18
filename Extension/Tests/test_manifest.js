"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const extensionDir = path.resolve(__dirname, "..");
const manifestPath = path.join(extensionDir, "manifest.json");
const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
const requiredSizes = ["16", "32", "48", "96"];

function checkedExtensionPath(relativePath, label) {
    assert.equal(typeof relativePath, "string", `${label} must be a path`);
    assert.ok(relativePath.length > 0, `${label} must not be empty`);
    assert.equal(path.isAbsolute(relativePath), false, `${label} must be relative`);
    const resolved = path.resolve(extensionDir, relativePath);
    assert.ok(
        resolved.startsWith(`${extensionDir}${path.sep}`),
        `${label} must stay inside Extension/`
    );
    assert.ok(fs.existsSync(resolved), `${label} file must exist`);
    assert.ok(fs.statSync(resolved).isFile(), `${label} file must exist`);
    return resolved;
}

assert.equal(manifest.manifest_version, 3);
assert.deepEqual(
    manifest.action?.default_icon,
    manifest.icons,
    "toolbar and add-ons chrome must use the same CL mark"
);
for (const [label, icons] of [
    ["extension icon", manifest.icons],
    ["action icon", manifest.action?.default_icon]
]) {
    assert.equal(typeof icons, "object", `${label} map is required`);
    for (const size of requiredSizes) {
        checkedExtensionPath(icons[size], `${label} ${size}px`);
    }
}

const sourceIcon = checkedExtensionPath(
    manifest.icons["48"], "canonical extension icon"
);
assert.equal(path.extname(sourceIcon), ".svg", "canonical icon must stay scalable");
const svg = fs.readFileSync(sourceIcon, "utf8");
assert.match(svg, /<svg\b[^>]*xmlns="http:\/\/www\.w3\.org\/2000\/svg"/);
assert.match(svg, /viewBox="0 0 128 128"/);
assert.match(svg, /#e8d5b4/i, "icon must use the parchment board token");
assert.match(svg, /#987652/i, "icon must use the walnut board token");
assert.match(svg, /#151714/i, "icon must use the matte shell token");
assert.match(svg, />C<\/text>/);
assert.match(svg, />L<\/text>/);
assert.doesNotMatch(svg, /<script\b|<image\b/i, "icon must remain self-contained");

checkedExtensionPath(manifest.action.default_popup, "action popup");
for (const script of manifest.background?.scripts ?? []) {
    checkedExtensionPath(script, "background script");
}
for (const entry of manifest.content_scripts ?? []) {
    for (const script of entry.js ?? []) {
        checkedExtensionPath(script, "content script");
    }
}

console.log("PASS manifest resources and Firefox CL icon contract");
