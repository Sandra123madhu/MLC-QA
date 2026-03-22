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
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json", "Prefer": "return=representation"}

def sb_storage_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "image/png"}

def get_user_by_email(email: str):
    with httpx.Client() as client:
        res = client.get(f"{SUPABASE_URL}/rest/v1/users?email=eq.{email}&limit=1", headers=sb_headers())
    if res.status_code == 200 and res.json():
        return res.json()[0]
    return None

def create_user(name, email, hashed_password):
    with httpx.Client() as client:
        res = client.post(f"{SUPABASE_URL}/rest/v1/users", headers=sb_headers(),
                          json={"name": name, "email": email, "password": hashed_password})
    if res.status_code in (200, 201):
        data = res.json()
        return data[0] if isinstance(data, list) else data
    raise HTTPException(status_code=500, detail=f"Could not create user: {res.text}")

def upload_plot_to_supabase(image_path, plot_filename):
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

def get_user_analyses(user_email):
    with httpx.Client() as client:
        res = client.get(
            f"{SUPABASE_URL}/rest/v1/analyses?user_email=eq.{user_email}&order=created_at.desc&limit=50",
            headers=sb_headers())
    return res.json() if res.status_code == 200 else []

# ─── Auth ─────────────────────────────────────────────────────────────────────
def hash_password(password):
    return hmac.new(SECRET_KEY.encode(), password.encode(), hashlib.sha256).hexdigest()

def verify_password(plain, hashed):
    return hmac.compare_digest(hash_password(plain), hashed)

bearer_scheme = HTTPBearer()

def create_token(email, name):
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

@app.get("/")
@app.head("/")
def home():
    return {"status": "MLC QA Backend is Live and listening."}

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
def get_result(job_id, current_user: dict = Depends(get_current_user)):
    if job_id not in jobs:
        return {"status": "Error", "message": "Job ID not found."}
    return jobs[job_id]

# ═══════════════════════════════════════════════════════════════════════════════
# PICKET FENCE
# ═══════════════════════════════════════════════════════════════════════════════
def _extract_pf_chart_data(pf):
    """Extract real leaf/picket error data from pylinac 3.x PicketFence object."""
    import numpy as np
    try:
        # pylinac 3.x: pf.results_data() returns a PicketFenceResults dataclass
        rd = pf.results_data()

        # --- leaf max errors ---
        # mlc_meas is list[list[MLCMeas]] — [picket][leaf]
        mlc = pf.mlc_meas
        n_pickets = len(mlc)
        n_leaves  = len(mlc[0]) if n_pickets else 0

        leaf_max = []
        for li in range(n_leaves):
            max_e = max(abs(float(mlc[pi][li].error)) for pi in range(n_pickets))
            leaf_max.append(round(max_e, 4))

        picket_means = []
        for pi in range(n_pickets):
            vals = [abs(float(mlc[pi][li].error)) for li in range(n_leaves)]
            picket_means.append(round(float(np.mean(vals)), 4))

        flat = [abs(float(mlc[pi][li].error))
                for pi in range(n_pickets) for li in range(n_leaves)]
        max_err  = round(float(max(flat)), 4)
        mean_err = round(float(np.mean(flat)), 4)
        failed   = int(sum(1 for v in flat if v > 0.5))

        return {
            "leaf_max_errors":    leaf_max,
            "picket_mean_errors": picket_means,
            "max_error":          max_err,
            "mean_error":         mean_err,
            "failed_leaves":      failed,
            "n_leaves":           n_leaves,
            "n_pickets":          n_pickets,
        }
    except Exception as e:
        # Fallback: try results_data() structured fields
        try:
            rd = pf.results_data()
            max_err  = round(float(rd.max_error), 4)
            mean_err = round(float(rd.mean_error), 4)
            # Build approximate leaf data from picket data
            picket_means = [round(float(p.mean_error), 4) for p in rd.pickets]
            return {
                "leaf_max_errors":    [],
                "picket_mean_errors": picket_means,
                "max_error":          max_err,
                "mean_error":         mean_err,
                "failed_leaves":      int(rd.num_failed_leaves),
                "n_leaves":           0,
                "n_pickets":          len(picket_means),
            }
        except Exception as e2:
            return {"error": f"PF extraction failed: {e} / {e2}",
                    "leaf_max_errors": [], "picket_mean_errors": [],
                    "max_error": 0, "mean_error": 0, "failed_leaves": 0}


def run_analysis(job_id, temp_path, user_email, filename):
    plot_path = None
    try:
        pf = PicketFence(temp_path)
        pf.analyze(tolerance=0.5, action_tolerance=0.25)
        summary  = pf.results()
        passed   = pf.passed
        chart_data = _extract_pf_chart_data(pf)

        plot_filename = f"{job_id}.png"
        plot_path     = f"/tmp/{plot_filename}"
        pf.save_analyzed_image(plot_path)
        plt.close("all")
        image_url = upload_plot_to_supabase(plot_path, plot_filename)

        jobs[job_id] = {"status": "Success", "passed": passed,
                        "analysis_summary": summary, "image_url": image_url,
                        "chart_data": chart_data}
        save_analysis(user_email, "Picket Fence", filename, passed, summary, image_url)
    except Exception as e:
        jobs[job_id] = {"status": "Error", "message": f"Analysis Error: {str(e)}"}
    finally:
        for p in [temp_path, plot_path]:
            try:
                if p and os.path.exists(p): os.remove(p)
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
# WINSTON-LUTZ
# ═══════════════════════════════════════════════════════════════════════════════
def _extract_wl_chart_data(wl):
    """Extract real per-image BB offset data from pylinac 3.x WinstonLutz."""
    import numpy as np
    try:
        images_data = []
        for img in wl.images:
            # pylinac 3.x: bb_shift_vector, field_cax, bb_location on image plane
            try:
                # Try the 3.x attribute names
                gt  = round(float(img.bb_shift_vector.x), 4)
                lt  = round(float(img.bb_shift_vector.y), 4)
                err = round(float(np.sqrt(gt**2 + lt**2)), 4)
            except AttributeError:
                try:
                    # Alternative: cax2bb_vector
                    vec = img.cax2bb_vector
                    gt  = round(float(vec.x), 4)
                    lt  = round(float(vec.y), 4)
                    err = round(float(vec.length), 4)
                except AttributeError:
                    gt, lt, err = 0.0, 0.0, 0.0

            try:
                gantry = round(float(img.gantry_angle), 1)
            except Exception:
                gantry = 0.0

            images_data.append({"gantry": gantry, "gt": gt, "lt": lt, "error_2d": err})

        if not images_data:
            return {"error": "No images found", "images": []}

        max_2d  = round(float(max(d["error_2d"] for d in images_data)), 4)
        mean_2d = round(float(np.mean([d["error_2d"] for d in images_data])), 4)

        # 3D isocenter radius
        try:
            iso_radius = round(float(wl.cax2bb_distance("max")), 4)
        except Exception:
            try:
                iso_radius = round(float(wl.max_2d_cax_to_bb_mm), 4)
            except Exception:
                iso_radius = max_2d

        return {"images": images_data, "max_2d_error": max_2d,
                "mean_2d_error": mean_2d, "iso_radius": iso_radius}
    except Exception as e:
        return {"error": f"WL extraction failed: {str(e)}", "images": []}


def run_winston_lutz(job_id, temp_path, user_email, filename):
    plot_path   = None
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
        summary    = wl.results()
        passed     = wl.passed
        chart_data = _extract_wl_chart_data(wl)

        plot_filename = f"{job_id}.png"
        plot_path     = f"/tmp/{plot_filename}"
        wl.save_summary_plot(plot_path)
        plt.close("all")
        image_url = upload_plot_to_supabase(plot_path, plot_filename)

        jobs[job_id] = {"status": "Success", "passed": passed,
                        "analysis_summary": summary, "image_url": image_url,
                        "chart_data": chart_data}
        save_analysis(user_email, "Winston-Lutz", filename, passed, summary, image_url)
    except Exception as e:
        jobs[job_id] = {"status": "Error", "message": f"Analysis Error: {str(e)}"}
    finally:
        for p in [temp_path, plot_path]:
            try:
                if p and os.path.exists(p): os.remove(p)
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
        fname = "wl_analysis"
        if file and file.filename.lower().endswith(".zip"):
            with tempfile.NamedTemporaryFile(delete=False, suffix=".zip", dir="/tmp") as tmp:
                shutil.copyfileobj(file.file, tmp)
                temp_path = tmp.name
            fname = file.filename
        elif files:
            import zipfile
            zip_path = f"/tmp/{uuid.uuid4()}.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                for f in files:
                    content = await f.read()
                    zf.writestr(f.filename, content)
            temp_path = zip_path
            fname = f"{len(files)}_images.zip"
        elif file:
            ext = file.filename.lower().split(".")[-1]
            with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}", dir="/tmp") as tmp:
                shutil.copyfileobj(file.file, tmp)
                temp_path = tmp.name
            fname = file.filename
        else:
            return {"status": "Error", "message": "No files provided."}

        job_id = str(uuid.uuid4())
        jobs[job_id] = {"status": "Processing"}
        background_tasks.add_task(run_winston_lutz, job_id, temp_path, current_user["email"], fname)
        return {"status": "Processing", "job_id": job_id}
    except Exception as e:
        return {"status": "Error", "message": f"Upload Error: {str(e)}"}

# ═══════════════════════════════════════════════════════════════════════════════
# STARSHOT
# ═══════════════════════════════════════════════════════════════════════════════
def _extract_ss_chart_data(ss):
    """Extract real spoke + radial data from pylinac 3.x Starshot."""
    import numpy as np
    try:
        # ── Wobble radius ──
        # pylinac 3.x: ss.wobble.radius is a NumberValue with .mm attribute
        try:
            wobble_radius = round(float(ss.wobble.radius.mm), 4)
        except AttributeError:
            wobble_radius = round(float(ss.wobble.radius), 4)

        # ── Wobble center ──
        try:
            cx = round(float(ss.wobble.center.x), 2)
            cy = round(float(ss.wobble.center.y), 2)
        except Exception:
            cx, cy = 0.0, 0.0

        # ── Spokes ──
        # pylinac 3.x: ss.lines is a list of Line objects
        # Each Line has .angle (Angle) and we compute distance to wobble center
        spokes = []
        try:
            for i, line in enumerate(ss.lines):
                try:
                    angle = round(float(line.angle.degrees), 2)
                except AttributeError:
                    angle = round(float(line.angle), 2)

                # Distance from wobble center to the spoke line
                try:
                    dist = round(float(line.distance_to(ss.wobble.center)), 4)
                except Exception:
                    # Fallback: compute manually
                    # Line defined by two points p1, p2
                    # distance = |cross(p2-p1, p1-pt)| / |p2-p1|
                    try:
                        p1 = np.array([line.point1.x, line.point1.y])
                        p2 = np.array([line.point2.x, line.point2.y])
                        pt = np.array([cx, cy])
                        num = abs(np.cross(p2-p1, p1-pt))
                        den = np.linalg.norm(p2-p1)
                        dist = round(float(num/den) * (ss.image.dpmm**-1), 4) if den > 0 else 0.0
                    except Exception:
                        dist = 0.0

                spokes.append({"index": i, "angle": angle, "residual_mm": dist})
        except Exception as e:
            spokes = [{"error": str(e)}]

        # ── Radial intensity profile ──
        radial = []
        try:
            img_arr = ss.image.array.astype(float)
            h, w = img_arr.shape[:2]
            # Use wobble center; clamp to image bounds
            icx = int(np.clip(cx, 0, w-1))
            icy = int(np.clip(cy, 0, h-1))
            max_r = min(icy, icx, h-icy, w-icx, 200)
            if max_r > 5:
                for r in range(1, max_r):
                    angles = np.linspace(0, 2*np.pi, max(12, r), endpoint=False)
                    vals = []
                    for a in angles:
                        xi = int(icx + r * np.cos(a))
                        yi = int(icy + r * np.sin(a))
                        if 0 <= xi < w and 0 <= yi < h:
                            v = img_arr[yi, xi]
                            if len(img_arr.shape) == 3:
                                v = float(np.mean(v))
                            vals.append(float(v))
                    if vals:
                        radial.append(round(float(np.mean(vals)), 2))
                # Normalise 0–1
                if radial:
                    mn, mx = min(radial), max(radial)
                    if mx > mn:
                        radial = [round((v-mn)/(mx-mn), 4) for v in radial]
        except Exception:
            radial = []

        return {
            "wobble_radius": wobble_radius,
            "wobble_center": {"x": cx, "y": cy},
            "spokes":        spokes,
            "radial_profile": radial[:120],
        }
    except Exception as e:
        return {"error": f"Starshot extraction failed: {str(e)}",
                "wobble_radius": 0, "spokes": [], "radial_profile": []}


def run_starshot(job_id, temp_path, user_email, filename):
    plot_path = None
    try:
        ss = Starshot.from_zip(temp_path) if temp_path.endswith(".zip") else Starshot(temp_path)
        ss.analyze(tolerance=1.0)
        summary    = ss.results()
        passed     = ss.passed
        chart_data = _extract_ss_chart_data(ss)

        plot_filename = f"{job_id}.png"
        plot_path     = f"/tmp/{plot_filename}"
        ss.save_analyzed_image(plot_path)
        plt.close("all")
        image_url = upload_plot_to_supabase(plot_path, plot_filename)

        jobs[job_id] = {"status": "Success", "passed": passed,
                        "analysis_summary": summary, "image_url": image_url,
                        "chart_data": chart_data}
        save_analysis(user_email, "Starshot", filename, passed, summary, image_url)
    except Exception as e:
        jobs[job_id] = {"status": "Error", "message": f"Analysis Error: {str(e)}"}
    finally:
        for p in [temp_path, plot_path]:
            try:
                if p and os.path.exists(p): os.remove(p)
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
