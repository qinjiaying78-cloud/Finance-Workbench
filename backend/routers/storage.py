"""Durable browser-workbench storage API.

The UI has several domain-specific record shapes (workpapers, invoice ledger,
cashier rows, vouchers). This API preserves those shapes losslessly in SQLite,
scoped by account set, so the existing UI can use a local FastAPI service without
rewriting its business modules. Amounts and business rules remain in the UI's
existing domain modules; normalized REST endpoints remain available separately.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from fastapi import Request, APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..database import get_connection, ensure_default_accounts

router = APIRouter(prefix="/api/storage", tags=["前端账套持久化"])
ALLOWED_STORES = {"Workpapers", "Invoices", "CashierRecords", "VoucherRecords", "Settings", "AssetCountOriginals"}
ACCOUNT_SET_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,119}$")


class BatchWrite(BaseModel):
    account_set_id: str = "default"
    account_set_name: str = "默认账套"
    is_demo: bool = False
    stores: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    replace_stores: list[str] = Field(default_factory=list)
    insert_only: dict[str, list[str]] = Field(default_factory=dict)
    delete_ids: dict[str, list[str]] = Field(default_factory=dict)


class AccountSetUpsert(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    is_demo: bool = False


def get_db():
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def _validate_set_id(value: str) -> str:
    if not ACCOUNT_SET_RE.fullmatch(value or ""):
        raise HTTPException(status_code=422, detail="账套 ID 格式无效")
    return value


def _validate_store(value: str) -> str:
    if value not in ALLOWED_STORES:
        raise HTTPException(status_code=404, detail=f"不支持的数据表：{value}")
    return value


def _record_id(store: str, row: dict[str, Any]) -> str:
    key = "invoiceNo" if store == "Invoices" else "id"
    value = row.get(key)
    if not isinstance(value, (str, int)) or str(value).strip() == "":
        raise HTTPException(status_code=422, detail=f"{store} 记录缺少有效主键 {key}")
    return str(value)


def _assert_not_retired(conn: sqlite3.Connection, account_set_id: str) -> None:
    if conn.execute("SELECT 1 FROM retired_account_sets WHERE id=?", (account_set_id,)).fetchone():
        raise HTTPException(410, "该账套已删除，不能由旧浏览器记录重新创建")


def _ensure_account_set(conn: sqlite3.Connection, payload: BatchWrite) -> None:
    _validate_set_id(payload.account_set_id)
    _assert_not_retired(conn, payload.account_set_id)
    name = (payload.account_set_name or payload.account_set_id).strip()[:120]
    conn.execute(
        """INSERT INTO account_sets(id,name,is_demo) VALUES(?,?,?)
           ON CONFLICT(id) DO UPDATE SET name=excluded.name,is_demo=excluded.is_demo,
             updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
        (payload.account_set_id, name, int(payload.is_demo)),
    )
    ensure_default_accounts(conn, payload.account_set_id)


def _apply_batch(conn: sqlite3.Connection, payload: BatchWrite) -> dict[str, int]:
    if not payload.stores and not payload.delete_ids and not payload.replace_stores:
        raise HTTPException(status_code=422, detail="没有提供需要保存的数据")
    all_stores = set(payload.stores) | set(payload.replace_stores) | set(payload.delete_ids) | set(payload.insert_only)
    for store in all_stores:
        _validate_store(store)
    if set(payload.replace_stores) - set(payload.stores):
        raise HTTPException(status_code=422, detail="replace_stores 中的表必须同时提供 stores 数据")

    _ensure_account_set(conn, payload)
    result: dict[str, int] = {}
    for store in sorted(all_stores):
        rows = payload.stores.get(store, [])
        ids = [_record_id(store, row) for row in rows]
        if len(ids) != len(set(ids)):
            raise HTTPException(status_code=422, detail=f"{store} 本次提交包含重复记录编号")
        insert_only = set(payload.insert_only.get(store, []))
        if not insert_only.issubset(set(ids)):
            raise HTTPException(status_code=422, detail=f"{store} insert_only 编号不在提交记录中")
        delete_ids = payload.delete_ids.get(store, [])
        if len(delete_ids) != len(set(delete_ids)):
            raise HTTPException(status_code=422, detail=f"{store} 删除编号重复")

        if store in payload.replace_stores:
            conn.execute(
                "DELETE FROM app_records WHERE account_set_id=? AND store_name=?",
                (payload.account_set_id, store),
            )
        if delete_ids:
            conn.executemany(
                "DELETE FROM app_records WHERE account_set_id=? AND store_name=? AND record_id=?",
                [(payload.account_set_id, store, str(record_id)) for record_id in delete_ids],
            )

        for row, record_id in zip(rows, ids):
            exists = conn.execute(
                "SELECT 1 FROM app_records WHERE account_set_id=? AND store_name=? AND record_id=?",
                (payload.account_set_id, store, record_id),
            ).fetchone()
            if record_id in insert_only and exists:
                raise HTTPException(status_code=409, detail=f"{store} 记录编号已存在：{record_id}")
            conn.execute(
                """INSERT INTO app_records(account_set_id,store_name,record_id,payload_json)
                   VALUES(?,?,?,?)
                   ON CONFLICT(account_set_id,store_name,record_id) DO UPDATE SET
                     payload_json=excluded.payload_json,
                     updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
                (payload.account_set_id, store, record_id, json.dumps(row, ensure_ascii=False, separators=(",", ":"))),
            )
        result[store] = len(rows)
    return result


@router.get("/account-sets")
def list_account_sets(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    rows = conn.execute("SELECT * FROM account_sets ORDER BY name,id").fetchall()
    retired = [row[0] for row in conn.execute("SELECT id FROM retired_account_sets ORDER BY id")]
    from ..collab_auth import session_actor
    actor=session_actor(conn,request)
    if actor:
        permitted={r[0] for r in conn.execute('SELECT DISTINCT account_set_id FROM fw_book_roles WHERE user_name=? AND active=1',(actor['name'],))}
        rows=[r for r in rows if r['id'] in permitted]
    # The internal default tenant is needed for compatibility, but an empty
    # placeholder should not displace the first-company setup screen.
    if actor or request.query_params.get('real_books') == 'true':
        rows = [row for row in rows if row["id"] != "default" or row["name"] != "默认账套" or conn.execute("""
            SELECT 1 FROM vouchers WHERE account_set_id='default'
            UNION ALL SELECT 1 FROM cashier_records WHERE account_set_id='default'
            UNION ALL SELECT 1 FROM invoices WHERE account_set_id='default'
            UNION ALL SELECT 1 FROM accounts WHERE account_set_id='default' AND (initial_debit<>0 OR initial_credit<>0)
            UNION ALL SELECT 1 FROM app_records WHERE account_set_id='default' AND store_name<>'Settings'
                AND record_id<>'meta:seeded_vouchers'
            UNION ALL SELECT 1 FROM fw_ledger_items WHERE account_set_id='default'
            LIMIT 1
        """).fetchone()]
    return {"items": [{**dict(row), "is_demo": bool(row["is_demo"])} for row in rows], "retired_ids": retired}


@router.put("/account-sets/{account_set_id}")
def upsert_account_set(
    account_set_id: str,
    payload: AccountSetUpsert,
    conn: sqlite3.Connection = Depends(get_db),
):
    _validate_set_id(account_set_id)
    _assert_not_retired(conn, account_set_id)
    conn.execute(
        """INSERT INTO account_sets(id,name,is_demo) VALUES(?,?,?)
           ON CONFLICT(id) DO UPDATE SET name=excluded.name,is_demo=excluded.is_demo,
             updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')""",
        (account_set_id, payload.name.strip(), int(payload.is_demo)),
    )
    ensure_default_accounts(conn, account_set_id)
    conn.commit()
    row = conn.execute("SELECT * FROM account_sets WHERE id=?", (account_set_id,)).fetchone()
    return {**dict(row), "is_demo": bool(row["is_demo"])}


@router.get("/{store_name}")
def read_store(
    store_name: str,
    account_set_id: str = Query(default="default"),
    conn: sqlite3.Connection = Depends(get_db),
):
    _validate_store(store_name)
    _validate_set_id(account_set_id)
    rows = conn.execute(
        "SELECT payload_json FROM app_records WHERE account_set_id=? AND store_name=? ORDER BY rowid",
        (account_set_id, store_name),
    ).fetchall()
    items: list[Any] = []
    for row in rows:
        try:
            items.append(json.loads(row["payload_json"]))
        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail=f"{store_name} 中存在损坏的 JSON 记录")
    return {"account_set_id": account_set_id, "store": store_name, "items": items, "count": len(items)}


@router.put("/batch")
def write_batch(payload: BatchWrite, conn: sqlite3.Connection = Depends(get_db)):
    try:
        conn.execute("BEGIN IMMEDIATE")
        counts = _apply_batch(conn, payload)
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except sqlite3.Error as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"SQLite 保存失败：{exc}") from exc
    return {"status": "saved", "account_set_id": payload.account_set_id, "counts": counts}


@router.put("/{store_name}")
def write_store(
    store_name: str,
    payload: dict[str, Any] = Body(...),
    account_set_id: str = Query(default="default"),
    account_set_name: str = Query(default="默认账套"),
    is_demo: bool = Query(default=False),
    conn: sqlite3.Connection = Depends(get_db),
):
    _validate_store(store_name)
    items = payload.get("items")
    if not isinstance(items, list):
        raise HTTPException(status_code=422, detail="请求体必须包含 items 数组")
    batch = BatchWrite(
        account_set_id=account_set_id,
        account_set_name=account_set_name,
        is_demo=is_demo,
        stores={store_name: items},
        replace_stores=[store_name] if payload.get("replace") else [],
        insert_only={store_name: payload.get("insert_only_ids", [])},
        delete_ids={store_name: payload.get("delete_ids", [])},
    )
    return write_batch(batch, conn)


@router.delete("/{store_name}")
def clear_store(
    store_name: str,
    account_set_id: str = Query(default="default"),
    conn: sqlite3.Connection = Depends(get_db),
):
    _validate_store(store_name)
    _validate_set_id(account_set_id)
    conn.execute(
        "DELETE FROM app_records WHERE account_set_id=? AND store_name=?",
        (account_set_id, store_name),
    )
    conn.commit()
    return {"status": "cleared", "account_set_id": account_set_id, "store": store_name}

