const BACKEND_URL = "https://mlc-qa.onrender.com";

// ── Auth guard ────────────────────────────────────────────────────────────────
// Sanitize: if the stored token is literally "undefined" or empty, wipe it now
// so we don't send a garbage Authorization header on every request.
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

// ── Populate sidebar name / avatar on every protected page ───────────────────
(function populateSidebar() {
    if (isAuthPage) return;
    const storedName = localStorage.getItem("mlcqa_name") || "";
    const nameEl   = document.getElementById("sidebarName");
    const avatarEl = document.getElementById("avatarInitial");
    if (nameEl)   nameEl.textContent   = storedName || "User";
    if (avatarEl) avatarEl.textContent = storedName ? storedName.charAt(0).toUpperCase() : "U";
})();

// Returns headers for JSON API calls (NOT for FormData uploads)
function authHeaders() {
    return {
        "Authorization": `Bearer ${localStorage.getItem("mlcqa_token")}`,
        "Content-Type": "application/json"
    };
}

// Returns just the Authorization header — use this for FormData/file uploads
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
// Only run for authenticated users — no need to ping on the landing/auth pages
if (!isAuthPage && localStorage.getItem("mlcqa_token")) {
    setInterval(() => {
        fetch(`${BACKEND_URL}/`).catch(() => {});
    }, 14 * 60 * 1000);
}


// ═══════════════════════════════════════════════════════════════════════════════
//  AnalysisHelpers — shared utility used by all analysis pages
// ═══════════════════════════════════════════════════════════════════════════════
const AnalysisHelpers = {

    // ── checkServer(elementId, btnId?) ────────────────────────────────────────
    // Polls the backend and updates a status indicator element.
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

    // ── waitForServer() ───────────────────────────────────────────────────────
    // Resolves true if backend responds within ~30 s, false otherwise.
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

    // ── makeApiCall(url, options, timeoutMs?) ─────────────────────────────────
    // fetch() wrapper with:
    //   • configurable timeout (default 30 s)
    //   • auto-logout on 401 (invalid/expired token)
    //   • human-readable error messages
    //   • always returns parsed JSON
    async makeApiCall(url, options = {}, timeoutMs = 30000) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);

        try {
            const res = await fetch(url, { ...options, signal: controller.signal });
            clearTimeout(timer);

            // 401 → token is bad or expired → kick to login
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
                throw new Error("Request timed out. The server may be under load — please try again.");
            }
            throw err;
        }
    },

    // ── pollForResult(jobId, onSuccess, onError?) ─────────────────────────────
    // Polls GET /result/:jobId with exponential back-off until complete.
    async pollForResult(jobId, onSuccess, onError) {
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
            attempts++;
            if (attempts > maxAttempts) {
                clearInterval(dotAnim);
                const msg = "Analysis is taking longer than expected — please try again.";
                if (onError) onError(msg); else console.error(msg);
                return;
            }

            try {
                const result = await this.makeApiCall(
                    `${BACKEND_URL}/result/${jobId}`,
                    { headers: authHeadersOnly() }
                );

                if (result.status === "Processing") {
                    setTimeout(poll, getDelay(attempts));
                } else if (result.status === "Success") {
                    clearInterval(dotAnim);
                    onSuccess(result);
                } else {
                    clearInterval(dotAnim);
                    const msg = result.message || "Analysis failed on the server.";
                    if (onError) onError(msg); else console.error(msg);
                }
            } catch (err) {
                clearInterval(dotAnim);
                if (onError) onError(err.message || "Lost connection while waiting for results.");
                else console.error(err);
            }
        };

        setTimeout(poll, getDelay(0));
    },

    // ── validateDicomFile(file) ───────────────────────────────────────────────
    // Client-side guard — throws if file is not a .dcm file.
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
