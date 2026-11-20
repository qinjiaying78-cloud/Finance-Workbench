from __future__ import annotations
import json, sqlite3
from fastapi import APIRouter, Depends, HTTPException, Query
from ..database import get_connection
from ..schemas import InvoiceCreate, InvoiceUpdate
from .common import assert_account_set, cents_to_yuan, money_to_cents, validate_upload_url

router=APIRouter(prefix="/api/invoices",tags=["发票管理"])
def get_db():
    conn=get_connection()
    try: yield conn
    finally: conn.close()
def _invoice_dict(row:sqlite3.Row)->dict:
    item=dict(row); item["amount"]=cents_to_yuan(item["amount"]); item["tax_amount"]=cents_to_yuan(item["tax_amount"])
    try: item["items"]=json.loads(item.pop("items_json") or "[]")
    except (json.JSONDecodeError,TypeError): item["items"]=[]
    try: item["extra"]=json.loads(item.pop("extra_json") or "{}")
    except (json.JSONDecodeError,TypeError): item["extra"]={}
    return item

@router.get("")
def list_invoices(start_date:str|None=None,end_date:str|None=None,invoice_no:str|None=None,buyer_name:str|None=None,status:str|None=None,limit:int=Query(200,ge=1,le=1000),offset:int=Query(0,ge=0),account_set_id:str=Query("default"),conn:sqlite3.Connection=Depends(get_db)):
    assert_account_set(conn,account_set_id); where=["account_set_id=?"]; params:list[object]=[account_set_id]
    if start_date: where.append("invoice_date>=?"); params.append(start_date)
    if end_date: where.append("invoice_date<=?"); params.append(end_date)
    if invoice_no: where.append("invoice_no LIKE ?"); params.append(f"%{invoice_no.strip()}%")
    if buyer_name: where.append("buyer_name LIKE ?"); params.append(f"%{buyer_name.strip()}%")
    if status: where.append("status=?"); params.append(status)
    rows=conn.execute("SELECT * FROM invoices WHERE "+" AND ".join(where)+" ORDER BY invoice_date DESC,id DESC LIMIT ? OFFSET ?",(*params,limit,offset)).fetchall()
    return {"account_set_id":account_set_id,"items":[_invoice_dict(r) for r in rows],"limit":limit,"offset":offset}

@router.post("",status_code=201)
def create_invoice(payload:InvoiceCreate,account_set_id:str=Query("default"),conn:sqlite3.Connection=Depends(get_db)):
    assert_account_set(conn,account_set_id); url=validate_upload_url(payload.pdf_or_img_url); amount=money_to_cents(payload.amount); tax=money_to_cents(payload.tax_amount)
    try:
        cur=conn.execute("INSERT INTO invoices(account_set_id,legacy_id,source,extra_json,invoice_code,invoice_no,invoice_date,buyer_name,buyer_tax_id,seller_name,amount,tax_amount,payment_method,status,pdf_or_img_url,items_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(account_set_id,payload.legacy_id,payload.source,json.dumps(payload.extra,ensure_ascii=False),payload.invoice_code,payload.invoice_no,payload.invoice_date.isoformat(),payload.buyer_name,payload.buyer_tax_id,payload.seller_name,amount,tax,payload.payment_method,payload.status,url,json.dumps(payload.items,ensure_ascii=False)))
        conn.commit()
    except sqlite3.IntegrityError as exc: conn.rollback(); raise HTTPException(409,f"发票保存失败：{exc}") from exc
    return _invoice_dict(conn.execute("SELECT * FROM invoices WHERE account_set_id=? AND id=?",(account_set_id,cur.lastrowid)).fetchone())

@router.put("/{invoice_id}")
def update_invoice(invoice_id:int,payload:InvoiceUpdate,account_set_id:str=Query("default"),conn:sqlite3.Connection=Depends(get_db)):
    assert_account_set(conn,account_set_id); changes=payload.model_dump(exclude_unset=True)
    if not changes: raise HTTPException(422,"没有提供需要更新的字段")
    row=conn.execute("SELECT * FROM invoices WHERE account_set_id=? AND id=?",(account_set_id,invoice_id)).fetchone()
    if not row: raise HTTPException(404,"发票记录不存在")
    if "amount" in changes and changes["amount"] is not None: changes["amount"]=money_to_cents(changes["amount"])
    if "tax_amount" in changes and changes["tax_amount"] is not None: changes["tax_amount"]=money_to_cents(changes["tax_amount"])
    if "invoice_date" in changes and changes["invoice_date"] is not None: changes["invoice_date"]=changes["invoice_date"].isoformat()
    if "pdf_or_img_url" in changes: changes["pdf_or_img_url"]=validate_upload_url(changes["pdf_or_img_url"])
    if "items" in changes: changes["items_json"]=json.dumps(changes.pop("items") or [],ensure_ascii=False)
    if "extra" in changes: changes["extra_json"]=json.dumps(changes.pop("extra") or {},ensure_ascii=False)
    allowed={"invoice_code","invoice_no","invoice_date","buyer_name","buyer_tax_id","seller_name","amount","tax_amount","payment_method","status","pdf_or_img_url","source","items_json","extra_json"}
    changes={k:v for k,v in changes.items() if k in allowed}
    clause=",".join(f"{k}=?" for k in changes)
    try: conn.execute(f"UPDATE invoices SET {clause} WHERE account_set_id=? AND id=?",(*changes.values(),account_set_id,invoice_id)); conn.commit()
    except sqlite3.IntegrityError as exc: conn.rollback(); raise HTTPException(409,f"发票更新失败：{exc}") from exc
    return _invoice_dict(conn.execute("SELECT * FROM invoices WHERE account_set_id=? AND id=?",(account_set_id,invoice_id)).fetchone())

@router.delete("/{invoice_id}")
def delete_invoice(invoice_id:int,account_set_id:str=Query("default"),conn:sqlite3.Connection=Depends(get_db)):
    assert_account_set(conn,account_set_id); cur=conn.execute("DELETE FROM invoices WHERE account_set_id=? AND id=?",(account_set_id,invoice_id)); conn.commit()
    if not cur.rowcount: raise HTTPException(404,"发票记录不存在")
    return {"deleted":True,"id":invoice_id}
