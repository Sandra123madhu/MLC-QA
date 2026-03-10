const BACKEND_URL = "https://mlc-qa.onrender.com";

// Ping the backend on page load to wake it up
window.addEventListener('load', () => {
    fetch(`${BACKEND_URL}/`)
        .then(() => console.log("Backend is awake."))
        .catch(() => console.warn("Backend may be waking up..."));
});

async function uploadFile() {
    const fileInput = document.getElementById('dicomFile');
    const statusDiv = document.getElementById('results');

    if (fileInput.files.length === 0) {
        alert("Please select a .dcm file first!");
        return;
    }

    const formData = new FormData();
    formData.append("file", fileInput.files[0]);

    statusDiv.innerHTML = `<p>⏳ Uploading file to physics engine...</p>`;

    try {
        // STEP 1: Submit the file — backend returns a job_id immediately
        const response = await fetch(`${BACKEND_URL}/analyze`, {
            method: 'POST',
            body: formData
        });

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

        // STEP 2: Got a job_id — start polling for the result
        const jobId = data.job_id;
        statusDiv.innerHTML = `<p>🔬 Analysis running... <span id="dots">.</span></p>`;

        let dotCount = 0;
        const dotAnim = setInterval(() => {
            dotCount = (dotCount + 1) % 4;
            const dotsEl = document.getElementById('dots');
            if (dotsEl) dotsEl.textContent = '.'.repeat(dotCount + 1);
        }, 500);

        // STEP 3: Poll /result/{job_id} every 3 seconds
        let attempts = 0;
        const maxAttempts = 40; // 40 × 3s = 2 minutes max wait

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
                    setTimeout(poll, 3000);
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

        setTimeout(poll, 3000);

    } catch (error) {
        statusDiv.innerHTML = `
            <p style="color: red;">❌ Could not reach the server.</p>
            <p style="color: #888; font-size: 0.85em;">Please wait 30–60 seconds and try again.</p>`;
        console.error("Fetch Error:", error);
    }
}
