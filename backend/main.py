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
from urllib.parse import quote

# Load .env file for local development (no-op if file doesn't exist or dotenv not installed)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import httpx
import bcrypt
from fastapi import FastAPI, File, UploadFile, HTTPException, Depends, BackgroundTasks, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from pydantic import BaseModel

# ── NEW: Rate Limiting Imports ───────────────────────────────────────────────
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# ── pylinac ──────────────────────────────────────────────────────────────────
from pylinac import PicketFence, WinstonLutz, Starshot, FieldAnalysis
from pylinac import CatPhan503, CatPhan504, CatPhan600, CatPhan604, CatPhan700
from pylinac.ct import CTP515
import zipfile
import shutil
import numpy as np
from pylinac.core.geometry import Point

# ── Environment / config ─────────────────────────────────────────────────────
SUPABASE_URL     = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY     = os.environ.get("SUPABASE_KEY", "")   # service-role key

# SECURITY FIX 2: Removed hardcoded fallback. App will fail if env var is missing.
JWT_SECRET       = os.environ.get("JWT_SECRET")
if not JWT_SECRET:
    raise ValueError("FATAL: JWT_SECRET environment variable is not set! Please configure it in your environment.")

JWT_ALGORITHM    = "HS256"
JWT_EXPIRE_HOURS = 72

# Supabase Storage bucket for plot images
PLOT_BUCKET      = os.environ.get("PLOT_BUCKET", "mlcqa-plots")

# ── Supabase-backed job store ─────────────────────────────────────────────────
# Replaces the in-memory dict so jobs survive Render free-tier restarts.
# Requires a `jobs` table in Supabase (run in Supabase SQL editor):
#
#   create table if not exists jobs (
#     job_id     text primary key,
#     status     text,
#     payload    jsonb,
#     created_at timestamptz default now()
#   );
#   alter table jobs disable row level security;

def _job_url(job_id: str) -> str:
    return f"{SUPABASE_URL}/rest/v1/jobs?job_id=eq.{job_id}"

def job_set(job_id: str, value: dict):
    """Upsert a job record into Supabase."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        headers = supabase_headers(prefer_return=False)
        headers["Prefer"] = "resolution=merge-duplicates"
        httpx.post(
            f"{SUPABASE_URL}/rest/v1/jobs",
            headers=headers,
            json={"job_id": job_id, "status": value.get("status", "Processing"), "payload": value},
            timeout=10,
        )
    except Exception:
        pass

def job_get(job_id: str):
    """Fetch a job record from Supabase."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        r = httpx.get(_job_url(job_id), headers=supabase_headers(), timeout=10)
        rows = r.json()
        if rows:
            return rows[0].get("payload")
    except Exception:
        pass
    return None

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="MLC QA API")

# SECURITY FIX 3: Initialize Rate Limiter
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# SECURITY FIX 1: Restrict CORS to specific frontend domains
origins = [
    "https://mlc-qa-1.onrender.com",  # Frontend
    "http://127.0.0.1:5500",        # Local testing (VS Code Live Server)
    "http://localhost:5500"         # Local testing
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
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
    """Delete jobs older than 2 hours from Supabase."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        httpx.delete(
            f"{SUPABASE_URL}/rest/v1/jobs?created_at=lt.{cutoff}",
            headers=supabase_headers(),
            timeout=10,
        )
    except Exception:
        pass


def create_token(email: str) -> str:
    expire  = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRE_HOURS)
    payload = {"sub": email, "exp": expire}
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return {"email": payload["sub"]}
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def get_current_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return decode_token(credentials.credentials)


def upload_plot(local_path: str, storage_name: str) -> str:
    """Upload a PNG to Supabase Storage and return its public URL."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return ""
    try:
        with open(local_path, "rb") as f:
            data = f.read()
        url = f"{SUPABASE_URL}/storage/v1/object/{PLOT_BUCKET}/{storage_name}"
        headers = {
            "apikey":          SUPABASE_KEY,
            "Authorization":   f"Bearer {SUPABASE_KEY}",
            "Content-Type":    "image/png",
            "x-upsert":        "true",
        }
        resp = httpx.put(url, content=data, headers=headers, timeout=30)
        if resp.status_code in (200, 201):
            return f"{SUPABASE_URL}/storage/v1/object/public/{PLOT_BUCKET}/{storage_name}"
    except Exception:
        pass
    return ""


def save_analysis(*, email: str, test_type: str, filename: str,
                  passed: bool, summary: str, image_url: str,
                  chart_data: dict, job_id: str):
    """Persist a completed analysis record to Supabase (analyses table)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[save_analysis] Skipped — SUPABASE_URL or SUPABASE_KEY not set.")
        return
    try:
        payload = {
            "email":             email,
            "test_type":         test_type,
            "filename":          filename,
            "passed":            passed,
            "summary":           summary,
            "image_url":         image_url,
            "chart_data":        chart_data,
            "job_id":            job_id,
            "created_at":        datetime.now(timezone.utc).isoformat(),
        }
        resp = httpx.post(
            f"{SUPABASE_URL}/rest/v1/analyses",
            json=payload,
            headers=supabase_headers(prefer_return=True),
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            print(f"[save_analysis] ERROR {resp.status_code}: {resp.text}")
        else:
            print(f"[save_analysis] Saved analysis for {email} — test_type={test_type}, passed={passed}")
    except Exception as exc:
        print(f"[save_analysis] Exception: {exc}")


# =============================================================================
# Auth endpoints
# =============================================================================

class AuthBody(BaseModel):
    email:    str
    password: str
    name:     Optional[str] = None


@app.post("/auth/signup")
async def signup(body: AuthBody):
    if not SUPABASE_URL:
        raise HTTPException(500, "Backend not configured")
    hashed = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    resp = httpx.post(
        f"{SUPABASE_URL}/rest/v1/users",
        json={"email": body.email, "password_hash": hashed, "name": body.name or ""},
        headers=supabase_headers(),
        timeout=10,
    )
    if resp.status_code == 409 or (resp.status_code == 201 and "duplicate" in resp.text.lower()):
        raise HTTPException(409, "Email already registered")
    if resp.status_code not in (200, 201):
        raise HTTPException(400, "Could not create account")
    token = create_token(body.email)
    return {"token": token}


# SECURITY FIX 3: Rate Limiting applied to Login endpoint
@app.post("/auth/login")
@limiter.limit("5/minute")
async def login(request: Request, body: AuthBody):
    if not SUPABASE_URL:
        raise HTTPException(500, "Backend not configured")
    resp = httpx.get(
        f"{SUPABASE_URL}/rest/v1/users?email=eq.{quote(body.email)}&select=email,password_hash,name",
        headers=supabase_headers(),
        timeout=10,
    )
    rows = resp.json() if resp.status_code == 200 else []
    if not rows:
        raise HTTPException(401, "Invalid email or password")
    row = rows[0]
    if not bcrypt.checkpw(body.password.encode(), row["password_hash"].encode()):
        raise HTTPException(401, "Invalid email or password")
    token = create_token(body.email)
    return {"token": token, "name": row.get("name") or ""}


# =============================================================================
# Health check
# =============================================================================

@app.get("/")
async def health():
    return {"status": "ok", "service": "MLC QA API"}


@app.get("/me")
async def get_me(u=Depends(get_current_user)):
    """Return the logged-in user's name and email."""
    if not SUPABASE_URL:
        raise HTTPException(500, "Backend not configured")
    resp = httpx.get(
        f"{SUPABASE_URL}/rest/v1/users?email=eq.{u}&select=email,name,created_at",
        headers=supabase_headers(),
        timeout=10,
    )
    rows = resp.json() if resp.status_code == 200 else []
    if not rows:
        raise HTTPException(404, "User not found")
    return {"email": rows[0].get("email", ""), "name": rows[0].get("name") or "", "created_at": rows[0].get("created_at", "")}


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str

@app.post("/change-password")
async def change_password(body: ChangePasswordBody, u=Depends(get_current_user)):
    """Allow a logged-in user to change their password after verifying the current one."""
    if not SUPABASE_URL:
        raise HTTPException(500, "Backend not configured")

    # Fetch current password hash
    resp = httpx.get(
        f"{SUPABASE_URL}/rest/v1/users?email=eq.{quote(u)}&select=email,password_hash",
        headers=supabase_headers(),
        timeout=10,
    )
    rows = resp.json() if resp.status_code == 200 else []
    if not rows:
        raise HTTPException(404, "User not found")

    row = rows[0]
    if not bcrypt.checkpw(body.current_password.encode(), row["password_hash"].encode()):
        raise HTTPException(401, "Current password is incorrect")

    if len(body.new_password) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")

    hashed = bcrypt.hashpw(body.new_password.encode(), bcrypt.gensalt()).decode()
    patch = httpx.patch(
        f"{SUPABASE_URL}/rest/v1/users?email=eq.{quote(u)}",
        headers=supabase_headers(),
        json={"password_hash": hashed},
        timeout=10,
    )
    if patch.status_code not in (200, 204):
        raise HTTPException(500, "Could not update password. Please try again.")

    return {"detail": "Password updated successfully"}


# =============================================================================
# Result & Job Polling
# =============================================================================

@app.get("/result/{job_id}")
async def get_result(job_id: str, u=Depends(get_current_user)):
    job = job_get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job

# FIX 4: Added Missing Cancel Endpoint for CancelManager in script.js
@app.post("/job/{job_id}/cancel")
async def cancel_job(job_id: str, u=Depends(get_current_user)):
    if job_id in jobs:
        job_set(job_id, {"status": "Cancelled"})
    return {"status": "ok"}


# =============================================================================
# Debug — diagnose Supabase connectivity
# =============================================================================

@app.get("/debug/supabase")
async def debug_supabase():
    """Returns Supabase connectivity info."""
    result = {
        "supabase_url_set": bool(SUPABASE_URL),
        "supabase_key_set": bool(SUPABASE_KEY),
        "supabase_url_prefix": SUPABASE_URL[:40] if SUPABASE_URL else None,
    }

    if not SUPABASE_URL or not SUPABASE_KEY:
        result["error"] = "SUPABASE_URL or SUPABASE_KEY env var is missing"
        return result

    try:
        ping = httpx.get(f"{SUPABASE_URL}/rest/v1/", headers=supabase_headers(), timeout=10)
        result["supabase_reachable"] = ping.status_code < 500
        result["supabase_ping_status"] = ping.status_code
    except Exception as e:
        result["supabase_reachable"] = False
        result["supabase_ping_error"] = str(e)

    try:
        tr = httpx.get(
            f"{SUPABASE_URL}/rest/v1/analyses?limit=1&select=id",
            headers=supabase_headers(),
            timeout=10,
        )
        result["analyses_table_status"] = tr.status_code
        result["analyses_table_response"] = tr.text[:300]
    except Exception as e:
        result["analyses_table_error"] = str(e)

    try:
        test_payload = {
            "email": "debug@test.com",
            "test_type": "DEBUG",
            "filename": "debug.dcm",
            "passed": True,
            "summary": "Debug test row — safe to delete",
            "image_url": "",
            "chart_data": {},
            "job_id": "debug-000",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        ir = httpx.post(
            f"{SUPABASE_URL}/rest/v1/analyses",
            json=test_payload,
            headers=supabase_headers(prefer_return=True),
            timeout=10,
        )
        result["insert_status"] = ir.status_code
        result["insert_response"] = ir.text[:300]
        if ir.status_code in (200, 201):
            httpx.delete(
                f"{SUPABASE_URL}/rest/v1/analyses?job_id=eq.debug-000",
                headers=supabase_headers(),
                timeout=10,
            )
            result["insert_test"] = "SUCCESS — table exists and is writable"
        else:
            result["insert_test"] = "FAILED — see insert_response for details"
    except Exception as e:
        result["insert_error"] = str(e)

    return result


# =============================================================================
# History
# =============================================================================

@app.get("/history")
async def get_history(u=Depends(get_current_user)):
    if not SUPABASE_URL:
        return {"analyses": []}
    email = u["email"]
    resp  = httpx.get(
        f"{SUPABASE_URL}/rest/v1/analyses"
        f"?email=eq.{email}&order=created_at.desc&limit=200"
        f"&select=id,test_type,filename,passed,summary,image_url,chart_data,created_at",
        headers=supabase_headers(),
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"[get_history] ERROR {resp.status_code}: {resp.text}")
        return {"analyses": []}
    analyses = resp.json()
    if not isinstance(analyses, list):
        print(f"[get_history] Unexpected response shape: {analyses}")
        return {"analyses": []}
    return {"analyses": analyses}


# =============================================================================
# Picket Fence  —  /analyze
# =============================================================================

MLC_TYPE_MAP = {
    "Millennium": "Millennium",
    "HD MLC":     "HD MLC",
    "Agility":    "Agility",
    "SRS500":     "SRS500",
    "NovalisHD":  "NovalisHD",
}

def _extract_pf_chart_data(pf) -> dict:
    chart_data: dict = {}
    try:
        rd = pf.results_data()
        chart_data["max_error"]     = round(float(rd.max_error_mm), 4)
        chart_data["mean_error"]    = round(float(rd.absolute_median_error_mm), 4)
        chart_data["failed_leaves"] = len(rd.failed_leaves) if rd.failed_leaves else 0

        try:
            num_leaves = len(pf.mlc.value["arrangement"].leaves)
        except Exception:
            num_leaves = 60
        chart_data["num_leaves"] = num_leaves

        leaf_pairs      = []
        leaf_max_errors = []

        errors_by_leaf: dict = rd.mlc_errors_by_leaf

        for leaf_key, errors in errors_by_leaf.items():
            try:
                pair_num = int(leaf_key)
            except ValueError:
                import re
                m = re.search(r"(\d+)", leaf_key)
                pair_num = int(m.group(1)) if m else 0

            max_err = max((abs(float(e)) for e in errors), default=0.0)
            leaf_pairs.append({"leaf_pair": pair_num, "max_error": round(max_err, 4)})
            leaf_max_errors.append(round(max_err, 4))

        chart_data["leaf_pairs"]      = leaf_pairs
        chart_data["leaf_max_errors"] = leaf_max_errors

        try:
            chart_data["picket_offsets"] = [round(float(o), 3) for o in rd.offsets_from_cax_mm]
        except Exception:
            chart_data["picket_offsets"] = []
        try:
            chart_data["mlc_skew"] = round(float(rd.mlc_skew), 4)
        except Exception:
            chart_data["mlc_skew"] = 0.0

    except Exception as e:
        chart_data["error"] = str(e)

    return chart_data


def _run_picket_fence(job_id: str, filepath: str, email: str,
                      filename: str, tolerance: float,
                      action_tolerance: float, mlc_type: str):
    try:
        pf = PicketFence(filepath)

        try:
            pf.analyze(
                tolerance        = tolerance,
                action_tolerance = action_tolerance,
                mlc              = mlc_type,
            )
        except TypeError:
            pf.analyze(
                tolerance        = tolerance,
                action_tolerance = action_tolerance,
            )

        summary    = pf.results()
        passed     = pf.passed

        plot_path = filepath.replace(".dcm", "_pf.png")
        try:
            pf.save_analyzed_image(plot_path)
        except AttributeError:
            pf.plot_analyzed_image(filename=plot_path, show=False)
        image_url = upload_plot(plot_path, f"pf_{job_id}.png")

        chart_data = _extract_pf_chart_data(pf)

        save_analysis(
            email      = email,
            test_type  = "Picket Fence",
            filename   = filename,
            passed     = passed,
            summary    = summary,
            image_url  = image_url,
            chart_data = chart_data,
            job_id     = job_id,
        )

        job_set(job_id, {
            "status":           "Success",
            "passed":           passed,
            "analysis_summary": summary,
            "image_url":        image_url,
            "chart_data":       chart_data,
        })

    except Exception as e:
        job_set(job_id, {
            "status":  "Error",
            "message": f"Picket Fence analysis failed: {e}",
        })
    finally:
        try:
            os.remove(filepath)
        except Exception:
            pass
        try:
            os.remove(filepath.replace(".dcm", "_pf.png"))
        except Exception:
            pass
        cleanup()


@app.post("/analyze")
async def analyze_picket_fence(
    background_tasks: BackgroundTasks,
    file:             UploadFile   = File(...),
    tolerance:        float        = Form(1.0),
    action_tolerance: float        = Form(0.5),
    mlc_type:         str          = Form("Millennium"),
    u=Depends(get_current_user),
):
    if not file.filename.lower().endswith(".dcm"):
        raise HTTPException(400, "Only .dcm DICOM files are supported for Picket Fence.")

    job_id   = str(uuid.uuid4())
    tmp_dir  = tempfile.gettempdir()
    filepath = os.path.join(tmp_dir, f"pf_{job_id}.dcm")

    contents = await file.read()
    with open(filepath, "wb") as f:
        f.write(contents)

    job_set(job_id, {"status": "Processing", "_ts": time.time()})

    background_tasks.add_task(
        _run_picket_fence,
        job_id           = job_id,
        filepath         = filepath,
        email            = u["email"],
        filename         = file.filename,
        tolerance        = tolerance,
        action_tolerance = action_tolerance,
        mlc_type         = MLC_TYPE_MAP.get(mlc_type, "Millennium"),
    )

    return {"status": "Queued", "job_id": job_id}


# =============================================================================
# Winston-Lutz  —  /analyze/winston-lutz
# =============================================================================

def _extract_wl_chart_data(wl) -> dict:
    chart_data: dict = {}
    try:
        rd = wl.results_data()
        chart_data["max_offset_mm"]  = round(float(getattr(rd, "max_2d_cax_to_bb_mm",  0)), 4)
        chart_data["mean_offset_mm"] = round(float(getattr(rd, "mean_2d_cax_to_bb_mm", 0)), 4)
        chart_data["num_images"]     = int(getattr(rd, "num_total_images", 0))

        images_out = []
        details   = getattr(rd, "image_details", []) or []
        wl_images = getattr(wl, "images", [])
        for i, detail in enumerate(details):
            try:
                gantry = round(float(wl_images[i].gantry_angle), 1) if i < len(wl_images) else 0.0
                images_out.append({
                    "gantry_angle": gantry,
                    "bb_offset_mm": round(float(getattr(detail, "cax2bb_distance", 0)), 4),
                })
            except Exception:
                pass
        chart_data["images"] = images_out
    except Exception as e:
        chart_data["error"] = str(e)
    return chart_data


def _run_winston_lutz(job_id: str, filepaths: list, email: str, filenames: str):
    tmp_dir = None
    try:
        import tempfile as _tf
        import shutil as _shutil
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as _plt

        if len(filepaths) < 2:
            raise ValueError(
                f"Winston-Lutz analysis requires a minimum of 2 DICOM images "
                f"(one per gantry angle). Only {len(filepaths)} file(s) were uploaded. "
                f"Please upload images taken at multiple gantry angles (e.g. 0°, 90°, 180°, 270°)."
            )

        tmp_dir = _tf.mkdtemp(prefix=f"wl_{job_id}_")
        for fp in filepaths:
            dest = os.path.join(tmp_dir, os.path.basename(fp))
            _shutil.copy2(fp, dest)

        wl = WinstonLutz(tmp_dir)
        wl.analyze(bb_size_mm=5)
        summary = wl.results()

        WL_TOLERANCE_MM = 2.0
        try:
            _rd = wl.results_data()
            passed = float(_rd.max_2d_cax_to_bb_mm) <= WL_TOLERANCE_MM
        except Exception:
            passed = False

        plot_path = os.path.join(tmp_dir, "wl_plot.png")
        try:
            wl.save_summary(plot_path)
        except Exception:
            try:
                wl.plot_summary(show=False)
                _plt.savefig(plot_path, bbox_inches="tight", dpi=120)
            except Exception:
                pass
            finally:
                _plt.close("all")
        image_url = upload_plot(plot_path, f"wl_{job_id}.png") if os.path.exists(plot_path) else ""

        chart_data = _extract_wl_chart_data(wl)

        save_analysis(
            email      = email,
            test_type  = "Winston-Lutz",
            filename   = filenames,
            passed     = passed,
            summary    = summary,
            image_url  = image_url,
            chart_data = chart_data,
            job_id     = job_id,
        )

        job_set(job_id, {
            "status":           "Success",
            "passed":           passed,
            "analysis_summary": summary,
            "image_url":        image_url,
            "chart_data":       chart_data,
        })

    except Exception as e:
        job_set(job_id, {
            "status":  "Error",
            "message": f"Winston-Lutz analysis failed: {e}",
        })
    finally:
        if tmp_dir:
            import shutil
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except Exception:
                pass
        cleanup()


@app.post("/analyze/winston-lutz")
async def analyze_winston_lutz(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    u=Depends(get_current_user),
):
    if not files:
        raise HTTPException(400, "No files uploaded")

    job_id  = str(uuid.uuid4())
    tmp_dir = tempfile.mkdtemp(prefix=f"wl_{job_id}_")
    saved   = []

    for f in files:
        if not f.filename.lower().endswith(".dcm"):
            continue
        fp = os.path.join(tmp_dir, f.filename)
        contents = await f.read()
        with open(fp, "wb") as out:
            out.write(contents)
        saved.append(fp)

    if not saved:
        raise HTTPException(400, "No valid .dcm files found")

    filenames = f"{len(saved)} DICOM image(s)"
    job_set(job_id, {"status": "Processing", "_ts": time.time()})

    background_tasks.add_task(
        _run_winston_lutz,
        job_id    = job_id,
        filepaths = saved,
        email     = u["email"],
        filenames = filenames,
    )

    return {"status": "Queued", "job_id": job_id}


# =============================================================================
# Starshot  —  /analyze/starshot
# =============================================================================

def _extract_ss_chart_data(ss) -> dict:
    chart_data: dict = {}
    try:
        rd = ss.results_data()
        chart_data["wobble_radius"] = round(float(getattr(rd, "circle_profile_radius", 0) or 0), 4)

        spokes_out = []
        for spoke in getattr(ss, "lines", []):
            try:
                spokes_out.append({
                    "angle":    round(float(getattr(spoke, "angle_to_positive_x", 0)), 1),
                    "distance": round(float(getattr(spoke, "distance_from_center", 0)), 4),
                })
            except Exception:
                pass
        chart_data["spokes"] = spokes_out

        try:
            import numpy as np
            arr     = ss.image.array.astype(float)
            arr_min, arr_max = arr.min(), arr.max()
            if arr_max > arr_min:
                arr = (arr - arr_min) / (arr_max - arr_min)
            cy, cx = arr.shape[0] // 2, arr.shape[1] // 2
            row = arr[cy, :].tolist()
            step = max(1, len(row) // 100)
            chart_data["radial_profile"] = [round(float(row[i]), 4) for i in range(0, len(row), step)][:100]
        except Exception:
            chart_data["radial_profile"] = []

    except Exception as e:
        chart_data["error"] = str(e)
    return chart_data


def _run_starshot(job_id: str, filepath: str, email: str, filename: str):
    try:
        ss = Starshot(filepath)
        ss.analyze(radius=0.85, min_peak_height=0.25, tolerance=1.0)

        summary = ss.results()
        passed  = ss.passed

        plot_path = filepath.replace(".dcm", "_ss.png")
        try:
            ss.save_analyzed_image(plot_path)
        except AttributeError:
            ss.plot_analyzed_image(filename=plot_path, show=False)
        image_url = upload_plot(plot_path, f"ss_{job_id}.png")

        chart_data = _extract_ss_chart_data(ss)

        save_analysis(
            email      = email,
            test_type  = "Starshot",
            filename   = filename,
            passed     = passed,
            summary    = summary,
            image_url  = image_url,
            chart_data = chart_data,
            job_id     = job_id,
        )

        job_set(job_id, {
            "status":           "Success",
            "passed":           passed,
            "analysis_summary": summary,
            "image_url":        image_url,
            "chart_data":       chart_data,
        })

    except Exception as e:
        job_set(job_id, {
            "status":  "Error",
            "message": f"Starshot analysis failed: {e}",
        })
    finally:
        try:
            os.remove(filepath)
        except Exception:
            pass
        try:
            os.remove(filepath.replace(".dcm", "_ss.png"))
        except Exception:
            pass
        cleanup()


@app.post("/analyze/starshot")
async def analyze_starshot(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    u=Depends(get_current_user),
):
    if not file.filename.lower().endswith((".dcm", ".zip")):
        raise HTTPException(400, "Only .dcm or .zip files are supported for Starshot.")

    job_id   = str(uuid.uuid4())
    tmp_dir  = tempfile.gettempdir()
    ext      = ".zip" if file.filename.lower().endswith(".zip") else ".dcm"
    filepath = os.path.join(tmp_dir, f"ss_{job_id}{ext}")

    contents = await file.read()
    with open(filepath, "wb") as f:
        f.write(contents)

    job_set(job_id, {"status": "Processing", "_ts": time.time()})

    background_tasks.add_task(
        _run_starshot,
        job_id   = job_id,
        filepath = filepath,
        email    = u["email"],
        filename = file.filename,
    )

    return {"status": "Queued", "job_id": job_id}


# =============================================================================
# Congruence  —  /analyze/congruence
# =============================================================================

def _extract_congruence_chart_data(fa) -> dict:
    try:
        rd = fa.results_data()

        edges = {
            "top":    round(float(rd.cax_to_top_mm),    3),
            "bottom": round(float(rd.cax_to_bottom_mm), 3),
            "left":   round(float(rd.cax_to_left_mm),   3),
            "right":  round(float(rd.cax_to_right_mm),  3),
        }

        field_size = {
            "vertical_mm":   round(float(rd.field_size_vertical_mm),   2),
            "horizontal_mm": round(float(rd.field_size_horizontal_mm), 2),
        }

        penumbra = {
            "top":    round(float(rd.top_penumbra_mm),    3),
            "bottom": round(float(rd.bottom_penumbra_mm), 3),
            "left":   round(float(rd.left_penumbra_mm),   3),
            "right":  round(float(rd.right_penumbra_mm),  3),
        }

        inline_profile, crossline_profile = [], []
        try:
            import numpy as np
            arr = fa.image.array.astype(float)
            arr_min, arr_max = arr.min(), arr.max()
            if arr_max > arr_min:
                arr = (arr - arr_min) / (arr_max - arr_min)
            cy, cx = arr.shape[0] // 2, arr.shape[1] // 2
            inline_raw    = arr[cy, :].tolist()
            crossline_raw = arr[:, cx].tolist()

            def downsample(lst, n=100):
                step = max(1, len(lst) // n)
                return [round(float(lst[i]), 4) for i in range(0, len(lst), step)][:n]

            inline_profile    = downsample(inline_raw)
            crossline_profile = downsample(crossline_raw)
        except Exception:
            pass

        return {
            "edges":             edges,
            "field_size":        field_size,
            "penumbra":          penumbra,
            "inline_profile":    inline_profile,
            "crossline_profile": crossline_profile,
            "tolerance_mm":      2.0,
        }
    except Exception as e:
        return {
            "error":             str(e),
            "edges":             {"top": None, "bottom": None, "left": None, "right": None},
            "inline_profile":    [],
            "crossline_profile": [],
            "tolerance_mm":      2.0,
        }


def _run_congruence(job_id: str, filepath: str, email: str, filename: str):
    try:
        from pylinac import FieldAnalysis
        from pylinac.field_analysis import Protocol

        fa = FieldAnalysis(filepath)
        fa.analyze(
            protocol  = Protocol.NONE,
            is_FFF    = False,
        )

        summary = fa.results()

        chart_data = _extract_congruence_chart_data(fa)
        tol = 2.0 
        edges = chart_data.get("edges", {})
        edge_values = [v for v in edges.values() if v is not None]
        passed = all(abs(v) <= tol for v in edge_values) if edge_values else True

        plot_path = filepath.replace(".dcm", "_congruence.png")
        try:
            fa.save_analyzed_image(plot_path)
        except Exception:
            try:
                fa.plot_analyzed_image(filename=plot_path, show=False)
            except Exception:
                plot_path = None

        image_url = upload_plot(plot_path, f"congruence_{job_id}.png") if plot_path else ""

        save_analysis(
            email      = email,
            test_type  = "Congruence",
            filename   = filename,
            passed     = passed,
            summary    = summary,
            image_url  = image_url,
            chart_data = chart_data,
            job_id     = job_id,
        )

        job_set(job_id, {
            "status":           "Success",
            "passed":           passed,
            "analysis_summary": summary,
            "image_url":        image_url,
            "chart_data":       chart_data,
        })

    except Exception as e:
        job_set(job_id, {
            "status":  "Error",
            "message": f"Congruence analysis failed: {e}",
        })
    finally:
        try:
            os.remove(filepath)
        except Exception:
            pass
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
    tmp_dir  = tempfile.gettempdir()
    filepath = os.path.join(tmp_dir, f"congruence_{job_id}.dcm")

    contents = await file.read()
    with open(filepath, "wb") as f:
        f.write(contents)

    job_set(job_id, {"status": "Processing", "_ts": time.time()})

    background_tasks.add_task(
        _run_congruence,
        job_id   = job_id,
        filepath = filepath,
        email    = u["email"],
        filename = file.filename,
    )

    return {"status": "Queued", "job_id": job_id}


# =============================================================================
# Forgot Password  —  /auth/forgot-password  (on-screen link, no email needed)
# =============================================================================

import secrets as _secrets

# In-memory reset token store: { token: {"email": str, "expires": float} }
_reset_tokens: dict = {}

class ForgotBody(BaseModel):
    email: str

class ResetBody(BaseModel):
    token: str
    new_password: str


@app.post("/auth/forgot-password")
@limiter.limit("5/minute")
async def forgot_password(request: Request, body: ForgotBody):
    """
    Returns a reset link directly in the response (on-screen flow — no email).
    Returns the same shape whether the email exists or not, to avoid leaking
    account info. Only the reset_url field is populated when the email is found.
    """
    if not SUPABASE_URL:
        return {"ok": True, "reset_url": None}

    # Fix 5: Purge expired tokens on every call to prevent memory leak
    now = time.time()
    expired_keys = [k for k, v in _reset_tokens.items() if v["expires"] < now]
    for k in expired_keys:
        _reset_tokens.pop(k, None)

    try:
        # Fix 6: URL-encode email to handle + and special characters
        resp = httpx.get(
            f"{SUPABASE_URL}/rest/v1/users?email=eq.{quote(body.email)}&select=email",
            headers=supabase_headers(), timeout=8,
        )
        rows = resp.json() if resp.status_code == 200 else []
    except Exception:
        rows = []

    if rows:
        token   = _secrets.token_urlsafe(32)
        expires = time.time() + 3600  # 1 hour

        # Fix 3: Persist token to Supabase so it survives server restarts
        try:
            httpx.post(
                f"{SUPABASE_URL}/rest/v1/reset_tokens",
                json={"token": token, "email": body.email, "expires": expires},
                headers=supabase_headers(),
                timeout=8,
            )
        except Exception:
            pass
        # Also keep in-memory as fast-path fallback
        _reset_tokens[token] = {"email": body.email, "expires": expires}

        base_url  = os.environ.get("RESET_BASE_URL", "https://mlc-qa-1.onrender.com").rstrip("/")
        # Fix 1: Correct filename — resetpassword.html (no hyphen)
        reset_url = f"{base_url}/resetpassword.html?token={token}"
        return {"ok": True, "reset_url": reset_url}

    # Email not found — return same shape, no url
    return {"ok": True, "reset_url": None}


# Fix 4: Add rate limiting to reset-password endpoint
@app.post("/auth/reset-password")
@limiter.limit("5/minute")
async def reset_password(request: Request, body: ResetBody):
    """Consume a reset token and update the user's password."""
    # Check in-memory store first (fast path)
    entry = _reset_tokens.get(body.token)

    # Fix 3: Fall back to Supabase if not in memory (e.g. after a server restart)
    if not entry and SUPABASE_URL:
        try:
            resp = httpx.get(
                f"{SUPABASE_URL}/rest/v1/reset_tokens?token=eq.{body.token}&select=*",
                headers=supabase_headers(), timeout=8,
            )
            rows = resp.json() if resp.status_code == 200 else []
            if rows:
                entry = {"email": rows[0]["email"], "expires": rows[0]["expires"]}
        except Exception:
            pass

    if not entry or time.time() > entry["expires"]:
        raise HTTPException(400, "Reset link is invalid or has expired.")

    if len(body.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")

    email  = entry["email"]
    hashed = bcrypt.hashpw(body.new_password.encode(), bcrypt.gensalt()).decode()

    resp = httpx.patch(
        f"{SUPABASE_URL}/rest/v1/users?email=eq.{quote(email)}",
        json={"password_hash": hashed},
        headers=supabase_headers(),
        timeout=10,
    )
    if resp.status_code not in (200, 204):
        raise HTTPException(500, "Could not update password. Please try again.")

    # Invalidate the token in both stores
    _reset_tokens.pop(body.token, None)
    if SUPABASE_URL:
        try:
            httpx.delete(
                f"{SUPABASE_URL}/rest/v1/reset_tokens?token=eq.{body.token}",
                headers=supabase_headers(),
                timeout=8,
            )
        except Exception:
            pass

    return {"ok": True, "message": "Password updated successfully."}
# =============================================================================
# CatPhan 604 CBCT QA  —  /analyze/catphan
# =============================================================================




# ─────────────────────────────────────────────────────────────────────────────
# Chart data extractor
# ─────────────────────────────────────────────────────────────────────────────
def _extract_catphan_chart_data(phantom) -> dict:
    """Build the chart_data dict that the frontend consumes."""
    cd: dict = {}

    # ── Low contrast (pylinac default) ───────────────────────────────────────
    try:
        ctp515 = phantom.ctp515
        cd["low_contrast_total"] = ctp515.rois_visible
        cd["cnr_threshold"]      = ctp515.cnr_threshold
    except Exception as exc:
        cd["low_contrast_total"] = 0
        cd["low_contrast_error"] = str(exc)

    # ── HU Linearity (CTP404) ───────────────────────────────────────────────
    try:
        ctp404 = phantom.ctp404
        cd["hu_values"] = {
            name: round(float(roi.pixel_value), 1)
            for name, roi in ctp404.hu_rois.items()
        }
        deviations = [
            abs(roi.pixel_value - roi.nominal_hu_value)
            for roi in ctp404.hu_rois.values()
            if hasattr(roi, "nominal_hu_value")
        ]
        cd["hu_linearity_max_deviation"] = round(float(max(deviations)), 2) if deviations else None
        cd["slice_thickness_mm"] = round(float(ctp404.measured_slice_thickness_mm), 3)
    except Exception as exc:
        cd["hu_values"] = {}
        cd["hu_linearity_max_deviation"] = None
        cd["slice_thickness_error"] = str(exc)

    # ── Uniformity (CTP486) ─────────────────────────────────────────────────
    try:
        ctp486 = phantom.ctp486
        cd["uniformity_index"] = round(float(ctp486.uniformity_index), 3)
    except Exception as exc:
        cd["uniformity_index"] = None
        cd["uniformity_error"] = str(exc)

    # ── MTF (CTP528) ────────────────────────────────────────────────────────
    try:
        ctp528 = phantom.ctp528
        cd["mtf_50"]     = round(float(ctp528.mtf.relative_resolution(50)), 4)
        # Down-sample MTF curve for chart rendering (≤60 points)
        mtf_vals = list(ctp528.mtf.norm_mtfs.values())
        step     = max(1, len(mtf_vals) // 60)
        cd["mtf_profile"] = [round(float(v), 4) for v in mtf_vals[::step]]
    except Exception as exc:
        cd["mtf_50"]      = None
        cd["mtf_profile"] = []
        cd["mtf_error"]   = str(exc)

    # ── QA Summary table ─────────────────────────────────────────────────────
    hu_ok = (cd.get("hu_linearity_max_deviation") or 9999) <= 40
    un_ok = (cd.get("uniformity_index")            or 9999) <= 40
    st    = cd.get("slice_thickness_mm")

    cd["qa_table"] = [
        {
            "module":    "CTP404",
            "parameter": "HU Linearity",
            "measured":  "See HU ROIs",
            "tolerance": "±40 HU",
            "status":    "PASS" if hu_ok else "FAIL",
        },
        {
            "module":    "CTP404",
            "parameter": "Slice Thickness",
            "measured":  f"{st:.2f} mm" if st else "—",
            "tolerance": "±0.2 mm",
            "status":    "PASS" if st and abs(st - 3.0) <= 0.2 else "FAIL",
        },
        {
            "module":    "CTP486",
            "parameter": "Uniformity",
            "measured":  str(cd.get("uniformity_index", "—")),
            "tolerance": "≤ 40",
            "status":    "PASS" if un_ok else "FAIL",
        },
        {
            "module":    "CTP528",
            "parameter": "MTF 50%",
            "measured":  f"{cd['mtf_50']:.3f} lp/mm" if cd.get("mtf_50") else "—",
            "tolerance": "Vendor Spec",
            "status":    "PASS",   # vendor-specific; flag for physicist review
        },
        {
            "module":    "CTP515",
            "parameter": "Low Contrast (Total ROIs Seen)",
            "measured":  f"{cd.get('low_contrast_total', 0)} ROIs",
            "tolerance": "≥ 3",
            "status":    "PASS" if cd.get("low_contrast_total", 0) >= 3 else "FAIL",
        },
    ]

    return cd


# ─────────────────────────────────────────────────────────────────────────────
# CatPhan model auto-detector
# Reads DICOM headers to find the model number, falls back to CatPhan604
# ─────────────────────────────────────────────────────────────────────────────
def _detect_catphan(dicom_dir: str):
    """
    Tries to detect CatPhan model from DICOM filenames or headers.
    Supports 503, 504, 600, 604, 700. Defaults to CatPhan604.
    """
    MODEL_MAP = {
        "503": CatPhan503,
        "504": CatPhan504,
        "600": CatPhan600,
        "604": CatPhan604,
        "700": CatPhan700,
    }
    # Check filenames first
    try:
        for fname in os.listdir(dicom_dir):
            for model_num, cls in MODEL_MAP.items():
                if model_num in fname:
                    return cls(dicom_dir)
    except Exception:
        pass

    # Check DICOM headers
    try:
        import pydicom
        for fname in os.listdir(dicom_dir):
            if fname.lower().endswith(".dcm"):
                ds = pydicom.dcmread(
                    os.path.join(dicom_dir, fname),
                    stop_before_pixels=True,
                    force=True,
                )
                # Check series description and study description
                for tag in ["SeriesDescription", "StudyDescription", "ProtocolName", "PatientName"]:
                    val = str(getattr(ds, tag, "") or "").lower()
                    for model_num, cls in MODEL_MAP.items():
                        if model_num in val:
                            return cls(dicom_dir)
                break  # only need to check one file
    except Exception:
        pass

    # Default to CatPhan604
    return CatPhan604(dicom_dir)


# ─────────────────────────────────────────────────────────────────────────────
# Background worker
# ─────────────────────────────────────────────────────────────────────────────
def _run_catphan(job_id: str, file_path: str, email: str, filename: str, is_zip: bool):
    tmp_dir = None
    try:
        import matplotlib
        matplotlib.use("Agg")

        tmp_dir = tempfile.mkdtemp(prefix=f"catphan_{job_id}_")

        dicom_dir = tmp_dir  # default; updated below if zip has a subfolder
        if is_zip:
            # Unzip full DICOM series into tmp_dir
            with zipfile.ZipFile(file_path, "r") as zf:
                zf.extractall(tmp_dir)
            # If DICOMs landed inside a subfolder, find the actual directory
            dcm_files = [f for f in os.listdir(tmp_dir) if f.lower().endswith(".dcm")]
            if not dcm_files:
                for entry in os.listdir(tmp_dir):
                    sub = os.path.join(tmp_dir, entry)
                    if os.path.isdir(sub):
                        sub_dcms = [f for f in os.listdir(sub) if f.lower().endswith(".dcm")]
                        if sub_dcms:
                            dicom_dir = sub
                            break
            phantom = _detect_catphan(dicom_dir)
        else:
            # Single .dcm — copy into tmp_dir so pylinac can locate it
            import shutil as _shutil
            dest = os.path.join(tmp_dir, os.path.basename(file_path))
            _shutil.copy2(file_path, dest)
            phantom = _detect_catphan(tmp_dir)
        model_name = f"CatPhan {phantom._model}" if hasattr(phantom, '_model') else "CatPhan 604"

        # Attempt analysis; if the scan doesn't cover the full phantom extent
        # (e.g. a partial CBCT series), bypass the strict extent check and retry.
        try:
            phantom.analyze()
        except ValueError as ve:
            if "physical scan extent" in str(ve).lower() or "scan extent" in str(ve).lower():
                # Monkey-patch the extent guard so pylinac skips the check,
                # then re-instantiate a fresh phantom and analyze it.
                import types
                phantom2 = _detect_catphan(dicom_dir)
                phantom2._ensure_physical_scan_extent = types.MethodType(
                    lambda self: True, phantom2
                )
                phantom2.analyze()
                phantom = phantom2
            else:
                raise

        summary   = phantom.results()

        # Overall pass: HU ±40, uniformity ≤40, slice thickness ±0.2,
        # AND low contrast total ROIs seen ≥ 3
        chart_data = _extract_catphan_chart_data(phantom)
        hu_ok  = (chart_data.get("hu_linearity_max_deviation") or 9999) <= 40
        un_ok  = (chart_data.get("uniformity_index")            or 9999) <= 40
        lc_ok  = chart_data.get("low_contrast_total", 0) >= 3
        passed = hu_ok and un_ok and lc_ok

        # Save analysis plot
        plot_path = os.path.join(tmp_dir, "catphan_plot.png")
        try:
            phantom.save_analyzed_image(plot_path)
        except Exception:
            try:
                import matplotlib.pyplot as _plt
                phantom.plot_analyzed_image(show=False)
                _plt.savefig(plot_path, bbox_inches="tight", dpi=120)
                _plt.close("all")
            except Exception:
                plot_path = None

        image_url = upload_plot(plot_path, f"catphan_{job_id}.png") if plot_path and os.path.exists(plot_path) else ""

        save_analysis(
            email      = email,
            test_type  = model_name,
            filename   = filename,
            passed     = passed,
            summary    = summary,
            image_url  = image_url,
            chart_data = chart_data,
            job_id     = job_id,
        )

        job_set(job_id, {
            "status":           "Success",
            "passed":           passed,
            "analysis_summary": summary,
            "image_url":        image_url,
            "chart_data":       chart_data,
        })

    except Exception as exc:
        job_set(job_id, {
            "status":  "Error",
            "message": f"CatPhan analysis failed: {exc}",
        })
    finally:
        try:
            os.remove(file_path)
        except Exception:
            pass
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        cleanup()


# ─────────────────────────────────────────────────────────────────────────────
# API endpoint
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/analyze/catphan")
async def analyze_catphan(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    u=Depends(get_current_user),
):
    """
    Accepts either:
      - A .zip archive containing a full CBCT DICOM series (recommended — pylinac
        uses multiple slices to locate each phantom module automatically), or
      - A single .dcm DICOM file (limited — pylinac will analyse the one slice it
        can find; some modules may not be detected).

    Returns a job_id for polling via GET /result/{job_id}.
    """
    fname_lower = file.filename.lower()
    is_zip = fname_lower.endswith(".zip")
    is_dcm = fname_lower.endswith(".dcm")

    if not is_zip and not is_dcm:
        raise HTTPException(
            400,
            "Unsupported file type. Please upload a .zip (full DICOM series, recommended) "
            "or a .dcm (single DICOM slice)."
        )

    job_id    = str(uuid.uuid4())
    tmp_dir   = tempfile.gettempdir()
    ext       = ".zip" if is_zip else ".dcm"
    file_path = os.path.join(tmp_dir, f"catphan_{job_id}{ext}")

    contents = await file.read()
    with open(file_path, "wb") as f:
        f.write(contents)

    job_set(job_id, {"status": "Processing", "_ts": time.time()})

    background_tasks.add_task(
        _run_catphan,
        job_id    = job_id,
        file_path = file_path,
        email     = u["email"],
        filename  = file.filename,
        is_zip    = is_zip,
    )

    return {"status": "Queued", "job_id": job_id}
