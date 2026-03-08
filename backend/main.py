from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

# This allows your HTML to talk to your Python code
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/")
def home():
    return {"status": "MLC QA System Online", "engine": "FastAPI"}

@app.post("/analyze")
async def analyze_mlc():
    # This is where we will eventually put the Pylinac code
    return {"result": "Placeholder for MLC Analysis"}
