Finance Workbench（简账）本地后端项目
本项目在现有 Finance Workbench 前端旁边提供 FastAPI + SQLite 本地服务。V8 已将凭证模块的实时读写切换到规范化关系表 `vouchers` / `voucher_entries`；出纳、发票及其他工作底稿仍保留原始记录兼容层，按账套写入 `app_records`，等待后续逐模块切换。前端页面设计和业务模块布局保持不变。
交付 ZIP 不覆盖运行时数据库文件 `backend/finance_workbench.db`，首次启动会自动创建；如果你已有旧版数据库，将新代码合并到原项目并保留原数据库文件，启动时会执行兼容迁移。升级前请先备份数据库及附件目录。
目录结构
```text
finance_workbench_local_backend/
├── index.html
├── README.md
├── CHANGELOG.md
├── pytest.ini
└── backend/
    ├── __init__.py
    ├── main.py                 # FastAPI 入口、CORS、静态附件挂载
    ├── main_config.py          # 本地文件路径配置
    ├── database.py             # SQLite 连接、自动建表和兼容迁移
    ├── schemas.py              # Pydantic 请求模型
    ├── requirements.txt
    ├── requirements-dev.txt
    ├── finance_workbench.db    # 本地数据库（首次启动自动建表）
    ├── uploads/                # 附件目录，按 YYYYMM 分文件夹
    ├── routers/
    │   ├── uploads.py
    │   ├── vouchers.py
    │   ├── cashier.py
    │   ├── invoices.py
    │   ├── reports.py
    │   ├── reference.py
    │   ├── storage.py             # 前端账套原始记录持久化
    │   ├── migration.py           # 历史 JSON 规范化迁移
    │   └── common.py
    └── tests/
        └── test_api.py
```
1. 环境与启动
要求 Python 3.10+。无需安装独立 SQLite 服务。
Windows PowerShell
在项目根目录运行：
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r backend/requirements.txt
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```
如果 PowerShell 不允许激活脚本：
```powershell
.\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```
macOS / Linux
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```
启动后：
前端工作台：http://127.0.0.1:8000/app
健康检查：http://127.0.0.1:8000/api/health
Swagger 接口文档：http://127.0.0.1:8000/docs
OpenAPI JSON：http://127.0.0.1:8000/openapi.json
健康检查成功响应：
```json
{"status":"ok","db":"connected"}
```
2. 金额、日期和状态约定
API 金额使用字符串形式的元，例如 `"123.45"`；最多两位小数。
SQLite 金额字段使用整数分保存，避免二进制浮点误差。
API 返回金额也使用两位小数字符串。
日期使用 `YYYY-MM-DD`，会计期间使用 `YYYY-MM`。
凭证数据库状态：`DRAFT`、`PENDING`、`POSTED`、`VOIDED`、`REVERSED`（审核通过以 `reviewed_at` 标记，查询筛选可用 `APPROVED`）；审核完成后保留 `PENDING` 并写入 `reviewed_at`，列表筛选支持 `status=APPROVED`。
出纳状态：`PENDING`、`DONE`、`POSTED`。
报表只汇总 `POSTED` 与 `REVERSED` 凭证。`REVERSED` 表示原凭证仍保留在账上，并由一张已过账负数红字凭证抵销；不能把原凭证从报表中排除。
3. 文件上传
`POST /api/upload`
使用 `multipart/form-data`，字段名 `file`。允许 JPG/JPEG、PNG、PDF，单文件最大 15 MiB。服务会检查文件签名，按服务器本地年月保存到 `backend/uploads/YYYYMM/`，文件名使用 UUID。
响应示例：
```json
{
  "filename": "随机 UUID.png",
  "url": "/uploads/202611/随机 UUID.png",
  "size_bytes": 2048,
  "content_type": "image/png"
}
```
附件访问地址：`http://127.0.0.1:8000` + 返回的 `url`。
旧接口 `POST /api/uploads` 仍作为隐藏兼容别名保留。
4. 凭证管理
`GET /api/vouchers`
可选查询参数：
`month=2026-11`
`status=DRAFT|PENDING|POSTED|VOIDED|REVERSED`
`account_keyword=1002`（科目编码或名称关键字）
`limit=200`、`offset=0`
返回凭证主表、附件路径和分录明细；分录同时返回科目名称、往来单位名称。
`POST /api/vouchers`
请求示例：
```json
{
  "voucher_no": "记-202611-0001",
  "voucher_date": "2026-11-20",
  "summary": "收到客户货款",
  "status": "DRAFT",
  "attachment_urls": ["/uploads/202611/receipt.png"],
  "entries": [
    {
      "account_code": "1002",
      "contact_id": null,
      "summary": "银行存款增加",
      "debit_amount": "1000.00",
      "credit_amount": "0.00"
    },
    {
      "account_code": "1122",
      "contact_id": 1,
      "summary": "冲减客户应收",
      "debit_amount": "0.00",
      "credit_amount": "1000.00"
    }
  ]
}
```
后端在同一个 SQLite 事务内校验并写入主表和分录：
至少两行分录；
每行只能填写借方或贷方其中一方，普通凭证金额不得为负；
借贷合计必须相等且不为零；
科目必须存在且启用，往来单位必须存在；
附件必须为本服务 `/uploads/` 相对路径；
凭证日期所在会计期间不能已锁定。
`POST /api/vouchers/{id}/approve`
审核凭证。只允许未审核的 `DRAFT` / `PENDING` 凭证，后端再次校验分录完整性、借贷平衡、科目启用状态和会计期间。审核通过后数据库仍保存 `status='PENDING'`，同时写入 `reviewed_at`；API 查询时可用 `status=APPROVED` 筛选已审核待记账凭证。这样无需破坏旧数据库的状态约束。
`POST /api/vouchers/{id}/post` / `POST /api/vouchers/batch-post`
必须先审核（`reviewed_at` 非空），再执行借贷平衡、分录和期间检查，成功后置为 `POSTED`。重复过账会返回 409。关联该凭证的出纳记录同步改为 `POSTED`。批量接口请求 `{ "ids": [1, 2] }`，所有凭证先整体校验，再在同一事务中全部过账；任意一张失败则全部回滚。
`POST /api/vouchers/batch-approve`
批量审核接口，接收 `{ "ids": [1, 2] }`，同样使用单一事务，确保整批凭证全部审核或全部不审核。
`PUT /api/vouchers/{id}` / `DELETE /api/vouchers/{id}`
仅允许未审核的 `DRAFT` / `PENDING` 凭证修改或删除。更新主表和替换分录使用同一个 SQLite 事务。已审核或已记账凭证禁止直接修改或删除；已记账凭证应通过红字冲销更正。
`POST /api/vouchers/{id}/reverse`
只允许冲销尚未冲销的 `POSTED` 凭证。默认冲销日期为服务器当天，也可传入：
```json
{"voucher_date":"2026-11-20","summary":"原业务退回"}
```
接口在一个事务内生成负数红字凭证（状态直接为 `POSTED`），并把原凭证状态改为 `REVERSED`，同时记录 `reverse_of_id`、`reversed_by_id` 和时间。红字凭证日期所在期间必须未锁定。冲销是不可静默删除的账务动作。
5. 出纳管理
`GET /api/cashier`
可选参数：`start_date`、`end_date`、`status`、`biz_type`、`limit`、`offset`。
`POST /api/cashier`
```json
{
  "record_date": "2026-11-20",
  "biz_type": "RECEIPT",
  "amount": "1000.00",
  "account_name": "银行存款",
  "contact_name": "客户 A",
  "source_doc": "收款单 001",
  "receipt_img_url": "/uploads/202611/receipt.png"
}
```
新记录默认为 `PENDING`。
`PUT /api/cashier/{id}`
更新未过账记录；允许将 `PENDING` 改为 `DONE`。已关联凭证或 `POSTED` 的记录禁止直接修改。
兼容形式 `PUT /api/cashier?id={id}` 也保留。
`POST /api/cashier/{id}/to-voucher`
将出纳记录转成凭证草稿，并把回单图片 URL 写入凭证附件。
收款/付款示例：
```json
{
  "cash_account_code": "1002",
  "counterpart_account_code": "1122",
  "voucher_date": "2026-11-20"
}
```
收款：借记现金/银行科目，贷记对方科目；
付款：借记对方科目，贷记现金/银行科目；
转账：需要 `cash_account_code`（转出科目）和 `transfer_to_account_code`（转入科目），生成借记转入、贷记转出。
该接口生成 `DRAFT` 凭证并将出纳记录关联为 `DONE`；凭证正式过账后，出纳记录自动变为 `POSTED`。会计对方科目由调用方明确提供，后端不会猜测应收、应付或费用科目。
6. 发票管理
`GET /api/invoices`
可选参数：`start_date`、`end_date`、`invoice_no`、`buyer_name`、`status`、`limit`、`offset`。
`POST /api/invoices`
```json
{
  "invoice_code": "电子发票",
  "invoice_no": "INV-001",
  "invoice_date": "2026-11-20",
  "buyer_name": "客户 A",
  "buyer_tax_id": "纳税人识别号",
  "seller_name": "本公司",
  "amount": "1000.00",
  "tax_amount": "130.00",
  "payment_method": "银行转账",
  "status": "ISSUED",
  "pdf_or_img_url": "/uploads/202611/invoice.pdf",
  "items": [{"name":"服务费","quantity":1,"unit_price":"1000.00"}]
}
```
`items` 会以 JSON 数组形式保存，发票金额和税额按分存储。
7. 账表查询
`GET /api/reports/trial-balance?period=2026-11`
根据科目期初余额、该期间之前所有已过账凭证和本期已过账凭证实时汇总。支持 `include_zero=true` 显示零余额科目。
七列展示字段为 `account`（科目编码与名称组合）、`initial_debit`、`initial_credit`、`period_debit`、`period_credit`、`ending_debit`、`ending_credit`。每行同时提供 `account_code`、`account_name` 两个扁平别名，方便前端筛选和绑定。
红字冲销分录保留负数，因此本期借/贷发生额可能出现负值；期末余额按借贷净额计算。
`GET /api/reports/sub-ledger`
必填参数：`account_code`、`contact_id`。可选 `period=YYYY-MM`，或 `start_date`、`end_date`。
返回单位往来三栏账：日期、凭证号、摘要、借方、贷方、逐笔余额及余额方向。
往来期初余额注意： `accounts.initial_debit/initial_credit` 是科目总账期初余额，不能自动分配给单个客户/供应商。需要按单位核算期初余额时，先调用 `PUT /api/contact-opening-balances` 写入辅助期初余额，字段包括 `account_code`、`contact_id`、`as_of_date`、`initial_debit`、`initial_credit`。没有单位辅助期初余额时，明细账会从已过账的历史往来分录累计，不会擅自把总账期初余额分摊给某个往来单位。
8. 基础资料及会计期间接口
为便于初始化和调用核心接口，另外提供：
`GET /api/accounts`、`POST /api/accounts`
`GET /api/contacts`、`POST /api/contacts`
`GET /api/periods`
`PUT /api/periods/{YYYY-MM}`，请求 `{ "is_locked": true, "lock_reason": "月结完成" }` 或解锁
`PUT /api/contact-opening-balances`
锁定期间时，后端会拒绝存在草稿/待审核凭证或未过账出纳记录的锁定请求。锁定后新增凭证、凭证过账和红字冲销均会被拒绝。
9. 数据库迁移说明
首次启动自动建表。对于上一版数据库，启动时会检测旧版凭证表的状态 CHECK 和非负金额 CHECK，并在 SQLite 事务中重建 `vouchers`、`voucher_entries`，保留已有 ID、凭证主表和分录数据，同时增加红字冲销关联字段、`posted_at` 及会计期间和往来辅助期初表。
本次已在旧版结构的测试数据库中验证历史凭证、分录、科目、往来单位、出纳、发票、期间及辅助期初迁移保留，并检查外键完整性。V7 会在原兼容迁移后将规范化表复制到带 `account_set_id` 的复合键结构，旧数据归入 `default`。正式升级前仍必须关闭服务并复制备份 `backend/finance_workbench.db` 和 `backend/uploads/`。
10. 测试
安装开发测试依赖：
```bash
pip install -r backend/requirements-dev.txt
```
在项目根目录运行：
```bash
python -m pytest -q backend/tests
```
V8 交付验证结果：`python -m pytest -q` 为 14 passed；Python 编译检查通过；前端 2 个内联 JavaScript 块均通过 `node --check`。另通过本地 HTTP smoke test 验证 `/api/health`、`GET /app`、账套存储批量写入和读取。测试覆盖旧数据库迁移、借贷不平衡拦截、凭证过账、负数红字冲销、出纳转凭证、往来三栏账、科目余额表、发票保存查询、文件上传、期间锁定、账套存储隔离、批量事务回滚、盘点原图存储及前端 `/app` 路由。
11. 规范化财务表多账套隔离（V7）
规范化财务表现已增加 `account_set_id`：`accounts`、`contacts`、`vouchers`、`voucher_entries`、`cashier_records`、`invoices`、`accounting_periods`、`contact_opening_balances`。`normalized_migration_map` 继续以账套 ID、模块和来源 ID 做幂等映射。
科目编码唯一性调整为 `(account_set_id, code)`，不同账套可以使用相同科目编码。
凭证号唯一性调整为 `(account_set_id, voucher_no)`，不同账套可以使用相同凭证号。
分录通过复合外键绑定同账套的凭证、科目和往来单位，数据库层阻止跨账套引用。
出纳关联凭证也使用 `(account_set_id, linked_voucher_id)` 复合外键。
会计期间锁定和往来辅助期初均按账套分别保存。
发票编号保留普通索引而非强制唯一约束，避免历史数据中存在重复发票编号时升级失败。
兼容规则： 原来没有账套字段的规范化财务数据在升级时全部归入 `default`（默认账套），保留原主键 ID、凭证号、金额、附件 URL 和期初余额。`app_records` 原有各账套数据不变；后续调用 `/api/migrate-from-local` 可将指定账套的历史记录迁移到同一账套的规范化表。
所有规范化接口支持 `account_set_id` 查询参数，默认值为 `default`。例如：
`GET /api/vouchers?account_set_id=book-a`
`POST /api/vouchers?account_set_id=book-a`
`GET /api/accounts?account_set_id=book-a`
`GET /api/cashier?account_set_id=book-a`
`GET /api/invoices?account_set_id=book-a`
`GET /api/reports/trial-balance?account_set_id=book-a&period=2026-11`
`GET /api/reports/sub-ledger?account_set_id=book-a&account_code=1122&contact_id=1&period=2026-11`
账套需先通过 `PUT /api/storage/account-sets/{account_set_id}` 建立，或由迁移接口创建。未知账套不会静默回退到默认账套，而是返回 404。
当前阶段边界： 规范化财务表和 SQL 报表已经实现账套隔离；但 Finance Workbench 前端的每个业务模块还没有全部切换为直接读写规范化 API，前端原始记录兼容层 `app_records` 仍保留。不要删除 `app_records` 或浏览器旧数据，直到后续完成前端模块切换和逐笔核对。
12. 重要安全边界
服务默认只监听 `127.0.0.1`；不要直接改成 `0.0.0.0` 暴露到局域网或公网。
当前是本地单用户服务，尚未实现登录、用户权限、审核人身份、操作审计、加密备份和恢复机制。
CORS 为本地开发便利配置，允许任意来源但不允许凭据；不能替代身份认证。
`backend/finance_workbench.db` 和 `backend/uploads/` 都包含敏感财务数据，请妥善备份和保护。
当前前端持久化适配器已接入本地存储 API，但尚未实现用户登录、角色权限、审核人身份、并发写冲突解决和操作审计。

13. 前端与 SQLite 持久化接入
前端默认连接 `http://127.0.0.1:8000`。推荐先启动 FastAPI，再访问 `http://127.0.0.1:8000/app`。也可以直接打开 `index.html`，本地 CORS 配置允许 `file://` 的 `null` Origin。
服务连接成功后，顶部会显示“SQLite 已连接”。此时凭证数据直接写入规范化 SQLite 表 `vouchers` / `voucher_entries`。以下其他前端数据仍写入 SQLite 的 `app_records`，并按 `account_set_id` 隔离：
`CashierRecords`：出纳收付款流水；
`Invoices`：销项发票台账；
`Workpapers`：应收应付、采购销售、库存、工资、资产、税务、对账、结账等业务工作底稿；
`Settings`：科目辅助配置、报表设置、资产盘点记录等原有同步设置；
`AssetCountOriginals`：资产盘点原始照片。
第一次连接时，程序会读取当前浏览器 IndexedDB 和本地设置，将后端缺少的记录复制到 SQLite；不会删除原浏览器数据。若 SQLite 中已有同 ID 记录，以 SQLite 版本为准；只迁移后端尚不存在的本地记录。旧 IndexedDB 保留，可作为回退备份。
可通过以下接口检查账套存储：
`GET /api/storage/{store_name}?account_set_id=default`
`PUT /api/storage/{store_name}`
`PUT /api/storage/batch`（支持多表事务、批量插入校验、删除和整表替换）
`GET /api/storage/account-sets`
`PUT /api/storage/account-sets/{account_set_id}`
附件处理：凭证、出纳、发票和盘点照片的 JPG/PNG/PDF 可同步复制到 `backend/uploads/YYYYMM/`，并在记录中附加 `/uploads/...` 路径。为保持旧前端预览兼容，常规附件数据 URL 仍保留在 JSON 记录中，因此当前版本存在数据库内联副本与 uploads 文件副本；盘点原图则优先保存为 uploads 文件引用，不支持的图片格式会以 data URL 保存在 SQLite。
重要边界： V8 的凭证列表与制单/审核/过账流程直接读写 `/api/vouchers` 及规范化表；出纳、发票、科目设置及其他工作底稿仍通过 `/api/storage/*` 保存兼容记录，部分模块已有独立规范化 API，但前端尚未全部切换。前端账表当前基于规范化凭证读取结果在浏览器侧计算，尚未全部改用 SQL 报表 API。不要删除 `app_records` 或浏览器旧数据，直到所有业务模块切换并完成逐笔对账。
如果顶部显示“浏览器本地存储”，表示 FastAPI 未连接，当前写入仍落在原浏览器 IndexedDB/localStorage；启动服务后刷新页面会尝试迁移缺失记录。若显示“SQLite 同步异常”，先检查后端服务和磁盘空间，再刷新确认状态。
规范化财务表迁移（新增）
启动服务并通过 `/app` 打开前端后，初始化阶段会把浏览器中已有的凭证、出纳流水和发票记录提交到 `POST /api/migrate-from-local`，按来源记录 ID 幂等拆解到 `vouchers`、`voucher_entries`、`cashier_records`、`invoices`。原 `app_records` 与浏览器数据不会被删除，便于核对及回滚。
也可手动调用：
```json
POST /api/migrate-from-local
{
  "account_set_id": "default",
  "vouchers": [],
  "cashier_records": [],
  "invoices": [],
  "accounts": [],
  "contacts": []
}
```
迁移接口通过 `normalized_migration_map` 以 `(account_set_id,module,source_id)` 防止重复导入。凭证科目编码若不存在会创建待核对的占位科目；正式记账前应在科目表中核对并修正类别、方向及期初余额。迁移事务遇到无效金额、日期或借贷不平会整体回滚。
重要边界： V8 已将凭证模块实时写入切换到规范化表；出纳、发票、科目和其他业务模块仍待逐个切换。历史 `VoucherRecords` app_records 快照作为回滚/核对来源保留，但 V8 不再向该 store 写入新凭证。

14. V8：凭证模块规范化读写
V8 将前端制证、凭证列表、审核和记账流程切换到规范化 API：
`getAllFromStore('VoucherRecords')` 在 API 模式下直接查询 `GET /api/vouchers`，不再从 `app_records` 读取实时凭证。
新凭证通过 `POST /api/vouchers` 写入 `vouchers`，分录写入 `voucher_entries`；每张凭证使用单个 SQLite 事务保存主表和分录。
前端旧 ID 保存到 `vouchers.legacy_id`，与数据库自增 ID 分开，保持工作底稿中的 `voucherId` 等历史引用稳定。
`voucher_entries.aux_name` 保存非往来辅助标签（例如银行账户名）；应收/应付等往来科目同时解析 `contact_id`。
`POST /api/vouchers/{id}/approve` 记录审核时间；`POST /api/vouchers/{id}/post` 只允许审核后过账。
已记账凭证不能删除，前端“红字冲销”操作调用 `/reverse`，生成负数分录并保留原凭证。未审核凭证可经 `DELETE` 删除，删除记录仍写入工作底稿审计记录。
历史 V7 `VoucherRecords` app_records 快照会用于补回 `legacy_id`、业务来源字段、附件 URL、辅助核算标签及审核状态；原 `app_records` 数据不删除，但 V8 不再向其中写入新的凭证记录。
前端对凭证的读写已直接切换；其他模块（出纳、发票、科目设置、业务工作底稿）尚未全部完成规范化读写切换。前端账表当前从规范化凭证读取数据并在浏览器侧计算，SQL 报表 API 尚未全部替换前端账表的取数逻辑。
升级步骤： 先停止服务并备份 `backend/finance_workbench.db` 与 `backend/uploads/`，再替换代码并启动。数据库初始化会以加列方式增加 `legacy_id`、`reviewed_at`、业务来源字段和 `voucher_entries.aux_name`，不会删除原凭证、分录或 `app_records` 历史快照。

15. V9：核心科目、往来、出纳、发票和 SQL 报表规范化
V9 将以下前端模块的实时读写从通用 `app_records` 切换到关系型财务表：
科目与期初：`GET /api/accounts` 读取科目；期初编辑器通过 `PUT /api/accounts/opening-balances` 在单一事务中批量更新；`PUT /api/accounts/{code}` 支持科目属性与单科目期初维护。
往来单位：前端应收/应付辅助单位从 `GET /api/contacts` 加载；凭证辅助核算自动创建的客户/供应商直接写入 `contacts`。新增 `PUT/DELETE /api/contacts/{contact_id}`。
往来期初：`GET /api/contact-opening-balances` 读取单位辅助期初，`PUT /api/contact-opening-balances` 保存；应收/应付三栏明细由 SQL `/api/reports/sub-ledger` 计算。总账余额与已分配往来明细之间的差额显示为“未分配/未指定往来”，不将差额静默分摊给某个单位。
出纳：前端出纳列表直接读取 `GET /api/cashier`；登记、修改、删除分别使用 `POST/PUT/DELETE /api/cashier`。银行转账两条流水通过 `POST /api/cashier/batch` 原子保存；相关流水批量更新、删除分别使用 `PUT /api/cashier/batch/update` 与 `POST /api/cashier/batch/delete`，避免只保存/删除转账的一侧。凭证与出纳记录通过关联接口建立关系，凭证过账后关联出纳自动标记为 `POSTED`。
智能开票：发票列表和保存直接读写 `invoices`，支持 `GET/POST/PUT/DELETE /api/invoices`。原前端内部记录 ID 保存在 `legacy_id`，实际发票号码保存在 `invoice_no`，其他 UI 扩展字段放入 `extra_json`，附件使用 `/uploads/` 路径。
科目余额表：前端七列余额表调用 `/api/reports/trial-balance`，期初、本期发生和期末余额由 SQLite SQL 聚合计算；当前期间逐笔明细仍从规范化凭证 API 读取。
单位往来账：单位余额及三栏明细调用 `/api/reports/sub-ledger`，只汇总 `POSTED` / `REVERSED` 状态的规范化凭证分录。
V9 兼容迁移
V9 为 `cashier_records` 增加 `legacy_id`、`transfer_id`、`direction`、`extra_json`，为 `invoices` 增加 `legacy_id`、`source`、`extra_json`。启动时通过 `normalized_migration_map` 和保留的 `app_records` 快照回填 V8 历史记录 ID、转账方向、实际发票号码及原 UI 扩展字段。`GET /api/migrate-from-local/status` 提供一次性同步标记；完成回填后，后续启动不再上传旧 JSON 附件或用旧快照覆盖 V9 实时记录。迁移为加列和回填，不删除历史记录。
首次从浏览器迁移出纳、发票记录时，附件会先上传到 `backend/uploads/YYYYMM/`，然后再写入规范化表；已成功上传的 Base64 内容不会继续复制到 `extra_json`。
注意： V9 的规范化实时读写范围是科目、往来、凭证、出纳和发票。`Workpapers`、`Settings`、工资、库存、固定资产、税务、银行对账、结账档案等其他业务仍有部分保存在 `app_records`，不能删除该表或浏览器历史数据。
---
V10：采购、销售、库存、生产、工资和固定资产规范化
V10 在 V9 的基础上新增本地关系型业务子账，前端原有模块和界面不变。以下业务记录在 API 模式下不再写入通用 `app_records` JSON：
业务	规范化表
存货主数据	`inventory_items`
存货期初	`inventory_openings`、`inventory_opening_lines`
采购/销售/生产出入库单据	`business_documents`、`inventory_movements`
应收收款/应付付款核销	`business_settlements`
生产订单与材料明细	`production_orders`、`production_material_lines`
已生成工资批次	`payroll_batches`、`payroll_lines`
工资草稿	`payroll_drafts`、`payroll_draft_lines`
固定资产及折旧	`fixed_assets`、`depreciation_runs`、`depreciation_lines`
业务来源凭证关系	`business_voucher_links`
旧数据迁移标记	`business_migration_map`
所有表均按 `account_set_id` 隔离。金额以整数分存储，库存数量以千分单位存储。
业务与凭证原子提交
前端采购、销售、往来核销、生产及工资计提/发放流程使用：
```text
POST /api/business/commit
```
请求包含规范化业务 `records` 和待审核凭证 `vouchers`。后端在同一个 SQLite `BEGIN IMMEDIATE` 事务中校验并写入业务主表、业务明细、凭证主表和凭证分录。凭证借贷不平、科目不存在、会计期间锁定、库存校验失败或唯一键冲突时，整批回滚，不留下半张业务单据。
其他接口
```text
POST   /api/business/migrate-from-local
GET    /api/business/records?account_set_id=default
POST   /api/business/records
DELETE /api/business/records?account_set_id=default
POST   /api/business/link-voucher?account_set_id=default
GET    /api/business/bootstrap?account_set_id=default
PUT    /api/business/assets
PUT    /api/business/depreciation/2026-11
POST   /api/business/depreciation/2026-11/commit
DELETE /api/business/depreciation/2026-11?account_set_id=default
PUT    /api/business/payroll-drafts/2026-11
DELETE /api/business/payroll-drafts/2026-11?account_set_id=default
GET    /api/business/inventory-ledger?period=2026-11
GET    /api/business/inventory-balance?period=2026-11
POST   /api/business/reset?account_set_id=default
```
库存及成本控制
采购入库/销售出库/生产领料/完工入库写入 `business_documents`，同步生成 `inventory_movements`。
销售出库和生产领料由后端检查已核实期初、库存数量、未完成库存单据、业务日期顺序及移动加权平均成本；不能只依赖前端 JavaScript 校验。
`/api/business/inventory-balance` 从已核实期初及其后已过账凭证关联的库存移动实时汇总数量和金额。未关联到已过账凭证的库存业务不会计入正式库存余额。
业务单据与凭证通过 `business_voucher_links` 绑定；凭证创建后写入真实 `voucher_id` 外键。
历史数据迁移
首次连接时，前端会在原 V9 规范化迁移之后调用 `/api/business/migrate-from-local`，将 `app_records` 中旧 `Workpapers` 的库存期初、采购销售单据、结算、生产订单、工资批次，以及 `Settings` 中的固定资产、折旧和工资草稿拆解到 V10 关系表。迁移以 `business_migration_map` 保证幂等；若规范化表已有同一业务记录，则以 SQLite 版本为准，不使用旧 JSON 覆盖。
完整账套备份会包含规范化业务记录、固定资产、折旧和工资草稿。导入到新账套时，兼容快照会先写入新账套，随后在启动初始化阶段拆解到新账套的规范化表。
V10 升级和验证
关闭 FastAPI 服务。
备份 `backend/finance_workbench.db` 和整个 `backend/uploads/`。
使用 V10 项目文件替换旧代码，但不要覆盖数据库及附件目录。
安装依赖并启动：
```powershell
pip install -r backend/requirements.txt
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```
打开 `http://127.0.0.1:8000/app`，等待顶部显示 SQLite 已连接；检查 `/docs` 中的 `/api/business/*` 接口。
对旧账套核对库存期初、业务单据、工资批次、固定资产原值/累计折旧、凭证数量及借贷合计。保留 `app_records` 和浏览器历史数据，完成逐笔核对后再考虑清理。
测试边界： V10 自动化测试覆盖 22 个 API/数据库场景，前端内联 JavaScript 语法检查及本地 HTTP 启动检查通过；尚未在最终用户电脑上完成全页面真实鼠标操作回归，也未使用真实企业账套完成完整月结验收。采购审批、销售订单、完整 BOM/工序成本、资产处置和税务/银行对账等仍需后续开发。
固定资产折旧与凭证原子提交
固定资产月度折旧先通过 `PUT /api/business/depreciation/{period}` 保存结构化折旧明细；随后前端生成折旧凭证时调用：
```text
POST /api/business/depreciation/2026-11/commit
```
该接口在同一事务中复核折旧行、期间锁定、凭证借贷平衡和科目有效性，并写入 `vouchers`、`voucher_entries`、`depreciation_runs.voucher_id` 与 `business_voucher_links`。已记账/冲销的折旧凭证不能删除，相关折旧记录也不能直接撤销。
生产领料及完工入库同样由后端校验：领料数量不能超过可用库存，成本必须匹配移动加权平均；完工入库必须关联已记账的生产领料凭证，完工成本必须等于材料成本加人工/制造费用。已记账的工资计提和生产领料核心明细受到修改保护。
