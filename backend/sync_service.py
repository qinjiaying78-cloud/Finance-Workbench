"""Official table connectors and append-only, idempotent ingestion."""
from __future__ import annotations
import json,os,time,urllib.parse,urllib.request,urllib.error
from collections import Counter
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from .database import get_connection
from .collab_db import actor_context,request_context
from .secret_store import get_secret

class SourceError(Exception): pass
def http_json(url,token=None,payload=None):
    # All URLs are assembled from fixed provider hosts and validated identifiers.
    headers={'Accept':'application/json'}
    if token: headers['Authorization']='Bearer '+token
    data=None
    if payload is not None: data=json.dumps(payload).encode();headers['Content-Type']='application/json'
    for attempt in range(4):
        try:
            req=urllib.request.Request(url,data=data,headers=headers)
            with urllib.request.urlopen(req,timeout=25) as result:
                if urllib.parse.urlsplit(result.url).hostname not in {'open.feishu.cn','sheets.googleapis.com'}: raise SourceError('来源响应重定向异常')
                raw=result.read(8*1024*1024+1)
                if len(raw)>8*1024*1024: raise SourceError('来源返回过大，请缩小业务表范围')
                value=json.loads(raw)
            if value.get('code',0)!=0: raise SourceError('来源拒绝请求，请检查应用权限和表格授权')
            return value
        except urllib.error.HTTPError as exc:
            if exc.code in (429,500,502,503,504) and attempt<3: time.sleep(2**attempt);continue
            raise SourceError('来源接口 HTTP '+str(exc.code)+'，请检查凭据、权限或稍后重试') from None
        except (urllib.error.URLError,TimeoutError,ValueError):
            if attempt<3: time.sleep(2**attempt);continue
            raise SourceError('来源接口连接失败或返回无效数据') from None

def read_source(connection):
    secret=get_secret(connection['account_set_id'],connection['secret_ref']);rows=[]
    if connection['provider']=='feishu':
        response=http_json('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',payload={'app_id':secret.get('app_id'),'app_secret':secret.get('app_secret')})
        token=response.get('tenant_access_token')
        if not token: raise SourceError('飞书未返回有效应用令牌')
        cursor='';seen=set()
        while True:
            query=urllib.parse.urlencode({'page_size':500,'automatic_fields':'true',**({'page_token':cursor} if cursor else {})})
            url=f"https://open.feishu.cn/open-apis/bitable/v1/apps/{connection['source_ref']}/tables/{connection['table_ref']}/records?{query}"
            data=http_json(url,token).get('data',{})
            if not isinstance(data.get('items',[]),list): raise SourceError('飞书分页格式异常')
            for item in data.get('items',[]):
                creator=item.get('created_by') or {}
                # Use provider metadata only, never a freely entered owner-name cell.
                principal=creator.get('id','') if isinstance(creator,dict) else ''
                rows.append({'external_id':item.get('record_id',''),'fields':item.get('fields',{}),'principal':principal})
            if len(rows)>20000: raise SourceError('当前本地连接单表上限 20000 行，请按固定业务范围拆表')
            if not data.get('has_more'): break
            cursor=data.get('page_token')
            if not cursor or cursor in seen: raise SourceError('飞书分页中断，未获得完整扫描')
            seen.add(cursor)
    else:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        # Prevent credential JSON from directing signing requests to arbitrary hosts.
        if secret.get('token_uri')!='https://oauth2.googleapis.com/token': raise SourceError('Google 服务账号 token_uri 必须为官方地址')
        try:
            creds=service_account.Credentials.from_service_account_info(secret,scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
            creds.refresh(Request())
        except Exception: raise SourceError('Google 服务账号鉴权失败，请检查凭据与表格共享授权') from None
        query=urllib.parse.urlencode({'valueRenderOption':'UNFORMATTED_VALUE','dateTimeRenderOption':'FORMATTED_STRING'})
        # table_ref must identify a complete sheet, not a filtered view or moving last-row range.
        cell_range="'"+connection['table_ref'].replace("'","''")+"'"
        url=f"https://sheets.googleapis.com/v4/spreadsheets/{connection['source_ref']}/values/{urllib.parse.quote(cell_range,safe='')}?{query}"
        values=http_json(url,creds.token).get('values',[])
        if not values: return []
        headers=[str(v).strip() for v in values[0]]
        if not headers or len(headers)!=len(set(headers)): raise SourceError('Google 表头为空或重复')
        if len(values)>20001: raise SourceError('当前本地连接单表上限 20000 行')
        mapping=json.loads(connection['mapping_json']);id_column=mapping.get('external_id','external_id')
        if id_column not in headers: raise SourceError('Google 表须包含受保护的稳定 external_id 列')
        for values_row in values[1:]:
            if not any(v not in ('',None) for v in values_row): continue
            fields=dict(zip(headers,values_row));external=str(fields.get(id_column,'')).strip()
            rows.append({'external_id':external,'fields':fields,'principal':''})
    return rows

def scalar(v):
    if isinstance(v,list):
        if len(v)==1: return scalar(v[0])
        return ''.join(str(scalar(x)) for x in v)
    if isinstance(v,dict): return v.get('text',v.get('name',v.get('value','')))
    return v
def normalize(c,book,module,raw,mapping):
    from .routers.collaboration import ledger_values
    fields=raw['fields'];p={k:scalar(fields.get(v)) for k,v in mapping.items() if k!='external_id' and scalar(fields.get(v)) not in (None,'')}
    p['module']=module;p.setdefault('kind',{'warehouse':'receipt','expense':'reimbursement','asset':'asset_entry'}[module]);p.setdefault('doc_no',raw['external_id'])
    for field in ('needs_invoice_followup','cashier_visible'):
        if field in p: p[field]=str(p[field]).lower() in ('true','1','是','需要','需催票')
    p['payment_kind']={'预付款':'advance','发货款':'shipment'}.get(p.get('payment_kind'),p.get('payment_kind'))
    if isinstance(p.get('invoice_state'),bool):p['invoice_state']='complete' if p['invoice_state'] else 'missing'
    p['invoice_state']={'是':'complete','否':'missing','已开齐':'complete','未开齐':'partial','无需开票':'not_required'}.get(p.get('invoice_state'),p.get('invoice_state','unknown'))
    p['kind']={'采购单':'purchase_order','入库单':'receipt','发票':'invoice','报销':'reimbursement','固定资产':'asset_entry'}.get(p['kind'],p['kind'])
    day=p.get('business_date')
    if isinstance(day,(int,float)) and day>10**11:
        p['business_date']=datetime.fromtimestamp(day/1000,timezone(timedelta(hours=8))).date().isoformat()
    if 'doc_line' in p:
        try:
            line=Decimal(str(p['doc_line']))
            if line!=line.to_integral_value():raise ValueError()
            p['doc_line']=int(line)
        except Exception:raise SourceError('单据行号须为整数')
    # Business dimensions may use stable business codes or an unambiguous name.
    # This resolution never establishes source ownership or user permissions.
    for field,kind in [('project_id','project'),('supplier_id','supplier'),('department_id','department')]:
        if p.get(field):
            options=c.execute('SELECT id FROM fw_dimensions WHERE account_set_id=? AND kind=? AND is_active=1 AND (id=? OR code=? OR name=?)',(book,kind,str(p[field]),str(p[field]),str(p[field]))).fetchall()
            if len(options)!=1:raise SourceError('辅助对象不存在或名称不唯一，请填写已配置的业务编码：'+field)
            p[field]=options[0]['id']
    return ledger_values(c,book,p,check_period=False)

def alert(c,book,connection,run,kind,external,evidence,ledger=None):
    from .routers.collaboration import digest,js,uid,create_case
    fingerprint=digest([connection,kind,external,evidence])
    found=c.execute('SELECT * FROM fw_reconcile_alerts WHERE account_set_id=? AND fingerprint=?',(book,fingerprint)).fetchone()
    if found:
        c.execute("UPDATE fw_reconcile_alerts SET last_seen_at=?,run_id=?,version=version+1 WHERE id=?",(datetime.now(timezone.utc).isoformat(),run,found['id']))
        ident=found['id']
    else:
        ident=uid();c.execute('INSERT INTO fw_reconcile_alerts(id,account_set_id,connection_id,run_id,external_id,ledger_id,alert_type,fingerprint,evidence_json) VALUES(?,?,?,?,?,?,?,?,?)',(ident,book,connection,run,external,ledger,kind,fingerprint,js(evidence)))
    pending=c.execute("SELECT 1 FROM fw_workflow_cases WHERE account_set_id=? AND category='reconcile' AND subject_id=? AND state='pending'",(book,ident)).fetchone()
    if not pending and (not found or found['status'] in ('open','investigating')):
        # A connection requires an accountant handler; configuration validates this first.
        users=c.execute("SELECT 1 FROM fw_book_roles WHERE account_set_id=? AND active=1 AND role='accountant'",(book,)).fetchone()
        if users: create_case(c,book,'reconcile',ident,1,{'changed':'外部已入账行变更','missing':'外部已入账行缺失','three_way_diff':'三单核对差异'}.get(kind,'同步核对异常')+' · '+str(external),'service:sync',first_role='accountant')
    return ident

def ingest(c,connection,run,rows):
    from .routers.collaboration import digest,js,uid
    book=connection['account_set_id'];ident=connection['id'];mapping=json.loads(connection['mapping_json'])
    counts=Counter(str(r.get('external_id') or '').strip() for r in rows)
    stats={'read':len(rows),'new':0,'success':0,'unchanged':0,'anomalies':0,'failed':0,'duplicate':0}
    existing=c.execute('SELECT * FROM fw_external_rows WHERE account_set_id=? AND connection_id=?',(book,ident)).fetchall()
    old_by_id={r['external_id']:r for r in existing}
    if not rows and existing:
        alert(c,book,ident,run,'source_incomplete','',{'reason':'已有入账行的来源本次返回空表，需复核权限和完整性'})
        stats['anomalies']+=1
        return stats,False
    versions={r['version']:json.loads(r['mapping_json']) for r in c.execute('SELECT * FROM fw_sync_mappings WHERE account_set_id=? AND connection_id=?',(book,ident))}
    handled=set()
    for raw in rows:
        external=str(raw.get('external_id') or '').strip()
        if not external or counts[external]>1:
            if external not in handled:
                alert(c,book,ident,run,'duplicate_id' if external else 'invalid_row',external,{'reason':'来源 ID 缺失或重复','occurrences':counts[external]})
                stats['anomalies']+=1;stats['duplicate']+=counts[external] if external else 0;handled.add(external)
            continue
        prior=old_by_id.get(external)
        try:
            accepted=normalize(c,book,connection['module'],raw,versions.get(prior['mapping_version'],mapping) if prior else mapping)
        except Exception as exc:
            # Validation details contain field names, never tokens or HTTP headers.
            detail=getattr(exc,'detail','字段格式无效')
            evidence={'reason':str(detail)[:500]}
            if prior: evidence.update(before=json.loads(prior['raw_json']),after=raw['fields'])
            alert(c,book,ident,run,'changed' if prior else 'invalid_row',external,evidence);stats['anomalies']+=1;stats['failed']+=1;continue
        if prior:
            if digest(accepted)==prior['accepted_hash']: stats['unchanged']+=1
            else:
                baseline=json.loads(prior['accepted_json'])
                alert(c,book,ident,run,'changed',external,{'before':baseline,'after':accepted});stats['anomalies']+=1
            continue
        stats['new']+=1
        try:
            c.execute('SAVEPOINT ingest_row')
            from .routers.common import assert_period_unlocked
            assert_period_unlocked(c,accepted['business_date'],book)
            c.execute('INSERT INTO fw_external_rows(account_set_id,connection_id,external_id,raw_json,accepted_json,accepted_hash,mapping_version,first_seen_at) VALUES(?,?,?,?,?,?,?,?)',(book,ident,external,js(raw['fields']),js(accepted),digest(accepted),connection['mapping_version'],datetime.now(timezone.utc).isoformat()))
            owner=c.execute('SELECT user_name FROM fw_source_identities WHERE account_set_id=? AND connection_id=? AND external_principal=?',(book,ident,raw.get('principal',''))).fetchone() if raw.get('principal') else None
            new=uid();values={**accepted,'id':new,'account_set_id':book,'root_id':new,'created_by':owner['user_name'] if owner else 'external:unverified','owner_verified':int(bool(owner)),
                'source_connection_id':ident,'source_row_id':external,'status':'pending_reconcile','is_locked':1}
            c.execute('INSERT INTO fw_ledger_items('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',tuple(values.values()))
            from .routers.collaboration import add_followup_task
            add_followup_task(c,book,c.execute('SELECT * FROM fw_ledger_items WHERE id=?',(new,)).fetchone())
            c.execute('RELEASE ingest_row');stats['success']+=1
        except Exception:
            c.execute('ROLLBACK TO ingest_row');c.execute('RELEASE ingest_row')
            alert(c,book,ident,run,'invalid_row',external,{'reason':'业务号重复或引用无效；未写入台账'});stats['failed']+=1;stats['anomalies']+=1
    for missing in old_by_id.keys()-counts.keys():
        # The first complete scan creates a candidate; only a subsequent independent run confirms it.
        previous=c.execute("SELECT * FROM fw_reconcile_alerts WHERE account_set_id=? AND connection_id=? AND external_id=? AND alert_type='missing_candidate' AND run_id<>? ORDER BY rowid DESC LIMIT 1",(book,ident,missing,run)).fetchone()
        latest=c.execute('SELECT id FROM fw_sync_runs WHERE account_set_id=? AND connection_id=? AND scan_complete=1 AND id<>? ORDER BY rowid DESC LIMIT 1',(book,ident,run)).fetchone()
        confirmed=bool(previous and latest and previous['run_id']==latest['id'])
        alert(c,book,ident,run,'missing' if confirmed else 'missing_candidate',missing,{'reason':'连续完整扫描未发现已入账行' if confirmed else '本次完整扫描缺行，需再次核验'})
        stats['anomalies']+=1
    return stats,True

def run_sync(run_id,reader=None):
    from .routers.collaboration import js,now_iso
    c=get_connection();token=actor_context.set('service:sync');req=request_context.set(run_id)
    try:
        run=c.execute('SELECT * FROM fw_sync_runs WHERE id=?',(run_id,)).fetchone()
        if not run or run['status']!='queued': return
        connection=c.execute('SELECT * FROM fw_sync_connections WHERE id=? AND account_set_id=?',(run['connection_id'],run['account_set_id'])).fetchone()
        with c:
            claimed=c.execute("UPDATE fw_sync_runs SET status='running' WHERE id=? AND lease_token=? AND status='queued'",(run_id,run['lease_token']))
            if claimed.rowcount!=1: return
        records=(reader or read_source)(dict(connection))
        with c:
            c.execute('BEGIN IMMEDIATE')
            current=c.execute("SELECT * FROM fw_sync_runs WHERE id=? AND lease_token=? AND status='running'",(run_id,run['lease_token'])).fetchone()
            latest=c.execute('SELECT version FROM fw_sync_connections WHERE id=?',(connection['id'],)).fetchone()
            if not current or latest['version']!=connection['version']: raise SourceError('连接配置已变化，本次结果未提交')
            stats,complete=ingest(c,connection,run_id,records)
            auto_reconcile(c,run['account_set_id'],connection['id'],run_id)
            state='partial' if stats['anomalies'] or not complete else 'succeeded'
            c.execute('UPDATE fw_sync_runs SET status=?,stats_json=?,scan_complete=?,finished_at=?,lease_until=NULL WHERE id=?',(state,js(stats),int(complete),now_iso(),run_id))
            if complete:c.execute('UPDATE fw_sync_connections SET last_success_at=? WHERE id=?',(now_iso(),connection['id']))
    except Exception as exc:
        c.rollback()
        message=str(exc) if isinstance(exc,(SourceError,ValueError)) else '同步失败，请检查字段映射、表格授权或服务端日志'
        with c: c.execute("UPDATE fw_sync_runs SET status='failed',error_summary=?,finished_at=?,lease_until=NULL WHERE id=? AND lease_token=? AND status IN ('queued','running')",(message[:500],now_iso(),run_id,run['lease_token']))
    finally:
        c.close();actor_context.reset(token);request_context.reset(req)

def auto_reconcile(c,book,connection,run):
    # Explicit document numbers group candidates; missing/ambiguous links remain alerts.
    docs=c.execute("SELECT * FROM fw_ledger_items l WHERE account_set_id=? AND module='warehouse' AND NOT EXISTS(SELECT 1 FROM fw_ledger_items n WHERE n.corrected_from=l.id AND n.status IN ('approved','posted'))",(book,)).fetchall()
    groups={}
    for d in docs: groups.setdefault((d['doc_no'],d['doc_line'],d['supplier_id'],d['item_code']),[]).append(d)
    for key,items in groups.items():
        if not any(x['source_connection_id']==connection for x in items): continue
        if all(x['payment_kind']=='advance' for x in items): continue
        kinds={x['kind'] for x in items}
        if kinds!={'purchase_order','receipt','invoice'}:
            alert(c,book,connection,run,'three_way_diff',str(key),{'reason':'缺少三单或需要人工匹配','document_ids':[d['id'] for d in items]});continue
        sums={kind:(sum(Decimal(x['quantity']) for x in items if x['kind']==kind),sum(x['amount_cents'] for x in items if x['kind']==kind),sum(x['tax_cents'] for x in items if x['kind']==kind)) for kind in kinds}
        if len(set(sums.values()))>1:
            alert(c,book,connection,run,'three_way_diff',str(key),{'reason':'三单数量／金额／税额不一致','totals':{k:[str(v[0]),v[1],v[2]] for k,v in sums.items()}})
