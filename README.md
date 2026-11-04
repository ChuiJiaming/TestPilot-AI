# 🚀 TestPilot AI

## 严肃声明：AI 只建议，判定由确定性执行器决定

**「我喜欢写代码，乐此不疲」**
**「我喜欢做开源，以此为乐」**

> 这不是一句装饰话，而是这个项目的技术红线。
>
> 用例由 AI 起草地更像样，页面观察由 AI 归纳得更清楚，失败原因由 AI 解释得更易懂 ——
> 但只要涉及 **PASS / FAIL 的判定权**，一律交还给确定性执行器和已发布的断言。
> AI 不能改写断言，不能跳过断言，也不能把一次执行说成通过。

我 🐶 在上海艰苦奋斗，白天认真搬砖，夜里为开源做贡献。

如果这个项目让你有所收获，记得 Star 关注哦，这对我是非常不错的鼓励与支持。

---

## 🐶 新手必读

- **后端**：Python >= 3.11（当前验证环境为 3.11）
- **前端**：Node.js >= 20.19.0，或 >= 22.13.0，或 >= 24（三档都支持，不用纠结版本）
- **网关 / Worker**：Node.js >= 22 < 25，Worker 还需要 Docker 与 Playwright 浏览器运行时
- **数据库 / 中间件**：PostgreSQL、Redis、SMTP、S3 兼容对象存储 —— 完整业务流程缺一不可
- **本地启动**：见下方 [本地开发](#-本地开发)，两条命令即可跑起来
- **当前验证状态**：[docs/current-validation-status.md](docs/current-validation-status.md) —— 唯一权威数字快照
- **实施计划与门禁**：[docs/PLAN.md](docs/PLAN.md)

> ⚠️ 第一次接触请先读[关键认知](#-关键认知阅读前必看)，那里写了 6 条会让新人踩坑的事实。

---

## 🐯 平台简介

**TestPilot AI**，面向企业 Web 应用的 AI 辅助自动化测试平台。

管理组织与项目、测试环境、测试用例与执行计划，并通过**隔离的浏览器 Worker** 运行 Web 自动化测试。
AI 负责辅助生成场景、整理页面观察、给出解释；**测试结果由确定性的执行器和断言决定**。

一句话概括设计取向：**把 AI 放在"建议位"，把裁判权留在"确定性"手里。**

- **租户隔离**：组织 → 项目 → 成员三级 RBAC，跨租户访问一律拒绝
- **不可变测试资产**：用例版本一旦审批发布即不可改，回滚只能新建版本，历史永远可追溯
- **双人审批**：用例版本审批、系统设置变更、视觉基线更新都强制职责分离
- **异步执行骨干**：Outbox + 租约 + Reconciler，保证任务不漏发、不重复、不越权写入
- **独立浏览器执行**：Worker 与业务 API 物理分离，走 mTLS 网关，出口受 ACL 约束
- **全程可审计**：哈希链式审计日志，篡改可检测
- **证据可校验**：报告四格式导出，artifact 落盘带 SHA-256，字节级可回读验证

---

## 技术栈

### 后端

| 框架 | 说明 | 版本 |
| --- | --- | --- |
| [Python](https://www.python.org/) | 运行时 | 3.11+ |
| [FastAPI](https://fastapi.tiangolo.com/) | Web 框架 | 0.115.0 |
| [Uvicorn](https://www.uvicorn.org/) | ASGI 服务器 | 0.30.6 |
| [Pydantic](https://docs.pydantic.dev/) | 数据校验 | 2.9.2 |
| [SQLAlchemy](https://www.sqlalchemy.org/) | ORM | 2.0.35 |
| [asyncpg](https://github.com/MagicStack/asyncpg) | 异步 PG 驱动 | 0.32.0 |
| [psycopg](https://www.psycopg.org/) | 同步 PG 驱动 | 3.3.6 |
| [PyJWT](https://pyjwt.readthedocs.io/) | 会话令牌 | 2.8.0 |
| [cryptography](https://cryptography.io/) | 加密与 Fernet | 46.0.7 |
| [pwdlib](https://frankie567.github.io/pwdlib/) | Argon2 密码哈希 | 0.3.1 |
| [Celery](https://docs.celeryq.dev/) | 异步任务队列 | 5.6.3 |
| [Redis](https://redis.io/) | Broker / 限流 | 6.4.0 (client) |
| [croniter](https://github.com/pallets-eco/croniter) | Cron 解析 | 6.2.4 |
| [LangGraph](https://langchain-ai.github.io/langgraph/) | 有界 Agent 编排 | 1.2.18 |
| [langchain-openai](https://python.langchain.com/) | 兼容 OpenAI 的模型接入 | 1.7.3 |
| [boto3](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html) | S3 对象存储 | 1.43.123 |
| [reportlab](https://www.reportlab.com/) | PDF 报告渲染 | 4.4.10 |
| [Pillow](https://python-pillow.org/) | 图像处理与像素 diff | 11.2.1 |
| [pytest](https://docs.pytest.org/) | 测试框架 | 8.4.2 |

### 前端

| 框架 | 说明 | 版本 |
| --- | --- | --- |
| [React](https://react.dev/) | UI 框架 | 18.3.1 |
| [Vite](https://cn.vitejs.dev/) | 开发与构建工具 | 5.4.21 |
| [TypeScript](https://www.typescriptlang.org/) | JavaScript 超集 | 5.7.2 |
| [lucide-react](https://lucide.dev/) | 图标库 | 0.468.0 |
| [Vitest](https://vitest.dev/) | 单元测试 | 2.1.8 |
| [Playwright](https://playwright.dev/) | 端到端测试 | 1.64.1 |
| [Testing Library](https://testing-library.com/) | 组件测试 | 16.1.0 |
| [ESLint](https://eslint.org/) | 代码检查 | 9.17.0 |

### 浏览器执行

| 框架 | 说明 | 版本 |
| --- | --- | --- |
| [Playwright](https://playwright.dev/) | 浏览器自动化 | 1.56.0 |
| [ipaddr.js](https://github.com/whitequark/ipaddr.js) | 出口 IP 判定 | 2.2.0 |
| [Node.js](https://nodejs.org/) | Worker 运行时 | 22 – 24 |

### 基础设施

| 组件 | 说明 |
| --- | --- |
| PostgreSQL 18 | 业务状态、事件、Outbox（15 兼容性待验证） |
| Redis | Celery Broker、验证码限流 |
| S3 / MinIO | 截图、trace、HAR、报告 artifact |
| Nginx | TLS 终止、SSE 反代、Gateway mTLS |
| PM2 | api / agent-worker / agent-beat 三进程守护 |
| Docker | 独立 Worker 运行时 |

---

## 🔥 架构

四条纵向边界，刻意分开部署：

```
+--------------+   HTTPS    +--------------+
|  Web (React) | -------->  |     API      |   FastAPI：租户隔离、事务、契约
+--------------+            +------+-------+
                                   |
                                   | Outbox / Celery
                                   v
                            +--------------+
                            | Agent Worker |   LangGraph：有界规划、预算控制
                            |    + Beat    |   超时即停，不无限重试
                            +------+-------+
                                   |
                                   | mTLS
                                   v
                            +--------------+
                            |   Gateway    |   唯一通道，证书双向校验
                            +------+-------+
                                   |
                                   v
                            +--------------+
                            |   Browser    |   独立 Playwright，出口 ACL 约束
                            |    Worker    |   与业务 API 物理隔离
                            +--------------+
```

关键设计取舍：

- **为什么 Worker 要独立**：浏览器是最不可信的一环（SSRF、DNS 重绑定、恶意页面），把它和业务 API 放同一个进程或同一个网络平面，等于把攻击面直接接到数据库上。
- **为什么用 Outbox 而不是直接投递**：投递必须与业务事务同源，否则「事务提交了但任务丢了」或「任务发了但事务回滚了」两种事故必然出现。
- **为什么 AI 不碰判定**：LLM 输出不可复现。判定一旦依赖 LLM，同一个用例两次运行可能给出不同结论，测试就失去意义。

---

## 内置功能

按 M01–M15 十五个模块组织。状态标记含义：

- ✅ **已实现并验证** —— 有端到端或真实依赖验证证据
- 🚧 **源码完成待验证** —— 代码在，但缺真实环境/浏览器/部署验收
- ⚠️ **已知边界** —— 有明确限制，使用时必须知道

### 平台基础（M01–M03）

| 模块 | 功能 | 状态 |
| --- | --- | --- |
| M01 | 账号注册、邮件验证、登录、刷新、登出、会话管理 | ✅ 已端到端验证 |
| M01 | 刷新令牌重放检测（重放即撤销整个令牌族） | ✅ |
| M01 | API Token 签发、轮换、删除 | 🚧 |
| M02 | 组织、项目、成员三级 RBAC | ✅ 已端到端验证 |
| M02 | 邀请 → 接受 → 成员权限生效 | ✅ 已端到端验证 |
| M02 | 成员移除即时失效访问权 | ✅ |
| M03 | 测试环境 CRUD、环境快照 | ✅ 已端到端验证 |
| M03 | SecretRef 元数据（密钥值永不出现在响应里） | 🚧 |
| M03 | 环境健康检查（经 mTLS 网关 + HTTP 探针） | ✅ 已验证（`HEALTHY`） |

### 测试资产（M04–M06）

| 模块 | 功能 | 状态 |
| --- | --- | --- |
| M04 | AI 场景生成任务、人工审核、接受/取消/重试 | 🚧 需 LLM 凭据 |
| M04 | 生成失败自动重试（2/4/8 秒退避，最多 3 次） | 🚧 |
| M04 | 生成死信查看（平台管理员） | 🚧 |
| M05 | 严格 DSL v2 用例版本、不可变历史 | ✅ 已端到端验证 |
| M05 | 版本 diff 对比 | 🚧 |
| M05 | 版本审批（职责分离）、发布、回滚 | ✅ 已端到端验证 |
| M06 | 套件与套件内用例排序 | ✅ 已端到端验证 |
| M06 | 执行计划、5 段式时区感知调度 | ✅ 已端到端验证 |
| M06 | 调度启停、暂停、恢复、手动触发 | 🚧 |
| M06 | 浏览器矩阵展开为 RunCase | 🚧 需真实浏览器 |

### 执行骨干（M07–M09）

| 模块 | 功能 | 状态 |
| --- | --- | --- |
| M07 | LangGraph 有界规划器 / 探索器 | 🚧 需 LLM 凭据 |
| M07 | 预算闸门：超预算立即停止模型调用 | 🚧 |
| M07 | Postgres Checkpoint 断点续跑 | 🚧 |
| M08 | mTLS Worker 注册、心跳、就绪发现 | ✅ 已端到端验证（DEV 自签证书） |
| M08 | 最小负载分配、Worker 会话亲和 | 🚧 单 Worker 已验证，多 Worker 未验 |
| M08 | Worker 出口 ACL（DNS 固定 + 默认拒绝） | ⚠️ 脚本为候选，未应用 |
| M09 | Run 创建/查询/取消/暂停/恢复/重试 | ✅ 已端到端验证 |
| M09 | Run 状态机与终态约束 | ✅ 已端到端验证 |
| M09 | SSE 实时事件流（一次性票据 + cookie） | ✅ 已验证建流，断线重连未验 |
| M09 | Outbox → Celery 异步准入 | ✅ 已端到端验证 |
| M09 | 租约、心跳、过期回收 | 🚧 并发场景未压测 |
| M09 | 确定性 DSL 执行器与断言引擎 | ✅ 已端到端验证（真实 PASS/FAIL） |
| M09 | Run Console 控制台 | 🚧 |

### 恢复与质量分析（M10–M12）

| 模块 | 功能 | 状态 |
| --- | --- | --- |
| M10 | 定位器恢复候选生成 | 🚧 |
| M10 | 每个候选强制人工审批，拒绝/过期绝不生效 | 🚧 |
| M10 | 恢复后重放单个失败动作并复核原始断言 | 🚧 |
| M11 | 失败分析器（证据约束、脱敏假设） | 🚧 |
| M11 | 确定性指纹归组、缺陷发生记录 | 🚧 |
| M11 | 缺陷分诊与哈希链审计 | 🚧 |
| M12 | 视觉基线提案（精确上下文 + 职责分离） | 🚧 |
| M12 | 遮罩式确定性像素 diff | 🚧 |
| M12 | 质量工作台页面 | 🚧 |

### 报告与集成（M13–M15）

| 模块 | 功能 | 状态 |
| --- | --- | --- |
| M13 | 报告快照（基于持久化 Run 事实） | ✅ 已端到端验证 |
| M13 | HTML / PDF / JSON / JUnit 四格式导出 | ✅ **4/4 渲染通过** |
| M13 | 导出 artifact 落盘 + SHA-256 校验 | ✅ **6/6 字节级一致** |
| M13 | 导出下载重新授权校验 | 🚧 |
| M14 | CI 触发（幂等键含环境与浏览器矩阵） | 🚧 |
| M14 | 签名 Webhook 入站、重放拒绝 | 🚧 |
| M14 | 出站投递重试与可见死信 | 🚧 |
| M15 | 配额预留与结算（provider 边界硬限制） | 🚧 |
| M15 | 通知去重、偏好设置、死信可见 | 🚧 |
| M15 | 用量统计、审计日志哈希链校验 | 🚧 |
| M15 | 系统设置双人审批 | 🚧 |
| M15 | 运维页面 | 🚧 |

### 部署（Phase 6）

| 项目 | 说明 | 状态 |
| --- | --- | --- |
| PM2 配置 | api / agent-worker / agent-beat 三进程 | 🚧 语法校验通过，未真机 |
| Nginx 配置 | TLS、SSE buffer-off、Gateway mTLS 反代 | 🚧 需目标主机 `nginx -t` |
| Worker 出口 ACL | DOCKER-USER 链，DNS 固定 + 默认 REJECT | ⚠️ 候选脚本，未应用 |

---

## 项目结构

```
TestPilot-AI/
├── apps/
│   ├── web/                 React + TypeScript + Vite 单页应用
│   ├── api/                 FastAPI 业务 API、租户权限、持久化、Run 服务
│   ├── browser_gateway/     API/Agent 与浏览器 Worker 之间的 mTLS 网关
│   └── browser_worker/      独立 Playwright Worker（按会话约束出口）
├── db/migrations/           编号 PostgreSQL 迁移 001–018 + 只读检查工具
├── api/openapi.yaml         HTTP API 契约
├── contracts/               契约测试
├── deploy/                  PM2 / Nginx / ACL / 部署说明
├── docs/                    规范、计划、决策、追踪矩阵、验证记录
└── tests/                   API、数据层、前端测试
```

---

## 🚀 本地开发

### 启动 API

```bash
# 仓库根目录
python -m pip install -e "apps/api[test]"
python -m uvicorn apps.api.app.main:app --reload --host 127.0.0.1 --port 8000
```

启动后打开 `http://127.0.0.1:8000/docs` 查看 OpenAPI 文档。

> 未配置认证运行环境时，API 仅供本地契约开发：认证端点返回 `503`，
> **不会创建账户、也不会签发占位令牌**。这是刻意设计，避免"假能登录"掩盖配置缺失。

完整变量列表见 [API 文档](apps/api/README.md)。

### 启动 Web

另开一个终端：

```bash
npm --prefix apps/web ci
npm --prefix apps/web run dev
```

Vite 默认把 `/api` 转发到 `http://127.0.0.1:8000`，可用 `TESTPILOT_API_PROXY` 覆盖。
前端独立命令与页面范围见 [Web 文档](apps/web/README.md)。

### 跑测试

```bash
# 后端：全依赖真实（PostgreSQL + Redis + Mailpit 都指向真实实例时零 skip）
TESTPILOT_TEST_DATABASE_URL=postgresql://postgres:***@127.0.0.1:5432/testpilot \
TESTPILOT_TEST_REDIS_URL=redis://127.0.0.1:6379/0 \
TESTPILOT_TEST_SMTP_HOST=127.0.0.1 TESTPILOT_TEST_SMTP_PORT=1025 \
TESTPILOT_TEST_MAILPIT_API_URL=http://127.0.0.1:8025 \
PYTHONPATH=. python -m pytest tests -q -rs

# 前端
npm --prefix apps/web test
npm --prefix apps/web run typecheck
```

### 迁移与漂移检查

```bash
# 只读预检，不对业务库动手
python -m apps.api.scripts.migrate --check

# ORM 与真实表结构漂移检测（0 fatal 才算过）
TESTPILOT_DATABASE_URL=postgresql://... PYTHONPATH=. \
  python apps/api/scripts/check_schema_drift.py
```

> Gateway、Worker、Celery 与迁移涉及额外凭据、网络策略和状态账本。
> 首次运行前请读对应服务文档、[迁移说明](db/migrations/README.md)与[统一验证计划](docs/PLAN.md)。
> **不要直接对业务数据库应用候选迁移。**

---

## 🧠 关键认知（阅读前必看）

这 6 条是踩过坑之后写下来的，能省你几个小时：

1. **单测不覆盖迁移** —— 所有后端单测用 `AuthBase.metadata.create_all()` 在 SQLite 建表，
   schema 来自 **ORM 而非 SQL 迁移**。所以单测全绿**不等于**迁移与 ORM 一致。
   真实 PostgreSQL 集成测试 + `check_schema_drift.py` 才是迁移的验收依据。
2. **JSON 列传 None 要显式 `none_as_null=True`** —— 否则写进库的是 JSON `null`
   而不是 SQL `NULL`，会撞 `jsonb_typeof` 类 CHECK 约束，报错信息还很不直观。
3. **SSE 不能用 Bearer** —— 必须 `POST /sse-tickets`（带 `Origin`）换一次性 cookie。
   直接带 `Authorization` 读 `/events` 是 403，极易误判成权限 bug。
4. **报告要求 Run 处于终态** —— `PASSED`/`FAILED`/`ERROR`/`CANCELLED` 才出报告。
   没有 Browser Gateway 时 Run 会快速转 `ERROR`，这恰好让报告链可以独立验证。
5. **回滚没有 down 迁移** —— 回滚 = `pg_dump` 快照恢复，没有 SQL 回滚路径。
6. **DSL 版本偏差** —— 项目选**严格 DSL v2**，V10.3 §44.2 要求 v3。
   这是明确的、有记录的选择，**对外不得宣称符合 v3**。详见[阶段二契约与决策记录](docs/decisions/phase-2-open-issues.md)。

---

## ⚠️ 现状与边界

**源码完成 ≠ 验收通过。** 这个项目严格区分这两件事，README 也必须说清楚。

当前已达成：

- 迁移链 001–018 在 PostgreSQL 18 上全量应用通过，账本 18/18，预检全零
- ORM 与真实表结构漂移 **0 fatal**
- 后端测试 **312 passed / 0 skipped**（PostgreSQL + Redis + Mailpit 全真实）
- 前端测试 31 passed，typecheck 通过
- 运行时路由注册 **101/101 基线 + 27/27 附加**
- 黄金路径端到端（无 LLM）**29 PASS / 0 FAIL**
- 报告四格式导出 **4/4**，artifact 字节级回读 **6/6**
- 真实浏览器链路（mTLS Gateway + Worker + Playwright）**43 PASS / 0 FAIL**：
  健康检查 `HEALTHY`、PASS 用例 `PASSED`、错误断言 `FAILED`、截图 sha256 与库中记录一致

仍未验证（不得视为通过）：

- 真实 AI 生成链路（需 LLM provider 凭据）
- 多 Worker 负载分配与会话亲和（当前单 Worker）
- Worker 出口 ACL（脚本为 CANDIDATE，未应用）
- trace / HAR 证据采集 —— **并未实现**，只有截图落盘
- 邀请邮件链路（未接入通知管道）
- Phase 6 部署真机验收（`nginx -t`、`pm2 ls`）
- SSE 断线重连
- PostgreSQL 15 兼容性
- 并发与锁（当前单 Run 串行验证）

完整清单见[当前验证状态](docs/current-validation-status.md)，
端到端细节见[Phase C-lite 验证记录](docs/2026-11-03-phase-c-lite-end-to-end.md)。

### 已知契约不一致（未修，接入方需注意）

| # | 问题 | 影响 |
| --- | --- | --- |
| 1 | `POST /test-runs` 运行时强制 `Idempotency-Key`，OpenAPI 标为可选 | 严格按契约实现的客户端会吃 400 |
| 2 | worker 强制 verify URL 为 https，API 侧不校验 | 同一份配置 API 接受、worker 拒绝 |
| 3 | `run_cases.browser` 与 `browser_name` 双列并存 | 语义重叠，待决策 |
| 4 | `generation_jobs.requester_id` 与 `created_by` 并存 | 同上 |
| 5 | `reports.artifact_id` 空悬字段 | 声明了但从不写入，无功能影响 |
| 6 | `ArtifactView` 不返回 `sha256`（库中已存） | 接入方无法自行做证据字节级校验 |
| 7 | 邀请邮件未接入通知管道 | Mailpit 收不到邀请邮件，token 只在创建响应返回 |

---

## 📚 关键文档

- [V10.3 产品与技术规范](docs/TestPilot_V10.3.docx)
- [实施计划与各阶段验证门禁](docs/PLAN.md)
- [当前验证状态（唯一权威数字）](docs/current-validation-status.md)
- [M01–M15 需求追踪矩阵](docs/traceability-matrix.md)
- [阶段二未决契约与业务决策](docs/decisions/phase-2-open-issues.md)
- [迁移验证记录](docs/2026-11-03-migration-validation.md)
- [Phase C-lite 端到端验证](docs/2026-11-03-phase-c-lite-end-to-end.md)
- [真实浏览器链路验证](docs/2026-11-04-browser-chain-validation.md)
- [API 与 OpenAPI 契约](api/openapi.yaml)
- [PostgreSQL 迁移说明](db/migrations/README.md)
- [Browser Gateway](apps/browser_gateway/README.md) · [Browser Worker](apps/browser_worker/README.md)

---

## 许可证

本仓库当前**尚未包含 LICENSE 文件**，因此未声明开源许可证。

如果你计划开源，建议补充 `MIT` 或 `Apache-2.0`；在此之前，
默认保留所有权利。请勿在未明确授权的情况下用于商业分发。

---

<p align="center">
  <sub>Built with ❤️ by <a href="https://github.com/ChuiJiaming">Cui Haidong</a> · 让 AI 建议，让代码裁决</sub>
</p>
