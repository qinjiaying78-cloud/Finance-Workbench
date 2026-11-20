from __future__ import annotations

import json
import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from ..database import get_connection
from ..schemas import CashierBatchCreate, CashierBatchUpdate, CashierCreate, CashierIdsRequest, CashierToVoucherRequest, CashierUpdate
from .common import (
    assert_period_unlocked,
    assert_account_set,
    cents_to_yuan,
    ensure_account,
    generate_voucher_no,
    money_to_cents,
    validate_upload_url,
    voucher_dict,
)

router = APIRouter(prefix="/api/cashier", tags=["出纳管理"])


def get_db():
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def _record_dict(row: sqlite3.Row) -> dict:
    item = dict(row)
    item["amount"] = cents_to_yuan(item["amount"])
    try: item["extra"] = json.loads(item.pop("extra_json") or "{}")
    except (json.JSONDecodeError, TypeError): item["extra"] = {}
    return item


def _resolve_account(conn: sqlite3.Connection, requested: str | None, fallback_name: str | None = None, account_set_id: str = "default") -> str:
    key = requested or fallback_name
    if not key:
        raise HTTPException(status_code=422, detail="必须指定现金/银行科目编码")
    row = conn.execute(
        "SELECT code FROM accounts WHERE account_set_id=? AND (code=? OR name=?) ORDER BY CASE WHEN code=? THEN 0 ELSE 1 END LIMIT 1",
        (account_set_id, key, key, key),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=422, detail=f"找不到会计科目：{key}")
    ensure_account(conn, row["code"], account_set_id)
    return row["code"]


@router.get("")
def list_cashier_records(
    start_date: date | None = None,
    end_date: date | None = None,
    status: str | None = None,
    biz_type: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    account_set_id: str = Query(default="default"),
    conn: sqlite3.Connection = Depends(get_db),
):
    assert_account_set(conn, account_set_id)
    where: list[str] = ["account_set_id=?"]
    params: list[object] = [account_set_id]
    if start_date:
        where.append("record_date >= ?")
        params.append(start_date.isoformat())
    if end_date:
        where.append("record_date <= ?")
        params.append(end_date.isoformat())
    if start_date and end_date and end_date < start_date:
        raise HTTPException(status_code=422, detail="end_date 不能早于 start_date")
    if status:
        if status not in {"PENDING", "DONE", "POSTED"}:
            raise HTTPException(status_code=422, detail="出纳状态无效")
        where.append("status=?")
        params.append(status)
    if biz_type:
        if biz_type not in {"RECEIPT", "PAYMENT", "TRANSFER"}:
            raise HTTPException(status_code=422, detail="出纳业务类型无效")
        where.append("biz_type=?")
        params.append(biz_type)
    sql = "SELECT * FROM cashier_records"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY record_date DESC,id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = conn.execute(sql, params).fetchall()
    return {"account_set_id": account_set_id, "items": [_record_dict(row) for row in rows], "limit": limit, "offset": offset}


@router.post("", status_code=201)
def create_cashier_record(payload: CashierCreate, account_set_id: str = Query(default="default"), conn: sqlite3.Connection = Depends(get_db)):
    assert_account_set(conn, account_set_id)
    receipt_url = validate_upload_url(payload.receipt_img_url)
    amount = money_to_cents(payload.amount)
    try:
        cursor = conn.execute(
            "INSERT INTO cashier_records(account_set_id,legacy_id,transfer_id,direction,extra_json,record_date,biz_type,amount,account_name,contact_name,source_doc,receipt_img_url,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (account_set_id, payload.legacy_id, payload.transfer_id, payload.direction,
             json.dumps(payload.extra, ensure_ascii=False), payload.record_date.isoformat(), payload.biz_type,
             amount, payload.account_name, payload.contact_name, payload.source_doc, receipt_url, payload.status),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback(); raise HTTPException(409, f"出纳记录保存失败：{exc}") from exc
    row = conn.execute("SELECT * FROM cashier_records WHERE account_set_id=? AND id=?", (account_set_id, cursor.lastrowid)).fetchone()
    return _record_dict(row)


@router.put("/{record_id}")
def update_cashier_record(record_id: int, payload: CashierUpdate, account_set_id: str = "default", conn: sqlite3.Connection = Depends(get_db)):
    try:
        assert_account_set(conn, account_set_id)
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute("SELECT * FROM cashier_records WHERE account_set_id=? AND id=?", (account_set_id, record_id)).fetchone()
        if not current:
            raise HTTPException(status_code=404, detail="出纳记录不存在")
        if current["status"] == "POSTED" or current["linked_voucher_id"] is not None:
            raise HTTPException(status_code=409, detail="已关联凭证的出纳记录不能直接修改")

        updates = payload.model_dump(exclude_unset=True)
        if not updates:
            raise HTTPException(status_code=422, detail="没有提供需要更新的字段")
        if "status" in updates:
            new_status = updates["status"]
            if current["status"] == "DONE" and new_status != "DONE":
                raise HTTPException(status_code=409, detail="出纳记录不能从 DONE 回退到 PENDING")
        if "receipt_img_url" in updates:
            updates["receipt_img_url"] = validate_upload_url(updates["receipt_img_url"])
        if "extra" in updates:
            updates["extra_json"] = json.dumps(updates.pop("extra") or {}, ensure_ascii=False)
        if "record_date" in updates and updates["record_date"] is not None:
            updates["record_date"] = updates["record_date"].isoformat()
        if "amount" in updates and updates["amount"] is not None:
            updates["amount"] = money_to_cents(updates["amount"])
        allowed = {"record_date", "biz_type", "amount", "account_name", "contact_name", "source_doc", "receipt_img_url", "status", "transfer_id", "direction", "extra_json"}
        updates = {k: v for k, v in updates.items() if k in allowed}
        assignments = ",".join(f"{key}=?" for key in updates)
        conn.execute(f"UPDATE cashier_records SET {assignments} WHERE account_set_id=? AND id=?", (*updates.values(), account_set_id, record_id))
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise HTTPException(status_code=422, detail=f"出纳记录更新失败：{exc}") from exc
    row = conn.execute("SELECT * FROM cashier_records WHERE account_set_id=? AND id=?", (account_set_id, record_id)).fetchone()
    return _record_dict(row)


@router.put("")
def update_cashier_record_by_query(
    id: int = Query(..., ge=1, description="需要更新的出纳记录 ID"),
    payload: CashierUpdate = ...,
    account_set_id: str = Query(default="default"),
    conn: sqlite3.Connection = Depends(get_db),
):
    """Compatibility form: PUT /api/cashier?id=<record_id>.

    The canonical REST path remains PUT /api/cashier/{record_id}.
    """
    return update_cashier_record(id, payload, account_set_id, conn)


@router.post("/{record_id}/to-voucher", status_code=201)
def cashier_to_voucher(
    record_id: int,
    payload: CashierToVoucherRequest,
    account_set_id: str = Query(default="default"),
    conn: sqlite3.Connection = Depends(get_db),
):
    assert_account_set(conn, account_set_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        record = conn.execute("SELECT * FROM cashier_records WHERE account_set_id=? AND id=?", (account_set_id, record_id)).fetchone()
        if not record:
            raise HTTPException(status_code=404, detail="出纳记录不存在")
        if record["status"] == "POSTED" or record["linked_voucher_id"] is not None:
            raise HTTPException(status_code=409, detail="该出纳记录已经生成或过账凭证")

        voucher_date = payload.voucher_date or date.fromisoformat(record["record_date"])
        assert_period_unlocked(conn, voucher_date, account_set_id)
        amount = record["amount"]
        contact_id = None
        if record["contact_name"]:
            contact = conn.execute(
                "SELECT id FROM contacts WHERE account_set_id=? AND name=? ORDER BY id LIMIT 1", (account_set_id, record["contact_name"])
            ).fetchone()
            contact_id = contact["id"] if contact else None

        if record["biz_type"] == "TRANSFER":
            source_code = _resolve_account(conn, payload.cash_account_code, record["account_name"], account_set_id)
            if not payload.transfer_to_account_code:
                raise HTTPException(status_code=422, detail="银行转账必须提供 transfer_to_account_code")
            target_code = _resolve_account(conn, payload.transfer_to_account_code, account_set_id=account_set_id)
            if source_code == target_code:
                raise HTTPException(status_code=422, detail="转出与转入科目不能相同")
            lines = [
                (target_code, None, "银行转入", amount, 0),
                (source_code, None, "银行转出", 0, amount),
            ]
        else:
            if not payload.counterpart_account_code:
                raise HTTPException(status_code=422, detail="收付款业务必须提供 counterpart_account_code")
            cash_code = _resolve_account(conn, payload.cash_account_code, record["account_name"], account_set_id)
            counterpart_code = _resolve_account(conn, payload.counterpart_account_code, account_set_id=account_set_id)
            if cash_code == counterpart_code:
                raise HTTPException(status_code=422, detail="现金/银行科目不能与对方科目相同")
            if record["biz_type"] == "RECEIPT":
                lines = [
                    (cash_code, None, "收到款项", amount, 0),
                    (counterpart_code, contact_id, "收款核销/往来", 0, amount),
                ]
            else:
                lines = [
                    (counterpart_code, contact_id, "支付款项/往来", amount, 0),
                    (cash_code, None, "银行付款", 0, amount),
                ]

        voucher_no = payload.voucher_no or generate_voucher_no(conn, voucher_date, prefix="出纳", account_set_id=account_set_id)
        summary = payload.summary or record["source_doc"] or f"出纳{ {'RECEIPT':'收款','PAYMENT':'付款','TRANSFER':'转账'}[record['biz_type']] }"
        attachments = [record["receipt_img_url"]] if record["receipt_img_url"] else []
        cursor = conn.execute(
            "INSERT INTO vouchers(account_set_id,voucher_no,voucher_date,summary,status,attachment_urls) VALUES(?,?,?, ?,'DRAFT',?)",
            (account_set_id, voucher_no, voucher_date.isoformat(), summary, json.dumps(attachments, ensure_ascii=False)),
        )
        voucher_id = cursor.lastrowid
        conn.executemany(
            "INSERT INTO voucher_entries(account_set_id,voucher_id,account_code,contact_id,summary,debit_amount,credit_amount) VALUES(?,?,?,?,?,?,?)",
            [(account_set_id, voucher_id, *line) for line in lines],
        )
        conn.execute(
            "UPDATE cashier_records SET status='DONE',linked_voucher_id=? WHERE account_set_id=? AND id=?",
            (voucher_id, account_set_id, record_id),
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise HTTPException(status_code=409, detail=f"出纳转凭证失败：{exc}") from exc
    row = conn.execute("SELECT * FROM vouchers WHERE account_set_id=? AND id=?", (account_set_id, voucher_id)).fetchone()
    updated_record = conn.execute("SELECT * FROM cashier_records WHERE account_set_id=? AND id=?", (account_set_id, record_id)).fetchone()
    return {"cashier": _record_dict(updated_record), "voucher": voucher_dict(conn, row)}


@router.delete("/{record_id}")
def delete_cashier_record(record_id: int, account_set_id: str = Query(default="default"), conn: sqlite3.Connection = Depends(get_db)):
    assert_account_set(conn, account_set_id)
    row = conn.execute("SELECT status,linked_voucher_id FROM cashier_records WHERE account_set_id=? AND id=?", (account_set_id, record_id)).fetchone()
    if not row: raise HTTPException(404, "出纳记录不存在")
    if row["linked_voucher_id"] is not None or row["status"] == "POSTED": raise HTTPException(409, "已关联总账凭证的出纳记录不能删除")
    conn.execute("DELETE FROM cashier_records WHERE account_set_id=? AND id=?", (account_set_id, record_id)); conn.commit()
    return {"deleted": True, "id": record_id}


@router.post("/link-voucher/{voucher_id}")
def link_cashier_to_voucher(voucher_id: int, record_ids: list[int], account_set_id: str = Query(default="default"), conn: sqlite3.Connection = Depends(get_db)):
    assert_account_set(conn, account_set_id)
    if not record_ids: raise HTTPException(422, "至少选择一条出纳记录")
    try:
        conn.execute("BEGIN IMMEDIATE")
        voucher = conn.execute("SELECT id,status FROM vouchers WHERE account_set_id=? AND id=?", (account_set_id, voucher_id)).fetchone()
        if not voucher: raise HTTPException(404, "凭证不存在")
        rows = conn.execute(f"SELECT id,status,linked_voucher_id FROM cashier_records WHERE account_set_id=? AND id IN ({','.join('?' for _ in record_ids)})", (account_set_id, *record_ids)).fetchall()
        if len(rows) != len(set(record_ids)): raise HTTPException(404, "部分出纳记录不存在")
        if any(r["linked_voucher_id"] not in (None, voucher_id) for r in rows): raise HTTPException(409, "出纳记录已关联其他凭证")
        new_status = "POSTED" if voucher["status"] in ("POSTED", "REVERSED") else "DONE"
        conn.executemany("UPDATE cashier_records SET linked_voucher_id=?,status=? WHERE account_set_id=? AND id=?", [(voucher_id,new_status,account_set_id,rid) for rid in set(record_ids)])
        conn.commit()
    except HTTPException: conn.rollback(); raise
    except sqlite3.IntegrityError as exc: conn.rollback(); raise HTTPException(409, f"出纳关联失败：{exc}") from exc
    return {"linked": len(set(record_ids)), "voucher_id": voucher_id, "status": new_status}


@router.post("/batch", status_code=201)
def create_cashier_batch(payload: CashierBatchCreate, account_set_id: str = Query(default="default"), conn: sqlite3.Connection = Depends(get_db)):
    """Atomically insert related cashier rows, e.g. both sides of a bank transfer."""
    assert_account_set(conn, account_set_id)
    ids=[r.legacy_id for r in payload.records if r.legacy_id]
    if len(ids)!=len(set(ids)): raise HTTPException(422, "批量出纳记录的 legacy_id 不能重复")
    try:
        conn.execute("BEGIN IMMEDIATE")
        inserted=[]
        for item in payload.records:
            url=validate_upload_url(item.receipt_img_url)
            cur=conn.execute("""INSERT INTO cashier_records(account_set_id,legacy_id,transfer_id,direction,extra_json,record_date,biz_type,amount,account_name,contact_name,source_doc,receipt_img_url,status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(account_set_id,item.legacy_id,item.transfer_id,item.direction,json.dumps(item.extra,ensure_ascii=False),item.record_date.isoformat(),item.biz_type,money_to_cents(item.amount),item.account_name,item.contact_name,item.source_doc,url,item.status))
            inserted.append(cur.lastrowid)
        conn.commit()
    except HTTPException: conn.rollback(); raise
    except sqlite3.IntegrityError as exc: conn.rollback(); raise HTTPException(409, f"批量出纳保存失败，已整体回滚：{exc}") from exc
    rows=[conn.execute("SELECT * FROM cashier_records WHERE account_set_id=? AND id=?",(account_set_id,rid)).fetchone() for rid in inserted]
    return {"account_set_id":account_set_id,"count":len(rows),"items":[_record_dict(r) for r in rows]}


@router.put("/batch/update")
def update_cashier_batch(payload: CashierBatchUpdate, account_set_id: str = Query(default="default"), conn: sqlite3.Connection = Depends(get_db)):
    """Atomically update a related group of cashier records, such as transfer legs."""
    assert_account_set(conn, account_set_id)
    ids=[item.id for item in payload.items]
    if len(ids)!=len(set(ids)): raise HTTPException(422,"批量出纳记录 ID 不能重复")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for item in payload.items:
            current=conn.execute("SELECT * FROM cashier_records WHERE account_set_id=? AND id=?",(account_set_id,item.id)).fetchone()
            if not current: raise HTTPException(404,f"出纳记录不存在：{item.id}")
            if current["status"]=="POSTED" or current["linked_voucher_id"] is not None: raise HTTPException(409,f"出纳记录 {item.id} 已关联凭证，不能修改")
            updates=item.changes.model_dump(exclude_unset=True)
            if not updates: continue
            if "status" in updates and current["status"]=="DONE" and updates["status"]!="DONE": raise HTTPException(409,"出纳记录不能从 DONE 回退到 PENDING")
            if "receipt_img_url" in updates: updates["receipt_img_url"]=validate_upload_url(updates["receipt_img_url"])
            if "record_date" in updates and updates["record_date"] is not None: updates["record_date"]=updates["record_date"].isoformat()
            if "amount" in updates and updates["amount"] is not None: updates["amount"]=money_to_cents(updates["amount"])
            if "extra" in updates: updates["extra_json"]=json.dumps(updates.pop("extra") or {},ensure_ascii=False)
            allowed={"record_date","biz_type","amount","account_name","contact_name","source_doc","receipt_img_url","status","transfer_id","direction","extra_json"}
            updates={k:v for k,v in updates.items() if k in allowed}
            if updates:
                clause=",".join(f"{key}=?" for key in updates)
                conn.execute(f"UPDATE cashier_records SET {clause} WHERE account_set_id=? AND id=?",(*updates.values(),account_set_id,item.id))
        conn.commit()
    except HTTPException: conn.rollback(); raise
    except sqlite3.IntegrityError as exc: conn.rollback(); raise HTTPException(409,f"批量出纳更新失败，已整体回滚：{exc}") from exc
    rows=[conn.execute("SELECT * FROM cashier_records WHERE account_set_id=? AND id=?",(account_set_id,rid)).fetchone() for rid in ids]
    return {"account_set_id":account_set_id,"count":len(rows),"items":[_record_dict(r) for r in rows]}


@router.post("/batch/delete")
def delete_cashier_batch(payload: CashierIdsRequest, account_set_id: str = Query(default="default"), conn: sqlite3.Connection = Depends(get_db)):
    assert_account_set(conn, account_set_id)
    ids=list(dict.fromkeys(payload.ids))
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows=conn.execute(f"SELECT id,status,linked_voucher_id FROM cashier_records WHERE account_set_id=? AND id IN ({','.join('?' for _ in ids)})",(account_set_id,*ids)).fetchall()
        if len(rows)!=len(ids): raise HTTPException(404,"部分出纳记录不存在")
        if any(r["status"]=="POSTED" or r["linked_voucher_id"] is not None for r in rows): raise HTTPException(409,"已关联总账凭证的出纳记录不能删除")
        conn.executemany("DELETE FROM cashier_records WHERE account_set_id=? AND id=?",[(account_set_id,rid) for rid in ids])
        conn.commit()
    except HTTPException: conn.rollback(); raise
    return {"deleted":len(ids),"ids":ids}
