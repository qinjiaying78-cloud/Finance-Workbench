from __future__ import annotations

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile

from ..main_config import UPLOADS_DIR, MAX_UPLOAD_BYTES

router = APIRouter(prefix="/api", tags=["文件上传"])
ALLOWED = {".jpg": "jpeg", ".jpeg": "jpeg", ".png": "png", ".pdf": "pdf"}


def _detect_type(content: bytes) -> str | None:
    if content.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"%PDF-"):
        return "pdf"
    return None


@router.post("/upload", status_code=201)
@router.post("/uploads", status_code=201, include_in_schema=False)
async def upload_attachment(file: UploadFile = File(...)) -> dict[str, object]:
    """Upload JPG/PNG/PDF into uploads/YYYYMM with a UUID filename.

    `/api/uploads` remains as a hidden compatibility alias for the previous
    initialization version; the documented endpoint is `/api/upload`.
    """
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED:
        await file.close()
        raise HTTPException(status_code=415, detail="仅支持 JPG、PNG 或 PDF 文件")

    try:
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        if not content:
            raise HTTPException(status_code=400, detail="上传文件为空")
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(status_code=413, detail="单个文件不能超过 15 MB")
        detected = _detect_type(content)
        if detected is None or detected != ALLOWED[suffix]:
            raise HTTPException(status_code=415, detail="文件扩展名与实际文件内容不匹配")

        month_folder = datetime.now().strftime("%Y%m")
        destination_dir = UPLOADS_DIR / month_folder
        destination_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid4().hex}{suffix}"
        destination = destination_dir / filename
        destination.write_bytes(content)
        relative_url = f"/uploads/{month_folder}/{filename}"
        return {
            "filename": filename,
            "url": relative_url,
            "size_bytes": len(content),
            "content_type": {"jpeg": "image/jpeg", "png": "image/png", "pdf": "application/pdf"}[detected],
        }
    finally:
        await file.close()
