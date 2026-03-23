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

SECRET_KEY   = os.environ.get("SECRET_KEY", "mlcqa-change-this-in-render")
ALGORITHM    = "HS256"
TOKEN_HOURS  = 24
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://swxrncaezcthahehhuu.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

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
    if r.status_code in (200,201):
        d = r.json(); return d[0] if isinstance(d,list) else d
    raise HTTPException(500, f"Could not create user: {r.text}")
def upload_plot(image_path, fname):
    try:
        with open(image_path,"rb") as f: b = f.read()
        with httpx.Client() as c:
            r = c.post(f"{SUPABASE_URL}/storage/v1/object/plots/{fname}",
                       headers=sb_storage_headers(), content=b)
        return f"{SUPABASE_URL}/storage/v1/object/public/plots/{fname}" if r.status_code in (200,201) else None
    except: return None
def save_analysis(email, test_type, filename, passed, summary, image_url=None, chart_data=None, job_id=None):
    with httpx.Client() as c:
        c.post(f"{SUPABASE_URL}/rest/v1/analyses", headers=sb_headers(),
               json={"user_email":email,"test_type":test_type,"filename":filename,
                     "passed":passed,"summary":summary,"image_url":image_url,
                     "chart_data":chart_data,"job_id":job_id})

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
    return r.json() if r.status_code==200 else []

def hash_password(pw): return hmac.new(SECRET_KEY.encode(), pw.encode(), hashlib.sha256).hexdigest()
def verify_password(plain, hashed): return hmac.compare_digest(hash_password(plain), hashed)

bearer_scheme = HTTPBearer()
def create_token(email, name):
    return jwt.encode({"sub":email,"name":name,"exp":datetime.utcnow()+timedelta(hours=TOKEN_HOURS)},
                      SECRET_KEY, algorithm=ALGORITHM)
def get_current_user(creds: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    try:
        p = jwt.decode(creds.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        if not p.get("sub"): raise HTTPException(401,"Invalid token")
        return {"email":p["sub"],"name":p.get("name")}
    except JWTError: raise HTTPException(401,"Invalid or expired token")

class SignupRequest(BaseModel): name:str; email:str; password:str
class LoginRequest(BaseModel): email:str; password:str

app = FastAPI()
app.add_middleware(CORSMiddleware,allow_origins=["*"],allow_credentials=True,
                   allow_methods=["*"],allow_headers=["*"])

# Safety net: ensure CORS headers are present even when an unhandled exception
# causes FastAPI to return a 500 before the middleware runs.
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
    if len(jobs)>50:
        for k in list(jobs.keys())[:len(jobs)-50]: del jobs[k]


@app.get("/")
@app.head("/")
def home(): return {"status":"MLC QA Backend is Live and listening."}

@app.post("/auth/signup")
def signup(req: SignupRequest):
    if not req.name.strip(): raise HTTPException(400,"Name required.")
    if len(req.password)<6: raise HTTPException(400,"Password min 6 chars.")
    if get_user_by_email(req.email): raise HTTPException(400,"Email already registered.")
    create_user(req.name.strip(), req.email, hash_password(req.password))
    return {"message":"Account created successfully."}

@app.post("/auth/login")
def login(req: LoginRequest):
    user = get_user_by_email(req.email)
    if not user or not verify_password(req.password, user["password"]):
        raise HTTPException(401,"Invalid email or password.")
    return {"access_token":create_token(req.email,user["name"]),"token_type":"bearer","name":user["name"]}

@app.get("/auth/me")
def get_me(u=Depends(get_current_user)): return {"email":u["email"],"name":u["name"]}

@app.get("/history")
def get_history(u=Depends(get_current_user)): return {"analyses":get_user_analyses(u["email"])}

@app.get("/result/{job_id}")
def get_result(job_id, u=Depends(get_current_user)):
    # Check in-memory first (job still running or just finished)
    if job_id in jobs:
        return jobs[job_id]
    # Fallback: check Supabase (covers server restarts wiping jobs{})
    db_result = get_job_result(job_id)
    if db_result:
        return db_result
    return {"status": "Error", "message": "Job not found. The server may have restarted — please re-upload your file."}

# ── DEBUG endpoint — call this to inspect pylinac attribute names live ──
@app.post("/debug/pf")
async def debug_pf(file: UploadFile = File(...), u=Depends(get_current_user)):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".dcm", dir="/tmp") as tmp:
        shutil.copyfileobj(file.file, tmp); path = tmp.name
    try:
        pf = PicketFence(path); pf.analyze(tolerance=0.5, action_tolerance=0.25)
        info = {"pf_attrs": [a for a in dir(pf) if not a.startswith("_")]}

        # --- pf.mlc inspection ---
        if hasattr(pf, "mlc"):
            info["mlc_type"] = str(type(pf.mlc))
            try:
                info["mlc_len"] = len(pf.mlc)
                if pf.mlc:
                    pk0 = pf.mlc[0]
                    info["mlc_pk0_type"] = str(type(pk0))
                    info["mlc_pk0_attrs"] = [a for a in dir(pk0) if not a.startswith("_")]
                    if hasattr(pk0, "mlc_meas"):
                        info["mlc_meas_len"] = len(pk0.mlc_meas)
                        if pk0.mlc_meas:
                            m0 = pk0.mlc_meas[0]
                            info["meas0_attrs"] = [a for a in dir(m0) if not a.startswith("_")]
                            info["meas0_error"] = str(getattr(m0, "error", "NO ERROR ATTR"))
                    # sample first 3 errors
                    sample = []
                    for meas in list(pk0.mlc_meas)[:3]:
                        sample.append(str(getattr(meas, "error", "?")))
                    info["mlc_pk0_meas_sample"] = sample
            except Exception as e: info["mlc_error"] = str(e)
        else:
            info["mlc_present"] = False

        # --- pf.pickets inspection ---
        if hasattr(pf, "pickets"):
            info["pickets_len"] = len(pf.pickets)
            if pf.pickets:
                pk = pf.pickets[0]
                info["picket0_attrs"] = [a for a in dir(pk) if not a.startswith("_")]
                info["picket0_sample"] = {a: str(getattr(pk, a, None))[:80]
                                          for a in info["picket0_attrs"]}
        else:
            info["pickets_present"] = False

        # --- results_data() inspection ---
        try:
            rd = pf.results_data()
            info["rd_attrs"] = [a for a in dir(rd) if not a.startswith("_")]
            if hasattr(rd, "pickets"):
                info["rd_pickets_len"] = len(rd.pickets)
                if rd.pickets:
                    p0 = rd.pickets[0]
                    info["rd_picket0_attrs"] = [a for a in dir(p0) if not a.startswith("_")]
                    info["rd_picket0_sample"] = {a: str(getattr(p0,a,None))[:80]
                                                 for a in info["rd_picket0_attrs"]}
        except Exception as e: info["rd_error"] = str(e)

        # --- what _extract_pf_chart_data actually returns ---
        info["chart_data_extracted"] = _extract_pf_chart_data(pf)

        return info
    except Exception as e: return {"error": str(e)}
    finally:
        try: os.remove(path)
        except: pass

@app.post("/debug/ss")
async def debug_ss(file: UploadFile = File(...), u=Depends(get_current_user)):
    ext = file.filename.lower().split(".")[-1]
    with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}", dir="/tmp") as tmp:
        shutil.copyfileobj(file.file, tmp); path = tmp.name
    try:
        ss = Starshot.from_zip(path) if path.endswith(".zip") else Starshot(path)
        ss.analyze(tolerance=1.0)
        info = {}
        info["ss_attrs"] = [a for a in dir(ss) if not a.startswith("_")]
        if hasattr(ss,"wobble"):
            info["wobble_attrs"] = [a for a in dir(ss.wobble) if not a.startswith("_")]
            if hasattr(ss.wobble,"radius"):
                info["radius_type"] = str(type(ss.wobble.radius))
                info["radius_attrs"] = [a for a in dir(ss.wobble.radius) if not a.startswith("_")]
        if hasattr(ss,"lines"):
            info["lines_len"] = len(ss.lines)
            if ss.lines:
                info["line0_attrs"] = [a for a in dir(ss.lines[0]) if not a.startswith("_")]
                l0 = ss.lines[0]
                info["line0_sample"] = {a: str(getattr(l0,a,None))[:80] for a in info["line0_attrs"]}
        try:
            rd = ss.results_data()
            info["rd_attrs"] = [a for a in dir(rd) if not a.startswith("_")]
            info["rd_sample"] = {a: str(getattr(rd,a,None))[:80] for a in info["rd_attrs"]}
        except Exception as e: info["rd_error"] = str(e)
        return info
    except Exception as e: return {"error": str(e)}
    finally:
        try: os.remove(path)
        except: pass

# ═══════════════════════════════════════════════════════════════
# PICKET FENCE — robust extraction using results_data()
# ═══════════════════════════════════════════════════════════════
def _extract_pf_chart_data(pf):
    import numpy as np

    leaf_max     = []
    picket_means = []
    max_err      = None
    mean_err     = None
    failed       = 0

    try:
        rd = pf.results_data()

        # ── Scalars (exact attr names from Pylinac 3.42 PFResult) ──
        if hasattr(rd, "max_error_mm"):
            try: max_err = round(float(rd.max_error_mm), 4)
            except: pass
        if hasattr(rd, "absolute_median_error_mm"):
            try: mean_err = round(float(rd.absolute_median_error_mm), 4)
            except: pass
        if hasattr(rd, "failed_leaves"):
            try: failed = int(rd.failed_leaves)
            except: pass

        # ── Per-leaf max errors across all pickets ──
        for attr_name in ["mlc_errors_by_leaf", "mlc_error", "leaf_errors", "errors"]:
            if hasattr(rd, attr_name):
                val = getattr(rd, attr_name)
                if isinstance(val, dict):
                    leaf_max = [round(abs(float(v)), 4) for v in val.values()]
                    break
                elif hasattr(val, "__len__") and not isinstance(val, (str, bool)):
                    try:
                        leaf_max = [round(abs(float(v)), 4) for v in val]
                        if leaf_max: break
                    except: pass

        # ── Per-picket mean errors: try rd.pickets first, then pf.pickets ──
        if hasattr(rd, "pickets") and rd.pickets:
            for pk in rd.pickets:
                for attr in ["mean_error", "mean_error_mm", "error", "max_error"]:
                    raw = getattr(pk, attr, None)
                    if raw is not None:
                        try: picket_means.append(round(abs(float(raw)), 4)); break
                        except: pass

        if not picket_means and hasattr(pf, "pickets") and pf.pickets:
            for picket in pf.pickets:
                meas_list = getattr(picket, "mlc_meas", None) or getattr(picket, "meas", None)
                if meas_list:
                    errs = []
                    for meas in meas_list:
                        for attr in ["error", "error_mm", "offset"]:
                            raw = getattr(meas, attr, None)
                            if raw is not None:
                                try: errs.append(abs(float(raw))); break
                                except: pass
                    if errs:
                        picket_means.append(round(float(np.mean(errs)), 4))

    except Exception:
        pass

    # ── Text fallback for scalars only ──
    if max_err is None:
        try:
            import re
            txt = pf.results()
            m = re.search(r"Max Error:\s*([\d.]+)\s*mm", txt)
            if m: max_err = round(float(m.group(1)), 4)
            m2 = re.search(r"(?:median|mean)[^\d]*([\d.]+)\s*mm", txt, re.IGNORECASE)
            if m2: mean_err = round(float(m2.group(1)), 4)
        except: pass

    if max_err  is None: max_err  = round(max(leaf_max), 4) if leaf_max else 0.0
    if mean_err is None: mean_err = round(float(np.mean(leaf_max)), 4) if leaf_max else 0.0

    return {
        "leaf_max_errors":    leaf_max,
        "picket_mean_errors": picket_means,
        "max_error":          max_err,
        "mean_error":         mean_err,
        "failed_leaves":      failed,
        "n_leaves":           len(leaf_max),
        "n_pickets":          len(picket_means),
    }

def run_analysis(job_id, temp_path, user_email, filename):
    plot_path = None
    try:
        pf = PicketFence(temp_path)
        pf.analyze(tolerance=0.5, action_tolerance=0.25)
        summary    = pf.results()
        passed     = pf.passed
        chart_data = _extract_pf_chart_data(pf)

        plot_filename = f"{job_id}.png"; plot_path = f"/tmp/{plot_filename}"
        pf.save_analyzed_image(plot_path); plt.close("all")
        image_url = upload_plot(plot_path, plot_filename)

        jobs[job_id] = {"status":"Success","passed":passed,"analysis_summary":summary,
                        "image_url":image_url,"chart_data":chart_data}
        save_analysis(user_email,"Picket Fence",filename,passed,summary,image_url,chart_data=chart_data,job_id=job_id)
    except Exception as e:
        msg = str(e)
        if "not a valid DICOM" in msg or "Invalid tag" in msg or "read_file" in msg:
            friendly = "The uploaded file does not appear to be a valid DICOM file."
        elif "picket" in msg.lower() or "mlc" in msg.lower() or "leaf" in msg.lower():
            friendly = "Could not detect a Picket Fence pattern in this image. Please verify the file is a Picket Fence DICOM."
        else:
            friendly = f"Analysis failed: {msg}"
        jobs[job_id] = {"status": "Error", "message": friendly}
    finally:
        for p in [temp_path,plot_path]:
            try:
                if p and os.path.exists(p): os.remove(p)
            except: pass
        cleanup()

@app.post("/analyze")
async def analyze_mlc(bg: BackgroundTasks, file: UploadFile = File(...), u=Depends(get_current_user)):
    if not file.filename.lower().endswith(".dcm"):
        return {"status":"Error","message":"Only DICOM (.dcm) files are supported."}
    try:
        with tempfile.NamedTemporaryFile(delete=False,suffix=".dcm",dir="/tmp") as tmp:
            shutil.copyfileobj(file.file,tmp); path=tmp.name
        jid = str(uuid.uuid4()); jobs[jid]={"status":"Processing"}
        bg.add_task(run_analysis,jid,path,u["email"],file.filename)
        return {"status":"Processing","job_id":jid}
    except Exception as e: return {"status":"Error","message":f"Upload Error: {str(e)}"}

# ═══════════════════════════════════════════════════════════════
# WINSTON-LUTZ
# ═══════════════════════════════════════════════════════════════
def _extract_wl_chart_data(wl):
    import numpy as np
    try:
        images_data = []
        for img in wl.images:
            gt, lt, err = 0.0, 0.0, 0.0
            # Try multiple attribute names
            for attr in ["bb_shift_vector","cax2bb_vector","bb_deviation_2d"]:
                if hasattr(img, attr):
                    vec = getattr(img, attr)
                    try:
                        gt  = round(float(vec.x), 4)
                        lt  = round(float(vec.y), 4)
                        err = round(float(getattr(vec,"length",None) or
                                         getattr(vec,"magnitude",None) or
                                         np.sqrt(gt**2+lt**2)), 4)
                        break
                    except: pass
            try: gantry = round(float(img.gantry_angle), 1)
            except: gantry = 0.0
            images_data.append({"gantry":gantry,"gt":gt,"lt":lt,"error_2d":err})

        if not images_data:
            return {"error":"No images parsed","images":[]}

        max_2d  = round(float(max(d["error_2d"] for d in images_data)), 4)
        mean_2d = round(float(np.mean([d["error_2d"] for d in images_data])), 4)

        iso_radius = max_2d
        for method in ["max","median"]:
            try: iso_radius = round(float(wl.cax2bb_distance(method)), 4); break
            except: pass

        return {"images":images_data,"max_2d_error":max_2d,
                "mean_2d_error":mean_2d,"iso_radius":iso_radius}
    except Exception as e:
        return {"error":str(e),"images":[]}

def run_winston_lutz(job_id, temp_path, user_email, filename):
    plot_path = None; extract_dir = None
    try:
        if temp_path.endswith(".zip"):
            import zipfile
            extract_dir = temp_path+"_ex"
            os.makedirs(extract_dir,exist_ok=True)
            with zipfile.ZipFile(temp_path,"r") as z: z.extractall(extract_dir)
            wl = WinstonLutz(extract_dir)
        else:
            wl = WinstonLutz(os.path.dirname(temp_path))
        wl.analyze()
        summary = wl.results(); passed = wl.passed
        chart_data = _extract_wl_chart_data(wl)

        plot_filename = f"{job_id}.png"; plot_path = f"/tmp/{plot_filename}"
        wl.save_summary_plot(plot_path); plt.close("all")
        image_url = upload_plot(plot_path, plot_filename)

        jobs[job_id] = {"status":"Success","passed":passed,"analysis_summary":summary,
                        "image_url":image_url,"chart_data":chart_data}
        save_analysis(user_email,"Winston-Lutz",filename,passed,summary,image_url,chart_data=chart_data,job_id=job_id)
    except Exception as e:
        msg = str(e)
        if "not a valid DICOM" in msg or "Invalid tag" in msg:
            friendly = "One or more uploaded files are not valid DICOM files."
        elif "bb" in msg.lower() or "ball bearing" in msg.lower() or "no images" in msg.lower():
            friendly = "Could not detect a BB marker in the images. Please verify these are Winston-Lutz DICOM files."
        elif "zip" in msg.lower():
            friendly = "Could not read the ZIP file. Ensure it contains valid DICOM images."
        else:
            friendly = f"Analysis failed: {msg}"
        jobs[job_id] = {"status": "Error", "message": friendly}
    finally:
        for p in [temp_path,plot_path]:
            try:
                if p and os.path.exists(p): os.remove(p)
            except: pass
        if extract_dir:
            try: shutil.rmtree(extract_dir,ignore_errors=True)
            except: pass
        cleanup()

@app.post("/analyze/winston-lutz")
async def analyze_wl(bg: BackgroundTasks, file: UploadFile = File(None),
                     files: list[UploadFile] = File(None), u=Depends(get_current_user)):
    try:
        fname = "wl"
        if file and file.filename.lower().endswith(".zip"):
            with tempfile.NamedTemporaryFile(delete=False,suffix=".zip",dir="/tmp") as tmp:
                shutil.copyfileobj(file.file,tmp); path=tmp.name
            fname = file.filename
        elif files:
            import zipfile
            zp = f"/tmp/{uuid.uuid4()}.zip"
            with zipfile.ZipFile(zp,"w") as zf:
                for f in files:
                    content = await f.read(); zf.writestr(f.filename,content)
            path=zp; fname=f"{len(files)}_images.zip"
        elif file:
            ext=file.filename.lower().split(".")[-1]
            with tempfile.NamedTemporaryFile(delete=False,suffix=f".{ext}",dir="/tmp") as tmp:
                shutil.copyfileobj(file.file,tmp); path=tmp.name
            fname=file.filename
        else: return {"status":"Error","message":"No files provided."}
        jid=str(uuid.uuid4()); jobs[jid]={"status":"Processing"}
        bg.add_task(run_winston_lutz,jid,path,u["email"],fname)
        return {"status":"Processing","job_id":jid}
    except Exception as e: return {"status":"Error","message":f"Upload Error: {str(e)}"}

# ═══════════════════════════════════════════════════════════════
# STARSHOT — robust extraction
# ═══════════════════════════════════════════════════════════════
def _extract_ss_chart_data(ss):
    import numpy as np
    try:
        # ── Wobble radius ──
        wobble_radius = 0.0
        for path in [("wobble","radius","mm"), ("wobble","radius"),
                     ("results_data","wobble_radius")]:
            try:
                obj = ss
                for attr in path:
                    if attr == "results_data":
                        obj = ss.results_data()
                    else:
                        obj = getattr(obj, attr)
                wobble_radius = round(float(obj), 4)
                break
            except: pass

        # ── Wobble center ──
        cx, cy = 0.0, 0.0
        try: cx=round(float(ss.wobble.center.x),2); cy=round(float(ss.wobble.center.y),2)
        except: pass

        # ── Spokes from results_data (most reliable in 3.x) ──
        spokes = []
        try:
            rd = ss.results_data()
            rd_attrs = dir(rd)
            # Try getting spoke/line info from results_data
            for attr in ["spokes","lines","spoke_angles","num_spokes"]:
                if attr in rd_attrs:
                    val = getattr(rd, attr)
                    if hasattr(val,"__len__") and len(val)>0:
                        raw = []
                        for i,item in enumerate(val):
                            if hasattr(item,"angle"):
                                try: ang = round(float(item.angle),2)
                                except: ang = i*60.0
                            else:
                                ang = i*60.0
                            raw.append({"norm_angle": ang % 180, "angle": ang % 180,
                                        "residual_mm": wobble_radius})
                        # Deduplicate
                        seen = []
                        for sp in raw:
                            if not any(abs(sp["norm_angle"]-s["norm_angle"])<10 for s in seen):
                                seen.append(sp)
                        seen.sort(key=lambda x: x["norm_angle"])
                        spokes = [{"index":i,"angle":s["angle"],"residual_mm":s["residual_mm"]}
                                  for i,s in enumerate(seen)]
                        break
                    elif isinstance(val,int):
                        # num_spokes — build evenly spaced within 0-180
                        unique = val // 2 if val > 3 else val
                        for i in range(unique):
                            spokes.append({"index":i,"angle":round(i*180/unique,1),"residual_mm":wobble_radius})
                        break
        except Exception: pass

        # ── Spokes from ss.lines directly ──
        if not spokes:
            try:
                raw_spokes = []
                for i, line in enumerate(ss.lines):
                    try: angle = round(float(line.angle.degrees),2)
                    except:
                        try: angle = round(float(line.angle),2)
                        except: angle = i*60.0
                    # Normalise to 0–180 (a line and its opposite are the same spoke)
                    norm_angle = angle % 180
                    dist = wobble_radius
                    try:
                        import math
                        x1,y1 = float(line.point1.x), float(line.point1.y)
                        x2,y2 = float(line.point2.x), float(line.point2.y)
                        num = abs((y2-y1)*cx-(x2-x1)*cy+x2*y1-y2*x1)
                        den = math.sqrt((y2-y1)**2+(x2-x1)**2)
                        dist_px = num/den if den>0 else 0
                        dpmm = getattr(ss.image,"dpmm",1)
                        dist = round(dist_px/dpmm,4) if dpmm>0 else round(dist_px,4)
                    except: pass
                    raw_spokes.append({"norm_angle": norm_angle, "angle": angle, "residual_mm": dist})

                # Deduplicate — collapse spokes within 10° of each other (opposite sides of same line)
                seen = []
                for sp in raw_spokes:
                    is_dup = False
                    for s in seen:
                        if abs(sp["norm_angle"] - s["norm_angle"]) < 10:
                            # Keep the one with larger residual (more conservative)
                            if sp["residual_mm"] > s["residual_mm"]:
                                s["residual_mm"] = sp["residual_mm"]
                            is_dup = True
                            break
                    if not is_dup:
                        seen.append({"norm_angle": sp["norm_angle"],
                                     "angle": sp["norm_angle"],  # display normalised
                                     "residual_mm": sp["residual_mm"]})

                # Sort by angle and re-index
                seen.sort(key=lambda x: x["norm_angle"])
                spokes = [{"index":i, "angle":s["angle"], "residual_mm":s["residual_mm"]}
                          for i,s in enumerate(seen)]
            except Exception: pass

        # ── Radial intensity profile ──
        radial = []
        try:
            arr = ss.image.array.astype(float)
            if len(arr.shape)==3: arr = arr.mean(axis=2)
            h,w = arr.shape
            icx = int(np.clip(cx,1,w-2))
            icy = int(np.clip(cy,1,h-2))
            max_r = min(icy,icx,h-icy,w-icx,180)
            if max_r>5:
                for r in range(1,max_r):
                    n_pts = max(16, r*2)
                    angles = np.linspace(0,2*np.pi,n_pts,endpoint=False)
                    xs = (icx + r*np.cos(angles)).astype(int).clip(0,w-1)
                    ys = (icy + r*np.sin(angles)).astype(int).clip(0,h-1)
                    radial.append(round(float(arr[ys,xs].mean()),2))
                if radial:
                    mn,mx = min(radial),max(radial)
                    if mx>mn: radial=[round((v-mn)/(mx-mn),4) for v in radial]
        except Exception: pass

        return {"wobble_radius":wobble_radius,"wobble_center":{"x":cx,"y":cy},
                "spokes":spokes,"radial_profile":radial[:120]}
    except Exception as e:
        return {"error":str(e),"wobble_radius":0,"spokes":[],"radial_profile":[]}

def run_starshot(job_id, temp_path, user_email, filename):
    plot_path = None
    try:
        ss = Starshot.from_zip(temp_path) if temp_path.endswith(".zip") else Starshot(temp_path)
        ss.analyze(tolerance=1.0)
        summary=ss.results(); passed=ss.passed
        chart_data=_extract_ss_chart_data(ss)

        plot_filename=f"{job_id}.png"; plot_path=f"/tmp/{plot_filename}"
        ss.save_analyzed_image(plot_path); plt.close("all")
        image_url=upload_plot(plot_path,plot_filename)

        jobs[job_id]={"status":"Success","passed":passed,"analysis_summary":summary,
                      "image_url":image_url,"chart_data":chart_data}
        save_analysis(user_email,"Starshot",filename,passed,summary,image_url,chart_data=chart_data,job_id=job_id)
    except Exception as e:
        msg = str(e)
        if "not a valid DICOM" in msg or "Invalid tag" in msg:
            friendly = "The uploaded file does not appear to be a valid DICOM file."
        elif "star" in msg.lower() or "spoke" in msg.lower() or "circle" in msg.lower():
            friendly = "Could not detect a Starshot pattern in this image. Please verify the file is a Starshot DICOM."
        elif "zip" in msg.lower():
            friendly = "Could not read the ZIP file. Ensure it contains valid DICOM images."
        else:
            friendly = f"Analysis failed: {msg}"
        jobs[job_id] = {"status": "Error", "message": friendly}
    finally:
        for p in [temp_path,plot_path]:
            try:
                if p and os.path.exists(p): os.remove(p)
            except: pass
        cleanup()

@app.post("/analyze/starshot")
async def analyze_ss(bg: BackgroundTasks, file: UploadFile = File(...), u=Depends(get_current_user)):
    ext=file.filename.lower().split(".")[-1]
    if ext not in ("dcm","zip"): return {"status":"Error","message":"Only .dcm or .zip supported."}
    try:
        with tempfile.NamedTemporaryFile(delete=False,suffix=f".{ext}",dir="/tmp") as tmp:
            shutil.copyfileobj(file.file,tmp); path=tmp.name
        jid=str(uuid.uuid4()); jobs[jid]={"status":"Processing"}
        bg.add_task(run_starshot,jid,path,u["email"],file.filename)
        return {"status":"Processing","job_id":jid}
    except Exception as e: return {"status":"Error","message":f"Upload Error: {str(e)}"}
