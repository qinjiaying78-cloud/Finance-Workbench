"""Authorization, immutable ingestion, approval races and tax close regression coverage."""
import json,uuid,sqlite3
import pytest
from fastapi.testclient import TestClient
from backend import database
from backend.main import app
from backend.collab_db import actor_context
from backend.routers.collaboration import queue_sync
from backend.sync_service import run_sync

def key(**kw): return {'requestKey':uuid.uuid4().hex,**kw}
@pytest.fixture
def env(tmp_path,monkeypatch):
    monkeypatch.setattr(database,'DB_PATH',tmp_path/'finance.db')
    monkeypatch.setenv('FW_SCHEDULER_ENABLED','false')
    monkeypatch.setenv('FINANCE_SECRET_DIR',str(tmp_path/'secrets'))
    with TestClient(app,client=('127.0.0.1',53000)) as client:
        password='test-password-12345'
        assert client.post('/api/control/setup',json={'name':'chief','password':password}).status_code==200
        def login(name):
            r=client.post('/api/control/login',json={'name':name,'password':password});assert r.status_code==200,r.text
            return {'Authorization':'Bearer '+r.json()['token']}
        chief=login('chief')
        for name,roles,scopes in [('accountant',['accountant'],[]),('reporter',['reporter'],['warehouse','expense']),('cashier',['cashier'],[])]:
            r=client.post('/api/v2/members',headers=chief,json=key(name=name,password=password,roles=roles,scopes=scopes));assert r.status_code==200,r.text
        yield client,chief,login

def test_read_isolation_membership_and_audit(env):
    client,chief,login=env;reporter=login('reporter');cashier=login('cashier')
    for path in ['/api/vouchers','/api/reports/trial-balance','/api/storage/Workpapers','/api/business/bootstrap','/api/accounts']:
        assert client.get(path,headers=reporter).status_code==403,path
        assert client.get(path,headers=cashier).status_code==403,path
    assert client.get('/api/v2/ledgers?account_set_id=unknown',headers=reporter).status_code==404
    assert client.post('/api/v2/ledgers?account_set_id=default',headers=reporter,json=key(account_set_id='another')).status_code==422
    assert client.get('/api/v2/audit-logs',headers=reporter).status_code==403
    c=database.get_connection()
    with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE audit_logs SET actor='forged'")
    c.close()

def ledger(client,headers,doc='D-001',**extra):
    p=key(module='warehouse',kind='receipt',business_date='2026-11-01',doc_no=doc,amount='100.00',quantity='2',invoice_state='missing',**extra)
    r=client.post('/api/v2/ledgers',headers=headers,json=p);assert r.status_code==200,r.text
    return r.json()

def test_owner_lock_and_independent_workflow(env):
    client,chief,login=env;reporter=login('reporter');accountant=login('accountant')
    first=ledger(client,reporter)
    second=ledger(client,accountant,'D-002')
    own=client.get('/api/v2/ledgers',headers=reporter).json()['items'];assert [x['id'] for x in own]==[first['id']]
    r=client.post('/api/v2/ledgers/'+first['id']+'/submit',headers=reporter,json=key(expectedVersion=first['version']));assert r.status_code==200,r.text
    assert client.patch('/api/v2/ledgers/'+first['id'],headers=reporter,json=key(expectedVersion=2)).status_code==409
    tasks=client.get('/api/v2/workflow/items',headers=chief).json()['items'];assert len(tasks)==1
    task=tasks[0]
    assert client.post('/api/v2/workflow/tasks/'+task['id']+'/actions',headers=reporter,json=key(decision='approve')).status_code==403
    p=key(decision='approve');r=client.post('/api/v2/workflow/tasks/'+task['id']+'/actions',headers=chief,json=p);assert r.status_code==200,r.text
    assert client.post('/api/v2/workflow/tasks/'+task['id']+'/actions',headers=chief,json=p).json()==r.json()
    assert client.post('/api/v2/workflow/tasks/'+task['id']+'/actions',headers=chief,json=key(decision='approve')).status_code==409
    assert client.get('/api/v2/workflow/summary',headers=reporter).json()['unread']==1

def test_shortcuts_versioning_and_account_tree(env):
    client,chief,login=env;a=login('accountant');r=login('reporter')
    p=key(shortcuts=['gl-balance','group-biz'],expectedVersion=0)
    assert client.put('/api/v2/preferences',headers=a,json=p).status_code==200
    assert client.get('/api/v2/preferences',headers=r).json()['shortcuts'] is None
    assert client.put('/api/v2/preferences',headers=r,json=key(shortcuts=['gl-balance'],expectedVersion=0)).status_code==403
    assert client.put('/api/v2/preferences',headers=a,json=key(shortcuts=[],expectedVersion=0)).status_code==409
    created=client.post('/api/v2/accounts',headers=a,json=key(code='10020101',name='基本户人民币',direction='借'));assert created.status_code==200,created.text
    assert client.post('/api/v2/accounts',headers=a,json=key(code='10020101',name='重复')).status_code==409
    assert client.post('/api/v2/accounts',headers=a,json=key(code='10021111',name='父级不存在')).status_code==422
    c=database.get_connection()
    with c:c.execute("UPDATE accounts SET initial_debit=12000 WHERE account_set_id='default' AND code='10020101'")
    c.close()
    balance=client.get('/api/v2/trial-balance?period=2026-11&level=4',headers=a);assert balance.status_code==200,balance.text
    items={x['code']:x for x in balance.json()['items']}
    assert all(items[code]['opening_debit']=='120.00' for code in ['1002','100201','10020101'])
    assert balance.json()['totals']['opening_debit']=='120.00'

def test_append_only_sync_and_missing_detection(env):
    client,chief,login=env
    project=client.post('/api/v2/dimensions',headers=chief,json=key(kind='project',code='R01',name='研发一号'));assert project.status_code==200,project.text
    p=key(name='仓库',provider='feishu',module='warehouse',source_ref='bascTestToken',table_ref='tblTest001',secret_ref='WAREHOUSE',mapping={'business_date':'日期','amount':'金额','quantity':'数量','doc_no':'单号','invoice_state':'发票','project_id':'项目'})
    config=client.post('/api/v2/sync/connections',headers=chief,json=p);assert config.status_code==200,config.text
    ident=config.json()['id']
    original=[{'external_id':'rec1','fields':{'日期':'2026-11-01','金额':'100.00','数量':'2','单号':'PO-1','发票':False,'项目':'R01'},'principal':''}]
    def scan(values):
        c=database.get_connection()
        with c:run,_=queue_sync(c,'default',ident,uuid.uuid4().hex,'chief')
        c.close();run_sync(run['id'],lambda _:values)
        c=database.get_connection();result=dict(c.execute('SELECT * FROM fw_sync_runs WHERE id=?',(run['id'],)).fetchone());c.close();return result
    one=scan(original);assert one['status'] in ('succeeded','partial'),one
    assert json.loads(one['stats_json'])['success']==1
    two=scan(original);assert json.loads(two['stats_json'])['unchanged']==1
    changed=[{**original[0],'fields':{**original[0]['fields'],'金额':'999.00'}}]
    assert json.loads(scan(changed)['stats_json'])['anomalies']>=1
    c=database.get_connection();assert c.execute('SELECT amount_cents FROM fw_ledger_items').fetchone()[0]==10000
    with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE fw_external_rows SET accepted_hash='forged'")
    c.close()
    # An empty result is a source-completeness failure, never a deletion instruction.
    assert scan([])['scan_complete']==0
    other=[{'external_id':'rec2','fields':{'日期':'2026-11-02','金额':'20.00','数量':'1','单号':'PO-2','发票':'否'},'principal':''}]
    scan(other);scan(other)
    alerts=client.get('/api/v2/reconcile/alerts',headers=chief).json()['items'];assert any(x['alert_type']=='missing' for x in alerts)

def voucher(client,headers,amount,code='22210102',debit=False):
    entries=[{'account_code':'1001','debit_amount':'0.00' if debit else amount,'credit_amount':amount if debit else '0.00'},
      {'account_code':code,'debit_amount':amount if debit else '0.00','credit_amount':'0.00' if debit else amount}]
    r=client.post('/api/vouchers',headers=headers,json={'voucher_date':'2026-11-10','summary':'测试税额','entries':entries});assert r.status_code==201,r.text
    return r.json()
def approve_and_post(client,chief,a,v):
    r=client.post('/api/v2/vouchers/'+str(v['id'])+'/submit',headers=a,json=key(expectedVersion=v['version']));assert r.status_code==200,r.text
    tasks=client.get('/api/v2/workflow/items',headers=chief).json()['items'];task=next(t for t in tasks if t['case_id']==r.json()['caseId'])
    r=client.post('/api/v2/workflow/tasks/'+task['id']+'/actions',headers=chief,json=key(decision='approve'));assert r.status_code==200,r.text
    r=client.post('/api/vouchers/'+str(v['id'])+'/post',headers=a);assert r.status_code==200,r.text

def test_vat_idempotency_approval_and_stale_basis(env):
    client,chief,login=env;a=login('accountant')
    v=voucher(client,a,'130.00');assert v['created_by']=='accountant'
    assert client.post('/api/vouchers/'+str(v['id'])+'/approve',headers=a).status_code==403
    approve_and_post(client,chief,a,v)
    preview=client.post('/api/v2/tax/vat/2026-11/preview',headers=a,json={}).json();assert preview['due']=='130.00',preview
    p=key(basisHash=preview['basis_hash'],opening_credit='0')
    generated=client.post('/api/v2/tax/vat/2026-11/generate',headers=a,json=p);assert generated.status_code==200,generated.text
    close=generated.json()
    duplicate=client.post('/api/v2/tax/vat/2026-11/generate',headers=a,json=key(basisHash=preview['basis_hash'],opening_credit='0'));assert duplicate.json()['voucher_id']==close['voucher_id']
    sub=client.post('/api/v2/tax/vat/'+close['id']+'/submit',headers=a,json=key());assert sub.status_code==200,sub.text
    task=next(t for t in client.get('/api/v2/workflow/items',headers=chief).json()['items'] if t['case_id']==sub.json()['caseId'])
    approved=client.post('/api/v2/workflow/tasks/'+task['id']+'/actions',headers=chief,json=key(decision='approve'));assert approved.status_code==200,approved.text
    # A later original tax voucher invalidates the previously approved calculation.
    approve_and_post(client,chief,a,voucher(client,a,'20.00'))
    post=client.post('/api/vouchers/'+str(close['voucher_id'])+'/post',headers=a);assert post.status_code==409,post.text

def test_reimbursement_two_steps(env):
    client,chief,login=env;r=login('reporter');a=login('accountant')
    created=client.post('/api/v2/ledgers',headers=r,json=key(module='expense',kind='reimbursement',business_date='2026-11-01',doc_no='EXP1',amount='33.00'));assert created.status_code==200,created.text
    doc=created.json();sub=client.post('/api/v2/ledgers/'+doc['id']+'/submit',headers=r,json=key(expectedVersion=doc['version']));assert sub.status_code==200,sub.text
    task=client.get('/api/v2/workflow/items',headers=a).json()['items'][0]
    first=client.post('/api/v2/workflow/tasks/'+task['id']+'/actions',headers=a,json=key(decision='approve'));assert first.json()['step']==2,first.text
    task=client.get('/api/v2/workflow/items',headers=chief).json()['items'][0]
    final=client.post('/api/v2/workflow/tasks/'+task['id']+'/actions',headers=chief,json=key(decision='approve'));assert final.status_code==200,final.text

def test_period_close_and_unlock_require_independent_approval(env):
    client,chief,login=env;a=login('accountant')
    assert client.post('/api/v11/closing/lock',headers=a,json={'period':'2026-11','reason':'bypass'}).status_code==409
    r=client.post('/api/v2/periods/2026-11/close-requests',headers=a,json=key(reason='本期核对完成'));assert r.status_code==200,r.text
    task=next(t for t in client.get('/api/v2/workflow/items',headers=chief).json()['items'] if t['case_id']==r.json()['caseId'])
    forbidden=client.post('/api/v2/workflow/tasks/'+task['id']+'/actions',headers=chief,json=key(decision='resolve'));assert forbidden.status_code==422
    approved=client.post('/api/v2/workflow/tasks/'+task['id']+'/actions',headers=chief,json=key(decision='approve'));assert approved.status_code==200,approved.text
    assert client.post('/api/v2/ledgers',headers=a,json=key(module='expense',kind='reimbursement',business_date='2026-11-03',doc_no='LOCKED',amount='1.00')).status_code==409
    r=client.post('/api/v2/periods/2026-11/unlock-requests',headers=a,json=key(reason='核准重开'));assert r.status_code==200,r.text
    task=next(t for t in client.get('/api/v2/workflow/items',headers=chief).json()['items'] if t['case_id']==r.json()['caseId'])
    approved=client.post('/api/v2/workflow/tasks/'+task['id']+'/actions',headers=chief,json=key(decision='approve'));assert approved.status_code==200,approved.text
    assert client.get('/api/v2/periods',headers=a).json()['items'][0]['is_locked']==0

def test_credentials_encrypted_and_never_audited_as_plaintext(env):
    client,chief,login=env
    p=key(secret_ref='WAREHOUSE',credentials={'app_id':'cli_test','app_secret':'secret-dont-log'})
    r=client.put('/api/v2/sync/credentials',headers=chief,json=p);assert r.status_code==200,r.text
    from backend.secret_store import folder,get_secret
    assert 'secret-dont-log' not in (folder()/'credentials.enc').read_text()
    assert get_secret('default','WAREHOUSE')['app_secret']=='secret-dont-log'
    assert 'secret-dont-log' not in client.get('/api/v2/audit-logs',headers=chief).text
    assert client.put('/api/v2/sync/credentials',headers=login('reporter'),json=p).status_code==403

def test_credit_carryforward_and_reversal_provenance(env):
    client,chief,login=env;a=login('accountant')
    original=voucher(client,a,'100.00','22210101',debit=True);approve_and_post(client,chief,a,original)
    preview=client.post('/api/v2/tax/vat/2026-11/preview',headers=a,json={}).json();assert preview['credit']=='100.00'
    generated=client.post('/api/v2/tax/vat/2026-11/generate',headers=a,json=key(basisHash=preview['basis_hash'])).json();assert generated['voucher_id'] is None
    sub=client.post('/api/v2/tax/vat/'+generated['id']+'/submit',headers=a,json=key()).json()
    task=next(t for t in client.get('/api/v2/workflow/items',headers=chief).json()['items'] if t['case_id']==sub['caseId'])
    r=client.post('/api/v2/workflow/tasks/'+task['id']+'/actions',headers=chief,json=key(decision='approve'));assert r.status_code==200,r.text
    following=client.post('/api/v2/tax/vat/2026-12/preview',headers=a,json={}).json();assert following['previous_credit']=='100.00'
    fresh=client.get('/api/vouchers',headers=a).json()['items'];v=next(x for x in fresh if x['id']==original['id'])
    sub=client.post('/api/v2/vouchers/'+str(v['id'])+'/reversals',headers=a,json=key(expectedVersion=v['version'],voucher_date='2026-12-02',reason='退货红冲'));assert sub.status_code==200,sub.text
    task=next(t for t in client.get('/api/v2/workflow/items',headers=chief).json()['items'] if t['case_id']==sub.json()['caseId'])
    r=client.post('/api/v2/workflow/tasks/'+task['id']+'/actions',headers=chief,json=key(decision='approve'));assert r.status_code==200,r.text
    reverse=next(v for v in client.get('/api/vouchers',headers=a).json()['items'] if v['reverse_of_id']==original['id'])
    assert reverse['created_by']=='accountant' and reverse['reviewed_by']=='chief'
    assert reverse['entries'][0]['credit_amount']=='-100.00'

def test_required_auth_has_no_unconfigured_bypass(tmp_path,monkeypatch):
    monkeypatch.setattr(database,'DB_PATH',tmp_path/'required-auth.db')
    monkeypatch.setenv('FW_REQUIRE_AUTH','true')
    with TestClient(app,client=('127.0.0.1',53000)) as client:
        assert client.get('/api/control/status').json()['required'] is True
        assert client.get('/api/accounts').status_code==401
        assert client.get('/api/storage/account-sets').status_code==401
        r=client.post('/api/control/setup',json={'name':'first-director','password':'required-pass-12345'});assert r.status_code==200,r.text

def test_posted_ledger_correction_keeps_original_history(env):
    client,chief,login=env;a=login('accountant');doc=ledger(client,a)
    sub=client.post('/api/v2/ledgers/'+doc['id']+'/submit',headers=a,json=key(expectedVersion=doc['version'])).json()
    task=next(t for t in client.get('/api/v2/workflow/items',headers=chief).json()['items'] if t['case_id']==sub['caseId'])
    assert client.post('/api/v2/workflow/tasks/'+task['id']+'/actions',headers=chief,json=key(decision='approve')).status_code==200
    current=client.get('/api/v2/ledgers',headers=a).json()['items'][0]
    result=client.post('/api/v2/ledgers/'+doc['id']+'/voucher',headers=a,json=key(expectedVersion=current['version'],entries=[{'account_code':'1405','debit_amount':'100.00'},{'account_code':'2202','credit_amount':'100.00'}]));assert result.status_code==200,result.text
    original=next(v for v in client.get('/api/vouchers',headers=a).json()['items'] if v['id']==result.json()['voucher_id']);approve_and_post(client,chief,a,original)
    original=next(v for v in client.get('/api/vouchers',headers=a).json()['items'] if v['id']==original['id'])
    sub=client.post('/api/v2/vouchers/'+str(original['id'])+'/reversals',headers=a,json=key(expectedVersion=original['version'],voucher_date='2026-11-15',reason='金额更正')).json()
    task=next(t for t in client.get('/api/v2/workflow/items',headers=chief).json()['items'] if t['case_id']==sub['caseId'])
    final=client.post('/api/v2/workflow/tasks/'+task['id']+'/actions',headers=chief,json=key(decision='approve'));assert final.status_code==200,final.text
    current=client.get('/api/v2/ledgers',headers=a).json()['items'][0];assert current['voucher_status']=='REVERSED'
    changed=client.post('/api/v2/ledgers/'+doc['id']+'/changes',headers=a,json=key(expectedVersion=current['version'],module='warehouse',kind='receipt',business_date='2026-11-15',doc_no=doc['doc_no'],amount='120.00',quantity='2',reason='金额核准更正'));assert changed.status_code==200,changed.text
    assert changed.json()['root_id']==doc['id'] and changed.json()['amount']=='120.00'
    c=database.get_connection();assert c.execute('SELECT amount_cents FROM fw_ledger_items WHERE id=?',(doc['id'],)).fetchone()[0]==10000;c.close()

def test_director_creates_book_without_default_membership(env):
    client, chief, login = env
    def new_book(name):
        return dict(id=uuid.uuid4().hex, name=name, period='2026-11', balances={}, trade=False)
    first = client.post('/api/control/books', headers=chief, json=new_book('existing book'))
    assert first.status_code == 201, first.text
    conn = database.get_connection()
    with conn:
        conn.execute("DELETE FROM fw_book_roles WHERE account_set_id='default' AND user_name='chief'")
        conn.execute("UPDATE fw_users SET role='clerk' WHERE name='chief'")
    conn.close()
    assert client.get('/api/control/status', headers=chief).json()['can_create_book']
    denied = client.post('/api/control/books', headers=login('accountant'), json=new_book('denied'))
    assert denied.status_code == 403, denied.text
    target = new_book('new director book')
    result = client.post('/api/control/books', headers=chief, json=target)
    assert result.status_code == 201, result.text
    caps = client.get('/api/v2/access', headers=chief, params={'account_set_id': target['id']}).json()['capabilities']
    assert all(caps[name] for name in ('financial_read','financial_write','configure','approve','cash'))
