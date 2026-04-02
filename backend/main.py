from fastapi import FastAPI, UploadFile, File, BackgroundTasks, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from jose import JWTError, jwt
from datetime import datetime, timedelta
from pylinac import PicketFence, WinstonLutz, Starshot, FieldAnalysis
import os, shutil, tempfile, uuid, hashlib, hmac
import bcrypt
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
# Pre-warm font cache so it doesn't block port binding on cold start
try:
    plt.plot([])
    plt.close()
except Exception:
    pass
import httpx

SECRET_KEY   = os.environ.get("SECRET_KEY", "mlcqa-change-this-in-render")
ALGORITHM    = "HS256"
TOKEN_HOURS  = 24

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL:
    raise RuntimeError(
        "SUPABASE_URL environment variable is not set. "
        "Add it in Render → Environment → Add Environment Variable."
    )
if not SUPABASE_KEY:
    raise RuntimeError(
        "SUPABASE_KEY environment variable is not set. "
        "Add it in Render → Environment → Add Environment Variable."
    )

# ── Supabase helpers ──────────────────────────────────────────────────────────

def sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=representation"}

def sb_storage_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "image/png"}

def get_user_by_email(email):
    with httpx.Client() as c:
        r = c.get(f"{SUPABASE_URL}/rest/v1/users?email=eq.{email}&limit=1", headers=sb_headers())
    return r.json()[0] if r.status_code == 200 and r.json() else None

def create_user(name, email, pw):
    with httpx.Client() as c:
        r = c.post(f"{SUPABASE_URL}/rest/v1/users", headers=sb_headers(),
                   json={"name": name, "email": email, "password": pw})
    if r.status_code in (200, 201):
        d = r.json(); return d[0] if isinstance(d, list) else d
    raise HTTPException(500, f"Could not create user: {r.text}")

def upload_plot(image_path, fname):
    try:
        with open(image_path, "rb") as f:
            b = f.read()
        with httpx.Client() as c:
            r = c.post(f"{SUPABASE_URL}/storage/v1/object/plots/{fname}",
                       headers=sb_storage_headers(), content=b)
        return f"{SUPABASE_URL}/storage/v1/object/public/plots/{fname}" if r.status_code in (200, 201) else None
    except:
        return None

def save_analysis(email, test_type, filename, passed, summary, image_url=None, chart_data=None, job_id=None):
    with httpx.Client() as c:
        c.post(f"{SUPABASE_URL}/rest/v1/analyses", headers=sb_headers(),
               json={"user_email": email, "test_type": test_type, "filename": filename,
                     "passed": passed, "summary": summary, "image_url": image_url,
                     "chart_data": chart_data, "job_id": job_id})

def get_job_result(job_id):
    with httpx.Client() as c:
        r = c.get(f"{SUPABASE_URL}/rest/v1/analyses?job_id=eq.{job_id}&limit=1",
                  headers=sb_headers())
    if r.status_code == 200 and r.json():
        row = r.json()[0]
        return {
            "status": "Success",
            "passed": row.get("passed"),
            "analysis_summary": row.get("summary"),
            "image_url": row.get("image_url"),
            "chart_data": row.get("chart_data") or {},
        }
    return None

def get_user_analyses(email):
    with httpx.Client() as c:
        r = c.get(f"{SUPABASE_URL}/rest/v1/analyses?user_email=eq.{email}&order=created_at.desc&limit=50",
                  headers=sb_headers())
    return r.json() if r.status_code == 200 else []

# ── Auth helpers ──────────────────────────────────────────────────────────────

def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def verify_password(plain: str, hashed: str) -> bool:
    try:
        if hashed.startswith("$2b$") or hashed.startswith("$2a$"):
            return bcrypt.checkpw(plain.encode(), hashed.encode())
        legacy = hmac.new(SECRET_KEY.encode(), plain.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(legacy, hashed):
            return True
        default = hmac.new(b"mlcqa-change-this-in-render", plain.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(default, hashed)
    except Exception:
        return False

bearer_scheme = HTTPBearer()

def create_token(email, name):
    return jwt.encode({"sub": email, "name": name, "exp": datetime.utcnow() + timedelta(hours=TOKEN_HOURS)},
                      SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(creds: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    try:
        p = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        if not p.get("sub"):
            raise HTTPException(401, "Invalid token")
        return {"email": p["sub"], "name": p.get("name")}
    except JWTError:
        raise HTTPException(401, "Invalid or expired token")

# ── Pydantic models ───────────────────────────────────────────────────────────

class SignupRequest(BaseModel): name: str; email: str; password: str
class LoginRequest(BaseModel): email: str; password: str
class ForgotPasswordRequest(BaseModel): email: str

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

from fastapi.responses import JSONResponse
from fastapi.requests import Request

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"status": "Error", "message": "An unexpected server error occurred."},
        headers={"Access-Control-Allow-Origin": "*"},
    )

jobs = {}

def cleanup():
    if len(jobs) > 50:
        for k in list(jobs.keys())[:len(jobs) - 50]:
            del jobs[k]

# ── Basic routes ──────────────────────────────────────────────────────────────

@app.get("/")
@app.head("/")
def home():
    return {"status": "MLC QA Backend is Live and listening."}

@app.get("/config/branding")
def get_branding():
    return {
        "institution_name": "University Medical Center",
        "department": "Department of Medical Physics",
        "logo_url": "https://img.icons8.com/ios-filled/100/ffffff/hospital-placeholder.png",
        "report_footer": "CONFIDENTIAL: Clinical Quality Assurance Report"
    }

# ── Auth routes ───────────────────────────────────────────────────────────────

@app.post("/auth/forgot-password")
def forgot_password(req: ForgotPasswordRequest):
    user = get_user_by_email(req.email)
    if not user:
        return {"message": "If that email is registered, you will receive a reset link."}
    print(f"PASSWORD RESET REQUEST FOR: {req.email}")
    return {"message": "Recovery instructions sent. Check your inbox."}

@app.post("/auth/signup")
def signup(req: SignupRequest):
    if not req.name.strip():
        raise HTTPException(400, "Name required.")
    if len(req.password) < 6:
        raise HTTPException(400, "Password min 6 chars.")
    if get_user_by_email(req.email):
        raise HTTPException(400, "Email already registered.")
    create_user(req.name.strip(), req.email, hash_password(req.password))
    return {"message": "Account created successfully."}

@app.post("/auth/login")
def login(req: LoginRequest):
    user = get_user_by_email(req.email)
    if not user or not verify_password(req.password, user["password"]):
        raise HTTPException(401, "Invalid email or password.")
    stored = user["password"]
    if not (stored.startswith("$2b$") or stored.startswith("$2a$")):
        new_hash = hash_password(req.password)
        try:
            with httpx.Client() as c:
                c.patch(f"{SUPABASE_URL}/rest/v1/users?email=eq.{req.email}",
                        headers=sb_headers(), json={"password": new_hash})
        except Exception:
            pass
    return {"access_token": create_token(req.email, user["name"]), "token_type": "bearer", "name": user["name"]}

@app.get("/auth/me")
def get_me(u=Depends(get_current_user)):
    return {"email": u["email"], "name": u["name"]}

@app.get("/history")
def get_history(u=Depends(get_current_user)):
    return {"analyses": get_user_analyses(u["email"])}

@app.get("/result/{job_id}")
def get_result(job_id, u=Depends(get_current_user)):
    if job_id in jobs:
        return jobs[job_id]
    db_result = get_job_result(job_id)
    if db_result:
        return db_result
    return {"status": "Error", "message": "Job not found. The server may have restarted — please re-upload your file."}

# ═════════════════════════════════════════════════════════════════════════════
# PICKET FENCE
# ═════════════════════════════════════════════════════════════════════════════

def _extract_pf_chart_data(pf) -> dict:
    """Extract per-leaf-pair errors and summary metrics from a PicketFence result."""
    try:
        results = pf.results_data()
        leaf_pairs = []
        try:
            for lp in results.mlc_meas:
                leaf_pairs.append({
                    "leaf_pair": int(lp.leaf_number),
                    "max_error_mm": round(float(lp.max_deviation), 4),
                    "passed": bool(lp.passed),
                })
        except Exception:
            pass

        max_error  = round(float(results.max_error_mm),  4) if hasattr(results, "max_error_mm")  else None
        mean_error = round(float(results.mean_error_mm), 4) if hasattr(results, "mean_error_mm") else None
        failed     = int(results.num_failed) if hasattr(results, "num_failed") else None

        return {
            "leaf_pairs":   leaf_pairs,
            "max_error":    max_error,
            "mean_error":   mean_error,
            "failed_leaves": failed,
        }
    except Exception as e:
        return {"error": str(e)}


def _run_picket_fence(job_id: str, filepath: str, email: str, filename: str):
    try:
        pf = PicketFence(filepath)
        pf.analyze(tolerance=1.0, action_tolerance=0.5)

        summary   = pf.results()
        passed    = pf.passed

        plot_path = filepath.replace(".dcm", "_pf.png")
        pf.plot_analyzed_image(filename=plot_path, show=False)
        image_url = upload_plot(plot_path, f"pf_{job_id}.png")

        chart_data = _extract_pf_chart_data(pf)

        save_analysis(email=email, test_type="Picket Fence", filename=filename,
                      passed=passed, summary=summary, image_url=image_url,
                      chart_data=chart_data, job_id=job_id)

        jobs[job_id] = {
            "status": "Success",
            "passed": passed,
            "analysis_summary": summary,
            "image_url": image_url,
            "chart_data": chart_data,
        }
    except Exception as e:
        jobs[job_id] = {"status": "Error", "message": f"Picket Fence analysis failed: {e}"}
    finally:
        try:
            os.remove(filepath)
        except Exception:
            pass
        cleanup()


@app.post("/analyze")
async def analyze_picket_fence(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    u=Depends(get_current_user),
):
    if not file.filename.lower().endswith(".dcm"):
        raise HTTPException(400, "Only .dcm DICOM files are supported.")

    job_id   = str(uuid.uuid4())
    tmp_dir  = tempfile.gettempdir()
    filepath = os.path.join(tmp_dir, f"pf_{job_id}.dcm")

    contents = await file.read()
    with open(filepath, "wb") as f:
        f.write(contents)

    jobs[job_id] = {"status": "Processing"}
    background_tasks.add_task(_run_picket_fence, job_id=job_id, filepath=filepath,
                               email=u["email"], filename=file.filename)
    return {"status": "Queued", "job_id": job_id}


# ═════════════════════════════════════════════════════════════════════════════
# STARSHOT
# ═════════════════════════════════════════════════════════════════════════════

def _extract_starshot_chart_data(ss) -> dict:
    try:
        results = ss.results_data()
        spokes = []
        try:
            for spoke in results.spokes:
                spokes.append({
                    "angle":       round(float(spoke.angle), 2),
                    "residual_mm": round(float(spoke.residual_mm), 4),
                })
        except Exception:
            pass

        wobble = None
        try:
            wobble = round(float(results.circle_profile.radius), 4)
        except Exception:
            try:
                wobble = round(float(results.wobble_radius_mm), 4)
            except Exception:
                pass

        radial_profile = []
        try:
            import numpy as np
            arr = ss.image.array.astype(float)
            cy, cx = arr.shape[0] // 2, arr.shape[1] // 2
            row = arr[cy, :]
            mn, mx = row.min(), row.max()
            if mx > mn:
                row = (row - mn) / (mx - mn)
            step = max(1, len(row) // 100)
            radial_profile = [round(float(v), 4) for v in row[::step]][:100]
        except Exception:
            pass

        return {"spokes": spokes, "wobble_radius": wobble, "radial_profile": radial_profile}
    except Exception as e:
        return {"error": str(e)}


def _run_starshot(job_id: str, filepath: str, email: str, filename: str):
    try:
        ss = Starshot(filepath)
        ss.analyze()

        summary   = ss.results()
        passed    = ss.passed

        plot_path = filepath.replace(".dcm", "_ss.png").replace(".zip", "_ss.png")
        ss.plot_analyzed_image(filename=plot_path, show=False)
        image_url = upload_plot(plot_path, f"ss_{job_id}.png")

        chart_data = _extract_starshot_chart_data(ss)

        save_analysis(email=email, test_type="Starshot", filename=filename,
                      passed=passed, summary=summary, image_url=image_url,
                      chart_data=chart_data, job_id=job_id)

        jobs[job_id] = {
            "status": "Success",
            "passed": passed,
            "analysis_summary": summary,
            "image_url": image_url,
            "chart_data": chart_data,
        }
    except Exception as e:
        jobs[job_id] = {"status": "Error", "message": f"Starshot analysis failed: {e}"}
    finally:
        try:
            os.remove(filepath)
        except Exception:
            pass
        cleanup()


@app.post("/analyze/starshot")
async def analyze_starshot(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    u=Depends(get_current_user),
):
    ext = file.filename.lower().rsplit(".", 1)[-1]
    if ext not in ("dcm", "zip"):
        raise HTTPException(400, "Only .dcm or .zip files are supported for Starshot.")

    job_id   = str(uuid.uuid4())
    tmp_dir  = tempfile.gettempdir()
    filepath = os.path.join(tmp_dir, f"ss_{job_id}.{ext}")

    contents = await file.read()
    with open(filepath, "wb") as f:
        f.write(contents)

    jobs[job_id] = {"status": "Processing"}
    background_tasks.add_task(_run_starshot, job_id=job_id, filepath=filepath,
                               email=u["email"], filename=file.filename)
    return {"status": "Queued", "job_id": job_id}


# ═════════════════════════════════════════════════════════════════════════════
# WINSTON-LUTZ
# ═════════════════════════════════════════════════════════════════════════════

def _extract_wl_chart_data(wl) -> dict:
    try:
        results = wl.results_data()
        images  = []
        try:
            for img in results.image_details:
                images.append({
                    "gantry_angle":     round(float(img.gantry_angle), 1),
                    "bb_offset_mm":     round(float(img.bb_offset_mm), 4),
                    "passed":           bool(img.passed),
                })
        except Exception:
            pass

        max_offset  = None
        mean_offset = None
        try:
            max_offset  = round(float(results.max_bb_deviation_2d), 4)
            mean_offset = round(float(results.mean_bb_deviation_2d), 4)
        except Exception:
            pass

        return {"images": images, "max_offset_mm": max_offset, "mean_offset_mm": mean_offset}
    except Exception as e:
        return {"error": str(e)}


def _run_winston_lutz(job_id: str, dirpath: str, email: str, filename: str):
    try:
        wl = WinstonLutz(dirpath)
        wl.analyze()

        summary   = wl.results()
        passed    = wl.passed

        plot_path = os.path.join(dirpath, f"wl_{job_id}.png")
        wl.plot_summary(filename=plot_path, show=False)
        image_url = upload_plot(plot_path, f"wl_{job_id}.png")

        chart_data = _extract_wl_chart_data(wl)

        save_analysis(email=email, test_type="Winston-Lutz", filename=filename,
                      passed=passed, summary=summary, image_url=image_url,
                      chart_data=chart_data, job_id=job_id)

        jobs[job_id] = {
            "status": "Success",
            "passed": passed,
            "analysis_summary": summary,
            "image_url": image_url,
            "chart_data": chart_data,
        }
    except Exception as e:
        jobs[job_id] = {"status": "Error", "message": f"Winston-Lutz analysis failed: {e}"}
    finally:
        try:
            shutil.rmtree(dirpath, ignore_errors=True)
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
        raise HTTPException(400, "At least one .dcm file is required.")

    job_id  = str(uuid.uuid4())
    dirpath = os.path.join(tempfile.gettempdir(), f"wl_{job_id}")
    os.makedirs(dirpath, exist_ok=True)

    for upload in files:
        if not upload.filename.lower().endswith(".dcm"):
            raise HTTPException(400, "All files must be .dcm DICOM files.")
        contents = await upload.read()
        with open(os.path.join(dirpath, upload.filename), "wb") as f:
            f.write(contents)

    jobs[job_id] = {"status": "Processing"}
    background_tasks.add_task(_run_winston_lutz, job_id=job_id, dirpath=dirpath,
                               email=u["email"], filename=f"{len(files)} images")
    return {"status": "Queued", "job_id": job_id}


# ═════════════════════════════════════════════════════════════════════════════
# CONGRUENCE (Radiation–Light Field)
# ═════════════════════════════════════════════════════════════════════════════

def _extract_congruence_chart_data(fa) -> dict:
    try:
        results = fa.results_data()
        edges   = {}

        try:
            attrs = vars(results) if hasattr(results, "__dict__") else {}

            def _get(candidates):
                for name in candidates:
                    v = attrs.get(name) or getattr(results, name, None)
                    if v is not None:
                        return round(float(v), 3)
                return None

            top    = _get(["top_penumbra_mm",    "top_field_edge_mm",    "top_mm"])
            bottom = _get(["bottom_penumbra_mm",  "bottom_field_edge_mm", "bottom_mm"])
            left   = _get(["left_penumbra_mm",    "left_field_edge_mm",   "left_mm"])
            right  = _get(["right_penumbra_mm",   "right_field_edge_mm",  "right_mm"])

            edges = {"top": top, "bottom": bottom, "left": left, "right": right}
        except Exception:
            edges = {"top": None, "bottom": None, "left": None, "right": None}

        inline_profile, crossline_profile = [], []
        try:
            import numpy as np
            arr = fa.image.array.astype(float)
            mn, mx = arr.min(), arr.max()
            if mx > mn:
                arr = (arr - mn) / (mx - mn)
            cy, cx = arr.shape[0] // 2, arr.shape[1] // 2
            step_i = max(1, arr.shape[1] // 100)
            step_c = max(1, arr.shape[0] // 100)
            inline_profile    = [round(float(v), 4) for v in arr[cy, ::step_i]][:100]
            crossline_profile = [round(float(v), 4) for v in arr[::step_c, cx]][:100]
        except Exception:
            pass

        return {
            "edges":              edges,
            "inline_profile":     inline_profile,
            "crossline_profile":  crossline_profile,
            "tolerance_mm":       2.0,
        }
    except Exception as e:
        return {"error": str(e), "edges": {}, "inline_profile": [], "crossline_profile": [], "tolerance_mm": 2.0}


def _run_congruence(job_id: str, filepath: str, email: str, filename: str):
    try:
        fa = FieldAnalysis(filepath)
        fa.analyze(protocol=None, is_FFF=False)

        summary   = fa.results()
        passed    = fa.passed

        plot_path = filepath.replace(".dcm", "_congruence.png")
        fa.plot_analyzed_image(filename=plot_path, show=False)
        image_url = upload_plot(plot_path, f"congruence_{job_id}.png")

        chart_data = _extract_congruence_chart_data(fa)

        save_analysis(email=email, test_type="Congruence", filename=filename,
                      passed=passed, summary=summary, image_url=image_url,
                      chart_data=chart_data, job_id=job_id)

        jobs[job_id] = {
            "status": "Success",
            "passed": passed,
            "analysis_summary": summary,
            "image_url": image_url,
            "chart_data": chart_data,
        }
    except Exception as e:
        jobs[job_id] = {"status": "Error", "message": f"Congruence analysis failed: {e}"}
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

    jobs[job_id] = {"status": "Processing"}
    background_tasks.add_task(_run_congruence, job_id=job_id, filepath=filepath,
                               email=u["email"], filename=file.filename)
    return {"status": "Queued", "job_id": job_id}
