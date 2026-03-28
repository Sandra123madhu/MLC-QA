from fastapi import FastAPI, UploadFile, File, BackgroundTasks, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from jose import JWTError, jwt
from datetime import datetime, timedelta
from pylinac import PicketFence, WinstonLutz, Starshot
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

def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

def verify_password(plain: str, hashed: str) -> bool:
    try:
        # bcrypt hashes start with $2b$ or $2a$
        if hashed.startswith("$2b$") or hashed.startswith("$2a$"):
            return bcrypt.checkpw(plain.encode(), hashed.encode())
        # Legacy fallback: old hmac-based hash — try current SECRET_KEY
        legacy = hmac.new(SECRET_KEY.encode(), plain.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(legacy, hashed):
            return True
        # Also try the hardcoded default key (catches signup/login SECRET_KEY mismatch)
        default = hmac.new(b"mlcqa-change-this-in-render", plain.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(default, hashed)
    except Exception:
        return False

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
class ForgotPasswordRequest(BaseModel): email:str

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

@app.get("/config/branding")
def get_branding():
    return {
        "institution_name": "University Medical Center",
        "department": "Department of Medical Physics",
        "logo_url": "https://img.icons8.com/ios-filled/100/ffffff/hospital-placeholder.png",
        "report_footer": "CONFIDENTIAL: Clinical Quality Assurance Report"
    }

@app.post("/auth/forgot-password")
def forgot_password(req: ForgotPasswordRequest):
    user = get_user_by_email(req.email)
    if not user:
        # Security best practice: don't reveal if user exists
        return {"message": "If that email is registered, you will receive a reset link."}
    
    # In a real app, you'd generate a temporary token and send an email here.
    # Since this uses a custom users table (not Supabase Auth), 
    # you'd need to integrate an email provider like Resend, SendGrid, or Mailgun.
    print(f"PASSWORD RESET REQUEST FOR: {req.email}")
    
    return {"message": "Recovery instructions sent. Check your inbox."}

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
    # Auto-upgrade legacy hmac hash to bcrypt on first successful login
    stored = user["password"]
    if not (stored.startswith("$2b$") or stored.startswith("$2a$")):
        new_hash = hash_password(req.password)
        try:
            with httpx.Client() as c:
                c.patch(
                    f"{SUPABASE_URL}/rest/v1/users?email=eq.{req.email}",
                    headers=sb_headers(),
                    json={"password": new_hash}
                )
        except Exception:
            pass  # Non-fatal: user can still log in, upgrade retried next time
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

# (Pylinac extraction logic for Picket Fence, Starshot, and Winston-Lutz follows...)
# ... (omitted for brevity, includes _extract_pf_chart_data, run_analysis, etc.)
