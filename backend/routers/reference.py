from __future__ import annotations
import json
import sqlite3
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from ..database import get_connection
from ..schemas import AccountCreate, AccountOpeningBalanceBatch, AccountUpdate, ContactCreate, ContactUpdate, ContactOpeningBalanceUpdate, PeriodLockUpdate
from .common import assert_account_set, cents_to_yuan, money_to_cents, validate_period

router = APIRouter(tags=["基础资料与期间控制"])

def get_db():
    conn = get_connection()
    try: yield conn
    finally: conn.close()

@router.get("/api/accounts")
def list_accounts(active_only: bool = True, account_set_id: str = Query("default"), conn: sqlite3.Connection = Depends(get_db)):
    assert_account_set(conn, account_set_id)
    sql = "SELECT * FROM accounts WHERE account_set_id=?"
    params: list[object] = [account_set_id]
    if active_only: sql += " AND is_active=1"
    sql += " ORDER BY code"
    rows = conn.execute(sql, params).fetchall()
    return {"account_set_id": account_set_id, "items": [{**dict(r), "initial_debit": cents_to_yuan(r["initial_debit"]), "initial_credit": cents_to_yuan(r["initial_credit"]), "is_active": bool(r["is_active"])} for r in rows]}

@router.post("/api/accounts", status_code=201)
def create_account(payload: AccountCreate, account_set_id: str = Query("default"), conn: sqlite3.Connection = Depends(get_db)):
    assert_account_set(conn, account_set_id)
    from .collaboration import validate_account
    validate_account(conn,account_set_id,payload.model_dump())
    debit, credit = money_to_cents(payload.initial_debit), money_to_cents(payload.initial_credit)
    if debit and credit: raise HTTPException(422, "科目期初余额只能填写借方或贷方其中一方")
    if payload.parent_code and not conn.execute("SELECT 1 FROM accounts WHERE account_set_id=? AND code=?", (account_set_id,payload.parent_code)).fetchone():
        raise HTTPException(422, "上级科目不存在")
    try:
        cur=conn.execute("INSERT INTO accounts(account_set_id,code,name,category,direction,parent_code,initial_debit,initial_credit,is_active) VALUES(?,?,?,?,?,?,?,?,?)", (account_set_id,payload.code,payload.name,payload.category,payload.direction,payload.parent_code,debit,credit,int(payload.is_active)))
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback(); raise HTTPException(409, f"科目保存失败：{exc}") from exc
    row=conn.execute("SELECT * FROM accounts WHERE account_set_id=? AND id=?",(account_set_id,cur.lastrowid)).fetchone()
    return {**dict(row),"initial_debit":cents_to_yuan(row["initial_debit"]),"initial_credit":cents_to_yuan(row["initial_credit"]),"is_active":bool(row["is_active"])}

@router.get("/api/contacts")
def list_contacts(type: str|None=Query(None,pattern="^(CUSTOMER|SUPPLIER)$"), keyword: str|None=None, account_set_id: str=Query("default"), conn: sqlite3.Connection=Depends(get_db)):
    assert_account_set(conn,account_set_id)
    where=["account_set_id=?"]; params: list[object]=[account_set_id]
    if type: where.append("type=?"); params.append(type)
    if keyword: where.append("(name LIKE ? OR tax_id LIKE ?)"); params.extend([f"%{keyword}%",f"%{keyword}%"])
    rows=conn.execute("SELECT * FROM contacts WHERE "+" AND ".join(where)+" ORDER BY name,id",params).fetchall()
    return {"account_set_id":account_set_id,"items":[dict(r) for r in rows]}

@router.post("/api/contacts",status_code=201)
def create_contact(payload: ContactCreate, account_set_id: str=Query("default"), conn: sqlite3.Connection=Depends(get_db)):
    assert_account_set(conn,account_set_id)
    cur=conn.execute("INSERT INTO contacts(account_set_id,name,type,tax_id,contact_info) VALUES(?,?,?,?,?)",(account_set_id,payload.name,payload.type,payload.tax_id,payload.contact_info)); conn.commit()
    return dict(conn.execute("SELECT * FROM contacts WHERE account_set_id=? AND id=?",(account_set_id,cur.lastrowid)).fetchone())

@router.get("/api/periods")
def list_periods(account_set_id: str=Query("default"), conn: sqlite3.Connection=Depends(get_db)):
    assert_account_set(conn,account_set_id)
    rows=conn.execute("SELECT * FROM accounting_periods WHERE account_set_id=? ORDER BY period DESC",(account_set_id,)).fetchall()
    return {"account_set_id":account_set_id,"items":[{**dict(r),"is_locked":bool(r["is_locked"])} for r in rows]}

@router.put("/api/periods/{period}")
def update_period_lock(period: str,payload: PeriodLockUpdate,account_set_id: str=Query("default"),conn: sqlite3.Connection=Depends(get_db)):
    assert_account_set(conn,account_set_id); validate_period(period)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if payload.is_locked:
            pending=conn.execute("SELECT COUNT(*) n FROM vouchers WHERE account_set_id=? AND substr(voucher_date,1,7)=? AND status IN ('DRAFT','PENDING')",(account_set_id,period)).fetchone()["n"]
            cash=conn.execute("SELECT COUNT(*) n FROM cashier_records WHERE account_set_id=? AND substr(record_date,1,7)=? AND status IN ('PENDING','DONE')",(account_set_id,period)).fetchone()["n"]
            totals=conn.execute("SELECT COALESCE(SUM(e.debit_amount),0) d,COALESCE(SUM(e.credit_amount),0) c FROM vouchers v JOIN voucher_entries e ON e.account_set_id=v.account_set_id AND e.voucher_id=v.id WHERE v.account_set_id=? AND substr(v.voucher_date,1,7)=? AND v.status IN ('POSTED','REVERSED')",(account_set_id,period)).fetchone()
            bank_open=conn.execute("SELECT COUNT(*) n FROM v11_bank_matches WHERE account_set_id=? AND period=? AND status<>'MATCH' AND (resolution IS NULL OR trim(COALESCE(evidence,''))='')",(account_set_id,period)).fetchone()["n"] if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='v11_bank_matches'").fetchone() else 0
            if pending or cash or totals["d"]!=totals["c"] or bank_open:
                raise HTTPException(409,f"期末结账检查未通过：待审核/草稿凭证 {pending} 张，未过账出纳记录 {cash} 笔，试算借贷差额 {totals['d']-totals['c']} 分，银行未处理差异 {bank_open} 条")
            locked_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")
        else:
            official = conn.execute("SELECT 1 FROM app_records WHERE account_set_id=? AND store_name='Workpapers' AND record_id=? AND json_type(payload_json,'$.archive')='object'", (account_set_id,f"closed:{period}")).fetchone()
            if official:
                raise HTTPException(409,"正式结账期间不能通过手动锁接口解锁")
            locked_at=None
        conn.execute("""INSERT INTO accounting_periods(account_set_id,period,is_locked,locked_at,lock_reason,updated_at) VALUES(?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now')) ON CONFLICT(account_set_id,period) DO UPDATE SET is_locked=excluded.is_locked,locked_at=excluded.locked_at,lock_reason=excluded.lock_reason,updated_at=excluded.updated_at""",(account_set_id,period,int(payload.is_locked),locked_at,payload.lock_reason)); conn.commit()
    except HTTPException: conn.rollback(); raise
    except Exception: conn.rollback(); raise
    row=conn.execute("SELECT * FROM accounting_periods WHERE account_set_id=? AND period=?",(account_set_id,period)).fetchone()
    return {**dict(row),"is_locked":bool(row["is_locked"])}

@router.post("/api/periods/{period}/close", status_code=201)
def close_period_with_snapshot(period: str, snapshot: dict, account_set_id: str = Query("default"), conn: sqlite3.Connection = Depends(get_db)):
    """Commit the period lock and the browser's approved snapshot together."""
    assert_account_set(conn, account_set_id)
    validate_period(period)
    archive = snapshot.get("archive")
    if snapshot.get("id") != f"closed:{period}" or snapshot.get("period") != period or not isinstance(archive, dict):
        raise HTTPException(422, "结账快照与会计期间不一致")
    vouchers = archive.get("vouchers")
    if not isinstance(vouchers, list) or any(not isinstance(row, dict) for row in vouchers):
        raise HTTPException(422, "结账快照缺少凭证清单")
    reports = archive.get("reports")
    checks = archive.get("checks")
    if not isinstance(reports, dict) or any(not isinstance(reports.get(key), dict) for key in ("balanceSheet", "profitAndLoss", "cashFlow")):
        raise HTTPException(422, "结账快照缺少三张报表")
    if not isinstance(checks, dict) or not checks or not all(value is True for value in checks.values()):
        raise HTTPException(422, "结账快照缺少通过的检查或三张报表")
    reason = "正式结账"
    try:
        conn.execute("BEGIN IMMEDIATE")
        locked = conn.execute("SELECT is_locked FROM accounting_periods WHERE account_set_id=? AND period=?", (account_set_id, period)).fetchone()
        marker = conn.execute("SELECT 1 FROM app_records WHERE account_set_id=? AND store_name='Workpapers' AND record_id=?", (account_set_id, snapshot["id"])).fetchone()
        if (locked and locked["is_locked"]) or marker:
            raise HTTPException(409, "该期间已锁定或已有结账快照")
        current = conn.execute("SELECT id,legacy_id,status,posted_at,reversed_by_id FROM vouchers WHERE account_set_id=? AND substr(voucher_date,1,7)=? AND status<>'VOIDED'", (account_set_id, period)).fetchall()
        identities = {(row["legacy_id"] or f'db-voucher-{row["id"]}', row["posted_at"], row["reversed_by_id"]) for row in current}
        expected = {(row.get("id"), row.get("postedAt"), row.get("reversedById")) for row in vouchers}
        if any(row["status"] not in ("POSTED", "REVERSED") for row in current) or identities != expected or len(current) != len(vouchers):
            raise HTTPException(409, "期间凭证已变化或仍有未记账凭证，请刷新复核")
        cash = conn.execute("SELECT 1 FROM cashier_records WHERE account_set_id=? AND substr(record_date,1,7)=? AND status<>'POSTED' LIMIT 1", (account_set_id, period)).fetchone()
        bank = conn.execute("SELECT 1 FROM v11_bank_matches WHERE account_set_id=? AND period=? AND status<>'MATCH' AND (resolution IS NULL OR trim(COALESCE(evidence,''))='') LIMIT 1", (account_set_id, period)).fetchone()
        if cash or bank:
            raise HTTPException(409, "期间还有未处理的出纳或银行对账记录")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        conn.execute("""INSERT INTO accounting_periods(account_set_id,period,is_locked,locked_at,lock_reason,updated_at)
          VALUES(?,?,1,?,?,?) ON CONFLICT(account_set_id,period) DO UPDATE SET
          is_locked=1,locked_at=excluded.locked_at,lock_reason=excluded.lock_reason,updated_at=excluded.updated_at""", (account_set_id, period, now, reason, now))
        conn.execute("INSERT INTO app_records(account_set_id,store_name,record_id,payload_json) VALUES(?,'Workpapers',?,?)", (account_set_id, snapshot["id"], json.dumps(snapshot, ensure_ascii=False)))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"period": period, "is_locked": True, "snapshot_id": snapshot["id"]}


@router.put("/api/contact-opening-balances")
def upsert_contact_opening_balance(payload: ContactOpeningBalanceUpdate,account_set_id: str=Query("default"),conn: sqlite3.Connection=Depends(get_db)):
    assert_account_set(conn,account_set_id)
    if not conn.execute("SELECT 1 FROM accounts WHERE account_set_id=? AND code=?",(account_set_id,payload.account_code)).fetchone(): raise HTTPException(404,"会计科目不存在")
    if not conn.execute("SELECT 1 FROM contacts WHERE account_set_id=? AND id=?",(account_set_id,payload.contact_id)).fetchone(): raise HTTPException(404,"往来单位不存在")
    debit,credit=money_to_cents(payload.initial_debit),money_to_cents(payload.initial_credit)
    if debit and credit: raise HTTPException(422,"往来期初余额只能填写借方或贷方其中一方")
    conn.execute("""INSERT INTO contact_opening_balances(account_set_id,account_code,contact_id,as_of_date,initial_debit,initial_credit) VALUES(?,?,?,?,?,?) ON CONFLICT(account_set_id,account_code,contact_id) DO UPDATE SET as_of_date=excluded.as_of_date,initial_debit=excluded.initial_debit,initial_credit=excluded.initial_credit""",(account_set_id,payload.account_code,payload.contact_id,payload.as_of_date.isoformat(),debit,credit)); conn.commit()
    return {"account_set_id":account_set_id,"account_code":payload.account_code,"contact_id":payload.contact_id,"as_of_date":payload.as_of_date.isoformat(),"initial_debit":cents_to_yuan(debit),"initial_credit":cents_to_yuan(credit)}

@router.put("/api/accounts/opening-balances")
def update_account_opening_balances(payload: AccountOpeningBalanceBatch, account_set_id: str = Query("default"), conn: sqlite3.Connection = Depends(get_db)):
    """Atomically update all chart-of-account opening balances for one book."""
    assert_account_set(conn, account_set_id)
    codes=[item.code for item in payload.items]
    if len(codes)!=len(set(codes)): raise HTTPException(422,"期初余额批次中科目编码不能重复")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for item in payload.items:
            row=conn.execute("SELECT 1 FROM accounts WHERE account_set_id=? AND code=?",(account_set_id,item.code)).fetchone()
            if not row: raise HTTPException(404,f"科目不存在：{item.code}")
            debit,credit=money_to_cents(item.initial_debit),money_to_cents(item.initial_credit)
            if debit and credit: raise HTTPException(422,f"科目 {item.code} 期初余额只能填写借方或贷方其中一方")
            conn.execute("UPDATE accounts SET initial_debit=?,initial_credit=? WHERE account_set_id=? AND code=?",(debit,credit,account_set_id,item.code))
        total = conn.execute("SELECT COALESCE(SUM(initial_debit-initial_credit),0) FROM accounts WHERE account_set_id=?", (account_set_id,)).fetchone()[0]
        if total:
            raise HTTPException(422, f"整套科目期初余额借贷不平，差额 {total} 分")
        conn.commit()
    except HTTPException: conn.rollback(); raise
    except Exception: conn.rollback(); raise
    return {"account_set_id":account_set_id,"updated":len(payload.items)}



@router.put("/api/accounts/{code}")
def update_account(code: str, payload: AccountUpdate, account_set_id: str = Query("default"), conn: sqlite3.Connection = Depends(get_db)):
    """Update account metadata and its opening balance without changing its code."""
    assert_account_set(conn, account_set_id)
    current = conn.execute("SELECT * FROM accounts WHERE account_set_id=? AND code=?", (account_set_id, code)).fetchone()
    if not current:
        raise HTTPException(404, "会计科目不存在")
    updates = payload.model_dump(exclude_unset=True)
    if "parent_code" in updates and updates["parent_code"] != current["parent_code"]: raise HTTPException(422,"已有科目的层级不可移动")
    if not updates:
        raise HTTPException(422, "没有提供需要更新的字段")
    if "parent_code" in updates and updates["parent_code"]:
        if updates["parent_code"] == code or not conn.execute("SELECT 1 FROM accounts WHERE account_set_id=? AND code=?", (account_set_id, updates["parent_code"])).fetchone():
            raise HTTPException(422, "上级科目不存在或不能设置为自身")
    if "initial_debit" in updates or "initial_credit" in updates:
        debit = money_to_cents(updates.get("initial_debit", cents_to_yuan(current["initial_debit"])))
        credit = money_to_cents(updates.get("initial_credit", cents_to_yuan(current["initial_credit"])))
        if debit and credit:
            raise HTTPException(422, "科目期初余额只能填写借方或贷方其中一方")
        updates["initial_debit"], updates["initial_credit"] = debit, credit
    if "is_active" in updates: updates["is_active"] = int(updates["is_active"])
    assignments = ",".join(f"{k}=?" for k in updates)
    try:
        conn.execute(f"UPDATE accounts SET {assignments} WHERE account_set_id=? AND code=?", (*updates.values(), account_set_id, code))
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback(); raise HTTPException(409, f"科目更新失败：{exc}") from exc
    row = conn.execute("SELECT * FROM accounts WHERE account_set_id=? AND code=?", (account_set_id, code)).fetchone()
    return {**dict(row), "initial_debit": cents_to_yuan(row["initial_debit"]), "initial_credit": cents_to_yuan(row["initial_credit"]), "is_active": bool(row["is_active"])}


@router.put("/api/contacts/{contact_id}")
def update_contact(contact_id: int, payload: ContactUpdate, account_set_id: str = Query("default"), conn: sqlite3.Connection = Depends(get_db)):
    assert_account_set(conn, account_set_id)
    current = conn.execute("SELECT * FROM contacts WHERE account_set_id=? AND id=?", (account_set_id, contact_id)).fetchone()
    if not current: raise HTTPException(404, "往来单位不存在")
    updates = payload.model_dump(exclude_unset=True)
    if not updates: raise HTTPException(422, "没有提供需要更新的字段")
    assignments = ",".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE contacts SET {assignments} WHERE account_set_id=? AND id=?", (*updates.values(), account_set_id, contact_id)); conn.commit()
    return dict(conn.execute("SELECT * FROM contacts WHERE account_set_id=? AND id=?", (account_set_id, contact_id)).fetchone())


@router.delete("/api/contacts/{contact_id}")
def delete_contact(contact_id: int, account_set_id: str = Query("default"), conn: sqlite3.Connection = Depends(get_db)):
    assert_account_set(conn, account_set_id)
    try:
        cur = conn.execute("DELETE FROM contacts WHERE account_set_id=? AND id=?", (account_set_id, contact_id))
        if cur.rowcount == 0: raise HTTPException(404, "往来单位不存在")
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback(); raise HTTPException(409, "该往来单位已被凭证或期初余额引用，不能删除") from exc
    return {"deleted": True, "id": contact_id}


@router.get("/api/contact-opening-balances")
def list_contact_opening_balances(account_set_id: str = Query("default"), conn: sqlite3.Connection = Depends(get_db)):
    assert_account_set(conn, account_set_id)
    rows = conn.execute("""SELECT o.account_code,o.contact_id,c.name AS contact_name,o.as_of_date,o.initial_debit,o.initial_credit
        FROM contact_opening_balances o JOIN contacts c ON c.account_set_id=o.account_set_id AND c.id=o.contact_id
        WHERE o.account_set_id=? ORDER BY o.account_code,c.name""", (account_set_id,)).fetchall()
    return {"account_set_id": account_set_id, "items": [{**dict(r), "initial_debit": cents_to_yuan(r["initial_debit"]), "initial_credit": cents_to_yuan(r["initial_credit"])} for r in rows]}
