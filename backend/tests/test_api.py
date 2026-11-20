from __future__ import annotations

import sqlite3
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import database
from backend.main import app
from backend.main_config import UPLOADS_DIR


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "finance_workbench_test.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)

    # Simulate the original initialization version so startup must migrate it.
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL, category TEXT NOT NULL,
            direction TEXT NOT NULL, parent_code TEXT,
            initial_debit INTEGER NOT NULL DEFAULT 0,
            initial_credit INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            type TEXT NOT NULL, tax_id TEXT, contact_info TEXT
        );
        CREATE TABLE vouchers (
            id INTEGER PRIMARY KEY AUTOINCREMENT, voucher_no TEXT NOT NULL UNIQUE,
            voucher_date TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'DRAFT'
              CHECK(status IN ('DRAFT','PENDING','POSTED','VOIDED')),
            attachment_urls TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE TABLE voucher_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT, voucher_id INTEGER NOT NULL,
            account_code TEXT NOT NULL, contact_id INTEGER,
            summary TEXT NOT NULL DEFAULT '',
            debit_amount INTEGER NOT NULL DEFAULT 0 CHECK(debit_amount >= 0),
            credit_amount INTEGER NOT NULL DEFAULT 0 CHECK(credit_amount >= 0),
            FOREIGN KEY(voucher_id) REFERENCES vouchers(id) ON DELETE CASCADE,
            FOREIGN KEY(account_code) REFERENCES accounts(code),
            FOREIGN KEY(contact_id) REFERENCES contacts(id)
        );
        INSERT INTO accounts(code,name,category,direction) VALUES
            ('1001','库存现金','资产','借'),
            ('1002','银行存款','资产','借'),
            ('1122','应收账款','资产','借'),
            ('2202','应付账款','负债','贷'),
            ('6001','主营业务收入','损益','贷');
        INSERT INTO vouchers(voucher_no,voucher_date,summary,status,attachment_urls)
            VALUES('OLD-001','2026-10-01','迁移保留测试','POSTED','[]');
        INSERT INTO voucher_entries(voucher_id,account_code,summary,debit_amount,credit_amount)
            VALUES(1,'1001','借',10000,0);
        INSERT INTO voucher_entries(voucher_id,account_code,summary,debit_amount,credit_amount)
            VALUES(1,'6001','贷',0,10000);
        """
    )
    conn.commit()
    conn.close()

    with TestClient(app) as test_client:
        yield test_client


def _account(client: TestClient, code: str, name: str, category: str, direction: str):
    existing = client.get("/api/accounts", params={"account_set_id": "default", "active_only": "false"}).json()["items"]
    if any(row["code"] == code for row in existing):
        return
    response = client.post(
        "/api/accounts",
        json={
            "code": code,
            "name": name,
            "category": category,
            "direction": direction,
            "initial_debit": "0.00",
            "initial_credit": "0.00",
        },
    )
    assert response.status_code == 201, response.text


def test_core_api_flow_and_legacy_migration(client: TestClient):
    # Health + legacy voucher data preserved by migration.
    assert client.get("/api/health").json() == {"status": "ok", "db": "connected"}
    legacy = client.get("/api/vouchers", params={"status": "POSTED"}).json()["items"]
    assert any(row["voucher_no"] == "OLD-001" for row in legacy)
    assert next(row for row in legacy if row["voucher_no"] == "OLD-001")["account_set_id"] == "default"

    # All five test accounts were carried over from the simulated legacy DB.
    contact = client.post("/api/contacts", json={"name": "测试客户", "type": "CUSTOMER"}).json()

    # Unbalanced voucher is rejected before writing either table.
    invalid = client.post(
        "/api/vouchers",
        json={
            "voucher_date": "2026-11-20",
            "summary": "借贷不平",
            "entries": [
                {"account_code": "1001", "debit_amount": "100.00"},
                {"account_code": "6001", "credit_amount": "99.99"},
            ],
        },
    )
    assert invalid.status_code == 422

    # Voucher create, post, reverse: negative red-letter lines are stored.
    created = client.post(
        "/api/vouchers",
        json={
            "voucher_date": "2026-11-20",
            "summary": "待冲销凭证",
            "entries": [
                {"account_code": "1001", "debit_amount": "100.00"},
                {"account_code": "6001", "credit_amount": "100.00"},
            ],
        },
    )
    assert created.status_code == 201, created.text
    voucher_id = created.json()["id"]
    assert client.post(f"/api/vouchers/{voucher_id}/post").status_code == 409  # 未审核不能过账
    assert client.post(f"/api/vouchers/{voucher_id}/approve").status_code == 200
    assert client.post(f"/api/vouchers/{voucher_id}/post").status_code == 200
    reversed_response = client.post(
        f"/api/vouchers/{voucher_id}/reverse", json={"voucher_date": "2026-11-20"}
    )
    assert reversed_response.status_code == 201, reversed_response.text
    assert reversed_response.json()["entries"][0]["debit_amount"] == "-100.00"
    assert reversed_response.json()["entries"][1]["credit_amount"] == "-100.00"
    assert client.get("/api/vouchers", params={"status": "REVERSED"}).status_code == 200

    # Cashier receipt -> draft voucher -> posted status transition.
    cashier = client.post(
        "/api/cashier",
        json={
            "record_date": "2026-11-20",
            "biz_type": "RECEIPT",
            "amount": "55.50",
            "account_name": "银行存款",
            "contact_name": "测试客户",
            "source_doc": "收款单-001",
        },
    )
    assert cashier.status_code == 201, cashier.text
    cashier_id = cashier.json()["id"]
    conversion = client.post(
        f"/api/cashier/{cashier_id}/to-voucher",
        json={
            "cash_account_code": "1002",
            "counterpart_account_code": "1122",
            "voucher_date": "2026-11-20",
        },
    )
    assert conversion.status_code == 201, conversion.text
    generated_id = conversion.json()["voucher"]["id"]
    assert conversion.json()["voucher"]["entries"][1]["contact_id"] == contact["id"]
    assert client.post(f"/api/vouchers/{generated_id}/approve").status_code == 200
    assert client.post(f"/api/vouchers/{generated_id}/post").status_code == 200
    cash_rows = client.get("/api/cashier").json()["items"]
    assert next(row for row in cash_rows if row["id"] == cashier_id)["status"] == "POSTED"

    # Sub-ledger contains contact-level running balance.
    opening = client.put(
        "/api/contact-opening-balances",
        json={
            "account_code": "1122", "contact_id": contact["id"],
            "as_of_date": "2026-10-31", "initial_debit": "200.00", "initial_credit": "0.00"
        },
    )
    assert opening.status_code == 200, opening.text
    ledger = client.get(
        "/api/reports/sub-ledger",
        params={"account_code": "1122", "contact_id": contact["id"], "period": "2026-11"},
    )
    assert ledger.status_code == 200, ledger.text
    assert ledger.json()["opening_balance"] == "200.00"
    assert ledger.json()["items"][0]["credit_amount"] == "55.50"
    assert ledger.json()["ending_balance"] == "144.50"

    # Trial balance reflects the original and red reversal as a net zero movement.
    trial = client.get(
        "/api/reports/trial-balance",
        params={"period": "2026-11", "include_zero": "true"},
    )
    assert trial.status_code == 200, trial.text
    cash_line = next(x for x in trial.json()["items"] if x["account_code"] == "1001")
    assert cash_line["period_debit"] == "0.00"

    # Invoice create + history query.
    invoice = client.post(
        "/api/invoices",
        json={
            "invoice_no": "INV-TEST-001",
            "invoice_date": "2026-11-20",
            "buyer_name": "测试客户",
            "amount": "100.00",
            "tax_amount": "13.00",
            "items": [{"name": "服务", "quantity": 1}],
        },
    )
    assert invoice.status_code == 201, invoice.text
    assert client.get("/api/invoices", params={"invoice_no": "INV-TEST-001"}).json()["items"][0]["amount"] == "100.00"

    # Upload JPG/PNG/PDF signature validation + YYYYMM path + static serving.
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
    uploaded = client.post("/api/upload", files={"file": ("test.png", png, "image/png")})
    assert uploaded.status_code == 201, uploaded.text
    url = uploaded.json()["url"]
    assert url.startswith("/uploads/20") and len(url.split("/")) == 4
    stored_path = UPLOADS_DIR.parent / url.lstrip("/")
    assert stored_path.exists()
    assert client.get(url).status_code == 200
    stored_path.unlink(missing_ok=True)

    # A locked period blocks new vouchers; period lock itself refuses open work.
    draft = client.post(
        "/api/vouchers",
        json={
            "voucher_date": "2026-11-21",
            "summary": "待审核",
            "entries": [
                {"account_code": "1001", "debit_amount": "1.00"},
                {"account_code": "6001", "credit_amount": "1.00"},
            ],
        },
    )
    assert draft.status_code == 201
    lock = client.put("/api/periods/2026-11", json={"is_locked": True, "lock_reason": "测试锁账"})
    assert lock.status_code == 409
    # An empty month can be locked; afterwards new vouchers in it are blocked.
    lock_empty = client.put("/api/periods/2026-12", json={"is_locked": True, "lock_reason": "测试锁账"})
    assert lock_empty.status_code == 200 and lock_empty.json()["is_locked"] is True
    locked_voucher = client.post(
        "/api/vouchers",
        json={
            "voucher_date": "2026-12-01", "summary": "锁定期间测试", "entries": [
                {"account_code": "1001", "debit_amount": "1.00"},
                {"account_code": "6001", "credit_amount": "1.00"},
            ]
        },
    )
    assert locked_voucher.status_code == 409

    # API also accepts PUT /api/cashier?id=... compatibility route.
    update = client.put(
        "/api/cashier",
        params={"id": cashier_id},
        json={"source_doc": "收款单-001-已核实"},
    )
    assert update.status_code == 409  # already posted; immutable


def test_browser_storage_persistence_and_account_set_isolation(client: TestClient):
    rows = [
        {"id": "voucher-1", "voucherNo": "记-202611-001", "date": "2026-11-20", "entries": []},
        {"id": "voucher-2", "voucherNo": "记-202611-002", "date": "2026-11-21", "entries": []},
    ]
    saved = client.put(
        "/api/storage/batch",
        json={
            "account_set_id": "book-a",
            "account_set_name": "测试账套 A",
            "is_demo": False,
            "stores": {
                "VoucherRecords": rows,
                "Workpapers": [{"id": "ar:1", "amount": 12345}],
                "Settings": [{"id": "finance-standard:period", "value": '"2026-11"'}],
            },
            "replace_stores": ["VoucherRecords", "Workpapers", "Settings"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["counts"]["VoucherRecords"] == 2

    read_back = client.get("/api/storage/VoucherRecords", params={"account_set_id": "book-a"})
    assert read_back.status_code == 200
    assert read_back.json()["items"] == rows

    # The same record key in another book must not overwrite the first book.
    client.put(
        "/api/storage/VoucherRecords",
        params={"account_set_id": "book-b", "account_set_name": "测试账套 B"},
        json={"items": [{"id": "voucher-1", "voucherNo": "另一账套凭证"}]},
    )
    assert client.get("/api/storage/VoucherRecords", params={"account_set_id": "book-a"}).json()["items"][0]["voucherNo"] == "记-202611-001"
    assert client.get("/api/storage/VoucherRecords", params={"account_set_id": "book-b"}).json()["items"][0]["voucherNo"] == "另一账套凭证"

    # Insert-only is enforced transactionally.
    conflict = client.put(
        "/api/storage/batch",
        json={
            "account_set_id": "book-a",
            "stores": {"VoucherRecords": [{"id": "voucher-1", "voucherNo": "duplicate"}]},
            "insert_only": {"VoucherRecords": ["voucher-1"]},
        },
    )
    assert conflict.status_code == 409
    assert client.get("/api/storage/VoucherRecords", params={"account_set_id": "book-a"}).json()["items"][0]["voucherNo"] == "记-202611-001"

    # A conflict in one store rolls back changes to every store in the batch.
    atomic_conflict = client.put(
        "/api/storage/batch",
        json={
            "account_set_id": "book-a",
            "stores": {
                "Workpapers": [{"id": "should-rollback", "value": 1}],
                "VoucherRecords": [{"id": "voucher-1", "voucherNo": "duplicate-again"}],
            },
            "insert_only": {"VoucherRecords": ["voucher-1"], "Workpapers": ["should-rollback"]},
        },
    )
    assert atomic_conflict.status_code == 409
    assert all(row["id"] != "should-rollback" for row in client.get("/api/storage/Workpapers", params={"account_set_id": "book-a"}).json()["items"])

    # Delete and clear operations are scoped to the requested account set.
    deleted = client.put(
        "/api/storage/batch",
        json={
            "account_set_id": "book-a",
            "stores": {"VoucherRecords": []},
            "delete_ids": {"VoucherRecords": ["voucher-2"]},
        },
    )
    assert deleted.status_code == 200
    assert len(client.get("/api/storage/VoucherRecords", params={"account_set_id": "book-a"}).json()["items"]) == 1
    assert len(client.get("/api/storage/VoucherRecords", params={"account_set_id": "book-b"}).json()["items"]) == 1


def test_account_set_metadata_and_asset_original_store(client: TestClient):
    meta = client.put(
        "/api/storage/account-sets/book-photo",
        json={"name": "盘点图片测试账套", "is_demo": False},
    )
    assert meta.status_code == 200, meta.text
    assert meta.json()["name"] == "盘点图片测试账套"

    photo = {"id": "photo-1", "name": "original.png", "type": "image/png", "url": "/uploads/202611/photo.png"}
    saved = client.put(
        "/api/storage/AssetCountOriginals",
        params={"account_set_id": "book-photo", "account_set_name": "盘点图片测试账套"},
        json={"items": [photo]},
    )
    assert saved.status_code == 200, saved.text
    loaded = client.get("/api/storage/AssetCountOriginals", params={"account_set_id": "book-photo"})
    assert loaded.status_code == 200
    assert loaded.json()["items"] == [photo]
    books = client.get("/api/storage/account-sets").json()["items"]
    assert any(row["id"] == "book-photo" and row["name"] == "盘点图片测试账套" for row in books)


def test_frontend_served_by_local_api(client: TestClient):
    response = client.get("/app")
    assert response.status_code == 200
    assert "finance workbench" in response.text.lower()
    assert "backend-status-pill" in response.text


def test_idempotent_normalized_migration(client: TestClient):
    payload = {
        "account_set_id": "migration-book",
        "contacts": [{"id": "C-1", "name": "迁移客户", "type": "CUSTOMER"}],
        "vouchers": [{
            "id": "V-LEGACY-1", "voucherNo": "LEGACY-202611-01", "date": "2026-11-01",
            "status": "PENDING", "attachment": "/uploads/202611/test.pdf",
            "entries": [
                {"account": "1001", "summary": "测试借方", "debit": 12.3, "credit": 0},
                {"account": "6001", "summary": "测试贷方", "debit": 0, "credit": 12.3},
            ],
        }],
        "cashier_records": [{"id": "CASH-LEGACY-1", "date": "2026-11-02", "type": "RECEIPT", "amount": 12.3, "account": "银行存款", "status": "PENDING"}],
        "invoices": [{"id": "INV-LEGACY-1", "invoiceNo": "INV-MIG-01", "date": "2026-11-03", "buyer": "迁移客户", "amount": 12.3, "tax": 0.73}],
    }
    first = client.post("/api/migrate-from-local", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["counts"] == {"accounts": 0, "contacts": 1, "vouchers": 1, "cashier_records": 1, "invoices": 1, "skipped": 0}
    second = client.post("/api/migrate-from-local", json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["counts"]["skipped"] == 4
    assert len(client.get("/api/vouchers", params={"month": "2026-11", "account_set_id": "migration-book"}).json()["items"]) == 1
    assert len(client.get("/api/cashier", params={"account_set_id": "migration-book"}).json()["items"]) == 1
    assert len(client.get("/api/invoices", params={"account_set_id": "migration-book"}).json()["items"]) == 1


def test_normalized_tables_are_isolated_by_account_set(client: TestClient):
    for book in ("book-alpha", "book-beta"):
        response = client.put(f"/api/storage/account-sets/{book}", json={"name": book, "is_demo": False})
        assert response.status_code == 200, response.text
        account = client.get("/api/accounts", params={"account_set_id": book}).json()["items"]
        assert any(row["code"] == "1001" for row in account)
        contact = client.post("/api/contacts", params={"account_set_id": book}, json={"name": f"客户-{book}", "type": "CUSTOMER"})
        assert contact.status_code == 201, contact.text
        invoice = client.post("/api/invoices", params={"account_set_id": book}, json={
            "invoice_no": "SAME-INV-001", "invoice_date": "2026-11-20", "buyer_name": book,
            "amount": "10.00", "tax_amount": "0.00"
        })
        assert invoice.status_code == 201, invoice.text
        cashier = client.post("/api/cashier", params={"account_set_id": book}, json={
            "record_date": "2026-11-20", "biz_type": "RECEIPT", "amount": "10.00", "account_name": "银行存款"
        })
        assert cashier.status_code == 201, cashier.text
        voucher = client.post("/api/vouchers", params={"account_set_id": book}, json={
            "voucher_no": "SAME-NO-001", "voucher_date": "2026-11-20", "summary": book,
            "entries": [
                {"account_code": "1001", "debit_amount": "10.00", "credit_amount": "0.00"},
                {"account_code": "1001", "debit_amount": "0.00", "credit_amount": "10.00"},
            ]
        })
        assert voucher.status_code == 201, voucher.text
        assert client.post(f"/api/vouchers/{voucher.json()['id']}/approve", params={"account_set_id": book}).status_code == 200
        assert client.post(f"/api/vouchers/{voucher.json()['id']}/post", params={"account_set_id": book}).status_code == 200

    alpha_vouchers = client.get("/api/vouchers", params={"account_set_id": "book-alpha"}).json()["items"]
    beta_vouchers = client.get("/api/vouchers", params={"account_set_id": "book-beta"}).json()["items"]
    assert len(alpha_vouchers) == len(beta_vouchers) == 1
    assert alpha_vouchers[0]["voucher_no"] == beta_vouchers[0]["voucher_no"] == "SAME-NO-001"
    assert alpha_vouchers[0]["summary"] == "book-alpha"
    assert beta_vouchers[0]["summary"] == "book-beta"
    duplicate_same_book = client.post("/api/vouchers", params={"account_set_id": "book-alpha"}, json={
        "voucher_no": "SAME-NO-001", "voucher_date": "2026-11-21", "entries": [
            {"account_code": "1001", "debit_amount": "1.00"},
            {"account_code": "1001", "credit_amount": "1.00"},
        ]
    })
    assert duplicate_same_book.status_code == 409
    assert len(client.get("/api/invoices", params={"account_set_id": "book-alpha"}).json()["items"]) == 1
    assert len(client.get("/api/invoices", params={"account_set_id": "book-beta"}).json()["items"]) == 1
    assert client.get("/api/invoices", params={"account_set_id": "book-alpha"}).json()["items"][0]["buyer_name"] == "book-alpha"
    assert len(client.get("/api/cashier", params={"account_set_id": "book-alpha"}).json()["items"]) == 1
    assert len(client.get("/api/cashier", params={"account_set_id": "book-beta"}).json()["items"]) == 1

    alpha_trial = client.get("/api/reports/trial-balance", params={"account_set_id": "book-alpha", "period": "2026-11", "include_zero": True})
    beta_trial = client.get("/api/reports/trial-balance", params={"account_set_id": "book-beta", "period": "2026-11", "include_zero": True})
    assert alpha_trial.status_code == beta_trial.status_code == 200
    assert any(x["account_code"] == "1001" and x["account_name"] == "库存现金" for x in alpha_trial.json()["items"])
    assert any(x["account_code"] == "1001" and x["account_name"] == "库存现金" for x in beta_trial.json()["items"])


def test_cross_book_contact_and_voucher_references_are_rejected(client: TestClient):
    for book in ("book-one", "book-two"):
        client.put(f"/api/storage/account-sets/{book}", json={"name": book}).raise_for_status()
        book_accounts = client.get("/api/accounts", params={"account_set_id": book}).json()["items"]
        assert {"1001", "6001"}.issubset({row["code"] for row in book_accounts})
    contact_one = client.post("/api/contacts", params={"account_set_id": "book-one"}, json={"name": "仅属于一号账套", "type": "CUSTOMER"}).json()
    bad = client.post("/api/vouchers", params={"account_set_id": "book-two"}, json={
        "voucher_date": "2026-11-20", "entries": [
            {"account_code": "1001", "debit_amount": "1.00", "contact_id": contact_one["id"]},
            {"account_code": "6001", "credit_amount": "1.00"},
        ]
    })
    assert bad.status_code == 422

    missing = client.post("/api/vouchers/999999/post", params={"account_set_id": "book-two"})
    assert missing.status_code == 404


def test_period_locks_are_isolated_between_books(client: TestClient):
    for book in ("lock-a", "lock-b"):
        client.put(f"/api/storage/account-sets/{book}", json={"name": book}).raise_for_status()
        book_accounts = client.get("/api/accounts", params={"account_set_id": book}).json()["items"]
        assert {"1001", "6001"}.issubset({row["code"] for row in book_accounts})
    locked = client.put("/api/periods/2026-11", params={"account_set_id": "lock-a"}, json={"is_locked": True, "lock_reason": "仅锁定 A"})
    assert locked.status_code == 200
    a = client.post("/api/vouchers", params={"account_set_id": "lock-a"}, json={
        "voucher_date": "2026-11-20", "entries": [
            {"account_code": "1001", "debit_amount": "1.00"},
            {"account_code": "6001", "credit_amount": "1.00"},
        ]
    })
    assert a.status_code == 409
    b = client.post("/api/vouchers", params={"account_set_id": "lock-b"}, json={
        "voucher_date": "2026-11-20", "entries": [
            {"account_code": "1001", "debit_amount": "1.00"},
            {"account_code": "6001", "credit_amount": "1.00"},
        ]
    })
    assert b.status_code == 201, b.text
    # A period lock in A must not make B locked.
    unlocked_periods = client.get("/api/periods", params={"account_set_id": "lock-b"}).json()["items"]
    assert not any(p["period"] == "2026-11" and p["is_locked"] for p in unlocked_periods)


def test_tenant_schema_is_idempotent_and_foreign_keys_are_valid(client: TestClient):
    conn = database.get_connection()
    try:
        required = {
            "accounts", "contacts", "vouchers", "voucher_entries",
            "cashier_records", "invoices", "accounting_periods",
            "contact_opening_balances",
        }
        for table in required:
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            assert "account_set_id" in columns, (table, columns)
        before = conn.execute("SELECT COUNT(*) n FROM vouchers WHERE account_set_id='default'").fetchone()["n"]
    finally:
        conn.close()

    # A second startup must not reassign or duplicate any migrated records.
    database.init_db()
    conn = database.get_connection()
    try:
        after = conn.execute("SELECT COUNT(*) n FROM vouchers WHERE account_set_id='default'").fetchone()["n"]
        assert after == before
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_account_migration_resolves_parent_after_child_order(client: TestClient):
    response = client.post("/api/migrate-from-local", json={
        "account_set_id": "chart-order-book",
        "accounts": [
            {"code": "100101", "name": "银行存款-基本户", "category": "资产", "direction": "借", "parent_code": "1001"},
            {"code": "1001", "name": "库存现金及银行存款", "category": "资产", "direction": "借"},
        ]
    })
    assert response.status_code == 200, response.text
    rows = client.get("/api/accounts", params={"account_set_id": "chart-order-book", "active_only": "false"}).json()["items"]
    child = next(row for row in rows if row["code"] == "100101")
    assert child["parent_code"] == "1001"


def test_full_legacy_finance_rows_migrate_to_default_book(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "legacy_full.db"
    monkeypatch.setattr(database, "DB_PATH", db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(database.SCHEMA_SQL)
    conn.execute("INSERT INTO account_sets(id,name) VALUES('default','默认账套')")
    conn.execute("INSERT INTO accounts(code,name,category,direction,initial_debit) VALUES('1122','应收账款','资产','借',5000)")
    conn.execute("INSERT INTO contacts(name,type) VALUES('旧客户','CUSTOMER')")
    conn.execute("INSERT INTO vouchers(voucher_no,voucher_date,summary,status,attachment_urls) VALUES('KEEP-001','2026-10-10','旧凭证','POSTED','[\\\"/uploads/202610/a.png\\\"]')")
    vid = conn.execute("SELECT id FROM vouchers WHERE voucher_no='KEEP-001'").fetchone()[0]
    cid = conn.execute("SELECT id FROM contacts WHERE name='旧客户'").fetchone()[0]
    conn.execute("INSERT INTO voucher_entries(voucher_id,account_code,contact_id,summary,debit_amount,credit_amount) VALUES(?,?,?,?,?,?)",(vid,'1122',cid,'旧分录',5000,0))
    # A balancing credit entry uses another legacy account.
    conn.execute("INSERT INTO accounts(code,name,category,direction) VALUES('4001','收入','损益','贷')")
    conn.execute("INSERT INTO voucher_entries(voucher_id,account_code,summary,debit_amount,credit_amount) VALUES(?,?,?,?,?)",(vid,'4001','贷方',0,5000))
    conn.execute("INSERT INTO cashier_records(record_date,biz_type,amount,account_name,status,linked_voucher_id) VALUES('2026-10-11','RECEIPT',5000,'银行存款','POSTED',?)",(vid,))
    conn.execute("INSERT INTO invoices(invoice_no,invoice_date,buyer_name,amount,tax_amount) VALUES('OLD-INV-1','2026-10-12','旧客户',5000,650)")
    conn.execute("INSERT INTO accounting_periods(period,is_locked,lock_reason) VALUES('2026-09',1,'旧期间已结账')")
    conn.execute("INSERT INTO contact_opening_balances(account_code,contact_id,as_of_date,initial_debit) VALUES('1122',?,'2026-09-30',3000)",(cid,))
    conn.commit(); conn.close()

    database.init_db()
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT initial_debit FROM accounts WHERE account_set_id='default' AND code='1122'").fetchone()[0] == 5000
        assert conn.execute("SELECT voucher_no FROM vouchers WHERE account_set_id='default' AND id=?",(vid,)).fetchone()[0] == "KEEP-001"
        assert conn.execute("SELECT COUNT(*) FROM voucher_entries WHERE account_set_id='default' AND voucher_id=?",(vid,)).fetchone()[0] == 2
        assert conn.execute("SELECT linked_voucher_id FROM cashier_records WHERE account_set_id='default'").fetchone()[0] == vid
        assert conn.execute("SELECT invoice_no FROM invoices WHERE account_set_id='default'").fetchone()[0] == "OLD-INV-1"
        assert conn.execute("SELECT is_locked FROM accounting_periods WHERE account_set_id='default' AND period='2026-09'").fetchone()[0] == 1
        assert conn.execute("SELECT initial_debit FROM contact_opening_balances WHERE account_set_id='default' AND account_code='1122' AND contact_id=?",(cid,)).fetchone()[0] == 3000
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_v8_normalized_voucher_crud_and_legacy_mapping(client: TestClient):
    _account(client, "6602", "管理费用", "损益", "借")
    created = client.post(
        "/api/vouchers",
        json={
            "legacy_id": "browser-voucher-abc",
            "voucher_no": "记-202611-ABC",
            "voucher_date": "2026-11-20",
            "summary": "V8 规范化凭证",
            "biz_label": "费用报销",
            "source": "BX-001",
            "source_type": "报销单",
            "counterparty": "测试员工",
            "memo": "V8 测试摘要",
            "attachment_name": "receipt.png",
            "attachment_urls": ["/uploads/202611/receipt.png"],
            "entries": [
                {"account_code": "6602", "aux_name": "部门一", "summary": "费用", "debit_amount": "88.00"},
                {"account_code": "1001", "summary": "现金支付", "credit_amount": "88.00"},
            ],
        },
    )
    assert created.status_code == 201, created.text
    row = created.json()
    assert row["legacy_id"] == "browser-voucher-abc"
    assert row["entries"][0]["aux_name"] == "部门一"
    assert row["attachment_urls"] == ["/uploads/202611/receipt.png"]

    listed = client.get("/api/vouchers", params={"legacy_id": "browser-voucher-abc"}).json()["items"]
    assert len(listed) == 1 and listed[0]["voucher_no"] == "记-202611-ABC"
    duplicate_legacy = client.post("/api/vouchers", json={
        "legacy_id":"browser-voucher-abc", "voucher_no":"记-202611-OTHER", "voucher_date":"2026-11-20",
        "entries":[{"account_code":"6602","debit_amount":"1.00"},{"account_code":"1001","credit_amount":"1.00"}]
    })
    assert duplicate_legacy.status_code == 409

    update = client.put(
        f"/api/vouchers/{row['id']}",
        json={"memo": "已修订摘要", "entries": [
            {"account_code": "6602", "aux_name": "部门二", "summary": "修订费用", "debit_amount": "90.00"},
            {"account_code": "1001", "summary": "现金支付", "credit_amount": "90.00"},
        ]},
    )
    assert update.status_code == 200, update.text
    assert update.json()["memo"] == "已修订摘要"
    assert update.json()["entries"][0]["debit_amount"] == "90.00"
    invalid_update = client.put(f"/api/vouchers/{row['id']}", json={
        "memo":"不应提交", "entries":[
            {"account_code":"6602","debit_amount":"90.00"},
            {"account_code":"1001","credit_amount":"89.00"}
        ]
    })
    assert invalid_update.status_code == 422
    after_rollback = client.get("/api/vouchers", params={"legacy_id":"browser-voucher-abc"}).json()["items"][0]
    assert after_rollback["memo"] == "已修订摘要" and after_rollback["entries"][0]["debit_amount"] == "90.00"

    assert client.post(f"/api/vouchers/{row['id']}/approve").status_code == 200
    assert client.get("/api/vouchers", params={"status": "APPROVED"}).json()["items"]
    assert client.post(f"/api/vouchers/{row['id']}/post").status_code == 200
    assert client.put(f"/api/vouchers/{row['id']}", json={"memo": "不能修改"}).status_code == 409
    assert client.delete(f"/api/vouchers/{row['id']}").status_code == 409

    conn = database.get_connection()
    try:
        voucher_count = conn.execute("SELECT COUNT(*) FROM vouchers WHERE account_set_id='default'").fetchone()[0]
        entry_count = conn.execute("SELECT COUNT(*) FROM voucher_entries WHERE account_set_id='default'").fetchone()[0]
        legacy_count = conn.execute("SELECT COUNT(*) FROM app_records WHERE account_set_id='default' AND store_name='VoucherRecords'").fetchone()[0]
        assert voucher_count >= 2 and entry_count >= 4
        # Direct API create/update did not create an app_records shadow copy.
        assert legacy_count == 0
    finally:
        conn.close()


def test_v8_backfills_v7_voucher_metadata_from_app_records(client: TestClient):
    conn = database.get_connection()
    try:
        conn.execute("INSERT INTO vouchers(account_set_id,voucher_no,voucher_date,summary,status,attachment_urls) VALUES('default','V7-BACKFILL-01','2026-11-20','旧凭证','PENDING','[]')")
        voucher_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("INSERT INTO voucher_entries(account_set_id,voucher_id,account_code,summary,debit_amount,credit_amount) VALUES('default',?,'1001','旧借方',1234,0)",(voucher_id,))
        conn.execute("INSERT INTO voucher_entries(account_set_id,voucher_id,account_code,summary,debit_amount,credit_amount) VALUES('default',?,'6001','旧贷方',0,1234)",(voucher_id,))
        payload = {
            "id":"legacy-v7-voucher-01", "voucherNo":"V7-BACKFILL-01", "date":"2026-11-20",
            "status":"APPROVED", "bizLabel":"历史费用", "source":"V7-SRC", "sourceType":"报销单",
            "attachment":{"name":"回单.png","url":"/uploads/202611/old.png"},
            "entries":[
                {"account":"1001","aux":"工商银行基本户","summary":"旧借方","debit":12.34,"credit":0},
                {"account":"6001","summary":"旧贷方","debit":0,"credit":12.34}
            ]
        }
        conn.execute("INSERT INTO normalized_migration_map(account_set_id,module,source_id,target_id) VALUES('default','vouchers','legacy-v7-voucher-01',?)",(voucher_id,))
        conn.execute("INSERT INTO app_records(account_set_id,store_name,record_id,payload_json) VALUES('default','VoucherRecords','legacy-v7-voucher-01',?)",(__import__('json').dumps(payload,ensure_ascii=False),))
        conn.commit()
    finally:
        conn.close()
    database.init_db()
    migrated = client.get("/api/vouchers", params={"legacy_id":"legacy-v7-voucher-01"}).json()["items"]
    assert len(migrated) == 1
    assert migrated[0]["reviewed_at"]
    assert migrated[0]["biz_label"] == "历史费用"
    assert migrated[0]["attachment_urls"] == ["/uploads/202611/old.png"]
    assert migrated[0]["entries"][0]["aux_name"] == "工商银行基本户"


def test_v8_batch_post_is_atomic(client: TestClient):
    ids = []
    for number in (1, 2):
        created = client.post("/api/vouchers", json={
            "legacy_id":f"batch-voucher-{number}", "voucher_no":f"BATCH-{number}",
            "voucher_date":"2026-11-20", "summary":"批量过账原子性测试",
            "entries":[
                {"account_code":"1001","summary":"借方","debit_amount":"20.00"},
                {"account_code":"6001","summary":"贷方","credit_amount":"20.00"}
            ]
        })
        assert created.status_code == 201, created.text
        ids.append(created.json()["id"])
    approved = client.post("/api/vouchers/batch-approve", json={"ids":ids})
    assert approved.status_code == 200, approved.text
    conn = database.get_connection()
    try:
        conn.execute("UPDATE voucher_entries SET credit_amount=1999 WHERE voucher_id=? AND account_code='6001'", (ids[1],))
        conn.commit()
    finally:
        conn.close()
    failed = client.post("/api/vouchers/batch-post", json={"ids":ids})
    assert failed.status_code == 422
    states = client.get("/api/vouchers", params={"month":"2026-11"}).json()["items"]
    assert all(next(v for v in states if v["id"] == item)["status"] == "PENDING" for item in ids)
    conn = database.get_connection()
    try:
        conn.execute("UPDATE voucher_entries SET credit_amount=2000 WHERE voucher_id=? AND account_code='6001'", (ids[1],))
        conn.commit()
    finally:
        conn.close()
    posted = client.post("/api/vouchers/batch-post", json={"ids":ids})
    assert posted.status_code == 200, posted.text
    assert all(item["status"] == "POSTED" for item in posted.json()["items"])


def test_v9_normalized_cashier_invoice_reference_and_reports(client: TestClient):
    # Account and contact setup are tenant scoped and mutable through normalized tables.
    account_update = client.put(
        "/api/accounts/1001",
        json={"name": "库存现金（V9）", "initial_debit": "125.00", "initial_credit": "0.00"},
    )
    assert account_update.status_code == 200, account_update.text
    assert account_update.json()["initial_debit"] == "125.00"

    contact = client.post("/api/contacts", json={"name": "V9往来客户", "type": "CUSTOMER"}).json()
    contact_update = client.put(f"/api/contacts/{contact['id']}", json={"tax_id": "91330000V9"})
    assert contact_update.status_code == 200, contact_update.text
    assert contact_update.json()["tax_id"] == "91330000V9"

    # Direct cashier create/read/update/delete uses cashier_records, not app_records.
    cashier = client.post(
        "/api/cashier",
        json={
            "legacy_id": "legacy-cash-v9-001",
            "record_date": "2026-11-20",
            "biz_type": "TRANSFER",
            "direction": "OUT",
            "transfer_id": "transfer-v9-001",
            "amount": "88.88",
            "account_name": "银行存款",
            "source_doc": "转账单-V9",
            "status": "DONE",
            "extra": {"type": "TRANSFER_OUT", "counterpartyAccount": "CMB"},
        },
    )
    assert cashier.status_code == 201, cashier.text
    cash_id = cashier.json()["id"]
    listed = client.get("/api/cashier", params={"account_set_id": "default"}).json()["items"]
    stored = next(x for x in listed if x["id"] == cash_id)
    assert stored["legacy_id"] == "legacy-cash-v9-001"
    assert stored["direction"] == "OUT" and stored["extra"]["type"] == "TRANSFER_OUT"
    updated = client.put(f"/api/cashier/{cash_id}", json={"status": "DONE", "extra": {"note": "updated"}})
    assert updated.status_code == 200, updated.text
    assert updated.json()["extra"]["note"] == "updated"
    deleted = client.delete(f"/api/cashier/{cash_id}")
    assert deleted.status_code == 200

    # Invoice legacy ID, source and arbitrary UI metadata round-trip through invoices.
    invoice = client.post(
        "/api/invoices",
        json={
            "legacy_id": "legacy-invoice-v9-001",
            "invoice_no": "INV-V9-001",
            "invoice_date": "2026-11-20",
            "buyer_name": "V9往来客户",
            "seller_name": "本公司",
            "amount": "188.00",
            "tax_amount": "24.44",
            "source": "合同-V9",
            "items": [{"name": "服务", "qty": 2, "price": 94}],
            "extra": {"evidence": "合同附件", "stamp": "seal-data"},
        },
    )
    assert invoice.status_code == 201, invoice.text
    inv_id = invoice.json()["id"]
    assert invoice.json()["legacy_id"] == "legacy-invoice-v9-001"
    assert invoice.json()["extra"]["stamp"] == "seal-data"
    updated_invoice = client.put(f"/api/invoices/{inv_id}", json={"status": "已开票", "amount": "190.00"})
    assert updated_invoice.status_code == 200, updated_invoice.text
    assert updated_invoice.json()["amount"] == "190.00"
    assert client.delete(f"/api/invoices/{inv_id}").status_code == 200

    # SQL trial balance reads normalized accounts and posted voucher entries.
    trial = client.get("/api/reports/trial-balance", params={"period": "2026-11", "include_zero": "true"})
    assert trial.status_code == 200, trial.text
    cash_line = next(x for x in trial.json()["items"] if x["account_code"] == "1001")
    assert cash_line["initial_debit"] == "225.00"  # opening plus the historical posted voucher
    # Contact opening is independently normalized and queryable through the SQL sub-ledger.
    opening = client.put(
        "/api/contact-opening-balances",
        json={"account_code": "1122", "contact_id": contact["id"], "as_of_date": "2026-10-31", "initial_debit": "77.00", "initial_credit": "0.00"},
    )
    assert opening.status_code == 200
    sub = client.get("/api/reports/sub-ledger", params={"account_code": "1122", "contact_id": contact["id"], "period": "2026-11"})
    assert sub.status_code == 200 and sub.json()["opening_balance"] == "77.00"

    # No newly created V9 normalized domain record is written to the generic store.
    conn = database.get_connection()
    try:
        assert conn.execute("SELECT COUNT(*) FROM app_records WHERE account_set_id='default' AND store_name IN ('Invoices','CashierRecords')").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_v9_cashier_transfer_batch_and_opening_balance_atomicity(client: TestClient):
    batch = client.post(
        "/api/cashier/batch",
        json={"records": [
            {"legacy_id": "v9-transfer-out", "transfer_id": "v9-transfer", "direction": "OUT", "record_date": "2026-11-20", "biz_type": "TRANSFER", "amount": "20.00", "account_name": "银行A", "status": "DONE", "extra": {"type": "TRANSFER_OUT"}},
            {"legacy_id": "v9-transfer-in", "transfer_id": "v9-transfer", "direction": "IN", "record_date": "2026-11-20", "biz_type": "TRANSFER", "amount": "20.00", "account_name": "银行B", "status": "DONE", "extra": {"type": "TRANSFER_IN"}}
        ]},
    )
    assert batch.status_code == 201, batch.text
    assert batch.json()["count"] == 2
    assert {x["direction"] for x in batch.json()["items"]} == {"OUT", "IN"}

    # Duplicate IDs inside a batch are rejected before any row is committed.
    failed = client.post(
        "/api/cashier/batch",
        json={"records": [
            {"legacy_id": "v9-dup", "record_date": "2026-11-20", "biz_type": "RECEIPT", "amount": "1.00", "account_name": "银行A"},
            {"legacy_id": "v9-dup", "record_date": "2026-11-20", "biz_type": "RECEIPT", "amount": "2.00", "account_name": "银行A"}
        ]},
    )
    assert failed.status_code == 422
    rows = client.get("/api/cashier", params={"account_set_id": "default"}).json()["items"]
    assert not any(x.get("legacy_id") == "v9-dup" for x in rows)

    # Bulk account opening updates are all-or-nothing when one code is missing.
    before = client.get("/api/accounts", params={"account_set_id": "default", "active_only": "false"}).json()["items"]
    old_cash = next(x for x in before if x["code"] == "1001")["initial_debit"]
    failed_opening = client.put(
        "/api/accounts/opening-balances",
        json={"items": [
            {"code": "1001", "initial_debit": "999.00", "initial_credit": "0.00"},
            {"code": "NOT-EXIST", "initial_debit": "1.00", "initial_credit": "0.00"}
        ]},
    )
    assert failed_opening.status_code == 404
    after = client.get("/api/accounts", params={"account_set_id": "default", "active_only": "false"}).json()["items"]
    assert next(x for x in after if x["code"] == "1001")["initial_debit"] == old_cash


def test_v9_backfills_v8_cashier_and_invoice_legacy_metadata(client: TestClient):
    conn=database.get_connection()
    try:
        cashier_id=conn.execute("INSERT INTO cashier_records(account_set_id,record_date,biz_type,amount,account_name,status) VALUES('default','2026-11-20','RECEIPT',100,'银行存款','DONE')").lastrowid
        invoice_id=conn.execute("INSERT INTO invoices(account_set_id,invoice_no,invoice_date,buyer_name,amount,tax_amount,items_json) VALUES('default','old-invoice-id','2026-11-20','旧客户',200,26,'[]')").lastrowid
        cash_payload={"id":"cash-v8-legacy","type":"TRANSFER_IN","transferId":"transfer-v8","account":"CMB","amount":1.0}
        invoice_payload={"invoiceNo":"invoice-v8-legacy","invoiceNumber":"REAL-INV-V8","date":"2026-11-20","buyer":"旧客户","seller":"本公司","total":2.0,"source":"合同V8"}
        conn.execute("INSERT INTO app_records(account_set_id,store_name,record_id,payload_json) VALUES('default','CashierRecords','cash-v8-legacy',?)",(json.dumps(cash_payload),))
        conn.execute("INSERT INTO app_records(account_set_id,store_name,record_id,payload_json) VALUES('default','Invoices','invoice-v8-legacy',?)",(json.dumps(invoice_payload),))
        conn.execute("INSERT INTO normalized_migration_map(account_set_id,module,source_id,target_id) VALUES('default','cashier','cash-v8-legacy',?)",(cashier_id,))
        conn.execute("INSERT INTO normalized_migration_map(account_set_id,module,source_id,target_id) VALUES('default','invoices','invoice-v8-legacy',?)",(invoice_id,))
        conn.commit()
        database._ensure_v9_normalized_columns(conn)
        cash=conn.execute("SELECT legacy_id,transfer_id,direction,extra_json FROM cashier_records WHERE id=?",(cashier_id,)).fetchone()
        invoice=conn.execute("SELECT legacy_id,invoice_no,source,extra_json FROM invoices WHERE id=?",(invoice_id,)).fetchone()
        assert cash["legacy_id"]=="cash-v8-legacy" and cash["transfer_id"]=="transfer-v8" and cash["direction"]=="IN"
        assert json.loads(cash["extra_json"])["type"]=="TRANSFER_IN"
        assert invoice["legacy_id"]=="invoice-v8-legacy" and invoice["invoice_no"]=="REAL-INV-V8" and invoice["source"]=="合同V8"
    finally:
        conn.close()

    # First V9 startup syncs the last V8 snapshot (including uploaded URL paths)
    # once; later stale snapshots cannot overwrite normalized live values.
    sync_payload={
        "account_set_id":"default",
        "cashier_records":[{"id":"cash-v8-legacy","date":"2026-11-20","type":"TRANSFER_IN","transferId":"transfer-v8-final","amount":3.0,"account":"CMB","status":"DONE","attachment":{"url":"/uploads/202611/cash-v9.png"}}],
        "invoices":[{"invoiceNo":"invoice-v8-legacy","invoiceNumber":"REAL-INV-V9","date":"2026-11-20","buyer":"旧客户","seller":"本公司","total":4.0,"source":"合同V9","attachments":[{"url":"/uploads/202611/invoice-v9.png"}]}]
    }
    sync=client.post("/api/migrate-from-local",json=sync_payload)
    assert sync.status_code==200, sync.text
    status=client.get("/api/migrate-from-local/status").json()
    assert "cash-v8-legacy" in status["cashier_synced_ids"] and "invoice-v8-legacy" in status["invoice_synced_ids"]
    conn=database.get_connection()
    try:
        cash=conn.execute("SELECT amount,receipt_img_url,direction FROM cashier_records WHERE id=?",(cashier_id,)).fetchone()
        invoice=conn.execute("SELECT invoice_no,amount,pdf_or_img_url FROM invoices WHERE id=?",(invoice_id,)).fetchone()
        assert cash["amount"]==300 and cash["receipt_img_url"]=="/uploads/202611/cash-v9.png" and cash["direction"]=="IN"
        assert invoice["invoice_no"]=="REAL-INV-V9" and invoice["amount"]==400 and invoice["pdf_or_img_url"]=="/uploads/202611/invoice-v9.png"
    finally:
        conn.close()
    stale={**sync_payload,"cashier_records":[{**sync_payload["cashier_records"][0],"amount":999.0}],"invoices":[{**sync_payload["invoices"][0],"total":999.0}]}
    assert client.post("/api/migrate-from-local",json=stale).status_code==200
    conn=database.get_connection()
    try:
        assert conn.execute("SELECT amount FROM cashier_records WHERE id=?",(cashier_id,)).fetchone()[0]==300
        assert conn.execute("SELECT amount FROM invoices WHERE id=?",(invoice_id,)).fetchone()[0]==400
    finally:
        conn.close()


def test_v9_cashier_batch_update_delete_and_voucher_link(client: TestClient):
    created = client.post(
        "/api/cashier/batch",
        json={"records": [
            {"legacy_id": "pair-out", "transfer_id": "pair-1", "direction": "OUT", "record_date": "2026-11-20", "biz_type": "TRANSFER", "amount": "10.00", "account_name": "银行A"},
            {"legacy_id": "pair-in", "transfer_id": "pair-1", "direction": "IN", "record_date": "2026-11-20", "biz_type": "TRANSFER", "amount": "10.00", "account_name": "银行B"}
        ]},
    )
    assert created.status_code == 201, created.text
    ids = [row["id"] for row in created.json()["items"]]
    failed_update = client.put(
        "/api/cashier/batch/update",
        json={"items": [
            {"id": ids[0], "changes": {"status": "DONE"}},
            {"id": 999999, "changes": {"status": "DONE"}}
        ]},
    )
    assert failed_update.status_code == 404
    still_pending = client.get("/api/cashier").json()["items"]
    assert all(next(row for row in still_pending if row["id"] == rid)["status"] == "PENDING" for rid in ids)
    update = client.put(
        "/api/cashier/batch/update",
        json={"items": [
            {"id": ids[0], "changes": {"status": "DONE", "extra": {"updated": True}}},
            {"id": ids[1], "changes": {"status": "DONE", "extra": {"updated": True}}}
        ]},
    )
    assert update.status_code == 200, update.text
    assert all(row["status"] == "DONE" and row["extra"]["updated"] for row in update.json()["items"])

    voucher = client.post(
        "/api/vouchers",
        json={"voucher_date": "2026-11-20", "summary": "出纳关联测试", "entries": [
            {"account_code": "1001", "debit_amount": "10.00"},
            {"account_code": "6001", "credit_amount": "10.00"}
        ]},
    ).json()
    linked = client.post(f"/api/cashier/link-voucher/{voucher['id']}", json=ids)
    assert linked.status_code == 200, linked.text
    assert client.post(f"/api/vouchers/{voucher['id']}/approve").status_code == 200
    assert client.post(f"/api/vouchers/{voucher['id']}/post").status_code == 200
    after_post = client.get("/api/cashier").json()["items"]
    assert all(next(row for row in after_post if row["id"] == rid)["status"] == "POSTED" for rid in ids)
    assert client.post("/api/cashier/batch/delete", json={"ids": ids}).status_code == 409


def _v10_account(client: TestClient, code: str, name: str, category: str, direction: str):
    existing = client.get("/api/accounts", params={"account_set_id": "default", "active_only": "false"}).json()["items"]
    if any(row["code"] == code for row in existing):
        return
    response = client.post("/api/accounts", json={
        "code": code, "name": name, "category": category, "direction": direction,
        "initial_debit": "0.00", "initial_credit": "0.00"
    })
    assert response.status_code == 201, response.text


def test_v10_normalized_operating_subledgers(client: TestClient):
    # Add the inventory, WIP and payroll accounts required by the operating subledgers.
    _v10_account(client, "1401", "原材料", "资产", "借")
    _v10_account(client, "1405", "库存商品", "资产", "借")
    _v10_account(client, "5001", "生产成本", "成本", "借")
    _v10_account(client, "6602", "管理费用", "损益", "借")
    _v10_account(client, "1602", "累计折旧", "资产", "贷")
    _v10_account(client, "2221", "应交税费", "负债", "贷")

    opening = {
        "id": "inventory-opening", "period": "2026-11", "verified": True,
        "rows": [
            {"account": "1401", "sku": "RM-001", "name": "钢材", "warehouse": "主仓库", "qty": 10, "unitCostCents": 1000, "stockCostCents": 10000},
            {"account": "1405", "sku": "FG-001", "name": "成品", "warehouse": "主仓库", "qty": 5, "unitCostCents": 3000, "stockCostCents": 15000}
        ],
        "totalCents": 25000, "totalByAccount": {"1401": 10000, "1405": 15000}
    }
    response = client.post("/api/business/records", json={"records": [opening], "insert_only_ids": ["inventory-opening"]})
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["rows"][0]["sku"] == "RM-001"
    # Duplicate insert-only is rejected; idempotent upsert without insert-only is safe.
    assert client.post("/api/business/records", json={"records": [opening], "insert_only_ids": ["inventory-opening"]}).status_code == 409
    assert client.post("/api/business/records", json={"records": [opening]}).status_code == 200

    purchase = {
        "id": "inventory-doc:PO-001", "kind": "PURCHASE", "docNo": "PO-001", "date": "2026-11-10", "period": "2026-11",
        "supplier": "测试供应商", "inventoryAccount": "1401", "sku": "RM-001", "name": "钢材", "warehouse": "主仓库",
        "qty": 2, "unitCostCents": 1200, "netCents": 2400, "taxCents": 312, "stockCostCents": 2400,
        "grossCents": 2712, "deductible": True, "vatRate": 13, "voucherId": "v-po-001", "createdAt": "2026-11-10T10:00:00Z"
    }
    response = client.post("/api/business/records", json={"records": [purchase]})
    assert response.status_code == 200, response.text
    # Link the normalized business document to a real, posted normalized voucher.
    purchase_voucher = client.post("/api/vouchers", json={
        "legacy_id": "v-po-001", "voucher_no": "V10-PO-001", "voucher_date": "2026-11-10", "summary": "采购入库",
        "entries": [
            {"account_code": "1401", "debit_amount": "24.00"},
            {"account_code": "2221", "debit_amount": "3.12"},
            {"account_code": "2202", "credit_amount": "27.12"}
        ]
    })
    assert purchase_voucher.status_code == 201, purchase_voucher.text
    pid=purchase_voucher.json()["id"]
    assert client.post(f"/api/vouchers/{pid}/approve").status_code == 200
    assert client.post(f"/api/vouchers/{pid}/post").status_code == 200
    linked=client.post("/api/business/link-voucher",json={"voucher_legacy_id":"v-po-001"})
    assert linked.status_code == 200 and linked.json()["link_count"] >= 1
    # Inventory movement is relational and queryable; purchase increases quantity/value.
    ledger = client.get("/api/business/inventory-ledger", params={"period": "2026-11"}).json()["items"]
    assert any(x["sku"] == "RM-001" and x["quantity_milli"] == 2000 and x["amount_cents"] == 2400 for x in ledger)
    balance=client.get("/api/business/inventory-balance",params={"period":"2026-11"}).json()
    assert any(x["sku"]=="RM-001" and x["quantity_milli"]==12000 and x["amount_cents"]==12400 for x in balance["items"])

    sale = {
        "id": "inventory-doc:SO-001", "kind": "SALE", "docNo": "SO-001", "date": "2026-11-15", "period": "2026-11",
        "customer": "测试客户", "inventoryAccount": "1405", "sku": "FG-001", "name": "成品", "warehouse": "主仓库",
        "qty": 1, "unitPriceCents": 5000, "netCents": 5000, "taxCents": 650, "grossCents": 5650,
        "costUnitCents": 3000, "costCents": 3000, "invoiceRecordId": "INV-001", "voucherId": "v-so-001"
    }
    # The backend independently rejects stock overdraw and a non-moving-average cost.
    overdraw = {**sale, "id": "inventory-doc:SO-OVER", "docNo": "SO-OVER", "qty": 6, "costCents": 18000}
    assert client.post("/api/business/records", json={"records": [overdraw]}).status_code == 409
    bad_cost = {**sale, "id": "inventory-doc:SO-COST", "docNo": "SO-COST", "costCents": 3500}
    assert client.post("/api/business/records", json={"records": [bad_cost]}).status_code == 409
    # Sales require a distinct 1405 item and therefore register the item in the same transaction.
    response = client.post("/api/business/records", json={"records": [sale]})
    assert response.status_code == 200, response.text
    docs = client.get("/api/business/records").json()["items"]
    assert {x["id"] for x in docs} >= {"inventory-opening", "inventory-doc:PO-001", "inventory-doc:SO-001"}

    production = {
        "id": "production-order:MO-001", "orderNo": "MO-001", "date": "2026-11-16", "period": "2026-11",
        "materials": [{"sku": "RM-001", "name": "钢材", "warehouse": "主仓库", "qty": 1, "costCents": 1200}],
        "materialCostCents": 1200, "finishedSku": "FG-002", "finishedName": "加工件", "finishedWarehouse": "成品仓",
        "finishedQty": 1, "issueVoucherId": "v-mo-001", "status": "ISSUE_PENDING"
    }
    response = client.post("/api/business/records", json={"records": [production]})
    assert response.status_code == 200, response.text
    restored_order = next(x for x in response.json()["items"] if x["id"] == "production-order:MO-001")
    assert restored_order["materials"][0]["costCents"] == 1200

    payroll = {
        "id": "payroll-batch:2026-11", "period": "2026-11", "grossCents": 100000, "netCents": 90000,
        "iitCents": 3000, "employeeSocialCents": 5000, "otherCents": 2000, "employerSocialCents": 8000,
        "deductionsCents": 15000, "expenseCents": 108000, "accrualVoucherId": "v-pay-001",
        "rows": [{"code": "E001", "name": "张三", "dept": "行政部", "gross": 100000, "employeeSocial": 5000,
                  "iit": 3000, "other": 2000, "employerSocial": 8000, "net": 90000, "expenseAccount": "6602"}]
    }
    response = client.post("/api/business/records", json={"records": [payroll]})
    assert response.status_code == 200, response.text
    payroll_back = next(x for x in response.json()["items"] if x["id"] == "payroll-batch:2026-11")
    assert payroll_back["rows"][0]["net"] == 90000

    assets = [{"code": "FA-100", "name": "办公电脑", "type": "电子设备", "cost": 240000, "residual": 5,
               "years": 3, "start": "2026-01-01", "dept": "行政部", "place": "办公室", "keeper": "张三", "qty": 1, "active": True}]
    response = client.put("/api/business/assets", json={"items": assets})
    assert response.status_code == 200, response.text
    draft = [{"code": "E002", "name": "李四", "dept": "财务部", "gross": 80000, "employeeSocial": 4000, "iit": 1000,
              "other": 0, "employerSocial": 6000, "net": 75000, "expenseAccount": "6602"}]
    response = client.put("/api/business/payroll-drafts/2026-11", json={"period": "2026-11", "rows": draft})
    assert response.status_code == 200, response.text
    depreciation = [{**assets[0], "amount": 6333, "cumulative": 6333, "net": 233667}]
    response = client.put("/api/business/depreciation/2026-11", json={"period": "2026-11", "rows": depreciation})
    assert response.status_code == 200, response.text
    bootstrap = client.get("/api/business/bootstrap").json()
    assert bootstrap["assets"][0]["code"] == "FA-100"
    assert bootstrap["payroll_drafts"][0]["rows"][0]["code"] == "E002"
    assert bootstrap["depreciation"][0]["rows"][0]["amount"] == 6333
    depr_voucher={"legacy_id":"cost:2026-11:depr:固定资产台账","voucher_no":"V10-DEPR-001","voucher_date":"2026-11-30","summary":"固定资产折旧","status":"PENDING","entries":[
        {"account_code":"6602","debit_amount":"63.33"},{"account_code":"1602","credit_amount":"63.33"}]}
    committed_depr=client.post("/api/business/depreciation/2026-11/commit",json={"period":"2026-11","rows":depreciation,"voucher":depr_voucher})
    assert committed_depr.status_code==200,committed_depr.text
    depr_id=committed_depr.json()["voucher"]["id"]
    assert client.delete("/api/business/depreciation/2026-11").status_code==409
    assert client.delete(f"/api/vouchers/{depr_id}").status_code==200
    assert client.delete("/api/business/depreciation/2026-11").status_code==200

    # SQL-backed data is tenant-isolated and a locked period rejects live operating changes.
    assert client.put("/api/storage/account-sets/other", json={"name": "另一个账套", "is_demo": False}).status_code == 200
    assert client.get("/api/business/records", params={"account_set_id": "other"}).json()["items"] == []
    assert client.put("/api/periods/2026-12", json={"is_locked": True}).status_code == 200
    locked_doc = {**purchase, "id": "inventory-doc:PO-LOCK", "docNo": "PO-LOCK", "date": "2026-12-01", "period": "2026-12"}
    assert client.post("/api/business/records", json={"records": [locked_doc]}).status_code == 409
    reset=client.post("/api/business/reset")
    assert reset.status_code==200
    after=client.get("/api/business/bootstrap").json()
    assert after["records"]==[] and after["assets"]==[] and after["depreciation"]==[] and after["payroll_drafts"]==[]


def test_v10_legacy_workpaper_and_settings_decomposition(client: TestClient):
    _v10_account(client, "1401", "原材料", "资产", "借")
    _v10_account(client, "1405", "库存商品", "资产", "借")
    _v10_account(client, "5001", "生产成本", "成本", "借")
    _v10_account(client, "6602", "管理费用", "损益", "借")
    opening={"id":"inventory-opening","period":"2026-11","verified":True,"totalCents":10000,
             "rows":[{"account":"1401","sku":"LEGACY-RM","name":"旧原料","warehouse":"主仓库","qty":1,"unitCostCents":10000,"stockCostCents":10000}]}
    purchase={"id":"inventory-doc:LEGACY-PO","kind":"PURCHASE","docNo":"LEGACY-PO","date":"2026-11-10","period":"2026-11","supplier":"旧供应商","inventoryAccount":"1401","sku":"LEGACY-RM","name":"旧原料","warehouse":"主仓库","qty":1,"unitCostCents":10000,"netCents":10000,"taxCents":1300,"stockCostCents":10000,"grossCents":11300,"deductible":True,"vatRate":13,"voucherId":"legacy-po-voucher"}
    settlement={"id":"biz-settlement:AP_PAYMENT:LEGACY-AP","kind":"AP_PAYMENT","settlementNo":"LEGACY-AP","sourceDocId":"inventory-doc:LEGACY-PO","sourceDocNo":"LEGACY-PO","sourceDate":"2026-11-10","counterparty":"旧供应商","date":"2026-11-15","period":"2026-11","bank":"基本户","amountCents":5000,"voucherId":"legacy-ap-voucher"}
    production={"id":"production-order:LEGACY-MO","orderNo":"LEGACY-MO","date":"2026-11-18","period":"2026-11","materials":[{"sku":"LEGACY-RM","name":"旧原料","warehouse":"主仓库","qty":1,"costCents":10000}],"materialCostCents":10000,"finishedSku":"LEGACY-FG","finishedName":"旧成品","finishedWarehouse":"成品仓","finishedQty":1,"issueVoucherId":"legacy-mo-voucher","status":"ISSUE_PENDING"}
    payroll={"id":"payroll-batch:2026-11","period":"2026-11","grossCents":100000,"netCents":90000,"iitCents":3000,"employeeSocialCents":5000,"otherCents":2000,"employerSocialCents":8000,"deductionsCents":15000,"expenseCents":108000,"accrualVoucherId":"legacy-pay-voucher","rows":[{"code":"LEG-E01","name":"旧员工","dept":"行政部","gross":100000,"employeeSocial":5000,"iit":3000,"other":2000,"employerSocial":8000,"net":90000,"expenseAccount":"6602"}]}
    # Seed the old generic store exactly as V8/V9 did.
    batch = {
        "account_set_id": "default", "account_set_name": "默认账套",
        "stores": {
            "Workpapers": [opening,purchase,settlement,production,payroll],
            "Settings": [{"id": "finance-standard:operations:assets", "value": json.dumps([
                {"code": "FA-LEGACY", "name": "旧电脑", "type": "电子设备", "cost": 100000, "residual": 5, "years": 3,
                 "start": "2025-01-01", "dept": "行政部", "place": "办公室", "keeper": "", "qty": 1, "active": True}
            ], ensure_ascii=False)}]
        }
    }
    response = client.put("/api/storage/batch", json=batch)
    assert response.status_code == 200, response.text
    migrated = client.post("/api/business/migrate-from-local").json()
    assert migrated["counts"]["inventory_openings"] == 1
    assert migrated["counts"]["inventory_documents"] == 1
    assert migrated["counts"]["settlements"] == 1
    assert migrated["counts"]["production_orders"] == 1
    assert migrated["counts"]["payroll_batches"] == 1
    assert migrated["counts"]["assets"] == 1
    bootstrap = client.get("/api/business/bootstrap").json()
    migrated_ids={x["id"] for x in bootstrap["records"]}
    assert {"inventory-opening","inventory-doc:LEGACY-PO","biz-settlement:AP_PAYMENT:LEGACY-AP","production-order:LEGACY-MO","payroll-batch:2026-11"}.issubset(migrated_ids)
    assert bootstrap["assets"][0]["code"] == "FA-LEGACY"
    # Migration is idempotent.
    again = client.post("/api/business/migrate-from-local").json()
    assert all(value==0 for value in again["counts"].values())


def test_v10_business_document_and_voucher_are_one_transaction(client: TestClient):
    _v10_account(client, "1401", "原材料", "资产", "借")
    _v10_account(client, "2221", "应交税费", "负债", "贷")
    opening={"id":"inventory-opening","period":"2026-11","verified":True,"rows":[{"account":"1401","sku":"ATOMIC-RM","name":"铜材","warehouse":"主仓库","qty":1,"unitCostCents":10000,"stockCostCents":10000}],"totalCents":10000}
    assert client.post("/api/business/records",json={"records":[opening]}).status_code==200
    doc = {
        "id": "inventory-doc:ATOMIC-PO", "kind": "PURCHASE", "docNo": "ATOMIC-PO", "date": "2026-11-20", "period": "2026-11",
        "supplier": "原子供应商", "inventoryAccount": "1401", "sku": "ATOMIC-RM", "name": "铜材", "warehouse": "主仓库",
        "qty": 1, "unitCostCents": 10000, "netCents": 10000, "taxCents": 1300, "stockCostCents": 10000,
        "grossCents": 11300, "deductible": True, "vatRate": 13, "voucherId": "atomic-voucher-1"
    }
    voucher = {
        "legacy_id": "atomic-voucher-1", "voucher_no": "ATOMIC-V-001", "voucher_date": "2026-11-20", "summary": "采购入库",
        "status": "PENDING", "entries": [
            {"account_code": "1401", "debit_amount": "100.00"},
            {"account_code": "2221", "debit_amount": "13.00"},
            {"account_code": "2202", "credit_amount": "113.00"}
        ]
    }
    invalid_voucher = {**voucher, "entries": [
        {"account_code": "1401", "debit_amount": "100.00"},
        {"account_code": "2202", "credit_amount": "99.00"}
    ]}
    failed = client.post("/api/business/commit", json={"records": [doc], "vouchers": [invalid_voucher]})
    assert failed.status_code == 422
    assert not any(x["id"] == doc["id"] for x in client.get("/api/business/records").json()["items"])
    assert not any(x["legacy_id"] == voucher["legacy_id"] for x in client.get("/api/vouchers").json()["items"])

    committed = client.post("/api/business/commit", json={"records": [doc], "vouchers": [voucher],
        "insert_only_record_ids": [doc["id"]], "insert_only_voucher_ids": [voucher["legacy_id"]]})
    assert committed.status_code == 200, committed.text
    assert committed.json()["record_count"] == 1 and committed.json()["voucher_count"] == 1
    conn = database.get_connection()
    try:
        link = conn.execute("SELECT voucher_id FROM business_voucher_links WHERE account_set_id='default' AND source_legacy_id=?", (doc["id"],)).fetchone()
        assert link and link["voucher_id"] is not None
    finally:
        conn.close()
    duplicate = client.post("/api/business/commit", json={"records": [doc], "vouchers": [voucher],
        "insert_only_record_ids": [doc["id"]], "insert_only_voucher_ids": [voucher["legacy_id"]]})
    assert duplicate.status_code == 409


def test_v10_production_issue_to_completion_is_normalized_and_linked(client: TestClient):
    _v10_account(client, "1401", "原材料", "资产", "借")
    _v10_account(client, "1405", "库存商品", "资产", "借")
    _v10_account(client, "5001", "生产成本", "成本", "借")
    opening={"id":"inventory-opening","period":"2026-11","verified":True,"totalCents":10000,
      "rows":[{"account":"1401","sku":"RM-P1","name":"原料P1","warehouse":"主仓库","qty":10,"unitCostCents":1000,"stockCostCents":10000}]}
    assert client.post("/api/business/records",json={"records":[opening]}).status_code==200
    order_id="production-order:MO-V10-1"
    issue_doc={"id":"inventory-doc:PROD-ISSUE:MO-V10-1:RM-P1","kind":"PROD_ISSUE","docNo":"MO-V10-1","date":"2026-11-10","period":"2026-11","inventoryAccount":"1401","sku":"RM-P1","name":"原料P1","warehouse":"主仓库","qty":1,"costCents":1000,"productionOrderId":order_id,"voucherId":"v10-issue-1"}
    order={"id":order_id,"orderNo":"MO-V10-1","date":"2026-11-10","period":"2026-11","materials":[{"sku":"RM-P1","name":"原料P1","warehouse":"主仓库","qty":1,"costCents":1000}],"materialCostCents":1000,"finishedSku":"FG-P1","finishedName":"成品P1","finishedWarehouse":"成品仓","finishedQty":1,"issueVoucherId":"v10-issue-1","status":"ISSUE_PENDING"}
    issue_voucher={"legacy_id":"v10-issue-1","voucher_no":"V10-ISSUE-1","voucher_date":"2026-11-10","summary":"生产领料","status":"PENDING","entries":[{"account_code":"5001","debit_amount":"10.00"},{"account_code":"1401","credit_amount":"10.00"}]}
    created=client.post("/api/business/commit",json={"records":[order,issue_doc],"vouchers":[issue_voucher],"insert_only_record_ids":[order_id,issue_doc["id"]],"insert_only_voucher_ids":["v10-issue-1"]})
    assert created.status_code==200,created.text
    issue_id=created.json()["vouchers"][0]["id"]
    assert client.post(f"/api/vouchers/{issue_id}/approve").status_code==200
    assert client.post(f"/api/vouchers/{issue_id}/post").status_code==200

    receipt_id="inventory-doc:PROD-RECEIPT:MO-V10-1"
    receipt={"id":receipt_id,"kind":"PROD_RECEIPT","docNo":"MO-V10-1","date":"2026-11-30","period":"2026-11","sku":"FG-P1","name":"成品P1","warehouse":"成品仓","qty":1,"stockCostCents":1000,"materialCostCents":1000,"conversionAllocationCents":0,"completionCostCents":1000,"productionOrderId":order_id,"voucherId":"v10-complete-1"}
    updated={**order,"status":"COMPLETION_PENDING","completionVoucherId":"v10-complete-1","completionDocId":receipt_id,"completionDate":"2026-11-30","completionPeriod":"2026-11","conversionAllocationCents":0,"completionCostCents":1000}
    completion_voucher={"legacy_id":"v10-complete-1","voucher_no":"V10-COMPLETE-1","voucher_date":"2026-11-30","summary":"生产完工入库","status":"PENDING","entries":[{"account_code":"1405","debit_amount":"10.00"},{"account_code":"5001","credit_amount":"10.00"}]}
    completed=client.post("/api/business/commit",json={"records":[updated,receipt],"vouchers":[completion_voucher],"insert_only_record_ids":[receipt_id],"update_only_record_ids":[order_id],"insert_only_voucher_ids":["v10-complete-1"]})
    assert completed.status_code==200,completed.text
    complete_id=completed.json()["vouchers"][0]["id"]
    assert client.post(f"/api/vouchers/{complete_id}/approve").status_code==200
    assert client.post(f"/api/vouchers/{complete_id}/post").status_code==200
    balance=client.get("/api/business/inventory-balance",params={"period":"2026-11"}).json()
    by_sku={row["sku"]:row for row in balance["items"]}
    assert by_sku["RM-P1"]["quantity_milli"]==9000 and by_sku["RM-P1"]["amount_cents"]==9000
    assert by_sku["FG-P1"]["quantity_milli"]==1000 and by_sku["FG-P1"]["amount_cents"]==1000


def test_v11_bank_omission_tax_persistence_and_closing_gate(client: TestClient):
    saved = client.put('/api/v11/bank-matches', json=[{
        'legacy_id':'V11-BANK-TEST-01','period':'2026-11','bank_account':'基本户',
        'bank_amount':'88.00','book_amount':'0.00','status':'BANK_ONLY',
        'difference_type':'ENTERPRISE_OMISSION','raw':{'debit_account':'1002','credit_account':'1122'}
    }])
    assert saved.status_code == 200, saved.text
    rows = client.get('/api/v11/bank-matches', params={'period':'2026-11'}).json()['items']
    assert rows[0]['bank_amount'] == '88.00'
    draft = client.post(f"/api/v11/bank-matches/{rows[0]['id']}/voucher")
    assert draft.status_code == 201, draft.text
    assert draft.json()['status'] == 'DRAFT'
    assert client.post(f"/api/v11/bank-matches/{rows[0]['id']}/voucher").json()['already_created'] is True

    tax = client.post('/api/v11/tax/workpapers', json={
        'period':'2026-11','tax_type':'VAT','basis_amount':'1000.00','tax_amount':'60.00',
        'rate':'0.06','payload':{'source':'test'}
    })
    assert tax.status_code == 201, tax.text
    assert client.get('/api/v11/tax/workpapers', params={'period':'2026-11'}).json()['items'][0]['tax_amount'] == '60.00'
    archived = client.post('/api/v11/tax/archives', json={'period':'2026-11','tax_type':'VAT','paid_date':'2026-11-20','amount':'60.00','certificate_no':'TAX-001'})
    assert archived.status_code == 201, archived.text
    assert client.get('/api/v11/tax/archives', params={'period':'2026-11'}).json()['items'][0]['certificate_no'] == 'TAX-001'
    exported = client.post('/api/v11/exports/trial-balance', json={'headers':['科目','金额'],'rows':[['1002',88]]})
    assert exported.status_code == 200 and 'spreadsheetml' in exported.headers['content-type']
    assert exported.content[:2] == b'PK'
    # Unposted draft created above blocks close.
    check = client.get('/api/v11/closing/check', params={'period':'2026-11'}).json()
    assert check['passed'] is False
    assert check['details']['unposted_vouchers'] == 1
    assert client.post('/api/v11/closing/lock', json={'period':'2026-11'}).status_code == 409
