"""Book-scoped collaboration commands; SQLite transactions are the unit of work."""
from __future__ import annotations
import calendar,hashlib,json,re,sqlite3,uuid
from datetime import date,datetime,timezone
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP
from fastapi import APIRouter,BackgroundTasks,Body,Depends,HTTPException,Query,Request
from ..database import get_connection
from ..collab_auth import access,capabilities,can_ledger
from ..collab_db import seed_details
from .common import assert_account_set,assert_period_unlocked,money_to_cents,cents_to_yuan,now_iso,validate_period,generate_voucher_no,voucher_dict

router=APIRouter(prefix='/api/v2',tags=['多人协作与流程'])
FIN={'director','accountant'}
READ=FIN|{'viewer'}
def db():
    c=get_connection()
    try: yield c
    finally: c.close()
def uid(): return uuid.uuid4().hex
def js(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(value): return hashlib.sha256(js(value).encode()).hexdigest()
def required(payload,key,limit=200):
    value=payload.get(key)
    if not isinstance(value,str) or not value.strip() or len(value)>limit: raise HTTPException(422,f'请填写有效的 {key}')
    return value.strip()
def version(payload,row):
    if payload.get('expectedVersion')!=row['version']: raise HTTPException(409,'记录已变化，请刷新后重试')
def command(c,a,book,name,p,fn):
    key=required(p,'requestKey',120); fingerprint=digest(p)
    try:
        with c:
            c.execute('BEGIN IMMEDIATE')
            prior=c.execute('SELECT * FROM fw_command_receipts WHERE account_set_id=? AND actor=? AND command=? AND request_key=?',(book,a['name'],name,key)).fetchone()
            if prior:
                if prior['input_hash']!=fingerprint: raise HTTPException(409,'同一请求号不能用于不同参数')
                return json.loads(prior['result_json'])
            result=fn()
            c.execute('INSERT INTO fw_command_receipts VALUES(?,?,?,?,?,?)',(book,a['name'],name,key,fingerprint,js(result)))
            return result
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409,'唯一性、引用或锁定约束冲突，请刷新核对') from exc
def row(c,table,book,ident):
    found=c.execute(f'SELECT * FROM {table} WHERE account_set_id=? AND id=?',(book,ident)).fetchone()
    if not found: raise HTTPException(404,'记录不存在或无权访问')
    return found
def period_open(c,book,value):
    try: day=date.fromisoformat(value)
    except (ValueError,TypeError): raise HTTPException(422,'业务日期必须为 YYYY-MM-DD')
    assert_period_unlocked(c,day,book)
    return day
def assign(c,book,case,role,step,excluded=()):
    users=c.execute('SELECT DISTINCT user_name FROM fw_book_roles WHERE account_set_id=? AND role=? AND active=1',(book,role)).fetchall()
    users=[r['user_name'] for r in users if r['user_name'] not in excluded]
    if not users: raise HTTPException(409,'请先配置另一位有权限的审批人：'+role)
    for user in users:
        c.execute('INSERT INTO fw_workflow_tasks(id,account_set_id,case_id,assignee,kind,required_role,step) VALUES(?,?,?,?,\'action\',?,?)',(uid(),book,case,user,role,step))
def notice(c,book,case,user,step=1):
    c.execute('INSERT OR IGNORE INTO fw_workflow_tasks(id,account_set_id,case_id,assignee,kind,step) VALUES(?,?,?,?,\'notice\',?)',(uid(),book,case,user,step))
def add_followup_task(c,book,ledger):
    if not ledger['needs_invoice_followup'] or ledger['invoice_state'] in ('complete','not_required'): return
    role='cashier' if ledger['cashier_visible'] else 'accountant'
    if not c.execute('SELECT 1 FROM fw_book_roles WHERE account_set_id=? AND role=? AND active=1',(book,role)).fetchone(): role='accountant'
    if not c.execute('SELECT 1 FROM fw_book_roles WHERE account_set_id=? AND role=? AND active=1',(book,role)).fetchone(): return
    create_case(c,book,'invoice_followup',ledger['id'],ledger['version'],'催票 · '+ledger['doc_no'],'service:invoice',first_role=role)
def create_case(c,book,category,subject,rev,title,actor,maker=None,payload=None,first_role='director'):
    ident=uid(); maker=maker or actor
    c.execute('INSERT INTO fw_workflow_cases(id,account_set_id,category,subject_id,subject_version,title,initiated_by,made_by,payload_json) VALUES(?,?,?,?,?,?,?,?,?)',(ident,book,category,str(subject),rev,title,actor,maker,js(payload or {})))
    assign(c,book,ident,first_role,1,(actor,maker) if first_role=='director' else ())
    return ident

@router.get('/access')
def get_access(request:Request,account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id)
    return {'name':a['name'],'roles':sorted(a['roles']),'capabilities':capabilities(a),'account_set_id':account_set_id}

@router.get('/members')
def members(request:Request,account_set_id:str='default',c=Depends(db)):
    access(c,request,account_set_id,{'director'})
    return {'items':[dict(r) for r in c.execute('SELECT user_name,role,scopes_json,active FROM fw_book_roles WHERE account_set_id=? ORDER BY user_name,role',(account_set_id,))]}

@router.post('/members')
def set_member(request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,{'director'})
    def apply():
        name=required(p,'name',60); roles=p.get('roles',[]); scopes=p.get('scopes',[])
        if not isinstance(roles,list) or set(roles)-{'director','accountant','cashier','reporter','viewer'}: raise HTTPException(422,'角色无效')
        if not isinstance(scopes,list) or set(scopes)-{'warehouse','expense','asset'}: raise HTTPException(422,'填报范围无效')
        existing=c.execute('SELECT 1 FROM fw_users WHERE name=?',(name,)).fetchone()
        if not existing:
            from .finance_controls import password_hash
            import secrets
            password=required(p,'password',256)
            if len(password)<10: raise HTTPException(422,'密码至少 10 位')
            salt=secrets.token_hex(16)
            primary='admin' if 'director' in roles else 'clerk' if 'accountant' in roles else next(iter(roles),'viewer')
            c.execute('INSERT INTO fw_users VALUES(?,?,?,?)',(name,primary,salt,password_hash(password,salt)))
        c.execute('UPDATE fw_book_roles SET active=0 WHERE account_set_id=? AND user_name=?',(account_set_id,name))
        for role in set(roles):
            c.execute('INSERT INTO fw_book_roles VALUES(?,?,?,?,1) ON CONFLICT(account_set_id,user_name,role) DO UPDATE SET scopes_json=excluded.scopes_json,active=1',(account_set_id,name,role,js(scopes)))
        if not c.execute("SELECT 1 FROM fw_book_roles WHERE account_set_id=? AND role='director' AND active=1",(account_set_id,)).fetchone(): raise HTTPException(409,'必须保留至少一位财务总监')
        # Open tasks are re-evaluated at read/action time, and reassigned on role removal.
        tasks=c.execute("SELECT * FROM fw_workflow_tasks WHERE account_set_id=? AND assignee=? AND kind='action' AND state='open'",(account_set_id,name)).fetchall()
        for task in tasks:
            if task['required_role'] not in roles:
                c.execute("UPDATE fw_workflow_tasks SET state='cancelled' WHERE id=?",(task['id'],))
                case=row(c,'fw_workflow_cases',account_set_id,task['case_id'])
                candidates=c.execute('SELECT DISTINCT user_name FROM fw_book_roles WHERE account_set_id=? AND role=? AND active=1',(account_set_id,task['required_role'])).fetchall()
                assigned=False
                for candidate in candidates:
                    user=candidate['user_name']
                    if task['required_role']=='director' and user in (case['made_by'],case['initiated_by']): continue
                    c.execute("INSERT INTO fw_workflow_tasks(id,account_set_id,case_id,assignee,kind,required_role,step) VALUES(?,?,?,?,'action',?,?) ON CONFLICT(case_id,assignee,kind,step) DO UPDATE SET state='open'",(uid(),account_set_id,case['id'],user,task['required_role'],task['step']))
                    assigned=True
                if not assigned: raise HTTPException(409,'此成员仍有待办，需先配置其他合格处理人')
        return {'name':name,'roles':roles,'scopes':scopes}
    return command(c,a,account_set_id,'set_member',p,apply)

@router.get('/preferences')
def preferences(request:Request,account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id)
    found=c.execute('SELECT * FROM fw_preferences WHERE account_set_id=? AND user_name=?',(account_set_id,a['name'])).fetchone()
    return {'shortcuts':json.loads(found['shortcuts_json']) if found else None,'version':found['version'] if found else 0}

@router.put('/preferences')
def save_preferences(request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id)
    def apply():
        items=p.get('shortcuts')
        if not isinstance(items,list) or len(items)>30 or len(items)!=len(set(items)) or any(not isinstance(v,str) or not re.fullmatch(r'[a-z0-9-]{1,80}',v) for v in items): raise HTTPException(422,'常用入口最多 30 项，且不能重复')
        if not a['roles']&READ:
            permitted={'home','workflow-center'}
            if 'cashier' in a['roles']: permitted.update({'fund-bank','group-fund','collab-ledgers','group-biz'})
            for scope,entries in {'warehouse':{'collab-ledgers','group-biz'},'expense':{'collab-expenses','group-fund'},'asset':{'collab-assets','group-fa'}}.items():
                if scope in a['scopes']: permitted.update(entries)
            if set(items)-permitted: raise HTTPException(403,'不能保存无权访问的常用入口')
        found=c.execute('SELECT version FROM fw_preferences WHERE account_set_id=? AND user_name=?',(account_set_id,a['name'])).fetchone()
        current=found['version'] if found else 0
        if p.get('expectedVersion')!=current: raise HTTPException(409,'常用入口已在另一页面更新')
        c.execute('INSERT INTO fw_preferences(account_set_id,user_name,shortcuts_json,version) VALUES(?,?,?,?) ON CONFLICT(account_set_id,user_name) DO UPDATE SET shortcuts_json=excluded.shortcuts_json,version=excluded.version,updated_at=CURRENT_TIMESTAMP',(account_set_id,a['name'],js(items),current+1))
        return {'shortcuts':items,'version':current+1}
    return command(c,a,account_set_id,'preferences',p,apply)

@router.get('/dimensions')
def dimensions(request:Request,account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id)
    allowed={'customer','supplier','department','project','bank_account'} if a['roles']&READ else {'department','project'}|({'bank_account'} if 'cashier' in a['roles'] else set())
    return {'items':[dict(r) for r in c.execute('SELECT * FROM fw_dimensions WHERE account_set_id=? AND is_active=1 ORDER BY kind,code',(account_set_id,)) if r['kind'] in allowed]}

@router.post('/dimensions')
def save_dimension(request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,FIN)
    def apply():
        kind=required(p,'kind'); code=required(p,'code');name=required(p,'name')
        if kind not in {'customer','supplier','department','project','bank_account'}: raise HTTPException(422,'辅助维度无效')
        ident=uid();c.execute('INSERT INTO fw_dimensions(id,account_set_id,kind,code,name) VALUES(?,?,?,?,?)',(ident,account_set_id,kind,code,name))
        return dict(row(c,'fw_dimensions',account_set_id,ident))
    return command(c,a,account_set_id,'dimension',p,apply)

def ledger_values(c,book,p,check_period=True):
    module=p.get('module');kind=p.get('kind')
    allowed={'warehouse':{'purchase_order','receipt','invoice'},'expense':{'reimbursement','invoice'},'asset':{'asset_entry','invoice'},'cash':{'cash_entry'}}
    if module not in allowed or kind not in allowed[module]: raise HTTPException(422,'模块与单据类型不一致')
    try: day=date.fromisoformat(required(p,'business_date')).isoformat()
    except ValueError: raise HTTPException(422,'业务日期必须为 YYYY-MM-DD')
    if check_period: period_open(c,book,day)
    try:
        quantity=Decimal(str(p.get('quantity','0')))
        if not quantity.is_finite() or quantity<0 or quantity.as_tuple().exponent < -6 or quantity>Decimal('99999999999999'): raise ValueError()
    except (InvalidOperation,ValueError): raise HTTPException(422,'数量须为非负数，最多六位小数')
    amount=money_to_cents(str(p.get('amount','0'))); tax=money_to_cents(str(p.get('tax_amount','0')))
    if amount<0 or tax<0 or tax>amount: raise HTTPException(422,'金额／税额无效；金额为含税金额')
    payment=p.get('payment_kind') or None
    if payment not in (None,'advance','shipment'): raise HTTPException(422,'款项性质无效')
    invoice=p.get('invoice_state','unknown')
    if invoice not in {'unknown','missing','partial','complete','not_required'}: raise HTTPException(422,'发票状态无效')
    values={'module':module,'kind':kind,'business_date':day,'doc_no':required(p,'doc_no'),
        'doc_line':p.get('doc_line',1),'amount_cents':amount,'tax_cents':tax,'quantity':str(quantity),'item_code':str(p.get('item_code',''))[:120],
        'payment_kind':payment,'invoice_state':invoice,'needs_invoice_followup':int(bool(p.get('needs_invoice_followup'))),'cashier_visible':int(bool(p.get('cashier_visible')))}
    if not isinstance(values['doc_line'],int) or not 1<=values['doc_line']<=100000: raise HTTPException(422,'单据行号无效')
    for field,kind in [('project_id','project'),('supplier_id','supplier'),('department_id','department')]:
        value=p.get(field) or None
        if value and not c.execute('SELECT 1 FROM fw_dimensions WHERE account_set_id=? AND id=? AND kind=? AND is_active=1',(book,value,kind)).fetchone(): raise HTTPException(422,'辅助核算对象不存在或类型错误：'+field)
        values[field]=value
    return values
def ledger_out(c,r):
    d=dict(r);d['amount']=cents_to_yuan(d['amount_cents']);d['tax_amount']=cents_to_yuan(d['tax_cents'])
    event=c.execute('SELECT * FROM fw_followups WHERE account_set_id=? AND ledger_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',(d['account_set_id'],d['id'])).fetchone()
    if event:
        d['followup_state']=event['state']
        if event['invoice_state']: d['effective_invoice_state']=event['invoice_state']
    if d['voucher_id']:
        voucher=c.execute('SELECT status FROM vouchers WHERE account_set_id=? AND id=?',(d['account_set_id'],d['voucher_id'])).fetchone()
        d['voucher_status']=voucher['status'] if voucher else None
    return d

@router.get('/ledgers')
def ledgers(request:Request,account_set_id:str='default',module:str|None=None,limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0),c=Depends(db)):
    a=access(c,request,account_set_id)
    where=['l.account_set_id=?'];params=[account_set_id]
    if module: where.append('l.module=?');params.append(module)
    if not a['roles'] & READ:
        clauses=[]
        if 'cashier' in a['roles']: clauses.append("(l.module='cash' OR l.cashier_visible=1)")
        if 'reporter' in a['roles'] and a['scopes']:
            clauses.append('(l.created_by=? AND l.owner_verified=1 AND l.module IN ('+','.join('?' for _ in a['scopes'])+'))');params.extend([a['name'],*sorted(a['scopes'])])
        where.append('('+' OR '.join(clauses)+')' if clauses else '0')
    # Only the latest approved replacement displaces an original in the effective view.
    where.append("NOT EXISTS(SELECT 1 FROM fw_ledger_items n WHERE n.account_set_id=l.account_set_id AND n.corrected_from=l.id AND n.status IN ('approved','posted'))")
    clause=' AND '.join(where)
    total=c.execute('SELECT count(*) FROM fw_ledger_items l WHERE '+clause,params).fetchone()[0]
    records=c.execute('SELECT l.* FROM fw_ledger_items l WHERE '+clause+' ORDER BY l.created_at DESC,l.rowid DESC LIMIT ? OFFSET ?',(*params,limit,offset)).fetchall()
    return {'items':[ledger_out(c,r) for r in records],'total':total,'limit':limit,'offset':offset}

@router.post('/ledgers')
def create_ledger(request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,FIN|{'reporter','cashier'})
    def apply():
        values=ledger_values(c,account_set_id,p)
        test={**values,'created_by':a['name'],'owner_verified':1}
        if not can_ledger(a,test,True): raise HTTPException(403,'不能填报此业务模块')
        if not a['roles'] & FIN: values['cashier_visible']=0
        ident=uid();values.update(id=ident,root_id=ident,account_set_id=account_set_id,created_by=a['name'])
        keys=list(values);c.execute('INSERT INTO fw_ledger_items('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',[values[k] for k in keys])
        saved=row(c,'fw_ledger_items',account_set_id,ident);add_followup_task(c,account_set_id,saved)
        return ledger_out(c,saved)
    return command(c,a,account_set_id,'ledger_create',p,apply)

@router.patch('/ledgers/{ident}')
def update_ledger(ident:str,request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,FIN|{'reporter','cashier'})
    def apply():
        old=row(c,'fw_ledger_items',account_set_id,ident);version(p,old)
        if not can_ledger(a,old,True): raise HTTPException(404,'记录不存在或无权修改')
        if old['is_locked'] or old['status'] not in ('draft','rejected'): raise HTTPException(409,'已锁定或已提交，请发起更正申请')
        values=ledger_values(c,account_set_id,p)
        if not a['roles']&FIN and (values['module']!=old['module'] or values['cashier_visible']!=old['cashier_visible']): raise HTTPException(403,'不能修改归属或可见范围')
        c.execute('UPDATE fw_ledger_items SET '+','.join(k+'=?' for k in values)+",version=version+1,status='draft' WHERE id=?",(*values.values(),ident))
        return ledger_out(c,row(c,'fw_ledger_items',account_set_id,ident))
    return command(c,a,account_set_id,'ledger_update:'+ident,p,apply)

@router.post('/ledgers/{ident}/submit')
def submit_ledger(ident:str,request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,FIN|{'reporter','cashier'})
    def apply():
        old=row(c,'fw_ledger_items',account_set_id,ident);version(p,old)
        if not can_ledger(a,old,True): raise HTTPException(404,'记录不存在或无权提交')
        if old['status'] not in ('draft','rejected','pending_reconcile'): raise HTTPException(409,'当前状态不能提交')
        if old['source_row_id'] and not a['roles']&FIN: raise HTTPException(403,'外部入账需要财务核对')
        period_open(c,account_set_id,old['business_date'])
        cat='reimbursement' if old['kind']=='reimbursement' else 'ledger_change' if old['corrected_from'] else 'ledger'
        first='accountant' if cat=='reimbursement' else 'director'
        case=create_case(c,account_set_id,cat,ident,old['version']+1,old['doc_no']+' · '+old['kind'],a['name'],old['created_by'],first_role=first)
        c.execute("UPDATE fw_ledger_items SET status='submitted',is_locked=1,version=version+1 WHERE id=?",(ident,))
        return {'caseId':case,'status':'submitted'}
    return command(c,a,account_set_id,'ledger_submit:'+ident,p,apply)

@router.post('/ledgers/{ident}/changes')
def change_ledger(ident:str,request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,FIN)
    def apply():
        old=row(c,'fw_ledger_items',account_set_id,ident);version(p,old)
        if not old['is_locked']: raise HTTPException(409,'草稿请直接编辑')
        if old['voucher_id']:
            voucher=row(c,'vouchers',account_set_id,old['voucher_id'])
            if voucher['status'] in ('DRAFT','PENDING') and not voucher['reviewed_at']:
                if c.execute("SELECT 1 FROM fw_workflow_cases WHERE account_set_id=? AND category='voucher' AND subject_id=? AND state='pending'",(account_set_id,str(voucher['id']))).fetchone(): raise HTTPException(409,'请先撤回关联凭证的审批')
                c.execute("UPDATE vouchers SET status='VOIDED',version=version+1 WHERE id=?",(voucher['id'],))
            elif voucher['status'] not in ('REVERSED','VOIDED'): raise HTTPException(409,'请先办理原凭证红冲，再创建更正记录')
        elif old['status']=='posted': raise HTTPException(409,'已过账台账缺少凭证关联，请先核对历史来源')
        if c.execute("SELECT 1 FROM fw_ledger_items WHERE account_set_id=? AND corrected_from=? AND status NOT IN ('rejected','voided')",(account_set_id,ident)).fetchone(): raise HTTPException(409,'已有更正版本，请先处理')
        reason=required(p,'reason',1000);values=ledger_values(c,account_set_id,p)
        new=uid();values.update(id=new,root_id=old['root_id'],corrected_from=ident,reason=reason,account_set_id=account_set_id,created_by=a['name'])
        c.execute('INSERT INTO fw_ledger_items('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',tuple(values.values()))
        return ledger_out(c,row(c,'fw_ledger_items',account_set_id,new))
    return command(c,a,account_set_id,'ledger_change:'+ident,p,apply)

@router.post('/ledgers/{ident}/followups')
def followup(ident:str,request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,FIN|{'cashier'})
    def apply():
        old=row(c,'fw_ledger_items',account_set_id,ident)
        if not can_ledger(a,old): raise HTTPException(404,'单据不存在或无权访问')
        state=p.get('state');invoice=p.get('invoice_state')
        if state not in {'pending','reminded','received','closed'} or invoice not in (None,'missing','partial','complete','not_required'): raise HTTPException(422,'催票状态无效')
        ident2=uid();c.execute('INSERT INTO fw_followups(id,account_set_id,ledger_id,state,invoice_state,note,created_by) VALUES(?,?,?,?,?,?,?)',(ident2,account_set_id,ident,state,invoice,required(p,'note',1000),a['name']))
        if state in ('received','closed') and invoice in ('complete','not_required'):
            for case in c.execute("SELECT * FROM fw_workflow_cases WHERE account_set_id=? AND category='invoice_followup' AND subject_id=? AND state='pending'",(account_set_id,ident)).fetchall():
                c.execute("UPDATE fw_workflow_cases SET state='resolved',decided_by=?,decided_at=?,note=? WHERE id=?",(a['name'],now_iso(),p['note'],case['id']))
                c.execute("UPDATE fw_workflow_tasks SET state='cancelled' WHERE case_id=? AND kind='action' AND state='open'",(case['id'],))
                role='cashier' if 'cashier' in a['roles'] else 'accountant' if 'accountant' in a['roles'] else 'director'
                c.execute("INSERT INTO fw_workflow_tasks(id,account_set_id,case_id,assignee,kind,required_role,step,state,acted_at) VALUES(?,?,?,?,'action',?,1,'done',?) ON CONFLICT(case_id,assignee,kind,step) DO UPDATE SET state='done',acted_at=excluded.acted_at",(uid(),account_set_id,case['id'],a['name'],role,now_iso()))
        return {'id':ident2,'state':state}
    return command(c,a,account_set_id,'followup:'+ident,p,apply)

@router.get('/audit-logs')
def audits(request:Request,account_set_id:str='default',module:str|None=None,record_id:str|None=None,limit:int=Query(50,ge=1,le=200),offset:int=Query(0,ge=0),c=Depends(db)):
    a=access(c,request,account_set_id,READ)
    where=['account_set_id=?',"module<>'fw_preferences'"];params=[account_set_id]
    if 'director' not in a['roles']: where.append("module NOT IN ('fw_book_roles','fw_sync_connections','fw_source_identities','fw_sync_mappings')")
    if module: where.append('module=?');params.append(module)
    if record_id: where.append('record_id=?');params.append(record_id)
    records=[dict(r) for r in c.execute('SELECT * FROM audit_logs WHERE '+' AND '.join(where)+' ORDER BY id DESC LIMIT ? OFFSET ?',(*params,limit,offset))]
    for r in records:
        for k in ('before_data','after_data'): r[k]=json.loads(r[k]) if r[k] else None
    return {'items':records}

@router.get('/workflow/summary')
def workflow_summary(request:Request,account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id)
    tasks=valid_tasks(c,a,account_set_id)
    todo=[r for r in tasks if r['kind']=='action' and r['state']=='open'];unread=[r for r in tasks if r['kind']=='notice' and not r['read_at']]
    return {'todo':len(todo),'unread':len(unread),'badge':len({r['case_id'] for r in todo+unread})}
def valid_tasks(c,a,book):
    return [dict(r) for r in c.execute('SELECT t.*,w.category,w.title,w.subject_id,w.initiated_by,w.state AS case_state FROM fw_workflow_tasks t JOIN fw_workflow_cases w ON w.id=t.case_id AND w.account_set_id=t.account_set_id WHERE t.account_set_id=? AND t.assignee=? ORDER BY t.created_at DESC,t.rowid DESC',(book,a['name'])) if not r['required_role'] or r['required_role'] in a['roles']]

@router.get('/workflow/items')
def workflow_items(request:Request,account_set_id:str='default',view:str='todo',category:str='',initiator:str='',status:str='',limit:int=Query(50,ge=1,le=200),offset:int=Query(0,ge=0),c=Depends(db)):
    a=access(c,request,account_set_id)
    if view=='initiated':
        items=[dict(r) for r in c.execute('SELECT *,id AS case_id,state AS case_state FROM fw_workflow_cases WHERE account_set_id=? AND initiated_by=? ORDER BY created_at DESC,rowid DESC',(account_set_id,a['name']))]
    else:
        tests={'todo':lambda r:r['kind']=='action' and r['state']=='open','unread':lambda r:r['kind']=='notice' and not r['read_at'],
            'done':lambda r:r['kind']=='action' and r['state']=='done','read':lambda r:r['kind']=='notice' and bool(r['read_at'])}
        if view not in tests: raise HTTPException(422,'流程视图无效')
        items=[r for r in valid_tasks(c,a,account_set_id) if tests[view](r)]
    items=[r for r in items if (not category or r['category']==category) and (not initiator or initiator in r['initiated_by']) and (not status or r['case_state']==status)]
    return {'items':items[offset:offset+limit],'total':len(items)}

@router.get('/workflow/cases/{ident}')
def case_detail(ident:str,request:Request,account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id);case=row(c,'fw_workflow_cases',account_set_id,ident)
    if case['initiated_by']!=a['name'] and not any(t['case_id']==ident for t in valid_tasks(c,a,account_set_id)) and 'director' not in a['roles']: raise HTTPException(404,'流程不存在或无权查看')
    out=dict(case);payload=json.loads(out.pop('payload_json'))
    if case['category'] in ('ledger','reimbursement','ledger_change','invoice_followup'):
        item=row(c,'fw_ledger_items',account_set_id,case['subject_id'])
        if can_ledger(a,item): out['subject']=ledger_out(c,item)
    elif case['category'] in ('voucher','reversal','vat') and a['roles']&READ:
        out['payload']=payload
        if case['category'] in ('voucher','reversal'): out['subject']=voucher_dict(c,row(c,'vouchers',account_set_id,int(case['subject_id'])))
    return out

@router.post('/workflow/tasks/{ident}/read')
def read_task(ident:str,request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id)
    def apply():
        task=row(c,'fw_workflow_tasks',account_set_id,ident)
        if task['assignee']!=a['name'] or task['kind']!='notice': raise HTTPException(404,'通知不存在')
        c.execute('UPDATE fw_workflow_tasks SET read_at=COALESCE(read_at,?) WHERE id=?',(now_iso(),ident))
        return {'read':True}
    return command(c,a,account_set_id,'notice_read:'+ident,p,apply)

@router.post('/workflow/tasks/{ident}/actions')
def act_task(ident:str,request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id)
    def apply():
        task=row(c,'fw_workflow_tasks',account_set_id,ident);case=row(c,'fw_workflow_cases',account_set_id,task['case_id'])
        if task['assignee']!=a['name'] or task['required_role'] not in a['roles']: raise HTTPException(403,'当前账号不能处理该任务')
        if task['state']!='open' or case['state']!='pending' or task['step']!=case['step']: raise HTTPException(409,'任务已处理或已进入下一环节')
        decision=p.get('decision');note=str(p.get('note','')).strip()[:1000]
        if decision not in ('approve','reject','resolve'): raise HTTPException(422,'处理动作无效')
        if decision=='reject' and not note: raise HTTPException(422,'驳回请填写原因')
        category=case['category'];subject=case['subject_id'];payload=json.loads(case['payload_json'])
        if (category=='reconcile')!=(decision=='resolve'): raise HTTPException(422,'此流程不支持该处理动作')
        if task['required_role']=='director' and a['name'] in (case['made_by'],case['initiated_by']): raise HTTPException(403,'不能终审本人制单或发起的申请')
        if category in ('ledger','ledger_change','reimbursement'):
            item=row(c,'fw_ledger_items',account_set_id,subject)
            if item['version']!=case['subject_version'] or item['status']!='submitted': raise HTTPException(409,'单据版本或状态已变化')
            period_open(c,account_set_id,item['business_date'])
            if category=='reimbursement' and case['step']==1 and decision=='approve':
                assign(c,account_set_id,case['id'],'director',2,(case['made_by'],case['initiated_by']))
                c.execute('UPDATE fw_workflow_cases SET step=2 WHERE id=?',(case['id'],))
                finish_tasks(c,case['id'],1,ident)
                return {'state':'pending','step':2}
            c.execute('UPDATE fw_ledger_items SET status=?,version=version+1 WHERE id=?',('rejected' if decision=='reject' else 'approved',subject))
        elif category=='voucher':
            v=row(c,'vouchers',account_set_id,int(subject))
            if v['version']!=case['subject_version'] or v['reviewed_at'] or v['status'] not in ('DRAFT','PENDING'): raise HTTPException(409,'凭证已变化')
            period_open(c,account_set_id,v['voucher_date'])
            if decision=='approve':
                from .vouchers import _validate_postable
                _validate_postable(c,account_set_id,v['id'])
                c.execute("UPDATE vouchers SET status='PENDING',reviewed_at=?,reviewed_by=?,version=version+1 WHERE id=?",(now_iso(),a['name'],v['id']))
        elif category=='reversal':
            if decision=='approve': apply_reversal(c,a,account_set_id,int(subject),{**payload,'requester':case['initiated_by']})
        elif category=='vat':
            closing=row(c,'fw_vat_closings',account_set_id,subject)
            period_open(c,account_set_id,closing['period']+'-01')
            preview=vat_basis(c,account_set_id,closing['period'],json.loads(closing['basis_json']).get('opening_credit'))
            if preview['basis_hash']!=closing['basis_hash'] or closing['is_stale']: raise HTTPException(409,'税务基础已变化，请取消后重新计算')
            if decision=='approve':
                if closing['voucher_id']:
                    c.execute('UPDATE vouchers SET reviewed_at=?,reviewed_by=?,version=version+1 WHERE id=?',(now_iso(),a['name'],closing['voucher_id']))
                    c.execute("UPDATE fw_vat_closings SET status='approved' WHERE id=?",(subject,))
                else: c.execute("UPDATE fw_vat_closings SET status='confirmed_no_voucher' WHERE id=?",(subject,))
            else: c.execute("UPDATE fw_vat_closings SET status='draft' WHERE id=?",(subject,))
        elif category=='reconcile':
            if decision!='resolve': raise HTTPException(422,'核对任务请选择处理完成')
            if not note: raise HTTPException(422,'请填写核对说明')
            c.execute("UPDATE fw_reconcile_alerts SET status='resolved',version=version+1 WHERE id=? AND account_set_id=?",(subject,account_set_id))
            c.execute('INSERT INTO fw_reconcile_events(id,account_set_id,alert_id,action,note,actor) VALUES(?,?,?,?,?,?)',(uid(),account_set_id,subject,'resolved',note,a['name']))
        elif category in ('period_close','period_unlock'):
            current=c.execute('SELECT * FROM accounting_periods WHERE account_set_id=? AND period=?',(account_set_id,subject)).fetchone()
            if not current or current['version']!=case['subject_version']: raise HTTPException(409,'账期状态已变化')
            if decision=='approve':
                if category=='period_close':
                    check=period_checks(c,account_set_id,subject)
                    if not check['passed']: raise HTTPException(409,{'message':'结账检查未通过','checks':check})
                    snapshot=[voucher_dict(c,v) for v in c.execute("SELECT * FROM vouchers WHERE account_set_id=? AND substr(voucher_date,1,7)=? AND status IN ('POSTED','REVERSED') ORDER BY id",(account_set_id,subject))]
                    c.execute('INSERT INTO fw_period_archives(id,account_set_id,period,snapshot_json,source_hash,closed_by) VALUES(?,?,?,?,?,?)',(uid(),account_set_id,subject,js(snapshot),digest(snapshot),a['name']))
                    c.execute('UPDATE accounting_periods SET is_locked=1,locked_at=?,lock_reason=?,version=version+1 WHERE account_set_id=? AND period=?',(now_iso(),payload['reason'],account_set_id,subject))
                else:
                    if c.execute('SELECT 1 FROM accounting_periods WHERE account_set_id=? AND period>? AND is_locked=1',(account_set_id,subject)).fetchone(): raise HTTPException(409,'请先从最近月份开始解锁')
                    c.execute('UPDATE accounting_periods SET is_locked=0,locked_at=NULL,lock_reason=?,version=version+1 WHERE account_set_id=? AND period=?',(payload['reason'],account_set_id,subject))
                    c.execute("DELETE FROM app_records WHERE account_set_id=? AND store_name='Workpapers' AND record_id=?",(account_set_id,'closed:'+subject))
        else: raise HTTPException(422,'请跳转到源单据处理此任务')
        state='rejected' if decision=='reject' else 'resolved' if decision=='resolve' else 'approved'
        c.execute('UPDATE fw_workflow_cases SET state=?,decided_by=?,decided_at=?,note=? WHERE id=?',(state,a['name'],now_iso(),note,case['id']))
        finish_tasks(c,case['id'],case['step'],ident)
        notice(c,account_set_id,case['id'],case['initiated_by'],case['step'])
        return {'state':state,'caseId':case['id']}
    return command(c,a,account_set_id,'task_act:'+ident,p,apply)
def finish_tasks(c,case,step,task):
    c.execute("UPDATE fw_workflow_tasks SET state=CASE WHEN id=? THEN 'done' ELSE 'cancelled' END,acted_at=? WHERE case_id=? AND step=? AND kind='action' AND state='open'",(task,now_iso(),case,step))

@router.post('/vouchers/{ident}/submit')
def submit_voucher(ident:int,request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,FIN)
    def apply():
        v=row(c,'vouchers',account_set_id,ident);version(p,v)
        if not v['created_by']: raise HTTPException(409,'历史凭证制单身份未知，请先核验归属')
        if v['reviewed_at'] or v['status'] not in ('DRAFT','PENDING'): raise HTTPException(409,'当前状态不能提交')
        period_open(c,account_set_id,v['voucher_date'])
        from .vouchers import _validate_postable
        _validate_postable(c,account_set_id,ident)
        case=create_case(c,account_set_id,'voucher',ident,v['version'],v['voucher_no']+' · '+v['summary'],a['name'],v['created_by'])
        return {'caseId':case,'status':'submitted'}
    return command(c,a,account_set_id,'voucher_submit:'+str(ident),p,apply)

@router.post('/vouchers/{ident}/reversals')
def reversal_request(ident:int,request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,FIN)
    def apply():
        v=row(c,'vouchers',account_set_id,ident);version(p,v)
        if v['status']!='POSTED' or v['reversed_by_id']: raise HTTPException(409,'原凭证不是可冲销的已过账凭证')
        reason=required(p,'reason',1000);day=period_open(c,account_set_id,required(p,'voucher_date')).isoformat()
        case=create_case(c,account_set_id,'reversal',ident,v['version'],'红冲 '+v['voucher_no'],a['name'],a['name'],{'date':day,'reason':reason})
        return {'caseId':case}
    return command(c,a,account_set_id,'reversal_request:'+str(ident),p,apply)
def apply_reversal(c,a,book,ident,p):
    v=row(c,'vouchers',book,ident)
    if v['status']!='POSTED' or v['reversed_by_id']: raise HTTPException(409,'原凭证已冲销或状态已变化')
    period_open(c,book,p['date'])
    # Preserve specialized stock/return controls of the existing reversal implementation.
    if str(v['legacy_id'] or '').startswith(('fw-return:','fw-refund:','fw-fx:')): raise HTTPException(409,'关联退货／退款／调汇业务请在原模块处理')
    linked=c.execute('SELECT 1 FROM business_voucher_links WHERE account_set_id=? AND voucher_id=?',(book,ident)).fetchone()
    if linked: raise HTTPException(409,'该凭证关联业务库存，需先在原业务模块完成反向处理')
    entries=c.execute('SELECT * FROM voucher_entries WHERE account_set_id=? AND voucher_id=?',(book,ident)).fetchall()
    if len(entries)<2 or sum(e['debit_amount'] for e in entries)!=sum(e['credit_amount'] for e in entries): raise HTTPException(422,'原凭证分录不完整')
    no=generate_voucher_no(c,p['date'],prefix='红字',account_set_id=book)
    new=c.execute("INSERT INTO vouchers(account_set_id,voucher_no,voucher_date,summary,status,attachment_urls,posted_at,reviewed_at,created_by,reviewed_by,reverse_of_id) VALUES(?,?,?,?,'POSTED','[]',?,?,?,?,?)",(book,no,p['date'],p['reason'],now_iso(),now_iso(),p['requester'],a['name'],ident)).lastrowid
    c.execute('UPDATE vouchers SET source_type=? WHERE id=?',(v['source_type'],new))
    for e in entries:
        entry_id=c.execute('INSERT INTO voucher_entries(account_set_id,voucher_id,account_code,contact_id,aux_name,summary,debit_amount,credit_amount) VALUES(?,?,?,?,?,?,?,?)',(book,new,e['account_code'],e['contact_id'],e['aux_name'],p['reason'],-e['debit_amount'],-e['credit_amount'])).lastrowid
        c.execute('INSERT INTO fw_entry_dimensions(account_set_id,entry_id,kind,dimension_id) SELECT account_set_id,?,kind,dimension_id FROM fw_entry_dimensions WHERE account_set_id=? AND entry_id=?',(entry_id,book,e['id']))
    c.execute("UPDATE vouchers SET status='REVERSED',reversed_by_id=?,reversed_at=?,version=version+1 WHERE id=?",(new,now_iso(),ident))
    c.execute("UPDATE fw_vat_closings SET status='superseded',is_stale=1 WHERE account_set_id=? AND voucher_id=?",(book,ident))
    c.execute('UPDATE fw_vat_closings SET is_stale=1 WHERE account_set_id=? AND period>(SELECT period FROM fw_vat_closings WHERE account_set_id=? AND voucher_id=?)',(book,book,ident))
    return new

def check_vat_posting(c,book,ident):
    closing=c.execute("SELECT * FROM fw_vat_closings WHERE account_set_id=? AND voucher_id=? AND status NOT IN ('cancelled','superseded')",(book,ident)).fetchone()
    if not closing: return
    if closing['status']!='approved' or closing['is_stale']: raise HTTPException(409,'增值税结转尚未终审或已失效')
    current=vat_basis(c,book,closing['period'],json.loads(closing['basis_json']).get('opening_credit'))
    if current['basis_hash']!=closing['basis_hash']: raise HTTPException(409,'税务基础已变化，禁止过账旧结转')

@router.post('/ledgers/{ident}/voucher')
def ledger_voucher(ident:str,request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,FIN)
    def apply():
        ledger=row(c,'fw_ledger_items',account_set_id,ident);version(p,ledger)
        if ledger['status']!='approved': raise HTTPException(409,'请先完成业务审批')
        if ledger['voucher_id']: return {'voucher_id':ledger['voucher_id']}
        day=period_open(c,account_set_id,p.get('voucher_date') or ledger['business_date']).isoformat()
        from ..schemas import VoucherEntryIn
        from .vouchers import _validate_entry_payload,_insert_entries
        try: models=[VoucherEntryIn.model_validate(e) for e in p.get('entries',[])]
        except Exception: raise HTTPException(422,'凭证分录字段无效')
        entries,debit,credit=_validate_entry_payload(c,models,account_set_id)
        if debit!=ledger['amount_cents']: raise HTTPException(422,'凭证借方合计须等于台账含税金额')
        no=generate_voucher_no(c,day,account_set_id=account_set_id)
        vid=c.execute("INSERT INTO vouchers(account_set_id,legacy_id,voucher_no,voucher_date,summary,status,created_by) VALUES(?,?,?,?,?,'DRAFT',?)",(account_set_id,'ledger:'+ident,no,day,ledger['doc_no'],a['name'])).lastrowid
        _insert_entries(c,account_set_id,vid,entries)
        c.execute('UPDATE fw_ledger_items SET voucher_id=?,version=version+1 WHERE id=?',(vid,ident))
        return {'voucher_id':vid,'voucher_no':no}
    return command(c,a,account_set_id,'ledger_voucher:'+ident,p,apply)

@router.post('/workflow/cases/{ident}/cancel')
def cancel_case(ident:str,request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id)
    def apply():
        case=row(c,'fw_workflow_cases',account_set_id,ident)
        if case['initiated_by']!=a['name']: raise HTTPException(403,'仅发起人可以撤回')
        if case['state']!='pending': raise HTTPException(409,'流程已处理')
        if case['category'] not in ('voucher','ledger','reimbursement','ledger_change','reversal'): raise HTTPException(409,'请在源业务页面办理取消')
        c.execute("UPDATE fw_workflow_cases SET state='cancelled',note=? WHERE id=?",(required(p,'reason',1000),ident))
        c.execute("UPDATE fw_workflow_tasks SET state='cancelled' WHERE case_id=? AND kind='action' AND state='open'",(ident,))
        if case['category'] in ('ledger','reimbursement','ledger_change'):
            c.execute("UPDATE fw_ledger_items SET status='rejected',version=version+1 WHERE account_set_id=? AND id=?",(account_set_id,case['subject_id']))
        return {'state':'cancelled'}
    return command(c,a,account_set_id,'case_cancel:'+ident,p,apply)

def validate_account(c,book,p,code=None):
    code=code or required(p,'code',10)
    if not re.fullmatch(r'\d{4}(\d{2}){0,3}',code): raise HTTPException(422,'科目编码须为 4／6／8／10 位数字')
    parent=code[:-2] if len(code)>4 else None
    if p.get('parent_code') not in (None,parent): raise HTTPException(422,'上级编码须与科目编码前缀一致')
    if parent:
        found=c.execute('SELECT * FROM accounts WHERE account_set_id=? AND code=? AND is_active=1',(book,parent)).fetchone()
        if not found: raise HTTPException(422,'上级科目不存在或已停用')
        category=found['category'];side=p.get('direction',found['direction'])
    else: category=p.get('category');side=p.get('direction')
    if category not in {'资产','负债','权益','成本','损益'} or side not in {'借','贷'}: raise HTTPException(422,'科目类别或余额方向无效')
    return code,parent,category,side

@router.get('/accounts')
def accounts(request:Request,account_set_id:str='default',c=Depends(db)):
    access(c,request,account_set_id,READ)
    return {'items':[{**dict(r),'level':1+(len(r['code'])-4)//2,'dimensions':[dict(d) for d in c.execute('SELECT kind,required FROM fw_account_dimensions WHERE account_set_id=? AND account_code=?',(account_set_id,r['code']))]} for r in c.execute('SELECT * FROM accounts WHERE account_set_id=? ORDER BY code',(account_set_id,))]}

@router.post('/accounts')
def account_create(request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,FIN)
    def apply():
        code,parent,category,side=validate_account(c,account_set_id,p)
        c.execute('INSERT INTO accounts(account_set_id,code,name,category,direction,parent_code,is_custom) VALUES(?,?,?,?,?,?,1)',(account_set_id,code,required(p,'name',120),category,side,parent))
        set_account_dims(c,account_set_id,code,p.get('dimensions',[]))
        return {'code':code,'name':p['name']}
    return command(c,a,account_set_id,'account_create',p,apply)
def set_account_dims(c,book,code,dimensions):
    if not isinstance(dimensions,list) or len(dimensions)>5: raise HTTPException(422,'辅助核算设置无效')
    seen=set()
    c.execute('DELETE FROM fw_account_dimensions WHERE account_set_id=? AND account_code=?',(book,code))
    for d in dimensions:
        if not isinstance(d,dict) or d.get('kind') not in {'customer','supplier','department','project','bank_account'} or d['kind'] in seen: raise HTTPException(422,'辅助核算类型重复或无效')
        seen.add(d['kind']);c.execute('INSERT INTO fw_account_dimensions VALUES(?,?,?,?)',(book,code,d['kind'],int(bool(d.get('required')))))

@router.patch('/accounts/{code}')
def account_edit(code:str,request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,FIN)
    def apply():
        old=c.execute('SELECT * FROM accounts WHERE account_set_id=? AND code=?',(account_set_id,code)).fetchone()
        if not old: raise HTTPException(404,'科目不存在')
        version(p,old)
        active=int(bool(p.get('is_active',old['is_active'])))
        if not active and c.execute('SELECT 1 FROM accounts WHERE account_set_id=? AND parent_code=? AND is_active=1',(account_set_id,code)).fetchone(): raise HTTPException(409,'请先停用下级科目')
        if active and old['parent_code'] and not c.execute('SELECT 1 FROM accounts WHERE account_set_id=? AND code=? AND is_active=1',(account_set_id,old['parent_code'])).fetchone(): raise HTTPException(409,'请先启用上级科目')
        c.execute('UPDATE accounts SET name=?,is_active=?,version=version+1 WHERE account_set_id=? AND code=?',(required(p,'name',120),active,account_set_id,code))
        if 'dimensions' in p: set_account_dims(c,account_set_id,code,p['dimensions'])
        return {'code':code,'version':old['version']+1,'is_active':bool(active)}
    return command(c,a,account_set_id,'account_edit:'+code,p,apply)

@router.get('/trial-balance')
def tree_balance(request:Request,period:str,account_set_id:str='default',level:int=Query(3,ge=1,le=4),dimension_id:str|None=None,c=Depends(db)):
    access(c,request,account_set_id,READ);validate_period(period)
    all_accounts=[dict(r) for r in c.execute('SELECT * FROM accounts WHERE account_set_id=? ORDER BY code',(account_set_id,))]
    if dimension_id:
        if not c.execute('SELECT 1 FROM fw_dimensions WHERE account_set_id=? AND id=?',(account_set_id,dimension_id)).fetchone(): raise HTTPException(404,'辅助对象不存在')
    params=[period+'-01',period+'-01',period+'-01',period+'-01',account_set_id,period+'-01']
    clause=''
    if dimension_id:
        clause=' AND EXISTS(SELECT 1 FROM fw_entry_dimensions d WHERE d.account_set_id=e.account_set_id AND d.entry_id=e.id AND d.dimension_id=?)';params.append(dimension_id)
    flows={r['account_code']:dict(r) for r in c.execute('''SELECT e.account_code,
      SUM(CASE WHEN v.voucher_date<? THEN e.debit_amount-e.credit_amount ELSE 0 END) AS prior,
      SUM(CASE WHEN v.voucher_date>=? THEN e.debit_amount ELSE 0 END) AS debit,
      SUM(CASE WHEN v.voucher_date>=? THEN e.credit_amount ELSE 0 END) AS credit,
      SUM(CASE WHEN v.voucher_date<? THEN 0 ELSE 0 END) AS unused
      FROM voucher_entries e JOIN vouchers v ON v.id=e.voucher_id AND v.account_set_id=e.account_set_id
      WHERE e.account_set_id=? AND v.status IN ('POSTED','REVERSED') AND v.voucher_date<date(?,'+1 month')'''+clause+' GROUP BY e.account_code',params)}
    direct={};totals={'opening_debit':0,'opening_credit':0,'debit':0,'credit':0,'ending_debit':0,'ending_credit':0}
    for r in all_accounts:
        f=flows.get(r['code'],{});opening=(0 if dimension_id else r['initial_debit']-r['initial_credit'])+(f.get('prior') or 0)
        debit=f.get('debit') or 0;credit=f.get('credit') or 0;ending=opening+debit-credit
        value={'opening_debit':max(opening,0),'opening_credit':max(-opening,0),'debit':debit,'credit':credit,'ending_debit':max(ending,0),'ending_credit':max(-ending,0)}
        direct[r['code']]=value
        for k in totals: totals[k]+=value[k]
    items=[]
    for r in all_accounts:
        depth=1+(len(r['code'])-4)//2
        if depth>level: continue
        children=[x for code,x in direct.items() if code.startswith(r['code'])]
        sums={k:sum(x[k] for x in children) for k in totals}
        items.append({'code':r['code'],'name':r['name'],'level':depth,'parent_code':r['parent_code'],'is_active':bool(r['is_active']),
          'has_children':any(x['parent_code']==r['code'] for x in all_accounts),'direct':{k:cents_to_yuan(v) for k,v in direct[r['code']].items()},**{k:cents_to_yuan(v) for k,v in sums.items()}})
    return {'items':items,'totals':{k:cents_to_yuan(v) for k,v in totals.items()},'period':period,
        'opening_dimensions_complete':not dimension_id,'note':'辅助维度期初尚未分配，维度视图仅列已关联凭证；总表含完整期初。' if dimension_id else ''}

@router.get('/reconcile/alerts')
def alerts(request:Request,account_set_id:str='default',status:str='',limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0),c=Depends(db)):
    access(c,request,account_set_id,READ)
    records=c.execute("SELECT * FROM fw_reconcile_alerts WHERE account_set_id=? AND (?='' OR status=?) ORDER BY last_seen_at DESC,rowid DESC LIMIT ? OFFSET ?",(account_set_id,status,status,limit,offset)).fetchall()
    return {'items':[{**dict(r),'evidence':json.loads(r['evidence_json'])} for r in records]}

@router.post('/reconcile/alerts/{ident}/actions')
def resolve_alert(ident:str,request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,FIN)
    def apply():
        old=row(c,'fw_reconcile_alerts',account_set_id,ident);version(p,old)
        state=p.get('status')
        if state not in {'investigating','resolved','dismissed'}: raise HTTPException(422,'处理状态无效')
        note=required(p,'note',1000)
        c.execute('UPDATE fw_reconcile_alerts SET status=?,version=version+1,last_seen_at=? WHERE id=?',(state,now_iso(),ident))
        c.execute('INSERT INTO fw_reconcile_events(id,account_set_id,alert_id,action,note,actor) VALUES(?,?,?,?,?,?)',(uid(),account_set_id,ident,state,note,a['name']))
        if state in ('resolved','dismissed'):
            cases=c.execute("SELECT id FROM fw_workflow_cases WHERE account_set_id=? AND category='reconcile' AND subject_id=? AND state='pending'",(account_set_id,ident)).fetchall()
            for case in cases:
                c.execute("UPDATE fw_workflow_cases SET state='resolved',decided_by=?,decided_at=?,note=? WHERE id=?",(a['name'],now_iso(),note,case['id']))
                c.execute("UPDATE fw_workflow_tasks SET state='done',acted_at=? WHERE case_id=? AND kind='action' AND state='open'",(now_iso(),case['id']))
        return {'id':ident,'status':state,'version':old['version']+1}
    return command(c,a,account_set_id,'alert_action:'+ident,p,apply)

@router.post('/reconcile/matches')
def match_records(request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,FIN)
    def apply():
        docs=[row(c,'fw_ledger_items',account_set_id,required(p,key)) for key in ('po_id','receipt_id','invoice_id')]
        if [d['kind'] for d in docs]!=['purchase_order','receipt','invoice']: raise HTTPException(422,'请选择采购单、入库单、发票对应行')
        if any(d['status'] not in ('approved','posted') for d in docs): raise HTTPException(409,'请先核对并批准三种单据')
        if not docs[0]['supplier_id'] or not docs[0]['item_code'] or len({d['supplier_id'] for d in docs})!=1 or len({d['item_code'] for d in docs})!=1: raise HTTPException(422,'供应商与商品必须明确且一致')
        try: qty=Decimal(str(p.get('quantity','0')))
        except InvalidOperation: raise HTTPException(422,'数量无效')
        if not qty.is_finite() or qty<=0 or qty.as_tuple().exponent < -6: raise HTTPException(422,'分配数量须大于零且最多六位小数')
        net=money_to_cents(str(p.get('net_amount','0')));tax=money_to_cents(str(p.get('tax_amount','0')))
        if net<0 or tax<0: raise HTTPException(422,'分配金额不能为负')
        for field,d in zip(('po_id','receipt_id','invoice_id'),docs):
            allocated=c.execute(f'SELECT quantity,net_cents,tax_cents FROM fw_match_allocations WHERE account_set_id=? AND {field}=?',(account_set_id,d['id'])).fetchall()
            used_qty=sum((Decimal(r['quantity']) for r in allocated),Decimal(0));doc_qty=Decimal(d['quantity'])
            if doc_qty<=0 or used_qty+qty>doc_qty or sum(r['net_cents'] for r in allocated)+net>d['amount_cents']-d['tax_cents'] or sum(r['tax_cents'] for r in allocated)+tax>d['tax_cents']: raise HTTPException(409,'累计匹配超过单据数量或金额')
            def share(total,count):return int((Decimal(total)*count/doc_qty).quantize(Decimal(1),rounding=ROUND_HALF_UP))
            if net!=share(d['amount_cents']-d['tax_cents'],used_qty+qty)-share(d['amount_cents']-d['tax_cents'],used_qty) or tax!=share(d['tax_cents'],used_qty+qty)-share(d['tax_cents'],used_qty): raise HTTPException(409,'三单单价或税额不一致，请核对后再分配')
        ident=uid();c.execute('INSERT INTO fw_match_allocations(id,account_set_id,po_id,receipt_id,invoice_id,quantity,net_cents,tax_cents,created_by) VALUES(?,?,?,?,?,?,?,?,?)',(ident,account_set_id,*[d['id'] for d in docs],str(qty),net,tax,a['name']))
        return {'id':ident,'matched':True}
    return command(c,a,account_set_id,'three_way_match',p,apply)

def vat_basis(c,book,period,opening_credit=None):
    validate_period(period)
    b=c.execute('SELECT taxpayer_type FROM account_sets WHERE id=?',(book,)).fetchone()
    if b['taxpayer_type']!='general': raise HTTPException(422,'小规模纳税人不适用进销项抵扣结转')
    prior_date=date.fromisoformat(period+'-01');prior=(date(prior_date.year-1,12,1) if prior_date.month==1 else date(prior_date.year,prior_date.month-1,1)).strftime('%Y-%m')
    previous=c.execute("SELECT * FROM fw_vat_closings WHERE account_set_id=? AND period=? AND status IN ('posted','confirmed_no_voucher') AND is_stale=0",(book,prior)).fetchone()
    if previous: carry=previous['credit_cents'];opening=None
    else:
        earlier=c.execute("SELECT 1 FROM fw_vat_closings WHERE account_set_id=? AND period<? AND status NOT IN ('cancelled','superseded')",(book,period)).fetchone()
        if earlier: raise HTTPException(409,'上月结转未确认或已失效，请先处理')
        carry=money_to_cents(str(opening_credit or '0'));opening=cents_to_yuan(carry)
        if carry<0: raise HTTPException(422,'期初留抵不能为负')
    entries=[dict(r) for r in c.execute("SELECT e.id,e.account_code,e.debit_amount,e.credit_amount,v.id AS voucher_id FROM voucher_entries e JOIN vouchers v ON v.id=e.voucher_id AND v.account_set_id=e.account_set_id WHERE e.account_set_id=? AND v.voucher_date>=? AND v.voucher_date<date(?,'+1 month') AND v.status IN ('POSTED','REVERSED') AND e.account_code LIKE '2221%' AND COALESCE(v.source_type,'')<>'VAT_CLOSE' ORDER BY e.id",(book,period+'-01',period+'-01'))]
    def net(prefix,credit=False): return sum((e['credit_amount']-e['debit_amount'] if credit else e['debit_amount']-e['credit_amount']) for e in entries if e['account_code'].startswith(prefix))
    output=net('22210102',True)-net('22210106');inputs=net('22210101')-net('22210105',True)
    unclassified=any(e['account_code'] in ('2221','222101') for e in entries)
    special=any(e['account_code'].startswith(('222103','22210104','22210107','22210108','22210109','22210110')) for e in entries)
    basis={'entries':entries,'prior_id':previous['id'] if previous else None,'prior_hash':previous['basis_hash'] if previous else None,'carry_cents':carry,'opening_credit':opening,'policy':1}
    diff=output-inputs-carry
    return {'period':period,'output':cents_to_yuan(output),'input':cents_to_yuan(inputs),'previous_credit':cents_to_yuan(carry),'difference':cents_to_yuan(diff),'due':cents_to_yuan(max(diff,0)),'credit':cents_to_yuan(max(-diff,0)),
      'requires_manual_review':special or unclassified,'review_reason':'存在汇总税额、已缴／预缴／减免等特殊事项，请先核对明细和适用口径' if special or unclassified else '',
      'basis_hash':digest(basis),'basis':basis}

@router.post('/tax/vat/{period}/preview')
def vat_preview(period:str,request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    access(c,request,account_set_id,READ)
    result=vat_basis(c,account_set_id,period,p.get('opening_credit'))
    existing=c.execute("SELECT * FROM fw_vat_closings WHERE account_set_id=? AND period=? AND status NOT IN ('cancelled','superseded')",(account_set_id,period)).fetchone()
    result['closing']=dict(existing) if existing else None
    result.pop('basis')
    return result

@router.post('/tax/vat/{period}/generate')
def vat_generate(period:str,request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,FIN)
    def apply():
        period_open(c,account_set_id,period+'-01')
        preview=vat_basis(c,account_set_id,period,p.get('opening_credit'))
        if p.get('basisHash')!=preview['basis_hash']: raise HTTPException(409,'基础数据已变化，请重新预览')
        if preview['requires_manual_review']: raise HTTPException(409,preview['review_reason'])
        old=c.execute("SELECT * FROM fw_vat_closings WHERE account_set_id=? AND period=? AND status NOT IN ('cancelled','superseded')",(account_set_id,period)).fetchone()
        if old:
            if old['basis_hash']!=preview['basis_hash']: raise HTTPException(409,'已有结转基础不同，请先取消或申请红冲')
            return dict(old)
        ident=uid();due=money_to_cents(preview['due']);voucher=None
        if due:
            for code in ('22210103','222102'):
                if not c.execute('SELECT 1 FROM accounts WHERE account_set_id=? AND code=? AND is_active=1',(account_set_id,code)).fetchone(): raise HTTPException(422,'结转科目未启用')
            d=date.fromisoformat(period+'-01');day=f'{period}-{calendar.monthrange(d.year,d.month)[1]}'
            voucher=c.execute("INSERT INTO vouchers(account_set_id,legacy_id,voucher_no,voucher_date,summary,status,source_type,created_by) VALUES(?,?,?,?,?,'DRAFT','VAT_CLOSE',?)",(account_set_id,'vat:'+ident,generate_voucher_no(c,day,account_set_id=account_set_id),day,'增值税期末结转 '+period,a['name'])).lastrowid
            c.executemany('INSERT INTO voucher_entries(account_set_id,voucher_id,account_code,summary,debit_amount,credit_amount) VALUES(?,?,?,?,?,?)',[(account_set_id,voucher,'22210103','转出未交增值税',due,0),(account_set_id,voucher,'222102','未交增值税',0,due)])
        revision=c.execute('SELECT COALESCE(MAX(version),0)+1 FROM fw_vat_closings WHERE account_set_id=? AND period=?',(account_set_id,period)).fetchone()[0]
        c.execute('INSERT INTO fw_vat_closings(id,account_set_id,period,version,output_cents,input_cents,previous_credit_cents,due_cents,credit_cents,basis_json,basis_hash,voucher_id,created_by,request_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(ident,account_set_id,period,revision,money_to_cents(preview['output']),money_to_cents(preview['input']),money_to_cents(preview['previous_credit']),due,money_to_cents(preview['credit']),js(preview['basis']),preview['basis_hash'],voucher,a['name'],p['requestKey']))
        return dict(row(c,'fw_vat_closings',account_set_id,ident))
    return command(c,a,account_set_id,'vat_generate:'+period,p,apply)

@router.post('/tax/vat/{ident}/submit')
def vat_submit(ident:str,request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,FIN)
    def apply():
        old=row(c,'fw_vat_closings',account_set_id,ident)
        period_open(c,account_set_id,old['period']+'-01')
        if old['status']!='draft': raise HTTPException(409,'结转已提交或已生效')
        basis=vat_basis(c,account_set_id,old['period'],json.loads(old['basis_json']).get('opening_credit'))
        if basis['basis_hash']!=old['basis_hash'] or old['is_stale']: raise HTTPException(409,'基础已变化，请取消后重新生成')
        case=create_case(c,account_set_id,'vat',ident,old['version'],'增值税结转 '+old['period'],a['name'],old['created_by'])
        c.execute("UPDATE fw_vat_closings SET status='submitted' WHERE id=?",(ident,))
        return {'caseId':case,'status':'submitted'}
    return command(c,a,account_set_id,'vat_submit:'+ident,p,apply)

@router.post('/tax/vat/{ident}/cancel')
def vat_cancel(ident:str,request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,FIN)
    def apply():
        old=row(c,'fw_vat_closings',account_set_id,ident)
        if old['status'] not in ('draft','submitted','approved'): raise HTTPException(409,'生效结转须办理红冲')
        if old['voucher_id']:
            v=row(c,'vouchers',account_set_id,old['voucher_id'])
            if v['status'] not in ('DRAFT','PENDING'): raise HTTPException(409,'凭证已过账，请申请红冲')
            c.execute("UPDATE vouchers SET status='VOIDED',version=version+1 WHERE id=?",(v['id'],))
        cases=c.execute("SELECT id FROM fw_workflow_cases WHERE account_set_id=? AND category='vat' AND subject_id=? AND state='pending'",(account_set_id,ident)).fetchall()
        for case in cases:
            c.execute("UPDATE fw_workflow_cases SET state='cancelled',note=? WHERE id=?",(required(p,'reason',1000),case['id']))
            c.execute("UPDATE fw_workflow_tasks SET state='cancelled' WHERE case_id=? AND kind='action'",(case['id'],))
        c.execute("UPDATE fw_vat_closings SET status='cancelled' WHERE id=?",(ident,))
        return {'status':'cancelled'}
    return command(c,a,account_set_id,'vat_cancel:'+ident,p,apply)

@router.get('/sync/connections')
def sync_connections(request:Request,account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,READ)
    records=[dict(r) for r in c.execute('SELECT * FROM fw_sync_connections WHERE account_set_id=? ORDER BY name',(account_set_id,))]
    for record in records:
        record['mapping']=json.loads(record.pop('mapping_json'))
        if 'director' not in a['roles']: record.pop('secret_ref',None)
        last=c.execute('SELECT * FROM fw_sync_runs WHERE connection_id=? ORDER BY started_at DESC,rowid DESC LIMIT 1',(record['id'],)).fetchone()
        record['last_run']={**dict(last),'stats':json.loads(last['stats_json'])} if last else None
    return {'items':records,'schedule':'Asia/Shanghai 02:17'}

@router.post('/sync/connections')
def save_connection(request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,{'director'})
    def apply():
        provider=p.get('provider');module=p.get('module')
        if provider not in ('feishu','google_sheets') or module not in ('warehouse','expense','asset'): raise HTTPException(422,'来源或模块无效')
        source=required(p,'source_ref');table=required(p,'table_ref')
        if not re.fullmatch(r'[A-Za-z0-9_-]{5,150}',source): raise HTTPException(422,'表格 Token／ID 格式无效')
        if provider=='feishu' and not re.fullmatch(r'tbl[A-Za-z0-9]{3,100}',table): raise HTTPException(422,'飞书 table_id 格式无效')
        if provider=='google_sheets' and any(ch in table for ch in '[]:/\\?*'): raise HTTPException(422,'Google 连接请填写完整工作表名称，不填写行区间')
        from ..secret_store import validate
        secret=required(p,'secret_ref');
        try: validate(secret)
        except ValueError as exc: raise HTTPException(422,str(exc))
        mapping=p.get('mapping')
        fields={'external_id','business_date','doc_no','doc_line','kind','amount','tax_amount','quantity','item_code','payment_kind','invoice_state','needs_invoice_followup','project_id','supplier_id','department_id','cashier_visible'}
        if not isinstance(mapping,dict) or set(mapping)-fields or any(not isinstance(v,str) or not v or len(v)>200 for v in mapping.values()) or not {'business_date','amount'}<=set(mapping): raise HTTPException(422,'字段映射至少包含 business_date 和 amount，值须为来源字段名')
        ident=p.get('id') or uid();old=c.execute('SELECT * FROM fw_sync_connections WHERE account_set_id=? AND id=?',(account_set_id,ident)).fetchone()
        if old:
            version(p,old)
            if (provider,source,table,module)!=(old['provider'],old['source_ref'],old['table_ref'],old['module']): raise HTTPException(409,'已有连接不能更换来源或模块，请新增连接')
            mapping_version=old['mapping_version']+int(js(mapping)!=old['mapping_json'])
            c.execute('UPDATE fw_sync_connections SET name=?,secret_ref=?,mapping_json=?,mapping_version=?,enabled=?,scheduled=?,version=version+1 WHERE id=?',(required(p,'name'),secret,js(mapping),mapping_version,int(bool(p.get('enabled',True))),int(bool(p.get('scheduled'))),ident))
        else:
            mapping_version=1
            c.execute('INSERT INTO fw_sync_connections(id,account_set_id,name,provider,module,source_ref,table_ref,secret_ref,mapping_json,enabled,scheduled) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(ident,account_set_id,required(p,'name'),provider,module,source,table,secret,js(mapping),int(bool(p.get('enabled',True))),int(bool(p.get('scheduled')))))
        c.execute('INSERT OR IGNORE INTO fw_sync_mappings(account_set_id,connection_id,version,mapping_json,created_by) VALUES(?,?,?,?,?)',(account_set_id,ident,mapping_version,js(mapping),a['name']))
        return {'id':ident,'mapping_version':mapping_version,'version':old['version']+1 if old else 1}
    return command(c,a,account_set_id,'sync_connection',p,apply)

@router.put('/sync/credentials')
def credentials(request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,{'director'})
    from ..secret_store import put_secret
    try: put_secret(account_set_id,required(p,'secret_ref'),p.get('credentials'))
    except ValueError as exc: raise HTTPException(422,str(exc))
    with c: c.execute("INSERT INTO audit_logs(account_set_id,module,record_id,operation,actor,after_data,request_id) VALUES(?,'credentials',?,'ROTATE',?,?,?)",(account_set_id,p['secret_ref'],a['name'],js({'configured':True}),required(p,'requestKey',120)))
    return {'configured':True}

@router.post('/sync/identities')
def bind_identity(request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,{'director'})
    def apply():
        connection=row(c,'fw_sync_connections',account_set_id,required(p,'connection_id'));name=required(p,'user_name',60)
        if connection['provider']!='feishu': raise HTTPException(422,'Google 普通行无法证明创建者，请使用系统内填报')
        if not c.execute('SELECT 1 FROM fw_book_roles WHERE account_set_id=? AND user_name=? AND active=1',(account_set_id,name)).fetchone(): raise HTTPException(422,'目标用户不是活跃账套成员')
        c.execute('INSERT INTO fw_source_identities VALUES(?,?,?,?,?) ON CONFLICT(account_set_id,connection_id,external_principal) DO UPDATE SET user_name=excluded.user_name,verified_by=excluded.verified_by',(account_set_id,connection['id'],required(p,'external_principal'),name,a['name']))
        return {'bound':True}
    return command(c,a,account_set_id,'bind_identity',p,apply)

def queue_sync(c,book,connection,key,actor,trigger='manual'):
    existing=c.execute('SELECT * FROM fw_sync_runs WHERE account_set_id=? AND connection_id=? AND request_key=?',(book,connection,key)).fetchone()
    if existing:
        if existing['status'] in ('queued','running') and existing['lease_until'] and existing['lease_until']<now_iso():
            from datetime import timedelta
            c.execute("UPDATE fw_sync_runs SET status='queued',lease_token=?,lease_until=? WHERE id=?",(uid(),(datetime.now(timezone.utc)+timedelta(minutes=20)).isoformat(),existing['id']))
            return dict(row(c,'fw_sync_runs',book,existing['id'])),True
        return dict(existing),False
    # Expired leases are terminalized; a new request receives a distinct fencing token.
    c.execute("UPDATE fw_sync_runs SET status='failed',error_summary='执行租约已过期，请重新同步',finished_at=? WHERE connection_id=? AND status IN ('queued','running') AND lease_until<?",(now_iso(),connection,now_iso()))
    running=c.execute("SELECT * FROM fw_sync_runs WHERE connection_id=? AND status IN ('queued','running')",(connection,)).fetchone()
    if running: return dict(running),False
    config=row(c,'fw_sync_connections',book,connection)
    if not config['enabled']: raise HTTPException(409,'连接已停用')
    from datetime import timedelta
    ident=uid();lease=(datetime.now(timezone.utc)+timedelta(minutes=20)).isoformat()
    c.execute("INSERT INTO fw_sync_runs(id,account_set_id,connection_id,request_key,trigger_type,initiated_by,status,mapping_version,lease_token,lease_until) VALUES(?,?,?,?,?,?,'queued',?,?,?)",(ident,book,connection,key,trigger,actor,config['mapping_version'],uid(),lease))
    return dict(row(c,'fw_sync_runs',book,ident)),True

@router.post('/sync/connections/{ident}/runs',status_code=202)
def sync_now(ident:str,request:Request,background_tasks:BackgroundTasks,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,FIN)
    with c:
        c.execute('BEGIN IMMEDIATE');run,new=queue_sync(c,account_set_id,ident,required(p,'requestKey',120),a['name'])
    if new:
        from ..sync_service import run_sync
        background_tasks.add_task(run_sync,run['id'])
    return {'run_id':run['id'],'status':run['status'],'reused':not new}

@router.get('/sync/runs/{ident}')
def sync_run(ident:str,request:Request,account_set_id:str='default',c=Depends(db)):
    access(c,request,account_set_id,READ)
    found=dict(row(c,'fw_sync_runs',account_set_id,ident));found['stats']=json.loads(found.pop('stats_json'));found.pop('lease_token',None)
    return found

@router.get('/entries')
def account_entries(request:Request,prefix:str,period:str,account_set_id:str='default',c=Depends(db)):
    access(c,request,account_set_id,READ);validate_period(period)
    if not re.fullmatch(r'\d{4}(\d{2}){0,3}',prefix): raise HTTPException(422,'科目编码无效')
    records=[dict(r) for r in c.execute("SELECT e.*,v.voucher_no,v.voucher_date FROM voucher_entries e JOIN vouchers v ON v.id=e.voucher_id AND v.account_set_id=e.account_set_id WHERE e.account_set_id=? AND e.account_code LIKE ? AND v.status IN ('POSTED','REVERSED') AND v.voucher_date>=? AND v.voucher_date<date(?,'+1 month') ORDER BY v.voucher_date,e.id LIMIT 501",(account_set_id,prefix+'%',period+'-01',period+'-01'))]
    for r in records:r['debit_amount']=cents_to_yuan(r['debit_amount']);r['credit_amount']=cents_to_yuan(r['credit_amount'])
    return {'items':records[:500],'truncated':len(records)>500}

def period_checks(c,book,period):
    validate_period(period)
    from .v11 import closing_check
    check=closing_check(period,book,c);values=check['checks']
    values['no_open_financial_approvals']=not c.execute("SELECT 1 FROM fw_workflow_cases WHERE account_set_id=? AND state='pending' AND category NOT IN ('period_close','period_unlock','reconcile','invoice_followup') AND ((category IN ('ledger','ledger_change','reimbursement') AND subject_id IN (SELECT id FROM fw_ledger_items WHERE account_set_id=? AND substr(business_date,1,7)=?)) OR (category IN ('voucher','reversal') AND subject_id IN (SELECT CAST(id AS TEXT) FROM vouchers WHERE account_set_id=? AND substr(voucher_date,1,7)=?)) OR category='vat')",(book,book,period,book,period)).fetchone()
    values['no_unresolved_sync_alerts']=not c.execute("SELECT 1 FROM fw_reconcile_alerts WHERE account_set_id=? AND status IN ('open','investigating')",(book,)).fetchone()
    closing=c.execute("SELECT * FROM fw_vat_closings WHERE account_set_id=? AND period=? AND status NOT IN ('cancelled','superseded')",(book,period)).fetchone()
    tax_activity=c.execute("SELECT 1 FROM voucher_entries e JOIN vouchers v ON v.id=e.voucher_id WHERE e.account_set_id=? AND substr(v.voucher_date,1,7)=? AND v.status IN ('POSTED','REVERSED') AND (e.account_code LIKE '222101%' OR e.account_code='2221')",(book,period)).fetchone()
    values['vat_close_confirmed']=not tax_activity or bool(closing and closing['status'] in ('posted','confirmed_no_voucher') and not closing['is_stale'])
    check['passed']=all(values.values());return check

@router.get('/periods')
def list_periods(request:Request,account_set_id:str='default',c=Depends(db)):
    access(c,request,account_set_id,READ)
    return {'items':[dict(r) for r in c.execute('SELECT * FROM accounting_periods WHERE account_set_id=? ORDER BY period DESC',(account_set_id,))]}

@router.get('/periods/{period}/check')
def period_check(period:str,request:Request,account_set_id:str='default',c=Depends(db)):
    access(c,request,account_set_id,READ);return period_checks(c,account_set_id,period)

@router.post('/periods/{period}/{action}')
def period_request(period:str,action:str,request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,FIN)
    def apply():
        validate_period(period)
        if action not in ('close-requests','unlock-requests'): raise HTTPException(404,'账期动作不存在')
        reason=required(p,'reason',1000)
        c.execute('INSERT OR IGNORE INTO accounting_periods(account_set_id,period) VALUES(?,?)',(account_set_id,period))
        current=c.execute('SELECT * FROM accounting_periods WHERE account_set_id=? AND period=?',(account_set_id,period)).fetchone()
        close=action=='close-requests'
        if bool(current['is_locked'])==close: raise HTTPException(409,'账期已经处于目标状态')
        if close and not period_checks(c,account_set_id,period)['passed']: raise HTTPException(409,'结账检查未通过，请先处理未过账凭证、核对异常及税务结转')
        if c.execute("SELECT 1 FROM fw_workflow_cases WHERE account_set_id=? AND subject_id=? AND category IN ('period_close','period_unlock') AND state='pending'",(account_set_id,period)).fetchone(): raise HTTPException(409,'本期已有待审批申请')
        case=create_case(c,account_set_id,'period_close' if close else 'period_unlock',period,current['version'],('锁定账期 ' if close else '解锁账期 ')+period,a['name'],payload={'reason':reason})
        return {'caseId':case}
    return command(c,a,account_set_id,'period_request:'+period+':'+action,p,apply)

@router.post('/vouchers/{ident}/attest-owner')
def attest_owner(ident:int,request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,{'director'})
    def apply():
        v=row(c,'vouchers',account_set_id,ident);version(p,v)
        if v['created_by'] or v['reviewed_at'] or v['status'] not in ('DRAFT','PENDING'): raise HTTPException(409,'只能核验未知制单身份的历史未审核凭证')
        name=required(p,'name',60);required(p,'reason',1000)
        if not c.execute("SELECT 1 FROM fw_book_roles WHERE account_set_id=? AND user_name=? AND active=1 AND role IN ('director','accountant')",(account_set_id,name)).fetchone(): raise HTTPException(422,'制单人须为当前账套财务成员')
        c.execute('UPDATE vouchers SET created_by=?,memo=COALESCE(memo,\'\')||?,version=version+1 WHERE id=?',(name,'；历史制单身份核验：'+p['reason'],ident))
        return {'id':ident,'created_by':name}
    return command(c,a,account_set_id,'attest_owner:'+str(ident),p,apply)

@router.get('/book-settings')
def book_settings(request:Request,account_set_id:str='default',c=Depends(db)):
    access(c,request,account_set_id,READ)
    return dict(c.execute('SELECT id,name,taxpayer_type FROM account_sets WHERE id=?',(account_set_id,)).fetchone())

@router.put('/book-settings')
def save_book_settings(request:Request,p:dict=Body(...),account_set_id:str='default',c=Depends(db)):
    a=access(c,request,account_set_id,{'director'})
    def apply():
        kind=p.get('taxpayer_type')
        if kind not in ('general','small_scale'): raise HTTPException(422,'纳税人类型无效')
        old=dict(c.execute('SELECT id,name,taxpayer_type FROM account_sets WHERE id=?',(account_set_id,)).fetchone())
        if old['taxpayer_type']!=kind and c.execute("SELECT 1 FROM fw_vat_closings WHERE account_set_id=? AND status NOT IN ('cancelled','superseded')",(account_set_id,)).fetchone(): raise HTTPException(409,'已有有效结转，纳税人类型变更需先核对历史结转')
        c.execute('UPDATE account_sets SET taxpayer_type=? WHERE id=?',(kind,account_set_id))
        c.execute("INSERT INTO audit_logs(account_set_id,module,record_id,operation,actor,before_data,after_data) VALUES(?,'books',?,'UPDATE',?,?,?)",(account_set_id,account_set_id,a['name'],js(old),js({**old,'taxpayer_type':kind})))
        return {'taxpayer_type':kind}
    return command(c,a,account_set_id,'book_settings',p,apply)
