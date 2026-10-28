# TestPilot AI

> 面向企业 Web 应用的 AI 辅助自动化测试平台。当前仓库包含第一至第三阶段的源码实现候选；是否通过验收，以统一验证记录为准。

## 项目简介

TestPilot AI 用于管理组织与项目、测试环境、测试用例和执行计划，并通过隔离的浏览器 Worker 运行 Web 自动化测试。AI 可以辅助生成场景、整理页面观察或给出解释；测试结果由确定性的执行器和断言决定，AI 不能改写已发布用例的断言。

目标范围对应 V10.3 的 M01–M15，并保留基线 OpenAPI 中的 101 个操作。当前实现与待完成事项见[实施计划](docs/PLAN.md)和[需求追踪矩阵](docs/traceability-matrix.md)。

## 当前实现状态

| 范围 | 当前源码 | 验证状态 |
| --- | --- | --- |
| M01–M03：平台基础 | 账号与会话、组织/项目成员权限、环境与快照、SecretRef 元数据、Web 基础界面及隔离健康检查候选实现 | 有阶段性本地验证记录；完整授权、生产邮件和部署验证仍待统一验证 |
| M04–M06：测试资产 | 场景生成与人工审核、用例版本与审批、套件/计划/定时调度候选实现 | 阶段二验收、数据库迁移、浏览器流程和构建验证尚未执行 |
| M07–M09：执行骨干 | Run API 与事件、Outbox/租约、计划并发准入、LangGraph 检查点、Run Console、Gateway 和 Playwright Worker 候选实现 | 阶段三端到端并发、恢复、浏览器、安全和部署验证尚未执行 |
| M10–M15 及发布 | 恢复与质量分析、报告与集成、部署加固和发布门禁 | 后续阶段，按[实施计划](docs/PLAN.md)推进 |

**注意事项：**

- `RunAdmissionRepository.claim` 已接入 `RUN_REQUESTED` 消费者和有界队列扫描器；并发上限是否端到端生效仍需统一验证。
- 项目决策保留严格 DSL v2。这与 V10.3 §44.2 指定的 DSL v3 有明确差异，当前实现不应描述为符合该 DSL v3 条款。详情见[第二阶段契约与决策记录](docs/decisions/phase-2-open-issues.md)。
- 数据库迁移 008–013 仍是未应用的候选迁移。应用迁移前，先核对[迁移说明](db/migrations/README.md)中的顺序、账本和验证要求。
- 阶段二、三的测试、迁移执行、构建、浏览器、安全和部署验证仍待统一验证；阶段一的局部证据及其限制见[验证记录](docs/TestPilot_AI_V10.3_验证记录.txt)。源码存在不代表相应验收已通过。

## 架构与目录

- `apps/web/`：React、TypeScript、Vite 单页应用。
- `apps/api/`：FastAPI 业务 API、租户权限、持久化、生成与 Run 相关服务。
- `apps/browser_gateway/`：API/Agent Worker 与独立浏览器 Worker 之间的 mTLS 网关。
- `apps/browser_worker/`：独立 Playwright Worker；浏览器流量通过按会话约束的出口代理。
- `db/migrations/`：编号 PostgreSQL 迁移候选及只读迁移检查工具说明。
- `api/openapi.yaml`：HTTP API 契约候选。
- `tests/`：API、数据层和前端测试代码。
- `docs/`：V10.3 规范、计划、决策、追踪矩阵及验证记录。

目标架构使用 PostgreSQL 保存业务状态、事件和 Outbox，Redis/Celery 处理异步任务，LangGraph 管理有界 Agent 流程；浏览器执行与业务 API 分离。对象存储、生产级证书、网络出口 ACL 和部署环境需要单独配置与验证。

## 本地开发

### 环境要求

- API：Python 3.11 或更新版本。
- Web：Node.js 20.19+、22.13+ 或 24+。
- Browser Gateway/Worker：Node.js 22–24；Worker 还需要 Docker、Playwright 浏览器运行时和受控网络环境。
- 完整业务流程还需要按服务配置 PostgreSQL、Redis、SMTP、LLM 凭据与价格表，以及 Gateway/Worker 的 mTLS 证书。不要把真实凭据提交到仓库。

### 启动 API

在仓库根目录安装 API 依赖并启动开发服务：

```powershell
python -m pip install -e "apps/api[test]"
python -m uvicorn apps.api.app.main:app --reload --host 127.0.0.1 --port 8000
```

未配置认证运行环境时，API 仅供本地契约开发；认证端点会返回 `503`，不会创建账户或签发占位令牌。完整变量列表和迁移说明见 [API 文档](apps/api/README.md)。

### 启动 Web

在另一个终端运行：

```powershell
npm --prefix apps/web ci
npm --prefix apps/web run dev
```

Vite 默认把 `/api` 请求转发到 `http://127.0.0.1:8000`；可通过 `TESTPILOT_API_PROXY` 覆盖。前端的独立命令、版本要求和当前页面范围见 [Web 文档](apps/web/README.md)。

Gateway、Worker、Celery 与数据库迁移涉及额外凭据、网络策略和状态账本；首次运行前请阅读对应服务文档、[迁移说明](db/migrations/README.md)及[统一验证计划](docs/PLAN.md)，不要直接对业务数据库应用候选迁移。

## 关键文档

- [V10.3 产品与技术规范](docs/TestPilot_V10.3.docx)
- [实施计划与各阶段验证门禁](docs/PLAN.md)
- [M01–M15 需求追踪矩阵](docs/traceability-matrix.md)
- [阶段二未决契约与业务决策](docs/decisions/phase-2-open-issues.md)
- [V10.3 验证记录与限制](docs/TestPilot_AI_V10.3_验证记录.txt)
- [API 与 OpenAPI 契约](api/openapi.yaml)
- [PostgreSQL 迁移说明](db/migrations/README.md)
- [Browser Gateway](apps/browser_gateway/README.md) · [Browser Worker](apps/browser_worker/README.md)
