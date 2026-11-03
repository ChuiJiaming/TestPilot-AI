# 迁移链全量验证记录（Phase B / Gate B）

Date: 2026-11-03
Scope: 干净数据库从 001 到 018 的全量应用、账本校验、005 预检、ORM 与真实表结构比对、真实 PostgreSQL 集成测试。
Database: 容器化 PostgreSQL 18，`postgres:18` 镜像，未触碰本机 5432 上的任何既有库。
This record does not claim populated-upgrade, rollback, PostgreSQL 15, or concurrency acceptance.

## 结论先说

**迁移链 001→018 原本是断的**：在 008 第一步就失败。修复后可在干净库上一次全量应用，账本 18/18 校验通过，ORM 与真实表结构零漂移。

发现的问题不是「偶发笔误」，而是一类**系统性缺陷**：见下文根因。

## 一、发现的问题

### 1. 六处重复定义（008 / 009 / 010）

| 迁移 | 对象 | 冲突 |
| --- | --- | --- |
| 008 | `test_case_versions.review_status` | 003 已建（NOT NULL DEFAULT 'DRAFT'） |
| 008 | `test_case_versions.approved_by` | 002/003 已建 |
| 008 | `test_case_versions.approved_at` | 002/003 已建 |
| 008 | 索引 `ix_case_versions_review` | 003 已建 |
| 009 | 表 `generation_jobs` | 002 已建（`CREATE TABLE IF NOT EXISTS`），009 用裸 `CREATE TABLE` |
| 010 | `test_runs.environment_snapshot_id` | 003 已建 |

另有 010 的 `DROP CONSTRAINT run_cases_run_id_case_version_id_key` —— 该约束在 002 基线中**根本不存在**，裸 DROP 会直接报错。

### 2. 009 与 002 的 `generation_jobs` 是语义不同的两套定义（最危险）

| 维度 | 002（已冻结） | 009（阶段二作者意图） |
| --- | --- | --- |
| 申请人列 | `requester_id uuid NOT NULL` | `created_by uuid`（可空） |
| 成本/用量 | 无 | cost / input_tokens / output_tokens |
| 重试与租约 | 无 | attempts / automatic_retries / cancel_requested / lease_expires_at / attempt_token |
| result 类型 | 仅 object | object 或 array |

代码（`apps/api/app/cases/planning.py:117`）映射的是 `created_by`，且写入时只给 `created_by`；002 的 `requester_id NOT NULL` 会让每次插入直接违反非空约束，缺少的列则触发 `UndefinedColumn`。

**也就是说：用迁移建库，阶段二的生成功能从第一行写入起就是不可用的。**

简单加 `IF NOT EXISTS` 会让 009 静默跳过，表就永远缺那些列 —— 这不是修好，是掩盖。正确做法是把 009 的建表改写成对 002 的**增量对齐**（加列、回填、放宽 `requester_id` 非空、放宽 result 约束）。

### 3. `approvals.payload` 在 ORM 中存在、迁移中不存在

`apps/api/app/runs/persistence.py` 的审批模型声明了 `payload jsonb NOT NULL`，而 001–007 从未创建该列。真实 PostgreSQL 上任何审批写入都会抛 `UndefinedColumn`。

### 4. 唯一的真实数据库集成测试已腐化

`tests/test_postgresql_phase1_integration.py` 调用了 `SqlAlchemyVerificationRepository.issue()` 的旧签名（缺 `token_ciphertext` 参数）。因为默认不设 `TESTPILOT_TEST_DATABASE_URL`，该测试长期处于 skip 状态，签名变更无人察觉。

## 二、根因（比单个缺陷更重要）

**测试用 `AuthBase.metadata.create_all()` 在 SQLite 上建表，schema 来自 ORM 模型，而不是 SQL 迁移。**

后果：
- 296 个通过的测试**结构上不可能**发现迁移与 ORM 的漂移；
- 迁移是否正确，只有 `migrate --apply` + 真实 PostgreSQL 才能证明；
- 而唯一的真实库测试又被 skip 且已腐化。

这不是某个人的疏忽，是验证链上缺了一环。

## 三、已做的修复

| 文件 | 修改 |
| --- | --- |
| `db/migrations/008_case_version_review.sql` | 列与索引改 `IF NOT EXISTS`；补 `approvals.payload jsonb NOT NULL DEFAULT '{}'` |
| `db/migrations/009_generation_and_scheduling.sql` | 建表改为增量对齐（11 个新列、`created_by` 回填、`requester_id` 放宽非空、result 约束放宽、复合唯一键用 DO 块守卫、索引 `IF NOT EXISTS`）；其余三张表改 `IF NOT EXISTS` |
| `db/migrations/010_run_browser_and_environment_pins.sql` | 列改 `IF NOT EXISTS`；DROP 改 `IF EXISTS`；两个 ADD CONSTRAINT 用 DO 块守卫 |
| `tests/test_postgresql_phase1_integration.py` | 补 `token_ciphertext` 参数，恢复可运行 |

未改动 001–007（已冻结的既有历史），也未改动 011–018（验证中直接通过，无需修改）。

## 四、验证结果

| 检查 | 结果 |
| --- | --- |
| 干净库 001→018 全量应用 | **18/18 APPLIED** |
| `migrate --check` 账本与校验和 | **18/18 VERIFIED** |
| `005_preflight_and_validate.sql` 五项计数 | **全部为 0** |
| ORM 表 vs DB 表 | 54 vs 57，无缺表 |
| ORM 声明列 vs DB 列 | **零缺失**（`approvals.payload` 已补齐） |
| `tests/test_postgresql_phase1_integration.py` | **2 passed**（含并发 refresh 重放撤销） |
| 完整后端套件（含真实库集成） | **298 passed, 2 skipped** |
| 存量升级演练（B3） | **通过**（旧用户 active、token family → Legacy device） |
| 回滚演练（B4） | **通过**（57 表一致，存量数据完整还原） |

DB 中多出的 3 张表为预期外管理对象：`agent_checkpoints`、`graph_threads`（LangGraph 托管）、`schema_migrations`（迁移账本）。

## 四之补、存量升级演练（B3）与回滚演练（B4）

### B3 — 存量升级（007 → 018）

- 新建 `testpilot_upg`，手动应用 001–006（各含事务包装），插入存量数据：1 个 active 用户、1 个组织、1 个 org_admin 成员、1 个项目、1 条 refresh-token family。
- 应用 007，验证旧 token family 自动回填为 `Legacy device` session（回填逻辑正确）。
- 登记 001–007 至账本后，`migrate --apply` 从 008 升级到 018：**11 个迁移全部 APPLIED**。
- 升级后校验：旧用户仍 `active`；旧 token family 映射为 `Legacy device`、未撤销、token 仍有效；组织/项目关系完整。**通过**。

### B4 — 回滚演练（快照恢复）

- `pg_dump -Fc` 生成 `upg_after_018.dump`（193 KB）。
- `pg_restore --no-owner --no-privileges` 恢复到全新 `testpilot_restore` 库。
- 比对：恢复库 57 张表与原库完全一致，存量数据（用户/组织/session）完整还原。**通过**。

结论：本项目没有 down 迁移，回滚 = 快照恢复，该路径已验证可用。

## 五、未验证项（不得视为通过）

- **PostgreSQL 15 兼容性**：仅在 18 上验证。
- **并发与锁**：迁移脚本自带 advisory lock，但多写入方并发未做压力验证。
- **011 的邮件 Outbox 加密密钥**：应用时未配置 `TESTPILOT_EMAIL_OUTBOX_ENCRYPTION_KEYS`，该路径运行期行为未验证。
- **两个 Windows 无法运行的路由测试套件**：`test_lifecycle_routes.py`、`test_m10_healing_routes.py` 仍未在 Linux 下取得结果。

## 六、遗留冗余列（无害，记录在案）

迁移后的库中存在 ORM 未使用的 001–007 遗留列：`generation_jobs.requester_id`、`run_cases.browser`、`run_cases.project_id`、`schedules.project_id`、`suite_cases.project_id`、`test_case_versions.published_at`、`test_case_versions.supersedes_version_id`、`test_runs.source_repository`。

其中 `run_cases.browser` 与阶段二新增的 `run_cases.browser_name` 是**同一语义的两列**（002 遗留 vs 阶段二代码实际读取）。ORM 只读 `browser_name`，故不阻断运行，但这是一个需要产品决策的双列局面，不能留到上线后再想起来。
