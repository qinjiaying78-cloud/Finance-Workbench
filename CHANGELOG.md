# V7 - 规范化财务表多账套隔离

## 本次变更

- 为 `accounts`、`contacts`、`vouchers`、`voucher_entries`、`cashier_records`、`invoices`、`accounting_periods`、`contact_opening_balances` 增加 `account_set_id`。
- 使用复合唯一键/外键约束账套内的科目编码、凭证号、凭证分录、往来单位和出纳凭证关联。
- 允许不同账套使用相同科目编码及凭证号。
- 所有规范化核心 API 和 SQL 报表接受 `account_set_id` 查询参数，默认使用 `default`。
- 期间锁定、往来辅助期初、迁移映射均按账套隔离。
- 老数据库原规范化数据升级时归入 `default`，保留原记录 ID、凭证号、附件 URL、金额和余额；`app_records` 不删除。
- 历史科目迁移改为先插入再解析 parent_code，支持导入数据中的子科目排在父科目前面。

## 升级要求

1. 停止正在运行的 FastAPI 服务。
2. 备份 `backend/finance_workbench.db` 和整个 `backend/uploads/`。
3. 用本版本代码替换项目代码，不要覆盖已有数据库和 uploads。
4. 启动服务，自动执行兼容迁移。
5. 检查 `/api/health`、`/docs`，并核对默认账套凭证数、科目数、期初余额及附件路径。

## API 账套参数

规范化 API 使用 query 参数 `account_set_id`，例如：

```text
GET /api/vouchers?account_set_id=book-a
POST /api/vouchers?account_set_id=book-a
GET /api/accounts?account_set_id=book-a
GET /api/reports/trial-balance?account_set_id=book-a&period=2026-11
```

账套先通过 `PUT /api/storage/account-sets/{account_set_id}` 创建，或由 `/api/migrate-from-local` 自动创建。未知账套返回 404，不会回退到默认账套。

## 尚未完成

V7 只完成规范化关系表的多账套结构和 API 隔离。前端现有业务模块仍有部分通过 `app_records` 保存原始 JSON；下一阶段应逐个切换前端读写至规范化 API，再核对 SQL 报表与原前端报表。当前不应删除 `app_records` 或浏览器旧数据。


# V8 - 凭证模块规范化读写

## 本次变更

- 前端凭证实时读取切换为 `GET /api/vouchers`，新建/更新切换为 `POST` / `PUT /api/vouchers`。
- 新增审核接口 `POST /api/vouchers/{id}/approve` 与 `POST /api/vouchers/batch-approve`，审核时间保存到 `reviewed_at`；过账必须先审核。
- 新增 `POST /api/vouchers/batch-post`，批量记账在单一事务内全量校验，确保全部成功或全部回滚。
- 新增未审核凭证删除接口 `DELETE /api/vouchers/{id}`；已记账凭证禁止删除，前端改用红字冲销。
- 凭证主表及分录在单一 SQLite 事务中写入 `vouchers` / `voucher_entries`。
- 新增 `legacy_id` 保留旧前端凭证 ID，新增业务来源元数据及 `voucher_entries.aux_name`。
- V7 历史 app_records 快照用于补回旧凭证 ID、审核状态、附件路径和辅助核算标签。
- 前端 `VoucherRecords` 不再新增写入 `app_records`；其他模块仍保留原兼容层。

## 测试

- 自动化测试：14 passed。
- 覆盖规范化凭证创建、修改、审核、单张/批量过账、批量失败回滚、红字冲销、重复操作拦截、历史元数据回填、账套隔离及 app_records 不再接收新凭证记录。
- Python 编译及前端 JavaScript 语法检查通过。

## 尚未完成

- 出纳、发票、科目和往来单位前端实时读写尚未全部切换到规范化表。
- 前端七列余额表、总账及往来三栏账仍需进一步切换为 SQL 报表 API。
- `app_records` 仍保留其他业务模块及旧凭证历史快照，不应删除。

# V10 - 采购、销售、库存、生产、工资及固定资产规范化

## 本次变更

- 新增 `inventory_items`、`inventory_openings`、`inventory_opening_lines`、`business_documents`、`inventory_movements`、`business_settlements`、`production_orders`、`production_material_lines`、`payroll_batches`、`payroll_lines`、`payroll_drafts`、`payroll_draft_lines`、`fixed_assets`、`depreciation_runs`、`depreciation_lines`、`business_voucher_links`、`business_migration_map`。
- 所有新表均包含 `account_set_id`，通过复合唯一键/外键隔离账套；金额使用整数分，数量使用千分单位。
- 采购入库、销售出库、生产领料及完工入库使用 `business_documents` + `inventory_movements` 保存，不再把完整业务记录写入 `app_records`。
- 应收收款、应付付款核销写入 `business_settlements`，来源单据通过复合外键关联，禁止超额核销、错误类型核销或删除仍被结算引用的来源单据。
- 生产订单及原材料明细分别写入 `production_orders` / `production_material_lines`；工资批次及员工明细分别写入 `payroll_batches` / `payroll_lines`；工资草稿写入 `payroll_drafts` / `payroll_draft_lines`。
- 固定资产卡片和折旧记录分别写入 `fixed_assets`、`depreciation_runs`、`depreciation_lines`。存在折旧历史后，不允许修改资产原值、残值率、使用年限和启用日期。
- 新增 `POST /api/business/commit`，将业务单据和对应凭证主表/分录放入同一个 SQLite 事务；任何一方校验失败时全部回滚。
- 前端采购、销售、结算、生产及工资凭证流程切换为原子提交接口；凭证与业务来源通过 `business_voucher_links` 建立外键关联。
- 库存出库在后端独立检查已核实期初、待处理单据、日期顺序、可用数量及移动加权平均成本；不能仅依赖前端校验。
- 新增 `/api/business/inventory-ledger` 和 `/api/business/inventory-balance`，后者只汇总已过账凭证对应的库存移动。
- 固定资产、折旧批次和工资草稿的前端同步设置切换为规范化 API；完整账套备份会将这些数据重新打包，导入时经兼容迁移恢复。
- `Workpapers` 仍保留其他未迁移业务的兼容记录；V10 识别的采购、销售、库存、生产、结算及工资批次记录在正常前端写入时会从 `Workpapers` 拆分并写入关系表，不再写入通用 JSON 存储。

## 新增 API

- `POST /api/business/migrate-from-local`
- `GET /api/business/records`
- `POST /api/business/records`
- `DELETE /api/business/records`
- `POST /api/business/commit`
- `POST /api/business/link-voucher`
- `GET /api/business/bootstrap`
- `PUT /api/business/assets`
- `PUT/DELETE /api/business/depreciation/{period}`
- `PUT/DELETE /api/business/payroll-drafts/{period}`
- `GET /api/business/inventory-ledger`
- `GET /api/business/inventory-balance`
- `POST /api/business/reset`

## 历史数据迁移

- 启动时前端调用 `/api/business/migrate-from-local`，将旧 `app_records` 中的 `Workpapers` 业务记录及 `Settings` 中的固定资产、折旧和工资草稿拆解到关系表。
- 迁移使用 `business_migration_map` 幂等标记；已存在的规范化记录优先于旧 JSON 快照，不会被旧快照覆盖。
- 本地浏览器仍存在而 SQLite 尚无对应 ID 的旧记录才会补迁移；旧浏览器数据与 `app_records` 历史快照均保留。
- 账套完整备份导出会将规范化固定资产、折旧及工资草稿一并纳入 `prefs`；导入新账套时先保留兼容快照，再由初始化迁移到新账套的规范化表。

## 测试

- 自动化测试：22 passed。
- 覆盖采购入库、销售库存校验、移动加权平均成本、生产订单材料明细、工资批次及工资草稿、固定资产及折旧、历史 Workpapers/Settings 拆解迁移、重复迁移拦截、多账套隔离、会计期间锁定、业务单据与凭证原子事务及失败回滚。
- Python 编译检查、3 个前端内联脚本 Node 语法检查、FastAPI 实际启动、`/api/health`、`/api/business/bootstrap` 和 `/app` HTTP 路由测试通过。

## 尚未完成

- 采购订单/销售订单审批、仓库调拨、批次/序列号、退货和完整 BOM/工序成本仍未覆盖。
- 工资薪税仍需依据当地政策及实际核定数据；本地模块不替代正式税务申报。
- 固定资产处置、减值、组件化折旧及完整资产卡片审计还需扩展。
- 税务管理、银行对账、期末结账档案仍有部分数据保存在兼容 `app_records` / Settings 层。
- 本版本已完成 API/数据库和前端存储适配语法测试，仍需在用户电脑的真实浏览器环境做完整鼠标操作、历史账套迁移核对和备份恢复验收。

**升级要求：** 停止旧服务，备份 `backend/finance_workbench.db` 和 `backend/uploads/`，用 V10 代码替换项目文件但保留数据库与附件目录，然后启动服务自动执行加表迁移。不要删除 `app_records` 或浏览器历史数据。
- 新增 `POST /api/business/depreciation/{period}/commit`，将固定资产月度折旧明细和对应折旧凭证在同一事务中提交，并在 `depreciation_runs` / `business_voucher_links` 中保存真实凭证外键；删除未记账折旧凭证时自动解除关联，已记账/冲销折旧记录不可直接撤销。
- 生产领料和完工入库增加后端联动校验：领料必须匹配原材料移动加权平均成本；完工必须关联已记账的领料订单，且完工成本等于材料成本加人工/制造费用。
- 工资计提凭证记账后保护员工明细和工资金额；生产领料凭证记账后保护订单/材料核心字段；固定资产存在折旧历史后保护折旧基础参数。
- 业务单据与凭证通过 `/api/business/commit` 原子提交；固定资产折旧通过 `/api/business/depreciation/{period}/commit` 原子提交。
