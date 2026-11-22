"""Shared filesystem configuration kept separate to avoid router import cycles."""
import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = Path(os.getenv("FINANCE_UPLOADS_DIR",str(BACKEND_DIR / "uploads"))).resolve()
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
