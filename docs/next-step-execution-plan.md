# 交付路线执行清单（阶段 6 前置）

> 目标：把「源码完成」推进到「全链路可验证」。本清单只做验证与加固，**不新增功能**。
> 起点：2026-11-03 实测基线。终点：一次可复现的黄金路径端到端 + 一份可交付的迁移证据。

---

## 起点基线（2026-11-03 实测）

| 检查 | 结果 | 说明 |
| --- | --- | --- |
| 后端测试 | **296 passed, 4 skipped**（132s） | 命令排除了两个 Windows 无法运行的套件 |
| 前端 typecheck | 通过 | `tsc -b` 无错误 |
| 前端单元测试 | **31 passed / 7 files**（67s） | Vitest |
| 路由注册 | 101/101 基线 + 27/27 附加 | 仅注册数，非契约符合性 |
| 迁移已应用 | 001–007（仅此前验证过） | **008–018 共 11 个未应用** |
| 环境 | Docker 28.0.1（引擎未启动）、PostgreSQL 18 本机 5432、WSL 被拦截 | — |

> ⚠️ `docs/phase-1-4-unified-validation.md` 记的是 10-30 的旧快照（151 passed / 15 web tests）。
> 该文件是历史证据，**不要改写**；第 10 天另建新记录并在 README 引用。

---

## Phase A — 修开发环境（Day 1–2）

目标：让完整测试能在 Linux 语义下跑通，消除「本机跑不了」的盲区。

- [x] **A1** 现状确认：Docker CLI 28.0.1 已装，引擎未运行（`docker info` 报 pipe 不存在）
- [x] **A2** 启动 Docker Desktop，确认引擎可用
  ```bash
  docker info --format '{{.ServerVersion}}'
  ```
- [ ] **A3** 起一次性验证栈（**不要**复用本机 5432 的 `testpilot` 库）
  ```bash
  cd "D:/workbench/个人工作室/TestPilot-AI"
  docker run -d --name tp-pg -e POSTGRES_PASSWORD=tp_dev_only \
    -p 55432:5432 postgres:18
  docker run -d --name tp-redis -p 56379:6379 redis:7
  ```
- [ ] **A4** 在 Linux 容器里跑两个挂起的路由套件（绕过 Windows asyncio socketpair 卡死）
  ```bash
  docker run --rm -v "$PWD":/src -w /src python:3.11-slim \
    bash -c "pip install -q -e 'apps/api[test]' && \
    python -m pytest tests/test_lifecycle_routes.py tests/test_m10_healing_routes.py -q"
  ```
- [ ] **A5** 记录 A4 的真实结果（通过或真实失败都算进展；**只有跑不了才是问题**）

**Gate A**：两个套件在 Linux 下产出明确结果，`docker info` 正常。

---

## Phase B — 迁移演练（Day 3–5）· 本项目最高风险项

风险判断：11 个迁移分属 5 个开发阶段，彼此可能踩过同一张表。它们能否在干净库上一次跑通**从未验证**。
这是全项目「验证成本最低、失败代价最高」的一件事。

- [x] **B1** 干净库全量应用
  ```bash
  export TESTPILOT_DATABASE_URL="postgresql://postgres:tp_dev_only@127.0.0.1:55432/testpilot_mig"
  python -m apps.api.scripts.migrate --apply
  python -m apps.api.scripts.migrate --check
  ```
  期望：001→018 顺序提交，账本 SHA-256 全部校验通过。**任一步失败即停止**，记录文件名与报错原文。
  **结果：首跑在 008 失败（6 处重复定义 + ORM 漂移），修复后 18/18 全通。详见 `docs/2026-11-03-migration-validation.md`。**
- [x] **B2** 005 只读预检五项计数必须全为 0
  ```bash
  psql "$TESTPILOT_DATABASE_URL" -f db/migrations/005_preflight_and_validate.sql
  ```
  **结果：五项全 0。**
- [x] **B3** 存量升级演练（模拟真实客户库）
  - 新建 `testpilot_upg`，只应用到 007
  - 插入样例数据：1 个 user、1 个 org、1 个项目、1 条 refresh-token family
  - 继续应用到 018，确认旧 user 仍 active、旧 token family 映射为 `Legacy device`
  **结果：通过。旧用户 active、token family → Legacy device 未撤销、组织项目关系完整。**
- [x] **B4** 回滚演练
  本项目**没有 down 迁移**，回滚 = 快照恢复。必须先建立这个认知，再验证：
  ```bash
  pg_dump "$TESTPILOT_DATABASE_URL" -Fc -f backup_after_018.dump
  # 恢复到新库并校验表数量与关键行数一致
  ```
  **结果：通过。pg_restore 后 57 表一致、存量数据完整还原。**
- [x] **B5** 产出迁移证据表：每个迁移一行（文件 / 应用耗时 / 结果 / 失败原文）
  **结果：见 `docs/2026-11-03-migration-validation.md`。**

**Gate B**：`--check` 全绿 + preflight 五项为 0 + 一份可恢复 dump + 一份迁移证据表。
未过 Gate B **不得**开始 Phase 6 部署配置（给未验证的 schema 写 Nginx/PM2 是给流沙盖房）。

---

## Phase 6 — 部署与加固（已完成候选配置，待真机验证）

> 在 Phase B（迁移 Gate B）通过后启动。交付物为候选配置，**需在 Ubuntu 主机实测**才可进入 Phase 7。

- [x] **6.1** PM2 ecosystem：`deploy/pm2/ecosystem.config.cjs`（api + agent-worker + agent-beat 单 leader）
- [x] **6.2** Nginx：`deploy/nginx/testpilot.conf`（TLS 终止、静态、API 反代、SSE buffer-off、Gateway mTLS）
- [x] **6.3** Worker 出口 ACL 脚本：`deploy/scripts/worker-egress-acl.sh`（DOCKER-USER，DNS 固定 + 默认 REJECT）
- [x] **6.4** 部署文档：`deploy/README.md`（拓扑、证书矩阵、环境变量、启动顺序、验收清单）
- [ ] **6.5** 真机验证（需 Ubuntu 主机）：`pm2 start`/`nginx -t`/SSE 重连/Worker 非 root/恢复演练/SSRF 负向
  - 语法已静态校验（ecosystem `node --check`、ACL `bash -n`）；nginx 需目标主机 `nginx -t`

**Gate 6**：`deploy/README.md` 第 7 节验收清单全部实测通过，方可进入 Phase 7。

---

## Phase C — 黄金路径端到端（Day 6–9）

只打通一条主干，不贪多。这条线穿过 8 个模块，通了说明架构假设成立。

| # | 步骤 | 模块 | 通过判据 |
| --- | --- | --- | --- |
| 1 | 注册 → 邮箱验证 → 登录 | M01 | 拿到 refresh cookie，`/me` 可读 |
| 2 | 建组织 → 建项目 → 加成员 | M02 | 跨租户访问被拒 |
| 3 | 建环境 → 建 SecretRef → 快照 | M03 | SecretRef 值不出现在任何响应 |
| 4 | AI 生成场景 → 人工审核 | M04 | 非法模型输出无法成为可运行版本 |
| 5 | 版本审批 → 发布 | M05 | 审批后版本不可变 |
| 6 | 建套件 → 计划 → 定时触发 | M06 | 同 key 重复触发只产生一次 |
| 7 | Run 入队 → 执行 → SSE 实时 | M09 | 事件不丢不重 |
| 8 | 断言判定 → 报告快照 | M13 | 报告数字与 Run 事实一致 |
| 9 | 导出 JSON/JUnit | M13 | 下载重新鉴权 |

- [ ] **C1** 准备依赖：LLM provider（OpenAI 兼容）凭据、SMTP（Mailpit 可替）、Worker 镜像
  - 若缺 LLM key：用 stub provider 跑通链路，但在证据里明确标注「stub」
- [ ] **C2** 用 Playwright 或脚本驱动，全程录制日志（含 request id）
- [ ] **C3** 每步失败即定位到具体模块，回写 PLAN 的对应 Status 行
- [ ] **C4** 产出 `docs/<日期>-golden-path-e2e.md`：命令、日志、截图、未通项

**Gate C**：9 步全通，或每处失败都有明确归因（代码缺陷 / 契约不符 / 环境缺失三者之一）。

---

## Phase D — 证据回填（Day 10）

- [x] **D1** 新建 `docs/current-validation-status.md`（**不改历史文件**，作为唯一权威快照）
- [x] **D2** 更新 `README.md` 状态表：补上阶段 4/5，修正测试数字（298/31），指向新记录
- [x] **D3** 更新 `docs/PLAN.md`：顶部加权威指针，标注 Status 段数字已过时
- [x] **D4** 复核 `db/migrations/README.md`：008–018 由「未应用候选」改为「已验证通过」结论

**Gate D**：文档数字 = 实测数字，无一处推断。

---

## 明确不做（本轮）

| 事项 | 原因 |
| --- | --- |
| 新增功能 / 补模块 | M01–M15 已覆盖，只会扩大未验证面积 |
| Phase 6 部署配置（PM2/Nginx） | 必须等 Gate B 通过 |
| 界面视觉统一 | 7 页 typecheck 与测试均过，是「不好看」不是「不能用」，排最后 |
| 改写 10-30 验证记录 | 历史证据不可篡改，只能新增 |

---

## 已知阻塞与需要你提供的东西

| 阻塞 | 影响 | 解法 |
| --- | --- | --- |
| Docker 引擎未启动 | Phase A 全部 | 手动启动 Docker Desktop |
| 本机 PostgreSQL 18 无凭据 | Phase B 无法自动化 | 提供连接串，或自己跑 B1/B2 贴结果 |
| WSL 被安全策略拦截 | 无法用 WSL 跑 Linux 测试 | 走 Docker 容器（A4），不需要 WSL |
| LLM provider 凭据 | Phase C 第 4 步 | 提供 key，或用 stub 并标注 |
| 真实 mTLS 证书 | M08 验收 | 自签证书可用于演练，需在证据中标注非生产 |

---

## 分支策略

- 建 `validation/phase-b-migrations` 分支专门做迁移演练，避免污染 `main`
- 每个 Gate 通过后合回 `main`，并打 tag（如 `v0.9-migrations-verified`）
- Gate B 通过前，`main` 保持冻结
