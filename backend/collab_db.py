"""Additive collaboration migration. No business history is rewritten."""
from __future__ import annotations
import json
from contextvars import ContextVar

actor_context = ContextVar('finance_actor', default='system')
request_context = ContextVar('finance_request', default='')
book_context = ContextVar('finance_book', default='default')

SCHEMA = r'''
CREATE TABLE IF NOT EXISTS fw_schema_versions(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS fw_book_roles(
 account_set_id TEXT NOT NULL REFERENCES account_sets(id),user_name TEXT NOT NULL REFERENCES fw_users(name),
 role TEXT NOT NULL CHECK(role IN ('director','accountant','cashier','reporter','viewer')),
 scopes_json TEXT NOT NULL DEFAULT '[]',active INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(account_set_id,user_name,role));
CREATE TABLE IF NOT EXISTS fw_preferences(
 account_set_id TEXT NOT NULL,user_name TEXT NOT NULL,shortcuts_json TEXT NOT NULL DEFAULT '[]',
 version INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(account_set_id,user_name));
CREATE TABLE IF NOT EXISTS fw_upload_ownership(url TEXT PRIMARY KEY,account_set_id TEXT NOT NULL REFERENCES account_sets(id),uploaded_by TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS fw_dimensions(
 id TEXT PRIMARY KEY,account_set_id TEXT NOT NULL REFERENCES account_sets(id),
 kind TEXT NOT NULL CHECK(kind IN ('customer','supplier','department','project','bank_account')),
 code TEXT NOT NULL,name TEXT NOT NULL,is_active INTEGER NOT NULL DEFAULT 1,
 UNIQUE(account_set_id,kind,code),UNIQUE(account_set_id,id));
CREATE TABLE IF NOT EXISTS fw_account_dimensions(
 account_set_id TEXT NOT NULL,account_code TEXT NOT NULL,kind TEXT NOT NULL,required INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(account_set_id,account_code,kind),
 FOREIGN KEY(account_set_id,account_code) REFERENCES accounts(account_set_id,code));
CREATE TABLE IF NOT EXISTS fw_entry_dimensions(
 account_set_id TEXT NOT NULL,entry_id INTEGER NOT NULL REFERENCES voucher_entries(id) ON DELETE CASCADE,
 kind TEXT NOT NULL,dimension_id TEXT NOT NULL,PRIMARY KEY(entry_id,kind),
 FOREIGN KEY(account_set_id,dimension_id) REFERENCES fw_dimensions(account_set_id,id));
CREATE TABLE IF NOT EXISTS fw_sync_connections(
 id TEXT PRIMARY KEY,account_set_id TEXT NOT NULL REFERENCES account_sets(id),name TEXT NOT NULL,
 provider TEXT NOT NULL CHECK(provider IN ('feishu','google_sheets')),
 module TEXT NOT NULL CHECK(module IN ('warehouse','expense','asset')),
 source_ref TEXT NOT NULL,table_ref TEXT NOT NULL,secret_ref TEXT NOT NULL,
 mapping_json TEXT NOT NULL,mapping_version INTEGER NOT NULL DEFAULT 1,
 enabled INTEGER NOT NULL DEFAULT 1,scheduled INTEGER NOT NULL DEFAULT 0,
 last_success_at TEXT,version INTEGER NOT NULL DEFAULT 1,
 UNIQUE(account_set_id,provider,source_ref,table_ref),UNIQUE(account_set_id,id));
CREATE TABLE IF NOT EXISTS fw_sync_mappings(
 account_set_id TEXT NOT NULL,connection_id TEXT NOT NULL,version INTEGER NOT NULL,mapping_json TEXT NOT NULL,
 created_by TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(account_set_id,connection_id,version),
 FOREIGN KEY(account_set_id,connection_id) REFERENCES fw_sync_connections(account_set_id,id));
CREATE TABLE IF NOT EXISTS fw_sync_runs(
 id TEXT PRIMARY KEY,account_set_id TEXT NOT NULL,connection_id TEXT NOT NULL,
 request_key TEXT NOT NULL,trigger_type TEXT NOT NULL,initiated_by TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','partial','failed')),
 stats_json TEXT NOT NULL DEFAULT '{}',mapping_version INTEGER NOT NULL,scan_complete INTEGER NOT NULL DEFAULT 0,
 error_summary TEXT,started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,finished_at TEXT,
 lease_token TEXT,lease_until TEXT,
 UNIQUE(account_set_id,connection_id,request_key),UNIQUE(account_set_id,id),
 FOREIGN KEY(account_set_id,connection_id) REFERENCES fw_sync_connections(account_set_id,id));
CREATE UNIQUE INDEX IF NOT EXISTS fw_sync_active ON fw_sync_runs(connection_id) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS fw_external_rows(
 account_set_id TEXT NOT NULL,connection_id TEXT NOT NULL,external_id TEXT NOT NULL,
 raw_json TEXT NOT NULL,accepted_json TEXT NOT NULL,accepted_hash TEXT NOT NULL,
 mapping_version INTEGER NOT NULL,hash_version INTEGER NOT NULL DEFAULT 1,first_seen_at TEXT NOT NULL,
 PRIMARY KEY(account_set_id,connection_id,external_id),
 FOREIGN KEY(account_set_id,connection_id) REFERENCES fw_sync_connections(account_set_id,id));
CREATE TABLE IF NOT EXISTS fw_source_identities(
 account_set_id TEXT NOT NULL,connection_id TEXT NOT NULL,external_principal TEXT NOT NULL,
 user_name TEXT NOT NULL REFERENCES fw_users(name),verified_by TEXT NOT NULL,
 PRIMARY KEY(account_set_id,connection_id,external_principal),
 FOREIGN KEY(account_set_id,connection_id) REFERENCES fw_sync_connections(account_set_id,id));
CREATE TABLE IF NOT EXISTS fw_ledger_items(
 id TEXT PRIMARY KEY,account_set_id TEXT NOT NULL REFERENCES account_sets(id),module TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('purchase_order','receipt','invoice','reimbursement','asset_entry','cash_entry')),
 business_date TEXT NOT NULL,doc_no TEXT NOT NULL,doc_line INTEGER NOT NULL DEFAULT 1,
 amount_cents INTEGER NOT NULL CHECK(amount_cents>=0),tax_cents INTEGER NOT NULL DEFAULT 0 CHECK(tax_cents>=0),
 quantity TEXT NOT NULL DEFAULT '0',item_code TEXT NOT NULL DEFAULT '',supplier_id TEXT,project_id TEXT,
 department_id TEXT,payment_kind TEXT CHECK(payment_kind IN ('advance','shipment') OR payment_kind IS NULL),
 invoice_state TEXT NOT NULL DEFAULT 'unknown' CHECK(invoice_state IN ('unknown','missing','partial','complete','not_required')),
 needs_invoice_followup INTEGER NOT NULL DEFAULT 0,cashier_visible INTEGER NOT NULL DEFAULT 0,
 created_by TEXT NOT NULL,owner_verified INTEGER NOT NULL DEFAULT 1,
 source_connection_id TEXT,source_row_id TEXT,root_id TEXT NOT NULL,corrected_from TEXT,reason TEXT,
 status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','pending_reconcile','submitted','approved','rejected','posted','voided')),
 is_locked INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 1,voucher_id INTEGER,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(account_set_id,id),
 UNIQUE(account_set_id,source_connection_id,source_row_id),
 FOREIGN KEY(account_set_id,source_connection_id,source_row_id) REFERENCES fw_external_rows(account_set_id,connection_id,external_id),
 FOREIGN KEY(account_set_id,corrected_from) REFERENCES fw_ledger_items(account_set_id,id),
 FOREIGN KEY(account_set_id,root_id) REFERENCES fw_ledger_items(account_set_id,id),
 FOREIGN KEY(account_set_id,voucher_id) REFERENCES vouchers(account_set_id,id),
 FOREIGN KEY(account_set_id,supplier_id) REFERENCES fw_dimensions(account_set_id,id),
 FOREIGN KEY(account_set_id,project_id) REFERENCES fw_dimensions(account_set_id,id),
 FOREIGN KEY(account_set_id,department_id) REFERENCES fw_dimensions(account_set_id,id));
CREATE UNIQUE INDEX IF NOT EXISTS fw_doc_unique ON fw_ledger_items(account_set_id,module,kind,doc_no,doc_line) WHERE corrected_from IS NULL;
CREATE INDEX IF NOT EXISTS fw_ledger_owner ON fw_ledger_items(account_set_id,created_by,module);
CREATE TABLE IF NOT EXISTS fw_followups(
 id TEXT PRIMARY KEY,account_set_id TEXT NOT NULL,ledger_id TEXT NOT NULL,state TEXT NOT NULL,
 invoice_state TEXT,note TEXT NOT NULL,created_by TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(account_set_id,ledger_id) REFERENCES fw_ledger_items(account_set_id,id));
CREATE TABLE IF NOT EXISTS fw_match_allocations(
 id TEXT PRIMARY KEY,account_set_id TEXT NOT NULL,po_id TEXT NOT NULL,receipt_id TEXT NOT NULL,invoice_id TEXT NOT NULL,
 quantity TEXT NOT NULL,net_cents INTEGER NOT NULL,tax_cents INTEGER NOT NULL,created_by TEXT NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 FOREIGN KEY(account_set_id,po_id) REFERENCES fw_ledger_items(account_set_id,id),
 FOREIGN KEY(account_set_id,receipt_id) REFERENCES fw_ledger_items(account_set_id,id),
 FOREIGN KEY(account_set_id,invoice_id) REFERENCES fw_ledger_items(account_set_id,id));
CREATE TABLE IF NOT EXISTS fw_reconcile_alerts(
 id TEXT PRIMARY KEY,account_set_id TEXT NOT NULL,connection_id TEXT,run_id TEXT,external_id TEXT,
 ledger_id TEXT,alert_type TEXT NOT NULL,fingerprint TEXT NOT NULL,evidence_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'open',version INTEGER NOT NULL DEFAULT 1,
 first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(account_set_id,fingerprint));
CREATE TABLE IF NOT EXISTS fw_reconcile_events(
 id TEXT PRIMARY KEY,account_set_id TEXT NOT NULL,alert_id TEXT NOT NULL REFERENCES fw_reconcile_alerts(id),
 action TEXT NOT NULL,note TEXT NOT NULL,actor TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS fw_workflow_cases(
 id TEXT PRIMARY KEY,account_set_id TEXT NOT NULL,category TEXT NOT NULL,subject_id TEXT NOT NULL,
 subject_version INTEGER NOT NULL,title TEXT NOT NULL,initiated_by TEXT NOT NULL,made_by TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending',step INTEGER NOT NULL DEFAULT 1,payload_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,decided_by TEXT,decided_at TEXT,note TEXT,
 UNIQUE(account_set_id,id));
CREATE UNIQUE INDEX IF NOT EXISTS fw_pending_case ON fw_workflow_cases(account_set_id,category,subject_id) WHERE state='pending';
CREATE TABLE IF NOT EXISTS fw_workflow_tasks(
 id TEXT PRIMARY KEY,account_set_id TEXT NOT NULL,case_id TEXT NOT NULL,assignee TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('action','notice')),required_role TEXT,step INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'open',read_at TEXT,acted_at TEXT,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(case_id,assignee,kind,step),
 FOREIGN KEY(account_set_id,case_id) REFERENCES fw_workflow_cases(account_set_id,id));
CREATE INDEX IF NOT EXISTS fw_task_inbox ON fw_workflow_tasks(account_set_id,assignee,state,kind);
CREATE TABLE IF NOT EXISTS fw_vat_closings(
 id TEXT PRIMARY KEY,account_set_id TEXT NOT NULL,period TEXT NOT NULL,version INTEGER NOT NULL,
 output_cents INTEGER NOT NULL,input_cents INTEGER NOT NULL,previous_credit_cents INTEGER NOT NULL,
 due_cents INTEGER NOT NULL,credit_cents INTEGER NOT NULL,basis_json TEXT NOT NULL,basis_hash TEXT NOT NULL,
 voucher_id INTEGER,status TEXT NOT NULL DEFAULT 'draft',is_stale INTEGER NOT NULL DEFAULT 0,
 created_by TEXT NOT NULL,request_key TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(account_set_id,period,version),UNIQUE(account_set_id,request_key),
 FOREIGN KEY(account_set_id,voucher_id) REFERENCES vouchers(account_set_id,id));
CREATE UNIQUE INDEX IF NOT EXISTS fw_vat_active ON fw_vat_closings(account_set_id,period) WHERE status NOT IN ('cancelled','superseded');
CREATE TABLE IF NOT EXISTS fw_period_archives(id TEXT PRIMARY KEY,account_set_id TEXT NOT NULL,period TEXT NOT NULL,snapshot_json TEXT NOT NULL,source_hash TEXT NOT NULL,closed_by TEXT NOT NULL,closed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS fw_command_receipts(
 account_set_id TEXT NOT NULL,actor TEXT NOT NULL,command TEXT NOT NULL,request_key TEXT NOT NULL,
 input_hash TEXT NOT NULL,result_json TEXT NOT NULL,PRIMARY KEY(account_set_id,actor,command,request_key));
CREATE TABLE IF NOT EXISTS audit_logs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,account_set_id TEXT NOT NULL,module TEXT NOT NULL,record_id TEXT,
 operation TEXT NOT NULL,actor TEXT NOT NULL,acted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
 before_data TEXT,after_data TEXT,request_id TEXT,reason TEXT);
CREATE INDEX IF NOT EXISTS fw_audit_timeline ON audit_logs(account_set_id,acted_at DESC,id DESC);
CREATE TRIGGER IF NOT EXISTS fw_audit_no_update BEFORE UPDATE ON audit_logs BEGIN SELECT RAISE(ABORT,'审计日志不可修改'); END;
CREATE TRIGGER IF NOT EXISTS fw_audit_no_delete BEFORE DELETE ON audit_logs BEGIN SELECT RAISE(ABORT,'审计日志不可删除'); END;
CREATE TRIGGER IF NOT EXISTS fw_external_no_update BEFORE UPDATE ON fw_external_rows BEGIN SELECT RAISE(ABORT,'外部入账基线不可覆盖'); END;
CREATE TRIGGER IF NOT EXISTS fw_external_no_delete BEFORE DELETE ON fw_external_rows BEGIN SELECT RAISE(ABORT,'外部入账基线不可删除'); END;
'''

DETAILS = [
 ('100201','基本户','1002','借'),('100202','一般户','1002','借'),('100203','专户','1002','借'),
 ('101201','外埠存款','1012','借'),('101202','银行汇票存款','1012','借'),('101203','银行本票存款','1012','借'),('101204','信用证保证金存款','1012','借'),
 ('222101','应交增值税','2221','贷'),('222102','未交增值税','2221','贷'),('222103','预交增值税','2221','借'),
 ('222104','待认证进项税额','2221','借'),('222105','待转销项税额','2221','贷'),('222106','应交企业所得税','2221','贷'),
 ('222107','应交城市维护建设税','2221','贷'),('222108','应交教育费附加','2221','贷'),('222109','应交地方教育附加','2221','贷'),
 ('222110','应交个人所得税','2221','贷'),
 ('22210101','进项税额','222101','借'),('22210102','销项税额','222101','贷'),('22210103','转出未交增值税','222101','借'),
 ('22210104','已交税金','222101','借'),('22210105','进项税额转出','222101','贷'),('22210106','销项税额抵减','222101','借'),
 ('22210107','转出多交增值税','222101','贷'),('22210108','减免税款','222101','借')]

def seed_details(conn, book):
    for code,name,parent,side in DETAILS:
        category='资产' if code.startswith(('100','101')) else '负债'
        conn.execute('INSERT OR IGNORE INTO accounts(account_set_id,code,name,category,direction,parent_code,initial_debit,initial_credit,is_active) VALUES(?,?,?,?,?,?,0,0,1)',(book,code,name,category,side,parent))

def init_collaboration():
    from .database import get_connection
    conn=get_connection()
    try:
        conn.executescript(SCHEMA)
        for table,columns in {
            'accounts': [('is_custom','INTEGER NOT NULL DEFAULT 0'),('version','INTEGER NOT NULL DEFAULT 1')],
            'vouchers':[('created_by','TEXT'),('reviewed_by','TEXT'),('version','INTEGER NOT NULL DEFAULT 1')],
            'account_sets':[('taxpayer_type',"TEXT NOT NULL DEFAULT 'general'")],
            'accounting_periods':[('version','INTEGER NOT NULL DEFAULT 1')],
        }.items():
            existing={r['name'] for r in conn.execute(f'PRAGMA table_info({table})')}
            for name,definition in columns:
                if name not in existing: conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        if not conn.execute('SELECT 1 FROM fw_schema_versions WHERE version=12').fetchone():
            # A legacy reviewer is not silently granted configuration/admin powers.
            mapping={'admin':'director','reviewer':'viewer','clerk':'accountant','viewer':'viewer'}
            for b in conn.execute('SELECT id FROM account_sets').fetchall():
                seed_details(conn,b['id'])
                for u in conn.execute('SELECT name,role FROM fw_users').fetchall():
                    conn.execute('INSERT OR IGNORE INTO fw_book_roles VALUES(?,?,?,\'[]\',1)',(b['id'],u['name'],mapping.get(u['role'],u['role'])))
            conn.execute('INSERT INTO fw_schema_versions(version) VALUES(12)')
        tables=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        skip={'audit_logs','fw_sessions','fw_users','fw_schema_versions','fw_command_receipts'}
        for table in tables:
            if table in skip or table.startswith('sqlite_'): continue
            cols=[r['name'] for r in conn.execute(f'PRAGMA table_info("{table}")')]
            if 'account_set_id' not in cols: continue
            key='id' if 'id' in cols else 'code' if 'code' in cols else 'account_set_id'
            def snapshot(prefix):
                fields=[c for c in cols if c not in ('secret_ref','salt','password_hash','token_hash')]
                return 'json_object('+','.join(f"'{c}',{prefix}.\"{c}\"" for c in fields)+')'
            for operation in ('INSERT','UPDATE','DELETE'):
                ref='OLD' if operation=='DELETE' else 'NEW'
                before='NULL' if operation=='INSERT' else snapshot('OLD')
                after='NULL' if operation=='DELETE' else snapshot('NEW')
                conn.execute(f'''CREATE TRIGGER IF NOT EXISTS fw_log_{table}_{operation} AFTER {operation} ON "{table}" BEGIN
                  INSERT INTO audit_logs(account_set_id,module,record_id,operation,actor,before_data,after_data,request_id)
                  VALUES({ref}.account_set_id,'{table}',CAST({ref}."{key}" AS TEXT),'{operation}',fw_actor(),{before},{after},fw_request()); END''')
        conn.executescript('''
        CREATE TRIGGER IF NOT EXISTS fw_voucher_author AFTER INSERT ON vouchers
        WHEN NEW.created_by IS NULL AND fw_actor()<>'system' BEGIN
          UPDATE vouchers SET created_by=fw_actor() WHERE id=NEW.id;
        END;
        CREATE TRIGGER IF NOT EXISTS fw_voucher_revision AFTER UPDATE ON vouchers
        WHEN NEW.version=OLD.version BEGIN UPDATE vouchers SET version=OLD.version+1 WHERE id=NEW.id; END;
        CREATE TRIGGER IF NOT EXISTS fw_no_self_review BEFORE UPDATE OF reviewed_at ON vouchers
        WHEN NEW.reviewed_at IS NOT NULL AND OLD.reviewed_at IS NULL AND fw_actor()<>'system'
          AND (OLD.created_by IS NULL OR OLD.created_by=fw_actor()) BEGIN
          SELECT RAISE(ABORT,'不能终审本人凭证或未知制单身份的历史凭证');
        END;
        CREATE TRIGGER IF NOT EXISTS fw_locked_ledger_no_delete BEFORE DELETE ON fw_ledger_items
        WHEN OLD.is_locked=1 BEGIN SELECT RAISE(ABORT,'锁定台账不可删除'); END;
        CREATE TRIGGER IF NOT EXISTS fw_locked_ledger_facts BEFORE UPDATE ON fw_ledger_items
        WHEN OLD.is_locked=1 AND (
          NEW.is_locked<>1 OR NEW.amount_cents<>OLD.amount_cents OR NEW.tax_cents<>OLD.tax_cents
          OR NEW.quantity<>OLD.quantity OR NEW.module<>OLD.module OR NEW.kind<>OLD.kind
          OR NEW.business_date<>OLD.business_date OR NEW.doc_no<>OLD.doc_no OR NEW.item_code<>OLD.item_code
          OR NEW.payment_kind IS NOT OLD.payment_kind OR NEW.invoice_state<>OLD.invoice_state
          OR NEW.project_id IS NOT OLD.project_id OR NEW.supplier_id IS NOT OLD.supplier_id
          OR NEW.department_id IS NOT OLD.department_id OR NEW.created_by<>OLD.created_by
          OR NEW.root_id<>OLD.root_id OR NEW.corrected_from IS NOT OLD.corrected_from)
        BEGIN SELECT RAISE(ABORT,'锁定事实需要另存更正版本'); END;
        CREATE TRIGGER IF NOT EXISTS fw_voucher_posted AFTER UPDATE OF status ON vouchers
        WHEN NEW.status='POSTED' AND OLD.status<>'POSTED' BEGIN
          UPDATE fw_vat_closings SET status='posted' WHERE account_set_id=NEW.account_set_id AND voucher_id=NEW.id AND status='approved';
          UPDATE fw_ledger_items SET status='posted',version=version+1 WHERE account_set_id=NEW.account_set_id AND voucher_id=NEW.id;
          UPDATE fw_vat_closings SET is_stale=1 WHERE account_set_id=NEW.account_set_id AND period>=substr(NEW.voucher_date,1,7)
            AND COALESCE(NEW.source_type,'')<>'VAT_CLOSE' AND EXISTS(SELECT 1 FROM voucher_entries WHERE voucher_id=NEW.id AND account_code LIKE '2221%');
        END;
        CREATE TRIGGER IF NOT EXISTS fw_tax_entry_insert AFTER INSERT ON voucher_entries
        WHEN NEW.account_code LIKE '2221%' BEGIN
          UPDATE fw_vat_closings SET is_stale=1 WHERE account_set_id=NEW.account_set_id
            AND period>=(SELECT substr(voucher_date,1,7) FROM vouchers WHERE id=NEW.voucher_id)
            AND EXISTS(SELECT 1 FROM vouchers WHERE id=NEW.voucher_id AND status IN ('POSTED','REVERSED') AND COALESCE(source_type,'')<>'VAT_CLOSE');
        END;
        ''')
        conn.commit()
    finally: conn.close()
