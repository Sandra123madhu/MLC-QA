const BACKEND_URL = "https://mlc-qa-1.onrender.com";

// Ping the backend on page load to wake it up (Render free tier spins down)
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

    statusDiv.innerHTML = `
        <p>⏳ Sending to physics engine... please wait.</p>
        <p style="color: #888; font-size: 0.85em;">
            Note: If the server was idle, it may take up to 60 seconds to wake up on first request.
        </p>`;

    try {
        const response = await fetch(`${BACKEND_URL}/analyze`, {
            method: 'POST',
            body: formData
        });

        // Get raw text first — avoids the "Unexpected end of JSON" crash
        const rawText = await response.text();

        if (!rawText || rawText.trim() === "") {
            statusDiv.innerHTML = `
                <p style="color: orange;">⚠️ The server returned an empty response.</p>
                <p style="color: #888; font-size: 0.85em;">
                    The backend on Render may have timed out while waking up. 
                    Please wait 30–60 seconds and try again.
                </p>`;
            return;
        }

        if (!response.ok) {
            statusDiv.innerHTML = `
                <p style="color: red;">❌ Server Error (Status ${response.status})</p>
                <pre style="white-space: pre-wrap; font-size: 0.85em;">${rawText}</pre>`;
            return;
        }

        // Now safely parse JSON
        let data;
        try {
            data = JSON.parse(rawText);
        } catch (parseErr) {
            statusDiv.innerHTML = `
                <p style="color: red;">❌ Could not parse server response as JSON.</p>
                <pre style="white-space: pre-wrap; font-size: 0.85em;">${rawText}</pre>`;
            return;
        }

        if (data.status === "Success") {
            const resultColor = data.passed ? 'green' : 'red';
            const resultIcon  = data.passed ? '✅' : '❌';
            const resultText  = data.passed ? 'PASS' : 'FAIL';

            statusDiv.innerHTML = `
                <h2 style="color: ${resultColor}">${resultIcon} Analysis Result: ${resultText}</h2>
                <pre style="white-space: pre-wrap;">${data.analysis_summary}</pre>
            `;
        } else {
            statusDiv.innerHTML = `
                <p style="color: red;">❌ Analysis Error:</p>
                <pre style="white-space: pre-wrap;">${data.message}</pre>`;
        }

    } catch (error) {
        statusDiv.innerHTML = `
            <p style="color: red;">❌ Could not reach the server.</p>
            <p style="color: #888; font-size: 0.85em;">
                This usually means the backend is still waking up (Render free tier). 
                Please wait 30–60 seconds and try again.
            </p>`;
        console.error("Fetch Error:", error);
    }
}
