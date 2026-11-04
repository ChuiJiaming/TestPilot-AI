# 真实浏览器链路验证（Phase C 完整版）

> 日期：2026-11-04 · 环境：Windows 10 + Docker Desktop（PG 18 / Redis 7 / Mailpit）
> 结论：**首次打通 API → mTLS Gateway → mTLS Browser Worker → Playwright → 公网靶场 的完整链路**，
> 真实浏览器执行与确定性断言首次拿到端到端证据；同时暴露并修复 2 个只在真实依赖下才会出现的缺陷。

## 1. 为什么这一轮必须做

在此之前，`execute_admitted_run` 一进入浏览器执行就抛 `BROWSER_GATEWAY_NOT_CONFIGURED`，
所有 Run 只能走到 `ERROR`。也就是说：认证、租户、用例、调度、报告都验证过，
唯独**平台最核心的能力——在真实浏览器里跑测试并给出判定——一次都没跑过**。

代码早已写完（`apps/browser_gateway`、`apps/browser_worker` 共 1264 行），
缺的是证书、环境接线和一个可复现的验证过程。

## 2. 靶场选择的硬约束（踩过才知道）

Worker 的出口策略在 `browser_sessions.mjs:105` 只放行 `ipaddr` 判定为 `unicast` 的地址，
并且禁止 IP 字面量与非 80/443 端口。因此：

- **本机 `127.0.0.1` 靶场走不通**（loopback 被拒），这不是 bug，是 SSRF 防线；
- 靶场必须是公网域名，且只允许 80/443；
- 反向影响：**任何本地开发想跑真实用例，都必须准备一个公网可访问的靶场**。

本轮选 `https://example.com`，域名白名单精确到 `example.com`（不支持通配）。

## 3. 证书矩阵（本轮新增）

`deploy/scripts/dev-mtls-certs.sh`（DEV ONLY，输出到 `.certs/`，已加入 `.gitignore`）：

| 文件 | 用途 | 签发者 |
| --- | --- | --- |
| `ca-api.crt/key` | API 侧信任根 | 自签 |
| `ca-worker.crt/key` | Worker 侧信任根 | 自签 |
| `api-client.crt/key` | API → Gateway 客户端证书 | ca-api |
| `gateway-server.crt/key` | Gateway TLS 服务端（SAN: localhost, 127.0.0.1） | ca-api |
| `gateway-worker-client.crt/key` | Gateway → Worker 客户端证书 | ca-worker |
| `worker-server.crt/key` | Worker TLS 服务端，**CN = browser-worker-1**，兼作注册客户端证书 | ca-worker |

要点：Worker 注册证书的 **CN 必须等于** `TESTPILOT_WORKER_INSTANCE` 且是 Gateway
`TESTPILOT_GATEWAY_WORKER_POOL_JSON` 的一个 key，否则 Gateway 直接 403 `WORKER_IDENTITY_MISMATCH`。
脚本同时产出 `.certs/env.sh`，三个进程共用一套 `TESTPILOT_*`，避免各写一遍写错。

## 4. 启动顺序（实测）

```bash
# 1) 证书
bash deploy/scripts/dev-mtls-certs.sh --force

# 2) 网关 + Worker（两个终端）
source .certs/env.sh && node apps/browser_gateway/server.mjs
source .certs/env.sh && node apps/browser_worker/server.mjs

# 3) 后端三件套
source .env.e2e && source .certs/env.sh && .venv/Scripts/python.exe -m uvicorn apps.api.app.main:app --port 18000
source .env.e2e && source .certs/env.sh && .venv/Scripts/python.exe -m celery -A apps.api.app.cases.worker:celery_app worker --pool=solo -l INFO
source .env.e2e && source .certs/env.sh && .venv/Scripts/python.exe -m celery -A apps.api.app.cases.worker:celery_app beat -l INFO
```

就绪判据（mTLS 强制）：

```bash
# 带客户端证书 → 200 且 workers[0].ready=true；不带 → TLSV13_ALERT_CERTIFICATE_REQUIRED
GET https://127.0.0.1:9443/internal/health/ready
```

> Windows 自带的 `curl.exe` 是 Schannel 后端，无法导入 PEM 私钥，会报
> `Failed to import cert file`。**用 httpx 验证，不要用系统 curl 判断 mTLS 是否正常。**

## 5. 实测结果（`全部 PASS`，无 SKIP）

驱动脚本：`.e2e-logs/browser_chain.py`（结果落 `.e2e-logs/browser-chain.json`）

| 检查项 | 结果 |
| --- | --- |
| Worker 注册 + 15s 心跳 + Gateway 汇聚就绪 | 通过（`ready=true, capacity=2`） |
| 无客户端证书访问 Gateway | 拒绝（`alert certificate required`） |
| 环境健康检查（API → 网关 → Worker → 目标站） | `HEALTHY`，`http=206` |
| PASS 用例（断言 `<title>` 含 `Example Domain`） | Run `61429b21` → **PASSED**（11.3s） |
| FAIL 用例（断言正文含不存在文案） | Run `b936931f` → **FAILED**（9.1s） |
| 每 Run 步骤记录 | 3 条（1 action + 2 assertion），状态与判定一致 |
| 截图证据 | 每 Run 1 张，1280×720，`redacted=true`，`color_scheme=light` |
| 截图字节回读 | 38229 B，`sha256=8010e303df0f7d74…`，与 `test_artifacts.sha256` **一致** |
| Worker 单测 / Gateway 单测 | 15 passed / 7 passed（含新增 lookup 用例） |

汇总行：**43 PASS / 0 FAIL**。

## 6. 本轮修掉的缺陷（都是"单测结构上看不到"的那类）

### 6.1 httpx 0.28 的 mTLS 语义变化 —— 网关调用全挂

- `POST /test-runs` 之后，Celery 侧调用 Gateway 直接抛
  `TypeError: post() got an unexpected keyword argument 'cert'`
  （`apps/api/app/runs/browser.py`、`apps/api/app/environments/health_check.py`）。
- 更隐蔽的一层：把 `cert=` 挪到 `httpx.Client(...)` 上**不报错但被忽略**，
  表现为 `TLSV13_ALERT_CERTIFICATE_REQUIRED` —— 客户端证书根本没发出去。
- 修复：新增 `apps/api/app/core/mtls.py`，用 `ssl.SSLContext` 显式
  `load_cert_chain`，`verify=ctx` 传入。0.27 / 0.28 行为一致。
- 为什么之前没发现：单测不触碰真实 TLS 握手，`pyproject.toml` 只约束 `httpx>=0.27,<1`，
  全新环境会装到 0.28。

### 6.2 Node 22 Happy Eyeballs 打穿自定义 lookup —— 健康检查恒定 UNHEALTHY

- 现象：`POST /environments/{id}/health-check` 返回 `UNHEALTHY / TARGET_CONNECTION_FAILED`，
  但同一域名用真实浏览器跑却是 `PASSED`。
- 根因：`server.mjs` 的探针用 `lookup: (_h,_o,cb) => cb(null, ip, family)`。
  Node 20+ 默认开启 `autoSelectFamily`，会以 `{ all: true }` 调用 lookup，
  此时回调必须返回地址数组，返回单个地址直接抛 `ERR_INVALID_IP_ADDRESS`（实测复现）。
- 修复：`worker_dns.mjs` 新增 `pinnedLookup(entry)`，兼容 `all: true` 与单地址两种调用，
  `server.mjs`（健康探针）与 `browser_sessions.mjs`（明文 HTTP 代理）统一改用它；
  新增 `worker_dns.test.mjs` 两个用例锁定行为。
- 修复后同一接口返回 `HEALTHY / http=206`。

## 7. 新发现的产品/契约缺口（未修，需决策）

| # | 发现 | 影响 |
| --- | --- | --- |
| 1 | **Worker 只实现截图证据**，`trace` / `har` 只存在于 `artifact_type` 枚举，没有任何生产路径 | README 里"截图、trace、HAR"属超额承诺，已在本轮修正表述 |
| 2 | `ArtifactView` 不返回 `sha256`（库里存了） | 接入方无法自行做字节级校验，只能依赖服务端 |
| 3 | 邀请邮件没接入通知管道：`invitations` 建了、`token` 只在创建响应里返回一次，Mailpit 收不到邀请邮件 | 被邀请人只能靠前端把 token 交给用户，邮件链路是断的 |
| 4 | 内容断言受浏览器 locale 影响：同一页面在 `zh-CN` 下正文为中文，`<title>` 不变 | 用例里的文案断言默认依赖执行机语言；本轮 PASS 用例改用 `<title>` 锚点规避 |
| 5 | 验证邮件首投可能失败重试：本轮有一次 `attempts=3`、约 35s 才投递成功 | 端到端脚本的邮件轮询窗口需 ≥ 60s，否则会误判 |

## 8. 仍未验证（不得视为通过）

- Worker 出口 ACL（`deploy/scripts/worker-egress-acl.sh` 仍是 CANDIDATE，未 apply）
- 多 Worker 的最小负载分配与会话亲和（本轮只有 1 个 Worker，亲和是平凡成立）
- 真实生产证书的签发/轮换（本轮是 30 天自签 DEV 证书）
- 定位器恢复（M10）在真实浏览器里的完整回放（代码在，未跑真实场景）
- 并发 Run、SSE 断线重连、PostgreSQL 15 兼容、Phase 6 真机部署验收

## 9. 复现命令

```bash
bash deploy/scripts/dev-mtls-certs.sh --force
# 起 5 个进程后：
.venv/Scripts/python.exe .e2e-logs/browser_chain.py
```
