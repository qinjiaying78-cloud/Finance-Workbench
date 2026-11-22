from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException


def money_to_cents(value: Decimal | str | int) -> int:
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HTTPException(status_code=422, detail="金额格式无效")
    if not amount.is_finite() or amount.as_tuple().exponent < -2:
        raise HTTPException(status_code=422, detail="金额必须为有效数字且最多保留两位小数")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise HTTPException(status_code=422, detail="金额最多保留两位小数")
    if abs(cents)>9_000_000_000_000_000:
        raise HTTPException(status_code=422,detail='金额超出可精确保存范围')
    return int(cents)


def cents_to_yuan(value: int) -> str:
    return f"{Decimal(int(value)) / Decimal(100):.2f}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_period(period: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}", period):
        raise HTTPException(status_code=422, detail="会计期间格式必须为 YYYY-MM")
    try:
        date.fromisoformat(period + "-01")
    except ValueError:
        raise HTTPException(status_code=422, detail="会计期间无效")
    return period


def period_of(value: str | date) -> str:
    return (value if isinstance(value, date) else date.fromisoformat(value)).strftime("%Y-%m")


def assert_period_unlocked(conn: sqlite3.Connection, voucher_date: str | date, account_set_id: str = "default") -> None:
    period = period_of(voucher_date)
    row = conn.execute(
        "SELECT is_locked FROM accounting_periods WHERE account_set_id=? AND period = ?", (account_set_id, period)
    ).fetchone()
    official = conn.execute("SELECT 1 FROM app_records WHERE account_set_id=? AND store_name='Workpapers' AND record_id=? AND json_type(payload_json,'$.archive')='object'", (account_set_id, f"closed:{period}")).fetchone()
    if (row and row["is_locked"]) or official:
        raise HTTPException(status_code=409, detail=f"会计期间 {period} 已锁定，不能新增或过账凭证")


def validate_upload_url(url: str | None) -> str | None:
    if url is None or url == "":
        return None
    if not url.startswith("/uploads/") or ".." in url.split("/"):
        raise HTTPException(status_code=422, detail="附件路径必须是本地 /uploads/ 相对路径")
    return url


def generate_voucher_no(conn: sqlite3.Connection, voucher_date: str | date, prefix: str = "记", account_set_id: str = "default") -> str:
    day = voucher_date.isoformat() if isinstance(voucher_date, date) else voucher_date
    stem = f"{prefix}-{day.replace('-', '')}"
    last = conn.execute(
        "SELECT voucher_no FROM vouchers WHERE account_set_id=? AND voucher_no LIKE ? ORDER BY id DESC LIMIT 1",
        (account_set_id, stem + "-%",),
    ).fetchone()
    seq = 1
    if last:
        try:
            seq = int(str(last["voucher_no"]).rsplit("-", 1)[1]) + 1
        except (ValueError, IndexError):
            seq = conn.execute("SELECT COUNT(*) + 1 AS n FROM vouchers WHERE account_set_id=? AND voucher_no LIKE ?", (account_set_id, stem + "-%",)).fetchone()["n"]
    return f"{stem}-{seq:04d}"


def parse_json_array(raw: str | None) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
        return value if isinstance(value, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def voucher_dict(conn: sqlite3.Connection, row: sqlite3.Row, include_entries: bool = True) -> dict[str, Any]:
    item = dict(row)
    item["attachment_urls"] = parse_json_array(item.get("attachment_urls"))
    if include_entries:
        entries = conn.execute(
            """SELECT e.id,e.account_code,a.name AS account_name,e.contact_id,
                      c.name AS contact_name,e.aux_name,e.summary,e.debit_amount,e.credit_amount
               FROM voucher_entries e
               JOIN accounts a ON a.account_set_id=e.account_set_id AND a.code=e.account_code
               LEFT JOIN contacts c ON c.account_set_id=e.account_set_id AND c.id=e.contact_id
               WHERE e.account_set_id=? AND e.voucher_id=? ORDER BY e.id""",
            (item["account_set_id"], item["id"],)
        ).fetchall()
        item["entries"] = [
            {
                **dict(entry),
                "debit_amount": cents_to_yuan(entry["debit_amount"]),
                "credit_amount": cents_to_yuan(entry["credit_amount"]),
                "aux_name": entry["aux_name"],
                "dimensions": {d['kind']:d['dimension_id'] for d in conn.execute('SELECT kind,dimension_id FROM fw_entry_dimensions WHERE account_set_id=? AND entry_id=?',(item['account_set_id'],entry['id']))},
            }
            for entry in entries
        ]
    return item


def account_is_active(conn: sqlite3.Connection, code: str, account_set_id: str = "default") -> bool:
    row = conn.execute("SELECT is_active FROM accounts WHERE account_set_id=? AND code = ?", (account_set_id, code)).fetchone()
    return bool(row and row["is_active"])


def ensure_account(conn: sqlite3.Connection, code: str, account_set_id: str = "default") -> None:
    row = conn.execute("SELECT code, is_active FROM accounts WHERE account_set_id=? AND code = ?", (account_set_id, code)).fetchone()
    if not row:
        raise HTTPException(status_code=422, detail=f"会计科目 {code} 不存在")
    if not row["is_active"]:
        raise HTTPException(status_code=422, detail=f"会计科目 {code} 已停用")


def assert_account_set(conn: sqlite3.Connection, account_set_id: str) -> str:
    if not account_set_id or len(account_set_id) > 120:
        raise HTTPException(status_code=422, detail="账套 ID 无效")
    row = conn.execute("SELECT 1 FROM account_sets WHERE id=?", (account_set_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"账套不存在：{account_set_id}")
    return account_set_id
