"""Check gate playback and fallback without a browser or network."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

HARNESS = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const scenario = process.argv[2];
const callbacks = new Map();
let nextFrame = 1;
let getContextCalls = 0;
let fetchCalls = 0;
let deletedPrograms = 0;
let observer;

class Element {
    constructor() {
        this.dataset = {};
        this.style = {};
        this.hidden = false;
        this.attributes = {};
        this.listeners = {};
        this.textContent = "";
    }
    addEventListener(name, callback) { this.listeners[name] = callback; }
    emit(name, event = {}) { this.listeners[name](event); }
    setAttribute(name, value) { this.attributes[name] = value; }
    toggleAttribute(name, enabled) {
        if (enabled) this.attributes[name] = "";
        else delete this.attributes[name];
    }
    getBoundingClientRect() { return { width: 448, height: 448 }; }
}

const root = new Element();
const canvas = new Element();
const fallback = new Element();
const toggle = new Element();
const label = new Element();
const ring = new Element();
const counterRing = new Element();
canvas.dataset.vertexShader = "/static/shaders/signal-gate.vert";
canvas.dataset.fragmentShader = "/static/shaders/signal-gate.frag";
canvas.hidden = true;
toggle.hidden = true;
toggle.querySelector = (name) => name === "[data-gate-toggle-label]" ? label : null;
root.querySelector = (name) => ({
    "[data-gate-canvas]": canvas,
    "[data-gate-fallback]": fallback,
    "[data-gate-toggle]": toggle,
    "[data-gate-ring]": ring,
    "[data-gate-counter-ring]": counterRing,
})[name];

const gl = {
    VERTEX_SHADER: 1, FRAGMENT_SHADER: 2, COMPILE_STATUS: 3, LINK_STATUS: 4,
    ARRAY_BUFFER: 5, STATIC_DRAW: 6, FLOAT: 7, TRIANGLE_STRIP: 8, NO_ERROR: 0,
    createShader: () => ({}), shaderSource() {}, compileShader() {},
    getShaderParameter: () => true, deleteShader() {},
    createProgram: () => ({}), attachShader() {}, linkProgram() {},
    getProgramParameter: () => true,
    deleteProgram() { deletedPrograms += 1; },
    createBuffer: () => ({}), bindBuffer() {}, bufferData() {}, deleteBuffer() {},
    getAttribLocation: () => 0, getUniformLocation: () => ({}),
    viewport() {}, useProgram() {}, enableVertexAttribArray() {},
    vertexAttribPointer() {}, uniform2f() {}, uniform1f() {}, drawArrays() {},
    getError: () => 0,
};
canvas.getContext = () => {
    getContextCalls += 1;
    return scenario === "no_webgl" ? null : gl;
};

const motion = {
    matches: scenario === "reduced_motion",
    addEventListener(name, callback) { this.callback = callback; },
    change(value) { this.matches = value; this.callback(); },
};
const connection = { saveData: scenario === "save_data", addEventListener() {} };
const windowListeners = {};
global.window = {
    matchMedia: () => motion, devicePixelRatio: 2,
    IntersectionObserver: true,
    requestAnimationFrame(callback) {
        const id = nextFrame++;
        callbacks.set(id, callback);
        return id;
    },
    cancelAnimationFrame(id) { callbacks.delete(id); },
    addEventListener(name, callback) { windowListeners[name] = callback; },
};
global.IntersectionObserver = class {
    constructor(callback) { this.callback = callback; observer = this; }
    observe() {}
    change(value) { this.callback([{ isIntersecting: value }]); }
};
global.document = {
    hidden: false, listeners: {}, querySelector: () => root,
    addEventListener(name, callback) { this.listeners[name] = callback; },
};
Object.defineProperty(global, "navigator", { value: { connection }, configurable: true });
global.fetch = async () => {
    fetchCalls += 1;
    if (scenario === "fetch_failure") throw new Error("Mock request failure");
    return { ok: true, text: async () => "mock shader" };
};

function advance(now) {
    const entries = [...callbacks.entries()];
    callbacks.clear();
    for (const [, callback] of entries) callback(now);
}
async function settle() {
    await new Promise((resolve) => setImmediate(resolve));
}

vm.runInThisContext(fs.readFileSync(process.argv[1], "utf8"));

(async () => {
    await settle();
    if (scenario === "reduced_motion" || scenario === "save_data") {
        assert.equal(root.dataset.gateState, "static");
        assert.equal(getContextCalls, 0);
        assert.equal(fetchCalls, 0);
        assert.equal(toggle.hidden, true);
        if (scenario === "reduced_motion") {
            motion.change(false);
            await settle();
            observer.change(true);
            assert.equal(root.dataset.gateState, "running");
            motion.change(true);
            assert.equal(root.dataset.gateState, "static");
            assert.equal(callbacks.size, 0);
        }
    } else if (scenario === "no_webgl" || scenario === "fetch_failure") {
        assert.equal(root.dataset.gateState, "static");
        assert.equal(canvas.hidden, true);
        assert.equal(toggle.hidden, true);
        assert.equal("hidden" in fallback.attributes, false);
    } else {
        assert.equal(root.dataset.gateState, "paused");
        assert.equal(canvas.hidden, false);
        assert.equal("hidden" in fallback.attributes, true);
        assert.equal(toggle.hidden, false);
        assert.equal(canvas.width <= 1024, true);
        assert.equal(canvas.height <= 1024, true);
        observer.change(true);
        assert.equal(root.dataset.gateState, "running");
        advance(34);
        advance(70);
        assert.notEqual(ring.attributes.transform, "rotate(0 400 400)");
        assert.equal(counterRing.attributes.transform.includes("rotate(-"), true);
        toggle.emit("click");
        assert.equal(root.dataset.gateState, "paused");
        assert.equal(toggle.attributes["aria-pressed"], "true");
        assert.equal(label.textContent, "Resume gate animation");
        assert.equal(toggle.attributes["title"], "Resume gate animation");
        assert.equal(callbacks.size, 0);
        toggle.emit("click");
        assert.equal(root.dataset.gateState, "running");
        assert.equal(toggle.attributes["aria-pressed"], "false");
        observer.change(false);
        assert.equal(root.dataset.gateState, "paused");
        assert.equal(callbacks.size, 0);
        observer.change(true);
        document.hidden = true;
        document.listeners.visibilitychange();
        assert.equal(root.dataset.gateState, "paused");
        document.hidden = false;
        document.listeners.visibilitychange();
        assert.equal(root.dataset.gateState, "running");
        if (scenario === "context_loss") {
            let prevented = false;
            canvas.emit("webglcontextlost", { preventDefault() { prevented = true; } });
            assert.equal(prevented, true);
            assert.equal(root.dataset.gateState, "static");
            assert.equal(toggle.hidden, true);
            canvas.emit("webglcontextrestored");
            await settle();
            assert.equal(root.dataset.gateState, "running");
            assert.equal(getContextCalls, 2);
        } else {
            toggle.emit("click");
            windowListeners.pagehide();
            assert.equal(root.dataset.gateState, "static");
            assert.equal(callbacks.size, 0);
            assert.equal(deletedPrograms > 0, true);
            windowListeners.pageshow();
            await settle();
            assert.equal(root.dataset.gateState, "paused");
            assert.equal(toggle.attributes["aria-pressed"], "true");
        }
    }
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.parametrize(
    "scenario",
    (
        "playback",
        "reduced_motion",
        "save_data",
        "no_webgl",
        "fetch_failure",
        "context_loss",
    ),
)
def test_gate_browser_states(scenario: str) -> None:
    """The gate keeps a static view when it cannot run."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    result = subprocess.run(
        [node, "-e", HARNESS, str(ROOT / "website/assets/js/signal-gate.js"), scenario],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
