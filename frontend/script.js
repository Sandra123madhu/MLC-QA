async function uploadFile() {
    const fileInput = document.getElementById('dicomFile');
    const statusDiv = document.getElementById('results');
    
    if (fileInput.files.length === 0) {
        alert("Please select a DICOM file!");
        return;
    }

    const formData = new FormData();
    formData.append("file", fileInput.files[0]);

    statusDiv.innerHTML = "Analyzing MLC leaves... please wait.";

    try {
        // REPLACE with your actual Render Backend URL
        const response = await fetch("https://mlc-qa.onrender.com", {
            method: 'POST',
            body: formData
        });
        
        const data = await response.json();
        
        if (data.status === "Success") {
            statusDiv.innerHTML = `
                <h2 style="color: ${data.passed ? 'green' : 'red'}">
                    RESULT: ${data.passed ? 'PASS' : 'FAIL'}
                </h2>
                <pre>${data.analysis_summary}</pre>`;
        } else {
            statusDiv.innerHTML = "Analysis Error: " + data.message;
        }
    } catch (error) {
        statusDiv.innerHTML = "Could not connect to the backend.";
    }
}
