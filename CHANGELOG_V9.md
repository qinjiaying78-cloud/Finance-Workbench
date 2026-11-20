# Finance Workbench V9 更新记录

## 本轮目标

将科目、期初余额、往来单位、出纳收付款、发票和核心报表从通用 `app_records` JSON 存储切换到规范化 SQLite 表。保留 V8 前端视觉设计与历史兼容数据，不删除 `app_records`。

## 已完成

- `accounts`：前端科目读取改为 `GET /api/accounts`；期初余额保存改为事务型 `PUT /api/accounts/opening-balances`，并提供科目更新接口。
- `contacts`：前端往来单位从 `GET /api/contacts` 加载；凭证辅助核算缺少往来单位时通过 `POST /api/contacts` 创建；新增往来单位更新、删除接口。
- `contact_opening_balances`：增加读取接口；演示账套初始化时将应收、应付单位期初余额写入关系表；单位往来明细通过 SQL 子账接口读取。
- `cashier_records`：前端出纳列表、新增、修改、删除全部切换到规范化 API；新增 `legacy_id`、`transfer_id`、`direction`、`extra_json` 保留原 UI 记录标识、转账方向和扩展字段；转账双行支持事务批量新增。
- `invoices`：前端发票列表、新增、修改、删除全部切换到规范化 API；新增 `legacy_id`、`source`、`extra_json` 保留原 UI 内部 ID、来源、印章及附件元数据。
- 附件：迁移时将旧出纳回单、发票附件和印章图片上传到本地 `uploads/YYYYMM/`；规范化业务扩展 JSON 去除已成功上传的 Base64 副本，保留 `/uploads/` URL。
- 历史兼容：对 V8 已迁入规范化表、但未回填旧业务 ID 的出纳和发票记录，通过 `normalized_migration_map` 与 `app_records` 旧快照回填，不重复导入、不删除旧快照；增加一次性 V9 同步标记，避免每次启动重复上传旧附件或让旧 JSON 覆盖 V9 新数据。
- 历史期初：将 V8 `Workpapers` 中保存的 `opening:` 快照一次性迁移到 `accounts`，并写入迁移标记；若没有历史快照，仅在确认数据库完全匹配旧版演示账套的已知换算错误时修复金额，避免覆盖用户调整过的真实余额。
- 报表：科目余额表通过 `/api/reports/trial-balance` SQL 汇总结果驱动前端七列报表；应收/应付单位余额及三栏明细通过 `/api/reports/sub-ledger` 实时查询。未分配辅助核算差额单独显示，避免往来明细合计与总账不一致。
- 期初金额：修复首次 V8 演示科目迁移时将前端元金额除以 100 的换算错误。仅在演示账套所有科目余额精确匹配旧版错误换算值时自动修复；用户已调整过的余额不会被覆盖。新迁移直接按元转换为分存储。

## 新增/调整接口

- `PUT /api/accounts/{code}`
- `PUT /api/accounts/opening-balances`（批量事务）
- `PUT /api/contacts/{contact_id}`
- `DELETE /api/contacts/{contact_id}`
- `GET /api/contact-opening-balances`
- `POST /api/cashier/batch`（转账双流水原子新增）
- `PUT /api/cashier/batch/update`（相关出纳流水原子更新）
- `POST /api/cashier/batch/delete`（相关出纳流水原子删除）
- `DELETE /api/cashier/{record_id}`
- `PUT /api/invoices/{invoice_id}`
- `DELETE /api/invoices/{invoice_id}`

已有凭证、出纳、发票、科目、往来和报表接口均继续按 `account_set_id` 隔离。

## 测试

V9 测试覆盖 V8 历史元数据回填、科目期初批量事务回滚、往来单位更新、出纳记录规范化读写、转账批量新增、发票规范化读写、SQL 七列余额表及单位往来三栏账。运行：

```bash
python -m pip install -r backend/requirements-dev.txt
python -m pytest -q backend/tests
```

前端 inline JavaScript 使用 `node --check` 检查。当前自动化测试不等于真实浏览器全流程 UI 验收；升级到正式账套前仍需在用户本机做业务回归和账务余额核对。

## 保留的边界

`Workpapers`、`Settings`、固定资产盘点原图等尚未全部完成规范化迁移的业务继续使用兼容存储。`app_records` 及浏览器旧数据仍保留作为迁移核对与回滚来源，不要手动删除。
