const BACKEND_URL = "https://mlc-qa.onrender.com";

// ── Auth guard ────────────────────────────────────────────────────────────────
(function sanitizeToken() {
    const t = localStorage.getItem("mlcqa_token");
    if (!t || t === "undefined" || t === "null" || t.trim() === "") {
        localStorage.removeItem("mlcqa_token");
        localStorage.removeItem("mlcqa_name");
    }
})();

const isAuthPage = window.location.pathname.includes("login.html") ||
                   window.location.pathname.includes("signup.html") ||
                   window.location.pathname.includes("index.html") ||
                   window.location.pathname.endsWith("/");

if (!localStorage.getItem("mlcqa_token") && !isAuthPage) {
    window.location.href = "login.html";
}

function authHeaders() {
    return {
        "Authorization": `Bearer ${localStorage.getItem("mlcqa_token")}`,
        "Content-Type": "application/json"
    };
}

function authHeadersOnly() {
    return {
        "Authorization": `Bearer ${localStorage.getItem("mlcqa_token")}`
    };
}

function logout() {
    localStorage.removeItem("mlcqa_token");
    localStorage.removeItem("mlcqa_name");
    window.location.href = "login.html";
}

// ── Keep-alive ping every 14 min to prevent Render cold starts ────────────────
if (!isAuthPage && localStorage.getItem("mlcqa_token")) {
    setInterval(() => {
        fetch(`${BACKEND_URL}/`).catch(() => {});
    }, 14 * 60 * 1000);

    const storedName = localStorage.getItem("mlcqa_name");
    if (!storedName || storedName === "undefined" || storedName === "null" || storedName.trim() === "") {
        fetch(`${BACKEND_URL}/me`, {
            headers: { "Authorization": `Bearer ${localStorage.getItem("mlcqa_token")}` }
        })
        .then(r => r.ok ? r.json() : null)
        .then(data => {
            if (data && data.name) {
                localStorage.setItem("mlcqa_name", data.name);
                const nameEl = document.getElementById("sidebarName");
                const avatarEl = document.getElementById("avatarInitial");
                if (nameEl) nameEl.textContent = data.name;
                if (avatarEl) avatarEl.textContent = data.name.charAt(0).toUpperCase();
            }
        })
        .catch(() => {});
    }
}


// ═══════════════════════════════════════════════════════════════════════════════
//  CancelManager — shared cancellation state for all analysis pages
//
//  Usage:
//    CancelManager.start({ cancelBtnId?, analyzeBtnId?, analyzeLabel?, resultsId? })
//    CancelManager.setJobId(jobId)        — call once the server returns a job_id
//    CancelManager.cancel()              — user clicked Cancel
//    CancelManager.finish()              — analysis completed (success or error)
//    CancelManager.isCancelled()         — true if cancel was requested
//    CancelManager.getSignal()           — AbortSignal for fetch calls
// ═══════════════════════════════════════════════════════════════════════════════
const CancelManager = (function () {
    let _cancelled  = false;
    let _controller = null;
    let _jobId      = null;
    let _cancelBtnId  = "cancelBtn";
    let _analyzeBtnId = "analyzeBtn";
    let _analyzeLabel = "Run Analysis";
    let _resultsId    = "resultsBody";

    function _showCancelBtn(show) {
        const c = document.getElementById(_cancelBtnId);
        if (c) c.style.display = show ? "inline-flex" : "none";
    }

    function _setAnalyzeBtn(disabled, label) {
        const btn = document.getElementById(_analyzeBtnId);
        if (!btn) return;
        if (disabled !== null) btn.disabled = disabled;
        if (label)             btn.textContent = label;
    }

    return {
        start({ cancelBtnId = "cancelBtn", analyzeBtnId = "analyzeBtn",
                analyzeLabel = "Run Analysis", resultsId = "resultsBody" } = {}) {
            _cancelled    = false;
            _controller   = new AbortController();
            _jobId        = null;
            _cancelBtnId  = cancelBtnId;
            _analyzeBtnId = analyzeBtnId;
            _analyzeLabel = analyzeLabel;
            _resultsId    = resultsId;
            _showCancelBtn(true);
        },

        setJobId(jobId) {
            _jobId = jobId || null;
        },

        async cancel() {
            if (_cancelled) return;
            _cancelled = true;          // set flag FIRST — nothing reads false after this

            if (_controller) {
                _controller.abort();
                _controller = null;
            }

            if (_jobId) {
                const jid = _jobId;
                _jobId = null;
                try {
                    await fetch(`${BACKEND_URL}/job/${jid}/cancel`, {
                        method: "POST",
                        headers: authHeadersOnly()
                    });
                } catch (_) {}
            }

            _showCancelBtn(false);
            _setAnalyzeBtn(false, _analyzeLabel);
            const res = document.getElementById(_resultsId);
            if (res) res.innerHTML = "<p>⚠️ Analysis cancelled.</p>";
        },

        finish() {
            _cancelled  = false;
            _controller = null;
            _jobId      = null;
            _showCancelBtn(false);
        },

        reset() {
            _cancelled  = false;
            _controller = null;
            _jobId      = null;
            _showCancelBtn(false);
        },

        isCancelled() { return _cancelled; },
        getSignal()   { return _controller ? _controller.signal : undefined; }
    };
})();


// ═══════════════════════════════════════════════════════════════════════════════
//  AnalysisHelpers — shared utility used by all analysis pages
// ═══════════════════════════════════════════════════════════════════════════════
const AnalysisHelpers = {

    checkServer(elementId, btnId) {
        const el  = document.getElementById(elementId);
        const btn = btnId ? document.getElementById(btnId) : null;
        if (!el) return;

        el.innerHTML = `<div class="status-dot-sm dot-warn"></div><span>Checking server status...</span>`;

        async function ping() {
            try {
                const res = await fetch(`${BACKEND_URL}/`, { signal: AbortSignal.timeout(5000) });
                if (res.ok) {
                    el.innerHTML = `<div class="status-dot-sm dot-ok"></div><span>Server online</span>`;
                    if (btn) btn.disabled = false;
                    return true;
                }
            } catch (e) {}
            return false;
        }

        ping().then(online => {
            if (online) return;
            let seconds = 60;
            if (btn) btn.disabled = false;
            el.innerHTML = `<div class="status-dot-sm dot-warn" style="animation:blink 1s infinite"></div>
                            <span>Server waking up... (~${seconds}s)</span>`;
            const interval = setInterval(async () => {
                seconds = Math.max(seconds - 2, 5);
                el.innerHTML = `<div class="status-dot-sm dot-warn" style="animation:blink 1s infinite"></div>
                                <span>Server waking up... (~${seconds}s)</span>`;
                if (await ping()) clearInterval(interval);
            }, 2000);
        });
    },

    async waitForServer() {
        for (let i = 0; i < 15; i++) {
            try {
                const res = await fetch(`${BACKEND_URL}/`, { signal: AbortSignal.timeout(5000) });
                if (res.ok) return true;
            } catch (e) {}
            await new Promise(r => setTimeout(r, 2000));
        }
        return false;
    },

    async makeApiCall(url, options = {}, timeoutMs = 30000) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);

        // Chain an external signal (e.g. from CancelManager) to this controller
        const externalSignal = options.signal;
        if (externalSignal) {
            if (externalSignal.aborted) {
                clearTimeout(timer);
                const err = new DOMException("Aborted", "AbortError");
                throw err;
            }
            externalSignal.addEventListener("abort", () => controller.abort(), { once: true });
        }

        try {
            const res = await fetch(url, { ...options, signal: controller.signal });
            clearTimeout(timer);

            if (res.status === 401) {
                localStorage.removeItem("mlcqa_token");
                localStorage.removeItem("mlcqa_name");
                window.location.href = "login.html?reason=session_expired";
                throw new Error("Session expired. Redirecting to login...");
            }

            const text = await res.text();
            if (!text || text.trim() === "") {
                throw new Error("Server returned an empty response. It may still be starting up — please try again.");
            }

            let data;
            try {
                data = JSON.parse(text);
            } catch {
                throw new Error(`Could not parse server response: ${text.slice(0, 200)}`);
            }

            if (!res.ok) {
                throw new Error(data.message || data.detail || `Server error ${res.status}`);
            }

            return data;

        } catch (err) {
            clearTimeout(timer);
            if (err.name === "AbortError") {
                throw new Error("Request timed out or was cancelled.");
            }
            throw err;
        }
    },

    // ── pollForResult ─────────────────────────────────────────────────────────
    // Returns a Promise that:
    //   • resolves when analysis succeeds (after calling onSuccess) or is cancelled
    //   • rejects on server error or timeout (after calling onError if provided)
    //
    // This fixes the original bug where poll() was called via setTimeout so
    // errors thrown inside it (or inside onError callbacks) were silently lost
    // and never propagated to the caller's try/catch.
    pollForResult(jobId, onSuccess, onError, isCancelled) {
        return new Promise((resolve, reject) => {
            const maxAttempts = 60;
            let attempts = 0;
            const getDelay = (n) => n < 5 ? 2000 : n < 15 ? 3000 : 5000;

            let dotCount = 0;
            const dotAnim = setInterval(() => {
                dotCount = (dotCount + 1) % 4;
                const el = document.getElementById("dots");
                if (el) el.textContent = ".".repeat(dotCount + 1);
            }, 500);

            const poll = async () => {
                if (isCancelled && isCancelled()) {
                    clearInterval(dotAnim);
                    resolve();
                    return;
                }

                attempts++;
                if (attempts > maxAttempts) {
                    clearInterval(dotAnim);
                    const msg = "Analysis is taking longer than expected — please try again.";
                    if (onError) { try { onError(msg); } catch (_) {} }
                    reject(new Error(msg));
                    return;
                }

                try {
                    const result = await this.makeApiCall(
                        `${BACKEND_URL}/result/${jobId}`,
                        { headers: authHeadersOnly() }
                    );

                    if (isCancelled && isCancelled()) {
                        clearInterval(dotAnim);
                        resolve();
                        return;
                    }

                    if (result.status === "Processing") {
                        setTimeout(poll, getDelay(attempts));
                    } else if (result.status === "Cancelled") {
                        clearInterval(dotAnim);
                        resolve();
                    } else if (result.status === "Success") {
                        clearInterval(dotAnim);
                        try { onSuccess(result); } catch (_) {}
                        resolve();
                    } else {
                        clearInterval(dotAnim);
                        const msg = result.message || "Analysis failed on the server.";
                        if (onError) { try { onError(msg); } catch (_) {} }
                        reject(new Error(msg));
                    }
                } catch (err) {
                    clearInterval(dotAnim);
                    if (isCancelled && isCancelled()) {
                        resolve();
                        return;
                    }
                    const msg = err.message || "Lost connection while waiting for results.";
                    if (onError) { try { onError(msg); } catch (_) {} }
                    reject(err);
                }
            };

            setTimeout(poll, getDelay(0));
        });
    },

    validateDicomFile(file) {
        if (!file || !file.name.toLowerCase().endsWith(".dcm")) {
            throw new Error("Only DICOM (.dcm) files are supported. Please select a valid file.");
        }
        return true;
    }
};


// ── Auto server-check on any page that has #serverStatus ─────────────────────
window.addEventListener("DOMContentLoaded", () => {
    if (document.getElementById("serverStatus")) {
        const btnId = document.getElementById("loginBtn")   ? "loginBtn"
                    : document.getElementById("analyzeBtn") ? "analyzeBtn"
                    : null;
        AnalysisHelpers.checkServer("serverStatus", btnId);
    }
});


// ═══════════════════════════════════════════════════════════════════════════════
//  HistoryManager — records completed analyses to localStorage
//
//  Schema per entry:
//    { id, testType, filename, passed, summary, imageUrl, timestamp }
//
//  Usage:
//    HistoryManager.record({ testType, filename, passed, summary, imageUrl })
//    HistoryManager.getAll()   — returns entries newest-first, purged >30 days
//    HistoryManager.clear()
// ═══════════════════════════════════════════════════════════════════════════════
const HistoryManager = (function () {
    const STORAGE_KEY  = "mlcqa_history";
    const MAX_ENTRIES  = 100;
    const MAX_AGE_MS   = 30 * 24 * 60 * 60 * 1000;

    function _load() {
        try {
            const raw = localStorage.getItem(STORAGE_KEY);
            if (!raw) return [];
            const entries = JSON.parse(raw);
            const cutoff  = Date.now() - MAX_AGE_MS;
            return entries.filter(e => new Date(e.timestamp).getTime() > cutoff);
        } catch { return []; }
    }

    function _save(entries) {
        try { localStorage.setItem(STORAGE_KEY, JSON.stringify(entries)); } catch {}
    }

    return {
        record({ testType, filename, passed, summary, imageUrl }) {
            const entries = _load();
            entries.unshift({
                id:        Date.now().toString(36) + Math.random().toString(36).slice(2),
                testType:  testType  || "Unknown",
                filename:  filename  || "—",
                passed:    !!passed,
                summary:   summary   || "",
                imageUrl:  imageUrl  || null,
                timestamp: new Date().toISOString()
            });
            _save(entries.slice(0, MAX_ENTRIES));
        },

        getAll() { return _load(); },

        clear() { localStorage.removeItem(STORAGE_KEY); }
    };
})();


// ── Shared UI helpers ─────────────────────────────────────────────────────────
function togglePassword(inputId, iconEl) {
    const input = document.getElementById(inputId);
    if (input.type === "password") {
        input.type = "text";
        iconEl.textContent = "👁️";
    } else {
        input.type = "password";
        iconEl.textContent = "🙈";
    }
}
