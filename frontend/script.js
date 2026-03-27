function getBackendUrl() {
    const params = new URLSearchParams(window.location.search);
    if (params.get('backend') === 'local') return "http://127.0.0.1:10000";
    if (params.get('backend') === 'prod')  return "https://mlc-qa-1.onrender.com";

    const h = window.location.hostname;
    const p = window.location.protocol;

    // Local detection: localhost, 127.0.0.1, empty (file://), or private IP ranges
    const isLocal = h === "localhost" || 
                    h === "127.0.0.1" || 
                    h === "" || 
                    p === "file:" ||
                    h.startsWith("192.168.") || 
                    h.startsWith("10.") || 
                    h.startsWith("172.");

    return isLocal ? "http://127.0.0.1:10000" : "https://mlc-qa-1.onrender.com";
}
const BACKEND_URL = getBackendUrl();
console.log("Using backend:", BACKEND_URL);

// --- Auth: redirect to login if no token found ---
const token = localStorage.getItem("mlcqa_token");
const isAuthPage = window.location.pathname.includes("login.html") || window.location.pathname.includes("signup.html") || window.location.pathname.endsWith("/");

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


// --- Universal Server Status Check ---
async function checkServer(elementId, btnId) {
    const el = document.getElementById(elementId);
    const btn = btnId ? document.getElementById(btnId) : null;
    
    if (!el) return;
    
    el.innerHTML = `<div class="status-dot-sm dot-warn"></div><span>Checking server status...</span>`;
    if (btn) btn.disabled = true;

    async function ping() {
        try {
            const start = Date.now();
            const res = await fetch(`${BACKEND_URL}/`);
            if (res.ok) {
                el.innerHTML = `<div class="status-dot-sm dot-ok"></div><span>Server online</span>`;
                if (btn) btn.disabled = false;
                return true;
            }
        } catch (e) {}
        return false;
    }

    if (await ping()) return;

    // If not immediately online, start "waking up" countdown
    let seconds = 60;
    el.innerHTML = `<div class="status-dot-sm dot-warn" style="animation:blink 1s infinite"></div>
                    <span>Server waking up... (~${seconds}s)</span>`;
    
    const interval = setInterval(async () => {
        seconds -= 2;
        if (seconds <= 0) seconds = 5; // keep it low but non-zero
        
        el.innerHTML = `<div class="status-dot-sm dot-warn" style="animation:blink 1s infinite"></div>
                        <span>Server waking up... (~${seconds}s)</span>`;
        
        if (await ping()) {
            clearInterval(interval);
        }
    }, 2000);
}

// Auto-run check on pages that have the serverStatus element
window.addEventListener('DOMContentLoaded', () => {
    if (document.getElementById('serverStatus')) {
        checkServer('serverStatus', 'analyzeBtn' || 'loginBtn');
    }
});


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

async function uploadFile() {
    const fileInput = document.getElementById('dicomFile');
    const statusDiv = document.getElementById('results');

    if (fileInput.files.length === 0) {
        alert("Please select a .dcm file first!");
        return;
    }

    // FIX 3: Validate file type before uploading
    const fileName = fileInput.files[0].name;
    if (!fileName.toLowerCase().endsWith('.dcm')) {
        statusDiv.innerHTML = `<p style="color: red;">❌ Please upload a valid DICOM (.dcm) file.</p>`;
        return;
    }

    const formData = new FormData();
    formData.append("file", fileInput.files[0]);

    statusDiv.innerHTML = `<p>⏳ Uploading file to physics engine...</p>`;

    try {
        // FIX 4: Upload timeout with AbortController (30s)
        const controller = new AbortController();
        const uploadTimeout = setTimeout(() => controller.abort(), 30000);

        let response;
        try {
            response = await fetch(`${BACKEND_URL}/analyze`, {
                method: 'POST',
                headers: authHeaders(),
                body: formData,
                signal: controller.signal
            });
        } catch (err) {
            clearTimeout(uploadTimeout);
            if (err.name === 'AbortError') {
                statusDiv.innerHTML = `
                    <p style="color: orange;">⚠️ Upload timed out. The server may be starting up.</p>
                    <p style="color: #888; font-size: 0.85em;">Please wait 30 seconds and try again.</p>`;
            } else {
                statusDiv.innerHTML = `
                    <p style="color: red;">❌ Could not reach the server.</p>
                    <p style="color: #888; font-size: 0.85em;">Please wait 30–60 seconds and try again.</p>`;
            }
            return;
        }
        clearTimeout(uploadTimeout);

        const rawText = await response.text();

        if (!rawText || rawText.trim() === "") {
            statusDiv.innerHTML = `
                <p style="color: orange;">⚠️ Server returned empty response. It may still be waking up.</p>
                <p style="color: #888; font-size: 0.85em;">Please wait 30 seconds and try again.</p>`;
            return;
        }

        let data;
        try {
            data = JSON.parse(rawText);
        } catch {
            statusDiv.innerHTML = `
                <p style="color: red;">❌ Could not parse server response.</p>
                <pre style="white-space: pre-wrap; font-size: 0.85em;">${rawText}</pre>`;
            return;
        }

        if (data.status === "Error") {
            statusDiv.innerHTML = `<p style="color: red;">❌ Upload Error: ${data.message}</p>`;
            return;
        }

        const jobId = data.job_id;
        statusDiv.innerHTML = `<p>🔬 Analysis running... <span id="dots">.</span></p>`;

        let dotCount = 0;
        const dotAnim = setInterval(() => {
            dotCount = (dotCount + 1) % 4;
            const dotsEl = document.getElementById('dots');
            if (dotsEl) dotsEl.textContent = '.'.repeat(dotCount + 1);
        }, 500);

        // FIX 5: Adaptive polling — fast first, then slows down
        let attempts = 0;
        const maxAttempts = 60;

        const getDelay = (attempt) => {
            if (attempt < 5)  return 2000;
            if (attempt < 15) return 3000;
            return 5000;
        };

        const poll = async () => {
            attempts++;

            if (attempts > maxAttempts) {
                clearInterval(dotAnim);
                statusDiv.innerHTML = `
                    <p style="color: orange;">⚠️ Analysis is taking longer than expected.</p>
                    <p style="color: #888; font-size: 0.85em;">The server may be under load. Please try again in a moment.</p>`;
                return;
            }

            try {
                const resultRes = await fetch(`${BACKEND_URL}/result/${jobId}`, {
                    headers: authHeaders()
                });
                const result = await resultRes.json();

                if (result.status === "Processing") {
                    setTimeout(poll, getDelay(attempts));
                } else if (result.status === "Success") {
                    clearInterval(dotAnim);
                    const resultColor = result.passed ? 'green' : 'red';
                    const resultIcon  = result.passed ? '✅' : '❌';
                    const resultText  = result.passed ? 'PASS' : 'FAIL';

                    statusDiv.innerHTML = `
                        <h2 style="color: ${resultColor}">${resultIcon} Analysis Result: ${resultText}</h2>
                        <pre style="white-space: pre-wrap;">${result.analysis_summary}</pre>`;
                } else {
                    clearInterval(dotAnim);
                    statusDiv.innerHTML = `
                        <p style="color: red;">❌ Analysis Error:</p>
                        <pre style="white-space: pre-wrap;">${result.message}</pre>`;
                }
            } catch (err) {
                clearInterval(dotAnim);
                statusDiv.innerHTML = `<p style="color: red;">❌ Lost connection while polling for results.</p>`;
                console.error("Poll error:", err);
            }
        };

        setTimeout(poll, getDelay(0));

    } catch (error) {
        statusDiv.innerHTML = `
            <p style="color: red;">❌ Could not reach the server.</p>
            <p style="color: #888; font-size: 0.85em;">Please wait 30–60 seconds and try again.</p>`;
        console.error("Fetch Error:", error);
    }
}
