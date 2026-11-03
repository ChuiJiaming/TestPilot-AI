# Current Validation Status (authoritative as of 2026-11-03)

> 这是项目当前**唯一权威**的验证数字快照。此前分散在
> `phase-1-4-unified-validation.md`、`TestPilot_AI_V10.3_验证记录.txt`、
> `phase-2-deferred-validation.md` 里的数字是 **10-30 的历史快照**，保留原样
> 作为审计证据，不再更新。数字以后续此文档为准。

## 最新实测结果（2026-11-03）

| 检查项 | 结果 | 说明 |
|---|---|---|
| 后端测试套件 | **298 passed, 2 skipped** | 含真实 PostgreSQL 集成测试；排除两个 Windows 无法运行的套件 |
| 后端测试（排除真实库集成） | 296 passed, 4 skipped | 初始基线 |
| 真实 PostgreSQL 集成测试 | **2 passed** | `tests/test_postgresql_phase1_integration.py`，已从长期 skip 恢复 |
| 前端 Vitest 单元测试 | **31 passed / 7 文件** | 比 10-30 快照（15 passed / 4 文件）多 16 个 |
| 前端 typecheck | 通过 | |
| 路由注册 | 基线 101/101 + 附加 27/27 | 全部已注册 |

## 迁移链状态（Phase B 已完成）

| 检查项 | 结果 |
|---|---|
| 干净库 001→018 全量应用 | **18/18 通过**（此前在 008 断裂，已修复） |
| 账本 SHA-256 校验 | **18/18 通过** |
| 005 只读预检 | 五项全 0 |
| ORM 与真实表结构漂移 | **零漂移**（`approvals.payload` 缺失已补） |
| B3 存量升级演练 | 通过：旧用户保持 active、旧 token 映射 `Legacy device` |
| B4 回滚演练 | 通过：pg_dump/pg_restore 后 57 表一致、数据完整还原 |

修复的迁移文件：`008_case_version_review.sql`、`009_generation_and_scheduling.sql`、
`010_run_browser_and_environment_pins.sql`（详见 `2026-11-03-migration-validation.md`）。

## 仍为未验证 / 阻塞项（不得视为通过）

- **两个 Windows 无法运行的路由套件**：`tests/test_lifecycle_routes.py`、
  `tests/test_m10_healing_routes.py`（asyncio `socketpair` 在 Windows 卡死，需 Linux runner）。
- **迁移 011 的邮件 Outbox 加密密钥路径**：应用时未配置 `TESTPILOT_EMAIL_OUTBOX_ENCRYPTION_KEYS`，运行期行为未验证。
- **PostgreSQL 15 兼容性**：仅在 18 上验证。
- **并发与锁**：未做。
- **端到端黄金路径（Phase C）**：未做，需 LLM provider 凭据。
- **真实浏览器 / mTLS / Worker 出口 ACL**：未验证（`worker-egress-acl.sh` 为 CANDIDATE，未应用）。
- **部署验收（Phase 6 已产出配置，未在真实主机跑 `nginx -t`）**。

## 关键认知（供后续接手人）

1. **单测不覆盖迁移**：所有后端单测用 `AuthBase.metadata.create_all()` 在 SQLite
   建表，schema 来自 ORM 而非 SQL 迁移。因此单测全绿**不等于**迁移与 ORM 一致。
   真实 PostgreSQL 集成测试 + 结构比对才是迁移的验收依据。
2. **回滚无 down 迁移**：回滚 = `pg_dump` 快照恢复，无 SQL 回滚路径。
3. **DSL 版本偏差**：项目选严格 DSL v2，V10.3 §44.2 要求 v3，对外不得宣称符合 v3。
