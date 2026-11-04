# Current Validation Status (authoritative as of 2026-11-04)

> 这是项目当前**唯一权威**的验证数字快照。此前分散在
> `phase-1-4-unified-validation.md`、`TestPilot_AI_V10.3_验证记录.txt`、
> `phase-2-deferred-validation.md` 里的数字是 **10-30 的历史快照**，保留原样
> 作为审计证据，不再更新。数字以后续此文档为准。

## 最新实测结果（2026-11-03，Phase C-lite 之后）

| 检查项 | 结果 | 说明 |
|---|---|---|
| 后端测试套件（全依赖真实） | **312 passed, 0 skipped** | PostgreSQL + Redis + Mailpit 均为真实实例，163s |
| 后端测试（仅真实 PostgreSQL） | 310 passed, 2 skipped | 缺 live Redis/SMTP 环境变量时 |
| 后端测试（全部外依赖跳过） | 308 passed, 4 skipped | 仅本地默认环境 |
| 真实 PostgreSQL 集成测试 | **2 passed** | `tests/test_postgresql_phase1_integration.py` |
| 此前「Windows 无法运行」的两个套件 | **12 passed in 13.15s** | 旧结论「仅 Linux 可运行」已失效，见下 |
| 前端 Vitest 单元测试 | **31 passed / 7 文件** | 比 10-30 快照（15 passed / 4 文件）多 16 个 |
| 前端 typecheck | 通过 | |
| 路由注册 | 基线 101/101 + 附加 27/27 | 全部已注册 |

全依赖真实的那一档是当前**唯一完整绿**的跑法：

```bash
TESTPILOT_TEST_DATABASE_URL=postgresql://... \
TESTPILOT_TEST_REDIS_URL=redis://... \
TESTPILOT_TEST_SMTP_HOST=... TESTPILOT_TEST_SMTP_PORT=... \
TESTPILOT_TEST_MAILPIT_API_URL=... \
PYTHONPATH=. python -m pytest tests -q -rs
```

## 迁移链状态（Phase B 已完成）

| 检查项 | 结果 |
|---|---|
| 干净库 001→018 全量应用 | **18/18 通过**（此前在 008 断裂，已修复） |
| 账本 SHA-256 校验 | **18/18 通过** |
| 005 只读预检 | 五项全 0 |
| ORM 与真实表结构漂移 | **0 fatal / 7 warning**（`check_schema_drift.py`；warning 均为历史遗留列与 LangGraph 表） |
| B3 存量升级演练 | 通过：旧用户保持 active、旧 token 映射 `Legacy device` |
| B4 回滚演练 | 通过：pg_dump/pg_restore 后 57 表一致、数据完整还原 |

漂移检测已成常设脚本：`apps/api/scripts/check_schema_drift.py`。它存在的原因是
单测建表走 ORM、不走 SQL 迁移，因此**迁移里多出来的约束单测在结构上发现不了**。
本脚本已扫出 3 个此类缺陷（`suite_cases.project_id`、`run_cases.project_id`、
`schedules.project_id`）。

修复的迁移文件：`008_case_version_review.sql`、`009_generation_and_scheduling.sql`、
`010_run_browser_and_environment_pins.sql`（详见 `2026-11-03-migration-validation.md`）。

## Phase C-lite 端到端（已完成，无 LLM）

| 检查项 | 结果 |
|---|---|
| 黄金路径步骤 | 29 PASS / 2 BLOCK（2 个 BLOCK 是「Run 未终态不能出报告」的预期阻塞，现已随 Run 转 ERROR 解除） |
| Run 异步准入 | outbox → Celery beat → `RUN_REQUESTED` → `PREPARING` 打通 |
| SSE 事件流 | 一次性票据 + cookie 校验，`text/event-stream` 首帧正常 |
| 报告导出 | 9 次导出：修前 3 FAILED、修后 6 SUCCEEDED |
| 证据字节回读 | **6/6 artifact SHA-256 与库中记录一致** |
| 四种报告格式 | json / html / junit / pdf **4/4 渲染通过** |

详见 `2026-11-03-phase-c-lite-end-to-end.md`。本轮修复了 5 个只在真实 PostgreSQL
下暴露的缺陷（三处 `project_id` 漏写、worker 模型注册缺失、`viewport` 写成 JSON
`null`）。

## 真实浏览器链路（2026-11-04，首次验证通过）

驱动：`.e2e-logs/browser_chain.py`，靶场 `https://example.com`（公网，SSRF 策略禁止本机靶场）。

| 检查项 | 结果 |
|---|---|
| Worker mTLS 注册 / 15s 心跳 / Gateway 就绪汇聚 | 通过（`ready=true, capacity=2`） |
| 无客户端证书访问 Gateway | 拒绝（`alert certificate required`） |
| 环境健康检查（API→网关→Worker→目标站） | `HEALTHY`（`http=206`） |
| 真实浏览器 PASS 用例 | Run `61429b21` **PASSED**（11.3s） |
| 真实浏览器 FAIL 用例（错误断言） | Run `b936931f` **FAILED**（9.1s） |
| 每 Run 步骤 | 3 条（1 action + 2 assertion），状态与判定一致 |
| 截图证据 + 字节回读 | 38229 B，`sha256=8010e303df0f7d74…` 与库中记录一致，1280×720，redacted |
| 驱动汇总 | **43 PASS / 0 FAIL** |

本轮修掉两个只在真实依赖下暴露的缺陷：httpx 0.28 的 mTLS 证书不再被 request 级
`cert=` 接受（客户端证书静默不发），以及 Node 20+ Happy Eyeballs 以 `{all:true}`
调用自定义 lookup 导致健康探针恒定 `ERR_INVALID_IP_ADDRESS`。
详见 `2026-11-04-browser-chain-validation.md`。

## 仍为未验证 / 阻塞项（不得视为通过）

- **真实 AI 生成链路（Phase C 完整版）**：未做，需 LLM provider 凭据。
- **Worker 出口 ACL**：未应用（`worker-egress-acl.sh` 仍为 CANDIDATE）。
- **多 Worker 负载分配与会话亲和**：未验（本轮只有 1 个 Worker）。
- **证据采集范围**：真实浏览器已出截图（字节可回读）；**trace / HAR 只存在于
  `artifact_type` 枚举，没有任何生产写入路径**，不得宣称已采集。
- **邀请邮件链路**：未接入通知管道，Mailpit 收不到邀请邮件，token 只随创建响应返回。
- **部署验收（Phase 6 已产出配置，未在真实主机跑 `nginx -t` / `pm2 ls`）**。
- **SSE 断线重连**：未验（仅验了首次建流）。
- **迁移 011 的邮件 Outbox 加密密钥路径**：应用时未配置 `TESTPILOT_EMAIL_OUTBOX_ENCRYPTION_KEYS`，运行期行为未验证。
- **PostgreSQL 15 兼容性**：仅在 18 上验证。
- **并发与锁**：未做（单 Run 串行）。

## 已知契约不一致（已确认，未修）

| # | 问题 | 影响 |
|---|---|---|
| 1 | `POST /test-runs` 运行时强制 `Idempotency-Key`，OpenAPI 标可选 | 严格按契约实现的客户端会 400 |
| 2 | worker 强制 verify URL 为 https，API 侧不校验 | 同一份配置 API 接受、worker 拒绝 |
| 3 | `run_cases.browser`（002）与 `browser_name` 双列并存 | 语义重叠，待决策 |
| 4 | `generation_jobs.requester_id`（002）与 `created_by` 并存 | 同上 |
| 5 | `reports.artifact_id` 空悬字段 | 声明了但从不写入，无功能影响 |
| 6 | `ArtifactView` 不返回 `sha256`（`test_artifacts` 已存） | 接入方无法自行做证据字节级校验 |
| 7 | 邀请邮件未接入通知管道 | 被邀请人拿不到邮件，只能依赖创建响应里的 token |

## 关键认知（供后续接手人）

1. **单测不覆盖迁移**：所有后端单测用 `AuthBase.metadata.create_all()` 在 SQLite
   建表，schema 来自 ORM 而非 SQL 迁移。因此单测全绿**不等于**迁移与 ORM 一致。
   真实 PostgreSQL 集成测试 + `check_schema_drift.py` 才是迁移的验收依据。
2. **回滚无 down 迁移**：回滚 = `pg_dump` 快照恢复，无 SQL 回滚路径。
3. **DSL 版本偏差**：项目选严格 DSL v2，V10.3 §44.2 要求 v3，对外不得宣称符合 v3。
4. **报告要求 Run 终态**：`PASSED`/`FAILED`/`ERROR`/`CANCELLED` 才能出报告。
   没有 Browser Gateway 时 Run 会快速转 `ERROR`，这恰好让报告链可独立验证。
5. **SSE 不能用 Bearer**：必须 `POST /sse-tickets` 换一次性 cookie。直接带
   `Authorization` 读 `/events` 是 403，容易误判成权限 bug。
6. **JSON 列传 None 要显式 `none_as_null=True`**：否则写进去的是 JSON `null`
   而不是 SQL NULL，会撞 `jsonb_typeof` 类的 CHECK。同类字段新增时务必检查。
