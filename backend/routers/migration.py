"""Idempotent tenant-scoped import from browser JSON/app_records to normalized tables."""
from __future__ import annotations
import json, re, sqlite3
from datetime import date
from decimal import Decimal
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from ..database import get_connection, ensure_default_accounts
from .common import money_to_cents
router=APIRouter(prefix="/api",tags=["规范化数据迁移"])
class MigrationPayload(BaseModel):
    account_set_id:str=Field(default="default",min_length=1,max_length=120)
    account_set_name:str|None=None
    accounts:list[dict[str,Any]]=Field(default_factory=list)
    contacts:list[dict[str,Any]]=Field(default_factory=list)
    vouchers:list[dict[str,Any]]=Field(default_factory=list)
    cashier_records:list[dict[str,Any]]=Field(default_factory=list)
    invoices:list[dict[str,Any]]=Field(default_factory=list)
def db():
    c=get_connection()
    try: yield c
    finally: c.close()
def amount(v):
    if v in (None,""): return 0
    try: return money_to_cents(Decimal(str(v)))
    except Exception as exc: raise HTTPException(422,detail=f"历史数据金额无效：{v}") from exc
def sid(row,fallback): return str(row.get("id") or row.get("invoiceNo") or row.get("voucherNo") or fallback)
def exists(c,b,m,s): return c.execute("SELECT target_id FROM normalized_migration_map WHERE account_set_id=? AND module=? AND source_id=?",(b,m,s)).fetchone()
def mark(c,b,m,s,t): c.execute("INSERT OR IGNORE INTO normalized_migration_map(account_set_id,module,source_id,target_id) VALUES(?,?,?,?)",(b,m,s,t))
def valid_book(book):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,119}",book or ""): raise HTTPException(422,"账套 ID 格式无效")


@router.get("/migrate-from-local/status")
def normalized_migration_status(account_set_id: str = "default", c: sqlite3.Connection = Depends(db)):
    valid_book(account_set_id)
    if not c.execute("SELECT 1 FROM account_sets WHERE id=?",(account_set_id,)).fetchone():
        raise HTTPException(404, f"账套不存在：{account_set_id}")
    vouchers=[r[0] for r in c.execute("SELECT source_id FROM normalized_migration_map WHERE account_set_id=? AND module='vouchers'",(account_set_id,)).fetchall()]
    cashier=[r[0] for r in c.execute("SELECT source_id FROM normalized_migration_map WHERE account_set_id=? AND module='cashier-v9-sync'",(account_set_id,)).fetchall()]
    invoices=[r[0] for r in c.execute("SELECT source_id FROM normalized_migration_map WHERE account_set_id=? AND module='invoices-v9-sync'",(account_set_id,)).fetchall()]
    return {"account_set_id":account_set_id,"voucher_synced_ids":vouchers,"cashier_synced_ids":cashier,"invoice_synced_ids":invoices}

@router.post("/migrate-from-local")
def migrate_from_local(p:MigrationPayload,c:sqlite3.Connection=Depends(db)):
    b=p.account_set_id; valid_book(b)
    counts={"accounts":0,"contacts":0,"vouchers":0,"cashier_records":0,"invoices":0,"skipped":0}
    try:
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO account_sets(id,name) VALUES(?,?) ON CONFLICT(id) DO NOTHING",(b,p.account_set_name or b))
        ensure_default_accounts(c, b)
        for i,a in enumerate(p.accounts):
            code=str(a.get("code") or a.get("account_code") or "").strip(); name=str(a.get("name") or a.get("account_name") or "").strip()
            if not code or not name: counts["skipped"]+=1; continue
            s=code
            if exists(c,b,"accounts",s): counts["skipped"]+=1; continue
            debit,credit=amount(a.get("initial_debit")),amount(a.get("initial_credit"))
            if debit and credit: raise HTTPException(422,f"科目 {code} 期初借贷不能同时有余额")
            c.execute("""INSERT INTO accounts(account_set_id,code,name,category,direction,parent_code,initial_debit,initial_credit,is_active)
                VALUES(?,?,?,?,?,NULL,?,?,?) ON CONFLICT(account_set_id,code) DO UPDATE SET name=excluded.name,
                initial_debit=CASE WHEN accounts.initial_debit=0 AND accounts.initial_credit=0 THEN excluded.initial_debit ELSE accounts.initial_debit END,
                initial_credit=CASE WHEN accounts.initial_debit=0 AND accounts.initial_credit=0 THEN excluded.initial_credit ELSE accounts.initial_credit END""",
                (b,code,name,a.get("category") or "资产",a.get("direction") or "借",debit,credit,int(a.get("is_active",True))))
            target=c.execute("SELECT id FROM accounts WHERE account_set_id=? AND code=?",(b,code)).fetchone()[0]; mark(c,b,"accounts",s,target); counts["accounts"]+=1
        # Parent codes are resolved in a second pass so source ordering does not
        # matter (a child may appear before its parent in an exported chart).
        for a in p.accounts:
            code=str(a.get("code") or a.get("account_code") or "").strip()
            parent=str(a.get("parent_code") or "").strip() or None
            if code and parent:
                if not c.execute("SELECT 1 FROM accounts WHERE account_set_id=? AND code=?",(b,parent)).fetchone():
                    raise HTTPException(422,detail=f"科目 {code} 的上级科目 {parent} 不存在于当前账套")
                c.execute("UPDATE accounts SET parent_code=? WHERE account_set_id=? AND code=?",(parent,b,code))
        for i,x in enumerate(p.contacts):
            name=str(x.get("name") or x.get("contact_name") or "").strip(); typ=x.get("type") or "CUSTOMER"
            if not name: counts["skipped"]+=1; continue
            s=sid(x,f"{name}:{typ}")
            if exists(c,b,"contacts",s): counts["skipped"]+=1; continue
            found=c.execute("SELECT id FROM contacts WHERE account_set_id=? AND name=? AND type=? ORDER BY id LIMIT 1",(b,name,typ)).fetchone()
            if found: target=found[0]
            else: target=c.execute("INSERT INTO contacts(account_set_id,name,type,tax_id,contact_info) VALUES(?,?,?,?,?)",(b,name,typ,x.get("tax_id"),x.get("contact_info"))).lastrowid
            mark(c,b,"contacts",s,target); counts["contacts"]+=1
        for i,v in enumerate(p.vouchers):
            s=sid(v,f"voucher-{i}")
            if exists(c,b,"vouchers",s): counts["skipped"]+=1; continue
            no=str(v.get("voucherNo") or v.get("voucher_no") or s); vd=str(v.get("date") or v.get("voucher_date") or "")[:10]
            try: date.fromisoformat(vd)
            except ValueError: raise HTTPException(422,f"凭证 {no} 日期无效")
            raw=v.get("entries") or []
            if len(raw)<2: counts["skipped"]+=1; continue
            lines=[]; dt=ct=0
            for e in raw:
                code=str(e.get("account_code") or e.get("account") or "").strip()
                if not code: raise HTTPException(422,f"凭证 {no} 存在空科目编码")
                if not c.execute("SELECT 1 FROM accounts WHERE account_set_id=? AND code=?",(b,code)).fetchone():
                    label=str(e.get("account_name") or e.get("accountName") or code)
                    c.execute("INSERT INTO accounts(account_set_id,code,name,category,direction) VALUES(?,?,?,'资产','借')",(b,code,label))
                d,cr=amount(e.get("debit_amount",e.get("debit",0))),amount(e.get("credit_amount",e.get("credit",0)))
                if (d==0 and cr==0) or (d and cr): raise HTTPException(422,f"凭证 {no} 存在无效分录")
                contact_id=None; aux=e.get("contact_id"); aux_name=e.get("aux") or e.get("contact_name")
                if aux and str(aux).isdigit():
                    found=c.execute("SELECT id FROM contacts WHERE account_set_id=? AND id=?",(b,int(aux))).fetchone(); contact_id=found[0] if found else None
                if contact_id is None and aux_name:
                    found=c.execute("SELECT id FROM contacts WHERE account_set_id=? AND name=? ORDER BY id LIMIT 1",(b,str(aux_name))).fetchone(); contact_id=found[0] if found else None
                lines.append((code,contact_id,str(aux_name or "") or None,str(e.get("summary") or ""),d,cr)); dt+=d; ct+=cr
            if dt!=ct: raise HTTPException(422,f"凭证 {no} 借贷不平衡，未导入")
            raw_status=str(v.get("status") or "DRAFT"); status=raw_status
            status={"APPROVED":"PENDING","已审核":"PENDING","已记账":"POSTED","待审核":"PENDING"}.get(status,status)
            if status not in {"DRAFT","PENDING","POSTED","VOIDED","REVERSED"}: status="DRAFT"
            was_reviewed=raw_status in {"APPROVED","已审核"}
            was_posted=status in {"POSTED","REVERSED"}
            attachment_value=v.get("attachment")
            attachments=v.get("attachment_urls") or v.get("attachmentUrls") or (
                [attachment_value.get("url")] if isinstance(attachment_value,dict) and attachment_value.get("url") else
                [attachment_value] if isinstance(attachment_value,str) and attachment_value.startswith("/uploads/") else []
            )
            attachments=[u for u in attachments if isinstance(u,str) and u.startswith("/uploads/") and ".." not in u.split("/")]
            attachment_name = (v.get("attachment") or {}).get("name") if isinstance(v.get("attachment"),dict) else None
            vid=c.execute("""INSERT INTO vouchers
                (account_set_id,legacy_id,voucher_no,voucher_date,summary,status,attachment_urls,biz_label,source,source_type,counterparty,memo,attachment_name,reviewed_at,posted_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,CASE WHEN ? THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') END,CASE WHEN ? THEN strftime('%Y-%m-%dT%H:%M:%fZ','now') END)""",(b,s,no,vd,str(v.get("memo") or v.get("summary") or v.get("bizLabel") or ""),status,json.dumps(attachments,ensure_ascii=False),str(v.get("bizLabel") or ""),v.get("source"),v.get("sourceType"),v.get("counterparty"),v.get("memo"),attachment_name,int(was_reviewed),int(was_posted))).lastrowid
            c.executemany("INSERT INTO voucher_entries(account_set_id,voucher_id,account_code,contact_id,aux_name,summary,debit_amount,credit_amount) VALUES(?,?,?,?,?,?,?,?)",[(b,vid,line[0],line[1],line[2],line[3],line[4],line[5]) for line in lines]); mark(c,b,"vouchers",s,vid); counts["vouchers"]+=1
        for i,r in enumerate(p.cashier_records):
            s=sid(r,f"cashier-{i}")
            mapped=exists(c,b,"cashier",s)
            if mapped:
                # One-time V9 reconciliation of V8 app_records snapshots. This
                # captures final pre-V9 edits and uploads before app_records is
                # retired for this module; the v9 marker prevents later stale
                # snapshots from overwriting live normalized rows.
                if not exists(c,b,"cashier-v9-sync",s):
                    target=int(mapped["target_id"])
                    rd=str(r.get("date") or r.get("record_date") or "")[:10]
                    try: date.fromisoformat(rd)
                    except ValueError: rd=None
                    raw_type=str(r.get("type") or r.get("biz_type") or "RECEIPT")
                    direction="OUT" if raw_type.endswith("OUT") else ("IN" if raw_type.endswith("IN") else None)
                    typ={"TRANSFER_OUT":"TRANSFER","TRANSFER_IN":"TRANSFER"}.get(raw_type,raw_type)
                    if typ not in {"RECEIPT","PAYMENT","TRANSFER"}: typ="RECEIPT"
                    attachment=r.get("attachment")
                    img=r.get("receipt_img_url") or (attachment.get("url") if isinstance(attachment,dict) else attachment)
                    img=img if isinstance(img,str) and img.startswith("/uploads/") and ".." not in img.split("/") else None
                    status={"APPROVED":"DONE","已办理":"DONE","已入账":"POSTED"}.get(r.get("status"),r.get("status","PENDING"))
                    status=status if status in {"PENDING","DONE","POSTED"} else "PENDING"
                    current=c.execute("SELECT linked_voucher_id,status FROM cashier_records WHERE account_set_id=? AND id=?",(b,target)).fetchone()
                    if current:
                        keep_status=current["status"] if current["linked_voucher_id"] is not None else status
                        if current["linked_voucher_id"] is not None:
                            c.execute("UPDATE cashier_records SET legacy_id=?,transfer_id=?,direction=?,extra_json=?,receipt_img_url=COALESCE(?,receipt_img_url) WHERE account_set_id=? AND id=?",(s,r.get("transferId"),direction,json.dumps(r,ensure_ascii=False),img,b,target))
                        elif rd:
                            c.execute("""UPDATE cashier_records SET legacy_id=?,transfer_id=?,direction=?,extra_json=?,record_date=?,biz_type=?,amount=?,account_name=?,contact_name=?,source_doc=?,receipt_img_url=?,status=? WHERE account_set_id=? AND id=?""",(
                                s,r.get("transferId"),direction,json.dumps(r,ensure_ascii=False),rd,typ,amount(r.get("amount")),
                                str(r.get("account_name") or r.get("account") or "未指定账户"),r.get("counterparty") or r.get("contact_name"),
                                r.get("sourceDoc") or r.get("source_doc"),img,keep_status,b,target))
                    mark(c,b,"cashier-v9-sync",s,target); counts["skipped"]+=1
                else: counts["skipped"]+=1
                continue
            rd=str(r.get("date") or r.get("record_date") or "")[:10]
            try: date.fromisoformat(rd)
            except ValueError: counts["skipped"]+=1; continue
            typ=r.get("biz_type") or r.get("type") or "RECEIPT"; typ={"TRANSFER_OUT":"TRANSFER","TRANSFER_IN":"TRANSFER"}.get(typ,typ)
            if typ not in {"RECEIPT","PAYMENT","TRANSFER"}: counts["skipped"]+=1; continue
            attachment=r.get("attachment")
            img=r.get("receipt_img_url") or (attachment.get("url") if isinstance(attachment,dict) else attachment)
            img=img if isinstance(img,str) and img.startswith("/uploads/") and ".." not in img.split("/") else None
            status={"APPROVED":"DONE","已办理":"DONE","已入账":"POSTED"}.get(r.get("status"),r.get("status","PENDING")); status=status if status in {"PENDING","DONE","POSTED"} else "PENDING"
            direction = "OUT" if str(r.get("type") or "").endswith("OUT") else ("IN" if str(r.get("type") or "").endswith("IN") else None)
            target=c.execute("INSERT INTO cashier_records(account_set_id,legacy_id,transfer_id,direction,extra_json,record_date,biz_type,amount,account_name,contact_name,source_doc,receipt_img_url,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(b,s,r.get("transferId"),direction,json.dumps(r,ensure_ascii=False),rd,typ,amount(r.get("amount")),str(r.get("account_name") or r.get("account") or "未指定账户"),r.get("counterparty") or r.get("contact_name"),r.get("sourceDoc") or r.get("source_doc"),img,status)).lastrowid
            mark(c,b,"cashier",s,target); mark(c,b,"cashier-v9-sync",s,target); counts["cashier_records"]+=1
        for i,inv in enumerate(p.invoices):
            s=sid(inv,f"invoice-{i}")
            mapped=exists(c,b,"invoices",s)
            if mapped:
                if not exists(c,b,"invoices-v9-sync",s):
                    target=int(mapped["target_id"])
                    no=str(inv.get("invoice_no") or inv.get("invoiceNumber") or inv.get("invoiceNo") or s)
                    vd=str(inv.get("invoice_date") or inv.get("date") or "")[:10]
                    try: date.fromisoformat(vd)
                    except ValueError: vd=None
                    attachments=inv.get("attachments") or []
                    attachment=inv.get("attachment")
                    attachment_url=next((x.get("url") for x in attachments if isinstance(x,dict) and x.get("url")),None)
                    img=inv.get("pdf_or_img_url") or attachment_url or (attachment.get("url") if isinstance(attachment,dict) else attachment)
                    img=img if isinstance(img,str) and img.startswith("/uploads/") and ".." not in img.split("/") else None
                    if vd:
                        c.execute("""UPDATE invoices SET legacy_id=?,invoice_code=?,invoice_no=?,invoice_date=?,buyer_name=?,buyer_tax_id=?,seller_name=?,amount=?,tax_amount=?,payment_method=?,status=?,pdf_or_img_url=?,items_json=?,source=?,extra_json=? WHERE account_set_id=? AND id=?""",(
                            s,inv.get("invoice_code") or inv.get("invoiceCode"),no,vd,inv.get("buyer_name") or inv.get("buyer") or "未填写",
                            inv.get("buyer_tax_id") or inv.get("buyerTax"),inv.get("seller_name") or inv.get("seller"),
                            amount(inv.get("amount",inv.get("total"))),amount(inv.get("tax_amount",inv.get("tax",0))),
                            inv.get("payment_method") or inv.get("payment"),inv.get("status","DRAFT"),img,
                            json.dumps(inv.get("items") or [],ensure_ascii=False),inv.get("source"),json.dumps(inv,ensure_ascii=False),b,target))
                    mark(c,b,"invoices-v9-sync",s,target); counts["skipped"]+=1
                else: counts["skipped"]+=1
                continue
            no=str(inv.get("invoice_no") or inv.get("invoiceNumber") or inv.get("invoiceNo") or s); vd=str(inv.get("invoice_date") or inv.get("date") or "")[:10]
            try: date.fromisoformat(vd)
            except ValueError: counts["skipped"]+=1; continue
            attachment=inv.get("attachment")
            attachments=inv.get("attachments") or []
            attachment_url=next((x.get("url") for x in attachments if isinstance(x,dict) and x.get("url")),None)
            img=inv.get("pdf_or_img_url") or attachment_url or (attachment.get("url") if isinstance(attachment,dict) else attachment)
            img=img if isinstance(img,str) and img.startswith("/uploads/") and ".." not in img.split("/") else None
            target=c.execute("INSERT INTO invoices(account_set_id,legacy_id,source,extra_json,invoice_code,invoice_no,invoice_date,buyer_name,buyer_tax_id,seller_name,amount,tax_amount,payment_method,status,pdf_or_img_url,items_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(b,s,inv.get("source"),json.dumps(inv,ensure_ascii=False),inv.get("invoice_code") or inv.get("invoiceCode"),str(inv.get("invoiceNumber") or no),vd,inv.get("buyer_name") or inv.get("buyer") or "未填写",inv.get("buyer_tax_id") or inv.get("buyerTax"),inv.get("seller_name") or inv.get("seller"),amount(inv.get("amount",inv.get("total"))),amount(inv.get("tax_amount",inv.get("tax",0))),inv.get("payment_method") or inv.get("payment"),inv.get("status","DRAFT"),img,json.dumps(inv.get("items") or [],ensure_ascii=False))).lastrowid
            mark(c,b,"invoices",s,target); mark(c,b,"invoices-v9-sync",s,target); counts["invoices"]+=1
        c.commit()
    except HTTPException: c.rollback(); raise
    except sqlite3.IntegrityError as exc: c.rollback(); raise HTTPException(409,detail=f"规范化迁移冲突，事务已回滚：{exc}") from exc
    except Exception: c.rollback(); raise
    return {"status":"completed","account_set_id":b,"counts":counts,"note":"本次迁移写入所选账套；源浏览器数据与 app_records 均未删除。"}
