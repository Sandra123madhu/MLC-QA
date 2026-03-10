from fastapi import FastAPI, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pylinac import PicketFence
import os
import shutil
import tempfile
import uuid
import asyncio

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store for job results
jobs = {}

@app.get("/")
def home():
    return {"status": "MLC QA Backend is Live and listening."}


def run_analysis(job_id: str, temp_path: str):
    """Runs Pylinac analysis in the background and stores result."""
    try:
        pf = PicketFence(temp_path)
        pf.analyze(tolerance=0.5, action_tolerance=0.25)
        results_text = pf.results()

        jobs[job_id] = {
            "status": "Success",
            "passed": pf.passed,
            "analysis_summary": results_text
        }
    except Exception as e:
        jobs[job_id] = {
            "status": "Error",
            "message": f"Python Engine Error: {str(e)}"
        }
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


@app.post("/analyze")
async def analyze_mlc(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    """Accepts the file, starts background analysis, returns a job_id immediately."""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".dcm") as temp_file:
            shutil.copyfileobj(file.file, temp_file)
            temp_path = temp_file.name

        job_id = str(uuid.uuid4())
        jobs[job_id] = {"status": "Processing"}

        background_tasks.add_task(run_analysis, job_id, temp_path)

        return {"status": "Processing", "job_id": job_id}

    except Exception as e:
        return {"status": "Error", "message": f"Upload Error: {str(e)}"}


@app.get("/result/{job_id}")
def get_result(job_id: str):
    """Frontend polls this endpoint to check if analysis is done."""
    if job_id not in jobs:
        return {"status": "Error", "message": "Job ID not found."}
    return jobs[job_id]
