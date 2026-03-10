from fastapi import FastAPI, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pylinac import PicketFence
import os
import shutil
import tempfile
import uuid

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory store for job results
# FIX: Added job cleanup to prevent memory growing unboundedly on Render's free tier
jobs = {}
MAX_JOBS = 50  # Keep only the last 50 jobs in memory

def cleanup_old_jobs():
    """Remove oldest jobs if we exceed MAX_JOBS to prevent memory issues on free tier."""
    if len(jobs) > MAX_JOBS:
        oldest_keys = list(jobs.keys())[:len(jobs) - MAX_JOBS]
        for k in oldest_keys:
            del jobs[k]


@app.get("/")
@app.head("/")
def home():
    """Health check endpoint — also used by frontend and Render's health checker.
    Must support both GET and HEAD methods to prevent Render redeploy loops."""
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
        # FIX: Always clean up the temp file even if analysis crashes
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        cleanup_old_jobs()


@app.post("/analyze")
async def analyze_mlc(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    """Accepts the file, starts background analysis, returns a job_id immediately."""

    # FIX: Validate file type on the backend too (defence in depth)
    if not file.filename.lower().endswith(".dcm"):
        return {"status": "Error", "message": "Only DICOM (.dcm) files are supported."}

    try:
        # FIX: Use a named temp file in /tmp with proper suffix
        suffix = ".dcm"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir="/tmp") as temp_file:
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
