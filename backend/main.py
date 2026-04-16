from fastapi import FastAPI, UploadFile, File, BackgroundTasks, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from jose import JWTError, jwt
from datetime import datetime, timedelta
from pylinac import PicketFence, WinstonLutz, Starshot, FieldAnalysis
import os, shutil, tempfile, uuid, hashlib, hmac
import re
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
        r = c.get(
            f"{SUPABASE_URL}/rest/v1/users?email=eq.{email}&limit=1&select=name,email,password_hash",
            headers=sb_headers()
        )
        if r.status_code != 200:
            return None
        data = r.json()
        return data[0] if data else None

def create_user(name, email, password_hash):
    user = {"name": name, "email": email, "password_hash": password_hash, "created_at": datetime.utcnow().isoformat()}
    with httpx.Client() as c:
        r = c.post(f"{SUPABASE_URL}/rest/v1/users", json=user, headers=sb_headers())
        if r.status_code not in (200, 201):
            raise RuntimeError(f"Failed to create user: {r.text}")

def save_analysis(email, test_type, filename, passed, summary, image_url, chart_data, job_id):
    analysis = {
        "email": email,
        "test_type": test_type,
        "filename": filename,
        "passed": passed,
        "summary": summary,
        "image_url": image_url,
        "chart_data": chart_data,
        "job_id": job_id,
        "created_at": datetime.utcnow().isoformat()
    }
    with httpx.Client() as c:
        r = c.post(f"{SUPABASE_URL}/rest/v1/analyses", json=analysis, headers=sb_headers())
        if r.status_code not in (200, 201):
            print(f"Failed to save analysis: {r.text}")

def upload_plot(local_path, filename):
    with open(local_path, "rb") as f:
        with httpx.Client() as c:
            r = c.post(f"{SUPABASE_URL}/storage/v1/object/analyses/{filename}", 
                      data=f.read(), headers=sb_storage_headers())
            if r.status_code not in (200, 201):
                print(f"Failed to upload plot: {r.text}")
                return None
            return f"{SUPABASE_URL}/storage/v1/object/public/analyses/{filename}"

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

class SignupRequest(BaseModel): 
    name: str; 
    email: str; 
    password: str
    
class LoginRequest(BaseModel): 
    email: str; 
    password: str
    
class ForgotPasswordRequest(BaseModel): 
    email: str

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://mlc-qa-1.onrender.com",  # production frontend
        "http://localhost:3000",           # local dev
        "http://localhost:5500",           # local dev (Live Server)
        "http://127.0.0.1:5500",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

from fastapi.responses import JSONResponse
from fastapi.requests import Request

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    print(f"Global exception: {exc}")  # Log the actual error
    return JSONResponse(
        status_code=500,
        content={"status": "Error", "message": f"An unexpected server error occurred: {str(exc)}"},
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
    # ── TODO: Implement email delivery ──────────────────────────────────────
    # This endpoint currently does NOT send any email.
    # To fully implement, integrate an email provider such as:
    #   - Resend:   https://resend.com/docs/send-with-python
    #   - SendGrid: https://docs.sendgrid.com/for-developers/sending-email/quickstarts-python
    #
    # Steps:
    #   1. pip install resend  (or sendgrid)
    #   2. Add RESEND_API_KEY (or SENDGRID_API_KEY) to your Render environment variables
    #   3. Generate a signed reset token (e.g. itsdangerous.URLSafeTimedSerializer)
    #   4. Send the token as a link: https://your-frontend.com/reset-password.html?token=...
    #   5. Add a /auth/reset-password endpoint that verifies the token and updates the hash
    # ────────────────────────────────────────────────────────────────────────
    user = get_user_by_email(req.email)
    # Email delivery not yet implemented — log for operator awareness
    if user:
        print(f"[FORGOT PASSWORD] Reset requested for: {req.email} — email NOT sent (not implemented)")
    # Always return the same 200 message to prevent email enumeration
    # and to avoid showing a scary error banner on the login page
    return {"message": "If that email is registered, you will receive a reset link shortly. Please also contact your administrator if you need immediate access."}

@app.post("/auth/signup")
def signup(req: SignupRequest):
    try:
        if not req.name.strip():
            raise HTTPException(400, "Name required.")
        if len(req.password) < 6:
            raise HTTPException(400, "Password min 6 chars.")
        if get_user_by_email(req.email):
            raise HTTPException(400, "Email already registered.")
        create_user(req.name.strip(), req.email, hash_password(req.password))
        return {"message": "Account created successfully."}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Signup failed: {str(e)}")

@app.post("/auth/login")
def login(req: LoginRequest):
    try:
        user = get_user_by_email(req.email)
        pw_hash = user.get("password_hash") if user else None
        if not user or not pw_hash or not verify_password(req.password, pw_hash):
            raise HTTPException(401, "Invalid email or password")
        token = create_token(user["email"], user["name"])
        return {"token": token, "name": user["name"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Login failed: {str(e)}")

# ── Analysis history ─────────────────────────────────────────────────────────

@app.get("/history")
def get_history(u=Depends(get_current_user)):
    try:
        with httpx.Client() as c:
            r = c.get(f"{SUPABASE_URL}/rest/v1/analyses?email=eq.{u['email']}&order=created_at.desc", headers=sb_headers())
            if r.status_code != 200:
                return []
            return r.json()
    except Exception as e:
        print(f"History fetch error: {e}")
        return []

# ── Job status ───────────────────────────────────────────────────────────────

@app.get("/result/{job_id}")
def get_result(job_id: str):
    if job_id not in jobs:
        return {"status": "Error", "message": "Job not found. The server may have restarted — please re-upload your file."}
    return jobs[job_id]

# ═════════════════════════════════════════════════════════════════════════════
# DICOM TYPE VALIDATION  (shared by all analysis endpoints)
# ═════════════════════════════════════════════════════════════════════════════

_DICOM_SIGNATURES = {
    "picket_fence": {
        "filename": [r"picket", r"\bpf[_\-\s]", r"mlc[_\-\s]fence", r"leaf[_\-\s]pos",
                     r"mlcqa", r"pf_test", r"pf-test"],
        "header":   [r"picket[\s_\-]?fence", r"PicketFence", r"mlc.*picket", r"picket.*mlc",
                     r"Leaf\s*\d+\s*(Bank|Pair)", r"DMLC|SMLC", r"MLC QA",
                     r"leaf[\s_\-]?position", r"mlc_qa", r"mlcfence"],
    },
    "starshot": {
        "filename": [r"starshot", r"star[_\-\s]shot", r"gantry[_\-\s]rot",
                     r"collimator[_\-\s]rot", r"spoke"],
        "header":   [r"starshot", r"star[\s_\-]?shot", r"spoke[\s_\-]?angle", r"wobble",
                     r"collimator.*rotat", r"gantry.*spoke", r"radiation[\s_\-]?spoke"],
    },
    "winston_lutz": {
        "filename": [r"winston", r"\bwl\b", r"wl[_\-\s]", r"[_\-\s]wl",
                     r"winston[_\-]lutz", r"ball[_\-\s]?bear", r"isocent"],
        "header":   [r"winston[\s_\-]?lutz", r"WinstonLutz", r"ball[\s_\-]?bearing",
                     r"\bBB\b.*marker", r"isocenter.*bb", r"bb.*isocenter",
                     r"gantry.*angle.*bb", r"radiation[\s_\-]?isocent"],
    },
    "congruence": {
        "filename": [r"congruence", r"field[_\-\s]?size", r"light[_\-\s]?field",
                     r"rad[_\-\s]?light", r"open[_\-\s]?field", r"flatness", r"symmetry"],
        "header":   [r"congruence", r"field[\s_\-]?analysis", r"light[\s_\-]?field",
                     r"radiation[\s_\-]?field", r"field[\s_\-]?size", r"flatness",
                     r"symmetry", r"field[\s_\-]?edge"],
    },
}

_TEST_DISPLAY_NAMES = {
    "picket_fence": "Picket Fence",
    "starshot":     "Starshot",
    "winston_lutz": "Winston-Lutz",
    "congruence":   "Congruence",
}

def _score_dicom_type(test_type: str, filename: str, header_text: str) -> int:
    sig = _DICOM_SIGNATURES[test_type]
    score = 0
    for pat in sig["filename"]:
        if re.search(pat, filename, re.IGNORECASE):
            score += 3
    for pat in sig["header"]:
        if re.search(pat, header_text, re.IGNORECASE):
            score += 2
    return score

def _detect_dicom_type(filepath: str) -> str:
    """Return the best-matching QA test type for a DICOM file, or 'unknown'."""
    filename = os.path.basename(filepath).lower()
    try:
        with open(filepath, "rb") as f:
            raw = f.read(8192)
        header_text = raw.decode("latin-1", errors="replace")
    except Exception:
        header_text = ""
    scores = {t: _score_dicom_type(t, filename, header_text) for t in _DICOM_SIGNATURES}
    best_type, best_score = max(scores.items(), key=lambda kv: kv[1])
    return best_type if best_score > 0 else "unknown"

def _validate_dicom_type(filepath: str, expected: str) -> None:
    """
    Raise ValueError with a clear user-facing message if the DICOM file's
    detected type does not match *expected*.  Files that score 'unknown'
    (no recognisable metadata) are allowed through unchanged.
    """
    detected = _detect_dicom_type(filepath)
    if detected == "unknown" or detected == expected:
        return  # OK
    detected_name = _TEST_DISPLAY_NAMES.get(detected, detected)
    expected_name = _TEST_DISPLAY_NAMES.get(expected, expected)
    raise ValueError(
        f"Unacceptable data: the uploaded file appears to be a {detected_name} image, "
        f"not a {expected_name} file. "
        f"Please upload this file on the {detected_name} test page instead. "
        f"Analysing the wrong image type produces clinically meaningless results."
    )

# ═════════════════════════════════════════════════════════════════════════════
# PICKET FENCE
# ═════════════════════════════════════════════════════════════════════════════

def _extract_pf_chart_data(pf) -> dict:
    """
    Extract per-leaf-pair errors from a PicketFence result.
    Handles both single-bank and dual-bank (Bank A + Bank B) MLCs.
    Tries multiple pylinac API styles for compatibility.
    """
    try:
        results = pf.results_data()
        tol_mm  = float(getattr(results, "tolerance_mm", 1.0))

        leaf_max_errors = []
        leaf_pairs      = []

        # ── Helper: build leaf dict from mlc_errors_by_leaf ───────────────────
        def _build_from_dict(errors_dict):
            """Takes dict {leaf_key: [errors_per_picket]} → sorted (key, max_err) list.
            Keys are normalized to 1-based integers so the frontend 1-60 grid is always correct.
            Pylinac may emit 0-based (0..59) or centred (-30..29) indices — we re-map them."""
            def _sort_key(k):
                s = str(k).strip()
                try:
                    return (int(s) if int(s) >= 0 else int(s) + 10000,)
                except ValueError:
                    pass
                m = re.match(r'^(-?\d+)(.*)$', s)
                if m:
                    n = int(m.group(1))
                    return (n if n >= 0 else n + 10000, m.group(2))
                return (9999, s)

            # Collect all numeric keys to detect the base offset
            numeric_keys = []
            for k in errors_dict.keys():
                try:
                    numeric_keys.append(int(str(k).strip()))
                except ValueError:
                    pass

            # If the minimum key is ≤ 0 we need to shift so that min → 1
            shift = 0
            if numeric_keys:
                min_key = min(numeric_keys)
                if min_key <= 0:
                    shift = 1 - min_key   # e.g. 0-based → +1;  -30-based → +31

            out = []
            for k in sorted(errors_dict.keys(), key=_sort_key):
                errs = [abs(float(e)) for e in errors_dict[k] if e is not None]
                max_e  = round(max(errs),            4) if errs else 0.0
                mean_e = round(sum(errs)/len(errs),  4) if errs else 0.0
                # Apply shift so leaf labels are always 1-based
                try:
                    display_label = str(int(str(k).strip()) + shift)
                except ValueError:
                    display_label = str(k)
                out.append((display_label, max_e, mean_e))
            return out

        # ── Strategy 1: mlc_errors_by_leaf on results object ─────────────────
        ebl = getattr(results, "mlc_errors_by_leaf", None)
        if ebl and isinstance(ebl, dict) and len(ebl) > 0:
            print(f"PF Strategy 1 (results.mlc_errors_by_leaf): {len(ebl)} keys")
            pairs_data = _build_from_dict(ebl)
            for label, max_e, mean_e in pairs_data:
                leaf_max_errors.append(max_e)
                leaf_pairs.append({"leaf_pair": label, "max_error": max_e,
                                   "mean_error": mean_e, "passed": max_e <= tol_mm})

        # ── Strategy 2: pf.mlc has bank_a / bank_b attributes ────────────────
        if not leaf_max_errors:
            try:
                mlc = getattr(pf, "mlc", None)
                if mlc:
                    all_errs = {}
                    # Try bank_a and bank_b separately
                    for bank_attr, offset in [("bank_a", 0), ("bank_b", 1000)]:
                        bank = getattr(mlc, bank_attr, None)
                        if bank is not None:
                            bank_dict = getattr(bank, "mlc_errors_by_leaf", None)
                            if bank_dict and isinstance(bank_dict, dict):
                                for k, v in bank_dict.items():
                                    all_errs[f"{k}{'A' if offset==0 else 'B'}"] = v
                    if all_errs:
                        print(f"PF Strategy 2 (mlc banks): {len(all_errs)} keys")
                        for label, max_e, mean_e in _build_from_dict(all_errs):
                            leaf_max_errors.append(max_e)
                            leaf_pairs.append({"leaf_pair": label, "max_error": max_e,
                                               "mean_error": mean_e, "passed": max_e <= tol_mm})
            except Exception as ex:
                print(f"PF Strategy 2 failed: {ex}")

        # ── Strategy 3: iterate pf.pickets → guards ───────────────────────────
        if not leaf_max_errors:
            try:
                leaf_errs_dict = {}
                for picket in pf.pickets:
                    for guard in picket.guards:
                        ln  = str(getattr(guard, "leaf_num", getattr(guard, "leaf", "?")))
                        err = abs(float(getattr(guard, "error", 0)))
                        if ln not in leaf_errs_dict:
                            leaf_errs_dict[ln] = []
                        leaf_errs_dict[ln].append(err)
                if leaf_errs_dict:
                    print(f"PF Strategy 3 (picket guards): {len(leaf_errs_dict)} leaves")
                    for label, max_e, mean_e in _build_from_dict(leaf_errs_dict):
                        leaf_max_errors.append(max_e)
                        leaf_pairs.append({"leaf_pair": label, "max_error": max_e,
                                           "mean_error": mean_e, "passed": max_e <= tol_mm})
            except Exception as ex:
                print(f"PF Strategy 3 failed: {ex}")

        # ── Strategy 4: results.picket_results list ───────────────────────────
        if not leaf_max_errors:
            try:
                picket_results = getattr(results, "picket_results", None)
                if picket_results:
                    leaf_errs_dict = {}
                    for pr in picket_results:
                        leaf_errors_attr = getattr(pr, "leaf_errors", getattr(pr, "errors", None))
                        if leaf_errors_attr and isinstance(leaf_errors_attr, dict):
                            for k, v in leaf_errors_attr.items():
                                errs = v if isinstance(v, list) else [v]
                                if k not in leaf_errs_dict:
                                    leaf_errs_dict[k] = []
                                leaf_errs_dict[k].extend([abs(float(e)) for e in errs if e is not None])
                    if leaf_errs_dict:
                        print(f"PF Strategy 4 (picket_results): {len(leaf_errs_dict)} leaves")
                        for label, max_e, mean_e in _build_from_dict(leaf_errs_dict):
                            leaf_max_errors.append(max_e)
                            leaf_pairs.append({"leaf_pair": label, "max_error": max_e,
                                               "mean_error": mean_e, "passed": max_e <= tol_mm})
            except Exception as ex:
                print(f"PF Strategy 4 failed: {ex}")

        # ── Fallback: log all available attributes for debugging ──────────────
        if not leaf_max_errors:
            r_attrs = [a for a in dir(results) if not a.startswith("_")]
            pf_attrs = [a for a in dir(pf) if not a.startswith("_")]
            print(f"PF: all strategies failed.\nresults attrs: {r_attrs}\npf attrs: {pf_attrs}")
            # Last resort — try any attribute that looks like it contains leaf errors
            for attr in r_attrs:
                val = getattr(results, attr, None)
                if isinstance(val, dict) and len(val) > 5:
                    print(f"PF: attempting fallback on results.{attr} ({len(val)} keys)")
                    try:
                        for label, max_e, mean_e in _build_from_dict(
                                {k: (v if isinstance(v, list) else [v]) for k, v in val.items()}):
                            leaf_max_errors.append(max_e)
                            leaf_pairs.append({"leaf_pair": label, "max_error": max_e,
                                               "mean_error": mean_e, "passed": max_e <= tol_mm})
                        if leaf_max_errors:
                            print(f"PF: fallback on {attr} succeeded with {len(leaf_max_errors)} leaves")
                            break
                    except Exception:
                        leaf_max_errors.clear(); leaf_pairs.clear()

        # ── Summary metrics ───────────────────────────────────────────────────
        max_error = mean_error = failed_leaves = None
        try:
            max_error     = round(float(results.max_error_mm),             4)
            mean_error    = round(float(results.absolute_median_error_mm), 4)
            failed_leaves = int(results.failed_leaves) if results.failed_leaves is not None else 0
        except Exception as e:
            print(f"PF metrics error: {e}")

        print(f"PF FINAL: {len(leaf_max_errors)} leaf pairs | max={max_error} mean={mean_error}")
        return {
            "leaf_pairs":      leaf_pairs,
            "max_error":       max_error,
            "mean_error":      mean_error,
            "failed_leaves":   failed_leaves,
            "leaf_max_errors": leaf_max_errors,
        }
    except Exception as e:
        print(f"_extract_pf_chart_data error: {e}")
        return {"error": str(e)}

def _run_picket_fence(job_id: str, filepath: str, email: str, filename: str, tolerance: float = 1.0, action_tolerance: float = 0.5, mlc_type: str = "Millennium", edge_threshold: float = 50.0):
    try:
        print(f"Starting Picket Fence analysis for {filename}")
        _validate_dicom_type(filepath, "picket_fence")
        pf = PicketFence(filepath, mlc=mlc_type)
        pf.analyze(tolerance=tolerance, action_tolerance=action_tolerance, edge_threshold=edge_threshold)

        # Diagnostic: log pylinac result structure so we know exactly what data is available
        try:
            _res = pf.results_data()
            _ebl = getattr(_res, "mlc_errors_by_leaf", None)
            print(f"[PF DIAG] results_data type: {type(_res).__name__}")
            print(f"[PF DIAG] mlc_errors_by_leaf: {type(_ebl).__name__ if _ebl is not None else 'None'}, len={len(_ebl) if isinstance(_ebl, dict) else 'N/A'}")
            if isinstance(_ebl, dict) and len(_ebl) > 0:
                sample_keys = list(_ebl.keys())[:5]
                print(f"[PF DIAG] sample keys: {sample_keys}")
                print(f"[PF DIAG] sample values type: {type(list(_ebl.values())[0]).__name__}")
            print(f"[PF DIAG] all result attrs: {[a for a in dir(_res) if not a.startswith('_')]}")
        except Exception as _de:
            print(f"[PF DIAG] diagnostic failed: {_de}")

        summary   = pf.results()
        passed    = pf.passed

        plot_path = os.path.join(tempfile.gettempdir(), f"pf_{job_id}.png")
        pf.save_analyzed_image(filename=plot_path)
        image_url = upload_plot(plot_path, f"pf_{job_id}.png")

        chart_data = _extract_pf_chart_data(pf)

        # Extract MLC info for frontend so it can compute outside-field count correctly
        num_leaves = 60  # default fallback
        try:
            mlc_obj = getattr(pf, "mlc", None)
            if mlc_obj is not None:
                num_leaves = int(getattr(mlc_obj, "num_leaves", 60))
            print(f"[PF] num_leaves resolved to: {num_leaves}")
        except Exception as _mle:
            print(f"[PF] Could not read num_leaves: {_mle}")

        save_analysis(email=email, test_type="Picket Fence", filename=filename,
                      passed=passed, summary=summary, image_url=image_url,
                      chart_data=chart_data, job_id=job_id)

        jobs[job_id] = {
            "status": "Success",
            "passed": passed,
            "analysis_summary": summary,
            "image_url": image_url,
            "chart_data": chart_data,
            "num_leaves": num_leaves,
            "mlc_type": mlc_type,
        }
        print(f"Picket Fence analysis completed successfully for {filename}")
    except ValueError as e:
        print(f"Picket Fence validation error: {e}")
        jobs[job_id] = {"status": "Error", "message": str(e)}
    except Exception as e:
        print(f"Picket Fence analysis failed: {e}")
        jobs[job_id] = {"status": "Error", "message": f"Picket Fence analysis failed: {e}"}
    finally:
        try:
            os.remove(filepath)
        except Exception:
            pass
        try:
            plot_path = os.path.join(tempfile.gettempdir(), f"pf_{job_id}.png")
            os.remove(plot_path)
        except Exception:
            pass
        cleanup()

@app.post("/analyze")
async def analyze_picket_fence(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    tolerance: float = 1.0,
    action_tolerance: float = 0.5,
    mlc_type: str = "Millennium",
    edge_threshold: float = 50.0,
    u=Depends(get_current_user),
):
    try:
        # Validate file
        if not file.filename:
            raise HTTPException(400, "No file provided.")
            
        if not file.filename.lower().endswith(".dcm"):
            raise HTTPException(400, "Only .dcm DICOM files are supported.")

        # Read and validate file content
        contents = await file.read()
        if len(contents) == 0:
            raise HTTPException(400, "File is empty.")
            
        if len(contents) > 50 * 1024 * 1024:  # 50MB limit
            raise HTTPException(400, "File too large. Maximum size is 50MB.")

        job_id   = str(uuid.uuid4())
        tmp_dir  = tempfile.gettempdir()
        filepath = os.path.join(tmp_dir, f"pf_{job_id}.dcm")

        with open(filepath, "wb") as f:
            f.write(contents)

        jobs[job_id] = {"status": "Processing"}
        background_tasks.add_task(_run_picket_fence, job_id=job_id, filepath=filepath,
                                   email=u["email"], filename=file.filename,
                                   tolerance=tolerance, action_tolerance=action_tolerance,
                                   mlc_type=mlc_type, edge_threshold=edge_threshold)
        return {"status": "Queued", "job_id": job_id}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Upload failed: {str(e)}")

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
            for i, val in enumerate(results.circle_profile.values):
                radial_profile.append({
                    "index": i,
                    "value": round(float(val), 4),
                })
        except Exception:
            pass

        return {"spokes": spokes, "wobble_radius_mm": wobble, "radial_profile": radial_profile}
    except Exception as e:
        return {"error": str(e)}

def _run_starshot(job_id: str, filepath: str, email: str, filename: str):
    try:
        _validate_dicom_type(filepath, "starshot")
        ss = Starshot(filepath)
        ss.analyze()

        summary   = ss.results()
        passed    = ss.passed

        plot_path = os.path.join(tempfile.gettempdir(), f"ss_{job_id}.png")
        ss.save_analyzed_image(filename=plot_path)
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
    except ValueError as e:
        print(f"Starshot validation error: {e}")
        jobs[job_id] = {"status": "Error", "message": str(e)}
    except Exception as e:
        jobs[job_id] = {"status": "Error", "message": f"Starshot analysis failed: {e}"}
    finally:
        try:
            os.remove(filepath)
        except Exception:
            pass
        try:
            plot_path = os.path.join(tempfile.gettempdir(), f"ss_{job_id}.png")
            os.remove(plot_path)
        except Exception:
            pass
        cleanup()

@app.post("/analyze/starshot")
async def analyze_starshot(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    u=Depends(get_current_user),
):
    try:
        ext = file.filename.lower().rsplit(".", 1)[-1]
        if ext not in ("dcm", "zip"):
            raise HTTPException(400, "Only .dcm or .zip files are supported for Starshot.")

        job_id   = str(uuid.uuid4())
        tmp_dir  = tempfile.gettempdir()
        filepath = os.path.join(tmp_dir, f"ss_{job_id}.{ext}")

        contents = await file.read()
        if len(contents) > 50 * 1024 * 1024:
            raise HTTPException(400, "File too large. Maximum size is 50MB.")
            
        with open(filepath, "wb") as f:
            f.write(contents)

        jobs[job_id] = {"status": "Processing"}
        background_tasks.add_task(_run_starshot, job_id=job_id, filepath=filepath,
                                   email=u["email"], filename=file.filename)
        return {"status": "Queued", "job_id": job_id}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Upload failed: {str(e)}")

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
        # Validate the first DCM file found in the directory
        dcm_files = sorted([f for f in os.listdir(dirpath) if f.lower().endswith(".dcm")])
        if dcm_files:
            _validate_dicom_type(os.path.join(dirpath, dcm_files[0]), "winston_lutz")

        wl = WinstonLutz(dirpath)
        wl.analyze()

        summary   = wl.results()
        passed    = wl.passed

        plot_path = os.path.join(dirpath, f"wl_{job_id}.png")
        wl.save_analyzed_image(filename=plot_path)
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
    except ValueError as e:
        print(f"Winston-Lutz validation error: {e}")
        jobs[job_id] = {"status": "Error", "message": str(e)}
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
    try:
        if not files:
            raise HTTPException(400, "No files provided.")

        # Validate files
        total_size = 0
        for file in files:
            if not file.filename.lower().endswith(".dcm"):
                raise HTTPException(400, "Only .dcm DICOM files are supported for Winston-Lutz.")
            total_size += len(await file.read())
            
        if total_size > 50 * 1024 * 1024:
            raise HTTPException(400, "Files too large. Maximum total size is 50MB.")

        job_id   = str(uuid.uuid4())
        tmp_dir  = tempfile.mkdtemp()

        # Reset file pointers and save
        for file in files:
            file.file.seek(0)
            contents = await file.read()
            filepath = os.path.join(tmp_dir, file.filename)
            with open(filepath, "wb") as f:
                f.write(contents)

        jobs[job_id] = {"status": "Processing"}
        background_tasks.add_task(_run_winston_lutz, job_id=job_id, dirpath=tmp_dir,
                                   email=u["email"], filename=f"{len(files)} files")
        return {"status": "Queued", "job_id": job_id}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Upload failed: {str(e)}")

# ═════════════════════════════════════════════════════════════════════════════
# CONGRUENCE
# ═════════════════════════════════════════════════════════════════════════════

def _extract_congruence_chart_data(fa) -> dict:
    try:
        results = fa.results_data()
        
        edges = {}
        try:
            # Try different attribute names based on pylinac version
            if hasattr(results, 'top'):
                edges = {
                    "top": round(float(results.top), 4),
                    "bottom": round(float(results.bottom), 4),
                    "left": round(float(results.left), 4),
                    "right": round(float(results.right), 4),
                }
        except Exception:
            pass

        field_size = None
        try:
            if hasattr(results, 'field_size_x_mm'):
                field_size = {
                    "x": round(float(results.field_size_x_mm), 2),
                    "y": round(float(results.field_size_y_mm), 2),
                }
        except Exception:
            pass

        return {"edges": edges, "field_size": field_size}
    except Exception as e:
        return {"error": str(e)}

def _run_congruence(job_id: str, filepath: str, email: str, filename: str):
    try:
        _validate_dicom_type(filepath, "congruence")
        fa = FieldAnalysis(filepath)
        fa.analyze()

        summary   = fa.results()
        passed    = fa.passed

        plot_path = os.path.join(tempfile.gettempdir(), f"cg_{job_id}.png")
        fa.save_analyzed_image(filename=plot_path)
        image_url = upload_plot(plot_path, f"cg_{job_id}.png")

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
    except ValueError as e:
        print(f"Congruence validation error: {e}")
        jobs[job_id] = {"status": "Error", "message": str(e)}
    except Exception as e:
        jobs[job_id] = {"status": "Error", "message": f"Congruence analysis failed: {e}"}
    finally:
        try:
            os.remove(filepath)
        except Exception:
            pass
        try:
            plot_path = os.path.join(tempfile.gettempdir(), f"cg_{job_id}.png")
            os.remove(plot_path)
        except Exception:
            pass
        cleanup()

@app.post("/analyze/congruence")
async def analyze_congruence(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    u=Depends(get_current_user),
):
    try:
        if not file.filename.lower().endswith(".dcm"):
            raise HTTPException(400, "Only .dcm DICOM files are supported for Congruence.")

        job_id   = str(uuid.uuid4())
        tmp_dir  = tempfile.gettempdir()
        filepath = os.path.join(tmp_dir, f"cg_{job_id}.dcm")

        contents = await file.read()
        if len(contents) > 50 * 1024 * 1024:
            raise HTTPException(400, "File too large. Maximum size is 50MB.")
            
        with open(filepath, "wb") as f:
            f.write(contents)

        jobs[job_id] = {"status": "Processing"}
        background_tasks.add_task(_run_congruence, job_id=job_id, filepath=filepath,
                                   email=u["email"], filename=file.filename)
        return {"status": "Queued", "job_id": job_id}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Upload failed: {str(e)}")
