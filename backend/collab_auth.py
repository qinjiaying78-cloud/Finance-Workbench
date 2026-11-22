"""Resource authorization shared by v2 endpoints and the legacy API guard."""
from __future__ import annotations
import json,re,uuid,os
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from .collab_db import actor_context,request_context,book_context

FINANCE={'director','accountant','viewer'}
ALIASES={'admin':'director','reviewer':'director','clerk':'accountant'}

def session_actor(conn,request):
    from .routers.finance_controls import identity
    actor=identity(conn,request)
    return dict(actor) if actor else None

def can_create_book(conn, actor):
    if not actor:
        return False
    if conn.execute(
        "SELECT 1 FROM fw_book_roles WHERE user_name=? AND role='director' AND active=1",
        (actor['name'],),
    ).fetchone():
        return True
    # Recover the original administrator if the installation has no director
    # memberships left, such as after removing the last legacy default book.
    return actor['role']=='admin' and not conn.execute(
        "SELECT 1 FROM fw_book_roles WHERE role='director' AND active=1"
    ).fetchone()

def access(conn,request,book,roles=None):
    actor=session_actor(conn,request)
    if not actor: raise HTTPException(401,'请先登录并启用财务权限')
    rows=conn.execute('SELECT role,scopes_json FROM fw_book_roles WHERE account_set_id=? AND user_name=? AND active=1',(book,actor['name'])).fetchall()
    if not rows: raise HTTPException(404,'账套不存在或当前账号无权访问')
    actor['roles']={r['role'] for r in rows}
    actor['scopes']=set(s for r in rows for s in json.loads(r['scopes_json']))
    if roles and not actor['roles'].intersection(roles): raise HTTPException(403,'当前角色无此操作权限')
    return actor

def can_ledger(actor,row,write=False):
    if actor['roles'] & {'director','accountant'}: return True
    if not write and 'viewer' in actor['roles']: return True
    if 'cashier' in actor['roles']:
        return row['module']=='cash' or (not write and bool(row['cashier_visible']))
    return ('reporter' in actor['roles'] and row['module'] in actor['scopes'] and row['created_by']==actor['name'] and row['owner_verified'])

def capabilities(actor):
    roles=actor['roles']; financial=bool(roles & FINANCE)
    return {'financial_read':financial,'financial_write':bool(roles&{'director','accountant'}),
            'configure':'director' in roles,'approve':'director' in roles,
            'cash':'cashier' in roles or financial,'reporter':'reporter' in roles,
            'scopes':sorted(actor['scopes'])}

async def enforce_controls(request,call_next):
    from .database import get_connection
    path=request.url.path
    public={'/api/health','/api/control/status','/api/control/setup','/api/control/login','/api/control/logout'}
    if request.method=='OPTIONS' or path in public or not path.startswith(('/api/','/uploads/')):
        return await call_next(request)
    conn=get_connection()
    try:
        enabled=bool(conn.execute('SELECT 1 FROM fw_users LIMIT 1').fetchone())
        actor=session_actor(conn,request) if enabled else None
        if enabled and not actor: raise HTTPException(401,'请先登录财务工作台')
        if not enabled:
            if os.getenv('FW_REQUIRE_AUTH','false').lower()=='true': raise HTTPException(401,'请在本机创建首位财务总监并登录')
            return await call_next(request)
        payload={}
        if request.method not in {'GET','HEAD'} and 'application/json' in request.headers.get('content-type',''):
            try: payload=json.loads(await request.body() or '{}')
            except ValueError: raise HTTPException(422,'请求 JSON 格式无效')
        if not isinstance(payload,dict): payload={}
        qbook=request.query_params.get('account_set_id')
        pbook=payload.get('account_set_id')
        if qbook and pbook and qbook!=pbook: raise HTTPException(422,'请求中的账套不一致')
        book=qbook or pbook or 'default'
        if path.startswith('/uploads/'):
            owned=conn.execute('SELECT account_set_id FROM fw_upload_ownership WHERE url=?',(path,)).fetchone()
            if owned: book=owned['account_set_id']
            else:
                candidate=conn.execute('''SELECT r.account_set_id FROM (
                  SELECT account_set_id FROM vouchers WHERE EXISTS(SELECT 1 FROM json_each(attachment_urls) WHERE value=?)
                  UNION SELECT account_set_id FROM cashier_records WHERE receipt_img_url=?
                  UNION SELECT account_set_id FROM invoices WHERE pdf_or_img_url=?) r
                  JOIN fw_book_roles m ON m.account_set_id=r.account_set_id AND m.user_name=? AND m.active=1
                  WHERE m.role IN ('director','accountant','viewer') LIMIT 1''',(path,path,path,actor['name'])).fetchone()
                if candidate:book=candidate['account_set_id']
        if path.startswith('/api/storage/account-sets/'):
            book=path.split('/api/storage/account-sets/',1)[1]
        if path=='/api/storage/account-sets' and request.method=='GET':
            roles=set()
        elif path=='/api/control/books' and request.method=='POST':
            # The new book does not yet have memberships. The command checks
            # creation permission inside its transaction.
            roles={'director'} if can_create_book(conn,actor) else set()
        elif path.startswith('/api/control/') and not path.startswith('/api/control/budgets'):
            # Legacy global member management is replaced by book-scoped v2 commands.
            if path=='/api/control/users': raise HTTPException(409,'请通过协作设置按账套创建或授权成员')
            roles=access(conn,request,book,{'director'})['roles']
        else:
            actor=access(conn,request,book)
            roles=actor['roles']
        if path.startswith('/api/v2/'):
            pass  # Every v2 endpoint performs action- and row-level authorization.
        elif path.startswith('/uploads/'):
            ownership=conn.execute('SELECT * FROM fw_upload_ownership WHERE url=?',(path,)).fetchone()
            if ownership:
                owner_access=access(conn,request,ownership['account_set_id'])
                if not owner_access['roles']&FINANCE and ownership['uploaded_by']!=actor['name']: raise HTTPException(404,'附件不存在或无权访问')
            else:
                refs=conn.execute('''SELECT account_set_id FROM vouchers WHERE EXISTS(SELECT 1 FROM json_each(attachment_urls) WHERE value=?)
                  UNION SELECT account_set_id FROM cashier_records WHERE receipt_img_url=?
                  UNION SELECT account_set_id FROM invoices WHERE pdf_or_img_url=?''',(path,path,path)).fetchall()
                if not any(conn.execute("SELECT 1 FROM fw_book_roles WHERE account_set_id=? AND user_name=? AND active=1 AND role IN ('director','accountant','viewer')",(r['account_set_id'],actor['name'])).fetchone() for r in refs): raise HTTPException(404,'附件不存在或无权访问')
        elif path!='/api/storage/account-sets' or request.method!='GET':
            if not roles & FINANCE:
                # The old storage/bootstrap APIs contain mixed sensitive records.
                if not ('cashier' in roles and (path.startswith('/api/cashier') and 'to-voucher' not in path or path in ('/api/upload','/api/uploads'))):
                    raise HTTPException(403,'请使用当前角色授权的业务台账入口')
            if request.method not in {'GET','HEAD'}:
                if not roles & {'director','accountant','cashier'}: raise HTTPException(403,'当前账号为只读')
                if re.search(r'/vouchers/(?:\d+/approve|batch-approve)$',path) and 'director' not in roles:
                    raise HTTPException(403,'凭证终审需要财务总监')
                if re.search(r'/vouchers/(?:\d+/approve|batch-approve)$',path):
                    raise HTTPException(409,'协作账套请通过流程中心审批凭证')
                if path.endswith('/reverse') and path.startswith('/api/vouchers/'):
                    raise HTTPException(409,'请通过流程中心发起凭证红冲申请')
                if path.startswith(('/api/storage/account-sets','/api/business/reset','/api/migrate','/api/business/migrate','/api/accounts/opening')) and 'director' not in roles:
                    raise HTTPException(403,'此项需要财务总监')
                if path.startswith('/api/storage/') and not path.startswith('/api/storage/account-sets/'):
                    if path!='/api/storage/batch' or request.method not in {'PUT','POST'} or payload.get('replace_stores'):
                        raise HTTPException(403,'协作账套禁止直接整表替换或恢复')
                    if set(payload.get('stores',{})) - {'Workpapers','Settings','AssetCountOriginals'}:
                        raise HTTPException(403,'财务记录必须使用专用业务接口')
                    if any(str(row.get('id','')).startswith(('closed:','opening:','reopen:')) for row in payload.get('stores',{}).get('Workpapers',[])):
                        raise HTTPException(403,'请通过正式账期控制流程处理')
                if path.startswith('/api/periods/') and request.method=='PUT': raise HTTPException(403,'请通过正式结账或反结账流程处理')
                if path=='/api/v11/closing/lock' or re.fullmatch(r'/api/periods/[^/]+/close',path): raise HTTPException(409,'请通过账期锁定与解锁页面提交独立终审')
        name=actor['name']
    except HTTPException as exc:
        return JSONResponse({'detail':exc.detail},status_code=exc.status_code)
    finally: conn.close()
    tokens=[(actor_context,actor_context.set(name)),(request_context,request_context.set(request.headers.get('x-request-id') or uuid.uuid4().hex)),(book_context,book_context.set(book))]
    try: return await call_next(request)
    finally:
        for var,token in reversed(tokens): var.reset(token)
