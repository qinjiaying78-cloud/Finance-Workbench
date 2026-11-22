from __future__ import annotations
import json, sqlite3, io
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from ..database import get_connection
from ..schemas import VoucherCreate, VoucherEntryIn
from .common import assert_account_set, assert_period_unlocked, cents_to_yuan, money_to_cents, validate_period
from .vouchers import _validate_entry_payload, _insert_entries

router=APIRouter(prefix='/api/v11',tags=['V11财务闭环'])
def db():
 c=get_connection()
 try: yield c
 finally: c.close()
def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')

class BankMatch(BaseModel):
 legacy_id:str=Field(min_length=1,max_length=160)
 period:str
 bank_account:str
 bank_amount:Decimal
 book_amount:Decimal|None=None
 bank_date:str|None=None
 company_date:str|None=None
 bank_ref:str|None=None
 book_ref:str|None=None
 status:str='MATCH'
 difference_type:str|None=None
 resolution:str|None=None
 evidence:str|None=None
 voucher_no:str|None=None
 raw:dict[str,Any]=Field(default_factory=dict)
class TaxWorkpaper(BaseModel):
 period:str
 tax_type:str
 tax_amount:Decimal=Decimal('0')
 basis_amount:Decimal=Decimal('0')
 rate:Decimal|None=None
 status:str='DRAFT'
 payload:dict[str,Any]=Field(default_factory=dict)
class TaxArchive(BaseModel):
 period:str
 tax_type:str
 paid_date:str|None=None
 amount:Decimal
 certificate_no:str|None=None
 attachment_url:str|None=None
 note:str|None=None
class CloseRequest(BaseModel):
 period:str
 reason:str='期末结账'

@router.put('/bank-matches')
def save_bank_matches(
 items:list[BankMatch],account_set_id:str=Query('default'),conn=Depends(db),
 replace_period:str|None=None,replace_account:str|None=None,
):
 assert_account_set(conn,account_set_id)
 if len(items)>20000: raise HTTPException(413,'单次对账流水不得超过 20000 条')
 replace_scope = replace_period is not None or replace_account is not None
 if replace_scope:
  if not replace_period or not replace_account:
   raise HTTPException(422,'保存整批对账须指定期间和银行账户')
  validate_period(replace_period)
  if any((item.period,item.bank_account)!=(replace_period,replace_account) for item in items):
   raise HTTPException(422,'对账记录的期间或银行账户不一致')
  incoming_ids = {item.legacy_id for item in items}
  if len(incoming_ids)!=len(items):
   raise HTTPException(422,'对账结果中存在重复记录标识')
 try:
  conn.execute('BEGIN IMMEDIATE')
  if replace_scope:
   assert_period_unlocked(conn,replace_period+'-01',account_set_id)
   previous = conn.execute(
    'SELECT id,legacy_id FROM v11_bank_matches WHERE account_set_id=? AND period=? AND bank_account=?',
    (account_set_id,replace_period,replace_account),
   ).fetchall()
   conn.executemany('DELETE FROM v11_bank_matches WHERE id=?',
     [(row['id'],) for row in previous if row['legacy_id'] not in incoming_ids])
  for x in items:
   validate_period(x.period)
   conn.execute('''INSERT INTO v11_bank_matches(account_set_id,legacy_id,period,bank_account,bank_amount,book_amount,bank_date,company_date,bank_ref,book_ref,status,difference_type,resolution,evidence,voucher_no,raw_json,updated_at)
   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_set_id,legacy_id,period,bank_account) DO UPDATE SET bank_amount=excluded.bank_amount,book_amount=excluded.book_amount,bank_date=excluded.bank_date,company_date=excluded.company_date,bank_ref=excluded.bank_ref,book_ref=excluded.book_ref,status=excluded.status,difference_type=excluded.difference_type,resolution=excluded.resolution,evidence=excluded.evidence,voucher_no=excluded.voucher_no,raw_json=excluded.raw_json,updated_at=excluded.updated_at''',
   (account_set_id,x.legacy_id,x.period,x.bank_account,money_to_cents(x.bank_amount),money_to_cents(x.book_amount) if x.book_amount is not None else None,x.bank_date,x.company_date,x.bank_ref,x.book_ref,x.status,x.difference_type,x.resolution,x.evidence,x.voucher_no,json.dumps(x.raw,ensure_ascii=False),now()))
  conn.commit()
 except Exception: conn.rollback(); raise
 return {'saved':len(items)}

@router.get('/bank-matches')
def list_bank_matches(period:str,bank_account:str|None=None,account_set_id:str=Query('default'),conn=Depends(db)):
 assert_account_set(conn,account_set_id)
 sql='SELECT * FROM v11_bank_matches WHERE account_set_id=? AND period=?'; p=[account_set_id,period]
 if bank_account: sql+=' AND bank_account=?';p.append(bank_account)
 rows=conn.execute(sql+' ORDER BY bank_date,legacy_id',p).fetchall()
 return {'items':[{**dict(r),'bank_amount':cents_to_yuan(r['bank_amount']),'book_amount':cents_to_yuan(r['book_amount']) if r['book_amount'] is not None else None,'raw':json.loads(r['raw_json'] or '{}')} for r in rows]}

@router.post('/bank-matches/{match_id}/voucher',status_code=201)
def bank_match_voucher(match_id:int,account_set_id:str=Query('default'),conn=Depends(db)):
 assert_account_set(conn,account_set_id)
 r=conn.execute('SELECT * FROM v11_bank_matches WHERE account_set_id=? AND id=?',(account_set_id,match_id)).fetchone()
 if not r: raise HTTPException(404,'对账差异不存在')
 if r['difference_type'] not in ('ENTERPRISE_OMISSION','企业漏记') and r['status'] not in ('COMPANY_ONLY','企业漏记'): raise HTTPException(409,'仅企业漏记差异允许一键生成补记凭证')
 if r['voucher_id']: return {'voucher_id':r['voucher_id'],'already_created':True,'redirect':'/app#voucher-create'}
 if r['book_amount'] is not None and r['book_amount']!=0: raise HTTPException(409,'账面金额非零，不能按企业漏记直接补记')
 raw=json.loads(r['raw_json'] or '{}'); amount=Decimal(r['bank_amount'])/100
 debit=raw.get('debit_account') or '1002'; credit=raw.get('credit_account')
 if not credit: raise HTTPException(422,'请先在流水 raw 中提供 credit_account（对应科目）')
 voucher_date=r['bank_date'] or r['period']+'-01'; assert_period_unlocked(conn,voucher_date,account_set_id)
 entries=[VoucherEntryIn(account_code=debit,summary='银行对账企业漏记补记',debit_amount=amount),VoucherEntryIn(account_code=credit,summary='银行对账企业漏记补记',credit_amount=amount)]
 clean,dt,ct=_validate_entry_payload(conn,entries,account_set_id)
 cur=conn.execute('INSERT INTO vouchers(account_set_id,voucher_no,voucher_date,summary,status,attachment_urls,biz_label,source,source_type,memo) VALUES(?,?,?,?,?,?,?,?,?,?)',(account_set_id,'V11-BANK-'+str(r['id'])+'-'+r['period'].replace('-',''),voucher_date,'银行对账企业漏记补记：'+r['legacy_id'],'DRAFT','[]','银行补记','银行对账差异','BANK_RECON','自动生成草稿，请核对科目后制证'))
 _insert_entries(conn,account_set_id,cur.lastrowid,clean)
 conn.execute('UPDATE v11_bank_matches SET voucher_id=?,voucher_no=? WHERE id=?',(cur.lastrowid,'V11-BANK-'+str(r['id']),match_id));conn.commit()
 return {'voucher_id':cur.lastrowid,'voucher_no':'V11-BANK-'+str(r['id']),'status':'DRAFT','redirect':'/app#voucher-create'}

@router.post('/tax/workpapers',status_code=201)
def save_tax_workpaper(x:TaxWorkpaper,account_set_id:str=Query('default'),conn=Depends(db)):
 assert_account_set(conn,account_set_id);validate_period(x.period)
 cur=conn.execute('INSERT INTO v11_tax_workpapers(account_set_id,period,tax_type,basis_amount,tax_amount,rate,status,payload_json,updated_at) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(account_set_id,period,tax_type) DO UPDATE SET basis_amount=excluded.basis_amount,tax_amount=excluded.tax_amount,rate=excluded.rate,status=excluded.status,payload_json=excluded.payload_json,updated_at=excluded.updated_at RETURNING id',(account_set_id,x.period,x.tax_type,money_to_cents(x.basis_amount),money_to_cents(x.tax_amount),str(x.rate) if x.rate is not None else None,x.status,json.dumps(x.payload,ensure_ascii=False),now()))
 row=cur.fetchone();conn.commit();return {'id':row['id'],'saved':True}
@router.get('/tax/workpapers')
def tax_workpapers(period:str,account_set_id:str=Query('default'),conn=Depends(db)):
 assert_account_set(conn,account_set_id)
 rows=conn.execute('SELECT * FROM v11_tax_workpapers WHERE account_set_id=? AND period=? ORDER BY tax_type',(account_set_id,period)).fetchall()
 return {'items':[{**dict(r),'basis_amount':cents_to_yuan(r['basis_amount']),'tax_amount':cents_to_yuan(r['tax_amount']),'payload':json.loads(r['payload_json'] or '{}')} for r in rows]}
@router.post('/tax/archives',status_code=201)
def archive_tax(x:TaxArchive,account_set_id:str=Query('default'),conn=Depends(db)):
 assert_account_set(conn,account_set_id);validate_period(x.period)
 if x.attachment_url and (not x.attachment_url.startswith('/uploads/') or '..' in x.attachment_url.split('/')): raise HTTPException(422,'完税附件必须为本地 /uploads/ 路径')
 cur=conn.execute('INSERT INTO v11_tax_archives(account_set_id,period,tax_type,paid_date,amount,certificate_no,attachment_url,note,created_at) VALUES(?,?,?,?,?,?,?,?,?)',(account_set_id,x.period,x.tax_type,x.paid_date,money_to_cents(x.amount),x.certificate_no,x.attachment_url,x.note,now()));conn.commit();return {'id':cur.lastrowid,'archived':True}
@router.get('/tax/archives')
def list_tax_archives(period:str,account_set_id:str=Query('default'),conn=Depends(db)):
 assert_account_set(conn,account_set_id); rows=conn.execute('SELECT * FROM v11_tax_archives WHERE account_set_id=? AND period=? ORDER BY id DESC',(account_set_id,period)).fetchall()
 return {'items':[{**dict(r),'amount':cents_to_yuan(r['amount'])} for r in rows]}

@router.get('/closing/check')
def closing_check(period:str,account_set_id:str=Query('default'),conn=Depends(db)):
 assert_account_set(conn,account_set_id)
 unposted=conn.execute("SELECT COUNT(*) n FROM vouchers WHERE account_set_id=? AND substr(voucher_date,1,7)=? AND status IN ('DRAFT','PENDING')",(account_set_id,period)).fetchone()['n']
 totals=conn.execute("SELECT COALESCE(SUM(e.debit_amount),0) d,COALESCE(SUM(e.credit_amount),0) c FROM vouchers v JOIN voucher_entries e ON e.voucher_id=v.id AND e.account_set_id=v.account_set_id WHERE v.account_set_id=? AND substr(v.voucher_date,1,7)=? AND v.status IN ('POSTED','REVERSED')",(account_set_id,period)).fetchone()
 unmatched=conn.execute("SELECT COUNT(*) n FROM v11_bank_matches WHERE account_set_id=? AND period=? AND status<>'MATCH' AND (resolution IS NULL OR trim(COALESCE(evidence,''))='')",(account_set_id,period)).fetchone()['n']
 cashier_open=conn.execute("SELECT COUNT(*) n FROM cashier_records WHERE account_set_id=? AND substr(record_date,1,7)=? AND status IN ('PENDING','DONE')",(account_set_id,period)).fetchone()['n']
 checks={'no_unposted_vouchers':unposted==0,'trial_balance_balanced':totals['d']==totals['c'],'bank_items_processed':unmatched==0,'cashier_records_processed':cashier_open==0}
 return {'period':period,'passed':all(checks.values()),'checks':checks,'details':{'unposted_vouchers':unposted,'debit_total':cents_to_yuan(totals['d']),'credit_total':cents_to_yuan(totals['c']),'unprocessed_bank_items':unmatched,'unprocessed_cashier_records':cashier_open}}
@router.post('/closing/lock')
def lock_close(x:CloseRequest,account_set_id:str=Query('default'),conn=Depends(db)):
 validate_period(x.period)
 conn.execute('BEGIN IMMEDIATE')
 check=closing_check(x.period,account_set_id,conn)
 if not check['passed']:
  conn.rollback();raise HTTPException(409,detail={'message':'期末结账检查未通过','check':check})
 conn.execute("INSERT INTO accounting_periods(account_set_id,period,is_locked,locked_at,lock_reason,updated_at) VALUES(?,?,1,?,?,?) ON CONFLICT(account_set_id,period) DO UPDATE SET is_locked=1,locked_at=excluded.locked_at,lock_reason=excluded.lock_reason,updated_at=excluded.updated_at",(account_set_id,x.period,now(),x.reason,now()))
 conn.commit();return {'period':x.period,'is_locked':True,'checks':check['checks']}

@router.post('/exports/{report_name}')
def export_xlsx(report_name:str,payload:dict[str,Any],account_set_id:str=Query('default'),conn=Depends(db)):
 assert_account_set(conn,account_set_id)
 allowed={'trial-balance','sub-ledger','bank-adjustment','financial-statements'}
 if report_name not in allowed: raise HTTPException(404,'不支持的报表导出类型')
 headers=payload.get('headers') or []; rows=payload.get('rows') or []
 if not headers or not isinstance(headers,list) or not isinstance(rows,list): raise HTTPException(422,'导出需提供 headers 和 rows')
 wb=Workbook();ws=wb.active;ws.title=report_name[:31]
 ws.append([str(x) for x in headers])
 for c in ws[1]: c.font=Font(name='微软雅黑',bold=True,color='FFFFFF');c.fill=PatternFill('solid',fgColor='8B8175');c.alignment=Alignment(horizontal='center')
 for row in rows: ws.append([v if isinstance(v,(str,int,float)) or v is None else json.dumps(v,ensure_ascii=False) for v in row])
 ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
 for col in ws.columns:
  letter=col[0].column_letter; ws.column_dimensions[letter].width=min(36,max(12,max((len(str(c.value or '')) for c in col),default=10)+2))
 out=io.BytesIO();wb.save(out);out.seek(0)
 return StreamingResponse(out,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':f'attachment; filename="{report_name}.xlsx"'})
