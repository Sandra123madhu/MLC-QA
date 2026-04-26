# =============================================================================
# MLC QA Platform — FastAPI Backend
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

def supabase_headers(use_service_key: bool = True) -> dict:
    key = SUPABASE_KEY
    return {
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
    }


def cleanup():
    """Remove jobs older than 2 hours from the in-memory store."""
    cutoff = time.time() - 7200
    stale  = [k for k, v in jobs.items() if isinstance(v, dict) and v.get("_ts", time.time()) < cutoff]
    for k in stale:
        jobs.pop(k, None)


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
        httpx.post(
            f"{SUPABASE_URL}/rest/v1/analyses",
            json=payload,
            headers=supabase_headers(),
            timeout=15,
        )
    except Exception:
        pass


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


@app.post("/auth/login")
async def login(body: AuthBody):
    if not SUPABASE_URL:
        raise HTTPException(500, "Backend not configured")
    resp = httpx.get(
        f"{SUPABASE_URL}/rest/v1/users?email=eq.{body.email}&select=email,password_hash",
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
    return {"token": token}


# =============================================================================
# Health check
# =============================================================================

@app.get("/")
async def health():
    return {"status": "ok", "service": "MLC QA API"}


# =============================================================================
# Result polling
# =============================================================================

@app.get("/result/{job_id}")
async def get_result(job_id: str, u=Depends(get_current_user)):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


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
    analyses = resp.json() if resp.status_code == 200 else []
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
    """Extract per-leaf-pair errors and summary metrics from a pylinac 3.42 PicketFence result.

    In pylinac 3.42 the key data lives in PFResult:
      - max_error_mm          → float
      - absolute_median_error_mm → float (used as mean proxy)
      - failed_leaves         → list[str|int]
      - mlc_errors_by_leaf    → dict[str, list[float]]
            keys  = str(leaf_number)  e.g. "1", "2" … "60"
            values = list of errors (mm) — one per picket
      - offsets_from_cax_mm   → list[float]
      - mlc_skew              → float
    Total MLC pairs = length of the MLC arrangement used (60 for Millennium, 80 for Agility).
    """
    chart_data: dict = {}
    try:
        rd = pf.results_data()   # returns PFResult pydantic model

        # ── Scalar metrics ────────────────────────────────────────────────────
        chart_data["max_error"]     = round(float(rd.max_error_mm), 4)
        chart_data["mean_error"]    = round(float(rd.absolute_median_error_mm), 4)
        chart_data["failed_leaves"] = len(rd.failed_leaves) if rd.failed_leaves else 0

        # ── Total MLC leaf count ──────────────────────────────────────────────
        # Derive from the MLC arrangement that was used for this analysis.
        try:
            # pf.mlc is the MLC enum member; .value is a dict with 'arrangement'
            num_leaves = len(pf.mlc.value["arrangement"].leaves)
        except Exception:
            # Fallback: count unique keys in the errors dict (only measured leaves)
            # and default to 60 — the rendering layer will show grey for unmeasured ones.
            num_leaves = 60
        chart_data["num_leaves"] = num_leaves

        # ── Per-leaf-pair data from mlc_errors_by_leaf ────────────────────────
        # mlc_errors_by_leaf: { "1": [e_picket0, e_picket1, ...], "2": [...], ... }
        # We want, for each leaf: max(abs(error)) across all pickets.
        leaf_pairs      = []
        leaf_max_errors = []

        errors_by_leaf: dict = rd.mlc_errors_by_leaf  # already sorted ascending by key

        for leaf_key, errors in errors_by_leaf.items():
            try:
                # Keys are plain integers as strings for standard (non-separate) analysis
                pair_num = int(leaf_key)
            except ValueError:
                # Separate-leaves mode: keys look like "A30", "B30" — skip or combine
                import re
                m = re.search(r"(\d+)", leaf_key)
                pair_num = int(m.group(1)) if m else 0

            max_err = max((abs(float(e)) for e in errors), default=0.0)
            leaf_pairs.append({"leaf_pair": pair_num, "max_error": round(max_err, 4)})
            leaf_max_errors.append(round(max_err, 4))

        chart_data["leaf_pairs"]      = leaf_pairs
        chart_data["leaf_max_errors"] = leaf_max_errors

        # ── Extra fields ──────────────────────────────────────────────────────
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

        # Pass MLC type if pylinac accepts it
        try:
            pf.analyze(
                tolerance        = tolerance,
                action_tolerance = action_tolerance,
                mlc              = mlc_type,
            )
        except TypeError:
            # Older pylinac versions do not accept mlc= kwarg
            pf.analyze(
                tolerance        = tolerance,
                action_tolerance = action_tolerance,
            )

        summary    = pf.results()
        passed     = pf.passed

        # Plot
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

        jobs[job_id] = {
            "status":           "Success",
            "passed":           passed,
            "analysis_summary": summary,
            "image_url":        image_url,
            "chart_data":       chart_data,
        }

    except Exception as e:
        jobs[job_id] = {
            "status":  "Error",
            "message": f"Picket Fence analysis failed: {e}",
        }
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

    jobs[job_id] = {"status": "Processing", "_ts": time.time()}

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

        # pylinac >= 3.6: per-image data lives in rd.image_details (WinstonLutz2DResult)
        # gantry angle comes from wl.images[i].gantry_angle
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

        # Validate minimum image count before calling pylinac
        if len(filepaths) < 2:
            raise ValueError(
                f"Winston-Lutz analysis requires a minimum of 2 DICOM images "
                f"(one per gantry angle). Only {len(filepaths)} file(s) were uploaded. "
                f"Please upload images taken at multiple gantry angles (e.g. 0°, 90°, 180°, 270°)."
            )

        # Copy uploaded files into a dedicated temp directory for pylinac
        tmp_dir = _tf.mkdtemp(prefix=f"wl_{job_id}_")
        for fp in filepaths:
            dest = os.path.join(tmp_dir, os.path.basename(fp))
            _shutil.copy2(fp, dest)

        wl = WinstonLutz(tmp_dir)
        wl.analyze(bb_size_mm=5)
        summary = wl.results()

        # pylinac >= 3.6 removed WinstonLutz.passed — derive from tolerance
        WL_TOLERANCE_MM = 2.0
        try:
            _rd = wl.results_data()
            passed = float(_rd.max_2d_cax_to_bb_mm) <= WL_TOLERANCE_MM
        except Exception:
            passed = False

        # pylinac v3.43: correct save method is save_summary(), not save_summary_plot()
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

        jobs[job_id] = {
            "status":           "Success",
            "passed":           passed,
            "analysis_summary": summary,
            "image_url":        image_url,
            "chart_data":       chart_data,
        }

    except Exception as e:
        jobs[job_id] = {
            "status":  "Error",
            "message": f"Winston-Lutz analysis failed: {e}",
        }
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
    jobs[job_id] = {"status": "Processing", "_ts": time.time()}

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

        # Radial profile around the determined wobble circle
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

        jobs[job_id] = {
            "status":           "Success",
            "passed":           passed,
            "analysis_summary": summary,
            "image_url":        image_url,
            "chart_data":       chart_data,
        }

    except Exception as e:
        jobs[job_id] = {
            "status":  "Error",
            "message": f"Starshot analysis failed: {e}",
        }
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

    jobs[job_id] = {"status": "Processing", "_ts": time.time()}

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
# (full implementation in congruencebackend.py — pasted here verbatim)
# =============================================================================

def _extract_congruence_chart_data(fa) -> dict:
    """Extract edge offsets and profiles from a pylinac 3.42 FieldAnalysis result.

    In pylinac 3.42 the DeviceResult / FieldResult has explicit named fields:
      cax_to_top_mm, cax_to_bottom_mm, cax_to_left_mm, cax_to_right_mm
      field_size_vertical_mm, field_size_horizontal_mm
      top_penumbra_mm, bottom_penumbra_mm, left_penumbra_mm, right_penumbra_mm
    """
    try:
        rd = fa.results_data()   # returns FieldResult pydantic model

        # ── Edge offsets from CAX (signed, mm) ───────────────────────────────
        edges = {
            "top":    round(float(rd.cax_to_top_mm),    3),
            "bottom": round(float(rd.cax_to_bottom_mm), 3),
            "left":   round(float(rd.cax_to_left_mm),   3),
            "right":  round(float(rd.cax_to_right_mm),  3),
        }

        # ── Field size ────────────────────────────────────────────────────────
        field_size = {
            "vertical_mm":   round(float(rd.field_size_vertical_mm),   2),
            "horizontal_mm": round(float(rd.field_size_horizontal_mm), 2),
        }

        # ── Penumbra widths (mm) ──────────────────────────────────────────────
        penumbra = {
            "top":    round(float(rd.top_penumbra_mm),    3),
            "bottom": round(float(rd.bottom_penumbra_mm), 3),
            "left":   round(float(rd.left_penumbra_mm),   3),
            "right":  round(float(rd.right_penumbra_mm),  3),
        }

        # ── Inline / crossline profiles ───────────────────────────────────────
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
        # Protocol.NONE skips symmetry/flatness — appropriate for a congruence test
        # where we only care about field edge positions.
        fa.analyze(
            protocol  = Protocol.NONE,
            is_FFF    = False,
        )

        summary = fa.results()

        # FieldAnalysis has no .passed — derive from edge offsets vs tolerance
        chart_data = _extract_congruence_chart_data(fa)
        tol = 2.0  # mm — standard radiation/light congruence tolerance
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

        jobs[job_id] = {
            "status":           "Success",
            "passed":           passed,
            "analysis_summary": summary,
            "image_url":        image_url,
            "chart_data":       chart_data,
        }

    except Exception as e:
        jobs[job_id] = {
            "status":  "Error",
            "message": f"Congruence analysis failed: {e}",
        }
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

    jobs[job_id] = {"status": "Processing", "_ts": time.time()}

    background_tasks.add_task(
        _run_congruence,
        job_id   = job_id,
        filepath = filepath,
        email    = u["email"],
        filename = file.filename,
    )

    return {"status": "Queued", "job_id": job_id}
