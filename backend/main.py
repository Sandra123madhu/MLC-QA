# =============================================================================
# MLC QA Platform — FastAPI Backend
# =============================================================================
#
# SUPABASE TABLE SETUP — run this SQL in your Supabase SQL editor if not done:
#
#   create table if not exists analyses (
#     id          bigint generated always as identity primary key,
#     email       text not null,
#     test_type   text,
#     filename    text,
#     passed      boolean,
#     summary     text,
#     image_url   text,
#     chart_data  jsonb,
#     job_id      text,
#     created_at  timestamptz default now()
#   );
#
#   -- Disable RLS so the service-role key can read/write freely:
#   alter table analyses disable row level security;
#
#   -- Auto-delete records older than 30 days (optional):
#   -- Use Supabase cron or pg_cron for this.
#
# =============================================================================

import os
import io
import uuid
import tempfile
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import bcrypt
from fastapi import FastAPI, File, UploadFile, HTTPException, Depends, BackgroundTasks, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from pydantic import BaseModel

# ── pylinac ──────────────────────────────────────────────────────────────────
from pylinac import PicketFence, WinstonLutz, Starshot, FieldAnalysis
from pylinac.core.geometry import Point

# ── Environment / config ─────────────────────────────────────────────────────
SUPABASE_URL     = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY     = os.environ.get("SUPABASE_KEY", "")   # service-role key
JWT_SECRET       = os.environ.get("JWT_SECRET", "mlcqa-secret-change-me")
JWT_ALGORITHM    = "HS256"
JWT_EXPIRE_HOURS = 72

# Supabase Storage bucket for plot images
PLOT_BUCKET      = os.environ.get("PLOT_BUCKET", "mlcqa-plots")

# ── In-memory job store (keyed by job_id UUID) ────────────────────────────────
jobs: dict = {}

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="MLC QA API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer(auto_error=False)


# =============================================================================
# Helpers
# =============================================================================

def supabase_headers(use_service_key: bool = True, prefer_return: bool = False) -> dict:
    key = SUPABASE_KEY
    headers = {
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
    }
    if prefer_return:
        headers["Prefer"] = "return=representation"
    return headers


def cleanup():
    """Remove jobs older than 2 hours from the in-memory store."""
    cutoff = time.time() - 7200
    stale  = [k for k, v in jobs.items() if isinstance(v, dict) and v.get("_ts", time.time()) < cutoff]
    for k in stale:
        jobs.pop(k, None)


def create_token(email: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    payload = {"sub": email, "exp": expire}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def verify_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return {"email": payload["sub"]}
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return verify_token(credentials.credentials)


async def upload_plot(filepath: str, storage_name: str) -> str:
    """Upload a plot image to Supabase Storage and return its public URL."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return ""
    try:
        with open(filepath, "rb") as f:
            data = f.read()
        url = f"{SUPABASE_URL}/storage/v1/object/{PLOT_BUCKET}/{storage_name}"
        headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "image/png",
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(url, content=data, headers=headers)
        if r.status_code in (200, 201):
            return f"{SUPABASE_URL}/storage/v1/object/public/{PLOT_BUCKET}/{storage_name}"
        return ""
    except Exception:
        return ""


def save_analysis(email: str, test_type: str, filename: str, passed: bool,
                  summary: str, image_url: str, chart_data: dict, job_id: str):
    """Persist analysis result to Supabase analyses table."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    import json
    payload = {
        "email":      email,
        "test_type":  test_type,
        "filename":   filename,
        "passed":     passed,
        "summary":    summary,
        "image_url":  image_url,
        "chart_data": chart_data,
        "job_id":     job_id,
    }
    url = f"{SUPABASE_URL}/rest/v1/analyses"
    try:
        import requests
        requests.post(url, json=payload, headers=supabase_headers(prefer_return=True), timeout=10)
    except Exception:
        pass


# =============================================================================
# Auth endpoints
# =============================================================================

class SignupRequest(BaseModel):
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/auth/signup")
async def signup(body: SignupRequest):
    hashed = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    url = f"{SUPABASE_URL}/rest/v1/users"
    payload = {"email": body.email, "password_hash": hashed}
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, headers=supabase_headers(prefer_return=True))
    if r.status_code in (200, 201):
        return {"token": create_token(body.email)}
    detail = r.json() if r.content else {}
    if "duplicate" in str(detail).lower() or r.status_code == 409:
        raise HTTPException(400, "Email already registered")
    raise HTTPException(500, f"Signup failed: {detail}")


@app.post("/auth/login")
async def login(body: LoginRequest):
    url = f"{SUPABASE_URL}/rest/v1/users?email=eq.{body.email}&select=email,password_hash"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=supabase_headers())
    rows = r.json()
    if not rows:
        raise HTTPException(401, "Invalid credentials")
    stored_hash = rows[0]["password_hash"]
    if not bcrypt.checkpw(body.password.encode(), stored_hash.encode()):
        raise HTTPException(401, "Invalid credentials")
    return {"token": create_token(body.email)}


# =============================================================================
# History endpoint
# =============================================================================

@app.get("/history")
async def get_history(u=Depends(get_current_user)):
    url = f"{SUPABASE_URL}/rest/v1/analyses?email=eq.{u['email']}&order=created_at.desc&limit=50"
    async with httpx.AsyncClient() as client:
        r = await client.get(url, headers=supabase_headers())
    return r.json()


# =============================================================================
# Analysis helpers (shared by all test endpoints)
# =============================================================================

def _run_in_thread(fn, *args):
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()


@app.get("/job/{job_id}")
async def poll_job(job_id: str, u=Depends(get_current_user)):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


# =============================================================================
# Picket Fence
# =============================================================================

def _run_picket_fence(job_id: str, filepath: str, email: str, filename: str):
    try:
        pf = PicketFence(filepath)
        pf.analyze()
        summary   = pf.results()
        passed    = pf.passed
        plot_path = filepath.replace(".dcm", "_pf.png")
        pf.plot_analyzed_image(filename=plot_path, show=False)

        import asyncio, nest_asyncio
        try:
            nest_asyncio.apply()
            loop = asyncio.get_event_loop()
            image_url = loop.run_until_complete(upload_plot(plot_path, f"pf_{job_id}.png"))
        except Exception:
            image_url = ""

        chart_data = {}
        try:
            rd = pf.results_data()
            chart_data = {
                "max_error_mm":  getattr(rd, "max_error_mm", None),
                "mean_error_mm": getattr(rd, "mean_error_mm", None),
                "passed":        passed,
            }
        except Exception:
            pass

        save_analysis(email, "Picket Fence", filename, passed, summary, image_url, chart_data, job_id)
        jobs[job_id] = {"status": "Success", "passed": passed,
                        "analysis_summary": summary, "image_url": image_url, "chart_data": chart_data}
    except Exception as e:
        jobs[job_id] = {"status": "Error", "message": str(e)}
    finally:
        try: os.remove(filepath)
        except Exception: pass
        cleanup()


@app.post("/analyze/picket-fence")
async def analyze_picket_fence(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    u=Depends(get_current_user),
):
    if not file.filename.lower().endswith(".dcm"):
        raise HTTPException(400, "Only .dcm files are supported")
    job_id   = str(uuid.uuid4())
    filepath = os.path.join(tempfile.gettempdir(), f"pf_{job_id}.dcm")
    contents = await file.read()
    with open(filepath, "wb") as f:
        f.write(contents)
    jobs[job_id] = {"status": "Processing"}
    background_tasks.add_task(_run_picket_fence, job_id, filepath, u["email"], file.filename)
    return {"status": "Queued", "job_id": job_id}


# =============================================================================
# Winston-Lutz
# =============================================================================

def _run_winston_lutz(job_id: str, filepaths: list, email: str, filename: str):
    try:
        wl = WinstonLutz(images=filepaths)
        wl.analyze()
        summary  = wl.results()
        passed   = wl.passed

        plot_path = filepaths[0].replace(".dcm", "_wl.png")
        wl.plot_summary(filename=plot_path, show=False)

        import asyncio, nest_asyncio
        try:
            nest_asyncio.apply()
            loop = asyncio.get_event_loop()
            image_url = loop.run_until_complete(upload_plot(plot_path, f"wl_{job_id}.png"))
        except Exception:
            image_url = ""

        chart_data = {}
        try:
            rd = wl.results_data()
            chart_data = {
                "max_2d_cax_to_bb_mm": getattr(rd, "max_2d_cax_to_bb_mm", None),
                "passed": passed,
            }
        except Exception:
            pass

        save_analysis(email, "Winston-Lutz", filename, passed, summary, image_url, chart_data, job_id)
        jobs[job_id] = {"status": "Success", "passed": passed,
                        "analysis_summary": summary, "image_url": image_url, "chart_data": chart_data}
    except Exception as e:
        jobs[job_id] = {"status": "Error", "message": str(e)}
    finally:
        for fp in filepaths:
            try: os.remove(fp)
            except Exception: pass
        cleanup()


@app.post("/analyze/winston-lutz")
async def analyze_winston_lutz(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    u=Depends(get_current_user),
):
    if not files:
        raise HTTPException(400, "At least one .dcm file is required")
    job_id   = str(uuid.uuid4())
    tmp_dir  = tempfile.gettempdir()
    filepaths = []
    for i, f in enumerate(files):
        fp = os.path.join(tmp_dir, f"wl_{job_id}_{i}.dcm")
        with open(fp, "wb") as out:
            out.write(await f.read())
        filepaths.append(fp)
    jobs[job_id] = {"status": "Processing"}
    background_tasks.add_task(_run_winston_lutz, job_id, filepaths, u["email"], files[0].filename)
    return {"status": "Queued", "job_id": job_id}


# =============================================================================
# Starshot
# =============================================================================

def _run_starshot(job_id: str, filepath: str, email: str, filename: str):
    try:
        star = Starshot(filepath)
        star.analyze()
        summary  = star.results()
        passed   = star.passed
        plot_path = filepath.replace(".dcm", "_star.png")
        star.plot_analyzed_image(filename=plot_path, show=False)

        import asyncio, nest_asyncio
        try:
            nest_asyncio.apply()
            loop = asyncio.get_event_loop()
            image_url = loop.run_until_complete(upload_plot(plot_path, f"star_{job_id}.png"))
        except Exception:
            image_url = ""

        chart_data = {}
        try:
            rd = star.results_data()
            chart_data = {
                "radius_mm": getattr(rd, "radius_mm", None),
                "passed":    passed,
            }
        except Exception:
            pass

        save_analysis(email, "Starshot", filename, passed, summary, image_url, chart_data, job_id)
        jobs[job_id] = {"status": "Success", "passed": passed,
                        "analysis_summary": summary, "image_url": image_url, "chart_data": chart_data}
    except Exception as e:
        jobs[job_id] = {"status": "Error", "message": str(e)}
    finally:
        try: os.remove(filepath)
        except Exception: pass
        cleanup()


@app.post("/analyze/starshot")
async def analyze_starshot(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    u=Depends(get_current_user),
):
    if not file.filename.lower().endswith(".dcm"):
        raise HTTPException(400, "Only .dcm files are supported")
    job_id   = str(uuid.uuid4())
    filepath = os.path.join(tempfile.gettempdir(), f"star_{job_id}.dcm")
    with open(filepath, "wb") as f:
        f.write(await file.read())
    jobs[job_id] = {"status": "Processing"}
    background_tasks.add_task(_run_starshot, job_id, filepath, u["email"], file.filename)
    return {"status": "Queued", "job_id": job_id}


# =============================================================================
# Congruence (Field Analysis) — logic lives in congruencebackend.py
# =============================================================================

def _run_congruence(job_id: str, filepath: str, email: str, filename: str):
    try:
        fa = FieldAnalysis(filepath)
        fa.analyze(protocol=None, is_FFF=False)
        summary   = fa.results()
        passed    = fa.passed
        plot_path = filepath.replace(".dcm", "_congruence.png")
        fa.plot_analyzed_image(filename=plot_path, show=False)

        import asyncio, nest_asyncio
        try:
            nest_asyncio.apply()
            loop = asyncio.get_event_loop()
            image_url = loop.run_until_complete(upload_plot(plot_path, f"congruence_{job_id}.png"))
        except Exception:
            image_url = ""

        from congruencebackend import _extract_congruence_chart_data
        chart_data = _extract_congruence_chart_data(fa)

        save_analysis(email, "Congruence", filename, passed, summary, image_url, chart_data, job_id)
        jobs[job_id] = {"status": "Success", "passed": passed,
                        "analysis_summary": summary, "image_url": image_url, "chart_data": chart_data}
    except Exception as e:
        jobs[job_id] = {"status": "Error", "message": f"Congruence analysis failed: {e}"}
    finally:
        try: os.remove(filepath)
        except Exception: pass
        cleanup()


@app.post("/analyze/congruence")
async def analyze_congruence(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    u=Depends(get_current_user),
):
    if not file.filename.lower().endswith(".dcm"):
        raise HTTPException(400, "Only .dcm DICOM files are supported for the Congruence test.")
    job_id   = str(uuid.uuid4())
    filepath = os.path.join(tempfile.gettempdir(), f"congruence_{job_id}.dcm")
    with open(filepath, "wb") as f:
        f.write(await file.read())
    jobs[job_id] = {"status": "Processing"}
    background_tasks.add_task(_run_congruence, job_id, filepath, u["email"], file.filename)
    return {"status": "Queued", "job_id": job_id}
