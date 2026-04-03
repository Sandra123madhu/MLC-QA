const BACKEND_URL = "https://mlc-qa.onrender.com";

// --- Auth: redirect to login if no token found ---
const token = localStorage.getItem("mlcqa_token");
const isAuthPage = window.location.pathname.includes("login.html") ||
                   window.location.pathname.includes("signup.html") ||
                   window.location.pathname.endsWith("/");

if (!token && !isAuthPage) {
    window.location.href = "login.html";
}

function authHeaders() {
    return {
        "Authorization": `Bearer ${localStorage.getItem("mlcqa_token")}`,
        "Content-Type": "application/json"
    };
}

function logout() {
    localStorage.removeItem("mlcqa_token");
    localStorage.removeItem("mlcqa_name");
    window.location.href = "login.html";
}

// --- Keep-alive ping every 14 minutes to prevent Render cold starts ---
function startKeepAlive() {
    setInterval(() => {
        console.log("Keep-alive ping...");
        fetch(`${BACKEND_URL}/`).catch(() => {});
    }, 14 * 60 * 1000);
}
startKeepAlive();


// ============================================================
//  AnalysisHelpers — shared utility object used by all pages
// ============================================================
const AnalysisHelpers = {

    // checkServer(elementId, btnId?)
    //   Shows a live server-status indicator in the given element.
    //   Polls every 2s until the backend responds.
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
                            <span>Server waking up... (~${seconds}s) — you can still try</span>`;

            const interval = setInterval(async () => {
                seconds = Math.max(seconds - 2, 5);
                el.innerHTML = `<div class="status-dot-sm dot-warn" style="animation:blink 1s infinite"></div>
                                <span>Server waking up... (~${seconds}s) — you can still try</span>`;
                if (await ping()) clearInterval(interval);
            }, 2000);
        });
    },

    // waitForServer()
    //   Awaitable check — resolves true if server responds within ~30s.
    async waitForServer() {
        const maxTries = 15;
        for (let i = 0; i < maxTries; i++) {
            try {
                const res = await fetch(`${BACKEND_URL}/`, { signal: AbortSignal.timeout(5000) });
                if (res.ok) return true;
            } catch (e) {}
            await new Promise(r => setTimeout(r, 2000));
        }
        return false;
    },

    // makeApiCall(url, options, timeoutMs?)
    //   fetch() wrapper with timeout, error handling, and JSON parsing.
    async makeApiCall(url, options = {}, timeoutMs = 30000) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);

        try {
            const res = await fetch(url, { ...options, signal: controller.signal });
            clearTimeout(timer);

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

    // pollForResult(jobId, onSuccess, onError?)
    //   Polls GET /result/:jobId with back-off until done.
    async pollForResult(jobId, onSuccess, onError) {
        const maxAttempts = 60;
        let attempts = 0;

        const getDelay = (n) => {
            if (n < 5)  return 2000;
            if (n < 15) return 3000;
            return 5000;
        };

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
                const msg = "Analysis is taking longer than expected. The server may be under load — please try again.";
                if (onError) onError(msg); else console.error(msg);
                return;
            }

            try {
                const result = await this.makeApiCall(
                    `${BACKEND_URL}/result/${jobId}`,
                    { headers: authHeaders() }
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

    // validateDicomFile(file)
    //   Client-side guard — checks .dcm extension.
    validateDicomFile(file) {
        if (!file || !file.name.toLowerCase().endsWith(".dcm")) {
            throw new Error("Only DICOM (.dcm) files are supported. Please select a valid file.");
        }
        return true;
    }
};


// --- Auto-run server check on any page that has #serverStatus ---
window.addEventListener("DOMContentLoaded", () => {
    if (document.getElementById("serverStatus")) {
        const btnId = document.getElementById("loginBtn")   ? "loginBtn"
                    : document.getElementById("analyzeBtn") ? "analyzeBtn"
                    : null;
        AnalysisHelpers.checkServer("serverStatus", btnId);
    }
});


// --- Shared UI helpers ---
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
