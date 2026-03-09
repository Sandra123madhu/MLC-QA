async function uploadFile() {
    const fileInput = document.getElementById('dicomFile');
    const statusDiv = document.getElementById('results');
    
    if (fileInput.files.length === 0) {
        alert("Please select a .dcm file first!");
        return;
    }

    const formData = new FormData();
    formData.append("file", fileInput.files[0]);

    statusDiv.innerHTML = "<p>Sending to physics engine... please wait.</p>";

    try {
        const backendUrl = "https://mlc-qa-1.onrender.com/analyze"; 
        
        const response = await fetch(backendUrl, {
            method: 'POST',
            body: formData
        });
        
        // CHECK 1: Did the server return an error page instead of data?
        if (!response.ok) {
            const errorText = await response.text();
            throw new Error(`Server returned Status ${response.status}. Details: ${errorText}`);
        }

        // CHECK 2: Try to parse the JSON
        const data = await response.json();
        
        if (data.status === "Success") {
            const resultColor = data.passed ? 'green' : 'red';
            const resultText = data.passed ? 'PASS' : 'FAIL';
            
            statusDiv.innerHTML = `
                <h2 style="color: ${resultColor}">Analysis Result: ${resultText}</h2>
                <pre style="white-space: pre-wrap;">${data.analysis_summary}</pre>
            `;
        } else {
            statusDiv.innerHTML = `<p style="color: red;">Analysis Error: ${data.message}</p>`;
        }
    } catch (error) {
        statusDiv.innerHTML = `<p style="color: red;">Connection or Server Error. Check the console for details.</p>`;
        console.error("Exact Error:", error);
    }
}
