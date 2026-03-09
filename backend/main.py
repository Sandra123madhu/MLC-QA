from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pylinac import PicketFence
import os
import shutil

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/analyze")
async def analyze_mlc(file: UploadFile = File(...)):
    # 1. Save the uploaded DICOM file temporarily
    temp_path = f"temp_{file.filename}"
    with open(temp_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        # 2. Run Picket Fence Analysis
        pf = PicketFence(temp_path)
        pf.analyze(tolerance=0.5, action_tolerance=0.25)
        
        # 3. Get results
        results = pf.results()
        passed = pf.passed
        
        return {
            "status": "Success",
            "passed": passed,
            "analysis_summary": results
        }
    except Exception as e:
        return {"status": "Error", "message": str(e)}
    finally:
        # Clean up the file
        if os.path.exists(temp_path):
            os.remove(temp_path)
