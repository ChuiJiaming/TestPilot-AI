# TestPilot AI — 部署与加固（Phase 6）

本目录是 Phase 6 的交付物：把 Phase 1–5 的业务进程部署到 Ubuntu Linux 上，并落实安全加固。

> **状态**：候选配置，**未在真实主机上运行验证**。所有路径、端口、证书名按参考拓扑给出；落地时替换 `testpilot.example.com` 与示例路径。不要把真实凭据、签名密钥、证书私钥提交到仓库。

---

## 1. 部署拓扑

```
                         ┌─────────────────────────────────────────────┐
  浏览器 / 客户端 ──HTTPS──▶  Nginx (TLS 终止 / 静态资源 / 反代)          │
                         │    ├─ /assets        → React SPA 构建产物      │
                         │    ├─ /api/**        → FastAPI :8000           │
                         │    ├─ /api/.../SSE   → FastAPI (buffer off)    │
                         │    └─ browser/healing→ Gateway :9443 (mTLS)    │
                         └──────────────┬────────────────────────────────┘
                                        │
        ┌───────────────────────────────┼───────────────────────────────┐
        │  PM2 业务主机 (Ubuntu)          │                                │
        │   ├─ testpilot-api        :8000   FastAPI + PostgreSQL 事务      │
        │   ├─ testpilot-agent-worker        Celery worker（有界）          │
        │   └─ testpilot-agent-beat          Celery beat（单 leader）       │
        │        │ 依赖 PostgreSQL / Redis / S3(MinIO) / SMTP / LLM        │
        └────────┼────────────────────────────────────────────────────────┘
                 │ mTLS (9443)
        ┌────────▼───────────┐          ┌──────────────────────────────────┐
        │ Browser Gateway    │──mTLS──▶│ Browser Worker (独立 Docker)      │
        │ :9443              │          │  read_only / 非 root / cap_drop  │
        └────────────────────┘          │  专用子网 / DNS 固定 / 出口 ACL   │
                                        └──────────────────────────────────┘
```

要点：

- **业务 API 与浏览器执行彻底分离**：API 永远不直连 Worker，只经过 Gateway 的 mTLS 跳。
- **单 Beat leader**：`testpilot-agent-beat` 全集群只能有一个。消费者全部幂等，重复 beat 只浪费资源、不会损坏状态，但仍是部署纪律。
- **浏览器出口不经 Nginx**：Worker 通过专用、DNS 固定的子网出网，受 Docker 层 ACL 约束。

---

## 2. 目录与文件

| 文件 | 用途 |
| --- | --- |
| `pm2/ecosystem.config.cjs` | API + Agent Worker + Beat 三个进程 |
| `nginx/testpilot.conf` | TLS 终止、静态资源、API 反代、SSE、Gateway mTLS |
| `apps/browser_worker/compose.yaml` | Worker 的容器定义（安全基线已内置） |
| `apps/browser_worker/Dockerfile` | Worker 镜像（非 root、只读、Playwright 全浏览器） |

---

## 3. 前置依赖（需在主机上就绪）

| 依赖 | 版本 | 说明 |
| --- | --- | --- |
| Ubuntu Linux | 22.04/24.04 LTS | PM2 业务主机 |
| Node.js | 22–24 | Gateway / Worker |
| Python | 3.11+ | API / Celery |
| PostgreSQL | 15+ | 业务状态、事件、Outbox、租约 |
| Redis | 7 | Celery broker / 结果 |
| MinIO 或 S3 | — | 私有 PNG 证据存储（`TESTPILOT_S3_BUCKET`） |
| SMTP | — | 验证邮件（生产用真实 provider；开发可用 Mailpit） |
| LLM provider | OpenAI 兼容 | 生成 / Planner（`TESTPILOT_LLM_*`） |
| Docker | — | Worker 隔离运行 |

---

## 4. 安装与启动

```bash
# 1) 依赖与代码
sudo apt-get update && sudo apt-get install -y nginx redis-server
# PostgreSQL 按发行版安装；Node 22 用 nvm 或 apt 源；Python 3.11+ 建 venv
git clone <repo> /srv/testpilot && cd /srv/testpilot

# 2) Python 环境
python3 -m venv .venv && source .venv/bin/activate
pip install -e "apps/api"          # 生产依赖（不含 test extra）
pip install "celery[redis]" gunicorn

# 3) 前端构建
npm --prefix apps/web ci && npm --prefix apps/web run build
# 产物在 apps/web/dist

# 4) 数据库迁移（先核对 db/migrations/README.md 的顺序与账本要求）
export TESTPILOT_DATABASE_URL="postgresql://user:pass@127.0.0.1:5432/testpilot"
python -m apps.api.scripts.migrate --apply
python -m apps.api.scripts.migrate --check   # 账本与校验和必须全绿

# 5) PM2（把真实 TESTPILOT_* 环境变量注入启动前）
pm2 start deploy/pm2/ecosystem.config.cjs
pm2 save
pm2 ls          # 应看到 testpilot-api / testpilot-agent-worker / testpilot-agent-beat 三者 online

# 6) Nginx（校验后重载）
sudo cp deploy/nginx/testpilot.conf /etc/nginx/conf.d/testpilot.conf
sudo nginx -t
sudo systemctl reload nginx
```

> PM2 启动前必须导出全部 `TESTPILOT_*` 变量（见第 6 节）。推荐用 systemd 环境文件或 `pm2 start ... --update-env` 配一份受保护的 env 文件，不要把密钥写进 `ecosystem.config.cjs`。

---

## 5. 环境变量清单

完整变量以代码为准（`grep -rhoE "TESTPILOT_[A-Z_]+" apps/ | sort -u`）。关键项：

| 变量 | 用途 | 敏感度 |
| --- | --- | --- |
| `TESTPILOT_DATABASE_URL` | PostgreSQL DSN | 含密码 |
| `TESTPILOT_REDIS_URL` / `TESTPILOT_CELERY_BROKER_URL` | Redis / broker | 含密码 |
| `TESTPILOT_AUTH_JWT_SIGNING_KEY` | 会话令牌签名 | 高 |
| `TESTPILOT_EMAIL_OUTBOX_ENCRYPTION_KEYS` | 验证邮件 Outbox 加密 | 高 |
| `TESTPILOT_SMTP_HOST/PORT/USERNAME/PASSWORD/SENDER/STARTTLS` | 邮件 | 含密码 |
| `TESTPILOT_S3_BUCKET` / `TESTPILOT_S3_ENDPOINT_URL` + AWS 凭据 | 对象存储 | 含密钥 |
| `TESTPILOT_LLM_API_KEY` / `TESTPILOT_LLM_BASE_URL` / `TESTPILOT_LLM_PRICING_JSON` | LLM | 含密钥 |
| `TESTPILOT_BROWSER_JOB_TOKEN_SECRET` | 浏览器任务令牌（API 与 Worker 共享） | 高 |
| `TESTPILOT_AUTH_ALLOWED_ORIGINS` | CORS 白名单 | — |

---

## 6. mTLS 证书矩阵（Gateway / Worker）

证书名对应 `compose.yaml` 与 Gateway `server.mjs` 的挂载路径。全部由内部 CA 签发，**生产前替换为正式 CA**：

| 证书 | 挂载到 | 用途 |
| --- | --- | --- |
| `gateway-server.crt/key` | Gateway | Gateway 对 API/Worker 的服务端身份 |
| `api-client.crt/key` | API（Nginx 反代时） | API 作为 Gateway 的 mTLS 客户端 |
| `gateway-worker.crt/key` | Gateway | Gateway 作为 Worker 的 mTLS 客户端 |
| `worker.crt/key` | Worker | Worker 服务端身份（CN = Worker ID） |
| `worker-client.crt/key` | Worker | Worker 注册客户端身份 |
| `gateway-server-ca.crt` | API/Worker | 验证 Gateway 服务端 |
| `gateway-client-ca.crt` | Gateway | 验证 API 客户端 |
| `gateway-worker-ca.crt` / `worker-ca.crt` | 双方 | 验证 Worker / Gateway 客户端 |

> 生产环境禁止提交任何 `.key` 到仓库。用 `deploy/tls/` 之外的保密目录，通过 volume 只读挂载。

---

## 7. 验收清单（Phase 6 门禁）

按 `docs/PLAN.md` Phase 6 的 Acceptance 逐项核对，全部需在真实主机实测：

- [ ] `pm2 start` / `pm2 save` / `pm2 ls` 正常，三进程 online
- [ ] 主机重启后 `pm2 resurrect` 恢复三进程
- [ ] **只有一个 Beat leader** 活跃（多机部署时确认仅一节点跑 beat）
- [ ] `sudo nginx -t` 通过
- [ ] SSE 断线自动重连、不丢不重（`curl -N` 观察事件流）
- [ ] Worker 以非 root 运行、`read_only`、`cap_drop ALL`、专用子网
- [ ] PostgreSQL 与对象存储能一起恢复，且满足 RPO/RTO
- [ ] SSRF 负向用例通过（DNS 重绑定、私网段、IPv6、云元数据、重定向、子资源）
- [ ] 证据链路无明文密钥泄漏（响应/事件/日志/截图/报告）

**以上任何一项未通过，不得进入 Phase 7 发布门禁。**

---

## 8. 已知边界

- 参考拓扑假设单机 PM2 业务主机；水平扩展时 beat 的「单 leader」纪律需要额外编排（如只在某节点启动 beat）。
- `ecosystem.config.cjs` 未把 Gateway 纳入 PM2，因为 Gateway 的 TLS 身份应独立于 PM2 生命周期管理；如需统一，可另加一个进程项并挂载证书。
- 011 邮件 Outbox 的 `TESTPILOT_EMAIL_OUTBOX_ENCRYPTION_KEYS` 运行期行为在迁移验证中未配置，生产前必须补齐并验证。
- S3 的 AWS 凭据（`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`）由 boto3 标准链读取，`TESTPILOT_S3_ENDPOINT_URL` 指向 MinIO 时需一并设置 region。
