from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pylinac import PicketFence
import os
import shutil
import tempfile

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    return {"status": "MLC QA Backend is Live and listening."}

@app.post("/analyze")
async def analyze_mlc(file: UploadFile = File(...)):
    # Safely create a temporary file in the cloud environment
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".dcm") as temp_file:
            shutil.copyfileobj(file.file, temp_file)
            temp_path = temp_file.name

        # Run the Pylinac Analysis
        pf = PicketFence(temp_path)
        pf.analyze(tolerance=0.5, action_tolerance=0.25)
        
        results_text = pf.results()
        
        # Clean up
        os.remove(temp_path)

        return {
            "status": "Success",
            "passed": pf.passed,
            "analysis_summary": results_text
        }
    except Exception as e:
        # If it fails, clean up and send the exact error back
        if 'temp_path' in locals() and os.path.exists(temp_path):
            os.remove(temp_path)
        return {"status": "Error", "message": f"Python Engine Error: {str(e)}"}
