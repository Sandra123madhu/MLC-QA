const BACKEND_URL = "https://mlc-qa.onrender.com";

// --- FIX 1: Keep-alive ping every 10 minutes to prevent Render cold starts ---
setInterval(() => {
    fetch(`${BACKEND_URL}/`).catch(() => {});
}, 10 * 60 * 1000);


// --- FIX 2: Wake-up check on page load with visible user feedback ---
window.addEventListener('load', () => {
    const statusDiv = document.getElementById('results');
    const uploadBtn = document.getElementById('uploadBtn');

    if (uploadBtn) uploadBtn.disabled = true;
    if (statusDiv) statusDiv.innerHTML = `<p style="color: #aaa;">⏳ Connecting to analysis server, please wait...</p>`;

    let wakeAttempts = 0;
    const maxWakeAttempts = 20;

    const wakeUp = () => {
        fetch(`${BACKEND_URL}/`)
            .then(res => {
                if (res.ok) {
                    if (uploadBtn) uploadBtn.disabled = false;
                    if (statusDiv) statusDiv.innerHTML = `<p style="color: green;">✅ Server is ready. You may upload your file.</p>`;
                } else {
                    retry();
                }
            })
            .catch(() => retry());
    };

    const retry = () => {
        wakeAttempts++;
        if (wakeAttempts < maxWakeAttempts) {
            if (statusDiv) statusDiv.innerHTML = `<p style="color: #aaa;">⏳ Server is waking up... (${wakeAttempts * 5}s elapsed, please wait up to 90s)</p>`;
            setTimeout(wakeUp, 5000);
        } else {
            if (uploadBtn) uploadBtn.disabled = false;
            if (statusDiv) statusDiv.innerHTML = `<p style="color: orange;">⚠️ Server is slow to respond. You can still try uploading.</p>`;
        }
    };

    wakeUp();
});


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
                const resultRes = await fetch(`${BACKEND_URL}/result/${jobId}`);
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
