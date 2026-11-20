"""FastAPI entry point for the Finance Workbench local backend."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pathlib import Path
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .database import check_db_connection, init_db
from .main_config import UPLOADS_DIR
from .routers import business, cashier, invoices, reference, reports, storage, uploads, vouchers, migration, v11


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title="Finance Workbench Local API",
    description="本地财务工作台 API：多账套隔离 SQLite、凭证、出纳、发票和账表查询。",
    version="0.7.0",
    lifespan=lifespan,
)

# Local development: supports file:// origins (Origin: null) and localhost ports.
# Credentials are deliberately disabled; do not expose this unauthenticated API to a LAN/WAN.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(uploads.router)
app.include_router(vouchers.router)
app.include_router(cashier.router)
app.include_router(invoices.router)
app.include_router(reports.router)
app.include_router(reference.router)
app.include_router(storage.router)
app.include_router(migration.router)
app.include_router(business.router)
app.include_router(v11.router)

app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")


@app.get("/api/health")
def health() -> dict[str, str]:
    if check_db_connection():
        return {"status": "ok", "db": "connected"}
    return {"status": "error", "db": "disconnected"}


@app.get("/app", include_in_schema=False)
def frontend() -> FileResponse:
    """Serve only the single-file frontend; do not expose the project tree."""
    return FileResponse(Path(__file__).resolve().parent.parent / "index.html", media_type="text/html")


@app.get("/")
def root() -> dict[str, str]:
    return {
        "service": "Finance Workbench Local API",
        "health": "/api/health",
        "docs": "/docs",
    }
