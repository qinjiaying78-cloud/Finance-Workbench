"""Normalized operating subledgers for inventory, procurement, sales, payroll and fixed assets."""
from __future__ import annotations
import sqlite3

BUSINESS_SCHEMA = r"""
CREATE TABLE IF NOT EXISTS inventory_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_set_id TEXT NOT NULL,
  sku TEXT NOT NULL,
  name TEXT NOT NULL,
  inventory_account_code TEXT NOT NULL CHECK(inventory_account_code IN ('1401','1405')),
  unit TEXT NOT NULL DEFAULT '件',
  default_warehouse TEXT NOT NULL DEFAULT '主仓库',
  is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
  UNIQUE(account_set_id, sku, inventory_account_code),
  UNIQUE(account_set_id,id),
  FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE,
  FOREIGN KEY(account_set_id,inventory_account_code) REFERENCES accounts(account_set_id,code) ON UPDATE CASCADE ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS inventory_openings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_set_id TEXT NOT NULL,
  legacy_id TEXT NOT NULL,
  period TEXT NOT NULL,
  verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),
  total_cents INTEGER NOT NULL DEFAULT 0,
  total_1401_cents INTEGER NOT NULL DEFAULT 0,
  total_1405_cents INTEGER NOT NULL DEFAULT 0,
  created_at TEXT,
  UNIQUE(account_set_id,legacy_id),
  UNIQUE(account_set_id,id),
  FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS inventory_opening_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_set_id TEXT NOT NULL,
  opening_id INTEGER NOT NULL,
  row_no INTEGER NOT NULL,
  inventory_account_code TEXT NOT NULL CHECK(inventory_account_code IN ('1401','1405')),
  sku TEXT NOT NULL,
  item_name TEXT NOT NULL,
  warehouse TEXT NOT NULL,
  quantity_milli INTEGER NOT NULL CHECK(quantity_milli > 0),
  unit_cost_cents INTEGER NOT NULL CHECK(unit_cost_cents >= 0),
  stock_cost_cents INTEGER NOT NULL CHECK(stock_cost_cents >= 0),
  UNIQUE(account_set_id,opening_id,row_no),
  FOREIGN KEY(account_set_id,opening_id) REFERENCES inventory_openings(account_set_id,id) ON DELETE CASCADE,
  FOREIGN KEY(account_set_id,sku,inventory_account_code) REFERENCES inventory_items(account_set_id,sku,inventory_account_code) ON UPDATE CASCADE ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS business_documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_set_id TEXT NOT NULL,
  legacy_id TEXT NOT NULL,
  doc_type TEXT NOT NULL CHECK(doc_type IN ('PURCHASE','SALE','PROD_ISSUE','PROD_RECEIPT')),
  doc_no TEXT NOT NULL,
  doc_date TEXT NOT NULL,
  period TEXT NOT NULL,
  partner_name TEXT,
  inventory_account_code TEXT NOT NULL CHECK(inventory_account_code IN ('1401','1405')),
  sku TEXT NOT NULL,
  item_name TEXT NOT NULL,
  warehouse TEXT NOT NULL,
  quantity_milli INTEGER NOT NULL CHECK(quantity_milli > 0),
  unit_price_cents INTEGER NOT NULL DEFAULT 0,
  unit_cost_cents INTEGER NOT NULL DEFAULT 0,
  net_cents INTEGER NOT NULL DEFAULT 0,
  tax_cents INTEGER NOT NULL DEFAULT 0,
  stock_cost_cents INTEGER NOT NULL DEFAULT 0,
  gross_cents INTEGER NOT NULL DEFAULT 0,
  cost_cents INTEGER NOT NULL DEFAULT 0,
  deductible INTEGER NOT NULL DEFAULT 0 CHECK(deductible IN (0,1)),
  vat_rate TEXT,
  invoice_legacy_id TEXT,
  production_order_legacy_id TEXT,
  material_cost_cents INTEGER NOT NULL DEFAULT 0,
  conversion_allocation_cents INTEGER NOT NULL DEFAULT 0,
  completion_cost_cents INTEGER NOT NULL DEFAULT 0,
  voucher_legacy_id TEXT,
  created_at TEXT,
  UNIQUE(account_set_id,legacy_id),
  UNIQUE(account_set_id,id),
  FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE,
  FOREIGN KEY(account_set_id,sku,inventory_account_code) REFERENCES inventory_items(account_set_id,sku,inventory_account_code) ON UPDATE CASCADE ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS inventory_movements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_set_id TEXT NOT NULL,
  document_id INTEGER NOT NULL,
  movement_date TEXT NOT NULL,
  movement_type TEXT NOT NULL,
  sku TEXT NOT NULL,
  inventory_account_code TEXT NOT NULL,
  warehouse TEXT NOT NULL,
  quantity_milli INTEGER NOT NULL,
  unit_cost_cents INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL,
  voucher_legacy_id TEXT,
  UNIQUE(account_set_id,document_id),
  FOREIGN KEY(account_set_id,document_id) REFERENCES business_documents(account_set_id,id) ON DELETE CASCADE,
  FOREIGN KEY(account_set_id,sku,inventory_account_code) REFERENCES inventory_items(account_set_id,sku,inventory_account_code) ON UPDATE CASCADE ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS business_settlements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_set_id TEXT NOT NULL,
  legacy_id TEXT NOT NULL,
  settlement_type TEXT NOT NULL CHECK(settlement_type IN ('AR_RECEIPT','AP_PAYMENT')),
  settlement_no TEXT NOT NULL,
  source_doc_legacy_id TEXT NOT NULL,
  source_doc_no TEXT NOT NULL,
  source_date TEXT NOT NULL,
  counterparty TEXT NOT NULL,
  settlement_date TEXT NOT NULL,
  period TEXT NOT NULL,
  bank TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  voucher_legacy_id TEXT,
  created_at TEXT,
  UNIQUE(account_set_id,legacy_id),
  UNIQUE(account_set_id,settlement_no),
  FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE,
  FOREIGN KEY(account_set_id,source_doc_legacy_id) REFERENCES business_documents(account_set_id,legacy_id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS production_orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_set_id TEXT NOT NULL,
  legacy_id TEXT NOT NULL,
  order_no TEXT NOT NULL,
  order_date TEXT NOT NULL,
  period TEXT NOT NULL,
  finished_sku TEXT NOT NULL,
  finished_name TEXT NOT NULL,
  finished_warehouse TEXT NOT NULL,
  finished_qty_milli INTEGER NOT NULL CHECK(finished_qty_milli > 0),
  material_cost_cents INTEGER NOT NULL DEFAULT 0,
  issue_voucher_legacy_id TEXT,
  status TEXT NOT NULL,
  completion_voucher_legacy_id TEXT,
  completion_doc_legacy_id TEXT,
  completion_date TEXT,
  completion_period TEXT,
  conversion_allocation_cents INTEGER NOT NULL DEFAULT 0,
  completion_cost_cents INTEGER NOT NULL DEFAULT 0,
  created_at TEXT,
  UNIQUE(account_set_id,legacy_id),
  UNIQUE(account_set_id,order_no),
  UNIQUE(account_set_id,id),
  FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS production_material_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_set_id TEXT NOT NULL,
  production_order_id INTEGER NOT NULL,
  row_no INTEGER NOT NULL,
  sku TEXT NOT NULL,
  item_name TEXT NOT NULL,
  warehouse TEXT NOT NULL,
  quantity_milli INTEGER NOT NULL CHECK(quantity_milli > 0),
  cost_cents INTEGER NOT NULL CHECK(cost_cents >= 0),
  UNIQUE(account_set_id,production_order_id,row_no),
  FOREIGN KEY(account_set_id,production_order_id) REFERENCES production_orders(account_set_id,id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS payroll_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_set_id TEXT NOT NULL,
  legacy_id TEXT NOT NULL,
  period TEXT NOT NULL,
  gross_cents INTEGER NOT NULL,
  net_cents INTEGER NOT NULL,
  iit_cents INTEGER NOT NULL,
  employee_social_cents INTEGER NOT NULL,
  other_deductions_cents INTEGER NOT NULL,
  employer_social_cents INTEGER NOT NULL,
  deductions_cents INTEGER NOT NULL,
  expense_cents INTEGER NOT NULL,
  accrual_voucher_legacy_id TEXT NOT NULL,
  payment_voucher_legacy_id TEXT,
  remittance_voucher_legacy_id TEXT,
  created_at TEXT,
  UNIQUE(account_set_id,legacy_id),
  UNIQUE(account_set_id,period),
  UNIQUE(account_set_id,id),
  FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS payroll_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_set_id TEXT NOT NULL,
  payroll_batch_id INTEGER NOT NULL,
  row_no INTEGER NOT NULL,
  employee_code TEXT NOT NULL,
  employee_name TEXT NOT NULL,
  department TEXT NOT NULL DEFAULT '',
  gross_cents INTEGER NOT NULL,
  employee_social_cents INTEGER NOT NULL,
  iit_cents INTEGER NOT NULL,
  other_deductions_cents INTEGER NOT NULL,
  employer_social_cents INTEGER NOT NULL,
  net_cents INTEGER NOT NULL,
  expense_account_code TEXT NOT NULL,
  UNIQUE(account_set_id,payroll_batch_id,row_no),
  UNIQUE(account_set_id,payroll_batch_id,employee_code),
  FOREIGN KEY(account_set_id,payroll_batch_id) REFERENCES payroll_batches(account_set_id,id) ON DELETE CASCADE,
  FOREIGN KEY(account_set_id,expense_account_code) REFERENCES accounts(account_set_id,code) ON UPDATE CASCADE ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS payroll_drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_set_id TEXT NOT NULL,
  period TEXT NOT NULL,
  created_at TEXT,
  updated_at TEXT,
  UNIQUE(account_set_id,period),
  UNIQUE(account_set_id,id),
  FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS payroll_draft_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_set_id TEXT NOT NULL,
  payroll_draft_id INTEGER NOT NULL,
  row_no INTEGER NOT NULL,
  employee_code TEXT NOT NULL,
  employee_name TEXT NOT NULL,
  department TEXT NOT NULL DEFAULT '',
  gross_cents INTEGER NOT NULL,
  employee_social_cents INTEGER NOT NULL,
  iit_cents INTEGER NOT NULL,
  other_deductions_cents INTEGER NOT NULL,
  employer_social_cents INTEGER NOT NULL,
  net_cents INTEGER NOT NULL,
  expense_account_code TEXT NOT NULL,
  UNIQUE(account_set_id,payroll_draft_id,row_no),
  UNIQUE(account_set_id,payroll_draft_id,employee_code),
  FOREIGN KEY(account_set_id,payroll_draft_id) REFERENCES payroll_drafts(account_set_id,id) ON DELETE CASCADE,
  FOREIGN KEY(account_set_id,expense_account_code) REFERENCES accounts(account_set_id,code) ON UPDATE CASCADE ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS fixed_assets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_set_id TEXT NOT NULL,
  asset_code TEXT NOT NULL,
  name TEXT NOT NULL,
  asset_type TEXT NOT NULL,
  cost_cents INTEGER NOT NULL CHECK(cost_cents >= 0),
  residual_percent TEXT NOT NULL,
  useful_life_years TEXT NOT NULL,
  start_date TEXT NOT NULL,
  department TEXT NOT NULL,
  location TEXT NOT NULL DEFAULT '',
  keeper TEXT NOT NULL DEFAULT '',
  quantity_milli INTEGER NOT NULL DEFAULT 1000 CHECK(quantity_milli > 0),
  is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
  UNIQUE(account_set_id,asset_code),
  UNIQUE(account_set_id,id),
  FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS depreciation_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_set_id TEXT NOT NULL,
  period TEXT NOT NULL,
  created_at TEXT,
  voucher_legacy_id TEXT,
  voucher_id INTEGER,
  UNIQUE(account_set_id,period),
  UNIQUE(account_set_id,id),
  FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE,
  FOREIGN KEY(account_set_id,voucher_id) REFERENCES vouchers(account_set_id,id) ON UPDATE CASCADE ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS depreciation_lines (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_set_id TEXT NOT NULL,
  run_id INTEGER NOT NULL,
  asset_code TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  cumulative_cents INTEGER NOT NULL,
  net_cents INTEGER NOT NULL,
  UNIQUE(account_set_id,run_id,asset_code),
  FOREIGN KEY(account_set_id,run_id) REFERENCES depreciation_runs(account_set_id,id) ON DELETE CASCADE,
  FOREIGN KEY(account_set_id,asset_code) REFERENCES fixed_assets(account_set_id,asset_code) ON UPDATE CASCADE ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS business_voucher_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  account_set_id TEXT NOT NULL,
  source_module TEXT NOT NULL,
  source_legacy_id TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'PRIMARY',
  voucher_legacy_id TEXT NOT NULL,
  voucher_id INTEGER,
  UNIQUE(account_set_id,source_module,source_legacy_id,role),
  FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE,
  FOREIGN KEY(account_set_id,voucher_id) REFERENCES vouchers(account_set_id,id) ON UPDATE CASCADE ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS business_migration_map (
  account_set_id TEXT NOT NULL,
  module TEXT NOT NULL,
  source_id TEXT NOT NULL,
  target_id INTEGER,
  imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  PRIMARY KEY(account_set_id,module,source_id),
  FOREIGN KEY(account_set_id) REFERENCES account_sets(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_business_documents_tenant_date ON business_documents(account_set_id,doc_date,doc_type);
CREATE UNIQUE INDEX IF NOT EXISTS uq_business_document_number ON business_documents(account_set_id,doc_no) WHERE doc_type IN ('PURCHASE','SALE');
CREATE INDEX IF NOT EXISTS idx_inventory_movements_tenant_sku_date ON inventory_movements(account_set_id,sku,movement_date);
CREATE INDEX IF NOT EXISTS idx_payroll_batches_tenant_period ON payroll_batches(account_set_id,period);
CREATE INDEX IF NOT EXISTS idx_fixed_assets_tenant_active ON fixed_assets(account_set_id,is_active,asset_code);
"""

def ensure_business_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(BUSINESS_SCHEMA)
