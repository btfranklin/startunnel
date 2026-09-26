(function () {
    "use strict";

    const root = document.querySelector("[data-signal-gate]");
    if (!root) return;

    const canvas = root.querySelector("[data-gate-canvas]");
    const fallback = root.querySelector("[data-gate-fallback]");
    const toggle = root.querySelector("[data-gate-toggle]");
    const ring = root.querySelector("[data-gate-ring]");
    const counterRing = root.querySelector("[data-gate-counter-ring]");
    if (!canvas || !fallback || !toggle || !ring || !counterRing) return;

    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    const shaderUrls = [canvas.dataset.vertexShader, canvas.dataset.fragmentShader];
    let renderer = null;
    let pendingFetch = null;
    let generation = 0;
    let frame = 0;
    let elapsed = 0;
    let lastTick = 0;
    let lastDraw = 0;
    let userPaused = false;
    let inView = !window.IntersectionObserver;
    let pageHidden = false;
    let contextLost = false;

    function setButtonLabel() {
        const label = userPaused ? "Resume gate animation" : "Pause gate animation";
        toggle.setAttribute("aria-label", label);
        toggle.setAttribute("title", label);
        toggle.setAttribute("aria-pressed", String(userPaused));
        const visibleLabel = toggle.querySelector("[data-gate-toggle-label]");
        if (visibleLabel) visibleLabel.textContent = label;
        else toggle.textContent = label;
    }

    function cancelFrame() {
        if (frame) window.cancelAnimationFrame(frame);
        frame = 0;
        lastTick = 0;
        lastDraw = 0;
    }

    function resetRings() {
        ring.setAttribute("transform", "rotate(0 400 400)");
        counterRing.setAttribute("transform", "rotate(0 400 400)");
    }

    function releaseRenderer() {
        cancelFrame();
        if (!renderer) return;
        const { gl, program, buffer } = renderer;
        if (!contextLost) {
            gl.deleteBuffer(buffer);
            gl.deleteProgram(program);
        }
        renderer = null;
    }

    function showStatic() {
        generation += 1;
        if (pendingFetch) pendingFetch.abort();
        pendingFetch = null;
        releaseRenderer();
        canvas.hidden = true;
        canvas.style.visibility = "";
        fallback.toggleAttribute("hidden", false);
        toggle.hidden = true;
        root.dataset.gateState = "static";
        resetRings();
    }

    function compile(gl, kind, source) {
        const shader = gl.createShader(kind);
        if (!shader) throw new Error("Shader unavailable");
        gl.shaderSource(shader, source);
        gl.compileShader(shader);
        if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
            gl.deleteShader(shader);
            throw new Error("Shader compilation failed");
        }
        return shader;
    }

    function makeRenderer(gl, vertexSource, fragmentSource) {
        const vertex = compile(gl, gl.VERTEX_SHADER, vertexSource);
        let fragment;
        try {
            fragment = compile(gl, gl.FRAGMENT_SHADER, fragmentSource);
            const program = gl.createProgram();
            if (!program) throw new Error("Program unavailable");
            gl.attachShader(program, vertex);
            gl.attachShader(program, fragment);
            gl.linkProgram(program);
            if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
                gl.deleteProgram(program);
                throw new Error("Program link failed");
            }
            const buffer = gl.createBuffer();
            if (!buffer) {
                gl.deleteProgram(program);
                throw new Error("Buffer unavailable");
            }
            gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
            gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
            const position = gl.getAttribLocation(program, "a_position");
            const resolution = gl.getUniformLocation(program, "u_resolution");
            const time = gl.getUniformLocation(program, "u_time");
            if (position < 0 || !resolution || !time) {
                gl.deleteBuffer(buffer);
                gl.deleteProgram(program);
                throw new Error("Shader inputs unavailable");
            }
            return { gl, program, buffer, position, resolution, time };
        } finally {
            gl.deleteShader(vertex);
            if (fragment) gl.deleteShader(fragment);
        }
    }

    function sizeCanvas() {
        const bounds = canvas.getBoundingClientRect();
        if (bounds.width < 1 || bounds.height < 1) return false;
        const scale = Math.min(window.devicePixelRatio || 1, 1.5);
        const width = Math.min(1024, Math.max(1, Math.round(bounds.width * scale)));
        const height = Math.min(1024, Math.max(1, Math.round(bounds.height * scale)));
        if (canvas.width !== width) canvas.width = width;
        if (canvas.height !== height) canvas.height = height;
        return true;
    }

    function draw(checkError) {
        if (!renderer || !sizeCanvas()) return false;
        const { gl, program, buffer, position, resolution, time } = renderer;
        gl.viewport(0, 0, canvas.width, canvas.height);
        gl.useProgram(program);
        gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
        gl.enableVertexAttribArray(position);
        gl.vertexAttribPointer(position, 2, gl.FLOAT, false, 0, 0);
        gl.uniform2f(resolution, canvas.width, canvas.height);
        gl.uniform1f(time, elapsed);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
        const angle = (elapsed * 2) % 360;
        ring.setAttribute("transform", "rotate(" + angle.toFixed(3) + " 400 400)");
        counterRing.setAttribute("transform", "rotate(" + (-angle).toFixed(3) + " 400 400)");
        return !checkError || gl.getError() === gl.NO_ERROR;
    }

    function mayAnimate() {
        return renderer && !userPaused && inView && !document.hidden && !pageHidden && !motion.matches && !(connection && connection.saveData);
    }

    function tick(now) {
        frame = 0;
        if (!mayAnimate()) return;
        if (lastDraw && now - lastDraw < 1000 / 30) {
            frame = window.requestAnimationFrame(tick);
            return;
        }
        if (lastTick) elapsed += Math.min((now - lastTick) / 1000, 0.1);
        lastTick = now;
        lastDraw = now;
        try {
            if (!draw()) {
                showStatic();
                return;
            }
        } catch (_error) {
            showStatic();
            return;
        }
        frame = window.requestAnimationFrame(tick);
    }

    function updatePlayback() {
        if (!renderer) return;
        setButtonLabel();
        if (mayAnimate()) {
            root.dataset.gateState = "running";
            if (!frame) frame = window.requestAnimationFrame(tick);
        } else {
            root.dataset.gateState = "paused";
            cancelFrame();
        }
    }

    async function loadShader(url, signal) {
        const response = await fetch(url, { credentials: "same-origin", signal });
        if (!response.ok) throw new Error("Shader request failed");
        return response.text();
    }

    async function start() {
        if (motion.matches || (connection && connection.saveData) || contextLost || pageHidden) {
            showStatic();
            return;
        }
        if (!shaderUrls[0] || !shaderUrls[1]) {
            showStatic();
            return;
        }
        const current = ++generation;
        if (pendingFetch) pendingFetch.abort();
        releaseRenderer();
        canvas.hidden = true;
        fallback.toggleAttribute("hidden", false);
        toggle.hidden = true;
        root.dataset.gateState = "loading";
        let gl;
        try {
            gl = canvas.getContext("webgl", { alpha: true, antialias: false, depth: false, stencil: false, powerPreference: "low-power" });
            if (!gl) throw new Error("WebGL unavailable");
            const controller = new AbortController();
            pendingFetch = controller;
            const [vertexSource, fragmentSource] = await Promise.all(shaderUrls.map((url) => loadShader(url, controller.signal)));
            if (current !== generation || pageHidden || contextLost) return;
            pendingFetch = null;
            renderer = makeRenderer(gl, vertexSource, fragmentSource);
            canvas.hidden = false;
            canvas.style.visibility = "hidden";
            if (!draw(true)) throw new Error("First gate frame failed");
            canvas.style.visibility = "";
            fallback.toggleAttribute("hidden", true);
            toggle.hidden = false;
            updatePlayback();
        } catch (_error) {
            if (current === generation) showStatic();
        }
    }

    toggle.addEventListener("click", function () {
        if (!renderer) return;
        userPaused = !userPaused;
        updatePlayback();
    });
    canvas.addEventListener("webglcontextlost", function (event) {
        event.preventDefault();
        contextLost = true;
        showStatic();
    });
    canvas.addEventListener("webglcontextrestored", function () {
        contextLost = false;
        start();
    });
    document.addEventListener("visibilitychange", updatePlayback);
    window.addEventListener("pagehide", function () {
        pageHidden = true;
        showStatic();
    });
    window.addEventListener("pageshow", function () {
        if (!pageHidden) return;
        pageHidden = false;
        start();
    });
    window.addEventListener("resize", function () {
        if (!renderer) return;
        try {
            if (!draw()) showStatic();
        } catch (_error) {
            showStatic();
        }
    });
    const onMotionChange = function () {
        if (motion.matches) showStatic();
        else start();
    };
    if (motion.addEventListener) motion.addEventListener("change", onMotionChange);
    else motion.addListener(onMotionChange);
    if (connection && connection.addEventListener) {
        connection.addEventListener("change", function () {
            if (connection.saveData) showStatic();
            else start();
        });
    }
    if (window.IntersectionObserver) {
        const observer = new IntersectionObserver(function (entries) {
            inView = entries[0].isIntersecting;
            updatePlayback();
        }, { threshold: 0.01 });
        observer.observe(root);
    }

    resetRings();
    setButtonLabel();
    start();
})();
