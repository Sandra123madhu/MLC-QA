from fastapi import FastAPI, UploadFile, File, BackgroundTasks, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr
from sqlalchemy import create_engine, Column, Integer, String, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta
from pylinac import PicketFence
import os, shutil, tempfile, uuid

# ─── Config ───────────────────────────────────────────────────────────────────
SECRET_KEY = os.environ.get("SECRET_KEY", "mlcqa-change-this-in-production-secret")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24

DATABASE_URL = "sqlite:////tmp/mlcqa.db"

# ─── Database ─────────────────────────────────────────────────────────────────
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class UserModel(Base):
    __tablename__ = "users"
    id            = Column(Integer, primary_key=True, index=True)
    name          = Column(String, nullable=False)
    email         = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    created_at    = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ─── Auth Helpers ─────────────────────────────────────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer_scheme = HTTPBearer()

def hash_password(password: str) -> str:
    return pwd_context.hash(password)

def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)

def create_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db)
):
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if not email:
            raise HTTPException(status_code=401, detail="Invalid token")
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user = db.query(UserModel).filter(UserModel.email == email).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user

# ─── Pydantic Schemas ─────────────────────────────────────────────────────────
class SignupRequest(BaseModel):
    name: str
    email: EmailStr
    password: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

# ─── App ──────────────────────────────────────────────────────────────────────
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
def signup(req: SignupRequest, db: Session = Depends(get_db)):
    if db.query(UserModel).filter(UserModel.email == req.email).first():
        raise HTTPException(status_code=400, detail="Email already registered.")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters.")
    user = UserModel(
        name=req.name,
        email=req.email,
        hashed_password=hash_password(req.password)
    )
    db.add(user)
    db.commit()
    return {"message": "Account created successfully."}

@app.post("/auth/login")
def login(req: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(UserModel).filter(UserModel.email == req.email).first()
    if not user or not verify_password(req.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password.")
    token = create_token({"sub": user.email, "name": user.name})
    return {"access_token": token, "token_type": "bearer", "name": user.name}

@app.get("/auth/me")
def get_me(current_user: UserModel = Depends(get_current_user)):
    return {"id": current_user.id, "name": current_user.name, "email": current_user.email}

# ─── Analysis Routes ──────────────────────────────────────────────────────────
def run_analysis(job_id: str, temp_path: str):
    try:
        pf = PicketFence(temp_path)
        pf.analyze(tolerance=0.5, action_tolerance=0.25)
        jobs[job_id] = {
            "status": "Success",
            "passed": pf.passed,
            "analysis_summary": pf.results()
        }
    except Exception as e:
        jobs[job_id] = {"status": "Error", "message": f"Python Engine Error: {str(e)}"}
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
    current_user: UserModel = Depends(get_current_user)
):
    if not file.filename.lower().endswith(".dcm"):
        return {"status": "Error", "message": "Only DICOM (.dcm) files are supported."}
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".dcm", dir="/tmp") as tmp:
            shutil.copyfileobj(file.file, tmp)
            temp_path = tmp.name
        job_id = str(uuid.uuid4())
        jobs[job_id] = {"status": "Processing"}
        background_tasks.add_task(run_analysis, job_id, temp_path)
        return {"status": "Processing", "job_id": job_id}
    except Exception as e:
        return {"status": "Error", "message": f"Upload Error: {str(e)}"}

@app.get("/result/{job_id}")
def get_result(job_id: str, current_user: UserModel = Depends(get_current_user)):
    if job_id not in jobs:
        return {"status": "Error", "message": "Job ID not found."}
    return jobs[job_id]
