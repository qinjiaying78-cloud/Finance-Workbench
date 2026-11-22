"""SQLite database setup and connection helpers for Finance Workbench.

All monetary values are stored as integer cents (分). API payloads use decimal
strings in yuan and are converted exactly at the service boundary.
"""
from __future__ import annotations

import os
import json
import sqlite3
from pathlib import Path
from typing import Iterator

BACKEND_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BACKEND_DIR / "finance_workbench.db"
DB_PATH = Path(os.getenv("FINANCE_DB_PATH", str(DEFAULT_DB_PATH))).expanduser().resolve()

SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT NOT NULL UNIQUE,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL CHECK (category IN ('资产', '负债', '权益', '成本', '损益')),
    direction       TEXT NOT NULL CHECK (direction IN ('借', '贷')),
    parent_code     TEXT,
    initial_debit   INTEGER NOT NULL DEFAULT 0 CHECK (initial_debit >= 0),
    initial_credit  INTEGER NOT NULL DEFAULT 0 CHECK (initial_credit >= 0),
    is_active       INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    FOREIGN KEY (parent_code) REFERENCES accounts(code)
        ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS contacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    type            TEXT NOT NULL CHECK (type IN ('CUSTOMER', 'SUPPLIER')),
    tax_id          TEXT,
    contact_info    TEXT
);

CREATE TABLE IF NOT EXISTS vouchers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_no      TEXT NOT NULL UNIQUE,
    voucher_date    TEXT NOT NULL,
    summary         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'DRAFT'
                    CHECK (status IN ('DRAFT', 'PENDING', 'POSTED', 'VOIDED', 'REVERSED')),
    attachment_urls TEXT NOT NULL DEFAULT '[]',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    posted_at       TEXT,
    reverse_of_id   INTEGER,
    reversed_by_id  INTEGER,
    reversed_at     TEXT,
    FOREIGN KEY (reverse_of_id) REFERENCES vouchers(id) ON DELETE RESTRICT,
    FOREIGN KEY (reversed_by_id) REFERENCES vouchers(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS voucher_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    voucher_id      INTEGER NOT NULL,
    account_code    TEXT NOT NULL,
    contact_id      INTEGER,
    summary         TEXT NOT NULL DEFAULT '',
    debit_amount    INTEGER NOT NULL DEFAULT 0,
    credit_amount   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (voucher_id) REFERENCES vouchers(id)
        ON UPDATE CASCADE ON DELETE CASCADE,
    FOREIGN KEY (account_code) REFERENCES accounts(code)
        ON UPDATE CASCADE ON DELETE RESTRICT,
    FOREIGN KEY (contact_id) REFERENCES contacts(id)
        ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS cashier_records (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    record_date         TEXT NOT NULL,
    biz_type            TEXT NOT NULL CHECK (biz_type IN ('RECEIPT', 'PAYMENT', 'TRANSFER')),
    amount              INTEGER NOT NULL CHECK (amount >= 0),
    account_name        TEXT NOT NULL,
    contact_name        TEXT,
    source_doc          TEXT,
    receipt_img_url     TEXT,
    status              TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK (status IN ('PENDING', 'DONE', 'POSTED')),
    linked_voucher_id   INTEGER,
    FOREIGN KEY (linked_voucher_id) REFERENCES vouchers(id)
        ON UPDATE CASCADE ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_code    TEXT,
    invoice_no      TEXT NOT NULL,
    invoice_date    TEXT NOT NULL,
    buyer_name      TEXT NOT NULL,
    buyer_tax_id    TEXT,
    seller_name     TEXT,
    amount          INTEGER NOT NULL DEFAULT 0 CHECK (amount >= 0),
    tax_amount      INTEGER NOT NULL DEFAULT 0 CHECK (tax_amount >= 0),
    payment_method  TEXT,
    status          TEXT NOT NULL DEFAULT 'DRAFT',
    pdf_or_img_url  TEXT,
    items_json      TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS accounting_periods (
    period          TEXT PRIMARY KEY,
    is_locked       INTEGER NOT NULL DEFAULT 0 CHECK (is_locked IN (0, 1)),
    locked_at       TEXT,
    lock_reason     TEXT,
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Optional opening balances at contact level. This is required when a party's
-- opening balance must be shown accurately in its sub-ledger.
CREATE TABLE IF NOT EXISTS contact_opening_balances (
    account_code    TEXT NOT NULL,
    contact_id      INTEGER NOT NULL,
    as_of_date      TEXT NOT NULL,
    initial_debit   INTEGER NOT NULL DEFAULT 0 CHECK (initial_debit >= 0),
    initial_credit  INTEGER NOT NULL DEFAULT 0 CHECK (initial_credit >= 0),
    PRIMARY KEY (account_code, contact_id),
    FOREIGN KEY (account_code) REFERENCES accounts(code) ON UPDATE CASCADE ON DELETE CASCADE,
    FOREIGN KEY (contact_id) REFERENCES contacts(id) ON UPDATE CASCADE ON DELETE CASCADE
);

-- Browser-workbench persistence layer. The JSON payload preserves the full
-- front-end record shape while normalized finance tables remain available for
-- API integrations and SQL reports.
CREATE TABLE IF NOT EXISTS account_sets (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    is_demo         INTEGER NOT NULL DEFAULT 0 CHECK (is_demo IN (0, 1)),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS app_records (
    account_set_id  TEXT NOT NULL,
    store_name      TEXT NOT NULL,
    record_id       TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (account_set_id, store_name, record_id),
    FOREIGN KEY (account_set_id) REFERENCES account_sets(id) ON UPDATE CASCADE ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_app_records_store ON app_records(account_set_id, store_name, updated_at);

CREATE TABLE IF NOT EXISTS normalized_migration_map (
    account_set_id TEXT NOT NULL,
    module TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY(account_set_id,module,source_id)
);

CREATE INDEX IF NOT EXISTS idx_accounts_parent_code ON accounts(parent_code);
CREATE INDEX IF NOT EXISTS idx_contacts_name_type ON contacts(name, type);
CREATE INDEX IF NOT EXISTS idx_vouchers_date_status ON vouchers(voucher_date, status);
CREATE INDEX IF NOT EXISTS idx_voucher_entries_voucher_id ON voucher_entries(voucher_id);
CREATE INDEX IF NOT EXISTS idx_voucher_entries_account_code ON voucher_entries(account_code);
CREATE INDEX IF NOT EXISTS idx_voucher_entries_contact ON voucher_entries(account_code, contact_id);
CREATE INDEX IF NOT EXISTS idx_cashier_records_date_status ON cashier_records(record_date, status);
CREATE INDEX IF NOT EXISTS idx_cashier_records_voucher ON cashier_records(linked_voucher_id);
CREATE INDEX IF NOT EXISTS idx_invoices_date_no ON invoices(invoice_date, invoice_no);
"""


def get_connection() -> sqlite3.Connection:
    """Return a configured SQLite connection. Caller must close it."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # FastAPI can enter and exit a per-request dependency on different worker threads.
    conn = sqlite3.connect(str(DB_PATH), timeout=30, isolation_level="DEFERRED", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    from .collab_db import actor_context,request_context
    conn.create_function('fw_actor',0,actor_context.get)
    conn.create_function('fw_request',0,request_context.get)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _rebuild_voucher_tables_for_signed_reversals(conn: sqlite3.Connection) -> None:
    """Migrate the first-generation schema without discarding existing rows.

    The original CHECK constraints prohibited negative red-letter entries and
    did not allow REVERSED status. SQLite cannot alter CHECK constraints, so
    the two related tables are copied into replacement tables transactionally.
    """
    voucher_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='vouchers'"
    ).fetchone()
    entry_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='voucher_entries'"
    ).fetchone()
    voucher_sql = (voucher_sql_row["sql"] or "") if voucher_sql_row else ""
    entry_sql = (entry_sql_row["sql"] or "") if entry_sql_row else ""
    needs_migration = (
        "REVERSED" not in voucher_sql
        or "posted_at" not in voucher_sql
        or "debit_amount >= 0" in entry_sql
        or "reverse_of_id" not in voucher_sql
    )
    if not needs_migration:
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""
            CREATE TABLE vouchers_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_no TEXT NOT NULL UNIQUE,
                voucher_date TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'DRAFT'
                    CHECK (status IN ('DRAFT','PENDING','POSTED','VOIDED','REVERSED')),
                attachment_urls TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                posted_at TEXT,
                reverse_of_id INTEGER,
                reversed_by_id INTEGER,
                reversed_at TEXT,
                FOREIGN KEY (reverse_of_id) REFERENCES vouchers_new(id) ON DELETE RESTRICT,
                FOREIGN KEY (reversed_by_id) REFERENCES vouchers_new(id) ON DELETE RESTRICT
            )
        """)
        conn.execute("""
            INSERT INTO vouchers_new
                (id,voucher_no,voucher_date,summary,status,attachment_urls,created_at)
            SELECT id,voucher_no,voucher_date,summary,status,attachment_urls,created_at
            FROM vouchers
        """)
        conn.execute("""
            CREATE TABLE voucher_entries_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                voucher_id INTEGER NOT NULL,
                account_code TEXT NOT NULL,
                contact_id INTEGER,
                summary TEXT NOT NULL DEFAULT '',
                debit_amount INTEGER NOT NULL DEFAULT 0,
                credit_amount INTEGER NOT NULL DEFAULT 0,
                            FOREIGN KEY (voucher_id) REFERENCES vouchers_new(id)
                    ON UPDATE CASCADE ON DELETE CASCADE,
                FOREIGN KEY (account_code) REFERENCES accounts(code)
                    ON UPDATE CASCADE ON DELETE RESTRICT,
                FOREIGN KEY (contact_id) REFERENCES contacts(id)
                    ON UPDATE CASCADE ON DELETE RESTRICT
            )
        """)
        conn.execute("""
            INSERT INTO voucher_entries_new
                (id,voucher_id,account_code,contact_id,summary,debit_amount,credit_amount)
            SELECT id,voucher_id,account_code,contact_id,summary,debit_amount,credit_amount
            FROM voucher_entries
        """)
        conn.execute("DROP TABLE voucher_entries")
        conn.execute("DROP TABLE vouchers")
        conn.execute("ALTER TABLE vouchers_new RENAME TO vouchers")
        conn.execute("ALTER TABLE voucher_entries_new RENAME TO voucher_entries")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vouchers_date_status ON vouchers(voucher_date,status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_voucher_entries_voucher_id ON voucher_entries(voucher_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_voucher_entries_account_code ON voucher_entries(account_code)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_voucher_entries_contact ON voucher_entries(account_code,contact_id)")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")



def _ensure_v8_voucher_columns(conn: sqlite3.Connection) -> None:
    """Add normalized voucher metadata needed by the V8 frontend adapter.

    This is additive: existing voucher IDs, numbers, entries and balances are
       retained. The legacy browser ID is unique within an account set so the
    UI can keep its existing cross-module references without storing vouchers
    in app_records.
    """
    voucher_columns = {r["name"] for r in conn.execute("PRAGMA table_info(vouchers)")}
    entry_columns = {r["name"] for r in conn.execute("PRAGMA table_info(voucher_entries)")}
    additions = {
        "legacy_id": "TEXT",
        "biz_label": "TEXT NOT NULL DEFAULT ''",
        "source": "TEXT",
        "source_type": "TEXT",
        "counterparty": "TEXT",
        "memo": "TEXT",
        "attachment_name": "TEXT",
        "reviewed_at": "TEXT",
    }
    for name, ddl in additions.items():
        if name not in voucher_columns:
            conn.execute(f"ALTER TABLE vouchers ADD COLUMN {name} {ddl}")
    if "aux_name" not in entry_columns:
        conn.execute("ALTER TABLE voucher_entries ADD COLUMN aux_name TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_vouchers_tenant_legacy_id ON vouchers(account_set_id,legacy_id) WHERE legacy_id IS NOT NULL")
    # Recover original browser IDs for V6/V7 records already imported.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='normalized_migration_map'").fetchone():
        conn.execute("""
            UPDATE vouchers
               SET legacy_id = (
                   SELECT m.source_id FROM normalized_migration_map m
                    WHERE m.account_set_id=vouchers.account_set_id
                      AND m.module='vouchers' AND m.target_id=vouchers.id
                    LIMIT 1
               )
             WHERE legacy_id IS NULL
               AND EXISTS (SELECT 1 FROM normalized_migration_map m
                            WHERE m.account_set_id=vouchers.account_set_id
                              AND m.module='vouchers' AND m.target_id=vouchers.id)
        """)
        # Backfill V7 voucher metadata and non-contact auxiliary labels from the
        # retained VoucherRecords app_records snapshot. This preserves the old
        # UI's bank-account auxiliary labels and APPROVED review state.
        legacy_rows = conn.execute("""
            SELECT m.account_set_id,m.target_id,a.payload_json
              FROM normalized_migration_map m
              JOIN app_records a ON a.account_set_id=m.account_set_id
                                AND a.store_name='VoucherRecords' AND a.record_id=m.source_id
             WHERE m.module='vouchers'
        """).fetchall()
        for legacy in legacy_rows:
            try:
                payload = json.loads(legacy["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            book, target = legacy["account_set_id"], legacy["target_id"]
            status = str(payload.get("status") or "").upper()
            attachments = payload.get("attachmentUrls") or payload.get("attachment_urls") or []
            attachment = payload.get("attachment")
            if isinstance(attachment, dict) and attachment.get("url"):
                attachments = attachments or [attachment["url"]]
            elif isinstance(attachment, str) and attachment.startswith("/uploads/"):
                attachments = attachments or [attachment]
            attachments = [u for u in attachments if isinstance(u, str) and u.startswith("/uploads/") and ".." not in u.split("/")]
            conn.execute("""UPDATE vouchers SET
                biz_label=CASE WHEN biz_label='' THEN ? ELSE biz_label END,
                source=COALESCE(source,?), source_type=COALESCE(source_type,?),
                counterparty=COALESCE(counterparty,?), memo=COALESCE(memo,?),
                attachment_name=COALESCE(attachment_name,?),
                attachment_urls=CASE WHEN attachment_urls='[]' AND ?<>'[]' THEN ? ELSE attachment_urls END,
                reviewed_at=CASE WHEN ? IN ('APPROVED','已审核') AND reviewed_at IS NULL THEN created_at ELSE reviewed_at END,
                posted_at=CASE WHEN ? IN ('POSTED','已记账') AND posted_at IS NULL THEN created_at ELSE posted_at END
                WHERE account_set_id=? AND id=?""", (
                    str(payload.get("bizLabel") or ""), payload.get("source"), payload.get("sourceType"),
                    payload.get("counterparty"), payload.get("memo"),
                    (attachment.get("name") if isinstance(attachment, dict) else None),
                    json.dumps(attachments, ensure_ascii=False), json.dumps(attachments, ensure_ascii=False),
                    status, status, book, target,
                ))
            for index, entry in enumerate(payload.get("entries") or []):
                aux = entry.get("aux") or entry.get("aux_name")
                if aux:
                    conn.execute("""UPDATE voucher_entries SET aux_name=?
                        WHERE account_set_id=? AND voucher_id=? AND id=(
                            SELECT id FROM voucher_entries WHERE account_set_id=? AND voucher_id=? ORDER BY id LIMIT 1 OFFSET ?
                        ) AND aux_name IS NULL""", (str(aux), book, target, book, target, index))
    conn.commit()


def _ensure_v9_normalized_columns(conn: sqlite3.Connection) -> None:
    """Add stable legacy keys and lossless UI metadata for normalized cashier/invoice rows."""
    additions = {
        "cashier_records": {
            "legacy_id": "TEXT", "transfer_id": "TEXT", "direction": "TEXT",
            "extra_json": "TEXT NOT NULL DEFAULT '{}'",
        },
        "invoices": {
            "legacy_id": "TEXT", "source": "TEXT", "extra_json": "TEXT NOT NULL DEFAULT '{}'",
        },
    }
    for table, columns in additions.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_cashier_legacy_id ON cashier_records(account_set_id,legacy_id) WHERE legacy_id IS NOT NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_invoices_legacy_id ON invoices(account_set_id,legacy_id) WHERE legacy_id IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_invoices_tenant_buyer ON invoices(account_set_id,buyer_name,invoice_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cashier_tenant_transfer ON cashier_records(account_set_id,transfer_id)")
    # Backfill V8 rows from the retained app_records snapshot and idempotency map.
    # This is an additive compatibility step: source snapshots are never deleted.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='normalized_migration_map'").fetchone():
        for row in conn.execute("""SELECT m.account_set_id,m.source_id,m.target_id,a.store_name,a.payload_json
            FROM normalized_migration_map m JOIN app_records a
              ON a.account_set_id=m.account_set_id AND a.record_id=m.source_id
            WHERE (m.module='cashier' AND a.store_name='CashierRecords')
               OR (m.module='invoices' AND a.store_name='Invoices')""").fetchall():
            try: payload=json.loads(row["payload_json"] or "{}")
            except (TypeError,json.JSONDecodeError): continue
            book, source, target = row["account_set_id"], row["source_id"], row["target_id"]
            if row["store_name"] == "CashierRecords":
                raw_type=str(payload.get("type") or "")
                direction="OUT" if raw_type.endswith("OUT") else ("IN" if raw_type.endswith("IN") else None)
                conn.execute("""UPDATE cashier_records SET legacy_id=COALESCE(legacy_id,?),
                    transfer_id=COALESCE(transfer_id,?),direction=COALESCE(direction,?),
                    extra_json=CASE WHEN extra_json='{}' THEN ? ELSE extra_json END
                    WHERE account_set_id=? AND id=?""", (source,payload.get("transferId"),direction,json.dumps(payload,ensure_ascii=False),book,target))
            else:
                attachments=payload.get("attachments") or []
                image=next((x.get("url") for x in attachments if isinstance(x,dict) and x.get("url")),None)
                actual_no=str(payload.get("invoiceNumber") or payload.get("invoice_no") or "").strip()
                conn.execute("""UPDATE invoices SET legacy_id=COALESCE(legacy_id,?),
                    invoice_no=CASE WHEN ?<>'' THEN ? ELSE invoice_no END,
                    source=COALESCE(source,?),
                    buyer_tax_id=COALESCE(buyer_tax_id,?),seller_name=COALESCE(seller_name,?),
                    payment_method=COALESCE(payment_method,?),
                    pdf_or_img_url=COALESCE(pdf_or_img_url,?),
                    extra_json=CASE WHEN extra_json='{}' THEN ? ELSE extra_json END
                    WHERE account_set_id=? AND id=?""", (source,actual_no,actual_no,payload.get("source"),payload.get("buyerTax"),payload.get("seller"),payload.get("payment"),image,json.dumps(payload,ensure_ascii=False),book,target))
    conn.commit()


def check_db_connection() -> bool:
    conn = get_connection()
    try:
        row = conn.execute("SELECT 1 AS ok").fetchone()
        return bool(row and row["ok"] == 1)
    finally:
        conn.close()


def _migrate_to_tenant_schema(conn: sqlite3.Connection) -> None:
    """Move normalized finance tables to account-set scoped composite keys.

    Existing normalized rows are assigned to the `default` account set. The
    original app_records remain untouched; subsequent migrations can import
    their rows into the selected account set. IDs and financial values are
    preserved. This migration is transactional and safe to run repeatedly.
    """
    row = conn.execute("SELECT 1 FROM pragma_table_info('accounts') WHERE name='account_set_id'").fetchone()
    if row:
        return

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(r"""
        BEGIN IMMEDIATE;
        CREATE TABLE accounts_v7 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_set_id TEXT NOT NULL,
            code TEXT NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL CHECK(category IN ('资产','负债','权益','成本','损益')),
            direction TEXT NOT NULL CHECK(direction IN ('借','贷')),
            parent_code TEXT,
            initial_debit INTEGER NOT NULL DEFAULT 0 CHECK(initial_debit>=0),
            initial_credit INTEGER NOT NULL DEFAULT 0 CHECK(initial_credit>=0),
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
            UNIQUE(account_set_id,code), UNIQUE(account_set_id,id),
            FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE,
            FOREIGN KEY(account_set_id,parent_code) REFERENCES accounts_v7(account_set_id,code)
                ON UPDATE CASCADE ON DELETE RESTRICT
        );
        CREATE TABLE contacts_v7 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_set_id TEXT NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL CHECK(type IN ('CUSTOMER','SUPPLIER')),
            tax_id TEXT,
            contact_info TEXT,
            UNIQUE(account_set_id,id),
            FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE
        );
        CREATE TABLE vouchers_v7 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_set_id TEXT NOT NULL,
            voucher_no TEXT NOT NULL,
            voucher_date TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'DRAFT' CHECK(status IN ('DRAFT','PENDING','POSTED','VOIDED','REVERSED')),
            attachment_urls TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            posted_at TEXT,
            reverse_of_id INTEGER,
            reversed_by_id INTEGER,
            reversed_at TEXT,
            UNIQUE(account_set_id,voucher_no), UNIQUE(account_set_id,id),
            FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE,
            FOREIGN KEY(account_set_id,reverse_of_id) REFERENCES vouchers_v7(account_set_id,id) ON DELETE RESTRICT,
            FOREIGN KEY(account_set_id,reversed_by_id) REFERENCES vouchers_v7(account_set_id,id) ON DELETE RESTRICT
        );
        CREATE TABLE voucher_entries_v7 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_set_id TEXT NOT NULL,
            voucher_id INTEGER NOT NULL,
            account_code TEXT NOT NULL,
            contact_id INTEGER,
            summary TEXT NOT NULL DEFAULT '',
            debit_amount INTEGER NOT NULL DEFAULT 0,
            credit_amount INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(account_set_id,voucher_id) REFERENCES vouchers_v7(account_set_id,id) ON UPDATE CASCADE ON DELETE CASCADE,
            FOREIGN KEY(account_set_id,account_code) REFERENCES accounts_v7(account_set_id,code) ON UPDATE CASCADE ON DELETE RESTRICT,
            FOREIGN KEY(account_set_id,contact_id) REFERENCES contacts_v7(account_set_id,id) ON UPDATE CASCADE ON DELETE RESTRICT
        );
        CREATE TABLE cashier_records_v7 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_set_id TEXT NOT NULL,
            record_date TEXT NOT NULL,
            biz_type TEXT NOT NULL CHECK(biz_type IN ('RECEIPT','PAYMENT','TRANSFER')),
            amount INTEGER NOT NULL CHECK(amount>=0),
            account_name TEXT NOT NULL,
            contact_name TEXT,
            source_doc TEXT,
            receipt_img_url TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','DONE','POSTED')),
            linked_voucher_id INTEGER,
            FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE,
            FOREIGN KEY(account_set_id,linked_voucher_id) REFERENCES vouchers_v7(account_set_id,id) ON UPDATE CASCADE ON DELETE RESTRICT
        );
        CREATE TABLE invoices_v7 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_set_id TEXT NOT NULL,
            invoice_code TEXT,
            invoice_no TEXT NOT NULL,
            invoice_date TEXT NOT NULL,
            buyer_name TEXT NOT NULL,
            buyer_tax_id TEXT,
            seller_name TEXT,
            amount INTEGER NOT NULL DEFAULT 0 CHECK(amount>=0),
            tax_amount INTEGER NOT NULL DEFAULT 0 CHECK(tax_amount>=0),
            payment_method TEXT,
            status TEXT NOT NULL DEFAULT 'DRAFT',
            pdf_or_img_url TEXT,
            items_json TEXT NOT NULL DEFAULT '[]',
            FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE
        );
        CREATE TABLE accounting_periods_v7 (
            account_set_id TEXT NOT NULL,
            period TEXT NOT NULL,
            is_locked INTEGER NOT NULL DEFAULT 0 CHECK(is_locked IN (0,1)),
            locked_at TEXT,
            lock_reason TEXT,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY(account_set_id,period),
            FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE
        );
        CREATE TABLE contact_opening_balances_v7 (
            account_set_id TEXT NOT NULL,
            account_code TEXT NOT NULL,
            contact_id INTEGER NOT NULL,
            as_of_date TEXT NOT NULL,
            initial_debit INTEGER NOT NULL DEFAULT 0 CHECK(initial_debit>=0),
            initial_credit INTEGER NOT NULL DEFAULT 0 CHECK(initial_credit>=0),
            PRIMARY KEY(account_set_id,account_code,contact_id),
            FOREIGN KEY(account_set_id,account_code) REFERENCES accounts_v7(account_set_id,code) ON UPDATE CASCADE ON DELETE CASCADE,
            FOREIGN KEY(account_set_id,contact_id) REFERENCES contacts_v7(account_set_id,id) ON UPDATE CASCADE ON DELETE CASCADE
        );
        """)
        conn.execute(
            "INSERT INTO account_sets(id,name,is_demo) VALUES('default','默认账套',0) "
            "ON CONFLICT(id) DO NOTHING"
        )

        # Keep all legacy normalized rows in the default book without changing IDs.
        conn.execute("INSERT INTO accounts_v7 SELECT id,'default',code,name,category,direction,parent_code,initial_debit,initial_credit,is_active FROM accounts")
        conn.execute("INSERT INTO contacts_v7 SELECT id,'default',name,type,tax_id,contact_info FROM contacts")
        conn.execute("INSERT INTO vouchers_v7(id,account_set_id,voucher_no,voucher_date,summary,status,attachment_urls,created_at,posted_at,reverse_of_id,reversed_by_id,reversed_at) SELECT id,'default',voucher_no,voucher_date,summary,status,attachment_urls,created_at,posted_at,reverse_of_id,reversed_by_id,reversed_at FROM vouchers")
        conn.execute("INSERT INTO voucher_entries_v7 SELECT id,'default',voucher_id,account_code,contact_id,summary,debit_amount,credit_amount FROM voucher_entries")
        conn.execute("INSERT INTO cashier_records_v7 SELECT id,'default',record_date,biz_type,amount,account_name,contact_name,source_doc,receipt_img_url,status,linked_voucher_id FROM cashier_records")
        conn.execute("INSERT INTO invoices_v7 SELECT id,'default',invoice_code,invoice_no,invoice_date,buyer_name,buyer_tax_id,seller_name,amount,tax_amount,payment_method,status,pdf_or_img_url,items_json FROM invoices")
        conn.execute("INSERT INTO accounting_periods_v7 SELECT 'default',period,is_locked,locked_at,lock_reason,updated_at FROM accounting_periods")
        conn.execute("INSERT INTO contact_opening_balances_v7 SELECT 'default',account_code,contact_id,as_of_date,initial_debit,initial_credit FROM contact_opening_balances")

        # Drop legacy children before parents. Foreign keys are disabled only for
        # this controlled copy/swap migration and checked again at the end.
        for table in ("voucher_entries", "cashier_records", "contact_opening_balances", "vouchers", "invoices", "accounting_periods", "accounts", "contacts"):
            conn.execute(f"DROP TABLE {table}")
        for table in ("accounts", "contacts", "vouchers", "voucher_entries", "cashier_records", "invoices", "accounting_periods", "contact_opening_balances"):
            conn.execute(f"ALTER TABLE {table}_v7 RENAME TO {table}")

        for statement in (
            "CREATE INDEX IF NOT EXISTS idx_accounts_tenant_parent ON accounts(account_set_id,parent_code)",
            "CREATE INDEX IF NOT EXISTS idx_contacts_tenant_name ON contacts(account_set_id,name,type)",
            "CREATE INDEX IF NOT EXISTS idx_vouchers_tenant_date_status ON vouchers(account_set_id,voucher_date,status)",
            "CREATE INDEX IF NOT EXISTS idx_voucher_entries_tenant_voucher ON voucher_entries(account_set_id,voucher_id)",
            "CREATE INDEX IF NOT EXISTS idx_voucher_entries_tenant_account ON voucher_entries(account_set_id,account_code)",
            "CREATE INDEX IF NOT EXISTS idx_voucher_entries_tenant_contact ON voucher_entries(account_set_id,account_code,contact_id)",
            "CREATE INDEX IF NOT EXISTS idx_cashier_tenant_date_status ON cashier_records(account_set_id,record_date,status)",
            "CREATE INDEX IF NOT EXISTS idx_cashier_tenant_voucher ON cashier_records(account_set_id,linked_voucher_id)",
            "CREATE INDEX IF NOT EXISTS idx_invoices_tenant_date_no ON invoices(account_set_id,invoice_date,invoice_no)",
        ):
            conn.execute(statement)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")



# CAS first-level chart of accounts. Directions follow the normal balance of
# each account; opening balances remain zero until entered by the operator.
CAS_FIRST_LEVEL_ACCOUNTS = (
    ("1001", "库存现金", "资产", "借"), ("1002", "银行存款", "资产", "借"),
    ("1012", "其他货币资金", "资产", "借"), ("1101", "交易性金融资产", "资产", "借"),
    ("1121", "应收票据", "资产", "借"), ("1122", "应收账款", "资产", "借"),
    ("1123", "预付账款", "资产", "借"), ("1131", "应收股利", "资产", "借"),
    ("1132", "应收利息", "资产", "借"), ("1221", "其他应收款", "资产", "借"),
    ("1231", "坏账准备", "资产", "贷"), ("1401", "材料采购", "资产", "借"),
    ("1402", "在途物资", "资产", "借"), ("1403", "原材料", "资产", "借"),
    ("1404", "材料成本差异", "资产", "借"), ("1405", "库存商品", "资产", "借"),
    ("1406", "发出商品", "资产", "借"), ("1407", "商品进销差价", "资产", "贷"),
    ("1408", "委托加工物资", "资产", "借"), ("1411", "周转材料", "资产", "借"),
    ("1471", "存货跌价准备", "资产", "贷"), ("1501", "持有待售资产", "资产", "借"),
    ("1511", "长期股权投资", "资产", "借"), ("1521", "投资性房地产", "资产", "借"),
    ("1531", "长期应收款", "资产", "借"), ("1601", "固定资产", "资产", "借"),
    ("1602", "累计折旧", "资产", "贷"), ("1603", "固定资产减值准备", "资产", "贷"),
    ("1604", "在建工程", "资产", "借"), ("1605", "工程物资", "资产", "借"),
    ("1606", "固定资产清理", "资产", "借"), ("1701", "无形资产", "资产", "借"),
    ("1702", "累计摊销", "资产", "贷"), ("1703", "无形资产减值准备", "资产", "贷"),
    ("1801", "长期待摊费用", "资产", "借"), ("1811", "递延所得税资产", "资产", "借"),
    ("1901", "待处理财产损溢", "资产", "借"),
    ("2001", "短期借款", "负债", "贷"), ("2101", "交易性金融负债", "负债", "贷"),
    ("2201", "应付票据", "负债", "贷"), ("2202", "应付账款", "负债", "贷"),
    ("2203", "预收账款", "负债", "贷"), ("2211", "应付职工薪酬", "负债", "贷"),
    ("2221", "应交税费", "负债", "贷"), ("2231", "应付利息", "负债", "贷"),
    ("2232", "应付股利", "负债", "贷"), ("2241", "其他应付款", "负债", "贷"),
    ("2401", "递延收益", "负债", "贷"), ("2501", "长期借款", "负债", "贷"),
    ("2502", "应付债券", "负债", "贷"), ("2701", "长期应付款", "负债", "贷"),
    ("2711", "专项应付款", "负债", "贷"), ("2801", "预计负债", "负债", "贷"),
    ("2901", "递延所得税负债", "负债", "贷"),
    ("4001", "实收资本", "权益", "贷"), ("4002", "资本公积", "权益", "贷"),
    ("4101", "盈余公积", "权益", "贷"), ("4103", "本年利润", "权益", "贷"),
    ("4104", "利润分配", "权益", "贷"),
    ("5001", "生产成本", "成本", "借"), ("5101", "制造费用", "成本", "借"),
    ("5201", "劳务成本", "成本", "借"),
    ("6001", "主营业务收入", "损益", "贷"), ("6051", "其他业务收入", "损益", "贷"),
    ("6101", "公允价值变动损益", "损益", "贷"), ("6111", "投资收益", "损益", "贷"),
    ("6117", "资产处置损益", "损益", "贷"), ("6301", "营业外收入", "损益", "贷"),
    ("6401", "主营业务成本", "损益", "借"), ("6402", "其他业务成本", "损益", "借"),
    ("6403", "税金及附加", "损益", "借"), ("6601", "销售费用", "损益", "借"),
    ("6602", "管理费用", "损益", "借"), ("6603", "财务费用", "损益", "借"),
    ("6604", "勘探费用", "损益", "借"), ("6701", "资产减值损失", "损益", "借"),
    ("6711", "营业外支出", "损益", "借"), ("6801", "所得税费用", "损益", "借"),
    ("6901", "以前年度损益调整", "损益", "借"),
)

def ensure_default_accounts(conn: sqlite3.Connection, account_set_id: str) -> int:
    """Idempotently seed CAS first-level accounts for a single account set."""
    conn.executemany(
        """INSERT OR IGNORE INTO accounts
           (account_set_id,code,name,category,direction,parent_code,initial_debit,initial_credit,is_active)
           VALUES(?,?,?,?,?,NULL,0,0,1)""",
        [(account_set_id, code, name, category, direction)
         for code, name, category, direction in CAS_FIRST_LEVEL_ACCOUNTS],
    )
    from .collab_db import seed_details
    seed_details(conn, account_set_id)
    return conn.execute(
        "SELECT COUNT(*) FROM accounts WHERE account_set_id=?", (account_set_id,)
    ).fetchone()[0]

def init_db() -> Path:
    """Create tables and apply safe compatibility and tenant migrations."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        _rebuild_voucher_tables_for_signed_reversals(conn)
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        _migrate_to_tenant_schema(conn)
        _ensure_v8_voucher_columns(conn)
        _ensure_v9_normalized_columns(conn)
        _ensure_v10_business_schema(conn)
        _ensure_v11_schema(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS retired_account_sets(id TEXT PRIMARY KEY)")
        retired_default = conn.execute("SELECT 1 FROM retired_account_sets WHERE id='default'").fetchone()
        if not retired_default:
            conn.execute("INSERT INTO account_sets(id,name,is_demo) VALUES('default','默认账套',0) ON CONFLICT(id) DO NOTHING")
            ensure_default_accounts(conn, "default")
        conn.commit()
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"SQLite foreign-key check failed: {violations!r}")
    finally:
        conn.close()
    return DB_PATH


def _ensure_v10_business_schema(conn: sqlite3.Connection) -> None:
    """Create normalized operating subledgers; additive and safe for existing books."""
    from .business_schema import ensure_business_schema
    ensure_business_schema(conn)
    conn.commit()

# V11 normalized finance workflow tables (tenant-scoped, additive migration).
V11_SCHEMA_SQL = r'''
CREATE TABLE IF NOT EXISTS v11_bank_matches (
 id INTEGER PRIMARY KEY AUTOINCREMENT, account_set_id TEXT NOT NULL, legacy_id TEXT NOT NULL,
 period TEXT NOT NULL, bank_account TEXT NOT NULL, bank_amount INTEGER NOT NULL,
 book_amount INTEGER, bank_date TEXT, company_date TEXT, bank_ref TEXT, book_ref TEXT,
 status TEXT NOT NULL DEFAULT 'MATCH', difference_type TEXT, resolution TEXT, evidence TEXT,
 voucher_no TEXT, voucher_id INTEGER, raw_json TEXT NOT NULL DEFAULT '{}',
 updated_at TEXT NOT NULL, UNIQUE(account_set_id,legacy_id,period,bank_account),
 FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE,
 FOREIGN KEY(voucher_id) REFERENCES vouchers(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_v11_bank_period ON v11_bank_matches(account_set_id,period,bank_account,status);
CREATE TABLE IF NOT EXISTS v11_tax_workpapers (
 id INTEGER PRIMARY KEY AUTOINCREMENT, account_set_id TEXT NOT NULL, period TEXT NOT NULL,
 tax_type TEXT NOT NULL, basis_amount INTEGER NOT NULL DEFAULT 0, tax_amount INTEGER NOT NULL DEFAULT 0,
 rate TEXT, status TEXT NOT NULL DEFAULT 'DRAFT', payload_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
 UNIQUE(account_set_id,period,tax_type), FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS v11_tax_archives (
 id INTEGER PRIMARY KEY AUTOINCREMENT, account_set_id TEXT NOT NULL, period TEXT NOT NULL,
 tax_type TEXT NOT NULL, paid_date TEXT, amount INTEGER NOT NULL, certificate_no TEXT,
 attachment_url TEXT, note TEXT, created_at TEXT NOT NULL,
 FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE
);
'''

def _ensure_v11_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(V11_SCHEMA_SQL)
    conn.commit()
