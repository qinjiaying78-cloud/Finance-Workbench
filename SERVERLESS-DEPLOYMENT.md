# 财务工作台云端部署

此目录已包含 Supabase 建表迁移，以及 GitHub Pages 手动发布和 GitHub Actions 定时同步配置。现有网页沿用本机 FastAPI/SQLite 和浏览器数据模式。

| 文件 | 用途 |
| --- | --- |
| [Supabase 迁移](supabase/migrations/20261122000000_finance_workbench.sql) | 新项目初始化表、RLS、审计和禁止自审约束 |
| [Pages 工作流](.github/workflows/finance-pages.yml) | 仅发布根目录的 index.html，默认分支手动触发 |
| [定时同步工作流](.github/workflows/finance-sync.yml) | 北京时间 02:17 调用已部署的同步服务，默认关闭 |
| [定时同步调用脚本](scripts/sync-nightly.mjs) | 调用服务端、按同一请求号重试并读取结果 |
| [接口与状态流转设计](docs/serverless/接口与状态流转设计.html) | SQL、审批、飞书和将来适配器的实施范围 |

## Supabase 数据库

先准备一个空的 Supabase 测试项目。安装 Supabase CLI 并登录，在本项目根目录运行以下命令。先确认连接对象和迁移预览，再执行实际迁移。

```powershell
supabase init
supabase login
supabase link YOUR_PROJECT_REF
supabase db push --dry-run
supabase db push
```

也可以在空项目的 SQL Editor 一次执行完整迁移文件。如果已通过 SQL Editor 执行，将来开始用 CLI 管理同一个项目时，应核对数据库状态后登记已应用的迁移，避免同一迁移再执行一次。如果此前安装过旧版财务工作台 SQL，需要使用增量迁移。当前本地 SQLite 账套数据尚未导入云端。

## GitHub Pages

当前目录是下载解压的项目，没有保留 Git 关联。将部署文件加入实际 Git 仓库后，在仓库的 **Settings → Pages → Source** 选择 **GitHub Actions**，再从默认分支手动运行 **Publish Finance Workbench static page**。发布包只含 index.html。现阶段发布的是静态工作台，浏览器数据仍保存在本机；跨用户协作需要完成 Supabase Auth 前端接入及写入接口。

提交前检查 Git 的暂存区。SQLite、账套导出、账簿备份和实际业务附件应留在本地，仓库只提交要发布的代码和经过确认的公开素材。此项目目录里的 `演示资料` 和 `升级前备份_*` 是本地交付内容，建议只暂存此次列出的部署文件。

## 飞书定时同步

先实现并部署文档约定的 Supabase Edge Function，定时入口为 `/functions/v1/sync-cron`。调用脚本使用 `x-fw-cron-secret` 和幂等 `request_key`，重复请求同一个批次。服务须返回 `run_id`、`status`，终态还需返回 `stats.read`、`stats.new`、`stats.success`、`stats.anomalies`。这些字段描述批次汇总，连接级别的详细统计由同步服务另行保留。

将 `SUPABASE_URL` 放在 GitHub Actions 的仓库变量，将与 Edge Function 中一致的 `FW_CRON_SECRET` 放在 GitHub Actions Secrets。飞书凭据保存在服务端。手动验证服务可联通后才设置仓库变量 `FW_SYNC_ENABLED=true`，此后可从默认分支手动运行工作流及定时触发。当前接入包含定时调用端，飞书 Edge Function 和前端同步按钮尚待实现。

官方资料：[Supabase 数据库迁移](https://supabase.com/docs/guides/deployment/database-migrations) · [GitHub Pages 工作流](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)
