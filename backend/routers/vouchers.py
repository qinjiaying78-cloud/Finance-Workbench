from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from ..database import get_connection
from ..schemas import ReverseVoucherRequest, VoucherCreate, VoucherEntryIn, VoucherIdsRequest, VoucherUpdate
from .common import (
    assert_account_set, assert_period_unlocked, cents_to_yuan, ensure_account,
    generate_voucher_no, money_to_cents, now_iso, validate_period,
    validate_upload_url, voucher_dict,
)

router = APIRouter(prefix="/api/vouchers", tags=["凭证管理"])


def get_db():
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def _validate_entry_payload(conn: sqlite3.Connection, entries, account_set_id: str):
    if len(entries) < 2:
        raise HTTPException(422, "凭证分录至少需要 2 行")
    normalized: list[dict[str, Any]] = []
    debit_total = credit_total = 0
    for i, entry in enumerate(entries, 1):
        ensure_account(conn, entry.account_code, account_set_id)
        debit = money_to_cents(entry.debit_amount)
        credit = money_to_cents(entry.credit_amount)
        if debit < 0 or credit < 0:
            raise HTTPException(422, f"第 {i} 行普通凭证金额不能为负数")
        if (debit == 0 and credit == 0) or (debit != 0 and credit != 0):
            raise HTTPException(422, f"第 {i} 行必须且只能填写借方或贷方金额")
        if entry.contact_id is not None and not conn.execute(
            "SELECT 1 FROM contacts WHERE account_set_id=? AND id=?",
            (account_set_id, entry.contact_id),
        ).fetchone():
            raise HTTPException(422, f"第 {i} 行往来单位不存在或不属于当前账套")
        for dimension in conn.execute('SELECT kind,required FROM fw_account_dimensions WHERE account_set_id=? AND account_code=?',(account_set_id,entry.account_code)):
            if dimension['required'] and dimension['kind'] not in entry.dimensions:
                raise HTTPException(422,'缺少必填辅助核算：'+dimension['kind'])
        for kind,ident in entry.dimensions.items():
            if not conn.execute('SELECT 1 FROM fw_dimensions WHERE account_set_id=? AND id=? AND kind=? AND is_active=1',(account_set_id,ident,kind)).fetchone():
                raise HTTPException(422,'辅助核算对象无效')
        normalized.append({
            "dimensions": entry.dimensions,
            "account_code": entry.account_code,
            "contact_id": entry.contact_id,
            "aux_name": entry.aux_name,
            "summary": entry.summary or "",
            "debit_amount": debit,
            "credit_amount": credit,
        })
        debit_total += debit
        credit_total += credit
    if debit_total != credit_total:
        raise HTTPException(422, detail={
            "message": "凭证借贷不平衡",
            "debit_total": cents_to_yuan(debit_total),
            "credit_total": cents_to_yuan(credit_total),
            "difference": cents_to_yuan(debit_total - credit_total),
        })
    if debit_total == 0:
        raise HTTPException(422, "凭证借贷金额不能同时为零")
    return normalized, debit_total, credit_total


def _insert_entries(conn: sqlite3.Connection, account_set_id: str, voucher_id: int, entries):
    conn.executemany(
        """INSERT INTO voucher_entries
           (account_set_id,voucher_id,account_code,contact_id,aux_name,summary,debit_amount,credit_amount)
           VALUES(?,?,?,?,?,?,?,?)""",
        [(
            account_set_id, voucher_id, e["account_code"], e["contact_id"],
            e.get("aux_name"), e["summary"], e["debit_amount"], e["credit_amount"],
        ) for e in entries],
    )

    inserted=conn.execute('SELECT id FROM voucher_entries WHERE account_set_id=? AND voucher_id=? ORDER BY id',(account_set_id,voucher_id)).fetchall()
    for saved,entry in zip(inserted,entries):
        for kind,dimension_id in entry.get('dimensions',{}).items():
            conn.execute('INSERT INTO fw_entry_dimensions VALUES(?,?,?,?)',(account_set_id,saved['id'],kind,dimension_id))

def _validate_postable(conn: sqlite3.Connection, account_set_id: str, voucher_id: int):
    entries = conn.execute(
        "SELECT * FROM voucher_entries WHERE account_set_id=? AND voucher_id=? ORDER BY id",
        (account_set_id, voucher_id),
    ).fetchall()
    if len(entries) < 2:
        raise HTTPException(422, "凭证分录至少需要 2 行")
    debit_total = sum(row["debit_amount"] for row in entries)
    credit_total = sum(row["credit_amount"] for row in entries)
    if debit_total <= 0 or debit_total != credit_total:
        raise HTTPException(422, "凭证借贷不平衡或金额为零，不能过账")
    for row in entries:
        if row["debit_amount"] < 0 or row["credit_amount"] < 0:
            raise HTTPException(422, "普通凭证不能包含负数分录")
        if (row["debit_amount"] == 0 and row["credit_amount"] == 0) or (
            row["debit_amount"] != 0 and row["credit_amount"] != 0
        ):
            raise HTTPException(422, "每行分录必须且只能填写借方或贷方金额")
        ensure_account(conn, row["account_code"], account_set_id)
        missing=conn.execute('''SELECT d.kind FROM fw_account_dimensions d WHERE d.account_set_id=? AND d.account_code=? AND d.required=1
          AND NOT EXISTS(SELECT 1 FROM fw_entry_dimensions e WHERE e.account_set_id=d.account_set_id AND e.entry_id=? AND e.kind=d.kind)''',(account_set_id,row['account_code'],row['id'])).fetchone()
        if missing: raise HTTPException(422,'缺少必填辅助核算：'+missing['kind'])
    from .collaboration import check_vat_posting
    check_vat_posting(conn,account_set_id,voucher_id)
    return debit_total, credit_total


@router.get("")
def list_vouchers(
    month: str | None = Query(None),
    status: str | None = None,
    account_keyword: str | None = None,
    legacy_id: str | None = None,
    limit: int = Query(1000, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    account_set_id: str = Query("default"),
    conn: sqlite3.Connection = Depends(get_db),
):
    assert_account_set(conn, account_set_id)
    where = ["v.account_set_id=?"]
    params: list[object] = [account_set_id]
    if month:
        validate_period(month)
        where.append("substr(v.voucher_date,1,7)=?")
        params.append(month)
    if status:
        allowed = {"DRAFT", "PENDING", "APPROVED", "POSTED", "VOIDED", "REVERSED"}
        if status not in allowed:
            raise HTTPException(422, "凭证状态无效")
        if status == "APPROVED":
            where.append("v.status='PENDING' AND v.reviewed_at IS NOT NULL")
        else:
            where.append("v.status=?")
            params.append(status)
    if legacy_id:
        where.append("v.legacy_id=?")
        params.append(legacy_id)
    if account_keyword:
        keyword = f"%{account_keyword.strip()}%"
        where.append("""EXISTS(
            SELECT 1 FROM voucher_entries e
            JOIN accounts a ON a.account_set_id=e.account_set_id AND a.code=e.account_code
            WHERE e.account_set_id=v.account_set_id AND e.voucher_id=v.id
              AND (e.account_code LIKE ? OR a.name LIKE ?)
        )""")
        params.extend([keyword, keyword])
    rows = conn.execute(
        "SELECT v.* FROM vouchers v WHERE " + " AND ".join(where)
        + " ORDER BY v.voucher_date DESC,v.id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return {
        "account_set_id": account_set_id,
        "items": [voucher_dict(conn, row) for row in rows],
        "limit": limit,
        "offset": offset,
    }


@router.post("", status_code=201)
def create_voucher(
    payload: VoucherCreate,
    account_set_id: str = Query("default"),
    conn: sqlite3.Connection = Depends(get_db),
):
    assert_account_set(conn, account_set_id)
    voucher_date = payload.voucher_date.isoformat()
    attachments = [validate_upload_url(url) for url in payload.attachment_urls]
    try:
        conn.execute("BEGIN IMMEDIATE")
        assert_period_unlocked(conn, voucher_date, account_set_id)
        entries, debit_total, credit_total = _validate_entry_payload(conn, payload.entries, account_set_id)
        voucher_no = payload.voucher_no or generate_voucher_no(conn, payload.voucher_date, account_set_id=account_set_id)
        cursor = conn.execute(
            """INSERT INTO vouchers
               (account_set_id,legacy_id,voucher_no,voucher_date,summary,status,attachment_urls,
                biz_label,source,source_type,counterparty,memo,attachment_name)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                account_set_id, payload.legacy_id, voucher_no, voucher_date,
                payload.summary, payload.status, json.dumps(attachments, ensure_ascii=False),
                payload.biz_label, payload.source, payload.source_type, payload.counterparty,
                payload.memo, payload.attachment_name,
            ),
        )
        voucher_id = cursor.lastrowid
        _insert_entries(conn, account_set_id, voucher_id, entries)
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise HTTPException(409, f"凭证保存失败：{exc}") from exc
    row = conn.execute(
        "SELECT * FROM vouchers WHERE account_set_id=? AND id=?", (account_set_id, voucher_id)
    ).fetchone()
    result = voucher_dict(conn, row)
    result["debit_total"] = cents_to_yuan(debit_total)
    result["credit_total"] = cents_to_yuan(credit_total)
    return result


@router.put("/{voucher_id}")
def update_voucher(
    voucher_id: int,
    payload: VoucherUpdate,
    account_set_id: str = Query("default"),
    conn: sqlite3.Connection = Depends(get_db),
):
    assert_account_set(conn, account_set_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(422, "没有提供需要修改的字段")
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT * FROM vouchers WHERE account_set_id=? AND id=?", (account_set_id, voucher_id)
        ).fetchone()
        if not current:
            raise HTTPException(404, "凭证不存在")
        if current["status"] not in ("DRAFT", "PENDING") or current["reviewed_at"]:
            raise HTTPException(409, "只有未审核的草稿或待审核凭证可以修改")
        new_date = changes.get("voucher_date", current["voucher_date"])
        new_date_text = new_date.isoformat() if isinstance(new_date, date) else str(new_date)
        assert_period_unlocked(conn, new_date_text, account_set_id)
        attachments = changes.pop("attachment_urls", None)
        entries_payload = changes.pop("entries", None)
        allowed = {
            "voucher_no", "voucher_date", "summary", "biz_label", "source", "source_type",
            "counterparty", "memo", "attachment_name",
        }
        values = {key: value for key, value in changes.items() if key in allowed}
        for required_field in ("voucher_no", "voucher_date", "summary"):
            if required_field in values and values[required_field] is None:
                raise HTTPException(422, f"{required_field} 不能设置为空")
        if "voucher_date" in values and isinstance(values["voucher_date"], date):
            values["voucher_date"] = values["voucher_date"].isoformat()
        if attachments is not None:
            values["attachment_urls"] = json.dumps(
                [validate_upload_url(url) for url in attachments], ensure_ascii=False
            )
        if values:
            clause = ",".join(f"{key}=?" for key in values)
            conn.execute(
                f"UPDATE vouchers SET {clause} WHERE account_set_id=? AND id=?",
                (*values.values(), account_set_id, voucher_id),
            )
        if entries_payload is not None:
            entry_models = [entry if isinstance(entry, VoucherEntryIn) else VoucherEntryIn.model_validate(entry) for entry in entries_payload]
            entries, _, _ = _validate_entry_payload(conn, entry_models, account_set_id)
            conn.execute(
                "DELETE FROM voucher_entries WHERE account_set_id=? AND voucher_id=?",
                (account_set_id, voucher_id),
            )
            _insert_entries(conn, account_set_id, voucher_id, entries)
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise HTTPException(409, f"凭证修改失败：{exc}") from exc
    row = conn.execute(
        "SELECT * FROM vouchers WHERE account_set_id=? AND id=?", (account_set_id, voucher_id)
    ).fetchone()
    return voucher_dict(conn, row)


@router.post("/{voucher_id}/approve")
def approve_voucher(
    voucher_id: int,
    account_set_id: str = Query("default"),
    conn: sqlite3.Connection = Depends(get_db),
):
    assert_account_set(conn, account_set_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM vouchers WHERE account_set_id=? AND id=?", (account_set_id, voucher_id)
        ).fetchone()
        if not row:
            raise HTTPException(404, "凭证不存在")
        if row["status"] not in ("DRAFT", "PENDING") or row["reviewed_at"]:
            raise HTTPException(409, "当前凭证状态不允许审核，或已经审核")
        assert_period_unlocked(conn, row["voucher_date"], account_set_id)
        _validate_postable(conn, account_set_id, voucher_id)
        conn.execute(
            "UPDATE vouchers SET status='PENDING',reviewed_at=? WHERE account_set_id=? AND id=?",
            (now_iso(), account_set_id, voucher_id),
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    row = conn.execute(
        "SELECT * FROM vouchers WHERE account_set_id=? AND id=?", (account_set_id, voucher_id)
    ).fetchone()
    return voucher_dict(conn, row)


@router.post("/batch-approve")
def batch_approve_vouchers(
    payload: VoucherIdsRequest,
    account_set_id: str = Query("default"),
    conn: sqlite3.Connection = Depends(get_db),
):
    assert_account_set(conn, account_set_id)
    ids = payload.ids
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "批量凭证 ID 不能重复")
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = []
        for voucher_id in ids:
            row = conn.execute(
                "SELECT * FROM vouchers WHERE account_set_id=? AND id=?", (account_set_id, voucher_id)
            ).fetchone()
            if not row:
                raise HTTPException(404, f"凭证不存在：{voucher_id}")
            if row["status"] not in ("DRAFT", "PENDING") or row["reviewed_at"]:
                raise HTTPException(409, f"凭证 {row['voucher_no']} 已审核或状态不允许审核")
            assert_period_unlocked(conn, row["voucher_date"], account_set_id)
            _validate_postable(conn, account_set_id, voucher_id)
            rows.append(row)
        reviewed_at = now_iso()
        conn.executemany(
            "UPDATE vouchers SET status='PENDING',reviewed_at=? WHERE account_set_id=? AND id=?",
            [(reviewed_at, account_set_id, voucher_id) for voucher_id in ids],
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    return {"account_set_id": account_set_id, "count": len(ids), "items": [
        voucher_dict(conn, conn.execute("SELECT * FROM vouchers WHERE account_set_id=? AND id=?", (account_set_id, voucher_id)).fetchone())
        for voucher_id in ids
    ]}


@router.post("/batch-post")
def batch_post_vouchers(
    payload: VoucherIdsRequest,
    account_set_id: str = Query("default"),
    conn: sqlite3.Connection = Depends(get_db),
):
    assert_account_set(conn, account_set_id)
    ids = payload.ids
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "批量凭证 ID 不能重复")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for voucher_id in ids:
            row = conn.execute(
                "SELECT * FROM vouchers WHERE account_set_id=? AND id=?", (account_set_id, voucher_id)
            ).fetchone()
            if not row:
                raise HTTPException(404, f"凭证不存在：{voucher_id}")
            if row["status"] != "PENDING" or not row["reviewed_at"]:
                raise HTTPException(409, f"凭证 {row['voucher_no']} 尚未审核或状态不允许过账")
            assert_period_unlocked(conn, row["voucher_date"], account_set_id)
            _validate_postable(conn, account_set_id, voucher_id)
        posted_at = now_iso()
        conn.executemany(
            "UPDATE vouchers SET status='POSTED',posted_at=? WHERE account_set_id=? AND id=?",
            [(posted_at, account_set_id, voucher_id) for voucher_id in ids],
        )
        conn.executemany(
            "UPDATE cashier_records SET status='POSTED' WHERE account_set_id=? AND linked_voucher_id=?",
            [(account_set_id, voucher_id) for voucher_id in ids],
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    return {"account_set_id": account_set_id, "count": len(ids), "items": [
        voucher_dict(conn, conn.execute("SELECT * FROM vouchers WHERE account_set_id=? AND id=?", (account_set_id, voucher_id)).fetchone())
        for voucher_id in ids
    ]}


@router.post("/{voucher_id}/post")
def post_voucher(
    voucher_id: int,
    account_set_id: str = Query("default"),
    conn: sqlite3.Connection = Depends(get_db),
):
    assert_account_set(conn, account_set_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM vouchers WHERE account_set_id=? AND id=?", (account_set_id, voucher_id)
        ).fetchone()
        if not row:
            raise HTTPException(404, "凭证不存在")
        if row["status"] not in ("DRAFT", "PENDING"):
            raise HTTPException(409, f"当前凭证状态 {row['status']} 不允许过账")
        if not row["reviewed_at"]:
            raise HTTPException(409, "凭证尚未审核，不能过账")
        assert_period_unlocked(conn, row["voucher_date"], account_set_id)
        _validate_postable(conn, account_set_id, voucher_id)
        conn.execute(
            "UPDATE vouchers SET status='POSTED',posted_at=? WHERE account_set_id=? AND id=?",
            (now_iso(), account_set_id, voucher_id),
        )
        conn.execute(
            "UPDATE cashier_records SET status='POSTED' WHERE account_set_id=? AND linked_voucher_id=?",
            (account_set_id, voucher_id),
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    row = conn.execute(
        "SELECT * FROM vouchers WHERE account_set_id=? AND id=?", (account_set_id, voucher_id)
    ).fetchone()
    return voucher_dict(conn, row)


@router.delete("/{voucher_id}")
def delete_voucher(
    voucher_id: int,
    account_set_id: str = Query("default"),
    conn: sqlite3.Connection = Depends(get_db),
):
    assert_account_set(conn, account_set_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM vouchers WHERE account_set_id=? AND id=?", (account_set_id, voucher_id)
        ).fetchone()
        if not row:
            raise HTTPException(404, "凭证不存在")
        if row["status"] not in ("DRAFT", "PENDING") or row["reviewed_at"]:
            raise HTTPException(409, "已审核或已记账凭证不能删除；请按财务制度执行红字冲销")
        linked = conn.execute(
            "SELECT COUNT(*) AS n FROM cashier_records WHERE account_set_id=? AND linked_voucher_id=?",
            (account_set_id, voucher_id),
        ).fetchone()["n"]
        if linked:
            raise HTTPException(409, "凭证已被出纳记录关联，不能删除")
        # Remove pending business-source links first; posted/approved vouchers are blocked above.
        conn.execute("DELETE FROM business_voucher_links WHERE account_set_id=? AND voucher_id=?",(account_set_id,voucher_id))
        conn.execute("UPDATE depreciation_runs SET voucher_id=NULL,voucher_legacy_id=NULL WHERE account_set_id=? AND voucher_id=?",(account_set_id,voucher_id))
        conn.execute(
            "DELETE FROM vouchers WHERE account_set_id=? AND id=?", (account_set_id, voucher_id)
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise HTTPException(409, f"凭证删除失败：{exc}") from exc
    return {"status": "deleted", "id": voucher_id, "account_set_id": account_set_id}


@router.post("/{voucher_id}/reverse", status_code=201)
def reverse_voucher(
    voucher_id: int,
    payload: ReverseVoucherRequest | None = None,
    account_set_id: str = Query("default"),
    conn: sqlite3.Connection = Depends(get_db),
):
    assert_account_set(conn, account_set_id)
    payload = payload or ReverseVoucherRequest()
    reverse_date = payload.voucher_date or date.today()
    reverse_date_text = reverse_date.isoformat()
    try:
        conn.execute("BEGIN IMMEDIATE")
        original = conn.execute(
            "SELECT * FROM vouchers WHERE account_set_id=? AND id=?", (account_set_id, voucher_id)
        ).fetchone()
        if not original:
            raise HTTPException(404, "原凭证不存在")
        if original["status"] != "POSTED":
            raise HTTPException(409, "只有已过账且尚未冲销的凭证可以冲销")
        if original["reversed_by_id"] is not None:
            raise HTTPException(409, "该凭证已经存在冲销凭证")
        from .returns_core import active_returns
        legacy_id=original['legacy_id'] or ''
        if legacy_id.startswith(('fw-return:','fw-refund:','fw-fx:')):
            raise HTTPException(409,'该凭证关联退货、退款或调汇底稿，不能单独冲销')
        source=conn.execute('SELECT legacy_id FROM business_documents WHERE account_set_id=? AND voucher_legacy_id=?',(account_set_id,legacy_id)).fetchone()
        if source and any(row['sourceDocId']==source['legacy_id'] for row,_ in active_returns(conn,account_set_id)):
            raise HTTPException(409,'该原单已有退货记录，不能单独冲销原凭证')
        assert_period_unlocked(conn, reverse_date_text, account_set_id)
        entries = conn.execute(
            "SELECT * FROM voucher_entries WHERE account_set_id=? AND voucher_id=? ORDER BY id",
            (account_set_id, voucher_id),
        ).fetchall()
        if len(entries) < 2 or sum(e["debit_amount"] for e in entries) != sum(e["credit_amount"] for e in entries):
            raise HTTPException(422, "原凭证分录不足或借贷不平衡，不能冲销")
        voucher_no = payload.voucher_no or generate_voucher_no(
            conn, reverse_date, prefix="红字", account_set_id=account_set_id
        )
        summary = payload.summary or f"红字冲销：{original['summary'] or original['voucher_no']}"
        cursor = conn.execute(
            """INSERT INTO vouchers
               (account_set_id,voucher_no,voucher_date,summary,status,attachment_urls,posted_at,
                reverse_of_id,biz_label,source,source_type,counterparty,memo,attachment_name)
               VALUES(?,?,?,?, 'POSTED',?,?,?,?,?,?,?,?,?)""",
            (
                account_set_id, voucher_no, reverse_date_text, summary, original["attachment_urls"],
                now_iso(), voucher_id, original["biz_label"], original["source"], original["source_type"],
                original["counterparty"], f"红字冲销：{original['memo'] or original['summary']}",
                original["attachment_name"],
            ),
        )
        reverse_id = cursor.lastrowid
        conn.executemany(
            """INSERT INTO voucher_entries
               (account_set_id,voucher_id,account_code,contact_id,aux_name,summary,debit_amount,credit_amount)
               VALUES(?,?,?,?,?,?,?,?)""",
            [(
                account_set_id, reverse_id, e["account_code"], e["contact_id"], e["aux_name"],
                f"红字冲销：{e['summary'] or original['summary']}",
                -e["debit_amount"], -e["credit_amount"],
            ) for e in entries],
        )
        conn.execute(
            "UPDATE vouchers SET status='REVERSED',reversed_by_id=?,reversed_at=? WHERE account_set_id=? AND id=?",
            (reverse_id, now_iso(), account_set_id, voucher_id),
        )
        conn.commit()
    except HTTPException:
        conn.rollback()
        raise
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise HTTPException(409, f"冲销凭证生成失败：{exc}") from exc
    except Exception:
        conn.rollback()
        raise
    row = conn.execute(
        "SELECT * FROM vouchers WHERE account_set_id=? AND id=?", (account_set_id, reverse_id)
    ).fetchone()
    result = voucher_dict(conn, row)
    result["reversed_voucher_id"] = voucher_id
    return result
