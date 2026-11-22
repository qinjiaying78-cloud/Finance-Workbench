-- Finance Workbench. First migration for an empty Supabase project. Run as postgres.
begin;
create schema if not exists fw_private;
revoke all on schema fw_private from public, anon, authenticated;
grant usage on schema fw_private to authenticated;

create table public.books (
  id uuid primary key default gen_random_uuid(), name text not null,
  taxpayer_type text not null check (taxpayer_type in ('general','small_scale')),
  timezone text not null default 'Asia/Shanghai', currency text not null default 'CNY' check (currency = 'CNY')
);
create table public.book_members (
  book_id uuid references books, user_id uuid references auth.users,
  role text not null check (role in ('director','accountant','cashier','reporter')),
  active boolean not null default true, primary key (book_id,user_id)
);
create table public.periods (
  book_id uuid references books, month date check (extract(day from month)=1),
  status text not null default 'open' check (status in ('open','review','locked')),
  version bigint not null default 1, locked_by uuid references auth.users, locked_at timestamptz,
  primary key(book_id,month), check (status <> 'locked' or (locked_by is not null and locked_at is not null))
);
create table public.dimensions (
  book_id uuid references books, kind text check (kind in ('customer','supplier','department','project','bank_account')),
  id uuid default gen_random_uuid(), code text not null, name text not null, active boolean not null default true,
  primary key(book_id,kind,id), unique(book_id,kind,code)
);
create table public.accounts (
  book_id uuid references books, code text,
  parent_code text generated always as (case when length(code)>4 then left(code,length(code)-2) end) stored,
  name text not null, normal_side char(1) not null check (normal_side in ('D','C')),
  active boolean not null default true, allow_posting boolean not null default true,
  tax_tag text check(tax_tag in ('vat_input','vat_output','vat_output_reduction','vat_input_transfer_out','vat_paid','vat_transfer_due','vat_transfer_overpaid','vat_unpaid','vat_prepaid','vat_unverified','vat_deferred_output')),
  primary key(book_id,code), foreign key(book_id,parent_code) references accounts(book_id,code),
  unique(book_id,tax_tag), check (code ~ '^[0-9]{4}([0-9]{2}){0,3}$')
);
create table public.account_dimensions (
  book_id uuid, account_code text, kind text check (kind in ('customer','supplier','department','project','bank_account')),
  required boolean not null default false, primary key(book_id,account_code,kind),
  foreign key(book_id,account_code) references accounts(book_id,code)
);
create table public.vouchers (
  book_id uuid, id uuid default gen_random_uuid(), month date not null, voucher_date date not null,
  voucher_no text, memo text not null, status text not null default 'draft'
    check(status in ('draft','submitted','approved','posted','rejected','voided')),
  created_by uuid not null references auth.users, reviewed_by uuid references auth.users,
  posted_at timestamptz, version bigint not null default 1, reversal_of uuid, request_key uuid not null,
  primary key(book_id,id), foreign key(book_id,month) references periods(book_id,month),
  foreign key(book_id,reversal_of) references vouchers(book_id,id),
  unique(book_id,month,voucher_no), unique(book_id,request_key),
  check(voucher_date>=month and voucher_date<month+interval '1 month'),
  check(reversal_of is null or reversal_of<>id),
  check(status<>'posted' or (reviewed_by is not null and posted_at is not null and voucher_no is not null)),
  check(reviewed_by is null or reviewed_by<>created_by)
);
create unique index one_posted_reversal on public.vouchers(book_id,reversal_of) where status='posted' and reversal_of is not null;
create table public.voucher_lines (
  book_id uuid, voucher_id uuid, line_no integer check(line_no>0), account_code text not null,
  debit_cents bigint not null default 0, credit_cents bigint not null default 0, summary text not null,
  primary key(book_id,voucher_id,line_no),
  foreign key(book_id,voucher_id) references vouchers(book_id,id),
  foreign key(book_id,account_code) references accounts(book_id,code),
  check ((debit_cents>0 and credit_cents=0) or (credit_cents>0 and debit_cents=0))
);
create table public.voucher_line_dimensions (
  book_id uuid, voucher_id uuid, line_no integer, kind text, dimension_id uuid not null,
  primary key(book_id,voucher_id,line_no,kind),
  foreign key(book_id,voucher_id,line_no) references voucher_lines(book_id,voucher_id,line_no),
  foreign key(book_id,kind,dimension_id) references dimensions(book_id,kind,id)
);
-- One migration opening snapshot per book, versioned outside routine entry. No monthly duplicate opening inputs.
create table public.opening_snapshots (
  book_id uuid primary key, month date not null, source_hash text not null,
  status text not null default 'draft' check(status in ('draft','confirmed')),
  unique(book_id,month), foreign key(book_id,month) references periods(book_id,month)
);
create table public.opening_entries (
  book_id uuid, id uuid default gen_random_uuid(), month date not null, account_code text not null,
  signed_cents bigint not null check(signed_cents<>0), primary key(book_id,id),
  foreign key(book_id,month) references opening_snapshots(book_id,month),
  foreign key(book_id,account_code) references accounts(book_id,code)
);
create table public.opening_entry_dimensions (
  book_id uuid, entry_id uuid, kind text, dimension_id uuid not null, primary key(book_id,entry_id,kind),
  foreign key(book_id,entry_id) references opening_entries(book_id,id),
  foreign key(book_id,kind,dimension_id) references dimensions(book_id,kind,id)
);

create table public.sync_connections (
  book_id uuid references books, id uuid default gen_random_uuid(), provider text not null check(provider in ('feishu','google_sheets')),
  module text not null check(module in ('warehouse','expense','asset')),
  source_ref text not null, table_ref text not null, secret_ref text not null,
  mapping jsonb not null check(jsonb_typeof(mapping)='object'), mapping_version integer not null default 1,
  active boolean not null default true, scheduled boolean not null default false,
  primary key(book_id,id), unique(book_id,provider,source_ref,table_ref)
);
create table public.sync_batches (
  id uuid primary key default gen_random_uuid(), request_key text not null unique,
  status text not null default 'queued' check(status in ('queued','running','succeeded','partial','failed')),
  target_connections jsonb not null check(jsonb_typeof(target_connections)='array'),
  created_at timestamptz not null default now(), finished_at timestamptz
);
create table public.sync_runs (
  book_id uuid, id uuid default gen_random_uuid(), connection_id uuid not null, request_key text not null,
  initiated_by uuid references auth.users, trigger_type text not null check(trigger_type in ('manual','schedule')),
  batch_id uuid references sync_batches,
  status text not null default 'queued' check(status in ('queued','running','succeeded','partial','failed')),
  mapping_version integer not null, stats jsonb not null default '{}',
  started_at timestamptz, finished_at timestamptz, lease_until timestamptz, lease_token uuid,
  primary key(book_id,id), foreign key(book_id,connection_id) references sync_connections(book_id,id),
  unique(book_id,connection_id,request_key), unique(book_id,connection_id,id),
  check ((trigger_type='manual' and initiated_by is not null and batch_id is null) or
         (trigger_type='schedule' and initiated_by is null and batch_id is not null))
);
create unique index one_active_sync_per_connection on sync_runs(book_id,connection_id) where status in ('queued','running');
create table public.external_rows (
  book_id uuid, connection_id uuid, external_id text, accepted_payload jsonb not null,
  accepted_hash text not null check(accepted_hash ~ '^[a-f0-9]{64}$'),
  hash_version integer not null default 1, mapping_version integer not null,
  first_seen_at timestamptz not null default now(),
  primary key(book_id,connection_id,external_id), foreign key(book_id,connection_id) references sync_connections(book_id,id)
);
-- Internal scan staging. No access for anon or ordinary users; clear after the retention window.
create table public.sync_observations (
  book_id uuid, connection_id uuid, run_id uuid, external_id text,
  payload_hash text, payload jsonb, occurrences integer not null default 1 check(occurrences>0),
  primary key(book_id,connection_id,run_id,external_id),
  foreign key(book_id,connection_id,run_id) references sync_runs(book_id,connection_id,id)
);
-- Membership scopes apply to the reporter's own submissions only.
create table public.reporter_scopes (
  book_id uuid, user_id uuid, module text check(module in ('warehouse','expense','asset')),
  primary key(book_id,user_id,module), foreign key(book_id,user_id) references book_members(book_id,user_id)
);
create table public.ledger_items (
  book_id uuid, id uuid default gen_random_uuid(), month date not null,
  module text not null check(module in ('warehouse','expense','asset','cash')),
  kind text not null check(kind in ('purchase_order','receipt','invoice','reimbursement','asset_entry','cash_entry')),
  doc_no text not null, doc_line integer not null default 1 check(doc_line>0),
  amount_cents bigint not null check(amount_cents>=0), tax_cents bigint not null default 0 check(tax_cents>=0),
  quantity numeric(20,6) check(quantity>=0), item_code text, match_key text,
  tax_rate_bps integer check(tax_rate_bps between 0 and 10000),
  payment_kind text check(payment_kind in ('advance','shipment')),
  invoice_state text not null default 'unknown' check(invoice_state in ('unknown','missing','partial','complete','not_required')),
  needs_invoice_followup boolean not null default false,
  project_id uuid, project_kind text generated always as ('project'::text) stored,
  supplier_id uuid, supplier_kind text generated always as ('supplier'::text) stored,
  submitted_by uuid references auth.users, voucher_id uuid,
  source_connection_id uuid, source_row_id text,
  corrected_from uuid, reason text,
  status text not null default 'draft' check(status in ('draft','pending_reconcile','submitted','approved','rejected','posted','voided')),
  is_locked boolean not null default false, version bigint not null default 1,
  primary key(book_id,id), unique(book_id,module,kind,doc_no,doc_line), unique(book_id,source_connection_id,source_row_id),
  foreign key(book_id,month) references periods(book_id,month), foreign key(book_id,voucher_id) references vouchers(book_id,id),
  foreign key(book_id,project_kind,project_id) references dimensions(book_id,kind,id),
  foreign key(book_id,supplier_kind,supplier_id) references dimensions(book_id,kind,id),
  foreign key(book_id,source_connection_id,source_row_id) references external_rows(book_id,connection_id,external_id) match simple,
  foreign key(book_id,corrected_from) references ledger_items(book_id,id),
  check ((source_connection_id is null)=(source_row_id is null)),
  check (source_row_id is null or is_locked), check (status<>'posted' or is_locked)
);
-- Cashier follow-ups are independent events to preserve locked source rows.
create table public.invoice_followups (
  book_id uuid, id uuid default gen_random_uuid(), ledger_id uuid not null,
  state text not null check(state in ('pending','reminded','received','closed')),
  note text, created_by uuid not null references auth.users, created_at timestamptz not null default now(),
  primary key(book_id,id), foreign key(book_id,ledger_id) references ledger_items(book_id,id)
);
-- A correspondence is one purchase order line, receipt line and invoice line allocation.
create table public.match_allocations (
  book_id uuid references books, id uuid default gen_random_uuid(), po_id uuid not null, receipt_id uuid not null, invoice_id uuid not null,
  quantity numeric(20,6) not null check(quantity>0), net_cents bigint not null check(net_cents>=0),
  primary key(book_id,id), unique(book_id,po_id,receipt_id,invoice_id),
  foreign key(book_id,po_id) references ledger_items(book_id,id), foreign key(book_id,receipt_id) references ledger_items(book_id,id),
  foreign key(book_id,invoice_id) references ledger_items(book_id,id)
);
create table public.reconcile_alerts (
  book_id uuid, id uuid default gen_random_uuid(), run_id uuid not null, connection_id uuid not null,
  alert_type text not null check(alert_type in ('changed','missing','duplicate_id','invalid_row','field_diff','three_way_diff','source_incomplete')),
  fingerprint text not null, external_id text, ledger_id uuid, evidence jsonb not null default '{}',
  status text not null default 'open' check(status in ('open','investigating','resolved','dismissed')),
  first_seen timestamptz not null default now(), last_seen timestamptz not null default now(),
  primary key(book_id,id), unique(book_id,connection_id,fingerprint),
  foreign key(book_id,connection_id,run_id) references sync_runs(book_id,connection_id,id),
  foreign key(book_id,ledger_id) references ledger_items(book_id,id)
);

create table public.workflow_cases (
  book_id uuid references books, id uuid default gen_random_uuid(), category text not null,
  voucher_id uuid, ledger_id uuid, alert_id uuid, initiated_by uuid references auth.users,
  initiated_at timestamptz not null default now(), version bigint not null default 1,
  state text not null default 'pending' check(state in ('pending','approved','rejected','resolved','cancelled')),
  primary key(book_id,id), check(num_nonnulls(voucher_id,ledger_id,alert_id)=1),
  foreign key(book_id,voucher_id) references vouchers(book_id,id), foreign key(book_id,ledger_id) references ledger_items(book_id,id),
  foreign key(book_id,alert_id) references reconcile_alerts(book_id,id)
);
create table public.workflow_tasks (
  book_id uuid, id uuid default gen_random_uuid(), case_id uuid not null, assignee_id uuid not null,
  kind text not null check(kind in ('action','notice')),
  state text not null default 'open' check(state in ('open','done','cancelled')),
  created_at timestamptz not null default now(), read_at timestamptz, acted_at timestamptz,
  primary key(book_id,id), foreign key(book_id,case_id) references workflow_cases(book_id,id),
  foreign key(book_id,assignee_id) references book_members(book_id,user_id)
);
create index tasks_inbox on workflow_tasks(book_id,assignee_id,state,created_at desc);
create index posted_month on vouchers(book_id,month,status);
create index ledger_owner on ledger_items(book_id,submitted_by,module);
create table public.user_preferences (
  book_id uuid, user_id uuid, shortcuts text[] not null default '{}',
  version bigint not null default 1, updated_at timestamptz not null default now(),
  primary key(book_id,user_id), foreign key(book_id,user_id) references book_members(book_id,user_id),
  check(cardinality(shortcuts)<=30)
);
create table public.vat_closings (
  book_id uuid, month date, version integer check(version>0),
  output_cents bigint not null, eligible_input_net_cents bigint not null,
  previous_credit_cents bigint not null check(previous_credit_cents>=0),
  transfer_due_cents bigint not null check(transfer_due_cents>=0), closing_credit_cents bigint not null check(closing_credit_cents>=0),
  basis jsonb not null, source_hash text not null, voucher_id uuid, request_key uuid not null,
  status text not null check(status in ('draft','submitted','posted','confirmed_no_voucher','superseded','cancelled')),
  primary key(book_id,month,version), unique(book_id,request_key), unique(book_id,voucher_id),
  foreign key(book_id,month) references periods(book_id,month), foreign key(book_id,voucher_id) references vouchers(book_id,id),
  check(status not in ('submitted','posted') or voucher_id is not null),
  check(status<>'confirmed_no_voucher' or (transfer_due_cents=0 and voucher_id is null))
);
create unique index one_live_vat_close on vat_closings(book_id,month) where status not in ('superseded','cancelled');

create table public.audit_logs (
  book_id uuid not null references books,
  id bigint generated always as identity primary key, module text not null, record_key jsonb not null,
  operation text not null, actor_user_id uuid references auth.users,
  actor_type text not null check(actor_type in ('user','service','maintenance')),
  acted_at timestamptz not null default clock_timestamp(), request_id text,
  before_data jsonb, after_data jsonb
);
create index audit_book_timeline on audit_logs(book_id,acted_at desc,id desc);

create function fw_private.book_role(target uuid) returns text language sql stable security definer
set search_path = '' as $$
  select m.role from public.book_members m where m.book_id=target and m.user_id=auth.uid() and m.active
$$;
revoke all on function fw_private.book_role(uuid) from public, anon, authenticated;
grant execute on function fw_private.book_role(uuid) to authenticated;

do $$ declare t text; begin
  foreach t in array array['books','book_members','periods','dimensions','accounts','account_dimensions','vouchers','voucher_lines',
    'voucher_line_dimensions','opening_snapshots','opening_entries','opening_entry_dimensions','sync_connections','sync_runs','external_rows','reporter_scopes',
    'ledger_items','invoice_followups','match_allocations','reconcile_alerts','workflow_cases','workflow_tasks','user_preferences','vat_closings','audit_logs'] loop
    execute format('alter table public.%I enable row level security',t);
    execute format('revoke all on public.%I from public, anon, authenticated',t);
    execute format('revoke insert,update,delete,truncate on public.%I from service_role',t);
    execute format('grant select on public.%I to authenticated',t);
  end loop;
  foreach t in array array['periods','dimensions','accounts','account_dimensions','vouchers','voucher_lines','voucher_line_dimensions',
    'opening_snapshots','opening_entries','opening_entry_dimensions','sync_runs','external_rows','match_allocations','reconcile_alerts','vat_closings','audit_logs'] loop
    execute format('create policy finance_reads on public.%I for select to authenticated using (fw_private.book_role(book_id) in (''director'',''accountant''))',t);
  end loop;
end $$;
alter table public.sync_observations enable row level security;
alter table public.sync_batches enable row level security;
revoke all on public.sync_observations, public.sync_batches from public, anon, authenticated;
revoke insert,update,delete,truncate on public.sync_observations, public.sync_batches from service_role;
create policy book_reads on books for select to authenticated using(fw_private.book_role(id) is not null);
create policy member_reads on book_members for select to authenticated using(fw_private.book_role(book_id)='director' or (user_id=auth.uid() and active));
create policy scope_reads on reporter_scopes for select to authenticated using(fw_private.book_role(book_id)='director' or (user_id=auth.uid() and fw_private.book_role(book_id)='reporter'));
create policy connection_reads on sync_connections for select to authenticated using(fw_private.book_role(book_id)='director');
create policy ledger_reads on ledger_items for select to authenticated using(
  fw_private.book_role(book_id) in ('director','accountant')
  or (fw_private.book_role(book_id)='cashier' and (module='cash' or kind in ('purchase_order','receipt','invoice')))
  or (fw_private.book_role(book_id)='reporter' and submitted_by=auth.uid() and exists(
      select 1 from public.reporter_scopes s where s.book_id=ledger_items.book_id and s.user_id=auth.uid() and s.module=ledger_items.module))
);
create policy followup_reads on invoice_followups for select to authenticated using(
  fw_private.book_role(book_id) in ('director','accountant') or
  (fw_private.book_role(book_id)='cashier' and exists(select 1 from public.ledger_items l
   where l.book_id=invoice_followups.book_id and l.id=invoice_followups.ledger_id))
);
create policy task_reads on workflow_tasks for select to authenticated using(assignee_id=auth.uid() and fw_private.book_role(book_id) is not null);
create policy case_reads on workflow_cases for select to authenticated using(fw_private.book_role(book_id) is not null and (
  initiated_by=auth.uid() or exists(select 1 from public.workflow_tasks t where t.book_id=workflow_cases.book_id and t.case_id=workflow_cases.id and t.assignee_id=auth.uid())
));
create policy preference_reads on user_preferences for select to authenticated using(user_id=auth.uid() and fw_private.book_role(book_id) is not null);
-- Writes use narrowly granted RPCs in the implementation phase. No broad authenticated table write grants.

create function fw_private.append_audit() returns trigger language plpgsql security definer set search_path = '' as $$
declare old_json jsonb; new_json jsonb; row_json jsonb; actor uuid := auth.uid();
begin
  if tg_op<>'INSERT' then old_json:=to_jsonb(old); end if;
  if tg_op<>'DELETE' then new_json:=to_jsonb(new); end if;
  row_json:=coalesce(new_json,old_json);
  insert into public.audit_logs(book_id,module,record_key,operation,actor_user_id,actor_type,request_id,before_data,after_data)
  values(coalesce((row_json->>'book_id')::uuid,(row_json->>'id')::uuid),tg_table_name,
    (select jsonb_object_agg(key,value) from jsonb_each(row_json)
     where key=any(array['book_id','id','user_id','code','account_code','kind','module','entry_id','month','voucher_id','line_no','connection_id','external_id','version'])),
    tg_op,actor,case when actor is not null then 'user' when auth.role()='service_role' then 'service' else 'maintenance' end,
    nullif(current_setting('fw.request_id',true),''),old_json,new_json);
  return null;
end $$;
revoke all on function fw_private.append_audit() from public, anon, authenticated;

do $$ declare t text; begin
  foreach t in array array['books','book_members','periods','dimensions','accounts','account_dimensions','vouchers','voucher_lines',
    'voucher_line_dimensions','opening_snapshots','opening_entries','opening_entry_dimensions','sync_connections','sync_runs','external_rows','reporter_scopes',
    'ledger_items','invoice_followups','match_allocations','reconcile_alerts','workflow_cases','workflow_tasks','user_preferences','vat_closings'] loop
    execute format('create trigger audit_changes after insert or update or delete on public.%I for each row execute function fw_private.append_audit()',t);
  end loop;
end $$;
create function fw_private.protect_audit() returns trigger language plpgsql set search_path='' as $$
begin raise exception 'Audit logs are append-only'; end $$;
create trigger protect_audit before update or delete or truncate on public.audit_logs
for each statement execute function fw_private.protect_audit();
revoke insert,update,delete,truncate on public.audit_logs from service_role;

-- Format preserves the imported evidence even when a finance correction creates a new ledger version.
create function fw_private.protect_source_baseline() returns trigger language plpgsql set search_path='' as $$
begin raise exception 'Accepted external baseline is immutable'; end $$;
create trigger protect_source_baseline before update or delete or truncate on public.external_rows
for each statement execute function fw_private.protect_source_baseline();

-- The sync worker can read connection metadata. Sensitive business writes will be through worker RPCs.
grant select on public.sync_connections to service_role;

commit;
