# Sureshot 生产就绪度差距评估

> 评估日期：2026-11-05
> 评估方式：源码走查 + 实测复跑（不是读文档）
> 结论先行：**功能完成度高，可运营性低。这套系统现在能演示、能交付源码，但不能交给一个客户自己跑生产。**

---

## 1. 一句话结论

| 维度 | 现状 | 判断 |
| --- | --- | --- |
| 功能覆盖（M01–M15） | 140 个 HTTP 操作、18 个迁移、15 个模块 | 约 85%，够用 |
| 测试体量 | 后端 328 项（今天实测 323 passed / 1 failed / 4 skipped，114s）、前端 34 项 | 体量达标，稳定性不达标 |
| **可运维性** | **无 CI、无日志、无指标、无告警、无自动备份** | **约 20%，这是主要缺口** |
| **可交付性** | **部署配置从未在真机跑过，无一键安装、无升级路径** | **约 30%** |
| 安全落地 | 设计完整（mTLS、租户隔离、审计链），落地缺 3 项关键 | 约 55% |
| 性能容量 | 无任何压测、无并发验证、连接池用默认值 | **未评估** |

**综合生产就绪度：约 50%。缺口不在功能，在「出问题能不能查、能不能恢复、能不能升级」。**

---

## 1.1 修复进展（2026-11-05，本轮）

| 原 P0 项 | 状态 | 落地内容 | 证据 |
| --- | --- | --- | --- |
| 2.1 无 CI/CD | ✅ 已补 | `.github/workflows/ci.yml`：后端测试 + 迁移链 001–019 全量应用与账本校验 + 前端 lint/typecheck/test/build | 工作流文件已就位，**尚未在 GitHub 上真实跑过** |
| 2.3 可观测性为零 | ✅ 已补 | 请求 ID 中间件、JSON 结构化日志、`/metrics`（Prometheus 文本）、5xx 堆栈落日志、`/health/live` | `tests/test_observability.py` 8 例通过 |
| 2.4 无自动备份 | ✅ 已补 | `backup-postgres.sh`（含 SHA-256 清单与保留策略）、`verify-backup.sh`、`restore-postgres.sh`；`docs/RUNBOOK.md` 第 4 节 | 脚本已就位，**未在真实主机跑过** |
| 2.5 无密码找回 | ✅ 已补 | `POST /auth/password-reset/request` + `/confirm`；迁移 019 增加 `purpose`；重置即吊销全部会话与 refresh token | `tests/test_password_reset.py` 8 例通过 |
| 2.7 SecretRef 无实现 | ✅ 已补 | `EnvironmentSecretBackend`（env / 只读文件挂载）+ `DatabaseSecretRefResolver`，租户范围写在查询条件里，缺密钥即 503 | `tests/test_secret_resolver.py` 8 例通过 |
| 2.9 依赖未锁版本 | ✅ 已补 | `apps/api/constraints.txt`（95 个包精确版本）；CI 用 `-c` 安装 | 全量测试在锁定版本下 348 passed |
| 3.4 连接池默认值 | ✅ 已补 | `core/db.py`：pool_size/max_overflow/timeout/recycle + PostgreSQL `statement_timeout` | 环境变量见 `deploy/.env.example` |
| 2.2 真机部署未验证 | ❌ 未动 | 需要一台真实 Ubuntu 主机 | — |
| 2.6 真实 LLM 未验证 | ❌ 未动 | 需要 provider 凭据 | — |
| 2.8 邀请邮件未接管道 | ❌ 未动 | 邀请人可能还不是平台用户，无法复用通知管道，需要独立发信路径 | — |
| 2.10 单点架构 | ❌ 未动 | 架构决策，非本轮范围 | — |

> 本轮后端实测：**348 passed / 4 skipped / 0 failed**（207s）。相比评估时的 323 passed + 1 failed，失败的 M14 用例已修（根因是 FastAPI 0.162 把 `include_router` 改成惰性对象，测试反射 `app.routes` 看不到子路由）。

---

## 2. P0 阻断项（不修完，上生产就是事故）

### 2.1 没有任何 CI/CD ✅ 已修

```
$ ls .github .gitlab-ci.yml .gitlab
（全部不存在）
```

全部测试、构建、迁移校验都靠本机手工敲命令。后果很直接：今天实测就出现了文档说「312 passed」而实际「1 failed」的偏差——没有任何机制阻止它发生。没有 tag、没有 release 流程，`git tag` 为空，版本号还停在 `0.1.0`。

**修法**：一条 GitHub Actions / GitLab CI 流水线，跑 pytest + tsc + vitest + 迁移 `--apply`/`--check`（起 PG/Redis service 容器）。半天工作量，是全项目性价比最高的一件事。

### 2.2 部署配置从未在真机验证

`deploy/README.md` 自己写明「候选配置，未在真实主机上运行验证」。`nginx -t` 没跑过、`pm2 start` 没跑过、`worker-egress-acl.sh` 头部写着 `STATUS: CANDIDATE — NOT applied`。三份配置文件只做过 `node --check` 和 `bash -n` 的语法检查。

这意味着：**第一次真实部署大概率当场失败**，且失败点无法预测（路径、权限、TLS 链、SSE 反代、DNS 固定）。

**修法**：拿一台干净 Ubuntu，从零走一遍 `deploy/README.md` 第 4 节，把第 7 节验收清单逐条打勾，把踩到的坑回写文档。这一步至少 1–2 天。

### 2.3 可观测性为零 ✅ 已修

全库（`apps/api/app`、`apps/browser_gateway`、`apps/browser_worker`）搜索结果：

- `add_middleware`：**0 处**
- `logging.basicConfig` / `dictConfig` / `LOG_LEVEL`：**0 处**
- 指标端点（Prometheus / OpenTelemetry）：**0 处**
- 错误上报（Sentry 类）：**0 处**
- 请求 ID：**0 处**。`core/errors.py` 里的 `trace_id` 是每次 `uuid4()` 现场生成的，不写入任何日志，也不进响应头，**无法把用户报障和日志对上**

唯一一处日志是 `quality/persistence.py:1153` 的 `logger.warning`。

生产后果：客户说「昨天下午有个 Run 一直转圈」，你手上没有任何数据能定位。**这是能不能做生意的问题，不是技术洁癖。**

**修法**：请求 ID 中间件（写响应头 + 结构化日志）→ JSON 日志 → Prometheus `/metrics`（Run 状态数、队列深度、Worker 心跳、LLM 调用耗时与花费、5xx 率）→ 告警规则（队列积压、Worker 掉线、错误率）。约 3–4 天。

### 2.4 没有自动备份与灾难恢复 ✅ 已补脚本

只有一次性的 `pg_dump`/`pg_restore` 演练记录（`docs/2026-11-03-migration-validation.md`），仓库里没有任何备份脚本、没有定时任务、没有 RPO/RTO 承诺、没有恢复演练计划。S3 里的截图/trace/report 与 PostgreSQL 之间的一致性也没有备份方案。

**修法**：`pg_dump` 定时 + 保留策略 + 异地存放 + S3 版本化，再写一份「从零恢复到可用」的 runbook 并实际演练一次。

### 2.5 用户忘记密码 = 账号永久锁死 ✅ 已修

`grep -rn "reset\|forgot" apps/api/app/auth/` → **0 结果**。没有找回密码、没有改密码入口、没有 MFA、没有账号注销、没有数据导出。

这是 To B SaaS 的硬伤：第一个客户忘记密码，你就得手动改数据库。

**修法**：找回密码（复用已有邮件 Outbox，成本很低）→ 改密码 → 账号注销 → 数据导出。找回密码是 P0，其余 P1。

### 2.6 核心卖点「AI 生成」从未用真实模型验证

`docs/current-validation-status.md` 明确写着：「真实 AI 生成链路（Phase C 完整版）：未做，需 LLM provider 凭据」。Phase C-lite 是 stub 跑通的。同时 `planner_provider.py:51` 硬编码 `timeout=90, max_retries=0`，`runs/planner.py:44` 硬编码 `timeout=30, max_retries=0`——**真实 LLM 首token延迟抖动或限流时会大面积失败且不重试**。

**修法**：接一个真实 provider，跑 20 次连续生成，记录成功率、P95 延迟、token 花费、失败模式。这一步不做，产品叙事就是空的。

### 2.7 SecretRef 没有生产实现 ✅ 已修

`apps/api/app/integrations/secrets.py` 只有两个类：`SecretRefResolver`（Protocol）和 `UnavailableSecretRefResolver`（一调用就抛 `SECRET_PROVIDER_UNAVAILABLE`）。`create_default_app()` 里根本没传 resolver。

也就是说：**任何走 SecretRef 的 CI 集成 / 环境鉴权，在生产里必然 503**。UI 上能配，实际功能是断的。

**修法**：实现一个环境变量 / Vault / AWS Secrets Manager 的 resolver，接进 `create_default_app`，补一个集成测试。

### 2.8 邀请邮件没接通知管道

邀请接口存在（`auth/lifecycle_routes.py:126`），但邮件从不发送，token 只随创建响应返回。真实客户流程：管理员邀请同事 → 同事收不到任何邮件 → 流程断在这里。

### 2.9 依赖不锁版本，已经造成回归 ✅ 已修

- `apps/web/package-lock.json` 有，前端可复现
- **后端没有任何 lockfile**（无 requirements.txt / uv.lock / poetry.lock），`pyproject.toml` 全是 `>=` 开区间
- 实测环境装的是 **FastAPI 0.162.0 / Starlette 1.10.0**，README 宣称 0.115

后果已经出现：`tests/test_m14_routes.py::test_baseline_integration_operation_ids_are_registered` 永久失败——FastAPI 新版把 `include_router` 变成惰性的 `_IncludedRouter`，测试遍历 `app.routes` 拿不到子路由（实测 `/openapi.json` 仍正常暴露 140 个操作，**接口没坏，是测试坏了**）。这就是不锁版本的典型代价：一次 minor 升级静默打红一个测试。

**修法**：`uv lock` 或 `pip-compile` 产出锁定文件，CI 用锁定文件安装。

### 2.10 单点架构，没有高可用

单机 PM2、单 Beat、单 PG、单 Redis、单 Gateway、Worker 只有 1 个实例（多 Worker 负载分配与会话亲和从未验证）。没有滚动发布、没有优雅停机验证、没有零停机升级路径。

对「卖源码给客户自己部署」的模式，这条可以降级为 P1（客户自己决定拓扑）；但你不能宣称「企业级」。

---

## 3. P1（上线第一个月一定会踩）

| # | 问题 | 证据 | 影响 |
| --- | --- | --- | --- |
| 3.1 | trace / HAR **没有任何生产写入路径**，只存在于 `artifact_type` 枚举 | `current-validation-status.md` 自认 | README「证据可校验」被过度宣称，客户会发现只有截图 |
| 3.2 | 出口 ACL 未应用，SSRF 防护只在内层容器 | `worker-egress-acl.sh` 标 CANDIDATE | DNS 重绑定绕过容器策略时没有主机层兜底 |
| 3.3 | 无全局限流，只有验证邮件限流 + Nginx 对 auth 路径 5r/s | `auth/adapters.py:62` 是唯一实现 | 登录爆破之外的接口可被刷；LLM 接口被打 = 直接烧钱 |
| 3.4 | 连接池用 SQLAlchemy 默认值（pool_size=5, overflow=10） | `auth/bootstrap.py:115` 只设了 `pool_pre_ping` | 20 个并发用户就能打满，表现为莫名 500 |
| 3.5 | 无查询超时、无 statement_timeout、无 LLM 重试 | 全库搜索为空 | 一条慢查询拖垮整个 API |
| 3.6 | SSE 断线重连从未验证 | 只验了首次建流 | Run 控制台在生产网络下会「假死」 |
| 3.7 | PostgreSQL 只在 18 上验证，文档却写支持 15+ | `current-validation-status.md` | 客户用 PG15 部署，行为未知 |
| 3.8 | 7 项已知契约不一致未修 | `current-validation-status.md` 第 99 行起 | 严格按 OpenAPI 接入的 CI 客户端会 400 |
| 3.9 | 无并发、无压测、无长稳测试 | 单 Run 串行验证 | 容量数字全靠猜，无法给客户任何承诺 |
| 3.10 | 无审计日志保留/归档策略 | 只有哈希链校验 | 表无限增长；合规审计拿不出「保留 6 个月」的证据 |
| 3.11 | 无 MFA、无账号注销、无数据导出 | 全库搜索为空 | 政企客户的合规问卷直接过不了 |
| 3.12 | 无 SBOM、无依赖漏洞扫描、无许可证合规检查 | 无 CI，自然没有 | 卖闭源商业软件却不做第三方组件清点，法律风险 |

---

## 4. P2 工程卫生（不影响能不能跑，影响能不能长期维护）

- **Python 端零静态检查**：无 ruff、无 mypy、无 black、无 pre-commit。前端有 ESLint，后端只有 328 个测试兜底。
- **无覆盖率统计**：不知道真实覆盖率。20,451 行后端代码，328 个测试，覆盖深浅无从判断。
- **前端测试 flaky**：同一台机器连跑三次，结果分别是 23 passed / 29 passed(3 failed) / 34 passed。测试用真实计时器 + 轮询，慢机器上就掉。CI 上会变成「随机红」。
- **前端是巨石应用**：4,472 行、9 个页面文件，`App.tsx` 680 行、`OperationsPage.tsx` 825 行；无路由库（手写 `Page` 联合类型）、无状态管理、无 ErrorBoundary、无 i18n、无 a11y 检查。能跑，但加第三个开发者就会互相踩。
- **E2E 只有 4 个 spec**，且跑在 `publish-demo/` 的 mock server 上，不是真后端。
- **运行态文件散落在仓库根**：`celerybeat-schedule.dat/.dir/.bak`（已 gitignore 但物理存在于工作区，说明生产跑法不干净）。
- **文档与实际脱节**：`current-validation-status.md` 记 312 passed，今天实测 323 passed 1 failed；README 记 FastAPI 0.115，实际 0.162。所谓「唯一权威快照」已经不准。

---

## 5. 建议路线（按投入产出排序）

### 阶段一：把「不会出事」补上（1 周）

1. 搭 CI：pytest + tsc + vitest + 迁移 --apply/--check（半天）
2. 锁依赖：`uv lock`，CI 用锁文件；顺手修掉 M14 那个测试（半天）
3. 请求 ID 中间件 + JSON 结构化日志 + `/metrics`（2 天）
4. 备份脚本 + 恢复 runbook + 实跑一次（1 天）
5. 找回密码 + 改密码（1 天）
6. SecretRef 真实实现 + 邀请邮件接入 Outbox（1 天）

### 阶段二：把「能跑起来」补上（1–2 周）

7. 干净 Ubuntu 真机全流程部署，逐条打勾 `deploy/README.md` 第 7 节
8. 应用出口 ACL，跑 SSRF 负向用例
9. 连接池 + statement_timeout + LLM 重试
10. SSE 断线重连
11. 真实 LLM 链路 20 次连续验证，记录成功率/延迟/成本
12. 修 7 项契约不一致

### 阶段三：把「能交付、能运营」补上（2–3 周）

13. 一键安装脚本（或 docker compose 全家桶）+ 升级/回滚路径
14. 压测与容量基线（多少并发、多少 Run/小时，写进交付文档）
15. 运维手册：故障树、日志在哪、怎么重启、怎么扩容
16. SBOM + 依赖扫描 + 许可证清单
17. 打 tag、出 release note、版本号离开 0.1.0

---

## 6. 两个不好听但必须说的话

**第一，你现在缺的不是功能，是「可运营性」。** M01–M15 十五个模块、140 个接口、18 个迁移、328 个测试——这个体量说明工程能力没问题。但从「能演示」到「客户敢用」，中间隔的是日志、备份、部署验证和密码找回这些不性感的东西。它们加起来不到一个月，但一件不做，前面所有功能都卖不出第二单。

**第二，¥699 源码买断和「企业级」叙事是冲突的。** 买断意味着客户自己运维，而现在这套东西没有安装器、没有升级路径、没有运维手册、没有监控。客户买回去大概率装不起来，然后来找你——这比不卖更消耗人。

要么把定价和定位改成「开发者工具包 / 二次开发底座」，承认它需要技术团队接手；要么把上面阶段一、二做完，改成「部署服务 + 年度维护」的定价模型。中间地带最难受。
