# Phase C-lite 端到端验证记录（黄金路径，无 LLM）

> 日期：2026-11-03
> 范围：M01→M13 主链路的真实进程级端到端验证
> 结论：**黄金路径打通**，附带发现并修复 5 个只在真实 PostgreSQL 下暴露的缺陷

## 1. 验证目标与边界

Phase C 原定义为「端到端黄金路径」，需要 LLM provider 凭据来驱动 AI 用例生成。
为在不引入外部依赖的前提下验证系统骨架，本轮执行 **Phase C-lite**：

| 维度 | 本轮覆盖 | 本轮不覆盖 |
|---|---|---|
| 组织/项目/环境/成员 | ✅ 全链路 | — |
| 用例 + DSL v2 版本 | ✅ 手写 DSL 提交 | ❌ AI 生成（需 LLM 凭据） |
| 审批 + 发布门禁 | ✅ 双人审批（owner 提交 / approver 批准） | — |
| 套件 + 计划 + Run 触发 | ✅ | — |
| 异步准入（outbox → Celery） | ✅ RUN_REQUESTED → PREPARING | — |
| SSE 实时事件流 | ✅ 一次性票据 + cookie 校验 | ❌ 断线重连 |
| 真实浏览器执行 | ❌ 无 Browser Gateway / Playwright Worker | 由 `simulate_executor.py` 接管模拟 |
| 证据采集（截图/trace/HAR） | ❌ 无浏览器即无证据 | — |
| 报告快照 + 四格式导出 | ✅ json / html / junit / pdf | — |

**关键边界认知**：本轮证明的是「API、数据库、消息队列、对象存储、报告渲染」
这条确定性骨架的正确性，**不证明**任何 AI 建议质量或真实浏览器执行结果。

## 2. 环境

本地容器 + 本地进程，全部真实组件，无 mock 中间件：

| 组件 | 地址 | 说明 |
|---|---|---|
| PostgreSQL 18 | `127.0.0.1:55432/testpilot` | 真实迁移库（001–018 已全量应用） |
| Redis | `127.0.0.1:16379` | Celery broker + auth 限流 |
| Mailpit | SMTP `11025` / API `18025` | 真实收信，用于取验证与邀请 token |
| 对象存储 | `127.0.0.1:19000` | 本地 S3 stub（Docker Hub 不可达，无法拉 MinIO） |
| API | `127.0.0.1:18000` | uvicorn，`/health/ready` 三探针全 ready |
| Celery worker + beat | 本机进程 | `--pool=solo` |

配置见 `.env.e2e`（已 gitignore）。两点约束值得记录：

1. `TESTPILOT_AUTH_ALLOWED_ORIGINS` 与 `TESTPILOT_EMAIL_VERIFY_URL` 必须是 **https**，
   否则 API 或 worker 拒绝启动。
2. Windows 有端口保留段，Redis 映射到 `56379` 时 bind 失败，改用 `16379`。

## 3. 黄金路径执行结果

驱动脚本 `.e2e-logs/drive.py`，逐步骤记录 PASS/FAIL/BLOCK 与原始 HTTP 状态。

首轮结果：**29 PASS / 2 BLOCK / 0 FAIL**（`.e2e-logs/golden-path.json`）。

两个 BLOCK 均为「报告要求 Run 处于终态，而当时 Run 仍是 QUEUED」——属于预期阻塞，
不是缺陷。后续 Celery worker 接管 Run 后报 `BROWSER_GATEWAY_NOT_CONFIGURED` 并转
`ERROR`，满足终态前置条件，报告与导出随之打通（见 §5）。

覆盖的 29 个 PASS 步骤包括：注册 → 邮件验证 → 登录 → 建组织/项目/环境 → 建用例 →
提交 DSL v2 版本 → 邀请第二用户 → 接受邀请 → 审批 → 发布 → 建套件 → 加用例 →
建计划 → 触发 Run → 读 Run → 申请 SSE 票据 → 读事件流 → 读步骤。

SSE 是其中最容易被误判的一环，记录其真实约束：

- 普通 Bearer 读 `/test-runs/{id}/events` 返回 **403**。
- 必须 `POST /sse-tickets`（带 `Origin` 头）换取一次性 cookie
  `testpilot_sse`（`secure` / `httponly` / `samesite=strict`），再用该 cookie 读流。
- 实测响应 `HTTP 200`、`content-type: text/event-stream; charset=utf-8`、首帧 `id: N`。

## 4. 修复的 5 个真实缺陷

这 5 个缺陷**全部无法被单元测试发现**，原因见 §6。它们只在「真实迁移库 + 真实进程」
的组合下才暴露。

| # | 缺陷 | 症状 | 根因 |
|---|---|---|---|
| 1 | `suite_cases.project_id` 未写入 | `POST /suites/{id}/cases` → 500，`CheckViolation: ck_suite_cases_project_required_v102` | 迁移 004 加了该列 + NOT NULL CHECK + 复合外键，ORM 模型没声明，插入语句里根本没有这一列 |
| 2 | `run_cases.project_id` 未写入 | `POST /test-runs` → 500，`CheckViolation: ck_run_cases_project_not_null_v103` | 同 #1，迁移 005 的要求 ORM 未跟进 |
| 3 | Celery worker 导入图缺模型 | 报告导出 → `NoReferencedTableError: report_exports.artifact_id -> test_artifacts` | worker 只导入少量模块，`quality/persistence.py` 未加载，`test_artifacts` 不在共享 `MetaData` 注册表里，配置无关 mapper 时炸 |
| 4 | `viewport` 传 None 写成 JSON `null` | 报告导出 → `CheckViolation: test_artifacts_viewport_check` | SQLAlchemy 默认把 Python `None` 序列化为 JSON 值 `null`，违反 `CHECK (viewport IS NULL OR jsonb_typeof(viewport)='object')` |
| 5 | `schedules.project_id` 未写入 | 建计划在真实库上报 CHECK 违规 | 与 #1/#2 同源，由漂移检测器扫出 |

修复要点：

- #1 / #2 / #5：模型补列 + 写入路径补值（`add_case()`、`create_run()`、`create_schedule()`）。
- #3：新增 `apps/api/app/orm_models.py` 集中注册全部 13 个含 `__tablename__` 的模块，
  并在 `main.py` 与 `cases/worker.py` 的导入阶段早期调用 `load_all_models()`。
- #4：`JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")`，
  作用于 `test_artifacts.viewport` 与 `generation_jobs.result` 两处。

## 5. 报告导出链路验证

9 次导出（3 轮 × json/html/junit），修复前 3 次 FAILED、修复后 6 次 SUCCEEDED。

```
status   | count          format | status    | count
---------+-------         --------+-----------+------
SUCCEEDED|     6          html   | FAILED    |     1
FAILED   |     3          html   | SUCCEEDED |     2
                         json   | SUCCEEDED |     2
                         json   | FAILED    |     1
                         junit  | FAILED    |     1
                         junit  | SUCCEEDED |     2
```

`test_artifacts` 落盘 6 条，`content_type` / `size_bytes` / `sha256` 全部正确，
`viewport` 为 SQL NULL（即 #4 的验证点）。

**字节级回读校验**（`.e2e-logs/verify_artifacts.py`）：从对象存储 GET 每个 report
artifact，比对其 SHA-256 与库中记录值 —— **6/6 完整一致**。

**四格式渲染校验**（`.e2e-logs/verify_formats.py`）：用真实导出的 JSON 快照作为输入，
重新渲染全部四种格式：

```
PASS  json     1012B  application/json
PASS  html     1315B  text/html; charset=utf-8
PASS  junit     383B  application/xml
PASS  pdf      3514B  application/pdf     (%PDF- 头 + %%EOF 尾完整，含 CJK 字体)
```

PDF 走 reportlab + `UnicodeCIDFont("STSong-Light")`，此前从未在真实快照上验证过。

## 6. 为什么单测看不到这些缺陷

这是本轮最重要的方法论结论：

> 后端单测全部用 `AuthBase.metadata.create_all()` 在 SQLite 上建表，
> **schema 来自 ORM 而非 `db/migrations`**。因此 ORM 与迁移的任何分歧，
> 单测在结构上都不可能发现 —— 建出来的表永远和 ORM 一致。

于是迁移里多出来的 `NOT NULL CHECK`、复合外键、`jsonb_typeof` 约束，
在单测里根本不存在；只有连上真实迁移库才会触发。

为此新增常设检测器 `apps/api/scripts/check_schema_drift.py`，检查三类漂移：

1. ORM 有表/列但迁移没有 → **FATAL**
2. 迁移要求 `NOT NULL` 或 `CHECK(col IS NOT NULL)`，但 ORM 不声明/不写入 → **FATAL**
3. 某模块声明了 `__tablename__` 却没注册进 `orm_models` → **FATAL**

用法：

```bash
TESTPILOT_DATABASE_URL=postgresql://... PYTHONPATH=. \
  python apps/api/scripts/check_schema_drift.py
```

当前结果：**0 fatal / 7 warning**。7 条 warning 均为历史遗留列
（`generation_jobs.requester_id`、`run_cases.browser`、`test_runs.source_repository`、
`test_case_versions.published_at`、`test_case_versions.supersedes_version_id`）
与 LangGraph 表（`agent_checkpoints`、`graph_threads`），无 ORM 映射，不阻塞。

## 7. 发现但本轮未修的契约不一致

以下问题已确认存在，改动会触及契约/前端，留待单独决策：

| # | 问题 | 影响 |
|---|---|---|
| 1 | `POST /test-runs` 运行时强制 `Idempotency-Key` 头，但 OpenAPI 标为可选 | 严格按契约实现的客户端会拿到 400 `INVALID_REQUEST` |
| 2 | worker 对 `TESTPILOT_EMAIL_VERIFY_URL` 强制 https，API 侧不校验 | 同一份配置 API 接受、worker 拒绝，故障点延迟到异步阶段 |
| 3 | `run_cases.browser`（迁移 002）与 `browser_name` 双列并存 | 语义重叠，需决定保留哪个 |
| 4 | `generation_jobs.requester_id`（迁移 002）与 `created_by` 并存 | 同上 |
| 5 | `reports.artifact_id` 是空悬字段 | ORM/迁移都声明了，但当前写入路径从不赋值；报告字节只存在于导出产出的 artifact。对 API 无功能影响 |

## 8. 与既有测试结论的差异

`docs/current-validation-status.md` 原记录「两个套件在 Windows 无法运行」
（`test_lifecycle_routes.py`、`test_m10_healing_routes.py`，此前在 Linux 容器里
12 passed / 19.03s 才跑通）。本轮在 Windows 上复测：**12 passed in 13.15s**。

结论修正：旧结论不再成立。但**因果未确认** —— 无法证明是 `orm_models` 注册修复
解决了它，也可能只是此前的进程/事件循环状态残留。因此记录为「当前环境下可运行」，
不再保留「仅 Linux 可运行」的说法。

含真实依赖的全量测试（PostgreSQL + Redis + Mailpit 均为真实实例）：

- **312 passed, 0 skipped**（163s）

旧基线 298 passed / 2 skipped 是排除 12 个套件用例、且跳过 2 个 PG 集成测试的结果。

## 9. 结论与建议下一步

Phase C-lite 达成：确定性骨架端到端打通，报告导出四格式全部可用且字节可校验。

**下一步优先级建议**（按风险/收益排序）：

1. **修 §7 的契约不一致 #1、#2** —— 低成本，且 #1 会直接打到接入方。
2. **Phase 6 真机部署验收** —— 配置已产出但未在真实主机跑 `nginx -t` / `pm2 ls`，
   SSE 重连与 SSRF 负向用例也未验。这是从「本地能跑」到「能交付」的关键一跳。
3. **真实 Browser Gateway + Playwright Worker** —— 补上证据采集与真实执行，
   此时才能验证「AI 只建议、判定由确定性执行器做」这一核心边界。
4. **并发与锁验证** —— 当前单 Run 串行验证，并发下的 lease/幂等/配额未压过。
5. **PostgreSQL 15 兼容性** —— 目前只在 18 上验证。
