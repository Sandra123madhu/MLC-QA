from fastapi import FastAPI, UploadFile, File, BackgroundTasks, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from jose import JWTError, jwt
from datetime import datetime, timedelta
from pylinac import PicketFence, WinstonLutz, Starshot
import os, shutil, tempfile, uuid, hashlib, hmac
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import httpx

# ─── Config ───────────────────────────────────────────────────────────────────
SECRET_KEY   = os.environ.get("SECRET_KEY", "mlcqa-change-this-in-render")
ALGORITHM    = "HS256"
TOKEN_HOURS  = 24
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://swxrncaezcthahehhuu.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# ─── Supabase helpers ─────────────────────────────────────────────────────────
def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def sb_storage_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "image/png"
    }

def get_user_by_email(email: str):
    with httpx.Client() as client:
        res = client.get(f"{SUPABASE_URL}/rest/v1/users?email=eq.{email}&limit=1", headers=sb_headers())
    if res.status_code == 200 and res.json():
        return res.json()[0]
    return None

def create_user(name: str, email: str, hashed_password: str):
    with httpx.Client() as client:
        res = client.post(f"{SUPABASE_URL}/rest/v1/users", headers=sb_headers(),
                          json={"name": name, "email": email, "password": hashed_password})
    if res.status_code in (200, 201):
        data = res.json()
        return data[0] if isinstance(data, list) else data
    raise HTTPException(status_code=500, detail=f"Could not create user: {res.text}")

def upload_plot_to_supabase(image_path: str, plot_filename: str):
    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        with httpx.Client() as client:
            res = client.post(f"{SUPABASE_URL}/storage/v1/object/plots/{plot_filename}",
                              headers=sb_storage_headers(), content=image_bytes)
        if res.status_code in (200, 201):
            return f"{SUPABASE_URL}/storage/v1/object/public/plots/{plot_filename}"
        return None
    except Exception:
        return None

def save_analysis(user_email, test_type, filename, passed, summary, image_url=None):
    with httpx.Client() as client:
        client.post(f"{SUPABASE_URL}/rest/v1/analyses", headers=sb_headers(),
                    json={"user_email": user_email, "test_type": test_type,
                          "filename": filename, "passed": passed,
                          "summary": summary, "image_url": image_url})

def get_user_analyses(user_email: str):
    with httpx.Client() as client:
        res = client.get(
            f"{SUPABASE_URL}/rest/v1/analyses?user_email=eq.{user_email}&order=created_at.desc&limit=50",
            headers=sb_headers())
    return res.json() if res.status_code == 200 else []

# ─── Auth helpers ─────────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    return hmac.new(SECRET_KEY.encode(), password.encode(), hashlib.sha256).hexdigest()

def verify_password(plain: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_password(plain), hashed)

bearer_scheme = HTTPBearer()

def create_token(email: str, name: str) -> str:
    payload = {"sub": email, "name": name,
                "exp": datetime.utcnow() + timedelta(hours=TOKEN_HOURS)}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub"); name = payload.get("name")
        if not email:
            raise HTTPException(status_code=401, detail="Invalid token")
        return {"email": email, "name": name}
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

# ─── Schemas ──────────────────────────────────────────────────────────────────
class SignupRequest(BaseModel):
    name: str; email: str; password: str

class LoginRequest(BaseModel):
    email: str; password: str

# ─── App ──────────────────────────────────────────────────────────────────────
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

jobs = {}
MAX_JOBS = 50

def cleanup_old_jobs():
    if len(jobs) > MAX_JOBS:
        for k in list(jobs.keys())[:len(jobs) - MAX_JOBS]:
            del jobs[k]

# ─── Health ───────────────────────────────────────────────────────────────────
@app.get("/")
@app.head("/")
def home():
    return {"status": "MLC QA Backend is Live and listening."}

# ─── Auth ─────────────────────────────────────────────────────────────────────
@app.post("/auth/signup")
def signup(req: SignupRequest):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Name is required.")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    if get_user_by_email(req.email):
        raise HTTPException(status_code=400, detail="Email already registered.")
    create_user(req.name.strip(), req.email, hash_password(req.password))
    return {"message": "Account created successfully."}

@app.post("/auth/login")
def login(req: LoginRequest):
    user = get_user_by_email(req.email)
    if not user or not verify_password(req.password, user["password"]):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token = create_token(req.email, user["name"])
    return {"access_token": token, "token_type": "bearer", "name": user["name"]}

@app.get("/auth/me")
def get_me(current_user: dict = Depends(get_current_user)):
    return {"email": current_user["email"], "name": current_user["name"]}

@app.get("/history")
def get_history(current_user: dict = Depends(get_current_user)):
    return {"analyses": get_user_analyses(current_user["email"])}

@app.get("/result/{job_id}")
def get_result(job_id: str, current_user: dict = Depends(get_current_user)):
    if job_id not in jobs:
        return {"status": "Error", "message": "Job ID not found."}
    return jobs[job_id]

# ═══════════════════════════════════════════════════════════════════════════════
# PICKET FENCE — with real leaf data
# ═══════════════════════════════════════════════════════════════════════════════
def run_analysis(job_id: str, temp_path: str, user_email: str, filename: str):
    plot_path = None
    try:
        pf = PicketFence(temp_path)
        pf.analyze(tolerance=0.5, action_tolerance=0.25)
        summary = pf.results()
        passed  = pf.passed

        # ── Extract real leaf-level error data ──
        chart_data = {}
        try:
            # mlc_meas is a list of MLC measurements per picket
            # Each has .error (array of per-leaf errors in mm)
            all_errors = []   # shape: [n_leaves]
            picket_means = [] # shape: [n_pickets]

            mlc_meas = pf.mlc_meas  # list of pickets
            n_pickets = len(mlc_meas)
            n_leaves  = len(mlc_meas[0]) if n_pickets > 0 else 0

            # Build per-leaf max error across all pickets
            leaf_max = []
            for leaf_idx in range(n_leaves):
                max_err = max(abs(mlc_meas[p][leaf_idx].error) for p in range(n_pickets))
                leaf_max.append(round(float(max_err), 4))

            # Build per-picket mean error
            for p in range(n_pickets):
                mean_err = sum(abs(mlc_meas[p][l].error) for l in range(n_leaves)) / n_leaves
                picket_means.append(round(float(mean_err), 4))

            # Overall metrics
            flat = [abs(mlc_meas[p][l].error) for p in range(n_pickets) for l in range(n_leaves)]
            max_err_overall = round(float(max(flat)), 4)
            mean_err_overall = round(float(sum(flat) / len(flat)), 4)
            failed_leaves = int(sum(1 for v in flat if v > 0.5))

            chart_data = {
                "leaf_max_errors": leaf_max,
                "picket_mean_errors": picket_means,
                "max_error": max_err_overall,
                "mean_error": mean_err_overall,
                "failed_leaves": failed_leaves,
                "n_leaves": n_leaves,
                "n_pickets": n_pickets,
            }
        except Exception as ex:
            chart_data = {"error": f"Chart data extraction failed: {str(ex)}"}

        # ── Plot ──
        plot_filename = f"{job_id}.png"
        plot_path     = f"/tmp/{plot_filename}"
        pf.save_analyzed_image(plot_path)
        plt.close("all")

        image_url = upload_plot_to_supabase(plot_path, plot_filename)

        jobs[job_id] = {
            "status": "Success",
            "passed": passed,
            "analysis_summary": summary,
            "image_url": image_url,
            "chart_data": chart_data
        }
        save_analysis(user_email=user_email, test_type="Picket Fence",
                      filename=filename, passed=passed, summary=summary, image_url=image_url)

    except Exception as e:
        jobs[job_id] = {"status": "Error", "message": f"Analysis Error: {str(e)}"}
    finally:
        for path in [temp_path, plot_path]:
            try:
                if path and os.path.exists(path): os.remove(path)
            except Exception: pass
        cleanup_old_jobs()

@app.post("/analyze")
async def analyze_mlc(background_tasks: BackgroundTasks,
                       file: UploadFile = File(...),
                       current_user: dict = Depends(get_current_user)):
    if not file.filename.lower().endswith(".dcm"):
        return {"status": "Error", "message": "Only DICOM (.dcm) files are supported."}
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".dcm", dir="/tmp") as tmp:
            shutil.copyfileobj(file.file, tmp)
            temp_path = tmp.name
        job_id = str(uuid.uuid4())
        jobs[job_id] = {"status": "Processing"}
        background_tasks.add_task(run_analysis, job_id, temp_path, current_user["email"], file.filename)
        return {"status": "Processing", "job_id": job_id}
    except Exception as e:
        return {"status": "Error", "message": f"Upload Error: {str(e)}"}

# ═══════════════════════════════════════════════════════════════════════════════
# WINSTON-LUTZ — with real per-image offset data
# ═══════════════════════════════════════════════════════════════════════════════
def run_winston_lutz(job_id: str, temp_path: str, user_email: str, filename: str):
    plot_path = None
    extract_dir = None
    try:
        if temp_path.endswith(".zip"):
            import zipfile
            extract_dir = temp_path + "_extracted"
            os.makedirs(extract_dir, exist_ok=True)
            with zipfile.ZipFile(temp_path, "r") as z:
                z.extractall(extract_dir)
            wl = WinstonLutz(extract_dir)
        else:
            wl = WinstonLutz(os.path.dirname(temp_path))

        wl.analyze()
        summary = wl.results()
        passed  = wl.passed

        # ── Extract real per-image offset data ──
        chart_data = {}
        try:
            images_data = []
            for img in wl.images:
                gt  = round(float(img.bb_deviation_2d.x), 4)  # Gt offset mm
                lt  = round(float(img.bb_deviation_2d.y), 4)  # Lt offset mm
                err = round(float(img.bb_deviation_2d.magnitude), 4)
                gantry = round(float(img.gantry_angle), 1)
                images_data.append({"gantry": gantry, "gt": gt, "lt": lt, "error_2d": err})

            max_2d  = round(float(max(d["error_2d"] for d in images_data)), 4)
            mean_2d = round(float(sum(d["error_2d"] for d in images_data) / len(images_data)), 4)

            # 3D isocenter radius
            try:
                iso_radius = round(float(wl.cax2bb_distance("max")), 4)
            except Exception:
                iso_radius = max_2d

            chart_data = {
                "images": images_data,
                "max_2d_error": max_2d,
                "mean_2d_error": mean_2d,
                "iso_radius": iso_radius,
            }
        except Exception as ex:
            chart_data = {"error": f"Chart data extraction failed: {str(ex)}"}

        # ── Plot ──
        plot_filename = f"{job_id}.png"
        plot_path     = f"/tmp/{plot_filename}"
        wl.save_summary_plot(plot_path)
        plt.close("all")

        image_url = upload_plot_to_supabase(plot_path, plot_filename)

        jobs[job_id] = {
            "status": "Success",
            "passed": passed,
            "analysis_summary": summary,
            "image_url": image_url,
            "chart_data": chart_data
        }
        save_analysis(user_email=user_email, test_type="Winston-Lutz",
                      filename=filename, passed=passed, summary=summary, image_url=image_url)

    except Exception as e:
        jobs[job_id] = {"status": "Error", "message": f"Analysis Error: {str(e)}"}
    finally:
        for path in [temp_path, plot_path]:
            try:
                if path and os.path.exists(path): os.remove(path)
            except Exception: pass
        if extract_dir:
            try: shutil.rmtree(extract_dir, ignore_errors=True)
            except Exception: pass
        cleanup_old_jobs()

@app.post("/analyze/winston-lutz")
async def analyze_winston_lutz(background_tasks: BackgroundTasks,
                                file: UploadFile = File(None),
                                files: list[UploadFile] = File(None),
                                current_user: dict = Depends(get_current_user)):
    try:
        # Single zip file
        if file and file.filename.lower().endswith(".zip"):
            with tempfile.NamedTemporaryFile(delete=False, suffix=".zip", dir="/tmp") as tmp:
                shutil.copyfileobj(file.file, tmp)
                temp_path = tmp.name
        # Multiple DCM files
        elif files:
            import zipfile
            zip_path = f"/tmp/{uuid.uuid4()}.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                for f in files:
                    content = await f.read()
                    zf.writestr(f.filename, content)
            temp_path = zip_path
            filename  = f"{len(files)}_images.zip"
        elif file:
            ext = file.filename.lower().split(".")[-1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}", dir="/tmp") as tmp:
                shutil.copyfileobj(file.file, tmp)
                temp_path = tmp.name
        else:
            return {"status": "Error", "message": "No files provided."}

        fname = file.filename if file else filename
        job_id = str(uuid.uuid4())
        jobs[job_id] = {"status": "Processing"}
        background_tasks.add_task(run_winston_lutz, job_id, temp_path, current_user["email"], fname)
        return {"status": "Processing", "job_id": job_id}
    except Exception as e:
        return {"status": "Error", "message": f"Upload Error: {str(e)}"}

# ═══════════════════════════════════════════════════════════════════════════════
# STARSHOT — with real wobble radius + spoke data
# ═══════════════════════════════════════════════════════════════════════════════
def run_starshot(job_id: str, temp_path: str, user_email: str, filename: str):
    plot_path = None
    try:
        if temp_path.endswith(".zip"):
            ss = Starshot.from_zip(temp_path)
        else:
            ss = Starshot(temp_path)

        ss.analyze(tolerance=1.0)
        summary = ss.results()
        passed  = ss.passed

        # ── Extract real wobble radius + spoke data ──
        chart_data = {}
        try:
            wobble_radius = round(float(ss.wobble.radius.mm), 4)
            wobble_center = {
                "x": round(float(ss.wobble.center.x), 2),
                "y": round(float(ss.wobble.center.y), 2)
            }

            # Extract spoke lines — each spoke has an angle
            spokes = []
            for i, line in enumerate(ss.lines):
                angle = round(float(line.angle.degrees), 2)
                # Distance from wobble center to spoke line (residual)
                dist  = round(float(line.distance_to(ss.wobble.center)), 4)
                spokes.append({"index": i, "angle": angle, "residual_mm": dist})

            # Radial profile from image array
            import numpy as np
            img_arr = ss.image.array.astype(float)
            cy = int(ss.wobble.center.y)
            cx = int(ss.wobble.center.x)
            max_r = min(cy, cx, img_arr.shape[0]-cy, img_arr.shape[1]-cx, 150)
            radial = []
            for r in range(1, max_r):
                # sample 36 points around circle of radius r
                angles = np.linspace(0, 2*np.pi, 36, endpoint=False)
                vals = []
                for a in angles:
                    xi = int(cx + r*np.cos(a))
                    yi = int(cy + r*np.sin(a))
                    if 0 <= xi < img_arr.shape[1] and 0 <= yi < img_arr.shape[0]:
                        vals.append(img_arr[yi, xi])
                if vals:
                    radial.append(round(float(np.mean(vals)), 2))

            # Normalise 0-1
            if radial:
                mx = max(radial); mn = min(radial)
                if mx > mn:
                    radial = [round((v-mn)/(mx-mn), 4) for v in radial]

            chart_data = {
                "wobble_radius": wobble_radius,
                "wobble_center": wobble_center,
                "spokes": spokes,
                "radial_profile": radial[:100],  # cap at 100 pts
            }
        except Exception as ex:
            chart_data = {"error": f"Chart data extraction failed: {str(ex)}"}

        # ── Plot ──
        plot_filename = f"{job_id}.png"
        plot_path     = f"/tmp/{plot_filename}"
        ss.save_analyzed_image(plot_path)
        plt.close("all")

        image_url = upload_plot_to_supabase(plot_path, plot_filename)

        jobs[job_id] = {
            "status": "Success",
            "passed": passed,
            "analysis_summary": summary,
            "image_url": image_url,
            "chart_data": chart_data
        }
        save_analysis(user_email=user_email, test_type="Starshot",
                      filename=filename, passed=passed, summary=summary, image_url=image_url)

    except Exception as e:
        jobs[job_id] = {"status": "Error", "message": f"Analysis Error: {str(e)}"}
    finally:
        for path in [temp_path, plot_path]:
            try:
                if path and os.path.exists(path): os.remove(path)
            except Exception: pass
        cleanup_old_jobs()

@app.post("/analyze/starshot")
async def analyze_starshot(background_tasks: BackgroundTasks,
                            file: UploadFile = File(...),
                            current_user: dict = Depends(get_current_user)):
    ext = file.filename.lower().split(".")[-1]
    if ext not in ("dcm", "zip"):
        return {"status": "Error", "message": "Only .dcm or .zip files are supported."}
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}", dir="/tmp") as tmp:
            shutil.copyfileobj(file.file, tmp)
            temp_path = tmp.name
        job_id = str(uuid.uuid4())
        jobs[job_id] = {"status": "Processing"}
        background_tasks.add_task(run_starshot, job_id, temp_path, current_user["email"], file.filename)
        return {"status": "Processing", "job_id": job_id}
    except Exception as e:
        return {"status": "Error", "message": f"Upload Error: {str(e)}"}
