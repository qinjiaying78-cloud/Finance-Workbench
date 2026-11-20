from __future__ import annotations

import sqlite3
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from ..database import get_connection
from .common import assert_account_set, cents_to_yuan, validate_period

router = APIRouter(prefix="/api/reports", tags=["账表查询"])
POSTED_STATES = ("POSTED", "REVERSED")


def get_db():
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def _period_bounds(period: str) -> tuple[str, str]:
    period = validate_period(period)
    year, month = map(int, period.split("-"))
    if month == 12:
        next_start = f"{year + 1:04d}-01-01"
    else:
        next_start = f"{year:04d}-{month + 1:02d}-01"
    return f"{period}-01", next_start


def _balance_fields(net: int) -> tuple[int, int]:
    return (net, 0) if net >= 0 else (0, -net)


@router.get("/trial-balance")
def trial_balance(
    period: str = Query(default_factory=lambda: date.today().strftime("%Y-%m"), description="会计期间 YYYY-MM"),
    include_zero: bool = False,
    account_set_id: str = Query(default="default"),
    conn: sqlite3.Connection = Depends(get_db),
):
    assert_account_set(conn, account_set_id)
    start_date, next_start = _period_bounds(period)
    accounts = conn.execute(
        "SELECT code,name,category,direction,initial_debit,initial_credit,is_active "
        "FROM accounts WHERE account_set_id=? ORDER BY code", (account_set_id,)
    ).fetchall()
    movements = conn.execute(
        """
        SELECT e.account_code,
            SUM(CASE WHEN v.voucher_date < ? THEN e.debit_amount ELSE 0 END) AS prior_debit,
            SUM(CASE WHEN v.voucher_date < ? THEN e.credit_amount ELSE 0 END) AS prior_credit,
            SUM(CASE WHEN v.voucher_date >= ? AND v.voucher_date < ? THEN e.debit_amount ELSE 0 END) AS period_debit,
            SUM(CASE WHEN v.voucher_date >= ? AND v.voucher_date < ? THEN e.credit_amount ELSE 0 END) AS period_credit
        FROM vouchers v
        JOIN voucher_entries e ON e.account_set_id=v.account_set_id AND e.voucher_id=v.id
        WHERE v.account_set_id=? AND e.account_set_id=? AND v.status IN ('POSTED','REVERSED') AND v.voucher_date < ?
        GROUP BY e.account_code
        """,
        (start_date, start_date, start_date, next_start, start_date, next_start, account_set_id, account_set_id, next_start),
    ).fetchall()
    by_code = {row["account_code"]: row for row in movements}
    items = []
    totals = {
        "initial_debit": 0, "initial_credit": 0,
        "period_debit": 0, "period_credit": 0,
        "ending_debit": 0, "ending_credit": 0,
    }
    for account in accounts:
        movement = by_code.get(account["code"])
        prior_debit = movement["prior_debit"] if movement else 0
        prior_credit = movement["prior_credit"] if movement else 0
        period_debit = movement["period_debit"] if movement else 0
        period_credit = movement["period_credit"] if movement else 0
        opening_net = account["initial_debit"] - account["initial_credit"] + prior_debit - prior_credit
        ending_net = opening_net + period_debit - period_credit
        opening_debit, opening_credit = _balance_fields(opening_net)
        ending_debit, ending_credit = _balance_fields(ending_net)
        if not include_zero and not any((opening_net, period_debit, period_credit, ending_net)):
            continue
        item = {
            "account": {"code": account["code"], "name": account["name"]},
            # Flat aliases are retained for clients that prefer separate fields.
            "account_code": account["code"],
            "account_name": account["name"],
            "category": account["category"],
            "direction": account["direction"],
            "initial_debit": cents_to_yuan(opening_debit),
            "initial_credit": cents_to_yuan(opening_credit),
            # Current-period red-letter movements remain negative by design.
            "period_debit": cents_to_yuan(period_debit),
            "period_credit": cents_to_yuan(period_credit),
            "ending_debit": cents_to_yuan(ending_debit),
            "ending_credit": cents_to_yuan(ending_credit),
        }
        items.append(item)
        for key, value in (
            ("initial_debit", opening_debit), ("initial_credit", opening_credit),
            ("period_debit", period_debit), ("period_credit", period_credit),
            ("ending_debit", ending_debit), ("ending_credit", ending_credit),
        ):
            totals[key] += value
    return {
        "account_set_id": account_set_id,
        "report": "trial_balance",
        "title": "七列科目余额表",
        "period": period,
        "money_unit": "CNY",
        "storage_unit": "cent",
        "columns": [
            "account", "initial_debit", "initial_credit",
            "period_debit", "period_credit", "ending_debit", "ending_credit",
        ],
        "items": items,
        "totals": {key: cents_to_yuan(value) for key, value in totals.items()},
    }


@router.get("/sub-ledger")
def contact_sub_ledger(
    account_code: str,
    contact_id: int,
    period: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    account_set_id: str = Query(default="default"),
    conn: sqlite3.Connection = Depends(get_db),
):
    assert_account_set(conn, account_set_id)
    account = conn.execute("SELECT code,name,direction FROM accounts WHERE account_set_id=? AND code=?", (account_set_id, account_code)).fetchone()
    if not account:
        raise HTTPException(status_code=404, detail="会计科目不存在")
    contact = conn.execute("SELECT id,name,type FROM contacts WHERE account_set_id=? AND id=?", (account_set_id, contact_id)).fetchone()
    if not contact:
        raise HTTPException(status_code=404, detail="往来单位不存在")
    if period:
        period_start, period_end = _period_bounds(period)
        if start_date and start_date.isoformat() != period_start:
            raise HTTPException(status_code=422, detail="period 与 start_date 不能同时指定不同起始日期")
        start_text = period_start
        end_exclusive = period_end
        report_period = period
    else:
        start_text = start_date.isoformat() if start_date else "0001-01-01"
        end_exclusive = (end_date.isoformat() if end_date else "9999-12-31")
        report_period = None
    if end_date and period:
        # Explicit end_date is inclusive; convert to exclusive next day.
        end_exclusive = date.fromordinal(end_date.toordinal() + 1).isoformat()
    elif end_date and not period:
        end_exclusive = date.fromordinal(end_date.toordinal() + 1).isoformat()
    if end_date and date.fromisoformat(start_text) > end_date:
        raise HTTPException(status_code=422, detail="end_date 不能早于查询起始日")

    opening = conn.execute(
        "SELECT as_of_date,initial_debit,initial_credit FROM contact_opening_balances WHERE account_set_id=? AND account_code=? AND contact_id=?",
        (account_set_id, account_code, contact_id),
    ).fetchone()
    if opening:
        as_of = opening["as_of_date"]
        if start_text <= as_of:
            raise HTTPException(
                status_code=422,
                detail=f"该往来辅助期初余额截止日为 {as_of}，查询起始日必须晚于该日期；如需更早账期，请先补充对应期初余额",
            )
        opening_net = opening["initial_debit"] - opening["initial_credit"]
        history = conn.execute(
            """
            SELECT COALESCE(SUM(e.debit_amount-e.credit_amount),0) AS net
            FROM vouchers v JOIN voucher_entries e ON e.account_set_id=v.account_set_id AND e.voucher_id=v.id
            WHERE v.account_set_id=? AND e.account_set_id=? AND v.status IN ('POSTED','REVERSED') AND e.account_code=? AND e.contact_id=?
              AND v.voucher_date > ? AND v.voucher_date < ?
            """,
            (account_set_id, account_set_id, account_code, contact_id, as_of, start_text),
        ).fetchone()
        opening_net += history["net"]
    else:
        prior = conn.execute(
            """
            SELECT COALESCE(SUM(e.debit_amount-e.credit_amount),0) AS net
            FROM vouchers v JOIN voucher_entries e ON e.account_set_id=v.account_set_id AND e.voucher_id=v.id
            WHERE v.account_set_id=? AND e.account_set_id=? AND v.status IN ('POSTED','REVERSED') AND e.account_code=? AND e.contact_id=?
              AND v.voucher_date < ?
            """,
            (account_set_id, account_set_id, account_code, contact_id, start_text),
        ).fetchone()
        opening_net = prior["net"]

    rows = conn.execute(
        """
        SELECT v.id AS voucher_id,v.voucher_no,v.voucher_date,v.summary AS voucher_summary,
               e.id AS entry_id,e.summary AS entry_summary,e.debit_amount,e.credit_amount
        FROM vouchers v JOIN voucher_entries e ON e.account_set_id=v.account_set_id AND e.voucher_id=v.id
        WHERE v.account_set_id=? AND e.account_set_id=? AND v.status IN ('POSTED','REVERSED') AND e.account_code=? AND e.contact_id=?
          AND v.voucher_date >= ? AND v.voucher_date < ?
        ORDER BY v.voucher_date,v.id,e.id
        """,
        (account_set_id, account_set_id, account_code, contact_id, start_text, end_exclusive),
    ).fetchall()

    running = opening_net
    items = []
    for row in rows:
        running += row["debit_amount"] - row["credit_amount"]
        items.append({
            "date": row["voucher_date"],
            "voucher_no": row["voucher_no"],
            "summary": row["entry_summary"] or row["voucher_summary"],
            "debit_amount": cents_to_yuan(row["debit_amount"]),
            "credit_amount": cents_to_yuan(row["credit_amount"]),
            "balance": cents_to_yuan(abs(running)),
            "balance_side": "借" if running > 0 else ("贷" if running < 0 else "平"),
        })
    return {
        "account_set_id": account_set_id,
        "report": "contact_sub_ledger",
        "title": "单位往来三栏账",
        "period": report_period,
        "account_code": account["code"],
        "account_name": account["name"],
        "contact_id": contact["id"],
        "contact_name": contact["name"],
        "money_unit": "CNY",
        "opening_balance": cents_to_yuan(abs(opening_net)),
        "opening_balance_side": "借" if opening_net > 0 else ("贷" if opening_net < 0 else "平"),
        "items": items,
        "ending_balance": cents_to_yuan(abs(running)),
        "ending_balance_side": "借" if running > 0 else ("贷" if running < 0 else "平"),
    }
