"""FastAPI entry point for the Finance Workbench local backend."""
from __future__ import annotations

from contextlib import asynccontextmanager, suppress
import asyncio,os,sqlite3

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pathlib import Path
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from .database import check_db_connection, init_db
from .main_config import UPLOADS_DIR
from . import collab_auth
from .collab_db import init_collaboration
from .routers import collaboration
from .routers import business, cashier, invoices, reference, reports, storage, uploads, vouchers, migration, v11, finance_controls, budgets


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    finance_controls.init_controls()
    init_collaboration()
    bootstrap=os.getenv('FW_BOOTSTRAP_ADMIN_NAME')
    bootstrap_password=os.getenv('FW_BOOTSTRAP_ADMIN_PASSWORD')
    if bootstrap and bootstrap_password:
        from .database import get_connection
        conn=get_connection()
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                if not conn.execute('SELECT 1 FROM fw_users LIMIT 1').fetchone():
                    item=finance_controls.Credentials(name=bootstrap,password=bootstrap_password)
                    finance_controls.add_user(conn,item,'admin')
                    for book in conn.execute('SELECT id FROM account_sets').fetchall():
                        conn.execute("INSERT INTO fw_book_roles VALUES(?,?,'director','[]',1)",(book['id'],bootstrap))
        finally: conn.close()
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    from .scheduler import loop
    task=asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError): await task


app = FastAPI(
    title="Finance Workbench Local API",
    description="本地财务工作台 API：多账套隔离 SQLite、凭证、出纳、发票和账表查询。",
    version="0.12.0",
    lifespan=lifespan,
)

app.middleware("http")(collab_auth.enforce_controls)

# Local development: supports file:// origins (Origin: null) and localhost ports.
# Credentials are deliberately disabled; do not expose this unauthenticated API to a LAN/WAN.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("FW_CORS_ORIGINS","http://127.0.0.1:8000,http://localhost:8000,null").split(","),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(collaboration.router)
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
app.include_router(finance_controls.router)
app.include_router(budgets.router)

app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")


@app.get("/api/health")
def health() -> dict[str, str]:
    if check_db_connection():
        return {"status": "ok", "db": "connected"}
    return {"status": "error", "db": "disconnected"}


@app.get('/assets/collaboration.js',include_in_schema=False)
def collaboration_script():
    return FileResponse(Path(__file__).resolve().parent.parent/'assets/collaboration.js',media_type='application/javascript')

@app.get('/assets/collaboration.css',include_in_schema=False)
def collaboration_styles():
    return FileResponse(Path(__file__).resolve().parent.parent/'assets/collaboration.css',media_type='text/css')

@app.exception_handler(sqlite3.IntegrityError)
async def constraint_error(request,exc):
    from fastapi.responses import JSONResponse
    return JSONResponse({'detail':'数据约束或锁定保护拒绝此操作，请刷新后核对'},status_code=409)

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
