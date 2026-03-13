from fastapi import FastAPI, UploadFile, File, BackgroundTasks, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from jose import JWTError, jwt
from datetime import datetime, timedelta
from pylinac import PicketFence
import os, shutil, tempfile, uuid, hashlib, hmac
import httpx

# ─── Config ───────────────────────────────────────────────────────────────────
SECRET_KEY   = os.environ.get("SECRET_KEY", "mlcqa-change-this-in-render")
ALGORITHM    = "HS256"
TOKEN_HOURS  = 24
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://swxrncaezcthahehhuu.supabase.co")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# ─── Supabase REST helpers ────────────────────────────────────────────────────
def sb_headers():
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

def get_user_by_email(email: str):
    with httpx.Client() as client:
        res = client.get(
            f"{SUPABASE_URL}/rest/v1/users?email=eq.{email}&limit=1",
            headers=sb_headers()
        )
    if res.status_code == 200 and res.json():
        return res.json()[0]
    return None

def create_user(name: str, email: str, hashed_password: str):
    with httpx.Client() as client:
        res = client.post(
            f"{SUPABASE_URL}/rest/v1/users",
            headers=sb_headers(),
            json={"name": name, "email": email, "password": hashed_password}
        )
    if res.status_code in (200, 201):
        data = res.json()
        return data[0] if isinstance(data, list) else data
    raise HTTPException(status_code=500, detail=f"Could not create user: {res.text}")

def save_analysis(user_email: str, test_type: str, filename: str, passed: bool, summary: str):
    with httpx.Client() as client:
        res = client.post(
            f"{SUPABASE_URL}/rest/v1/analyses",
            headers=sb_headers(),
            json={
                "user_email": user_email,
                "test_type": test_type,
                "filename": filename,
                "passed": passed,
                "summary": summary
            }
        )
    return res.status_code in (200, 201)

def get_user_analyses(user_email: str):
    with httpx.Client() as client:
        res = client.get(
            f"{SUPABASE_URL}/rest/v1/analyses"
            f"?user_email=eq.{user_email}"
            f"&order=created_at.desc"
            f"&limit=50",
            headers=sb_headers()
        )
    if res.status_code == 200:
        return res.json()
    return []

# ─── Password hashing ─────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    return hmac.new(SECRET_KEY.encode(), password.encode(), hashlib.sha256).hexdigest()

def verify_password(plain: str, hashed: str) -> bool:
    return hmac.compare_digest(hash_password(plain), hashed)

# ─── JWT ──────────────────────────────────────────────────────────────────────
bearer_scheme = HTTPBearer()

def create_token(email: str, name: str) -> str:
    payload = {
        "sub": email,
        "name": name,
        "exp": datetime.utcnow() + timedelta(hours=TOKEN_HOURS)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        email = payload.get("sub")
        name  = payload.get("name")
        if not email:
            raise HTTPException(status_code=401, detail="Invalid token")
        return {"email": email, "name": name}
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

# ─── Pydantic Schemas ─────────────────────────────────────────────────────────
class SignupRequest(BaseModel):
    name: str
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

# ─── App ──────────────────────────────────────────────────────────────────────
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory job store
jobs = {}
MAX_JOBS = 50

def cleanup_old_jobs():
    if len(jobs) > MAX_JOBS:
        for k in list(jobs.keys())[:len(jobs) - MAX_JOBS]:
            del jobs[k]

# ─── Health Check ─────────────────────────────────────────────────────────────
@app.get("/")
@app.head("/")
def home():
    return {"status": "MLC QA Backend is Live and listening."}

# ─── Auth Routes ──────────────────────────────────────────────────────────────
@app.post("/auth/signup")
def signup(req: SignupRequest):
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Name is required.")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    existing = get_user_by_email(req.email)
    if existing:
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

# ─── History Route ─────────────────────────────────────────────────────────────
@app.get("/history")
def get_history(current_user: dict = Depends(get_current_user)):
    analyses = get_user_analyses(current_user["email"])
    return {"analyses": analyses}

# ─── Analysis Routes ──────────────────────────────────────────────────────────
def run_analysis(job_id: str, temp_path: str, user_email: str, filename: str):
    try:
        pf = PicketFence(temp_path)
        pf.analyze(tolerance=0.5, action_tolerance=0.25)
        summary = pf.results()
        passed  = pf.passed
        jobs[job_id] = {
            "status": "Success",
            "passed": passed,
            "analysis_summary": summary
        }
        # ── Save to Supabase ──
        save_analysis(
            user_email=user_email,
            test_type="Picket Fence",
            filename=filename,
            passed=passed,
            summary=summary
        )
    except Exception as e:
        jobs[job_id] = {"status": "Error", "message": f"Analysis Error: {str(e)}"}
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        cleanup_old_jobs()

@app.post("/analyze")
async def analyze_mlc(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user)
):
    if not file.filename.lower().endswith(".dcm"):
        return {"status": "Error", "message": "Only DICOM (.dcm) files are supported."}
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".dcm", dir="/tmp") as tmp:
            shutil.copyfileobj(file.file, tmp)
            temp_path = tmp.name
        job_id = str(uuid.uuid4())
        jobs[job_id] = {"status": "Processing"}
        background_tasks.add_task(
            run_analysis, job_id, temp_path,
            current_user["email"], file.filename
        )
        return {"status": "Processing", "job_id": job_id}
    except Exception as e:
        return {"status": "Error", "message": f"Upload Error: {str(e)}"}

@app.get("/result/{job_id}")
def get_result(job_id: str, current_user: dict = Depends(get_current_user)):
    if job_id not in jobs:
        return {"status": "Error", "message": "Job ID not found."}
    return jobs[job_id]
