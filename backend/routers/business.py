"""Normalized operating subledgers: procurement, sales, inventory, production, payroll and fixed assets."""
from __future__ import annotations
from .returns_core import active_returns, stock_adjustments, validate_return, validate_auxiliary_voucher

import json
import re
import sqlite3
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field, ValidationError

from ..database import get_connection
from ..schemas import VoucherCreate
from .common import assert_period_unlocked, cents_to_yuan, generate_voucher_no, validate_period, validate_upload_url, voucher_dict
from .vouchers import _insert_entries, _validate_entry_payload

router = APIRouter(prefix="/api/business", tags=["采购销售库存工资固定资产"])


def db():
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _tenant(conn: sqlite3.Connection, account_set_id: str):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}", account_set_id or ""):
        raise HTTPException(422, "账套 ID 格式无效")
    if not conn.execute("SELECT 1 FROM account_sets WHERE id=?", (account_set_id,)).fetchone():
        raise HTTPException(404, "账套不存在")


def _qty(value: Any) -> int:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HTTPException(422, "数量格式无效")
    if not d.is_finite() or d <= 0 or d.as_tuple().exponent < -3:
        raise HTTPException(422, "数量必须大于零，最多三位小数")
    n = d * 1000
    if n != n.to_integral_value():
        raise HTTPException(422, "数量最多三位小数")
    return int(n)


def _cents(value: Any, field: str = "金额", allow_zero: bool = True) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f"{field}必须为整数分")
    if n < 0 or (not allow_zero and n == 0):
        raise HTTPException(422, f"{field}不能为负数或零")
    return n


def _str(row: dict, key: str, required: bool = True, default: str = "") -> str:
    value = row.get(key, default)
    if value is None:
        value = default
    value = str(value).strip()
    if required and not value:
        raise HTTPException(422, f"缺少必填字段：{key}")
    return value


def _ensure_item(conn, book, sku, name, account, warehouse):
    if account not in ("1401", "1405"):
        raise HTTPException(422, "库存科目只能为 1401 或 1405")
    exists = conn.execute("SELECT 1 FROM accounts WHERE account_set_id=? AND code=?", (book, account)).fetchone()
    if not exists:
        raise HTTPException(422, f"账套缺少存货科目 {account}，请先维护会计科目")
    conn.execute("""INSERT INTO inventory_items(account_set_id,sku,name,inventory_account_code,default_warehouse)
        VALUES(?,?,?,?,?) ON CONFLICT(account_set_id,sku,inventory_account_code) DO UPDATE SET
        name=excluded.name,default_warehouse=excluded.default_warehouse,is_active=1""",
        (book, sku, name, account, warehouse or "主仓库"))


def _link_voucher(conn, book, module, source_id, role, voucher_legacy_id):
    if not voucher_legacy_id:
        return
    voucher = conn.execute("SELECT id FROM vouchers WHERE account_set_id=? AND legacy_id=?", (book, str(voucher_legacy_id))).fetchone()
    conn.execute("""INSERT INTO business_voucher_links(account_set_id,source_module,source_legacy_id,role,voucher_legacy_id,voucher_id)
        VALUES(?,?,?,?,?,?) ON CONFLICT(account_set_id,source_module,source_legacy_id,role) DO UPDATE SET
        voucher_legacy_id=excluded.voucher_legacy_id,voucher_id=excluded.voucher_id""",
        (book, module, source_id, role, str(voucher_legacy_id), voucher["id"] if voucher else None))


def _upsert_inventory_opening(conn, book, r):
    legacy_id = _str(r, "id")
    period = validate_period(_str(r, "period"))
    rows = r.get("rows") or []
    if not isinstance(rows, list) or len(rows) > 20000:
        raise HTTPException(422, "期初库存明细格式无效或超过 20,000 行")
    seen = set(); by_account = {"1401": 0, "1405": 0}
    for line in rows:
        account = str(line.get("account") or "1405")
        sku, name = _str(line, "sku"), _str(line, "name")
        key = (account, sku.upper(), str(line.get("warehouse") or "主仓库").strip())
        if key in seen:
            raise HTTPException(422, f"期初库存商品编码重复：{sku}")
        seen.add(key)
        _ensure_item(conn, book, sku, name, account, str(line.get("warehouse") or "主仓库"))
        amount = _cents(line.get("stockCostCents", 0), "期初库存金额")
        by_account[account] += amount
    total = by_account["1401"] + by_account["1405"]
    if int(r.get("totalCents", total) or 0) != total:
        raise HTTPException(422, "期初库存总金额与明细合计不一致")
    stated_by_account=r.get("totalByAccount") or {}
    for code in ("1401","1405"):
        if code in stated_by_account and int(stated_by_account[code] or 0)!=by_account[code]:
            raise HTTPException(422,f"期初库存 {code} 科目汇总与明细合计不一致")
    conn.execute("""INSERT INTO inventory_openings(account_set_id,legacy_id,period,verified,total_cents,total_1401_cents,total_1405_cents,created_at)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(account_set_id,legacy_id) DO UPDATE SET period=excluded.period,
        verified=excluded.verified,total_cents=excluded.total_cents,total_1401_cents=excluded.total_1401_cents,
        total_1405_cents=excluded.total_1405_cents,created_at=excluded.created_at""",
        (book, legacy_id, period, int(bool(r.get("verified"))), total, by_account["1401"], by_account["1405"], r.get("createdAt") or now_iso()))
    opening = conn.execute("SELECT id FROM inventory_openings WHERE account_set_id=? AND legacy_id=?", (book, legacy_id)).fetchone()
    conn.execute("DELETE FROM inventory_opening_lines WHERE account_set_id=? AND opening_id=?", (book, opening["id"]))
    for i, line in enumerate(rows):
        account = str(line.get("account") or "1405")
        conn.execute("""INSERT INTO inventory_opening_lines(account_set_id,opening_id,row_no,inventory_account_code,sku,item_name,warehouse,quantity_milli,unit_cost_cents,stock_cost_cents)
            VALUES(?,?,?,?,?,?,?,?,?,?)""", (book, opening["id"], i+1, account, str(line["sku"]), str(line["name"]),
            str(line.get("warehouse") or "主仓库"), _qty(line.get("qty")), _cents(line.get("unitCostCents", 0), "单位成本"), _cents(line.get("stockCostCents", 0), "库存成本")))
    return opening["id"]


def _assert_inventory_ready(conn, book, doc_date, sku, account, warehouse, quantity_milli, cost_cents, legacy_id, kind):
    opening=conn.execute("SELECT * FROM inventory_openings WHERE account_set_id=? AND verified=1 AND period<=? ORDER BY period DESC,id DESC LIMIT 1",(book,doc_date[:7])).fetchone()
    if not opening: raise HTTPException(409,"请先建立并核实库存期初")
    if doc_date < opening["period"]+"-01": raise HTTPException(409,"库存业务日期不能早于库存启用期间")
    current=conn.execute("SELECT voucher_legacy_id FROM business_documents WHERE account_set_id=? AND legacy_id=?",(book,legacy_id)).fetchone()
    if current and current["voucher_legacy_id"]:
        current_voucher=conn.execute("SELECT status FROM vouchers WHERE account_set_id=? AND legacy_id=?",(book,current["voucher_legacy_id"])).fetchone()
        if current_voucher and current_voucher["status"] in ("POSTED","REVERSED","VOIDED"):
            raise HTTPException(409,"已记账或已冲销的库存单据不可修改")
    pending=conn.execute("""SELECT d.doc_no,v.status FROM business_documents d LEFT JOIN vouchers v
        ON v.account_set_id=d.account_set_id AND v.legacy_id=d.voucher_legacy_id
        WHERE d.account_set_id=? AND UPPER(d.sku)=UPPER(?) AND d.inventory_account_code=? AND d.warehouse=? AND d.legacy_id<>?
        AND COALESCE(v.status,'MISSING')<>'POSTED' LIMIT 1""",(book,sku,account,warehouse,legacy_id)).fetchone()
    if pending: raise HTTPException(409,f"商品 {sku} 存在未完成库存单据 {pending['doc_no']}，请先处理凭证")
    latest=conn.execute("""SELECT MAX(d.doc_date) AS latest FROM business_documents d JOIN vouchers v
        ON v.account_set_id=d.account_set_id AND v.legacy_id=d.voucher_legacy_id
        WHERE d.account_set_id=? AND UPPER(d.sku)=UPPER(?) AND d.inventory_account_code=? AND d.warehouse=? AND v.status='POSTED' AND d.legacy_id<>?""",
        (book,sku,account,warehouse,legacy_id)).fetchone()["latest"]
    if latest and doc_date<latest: raise HTTPException(409,"不允许补录早于该商品最近一笔已记账库存业务的日期")
    returned_qty, returned_value = stock_adjustments(conn, book, account, sku, warehouse, doc_date)
    if kind not in ("SALE","PROD_ISSUE","PURCHASE_RETURN"): return
    opening_qty=conn.execute("SELECT COALESCE(SUM(quantity_milli),0) AS qty,COALESCE(SUM(stock_cost_cents),0) AS value FROM inventory_opening_lines WHERE account_set_id=? AND opening_id=? AND UPPER(sku)=UPPER(?) AND inventory_account_code=? AND warehouse=?",
        (book,opening["id"],sku,account,warehouse)).fetchone()
    movement=conn.execute("""SELECT COALESCE(SUM(m.quantity_milli),0) AS qty,COALESCE(SUM(m.amount_cents),0) AS value
        FROM inventory_movements m JOIN business_documents d ON d.account_set_id=m.account_set_id AND d.id=m.document_id
        JOIN vouchers v ON v.account_set_id=m.account_set_id AND v.legacy_id=m.voucher_legacy_id
        WHERE m.account_set_id=? AND UPPER(m.sku)=UPPER(?) AND m.inventory_account_code=? AND m.warehouse=? AND m.movement_date>=? AND m.movement_date<=?
          AND d.legacy_id<>? AND v.status='POSTED'""",(book,sku,account,warehouse,opening["period"]+"-01",doc_date,legacy_id)).fetchone()
    available_qty=int(opening_qty["qty"])+int(movement["qty"])+returned_qty
    available_value=int(opening_qty["value"])+int(movement["value"])+returned_value
    if available_qty<quantity_milli: raise HTTPException(409,f"可用库存不足：当前 {available_qty/1000:g}，本次出库 {quantity_milli/1000:g}")
    if kind=='PURCHASE_RETURN':
        if cost_cents > available_value or (quantity_milli == available_qty and cost_cents != available_value):
            raise HTTPException(409, '采购退货后库存成本会出现差额，请核对原仓库存')
        return
    expected=round(quantity_milli*available_value/available_qty) if available_qty>0 else 0
    if abs(expected-cost_cents)>1: raise HTTPException(409,f"库存移动加权平均成本不一致：应为 {expected} 分，提交 {cost_cents} 分")


def _upsert_inventory_doc(conn, book, r, enforce_stock=False):
    legacy_id = _str(r, "id")
    kind = _str(r, "kind")
    if kind not in ("PURCHASE", "SALE", "PROD_ISSUE", "PROD_RECEIPT"):
        raise HTTPException(422, f"不支持的库存单据类型：{kind}")
    doc_no, doc_date = _str(r, "docNo"), _str(r, "date")
    try: date.fromisoformat(doc_date)
    except ValueError: raise HTTPException(422, "库存业务日期无效")
    period = validate_period(str(r.get("period") or doc_date[:7]))
    if period != doc_date[:7]: raise HTTPException(422, "库存业务期间必须与单据日期一致")
    sku, name, warehouse = _str(r, "sku"), _str(r, "name"), str(r.get("warehouse") or "主仓库")
    account = str(r.get("inventoryAccount") or ("1401" if kind == "PROD_ISSUE" else "1405"))
    _ensure_item(conn, book, sku, name, account, warehouse)
    qty = _qty(r.get("qty"))
    unit_price = _cents(r.get("unitPriceCents", 0), "销售单价")
    unit_cost = _cents(r.get("unitCostCents", r.get("costUnitCents", 0)), "单位成本")
    net = _cents(r.get("netCents", 0), "未税金额")
    tax = _cents(r.get("taxCents", 0), "税额")
    stock_cost = _cents(r.get("stockCostCents", 0), "入库成本")
    gross = _cents(r.get("grossCents", net + tax), "含税金额")
    cost = _cents(r.get("costCents", 0), "销售/领料成本")
    if kind == "PURCHASE":
        expected_stock = net if bool(r.get("deductible")) else net + tax
        if stock_cost != expected_stock or gross != net + tax:
            raise HTTPException(422,"采购入库金额、进项税和库存成本勾稽不一致")
    elif kind == "SALE":
        if account != "1405" or gross != net + tax or cost <= 0:
            raise HTTPException(422,"销售出库必须使用 1405 库存商品；含税金额须等于未税金额加税额，销售成本须大于零")
    elif kind == "PROD_ISSUE":
        if cost <= 0 or account != "1401": raise HTTPException(422,"生产领料必须使用 1401 原材料并提供正数成本")
    elif kind == "PROD_RECEIPT":
        if stock_cost <= 0 or account != "1405": raise HTTPException(422,"生产完工入库必须使用 1405 库存商品并提供正数成本")
        material_cost=_cents(r.get("materialCostCents",0),"完工材料成本")
        conversion_cost=_cents(r.get("conversionAllocationCents",0),"完工人工及制造费用")
        completion_cost=_cents(r.get("completionCostCents",stock_cost),"完工总成本")
        if material_cost+conversion_cost!=completion_cost or completion_cost!=stock_cost:
            raise HTTPException(422,"完工入库成本必须等于材料成本加人工及制造费用")
        if enforce_stock:
            order=conn.execute("SELECT * FROM production_orders WHERE account_set_id=? AND legacy_id=?",(book,str(r.get("productionOrderId") or ""))).fetchone()
            if not order: raise HTTPException(409,"完工入库必须关联已存在的生产订单")
            issue=conn.execute("SELECT status FROM vouchers WHERE account_set_id=? AND legacy_id=?",(book,order["issue_voucher_legacy_id"])).fetchone() if order["issue_voucher_legacy_id"] else None
            if not issue or issue["status"]!="POSTED": raise HTTPException(409,"请先完成生产领料凭证记账，再办理完工入库")
            if order["completion_doc_legacy_id"] and order["completion_doc_legacy_id"]!=legacy_id: raise HTTPException(409,"该生产订单已经存在完工入库单")
    if enforce_stock:
        _assert_inventory_ready(conn,book,doc_date,sku,account,warehouse,qty,cost,legacy_id,kind)
    voucher_legacy_id = str(r.get("voucherId") or "") or None
    invoice_legacy_id = str(r.get("invoiceRecordId") or "") or None
    if enforce_stock and kind == "SALE" and invoice_legacy_id:
        duplicate = conn.execute("SELECT 1 FROM business_documents WHERE account_set_id=? AND doc_type='SALE' AND invoice_legacy_id=? AND legacy_id<>?", (book, invoice_legacy_id, legacy_id)).fetchone()
        ar = conn.execute("SELECT 1 FROM app_records WHERE account_set_id=? AND store_name='Workpapers' AND record_id LIKE 'ar:due:invoice:%' AND json_extract(payload_json,'$.source')=?", (book, invoice_legacy_id)).fetchone()
        if duplicate or ar:
            raise HTTPException(409,"发票已关联其他销售或应收业务")
    production_id = str(r.get("productionOrderId") or "") or None
    values = (book, legacy_id, kind, doc_no, doc_date, period, r.get("supplier") or r.get("customer"), account,
        sku, name, warehouse, qty, unit_price, unit_cost, net, tax, stock_cost, gross, cost,
        int(bool(r.get("deductible"))), str(r.get("vatRate")) if r.get("vatRate") is not None else None,
        invoice_legacy_id, production_id, _cents(r.get("materialCostCents", 0)),
        _cents(r.get("conversionAllocationCents", 0)), _cents(r.get("completionCostCents", 0)),
        voucher_legacy_id, r.get("createdAt") or now_iso())
    conn.execute("""INSERT INTO business_documents(account_set_id,legacy_id,doc_type,doc_no,doc_date,period,partner_name,inventory_account_code,sku,item_name,warehouse,quantity_milli,unit_price_cents,unit_cost_cents,net_cents,tax_cents,stock_cost_cents,gross_cents,cost_cents,deductible,vat_rate,invoice_legacy_id,production_order_legacy_id,material_cost_cents,conversion_allocation_cents,completion_cost_cents,voucher_legacy_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_set_id,legacy_id) DO UPDATE SET doc_type=excluded.doc_type,doc_no=excluded.doc_no,doc_date=excluded.doc_date,
        period=excluded.period,partner_name=excluded.partner_name,inventory_account_code=excluded.inventory_account_code,sku=excluded.sku,
        item_name=excluded.item_name,warehouse=excluded.warehouse,quantity_milli=excluded.quantity_milli,unit_price_cents=excluded.unit_price_cents,
        unit_cost_cents=excluded.unit_cost_cents,net_cents=excluded.net_cents,tax_cents=excluded.tax_cents,stock_cost_cents=excluded.stock_cost_cents,
        gross_cents=excluded.gross_cents,cost_cents=excluded.cost_cents,deductible=excluded.deductible,vat_rate=excluded.vat_rate,
        invoice_legacy_id=excluded.invoice_legacy_id,production_order_legacy_id=excluded.production_order_legacy_id,
        material_cost_cents=excluded.material_cost_cents,conversion_allocation_cents=excluded.conversion_allocation_cents,
        completion_cost_cents=excluded.completion_cost_cents,voucher_legacy_id=excluded.voucher_legacy_id""", values)
    doc = conn.execute("SELECT id FROM business_documents WHERE account_set_id=? AND legacy_id=?", (book, legacy_id)).fetchone()
    conn.execute("DELETE FROM inventory_movements WHERE account_set_id=? AND document_id=?", (book, doc["id"]))
    sign = 1 if kind in ("PURCHASE", "PROD_RECEIPT") else -1
    amount = stock_cost if kind in ("PURCHASE", "PROD_RECEIPT") else cost
    movement_cost = (stock_cost // max(qty, 1)) if kind == "PURCHASE" else (unit_cost if kind == "PROD_RECEIPT" else (cost // max(qty, 1)))
    conn.execute("""INSERT INTO inventory_movements(account_set_id,document_id,movement_date,movement_type,sku,inventory_account_code,warehouse,quantity_milli,unit_cost_cents,amount_cents,voucher_legacy_id)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (book, doc["id"], doc_date, kind, sku, account, warehouse, sign*qty, movement_cost, sign*amount, voucher_legacy_id))
    _link_voucher(conn, book, "inventory", legacy_id, "PRIMARY", voucher_legacy_id)
    return doc["id"]


def _upsert_settlement(conn, book, r):
    legacy_id = _str(r, "id")
    kind = _str(r, "kind")
    if kind not in ("AR_RECEIPT", "AP_PAYMENT"):
        raise HTTPException(422, "不支持的往来结算类型")
    amount = _cents(r.get("amountCents"), "结算金额", allow_zero=False)
    existing=conn.execute("SELECT voucher_legacy_id FROM business_settlements WHERE account_set_id=? AND legacy_id=?",(book,legacy_id)).fetchone()
    if existing and existing["voucher_legacy_id"]:
        linked=conn.execute("SELECT status FROM vouchers WHERE account_set_id=? AND legacy_id=?",(book,existing["voucher_legacy_id"])).fetchone()
        if linked and linked["status"] in ("POSTED","REVERSED","VOIDED"):
            raise HTTPException(409,"已审核/记账的往来结算不能修改")
    source = _str(r, "sourceDocId")
    source_doc=conn.execute("SELECT doc_type,doc_no,doc_date,gross_cents,net_cents,tax_cents FROM business_documents WHERE account_set_id=? AND legacy_id=?", (book, source)).fetchone()
    if not source_doc:
        raise HTTPException(422, "结算来源业务单据不存在")
    expected_type="SALE" if kind=="AR_RECEIPT" else "PURCHASE"
    if source_doc["doc_type"]!=expected_type: raise HTTPException(422,"收款只能核销销售单，付款只能核销采购单")
    settlement_date=_str(r,"date")
    try: date.fromisoformat(settlement_date)
    except ValueError: raise HTTPException(422,"结算日期无效")
    settlement_period=validate_period(_str(r,"period"))
    if settlement_date[:7]!=settlement_period: raise HTTPException(422,"结算期间必须与结算日期一致")
    if settlement_date < source_doc["doc_date"]: raise HTTPException(422,"结算日期不能早于来源单据日期")
    source_amount=source_doc["gross_cents"] if kind=="AR_RECEIPT" else source_doc["net_cents"]+source_doc["tax_cents"]
    source_amount-=sum(row['grossCents'] for row,_ in active_returns(conn,book) if row['sourceDocId']==source)
    used=conn.execute("SELECT COALESCE(SUM(amount_cents),0) AS total FROM business_settlements WHERE account_set_id=? AND source_doc_legacy_id=? AND legacy_id<>?",(book,source,legacy_id)).fetchone()["total"]
    if amount+used>source_amount: raise HTTPException(409,"结算金额超过来源单据剩余未结金额")
    conn.execute("""INSERT INTO business_settlements(account_set_id,legacy_id,settlement_type,settlement_no,source_doc_legacy_id,source_doc_no,source_date,counterparty,settlement_date,period,bank,amount_cents,voucher_legacy_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_set_id,legacy_id) DO UPDATE SET settlement_type=excluded.settlement_type,
        settlement_no=excluded.settlement_no,source_doc_legacy_id=excluded.source_doc_legacy_id,source_doc_no=excluded.source_doc_no,
        source_date=excluded.source_date,counterparty=excluded.counterparty,settlement_date=excluded.settlement_date,period=excluded.period,
        bank=excluded.bank,amount_cents=excluded.amount_cents,voucher_legacy_id=excluded.voucher_legacy_id""",
        (book, legacy_id, kind, _str(r,"settlementNo"), source, _str(r,"sourceDocNo"), _str(r,"sourceDate"), _str(r,"counterparty"),
         settlement_date, settlement_period, _str(r,"bank"), amount, str(r.get("voucherId") or "") or None, r.get("createdAt") or now_iso()))
    _link_voucher(conn, book, "settlement", legacy_id, "PRIMARY", r.get("voucherId"))


def _upsert_production_order(conn, book, r):
    legacy_id = _str(r, "id")
    order_no = _str(r, "orderNo")
    order_date = _str(r, "date")
    try: date.fromisoformat(order_date)
    except ValueError: raise HTTPException(422, "生产订单日期无效")
    materials = r.get("materials") or []
    if not isinstance(materials, list) or not materials or len(materials) > 100:
        raise HTTPException(422, "生产订单必须包含 1 至 100 行原材料")
    existing_order=conn.execute("SELECT * FROM production_orders WHERE account_set_id=? AND legacy_id=?",(book,legacy_id)).fetchone()
    if existing_order and existing_order["issue_voucher_legacy_id"]:
        issue=conn.execute("SELECT status FROM vouchers WHERE account_set_id=? AND legacy_id=?",(book,existing_order["issue_voucher_legacy_id"])).fetchone()
        if issue and issue["status"] in ("POSTED","REVERSED","VOIDED"):
            old_materials=conn.execute("SELECT sku,item_name,warehouse,quantity_milli,cost_cents FROM production_material_lines WHERE account_set_id=? AND production_order_id=? ORDER BY row_no",(book,existing_order["id"])).fetchall()
            new_materials=[(str(m.get("sku") or ""),str(m.get("name") or ""),str(m.get("warehouse") or "主仓库"),_qty(m.get("qty")),_cents(m.get("costCents",0))) for m in materials]
            old_core=(existing_order["order_no"],existing_order["order_date"],existing_order["period"],existing_order["finished_sku"],existing_order["finished_name"],existing_order["finished_warehouse"],existing_order["finished_qty_milli"],existing_order["material_cost_cents"],existing_order["issue_voucher_legacy_id"])
            new_core=(_str(r,"orderNo"),_str(r,"date"),validate_period(str(r.get("period") or str(r.get("date") or "")[:7])),_str(r,"finishedSku"),_str(r,"finishedName"),str(r.get("finishedWarehouse") or "主仓库"),_qty(r.get("finishedQty")),_cents(r.get("materialCostCents",0)),str(r.get("issueVoucherId") or "") or None)
            if old_core!=new_core or [tuple(x) for x in old_materials]!=new_materials:
                raise HTTPException(409,"生产领料凭证已记账，订单、成品和原材料明细不可修改")
    if not isinstance(materials, list) or not materials or len(materials) > 100:
        raise HTTPException(422, "生产订单必须包含 1 至 100 行原材料")
    finished_sku = _str(r,"finishedSku")
    materials_total=sum(_cents(m.get("costCents",0),"原材料成本") for m in materials)
    if materials_total != _cents(r.get("materialCostCents",materials_total),"材料成本合计"):
        raise HTTPException(422,"生产订单材料成本合计与原材料明细不一致")
    if any(str(m.get("sku")) == finished_sku for m in materials):
        raise HTTPException(422, "成品编码不能与原材料编码相同")
    period=validate_period(str(r.get("period") or order_date[:7]))
    if period!=order_date[:7]: raise HTTPException(422,"生产订单期间必须与日期一致")
    conn.execute("""INSERT INTO production_orders(account_set_id,legacy_id,order_no,order_date,period,finished_sku,finished_name,finished_warehouse,finished_qty_milli,material_cost_cents,issue_voucher_legacy_id,status,completion_voucher_legacy_id,completion_doc_legacy_id,completion_date,completion_period,conversion_allocation_cents,completion_cost_cents,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_set_id,legacy_id) DO UPDATE SET order_no=excluded.order_no,order_date=excluded.order_date,
        period=excluded.period,finished_sku=excluded.finished_sku,finished_name=excluded.finished_name,finished_warehouse=excluded.finished_warehouse,
        finished_qty_milli=excluded.finished_qty_milli,material_cost_cents=excluded.material_cost_cents,issue_voucher_legacy_id=excluded.issue_voucher_legacy_id,
        status=excluded.status,completion_voucher_legacy_id=excluded.completion_voucher_legacy_id,completion_doc_legacy_id=excluded.completion_doc_legacy_id,
        completion_date=excluded.completion_date,completion_period=excluded.completion_period,conversion_allocation_cents=excluded.conversion_allocation_cents,
        completion_cost_cents=excluded.completion_cost_cents""",
        (book, legacy_id, order_no, order_date, period, finished_sku,
         _str(r,"finishedName"), str(r.get("finishedWarehouse") or "主仓库"), _qty(r.get("finishedQty")), _cents(r.get("materialCostCents",0)),
         str(r.get("issueVoucherId") or "") or None, _str(r,"status",False,"ISSUE_PENDING"), str(r.get("completionVoucherId") or "") or None,
         str(r.get("completionDocId") or "") or None, r.get("completionDate"), r.get("completionPeriod"),
         _cents(r.get("conversionAllocationCents",0)), _cents(r.get("completionCostCents",0)), r.get("createdAt") or now_iso()))
    row=conn.execute("SELECT id FROM production_orders WHERE account_set_id=? AND legacy_id=?",(book,legacy_id)).fetchone()
    conn.execute("DELETE FROM production_material_lines WHERE account_set_id=? AND production_order_id=?",(book,row["id"]))
    seen=set()
    for i,m in enumerate(materials):
        sku=_str(m,"sku")
        if sku.upper() in seen: raise HTTPException(422,"生产原材料编码重复")
        seen.add(sku.upper())
        _ensure_item(conn,book,sku,_str(m,"name"),"1401",str(m.get("warehouse") or "主仓库"))
        conn.execute("INSERT INTO production_material_lines(account_set_id,production_order_id,row_no,sku,item_name,warehouse,quantity_milli,cost_cents) VALUES(?,?,?,?,?,?,?,?)",
          (book,row["id"],i+1,sku,str(m["name"]),str(m.get("warehouse") or "主仓库"),_qty(m.get("qty")),_cents(m.get("costCents",0))))
    issue_docs=conn.execute("SELECT COALESCE(SUM(cost_cents),0) AS total FROM business_documents WHERE account_set_id=? AND production_order_legacy_id=? AND doc_type='PROD_ISSUE'",(book,legacy_id)).fetchone()["total"]
    if issue_docs and int(issue_docs)!=_cents(r.get("materialCostCents",0),"生产材料成本"):
        raise HTTPException(422,"生产领料单成本合计与生产订单材料成本不一致")
    completion_doc_id=str(r.get("completionDocId") or "")
    if completion_doc_id:
        receipt=conn.execute("SELECT * FROM business_documents WHERE account_set_id=? AND legacy_id=?",(book,completion_doc_id)).fetchone()
        if not receipt or receipt["doc_type"]!="PROD_RECEIPT" or receipt["production_order_legacy_id"]!=legacy_id or receipt["stock_cost_cents"]!=_cents(r.get("completionCostCents",0)):
            raise HTTPException(422,"生产订单完工信息与完工入库单不一致")
    _link_voucher(conn,book,"production",legacy_id,"ISSUE",r.get("issueVoucherId"))
    if r.get("completionVoucherId"):_link_voucher(conn,book,"production",legacy_id,"COMPLETION",r.get("completionVoucherId"))


def _upsert_payroll_batch(conn, book, r):
    legacy_id=_str(r,"id"); period=validate_period(_str(r,"period")); rows=r.get("rows") or []
    if not isinstance(rows,list) or not rows or len(rows)>5000: raise HTTPException(422,"工资批次必须包含 1 至 5,000 名员工")
    existing_batch=conn.execute("SELECT * FROM payroll_batches WHERE account_set_id=? AND legacy_id=?",(book,legacy_id)).fetchone()
    if existing_batch and existing_batch["accrual_voucher_legacy_id"]:
        accrual=conn.execute("SELECT status FROM vouchers WHERE account_set_id=? AND legacy_id=?",(book,existing_batch["accrual_voucher_legacy_id"])).fetchone()
        if accrual and accrual["status"] in ("POSTED","REVERSED","VOIDED"):
            old_lines=conn.execute("SELECT employee_code,employee_name,department,gross_cents,employee_social_cents,iit_cents,other_deductions_cents,employer_social_cents,net_cents,expense_account_code FROM payroll_lines WHERE account_set_id=? AND payroll_batch_id=? ORDER BY row_no",(book,existing_batch["id"])).fetchall()
            new_lines=[(_str(x,"code"),_str(x,"name"),str(x.get("dept") or ""),_cents(x.get("gross",0)),_cents(x.get("employeeSocial",0)),_cents(x.get("iit",0)),_cents(x.get("other",0)),_cents(x.get("employerSocial",0)),_cents(x.get("net",0)),_str(x,"expenseAccount")) for x in rows]
            old_core=(existing_batch["period"],existing_batch["gross_cents"],existing_batch["net_cents"],existing_batch["iit_cents"],existing_batch["employee_social_cents"],existing_batch["other_deductions_cents"],existing_batch["employer_social_cents"],existing_batch["deductions_cents"],existing_batch["expense_cents"],existing_batch["accrual_voucher_legacy_id"])
            new_core=(period,int(r.get("grossCents",0)),int(r.get("netCents",0)),int(r.get("iitCents",0)),int(r.get("employeeSocialCents",0)),int(r.get("otherCents",0)),int(r.get("employerSocialCents",0)),int(r.get("deductionsCents",0)),int(r.get("expenseCents",0)),str(r.get("accrualVoucherId") or ""))
            if old_core!=new_core or [tuple(x) for x in old_lines]!=new_lines: raise HTTPException(409,"工资计提凭证已记账，工资金额及员工明细不可修改")
    fields={"grossCents":"gross","netCents":"net","iitCents":"iit","employeeSocialCents":"employeeSocial","otherCents":"other","employerSocialCents":"employerSocial"}
    sums={key:sum(_cents(line.get(value,0),value) for line in rows) for key,value in fields.items()}
    sums["deductionsCents"]=sums["employeeSocialCents"]+sums["otherCents"]+sums["employerSocialCents"]
    sums["expenseCents"]=sums["grossCents"]+sums["employerSocialCents"]
    for key,total in sums.items():
        if int(r.get(key,total) or 0)!=total: raise HTTPException(422,f"工资批次汇总字段 {key} 与明细合计不一致")
    if sums["netCents"] != sums["grossCents"]-sums["employeeSocialCents"]-sums["iitCents"]-sums["otherCents"]:
        raise HTTPException(422,"工资净发金额与应发工资及个人扣款不一致")
    if sums["deductionsCents"] != sums["employeeSocialCents"]+sums["otherCents"]+sums["employerSocialCents"]:
        raise HTTPException(422,"工资代扣代缴汇总金额不一致")
    if sums["expenseCents"] != sums["grossCents"]+sums["employerSocialCents"]:
        raise HTTPException(422,"工资费用合计不一致")
    conn.execute("""INSERT INTO payroll_batches(account_set_id,legacy_id,period,gross_cents,net_cents,iit_cents,employee_social_cents,other_deductions_cents,employer_social_cents,deductions_cents,expense_cents,accrual_voucher_legacy_id,payment_voucher_legacy_id,remittance_voucher_legacy_id,created_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_set_id,legacy_id) DO UPDATE SET gross_cents=excluded.gross_cents,net_cents=excluded.net_cents,
      iit_cents=excluded.iit_cents,employee_social_cents=excluded.employee_social_cents,other_deductions_cents=excluded.other_deductions_cents,
      employer_social_cents=excluded.employer_social_cents,deductions_cents=excluded.deductions_cents,expense_cents=excluded.expense_cents,
      accrual_voucher_legacy_id=excluded.accrual_voucher_legacy_id,payment_voucher_legacy_id=excluded.payment_voucher_legacy_id,
      remittance_voucher_legacy_id=excluded.remittance_voucher_legacy_id""",
      (book,legacy_id,period,sums["grossCents"],sums["netCents"],sums["iitCents"],sums["employeeSocialCents"],sums["otherCents"],sums["employerSocialCents"],sums["deductionsCents"],sums["expenseCents"],
       _str(r,"accrualVoucherId"),str(r.get("paymentVoucherId") or "") or None,str(r.get("remittanceVoucherId") or "") or None,r.get("createdAt") or now_iso()))
    batch=conn.execute("SELECT id FROM payroll_batches WHERE account_set_id=? AND legacy_id=?",(book,legacy_id)).fetchone()
    conn.execute("DELETE FROM payroll_lines WHERE account_set_id=? AND payroll_batch_id=?",(book,batch["id"]))
    seen=set()
    for i,line in enumerate(rows):
        code=_str(line,"code")
        if code.upper() in seen: raise HTTPException(422,"工资员工编号重复")
        seen.add(code.upper())
        expense=_str(line,"expenseAccount")
        if not conn.execute("SELECT 1 FROM accounts WHERE account_set_id=? AND code=?",(book,expense)).fetchone(): raise HTTPException(422,f"工资费用科目不存在：{expense}")
        gross=_cents(line.get("gross",0)); employee=_cents(line.get("employeeSocial",0)); iit=_cents(line.get("iit",0)); other=_cents(line.get("other",0)); employer=_cents(line.get("employerSocial",0)); net=_cents(line.get("net",gross-employee-iit-other))
        if net != gross-employee-iit-other: raise HTTPException(422,f"员工 {code} 的净发工资与扣款不一致")
        conn.execute("""INSERT INTO payroll_lines(account_set_id,payroll_batch_id,row_no,employee_code,employee_name,department,gross_cents,employee_social_cents,iit_cents,other_deductions_cents,employer_social_cents,net_cents,expense_account_code)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(book,batch["id"],i+1,code,_str(line,"name"),str(line.get("dept") or ""),gross,employee,iit,other,employer,net,expense))
    _link_voucher(conn,book,"payroll",legacy_id,"ACCRUAL",r.get("accrualVoucherId"))
    if r.get("paymentVoucherId"):_link_voucher(conn,book,"payroll",legacy_id,"PAYMENT",r.get("paymentVoucherId"))
    if r.get("remittanceVoucherId"):_link_voucher(conn,book,"payroll",legacy_id,"REMITTANCE",r.get("remittanceVoucherId"))


def _upsert_asset(conn, book, a):
    code=_str(a,"code")
    if len(code)>80: raise HTTPException(422,"固定资产编号过长")
    cost=_cents(a.get("cost",0),"资产原值")
    qty=_qty(a.get("qty",1))
    residual=Decimal(str(a.get("residual",0))); years=Decimal(str(a.get("years",1)))
    if not residual.is_finite() or residual<0 or residual>100 or not years.is_finite() or years<=0 or years>100: raise HTTPException(422,"固定资产残值率或使用年限无效")
    start_date=_str(a,"start")
    try: date.fromisoformat(start_date)
    except ValueError: raise HTTPException(422,"固定资产启用日期无效")
    existing=conn.execute("SELECT * FROM fixed_assets WHERE account_set_id=? AND asset_code=?",(book,code)).fetchone()
    if existing:
        history=conn.execute("SELECT COUNT(*) AS n FROM depreciation_lines WHERE account_set_id=? AND asset_code=?",(book,code)).fetchone()["n"]
        protected_changed=(existing["cost_cents"]!=cost or Decimal(existing["residual_percent"])!=residual or Decimal(existing["useful_life_years"])!=years or existing["start_date"]!=start_date)
        if history and protected_changed: raise HTTPException(409,f"固定资产 {code} 已有折旧记录，原值、残值率、使用年限和启用日期不可修改")
    conn.execute("""INSERT INTO fixed_assets(account_set_id,asset_code,name,asset_type,cost_cents,residual_percent,useful_life_years,start_date,department,location,keeper,quantity_milli,is_active)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_set_id,asset_code) DO UPDATE SET name=excluded.name,asset_type=excluded.asset_type,cost_cents=excluded.cost_cents,
      residual_percent=excluded.residual_percent,useful_life_years=excluded.useful_life_years,start_date=excluded.start_date,department=excluded.department,
      location=excluded.location,keeper=excluded.keeper,quantity_milli=excluded.quantity_milli,is_active=excluded.is_active""",
      (book,code,_str(a,"name"),str(a.get("type") or "其他"),cost,str(residual),str(years),str(a["start"]),_str(a,"dept"),str(a.get("place") or ""),str(a.get("keeper") or ""),qty,int(a.get("active",True) is not False)))


def _upsert_depreciation(conn, book, period, rows):
    period=validate_period(period)
    if not isinstance(rows,list) or len(rows)>20000: raise HTTPException(422,"折旧明细格式无效")
    conn.execute("INSERT INTO depreciation_runs(account_set_id,period,created_at) VALUES(?,?,?) ON CONFLICT(account_set_id,period) DO NOTHING",(book,period,now_iso()))
    run=conn.execute("SELECT id,voucher_legacy_id,voucher_id FROM depreciation_runs WHERE account_set_id=? AND period=?",(book,period)).fetchone()
    if run["voucher_id"]:
        linked=conn.execute("SELECT status FROM vouchers WHERE account_set_id=? AND id=?",(book,run["voucher_id"])).fetchone()
        if linked and linked["status"] in ("POSTED","REVERSED","VOIDED"):
            old=conn.execute("SELECT asset_code,amount_cents,cumulative_cents,net_cents FROM depreciation_lines WHERE account_set_id=? AND run_id=? ORDER BY asset_code",(book,run["id"])).fetchall()
            new=[]
            for row in rows:
                new.append((_str(row,"code"),_cents(row.get("amount",0)),_cents(row.get("cumulative",0)),_cents(row.get("net",0))))
            if [tuple(x) for x in old]!=new: raise HTTPException(409,"折旧凭证已审核/记账，历史折旧明细不可修改")
            return
    if not run["voucher_id"]:
        legacy_voucher=f"cost:{period}:depr:固定资产台账"
        linked=conn.execute("SELECT id FROM vouchers WHERE account_set_id=? AND legacy_id=?",(book,legacy_voucher)).fetchone()
        if linked:
            conn.execute("UPDATE depreciation_runs SET voucher_legacy_id=?,voucher_id=? WHERE account_set_id=? AND id=?",(legacy_voucher,linked["id"],book,run["id"]))
            conn.execute("INSERT INTO business_voucher_links(account_set_id,source_module,source_legacy_id,role,voucher_legacy_id,voucher_id) VALUES(?,?,?,?,?,?) ON CONFLICT(account_set_id,source_module,source_legacy_id,role) DO UPDATE SET voucher_id=excluded.voucher_id,voucher_legacy_id=excluded.voucher_legacy_id",(book,"depreciation",period,"PRIMARY",legacy_voucher,linked["id"]))
    conn.execute("DELETE FROM depreciation_lines WHERE account_set_id=? AND run_id=?",(book,run["id"]))
    seen=set()
    for row in rows:
        code=_str(row,"code")
        if code in seen: raise HTTPException(422,"折旧资产编号重复")
        seen.add(code)
        asset=conn.execute("SELECT * FROM fixed_assets WHERE account_set_id=? AND asset_code=?",(book,code)).fetchone()
        if not asset:
            _upsert_asset(conn,book,row)
            asset=conn.execute("SELECT * FROM fixed_assets WHERE account_set_id=? AND asset_code=?",(book,code)).fetchone()
        amount=_cents(row.get("amount",0),"本期折旧")
        cumulative=_cents(row.get("cumulative",0),"累计折旧")
        net=_cents(row.get("net",0),"资产净值")
        residual=Decimal(asset["residual_percent"])
        residual_value=int((Decimal(asset["cost_cents"])*residual/Decimal(100)).quantize(Decimal("1"),rounding=ROUND_HALF_UP))
        depreciable_base=asset["cost_cents"]-residual_value
        if amount>cumulative or cumulative>depreciable_base or net!=asset["cost_cents"]-cumulative:
            raise HTTPException(422,f"固定资产 {code} 折旧金额、累计折旧或账面净值勾稽不一致")
        if period < asset["start_date"][:7]: raise HTTPException(422,f"固定资产 {code} 不能在启用前计提折旧")
        conn.execute("INSERT INTO depreciation_lines(account_set_id,run_id,asset_code,amount_cents,cumulative_cents,net_cents) VALUES(?,?,?,?,?,?)",
            (book,run["id"],code,amount,cumulative,net))


def _upsert_payroll_draft(conn, book, period, rows):
    period=validate_period(period)
    if not isinstance(rows,list) or len(rows)>5000: raise HTTPException(422,"工资草稿格式无效")
    conn.execute("INSERT INTO payroll_drafts(account_set_id,period,created_at,updated_at) VALUES(?,?,?,?) ON CONFLICT(account_set_id,period) DO UPDATE SET updated_at=excluded.updated_at",(book,period,now_iso(),now_iso()))
    draft=conn.execute("SELECT id FROM payroll_drafts WHERE account_set_id=? AND period=?",(book,period)).fetchone()
    conn.execute("DELETE FROM payroll_draft_lines WHERE account_set_id=? AND payroll_draft_id=?",(book,draft["id"]))
    seen=set()
    for i,line in enumerate(rows):
        code=_str(line,"code")
        if code.upper() in seen: raise HTTPException(422,"工资草稿员工编号重复")
        seen.add(code.upper())
        expense=_str(line,"expenseAccount")
        if not conn.execute("SELECT 1 FROM accounts WHERE account_set_id=? AND code=?",(book,expense)).fetchone(): raise HTTPException(422,f"工资费用科目不存在：{expense}")
        gross=_cents(line.get("gross",0)); emp=_cents(line.get("employeeSocial",0)); iit=_cents(line.get("iit",0)); other=_cents(line.get("other",0)); employer=_cents(line.get("employerSocial",0)); net=_cents(line.get("net",gross-emp-iit-other))
        if net!=gross-emp-iit-other: raise HTTPException(422,f"员工 {code} 净发工资校验失败")
        conn.execute("INSERT INTO payroll_draft_lines(account_set_id,payroll_draft_id,row_no,employee_code,employee_name,department,gross_cents,employee_social_cents,iit_cents,other_deductions_cents,employer_social_cents,net_cents,expense_account_code) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (book,draft["id"],i+1,code,_str(line,"name"),str(line.get("dept") or ""),gross,emp,iit,other,employer,net,expense))


def _line_dict(line):
    return {"code":line["employee_code"],"name":line["employee_name"],"dept":line["department"],"gross":line["gross_cents"],"employeeSocial":line["employee_social_cents"],"iit":line["iit_cents"],"other":line["other_deductions_cents"],"employerSocial":line["employer_social_cents"],"net":line["net_cents"],"expenseAccount":line["expense_account_code"]}


def _read_business_records(conn, book):
    items=[]
    for h in conn.execute("SELECT * FROM inventory_openings WHERE account_set_id=? ORDER BY period",(book,)):
        rows=conn.execute("SELECT * FROM inventory_opening_lines WHERE account_set_id=? AND opening_id=? ORDER BY row_no",(book,h["id"])).fetchall()
        items.append({"id":h["legacy_id"],"period":h["period"],"rows":[{"account":x["inventory_account_code"],"sku":x["sku"],"name":x["item_name"],"warehouse":x["warehouse"],"qty":x["quantity_milli"]/1000,"unitCostCents":x["unit_cost_cents"],"stockCostCents":x["stock_cost_cents"]} for x in rows],"totalCents":h["total_cents"],"totalByAccount":{"1401":h["total_1401_cents"],"1405":h["total_1405_cents"]},"verified":bool(h["verified"]),"createdAt":h["created_at"]})
    for d in conn.execute("SELECT * FROM business_documents WHERE account_set_id=? ORDER BY doc_date,id",(book,)):
        item={"id":d["legacy_id"],"kind":d["doc_type"],"docNo":d["doc_no"],"date":d["doc_date"],"period":d["period"],"supplier":d["partner_name"] if d["doc_type"]=="PURCHASE" else None,"customer":d["partner_name"] if d["doc_type"]=="SALE" else None,"inventoryAccount":d["inventory_account_code"],"sku":d["sku"],"name":d["item_name"],"warehouse":d["warehouse"],"qty":d["quantity_milli"]/1000,"unitPriceCents":d["unit_price_cents"],"unitCostCents":d["unit_cost_cents"],"netCents":d["net_cents"],"taxCents":d["tax_cents"],"stockCostCents":d["stock_cost_cents"],"grossCents":d["gross_cents"],"costCents":d["cost_cents"],"costUnitCents":d["unit_cost_cents"],"deductible":bool(d["deductible"]),"vatRate":float(d["vat_rate"]) if d["vat_rate"] is not None else 0,"invoiceRecordId":d["invoice_legacy_id"] or "","productionOrderId":d["production_order_legacy_id"],"materialCostCents":d["material_cost_cents"],"conversionAllocationCents":d["conversion_allocation_cents"],"completionCostCents":d["completion_cost_cents"],"voucherId":d["voucher_legacy_id"],"createdAt":d["created_at"]}
        items.append(item)
    for d in conn.execute("SELECT * FROM business_settlements WHERE account_set_id=? ORDER BY settlement_date,id",(book,)):
        items.append({"id":d["legacy_id"],"kind":d["settlement_type"],"settlementNo":d["settlement_no"],"sourceDocId":d["source_doc_legacy_id"],"sourceDocNo":d["source_doc_no"],"sourceDate":d["source_date"],"counterparty":d["counterparty"],"date":d["settlement_date"],"period":d["period"],"bank":d["bank"],"amountCents":d["amount_cents"],"voucherId":d["voucher_legacy_id"],"createdAt":d["created_at"]})
    for d in conn.execute("SELECT * FROM production_orders WHERE account_set_id=? ORDER BY order_date,id",(book,)):
        mats=conn.execute("SELECT * FROM production_material_lines WHERE account_set_id=? AND production_order_id=? ORDER BY row_no",(book,d["id"])).fetchall()
        items.append({"id":d["legacy_id"],"orderNo":d["order_no"],"date":d["order_date"],"period":d["period"],"materials":[{"sku":m["sku"],"name":m["item_name"],"warehouse":m["warehouse"],"qty":m["quantity_milli"]/1000,"costCents":m["cost_cents"]} for m in mats],"materialCostCents":d["material_cost_cents"],"finishedSku":d["finished_sku"],"finishedName":d["finished_name"],"finishedWarehouse":d["finished_warehouse"],"finishedQty":d["finished_qty_milli"]/1000,"issueVoucherId":d["issue_voucher_legacy_id"],"status":d["status"],"completionVoucherId":d["completion_voucher_legacy_id"],"completionDocId":d["completion_doc_legacy_id"],"completionDate":d["completion_date"],"completionPeriod":d["completion_period"],"conversionAllocationCents":d["conversion_allocation_cents"],"completionCostCents":d["completion_cost_cents"],"createdAt":d["created_at"]})
    for d in conn.execute("SELECT * FROM payroll_batches WHERE account_set_id=? ORDER BY period",(book,)):
        rows=conn.execute("SELECT * FROM payroll_lines WHERE account_set_id=? AND payroll_batch_id=? ORDER BY row_no",(book,d["id"])).fetchall()
        items.append({"id":d["legacy_id"],"period":d["period"],"rows":[_line_dict(x) for x in rows],"grossCents":d["gross_cents"],"netCents":d["net_cents"],"iitCents":d["iit_cents"],"employeeSocialCents":d["employee_social_cents"],"otherCents":d["other_deductions_cents"],"employerSocialCents":d["employer_social_cents"],"deductionsCents":d["deductions_cents"],"expenseCents":d["expense_cents"],"accrualVoucherId":d["accrual_voucher_legacy_id"],"paymentVoucherId":d["payment_voucher_legacy_id"],"remittanceVoucherId":d["remittance_voucher_legacy_id"],"createdAt":d["created_at"]})
    return items


def _upsert_record(conn, book, r, enforce_stock=False):
    legacy_id=_str(r,"id")
    if legacy_id=="inventory-opening": return _upsert_inventory_opening(conn,book,r)
    if legacy_id.startswith("inventory-doc:"): return _upsert_inventory_doc(conn,book,r,enforce_stock=enforce_stock)
    if legacy_id.startswith("biz-settlement:"): return _upsert_settlement(conn,book,r)
    if legacy_id.startswith("production-order:"): return _upsert_production_order(conn,book,r)
    if legacy_id.startswith("payroll-batch:"): return _upsert_payroll_batch(conn,book,r)
    raise HTTPException(422,f"不是 V10 规范化业务记录：{legacy_id}")


class BusinessBatch(BaseModel):
    account_set_id: str = "default"
    records: list[dict[str, Any]] = Field(default_factory=list)
    insert_only_ids: list[str] = Field(default_factory=list)
    replace_normalized: bool = False
    skip_stock_validation: bool = False


class BusinessCommit(BaseModel):
    account_set_id: str = "default"
    records: list[dict[str, Any]] = Field(default_factory=list)
    vouchers: list[dict[str, Any]] = Field(default_factory=list)
    insert_only_record_ids: list[str] = Field(default_factory=list)
    update_only_record_ids: list[str] = Field(default_factory=list)
    insert_only_voucher_ids: list[str] = Field(default_factory=list)
    update_only_voucher_ids: list[str] = Field(default_factory=list)


class AssetBatch(BaseModel):
    account_set_id: str = "default"
    items: list[dict[str, Any]] = Field(default_factory=list)


class PhaseRows(BaseModel):
    account_set_id: str = "default"
    period: str
    rows: list[dict[str, Any]] = Field(default_factory=list)


class DepreciationCommit(BaseModel):
    account_set_id: str = "default"
    period: str
    rows: list[dict[str, Any]] = Field(default_factory=list)
    voucher: dict[str, Any]


@router.post("/commit")
def commit_business_and_vouchers(payload: BusinessCommit, conn: sqlite3.Connection = Depends(db)):
    """Atomically save normalized operating documents and their draft vouchers/entries."""
    _tenant(conn,payload.account_set_id)
    record_ids=[_str(r,"id") for r in payload.records]
    voucher_ids=[str(v.get("legacy_id") or "").strip() for v in payload.vouchers]
    if len(record_ids)!=len(set(record_ids)) or len(voucher_ids)!=len(set(voucher_ids)) or any(not x for x in voucher_ids):
        raise HTTPException(422,"业务单据或凭证编号为空或重复")
    if not set(payload.insert_only_record_ids).issubset(set(record_ids)) or not set(payload.insert_only_voucher_ids).issubset(set(voucher_ids)):
        raise HTTPException(422,"insert_only 编号不在本次提交中")
    if not set(payload.update_only_record_ids).issubset(set(record_ids)) or not set(payload.update_only_voucher_ids).issubset(set(voucher_ids)):
        raise HTTPException(422,"update_only 编号不在本次提交中")
    if set(payload.insert_only_record_ids)&set(payload.update_only_record_ids) or set(payload.insert_only_voucher_ids)&set(payload.update_only_voucher_ids):
        raise HTTPException(422,"同一记录不能同时设置 insert_only 和 update_only")
    prepared_vouchers=[]
    for raw in payload.vouchers:
        try: prepared_vouchers.append(VoucherCreate.model_validate(raw))
        except ValidationError as exc: raise HTTPException(422,detail=exc.errors()) from exc
    try:
        conn.execute("BEGIN IMMEDIATE")
        for record in sorted(payload.records,key=lambda x:(0 if x.get('id')=='inventory-opening' else 1 if str(x.get('id','')).startswith('inventory-doc:') else 2 if str(x.get('id','')).startswith('production-order:') else 3 if str(x.get('id','')).startswith('payroll-batch:') else 4)):
            legacy=str(record["id"])
            period=str(record.get("period") or str(record.get("date") or "")[:7])
            if period:
                validate_period(period)
                assert_period_unlocked(conn, period + "-01", payload.account_set_id)
            if legacy.startswith(('fw-return:', 'fw-refund:', 'fw-fx:')):
                book=payload.account_set_id
                if conn.execute("SELECT 1 FROM app_records WHERE account_set_id=? AND store_name='Workpapers' AND record_id=?", (book,legacy)).fetchone():
                    raise HTTPException(409,'业务记录已经保存')
                if legacy.startswith('fw-return:'):
                    validate_return(conn,book,record,prepared_vouchers,_assert_inventory_ready)
                else:
                    validate_auxiliary_voucher(conn,book,record,prepared_vouchers)
                conn.execute("INSERT INTO app_records(account_set_id,store_name,record_id,payload_json) VALUES(?,'Workpapers',?,?)", (book,legacy,json.dumps(record,ensure_ascii=False)))
                continue
            if legacy.startswith("ar:due:invoice:"):
                book = payload.account_set_id
                source = _str(record, "source")
                if not any(str(model.legacy_id) == "ar-voucher:" + legacy for model in prepared_vouchers):
                    raise HTTPException(422,"发票应收须与对应凭证一起保存")
                duplicate = conn.execute("SELECT 1 FROM business_documents WHERE account_set_id=? AND doc_type='SALE' AND invoice_legacy_id=?", (book, source)).fetchone()
                if duplicate:
                    raise HTTPException(409,"发票已关联销售出库")
                old = conn.execute("SELECT 1 FROM app_records WHERE account_set_id=? AND store_name='Workpapers' AND record_id=?", (book, legacy)).fetchone()
                if (old and legacy in payload.insert_only_record_ids) or (not old and legacy in payload.update_only_record_ids):
                    raise HTTPException(409,"发票应收记录已变化，请刷新")
                conn.execute("""INSERT INTO app_records(account_set_id,store_name,record_id,payload_json) VALUES(?,'Workpapers',?,?)
                    ON CONFLICT(account_set_id,store_name,record_id) DO UPDATE SET payload_json=excluded.payload_json""", (book,legacy,json.dumps(record,ensure_ascii=False)))
                continue
            exists=any(conn.execute(f"SELECT 1 FROM {table} WHERE account_set_id=? AND legacy_id=?",(payload.account_set_id,legacy)).fetchone() for table in ("inventory_openings","business_documents","business_settlements","production_orders","payroll_batches"))
            if legacy in payload.insert_only_record_ids and exists: raise HTTPException(409,f"业务记录编号已存在：{legacy}")
            if legacy in payload.update_only_record_ids and not exists: raise HTTPException(404,f"待更新业务记录不存在：{legacy}")
            _upsert_record(conn,payload.account_set_id,record,enforce_stock=True)
        saved_vouchers=[]
        for model in prepared_vouchers:
            voucher_date=model.voucher_date.isoformat()
            assert_period_unlocked(conn,voucher_date,payload.account_set_id)
            attachments=[validate_upload_url(url) for url in model.attachment_urls]
            entries,debit_total,credit_total=_validate_entry_payload(conn,model.entries,payload.account_set_id)
            legacy_id=str(model.legacy_id)
            existing=conn.execute("SELECT * FROM vouchers WHERE account_set_id=? AND legacy_id=?",(payload.account_set_id,legacy_id)).fetchone()
            if existing and legacy_id in payload.insert_only_voucher_ids:
                raise HTTPException(409,f"凭证编号已存在：{legacy_id}")
            if not existing and legacy_id in payload.update_only_voucher_ids:
                raise HTTPException(404,f"待更新凭证不存在：{legacy_id}")
            if existing:
                if existing["status"] not in ("DRAFT","PENDING") or existing["reviewed_at"]:
                    raise HTTPException(409,"已审核或已记账凭证不能通过业务提交接口修改")
                voucher_no=model.voucher_no or existing["voucher_no"]
                conn.execute("""UPDATE vouchers SET voucher_no=?,voucher_date=?,summary=?,status=?,attachment_urls=?,biz_label=?,source=?,source_type=?,counterparty=?,memo=?,attachment_name=?
                    WHERE account_set_id=? AND id=?""",(voucher_no,voucher_date,model.summary,model.status,json.dumps(attachments,ensure_ascii=False),model.biz_label,model.source,model.source_type,model.counterparty,model.memo,model.attachment_name,payload.account_set_id,existing["id"]))
                voucher_id=existing["id"]
                conn.execute("DELETE FROM voucher_entries WHERE account_set_id=? AND voucher_id=?",(payload.account_set_id,voucher_id))
            else:
                voucher_no=model.voucher_no or generate_voucher_no(conn,model.voucher_date,account_set_id=payload.account_set_id)
                cur=conn.execute("""INSERT INTO vouchers(account_set_id,legacy_id,voucher_no,voucher_date,summary,status,attachment_urls,biz_label,source,source_type,counterparty,memo,attachment_name)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(payload.account_set_id,legacy_id,voucher_no,voucher_date,model.summary,model.status,json.dumps(attachments,ensure_ascii=False),model.biz_label,model.source,model.source_type,model.counterparty,model.memo,model.attachment_name))
                voucher_id=cur.lastrowid
            _insert_entries(conn,payload.account_set_id,voucher_id,entries)
            conn.execute("UPDATE business_voucher_links SET voucher_id=? WHERE account_set_id=? AND voucher_legacy_id=?",(voucher_id,payload.account_set_id,legacy_id))
            row=conn.execute("SELECT * FROM vouchers WHERE account_set_id=? AND id=?",(payload.account_set_id,voucher_id)).fetchone()
            result=voucher_dict(conn,row);result["debit_total"]=cents_to_yuan(debit_total);result["credit_total"]=cents_to_yuan(credit_total)
            saved_vouchers.append(result)
        conn.commit()
    except HTTPException:
        conn.rollback(); raise
    except sqlite3.IntegrityError as exc:
        conn.rollback(); raise HTTPException(409,f"业务单据及凭证保存失败，事务已回滚：{exc}") from exc
    except sqlite3.Error as exc:
        conn.rollback(); raise HTTPException(500,f"业务单据及凭证保存失败，事务已回滚：{exc}") from exc
    return {"status":"committed","account_set_id":payload.account_set_id,"record_count":len(payload.records),"voucher_count":len(saved_vouchers),"records":_read_business_records(conn,payload.account_set_id),"vouchers":saved_vouchers}


@router.post("/migrate-from-local")
def migrate_business_from_local(account_set_id: str = Query(default="default"), conn: sqlite3.Connection = Depends(db)):
    _tenant(conn,account_set_id)
    counts={"inventory_openings":0,"inventory_documents":0,"settlements":0,"production_orders":0,"payroll_batches":0,"assets":0,"depreciation_runs":0,"payroll_drafts":0}
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows=conn.execute("SELECT store_name,record_id,payload_json FROM app_records WHERE account_set_id=? AND store_name IN ('Workpapers','Settings')",(account_set_id,)).fetchall()
        def migration_order(row):
            source=row["record_id"]
            if row["store_name"] == "Settings":
                key = source.removeprefix("finance-standard:operations:")
                rank = 0 if key == "assets" else 1 if key.startswith("asset-depr:") else 2 if key.startswith("payroll-draft:") else 9
                return (1, f"{rank}:{source}")
            if source == "inventory-opening": return (0, "0")
            if source.startswith("inventory-doc:"): return (0, "1"+source)
            if source.startswith("production-order:"): return (0, "2"+source)
            if source.startswith("payroll-batch:"): return (0, "3"+source)
            if source.startswith("biz-settlement:"): return (0, "4"+source)
            return (2, source)
        for row in sorted(rows, key=migration_order):
            module="workpapers" if row["store_name"]=="Workpapers" else "settings"
            source=row["record_id"]
            if conn.execute("SELECT 1 FROM business_migration_map WHERE account_set_id=? AND module=? AND source_id=?",(account_set_id,module,source)).fetchone(): continue
            try: payload=json.loads(row["payload_json"])
            except (TypeError,json.JSONDecodeError): continue
            if module=="workpapers":
                legacy=str(payload.get("id") or "")
                table="inventory_openings" if legacy=="inventory-opening" else "business_documents" if legacy.startswith("inventory-doc:") else "business_settlements" if legacy.startswith("biz-settlement:") else "production_orders" if legacy.startswith("production-order:") else "payroll_batches" if legacy.startswith("payroll-batch:") else None
                if table:
                    already=conn.execute(f"SELECT 1 FROM {table} WHERE account_set_id=? AND legacy_id=?",(account_set_id,legacy)).fetchone()
                    if not already:
                        _upsert_record(conn,account_set_id,payload)
                        counts["inventory_openings" if table=="inventory_openings" else "inventory_documents" if table=="business_documents" else "settlements" if table=="business_settlements" else "production_orders" if table=="production_orders" else "payroll_batches"]+=1
                    # Existing normalized data is authoritative over an older JSON snapshot.
                    conn.execute("INSERT INTO business_migration_map(account_set_id,module,source_id) VALUES(?,?,?)",(account_set_id,module,source))
            else:
                key=source.removeprefix("finance-standard:operations:")
                raw_value=payload.get("value") if isinstance(payload,dict) else payload
                if isinstance(raw_value,str):
                    try: raw_value=json.loads(raw_value)
                    except json.JSONDecodeError: raw_value=None
                if key=="assets":
                    assets=raw_value if isinstance(raw_value,list) else []
                    for a in assets:
                        code=str(a.get("code") or "")
                        if code and not conn.execute("SELECT 1 FROM fixed_assets WHERE account_set_id=? AND asset_code=?",(account_set_id,code)).fetchone():
                            _upsert_asset(conn,account_set_id,a);counts["assets"]+=1
                    conn.execute("INSERT INTO business_migration_map(account_set_id,module,source_id) VALUES(?,?,?)",(account_set_id,module,source))
                elif key.startswith("asset-depr:"):
                    period=key.split(":",1)[1]
                    if not conn.execute("SELECT 1 FROM depreciation_runs WHERE account_set_id=? AND period=?",(account_set_id,period)).fetchone():
                        _upsert_depreciation(conn,account_set_id,period,(raw_value or {}).get("rows",[]) if isinstance(raw_value,dict) else [])
                        counts["depreciation_runs"]+=1
                    conn.execute("INSERT INTO business_migration_map(account_set_id,module,source_id) VALUES(?,?,?)",(account_set_id,module,source))
                elif key.startswith("payroll-draft:"):
                    period=key.split(":",1)[1]
                    if not conn.execute("SELECT 1 FROM payroll_drafts WHERE account_set_id=? AND period=?",(account_set_id,period)).fetchone():
                        _upsert_payroll_draft(conn,account_set_id,period,raw_value if isinstance(raw_value,list) else [])
                        counts["payroll_drafts"]+=1
                    conn.execute("INSERT INTO business_migration_map(account_set_id,module,source_id) VALUES(?,?,?)",(account_set_id,module,source))
        conn.commit()
    except HTTPException:
        conn.rollback(); raise
    except sqlite3.Error as exc:
        conn.rollback(); raise HTTPException(500,f"业务数据迁移失败：{exc}") from exc
    return {"status":"migrated","account_set_id":account_set_id,"counts":counts}


@router.get("/records")
def list_business_records(account_set_id: str = Query(default="default"), conn: sqlite3.Connection = Depends(db)):
    _tenant(conn,account_set_id)
    return {"account_set_id":account_set_id,"items":_read_business_records(conn,account_set_id)}


@router.post("/records")
def save_business_records(payload: BusinessBatch, conn: sqlite3.Connection = Depends(db)):
    _tenant(conn,payload.account_set_id)
    ids=[_str(r,"id") for r in payload.records]
    if len(ids)!=len(set(ids)): raise HTTPException(422,"业务记录编号重复")
    insert_only=set(payload.insert_only_ids)
    if not insert_only.issubset(set(ids)): raise HTTPException(422,"insert_only_ids 不在本次提交记录中")
    try:
        conn.execute("BEGIN IMMEDIATE")
        if payload.replace_normalized:
            posted_links=conn.execute("SELECT COUNT(*) AS n FROM business_voucher_links l JOIN vouchers v ON v.account_set_id=l.account_set_id AND v.id=l.voucher_id WHERE l.account_set_id=? AND v.status IN ('POSTED','REVERSED','VOIDED')",(payload.account_set_id,)).fetchone()["n"]
            if posted_links: raise HTTPException(409,"存在已记账业务单据，不能通过整表替换删除；请使用逐笔业务处理或账套重置")
            for table in ("business_settlements","business_documents","inventory_openings","production_orders","payroll_batches"):
                conn.execute(f"DELETE FROM {table} WHERE account_set_id=?",(payload.account_set_id,))
            conn.execute("DELETE FROM inventory_items WHERE account_set_id=?",(payload.account_set_id,))
            conn.execute("DELETE FROM business_voucher_links WHERE account_set_id=? AND source_module IN ('inventory','settlement','production','payroll')",(payload.account_set_id,))
            conn.execute("DELETE FROM business_migration_map WHERE account_set_id=? AND module='workpapers'",(payload.account_set_id,))
        for r in sorted(payload.records,key=lambda x:(0 if x.get('id')=='inventory-opening' else 1 if str(x.get('id','')).startswith('inventory-doc:') else 2 if str(x.get('id','')).startswith('production-order:') else 3 if str(x.get('id','')).startswith('payroll-batch:') else 4)):
            legacy=str(r.get("id") or "")
            if legacy.startswith(("inventory-doc:","biz-settlement:","production-order:","payroll-batch:")) or legacy=="inventory-opening":
                period = str(r.get("period") or str(r.get("date") or "")[:7])
                if period:
                    locked=conn.execute("SELECT is_locked FROM accounting_periods WHERE account_set_id=? AND period=?",(payload.account_set_id,period)).fetchone()
                    if locked and locked["is_locked"]: raise HTTPException(409,f"会计期间 {period} 已锁定，不能新增或修改业务单据")
            existing=conn.execute("SELECT 1 FROM business_migration_map WHERE account_set_id=? AND module='live' AND source_id=?",(payload.account_set_id,str(r["id"]))).fetchone()
            # Existence is checked against all normalized parent record tables, not a JSON blob.
            exists=any(conn.execute(f"SELECT 1 FROM {table} WHERE account_set_id=? AND legacy_id=?",(payload.account_set_id,str(r["id"]))).fetchone() for table in ("inventory_openings","business_documents","business_settlements","production_orders","payroll_batches"))
            if str(r["id"]) in insert_only and exists: raise HTTPException(409,f"业务记录编号已存在：{r['id']}")
            _upsert_record(conn,payload.account_set_id,r,enforce_stock=not (payload.replace_normalized or payload.skip_stock_validation))
        conn.commit()
    except HTTPException:
        conn.rollback(); raise
    except sqlite3.Error as exc:
        conn.rollback(); raise HTTPException(500,f"业务数据保存失败：{exc}") from exc
    return {"status":"saved","count":len(payload.records),"items":_read_business_records(conn,payload.account_set_id)}


@router.delete("/records")
def delete_business_records(account_set_id: str = Query(default="default"), payload: dict = Body(...), conn: sqlite3.Connection = Depends(db)):
    _tenant(conn,account_set_id)
    ids=payload.get("ids") or []
    if payload.get("clear_all") is True:
        ids=[]
        for table in ("inventory_openings","business_documents","business_settlements","production_orders","payroll_batches"):
            ids.extend(r["legacy_id"] for r in conn.execute(f"SELECT legacy_id FROM {table} WHERE account_set_id=?",(account_set_id,)))
    if not isinstance(ids,list) or any(not isinstance(x,(str,int)) for x in ids): raise HTTPException(422,"ids 必须为字符串数组")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for legacy in map(str,ids):
            posted=conn.execute("""SELECT COUNT(*) AS n FROM business_voucher_links l JOIN vouchers v
                ON v.account_set_id=l.account_set_id AND v.id=l.voucher_id
                WHERE l.account_set_id=? AND l.source_legacy_id=? AND v.status IN ('POSTED','REVERSED','VOIDED')""",(account_set_id,legacy)).fetchone()["n"]
            if posted: raise HTTPException(409,"该业务记录已关联已审核/已记账凭证，不能直接删除")
            opening=conn.execute("SELECT period FROM inventory_openings WHERE account_set_id=? AND legacy_id=?",(account_set_id,legacy)).fetchone()
            if opening and conn.execute("SELECT 1 FROM business_documents WHERE account_set_id=? AND doc_date>=? LIMIT 1",(account_set_id,opening["period"]+"-01")).fetchone():
                raise HTTPException(409,"库存期初已被后续库存业务引用，不能直接删除")
            order=conn.execute("SELECT 1 FROM production_orders WHERE account_set_id=? AND legacy_id=?",(account_set_id,legacy)).fetchone()
            if order and conn.execute("SELECT 1 FROM business_documents WHERE account_set_id=? AND production_order_legacy_id=? LIMIT 1",(account_set_id,legacy)).fetchone():
                raise HTTPException(409,"生产订单仍有关联领料/完工单据，不能直接删除")
            for table in ("business_settlements","business_documents","inventory_openings","production_orders","payroll_batches"):
                conn.execute(f"DELETE FROM {table} WHERE account_set_id=? AND legacy_id=?",(account_set_id,legacy))
            conn.execute("DELETE FROM business_voucher_links WHERE account_set_id=? AND source_legacy_id=?",(account_set_id,legacy))
            conn.execute("DELETE FROM business_migration_map WHERE account_set_id=? AND source_id=?",(account_set_id,legacy))
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback(); raise HTTPException(409,f"业务记录仍被其他业务引用，不能删除：{exc}") from exc
    return {"status":"deleted","count":len(ids)}


@router.post("/link-voucher")
def link_business_voucher(account_set_id: str = Query(default="default"), payload: dict = Body(...), conn: sqlite3.Connection = Depends(db)):
    _tenant(conn,account_set_id)
    legacy=str(payload.get("voucher_legacy_id") or "").strip()
    if not legacy: raise HTTPException(422,"缺少 voucher_legacy_id")
    voucher=conn.execute("SELECT id FROM vouchers WHERE account_set_id=? AND legacy_id=?",(account_set_id,legacy)).fetchone()
    if not voucher: raise HTTPException(404,"规范化凭证不存在")
    cursor=conn.execute("UPDATE business_voucher_links SET voucher_id=? WHERE account_set_id=? AND voucher_legacy_id=?",(voucher["id"],account_set_id,legacy))
    link_count=cursor.rowcount
    conn.execute("UPDATE depreciation_runs SET voucher_id=? WHERE account_set_id=? AND voucher_legacy_id=?",(voucher["id"],account_set_id,legacy))
    conn.commit()
    return {"status":"linked","voucher_id":voucher["id"],"link_count":link_count}


@router.get("/bootstrap")
def business_bootstrap(account_set_id: str = Query(default="default"), conn: sqlite3.Connection = Depends(db)):
    _tenant(conn,account_set_id)
    assets=[]
    for a in conn.execute("SELECT * FROM fixed_assets WHERE account_set_id=? ORDER BY asset_code",(account_set_id,)):
        assets.append({"code":a["asset_code"],"name":a["name"],"type":a["asset_type"],"cost":a["cost_cents"],"residual":float(a["residual_percent"]),"years":float(a["useful_life_years"]),"start":a["start_date"],"dept":a["department"],"place":a["location"],"keeper":a["keeper"],"qty":a["quantity_milli"]/1000,"active":bool(a["is_active"])})
    depreciation=[]
    for run in conn.execute("SELECT * FROM depreciation_runs WHERE account_set_id=? ORDER BY period",(account_set_id,)):
        lines=conn.execute("""SELECT d.*,a.name,a.asset_type,a.cost_cents,a.residual_percent,a.useful_life_years,a.start_date,a.department,a.location,a.keeper,a.quantity_milli,a.is_active
            FROM depreciation_lines d JOIN fixed_assets a ON a.account_set_id=d.account_set_id AND a.asset_code=d.asset_code
            WHERE d.account_set_id=? AND d.run_id=? ORDER BY d.asset_code""",(account_set_id,run["id"])).fetchall()
        depreciation.append({"period":run["period"],"rows":[{"code":x["asset_code"],"name":x["name"],"type":x["asset_type"],"cost":x["cost_cents"],"residual":float(x["residual_percent"]),"years":float(x["useful_life_years"]),"start":x["start_date"],"dept":x["department"],"place":x["location"],"keeper":x["keeper"],"qty":x["quantity_milli"]/1000,"active":bool(x["is_active"]),"amount":x["amount_cents"],"cumulative":x["cumulative_cents"],"net":x["net_cents"]} for x in lines]})
    drafts=[]
    for d in conn.execute("SELECT * FROM payroll_drafts WHERE account_set_id=? ORDER BY period",(account_set_id,)):
        lines=conn.execute("SELECT * FROM payroll_draft_lines WHERE account_set_id=? AND payroll_draft_id=? ORDER BY row_no",(account_set_id,d["id"])).fetchall()
        drafts.append({"period":d["period"],"rows":[_line_dict(x) for x in lines]})
    return {"account_set_id":account_set_id,"assets":assets,"depreciation":depreciation,"payroll_drafts":drafts,"records":_read_business_records(conn,account_set_id)}


@router.put("/assets")
def save_assets(payload: AssetBatch, conn: sqlite3.Connection = Depends(db)):
    _tenant(conn,payload.account_set_id)
    codes=[_str(x,"code") for x in payload.items]
    if len(codes)!=len(set(codes)): raise HTTPException(422,"固定资产编号重复")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for a in payload.items: _upsert_asset(conn,payload.account_set_id,a)
        if payload.items:
            placeholders=','.join('?' for _ in codes)
            conn.execute(f"DELETE FROM fixed_assets WHERE account_set_id=? AND asset_code NOT IN ({placeholders})",(payload.account_set_id,*codes))
        else:
            conn.execute("DELETE FROM fixed_assets WHERE account_set_id=?",(payload.account_set_id,))
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback(); raise HTTPException(409,f"资产仍有关联折旧记录，不能删除：{exc}") from exc
    return {"status":"saved","count":len(payload.items)}


@router.put("/depreciation/{period}")
def save_depreciation(period: str, payload: PhaseRows, conn: sqlite3.Connection = Depends(db)):
    _tenant(conn,payload.account_set_id)
    if validate_period(period)!=validate_period(payload.period): raise HTTPException(422,"路径期间与请求期间不一致")
    try:
        conn.execute("BEGIN IMMEDIATE")
        locked=conn.execute("SELECT is_locked FROM accounting_periods WHERE account_set_id=? AND period=?",(payload.account_set_id,period)).fetchone()
        if locked and locked["is_locked"]: raise HTTPException(409,f"会计期间 {period} 已锁定")
        _upsert_depreciation(conn,payload.account_set_id,period,payload.rows); conn.commit()
    except HTTPException: conn.rollback(); raise
    except sqlite3.Error as exc: conn.rollback(); raise HTTPException(500,f"折旧保存失败：{exc}") from exc
    return {"status":"saved","period":period,"count":len(payload.rows)}


@router.post("/depreciation/{period}/commit")
def commit_depreciation_and_voucher(period: str, payload: DepreciationCommit, conn: sqlite3.Connection = Depends(db)):
    _tenant(conn,payload.account_set_id)
    period=validate_period(period)
    if validate_period(payload.period)!=period: raise HTTPException(422,"路径期间与请求期间不一致")
    try: model=VoucherCreate.model_validate(payload.voucher)
    except ValidationError as exc: raise HTTPException(422,detail=exc.errors()) from exc
    if model.voucher_date.isoformat()[:7]!=period: raise HTTPException(422,"折旧凭证日期必须属于计提期间")
    legacy_id=str(model.legacy_id or "").strip()
    if not legacy_id: raise HTTPException(422,"折旧凭证缺少 legacy_id")
    try:
        conn.execute("BEGIN IMMEDIATE")
        locked=conn.execute("SELECT is_locked FROM accounting_periods WHERE account_set_id=? AND period=?",(payload.account_set_id,period)).fetchone()
        if locked and locked["is_locked"]: raise HTTPException(409,f"会计期间 {period} 已锁定")
        existing_run=conn.execute("SELECT * FROM depreciation_runs WHERE account_set_id=? AND period=?",(payload.account_set_id,period)).fetchone()
        if not existing_run: raise HTTPException(409,"请先保存本期固定资产折旧计提")
        if existing_run["voucher_id"]: raise HTTPException(409,"本期折旧凭证已经关联")
        existing_voucher=conn.execute("SELECT 1 FROM vouchers WHERE account_set_id=? AND legacy_id=?",(payload.account_set_id,legacy_id)).fetchone()
        if existing_voucher: raise HTTPException(409,"折旧凭证编号已存在")
        _upsert_depreciation(conn,payload.account_set_id,period,payload.rows)
        assert_period_unlocked(conn,model.voucher_date.isoformat(),payload.account_set_id)
        attachments=[validate_upload_url(url) for url in model.attachment_urls]
        entries,debit_total,credit_total=_validate_entry_payload(conn,model.entries,payload.account_set_id)
        voucher_no=model.voucher_no or generate_voucher_no(conn,model.voucher_date,account_set_id=payload.account_set_id)
        cur=conn.execute("""INSERT INTO vouchers(account_set_id,legacy_id,voucher_no,voucher_date,summary,status,attachment_urls,biz_label,source,source_type,counterparty,memo,attachment_name)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",(payload.account_set_id,legacy_id,voucher_no,model.voucher_date.isoformat(),model.summary,model.status,json.dumps(attachments,ensure_ascii=False),model.biz_label,model.source,model.source_type,model.counterparty,model.memo,model.attachment_name))
        voucher_id=cur.lastrowid
        _insert_entries(conn,payload.account_set_id,voucher_id,entries)
        conn.execute("UPDATE depreciation_runs SET voucher_legacy_id=?,voucher_id=? WHERE account_set_id=? AND period=?",(legacy_id,voucher_id,payload.account_set_id,period))
        conn.execute("INSERT INTO business_voucher_links(account_set_id,source_module,source_legacy_id,role,voucher_legacy_id,voucher_id) VALUES(?,?,?,?,?,?)",(payload.account_set_id,"depreciation",period,"PRIMARY",legacy_id,voucher_id))
        row=conn.execute("SELECT * FROM vouchers WHERE account_set_id=? AND id=?",(payload.account_set_id,voucher_id)).fetchone()
        result=voucher_dict(conn,row);result["debit_total"]=cents_to_yuan(debit_total);result["credit_total"]=cents_to_yuan(credit_total)
        conn.commit()
    except HTTPException:
        conn.rollback();raise
    except sqlite3.IntegrityError as exc:
        conn.rollback();raise HTTPException(409,f"折旧及凭证保存失败，事务已回滚：{exc}") from exc
    except sqlite3.Error as exc:
        conn.rollback();raise HTTPException(500,f"折旧及凭证保存失败，事务已回滚：{exc}") from exc
    return {"status":"committed","period":period,"voucher":result}


@router.delete("/depreciation/{period}")
def delete_depreciation(period: str, account_set_id: str = Query(default="default"), conn: sqlite3.Connection = Depends(db)):
    _tenant(conn,account_set_id); validate_period(period)
    run=conn.execute("SELECT voucher_id FROM depreciation_runs WHERE account_set_id=? AND period=?",(account_set_id,period)).fetchone()
    if run and run["voucher_id"]:
        voucher=conn.execute("SELECT status FROM vouchers WHERE account_set_id=? AND id=?",(account_set_id,run["voucher_id"])).fetchone()
        if voucher and voucher["status"] in ("POSTED","REVERSED","VOIDED"): raise HTTPException(409,"本期折旧凭证已审核/记账/冲销，不能直接撤销折旧记录")
        raise HTTPException(409,"请先删除本期未记账折旧凭证，再撤销折旧计提")
    conn.execute("DELETE FROM depreciation_runs WHERE account_set_id=? AND period=?",(account_set_id,period)); conn.commit()
    return {"status":"deleted","period":period}


@router.put("/payroll-drafts/{period}")
def save_payroll_draft(period: str, payload: PhaseRows, conn: sqlite3.Connection = Depends(db)):
    _tenant(conn,payload.account_set_id)
    if validate_period(period)!=validate_period(payload.period): raise HTTPException(422,"路径期间与请求期间不一致")
    try:
        conn.execute("BEGIN IMMEDIATE")
        locked=conn.execute("SELECT is_locked FROM accounting_periods WHERE account_set_id=? AND period=?",(payload.account_set_id,period)).fetchone()
        if locked and locked["is_locked"]: raise HTTPException(409,f"会计期间 {period} 已锁定")
        _upsert_payroll_draft(conn,payload.account_set_id,period,payload.rows); conn.commit()
    except HTTPException: conn.rollback(); raise
    except sqlite3.Error as exc: conn.rollback(); raise HTTPException(500,f"工资草稿保存失败：{exc}") from exc
    return {"status":"saved","period":period,"count":len(payload.rows)}


@router.delete("/payroll-drafts/{period}")
def delete_payroll_draft(period: str, account_set_id: str = Query(default="default"), conn: sqlite3.Connection = Depends(db)):
    _tenant(conn,account_set_id); validate_period(period)
    conn.execute("DELETE FROM payroll_drafts WHERE account_set_id=? AND period=?",(account_set_id,period)); conn.commit()
    return {"status":"deleted","period":period}


@router.post("/reset")
def reset_business_data(account_set_id: str = Query(default="default"), conn: sqlite3.Connection = Depends(db)):
    _tenant(conn,account_set_id)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for table in ("business_settlements","business_documents","inventory_openings","production_orders","payroll_batches","depreciation_runs","payroll_drafts","fixed_assets","inventory_items"):
            conn.execute(f"DELETE FROM {table} WHERE account_set_id=?",(account_set_id,))
        conn.execute("DELETE FROM business_voucher_links WHERE account_set_id=?",(account_set_id,))
        conn.execute("DELETE FROM business_migration_map WHERE account_set_id=?",(account_set_id,))
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback(); raise HTTPException(500,f"清理规范化业务数据失败：{exc}") from exc
    return {"status":"reset","account_set_id":account_set_id}


@router.get("/inventory-ledger")
def inventory_ledger(account_set_id: str = Query(default="default"), period: str | None = None, sku: str | None = None, conn: sqlite3.Connection = Depends(db)):
    _tenant(conn,account_set_id)
    params=[account_set_id]; where="m.account_set_id=?"
    if period:
        p=validate_period(period); where+=" AND m.movement_date < ?"; params.append(f"{p}-" + "32")
    if sku: where+=" AND m.sku=?"; params.append(sku)
    rows=conn.execute(f"""SELECT m.movement_date,m.movement_type,m.sku,i.name,m.warehouse,m.quantity_milli,m.unit_cost_cents,m.amount_cents,m.voucher_legacy_id
        FROM inventory_movements m JOIN inventory_items i ON i.account_set_id=m.account_set_id AND i.sku=m.sku AND i.inventory_account_code=m.inventory_account_code
        WHERE {where} ORDER BY m.movement_date,m.id""",params).fetchall()
    return {"account_set_id":account_set_id,"period":period,"items":[{**dict(x),"quantity":x["quantity_milli"]/1000,"amount_yuan":f"{x['amount_cents']/100:.2f}"} for x in rows]}


@router.get("/inventory-balance")
def inventory_balance(account_set_id: str = Query(default="default"), period: str = Query(...), conn: sqlite3.Connection = Depends(db)):
    """SQL inventory quantity/value balance: verified opening plus movements whose linked voucher is posted."""
    import calendar
    _tenant(conn,account_set_id); period=validate_period(period)
    last_day=calendar.monthrange(int(period[:4]),int(period[5:7]))[1]
    end_date=f"{period}-{last_day:02d}"
    opening=conn.execute("SELECT * FROM inventory_openings WHERE account_set_id=? AND verified=1 AND period<=? ORDER BY period DESC,id DESC LIMIT 1",(account_set_id,period)).fetchone()
    if not opening:
        return {"account_set_id":account_set_id,"period":period,"opening_period":None,"items":[],"message":"未找到已核实的库存期初"}
    start_date=opening["period"]+"-01"
    rows=conn.execute("""WITH opening_rows AS (
        SELECT inventory_account_code AS account_code,sku,warehouse,SUM(quantity_milli) AS qty,SUM(stock_cost_cents) AS value
        FROM inventory_opening_lines WHERE account_set_id=? AND opening_id=? GROUP BY inventory_account_code,sku,warehouse
      ), movement_rows AS (
        SELECT m.inventory_account_code AS account_code,m.sku,m.warehouse,SUM(m.quantity_milli) AS qty,SUM(m.amount_cents) AS value
        FROM inventory_movements m JOIN business_documents d ON d.account_set_id=m.account_set_id AND d.id=m.document_id
        JOIN vouchers v ON v.account_set_id=m.account_set_id AND v.legacy_id=m.voucher_legacy_id
        WHERE m.account_set_id=? AND m.movement_date>=? AND m.movement_date<=? AND v.status='POSTED'
        GROUP BY m.inventory_account_code,m.sku,m.warehouse
      ), combined AS (
        SELECT account_code,sku,warehouse,qty,value FROM opening_rows
        UNION ALL SELECT account_code,sku,warehouse,qty,value FROM movement_rows
        UNION ALL SELECT json_extract(a.payload_json,'$.inventoryAccount'),json_extract(a.payload_json,'$.sku'),json_extract(a.payload_json,'$.warehouse'),
          (CASE json_extract(a.payload_json,'$.kind') WHEN 'SALE_RETURN' THEN 1 ELSE -1 END)*json_extract(a.payload_json,'$.qtyMilli'),
          (CASE json_extract(a.payload_json,'$.kind') WHEN 'SALE_RETURN' THEN 1 ELSE -1 END)*json_extract(a.payload_json,'$.stockCostCents')
        FROM app_records a JOIN vouchers v ON v.account_set_id=a.account_set_id AND v.legacy_id=json_extract(a.payload_json,'$.voucherId')
        WHERE a.account_set_id=? AND a.store_name='Workpapers' AND a.record_id LIKE 'fw-return:%' AND v.status='POSTED' AND v.voucher_date BETWEEN ? AND ?
      )
      SELECT c.account_code,c.sku,i.name,c.warehouse,SUM(c.qty) AS quantity_milli,SUM(c.value) AS amount_cents
      FROM combined c JOIN inventory_items i ON i.account_set_id=? AND i.sku=c.sku AND i.inventory_account_code=c.account_code
      GROUP BY c.account_code,c.sku,i.name,c.warehouse ORDER BY c.account_code,c.sku,c.warehouse""",
      (account_set_id,opening["id"],account_set_id,start_date,end_date,account_set_id,start_date,end_date,account_set_id)).fetchall()
    items=[]
    for row in rows:
        qty=int(row["quantity_milli"] or 0); amount=int(row["amount_cents"] or 0)
        items.append({**dict(row),"quantity":qty/1000,"amount_yuan":f"{amount/100:.2f}","average_unit_cost_cents":(amount*1000//qty if qty else 0),"is_negative":qty<0 or amount<0})
    return {"account_set_id":account_set_id,"period":period,"opening_period":opening["period"],"items":items,
            "totals":{"quantity_milli":sum(x["quantity_milli"] for x in rows),"amount_cents":sum(x["amount_cents"] for x in rows)}}
