from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pylinac import PicketFence
import os
import shutil

app = FastAPI()

# Allow frontend to talk to backend
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
    temp_path = f"temp_{file.filename}"
    
    # Save the uploaded file temporarily
    with open(temp_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        # Run the Pylinac Analysis
        pf = PicketFence(temp_path)
        pf.analyze(tolerance=0.5, action_tolerance=0.25)
        
        return {
            "status": "Success",
            "passed": pf.passed,
            "analysis_summary": pf.results()
        }
    except Exception as e:
        return {"status": "Error", "message": str(e)}
    finally:
        # Clean up the server space
        if os.path.exists(temp_path):
            os.remove(temp_path)
