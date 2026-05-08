# =============================================================================
# MLC QA Platform — FastAPI Backend
# =============================================================================
#
# SUPABASE TABLE SETUP — run this SQL in your Supabase SQL editor if not done:
#
#   create table if not exists analyses (
#     id          bigint generated always as identity primary key,
#     email       text not null,
#     test_type   text,
#     filename    text,
#     passed      boolean,
#     summary     text,
#     image_url   text,
#     chart_data  jsonb,
#     job_id      text,
#     created_at  timestamptz default now()
#   );
#
#   -- Disable RLS so the service-role key can read/write freely:
#   alter table analyses disable row level security;
#
#   -- Auto-delete records older than 30 days (optional):
#   -- Use Supabase cron or pg_cron for this.
#
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

def supabase_headers(use_service_key: bool = True, prefer_return: bool = False) -> dict:
    key = SUPABASE_KEY
    headers = {
        "apikey":        key,
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
    }
    if prefer_return:
        headers["Prefer"] = "return=representation"
    return headers


def cleanup():
    """Remove jobs older than 2 hours from the in-memory store."""
    cutoff = time.time() - 7200
    stale  = [k for k, v in jobs.items() if isinstance(v, dict) and v.get("_ts", time.time()) < cutoff]
    for k in stale:
        jobs.pop(k, None)


def create_token(email: str) -> str:
