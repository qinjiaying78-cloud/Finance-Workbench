"""Local user controls and reviewed reopening of an accounting period."""
import hashlib
import hmac
import json
import secrets
import time
import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Literal

from ..database import get_connection
from .common import assert_account_set, validate_period
from ..database import ensure_default_accounts
from ..collab_auth import can_create_book, session_actor

router = APIRouter(prefix='/api/control')
ROLES = {'admin', 'clerk', 'reviewer', 'viewer'}


def init_controls():
    conn = get_connection()
    try:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS fw_users(name TEXT PRIMARY KEY, role TEXT NOT NULL, salt TEXT NOT NULL, password_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS fw_sessions(token_hash TEXT PRIMARY KEY, name TEXT NOT NULL, expires INTEGER NOT NULL, FOREIGN KEY(name) REFERENCES fw_users(name));
        CREATE TABLE IF NOT EXISTS fw_reopen(id TEXT PRIMARY KEY, account_set_id TEXT NOT NULL, period TEXT NOT NULL,
          reason TEXT NOT NULL, requester TEXT NOT NULL, reviewer TEXT, created_at INTEGER NOT NULL, approved_at INTEGER);
        CREATE UNIQUE INDEX IF NOT EXISTS fw_reopen_pending ON fw_reopen(account_set_id,period) WHERE approved_at IS NULL;
        ''')
    finally:
        conn.close()


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600_000).hex()


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def identity(conn, request):
    raw = request.headers.get('authorization', '').removeprefix('Bearer ')
    if not raw:
        return None
    return conn.execute('SELECT u.name,u.role FROM fw_sessions s JOIN fw_users u ON u.name=s.name WHERE s.token_hash=? AND s.expires>?',
                        (token_hash(raw), int(time.time()))).fetchone()


def permitted(role, method, path):
    if method in {'GET', 'HEAD'}:
        return True
    if role == 'admin':
        return True
    if role == 'viewer':
        return False
    if path.startswith(('/api/accounts', '/api/migrate-from-local', '/api/business/migrate-from-local', '/api/business/reset', '/api/storage/account-sets')):
        return False
    review = path.startswith('/api/vouchers/') and path.rsplit('/', 1)[-1] in {'approve', 'post', 'batch-approve', 'batch-post', 'reverse'}
    period_control = path.startswith(('/api/periods/', '/api/v11/closing'))
    if review or period_control:
        return role == 'reviewer'
    return role == 'clerk'


async def enforce_controls(request, call_next):
    path = request.url.path
    public = {'/api/health', '/api/control/status', '/api/control/setup', '/api/control/login', '/api/control/logout'}
    if request.method == 'OPTIONS' or path in public or not path.startswith(('/api/', '/uploads/')):
        return await call_next(request)
    conn = get_connection()
    try:
        enabled = conn.execute('SELECT 1 FROM fw_users LIMIT 1').fetchone()
        actor = identity(conn, request) if enabled else None
    finally:
        conn.close()
    if enabled and not actor:
        return JSONResponse({'detail': '请先登录财务工作台'}, status_code=401)
    if actor and (request.method=='PUT' and path.startswith('/api/periods/') or request.method=='POST' and path=='/api/v11/closing/lock'):
        return JSONResponse({'detail':'请通过正式结账或双人反结账流程变更期间状态'},status_code=403)
    if actor and not path.startswith('/api/control/') and not permitted(actor['role'], request.method, path):
        return JSONResponse({'detail': '当前角色没有此项权限'}, status_code=403)
    if actor and actor['role'] != 'admin' and request.method != 'GET' and path.startswith('/api/storage/'):
        # The generic storage endpoint can otherwise replace stores or import posted vouchers.
        body = await request.body()
        try:
            payload = json.loads(body or b'{}')
        except ValueError:
            payload = {}
        if request.method != 'POST' or path != '/api/storage/batch' or any(
            key not in {'Workpapers', 'Settings', 'AssetCountOriginals'} for key in (payload.get('stores') or {})
        ) or payload.get('replace_stores') or payload.get('delete_ids') or any(
            str(row.get('id', '')).startswith(('closed:', 'opening:', 'reopen:', 'fw-return:', 'fw-refund:', 'fw-fx:'))
            for row in (payload.get('stores') or {}).get('Workpapers', []) if isinstance(row, dict)
        ):
            return JSONResponse({'detail': '只有管理员可执行整表替换、恢复或修改期初/锁账记录'}, status_code=403)
    return await call_next(request)


class Credentials(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    password: str = Field(min_length=10, max_length=256)


class NewUser(Credentials):
    role: str


def require_actor(conn, request, roles):
    actor = identity(conn, request)
    if not actor or actor['role'] not in roles:
        raise HTTPException(403, '请使用有权限的账号登录')
    return actor


def add_user(conn, item, role):
    name = item.name.strip()
    if not name or role not in ROLES:
        raise HTTPException(422, '用户名或角色无效')
    if conn.execute('SELECT 1 FROM fw_users WHERE name=?', (name,)).fetchone():
        raise HTTPException(409, '用户名已存在')
    salt = secrets.token_hex(16)
    conn.execute('INSERT INTO fw_users VALUES(?,?,?,?)', (name, role, salt, password_hash(item.password, salt)))


@router.get('/status')
def status(request: Request):
    conn = get_connection()
    try:
        actor = identity(conn, request)
        return {'enabled': bool(conn.execute('SELECT 1 FROM fw_users LIMIT 1').fetchone()), 'required':os.getenv('FW_REQUIRE_AUTH','false').lower()=='true', 'actor': dict(actor) if actor else None, 'can_create_book': can_create_book(conn, actor)}
    finally:
        conn.close()


@router.post('/setup')
def setup(item: Credentials, request: Request):
    if not request.client or request.client.host not in {'127.0.0.1', '::1'}:
        raise HTTPException(403, '请在本机设置管理员')
    conn = get_connection()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM fw_users LIMIT 1').fetchone():
                raise HTTPException(409, '权限已经启用')
            add_user(conn, item, 'admin')
            for book in conn.execute('SELECT id FROM account_sets').fetchall():
                conn.execute("INSERT OR IGNORE INTO fw_book_roles VALUES(?,?,'director','[]',1)",(book['id'],item.name.strip()))
    finally:
        conn.close()
    return {'enabled': True}


@router.post('/login')
def login(item: Credentials):
    conn = get_connection()
    try:
        row = conn.execute('SELECT * FROM fw_users WHERE name=?', (item.name.strip(),)).fetchone()
        actual = password_hash(item.password, row['salt'] if row else '00'*16)
        if not row or not hmac.compare_digest(row['password_hash'], actual):
            raise HTTPException(401, '用户名或密码不正确')
        token = secrets.token_urlsafe(32)
        with conn:
            conn.execute('DELETE FROM fw_sessions WHERE expires<=?', (int(time.time()),))
            conn.execute('INSERT INTO fw_sessions VALUES(?,?,?)', (token_hash(token), row['name'], int(time.time()) + 8*3600))
        return {'token': token, 'name': row['name'], 'role': row['role']}
    finally:
        conn.close()


@router.post('/logout')
def logout(request: Request):
    conn = get_connection()
    try:
        with conn:
            conn.execute('DELETE FROM fw_sessions WHERE token_hash=?', (token_hash(request.headers.get('authorization','').removeprefix('Bearer ')),))
    finally:
        conn.close()
    return {'ok': True}


@router.post('/users')
def create_user(item: NewUser, request: Request):
    conn = get_connection()
    try:
        require_actor(conn, request, {'admin'})
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            add_user(conn, item, item.role)
    finally:
        conn.close()
    return {'created': True}


class ReopenRequest(BaseModel):
    period: str
    reason: str = Field(min_length=3, max_length=500)


class NewBook(BaseModel):
    id: str = Field(pattern=r'^[a-f0-9-]{12,60}$')
    name: str = Field(min_length=1, max_length=60)
    period: str
    balances: dict[str,int]
    trade: bool
    standard: Literal['small_enterprise','enterprise'] = 'small_enterprise'


@router.post('/books', status_code=201)
def create_book(item: NewBook, request: Request):
    from .business import _upsert_inventory_opening
    period=validate_period(item.period)
    if not item.name.strip() or len(item.balances)>1000 or any(abs(value)>10**13 for value in item.balances.values()) or sum(item.balances.values()):
        raise HTTPException(422,'账套名称或期初余额无效，借贷须平衡')
    conn=get_connection()
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            actor=session_actor(conn,request)
            if conn.execute('SELECT 1 FROM fw_users LIMIT 1').fetchone() and not can_create_book(conn,actor):
                raise HTTPException(403,'创建账套需要财务总监权限')
            if conn.execute('SELECT 1 FROM account_sets WHERE id=? OR name=?',(item.id,item.name.strip())).fetchone() or conn.execute('SELECT 1 FROM retired_account_sets WHERE id=?',(item.id,)).fetchone():
                raise HTTPException(409,'账套编号或名称已经使用')
            conn.execute('INSERT INTO account_sets(id,name,is_demo) VALUES(?,?,0)',(item.id,item.name.strip()))
            ensure_default_accounts(conn,item.id)
            from ..collab_db import seed_details
            seed_details(conn,item.id)
            if actor: conn.execute("INSERT OR IGNORE INTO fw_book_roles VALUES(?,?,'director','[]',1)",(item.id,actor['name']))
            for code,amount in item.balances.items():
                changed=conn.execute('UPDATE accounts SET initial_debit=?,initial_credit=? WHERE account_set_id=? AND code=?',(max(0,amount),max(0,-amount),item.id,code)).rowcount
                if changed!=1:
                    raise HTTPException(422,f'科目 {code} 不属于预设科目表')
            if item.trade and not item.balances.get('1401') and not item.balances.get('1405'):
                _upsert_inventory_opening(conn,item.id,{'id':'inventory-opening','period':period,'verified':True,'rows':[],'totalCents':0})
            for store,row in [('Workpapers',{'id':'opening:'+period,'period':period,'balances':item.balances}),('Settings',{'id':'finance-standard:period','value':json.dumps(period)}),('Settings',{'id':'finance-standard:accounting-standard','value':json.dumps(item.standard)})]:
                conn.execute('INSERT INTO app_records(account_set_id,store_name,record_id,payload_json) VALUES(?,?,?,?)',(item.id,store,row['id'],json.dumps(row,ensure_ascii=False)))
    finally:
        conn.close()
    return {'id':item.id,'period':period}


@router.get('/reopen')
def list_reopen(request: Request, account_set_id: str):
    conn = get_connection()
    try:
        require_actor(conn, request, {'admin', 'reviewer'})
        return {'items': [dict(row) for row in conn.execute('SELECT * FROM fw_reopen WHERE account_set_id=? ORDER BY created_at DESC', (account_set_id,))]}
    finally:
        conn.close()


@router.post('/reopen')
def request_reopen(item: ReopenRequest, request: Request, account_set_id: str):
    conn = get_connection()
    try:
        actor = require_actor(conn, request, {'admin', 'reviewer'})
        period = validate_period(item.period)
        assert_account_set(conn, account_set_id)
        if len(item.reason.strip()) < 3:
            raise HTTPException(422, '请填写反结账原因')
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            exists = conn.execute("SELECT 1 FROM app_records WHERE account_set_id=? AND store_name='Workpapers' AND record_id=? AND json_type(payload_json,'$.archive')='object'", (account_set_id, 'closed:'+period)).fetchone()
            if not exists:
                raise HTTPException(409, '该期间没有可撤销的正式结账记录')
            if conn.execute('SELECT 1 FROM fw_reopen WHERE account_set_id=? AND period=? AND approved_at IS NULL', (account_set_id, period)).fetchone():
                raise HTTPException(409, '该期间已有待复核的申请')
            conn.execute('INSERT INTO fw_reopen(id,account_set_id,period,reason,requester,created_at) VALUES(?,?,?,?,?,?)', (secrets.token_hex(16), account_set_id, period, item.reason.strip(), actor['name'], int(time.time())))
    finally:
        conn.close()
    return {'pending': True}


@router.post('/reopen/{request_id}/approve')
def approve_reopen(request_id: str, request: Request, account_set_id: str):
    conn = get_connection()
    try:
        actor = require_actor(conn, request, {'admin', 'reviewer'})
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT * FROM fw_reopen WHERE id=? AND account_set_id=? AND approved_at IS NULL', (request_id, account_set_id)).fetchone()
            if not row or row['requester'] == actor['name']:
                raise HTTPException(409, '申请不存在、已处理或需要由另一账号复核')
            period = row['period']
            later = conn.execute("SELECT 1 FROM accounting_periods WHERE account_set_id=? AND period>? AND is_locked=1 UNION ALL SELECT 1 FROM app_records WHERE account_set_id=? AND store_name='Workpapers' AND record_id LIKE 'closed:%' AND record_id>?", (account_set_id, period, account_set_id, 'closed:'+period)).fetchone()
            if later:
                raise HTTPException(409, '存在后续已结账期间，请从最近期间开始反结账')
            snapshot = conn.execute("SELECT payload_json FROM app_records WHERE account_set_id=? AND store_name='Workpapers' AND record_id=?", (account_set_id, 'closed:'+period)).fetchone()
            if not snapshot:
                raise HTTPException(409, '原结账快照已变化')
            now = int(time.time())
            archive = json.loads(snapshot['payload_json'])
            archive.update(id='reopen:'+request_id, previousClose=archive['id'], reason=row['reason'], requester=row['requester'], reviewer=actor['name'], reopenedAt=now)
            conn.execute("INSERT INTO app_records(account_set_id,store_name,record_id,payload_json) VALUES(?,'Workpapers',?,?)", (account_set_id, archive['id'], json.dumps(archive, ensure_ascii=False)))
            conn.execute("DELETE FROM app_records WHERE account_set_id=? AND store_name='Workpapers' AND record_id=?", (account_set_id, 'closed:'+period))
            conn.execute('UPDATE accounting_periods SET is_locked=0,locked_at=NULL,lock_reason=? WHERE account_set_id=? AND period=?', ('经复核反结账', account_set_id, period))
            conn.execute('UPDATE fw_reopen SET reviewer=?,approved_at=? WHERE id=?', (actor['name'],now,request_id))
    finally:
        conn.close()
    return {'reopened': True, 'period': period}
