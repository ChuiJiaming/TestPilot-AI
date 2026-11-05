# Sureshot 运维手册（Runbook）

> 面向接手生产环境的人。目标：出问题时不用读源码就能定位和恢复。
> 与 `deploy/README.md` 的分工：那份讲「怎么装」，这份讲「装完怎么活」。

---

## 1. 进程与端口

| 进程 | 端口 | 启动方式 | 挂了会怎样 |
| --- | --- | --- | --- |
| `testpilot-api` | 127.0.0.1:8000 | PM2 | 全站 502 |
| `testpilot-agent-worker` | — | PM2 / Celery | Run 停在 PREPARING、AI 生成不动、邮件不发 |
| `testpilot-agent-beat` | — | PM2（全局只能一个） | Outbox 不再投递，所有异步链路静默停摆 |
| `browser-gateway` | 9443 (mTLS) | 独立进程 | Run 转 ERROR，环境健康检查红 |
| `browser-worker` | 9444 (容器内) | Docker Compose | Gateway 无可用 Worker，Run 排队后转 ERROR |
| Nginx | 443 | systemd | 全站不可访问 |

**Beat 必须全局唯一。** 多节点部署时只在其中一个节点 `pm2 start testpilot-agent-beat`。

---

## 2. 健康检查

```bash
# 存活：不依赖任何外部服务，失败说明进程真的死了
curl -s http://127.0.0.1:8000/api/v1/health/live

# 就绪：postgresql / redis / object_storage 三项都 ready 才返回 200
curl -s http://127.0.0.1:8000/api/v1/health/ready | jq

# 指标：Prometheus 文本格式
curl -s http://127.0.0.1:8000/metrics | grep -E 'testpilot_http_requests_total|in_flight'
```

就绪返回 503 时看 `checks` 里哪一项不是 `ready`：

| 检查项 | 非 ready 的常见原因 |
| --- | --- |
| `postgresql` | PG 挂了、连接数打满、`TESTPILOT_DATABASE_URL` 错 |
| `redis` | Redis 挂了、密码错、网络 ACL |
| `object_storage` | S3/MinIO 不可达、bucket 不存在、凭据过期 |

---

## 3. 日志与排查

API 输出 **JSON 行日志到 stdout**，由 PM2 落到 `~/.pm2/logs/`。

每行都带 `request_id`，错误响应体里的 `trace_id` 与它一致：

```bash
# 用户报障时拿 trace_id / X-Request-ID 直接捞全链路
grep '<request-id>' ~/.pm2/logs/testpilot-api-out.log

# 最近 100 条 5xx
grep '"status_code":5' ~/.pm2/logs/testpilot-api-out.log | tail -100

# 最慢的请求（按 duration_ms 排序）
grep -o '"duration_ms":[0-9.]*' ~/.pm2/logs/testpilot-api-out.log | sort -t: -k2 -rn | head
```

**日志不含查询串和请求体**，这是刻意的：SSE 票据、验证链接都走 query string，记下来等于泄密。

关键环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TESTPILOT_LOG_LEVEL` | `INFO` | 排查时可临时调 `DEBUG`，**用完改回** |
| `TESTPILOT_LOG_FORMAT` | `json` | 设 `text` 便于人眼看 |
| `TESTPILOT_METRICS_TOKEN` | 未设置 | 设置后 `/metrics` 需 `Authorization: Bearer <token>` |

---

## 4. 备份与恢复

### 4.1 每日备份

```bash
# /etc/cron.d/sureshot-backup
SHELL=/bin/bash
TESTPILOT_DATABASE_URL=postgresql://...
TESTPILOT_BACKUP_DIR=/var/backups/sureshot
TESTPILOT_BACKUP_S3_URI=s3://acme-backups/sureshot
15 3 * * * root /srv/testpilot/deploy/scripts/backup-postgres.sh >> /var/log/sureshot-backup.log 2>&1
```

脚本会写 `.sha256` 清单（含字节数），并按 `TESTPILOT_BACKUP_RETAIN_DAYS`（默认 14）清理。

**S3 证据桶也要开版本化**，否则数据库恢复了、截图和报告对不上。

### 4.2 每周验证备份可恢复

未验证的备份等于没有备份。每周挑一份恢复到一个临时库：

```bash
./deploy/scripts/restore-postgres.sh \
  /var/backups/sureshot/sureshot-20261105T030000Z.dump \
  sureshot_restore_check \
  postgresql://postgres@127.0.0.1:5432/postgres
```

脚本会先校验 SHA-256 和归档头，再建库恢复，最后跑 `migrate --check` 验证账本。

### 4.3 真实事故恢复

1. 停写：`pm2 stop testpilot-api testpilot-agent-worker testpilot-agent-beat`
2. 备份当前坏库（`pg_dump`）以便事后取证
3. 按 4.2 恢复到正式库名
4. 起进程，验证 `/health/ready` 与一次 Run 全链路
5. 记录 RPO（丢了多少分钟数据）与 RTO（停了多久）

**没有 down 迁移。** 回滚 = 恢复快照，不是执行 SQL 反向脚本。

---

## 5. 常见故障处置

### 5.1 Run 一直停在 PREPARING

按顺序查：

```bash
# a) Beat 在跑吗？（beat 挂了 outbox 不投递）
pm2 ls | grep beat

# b) Outbox 有积压吗？
psql "$TESTPILOT_DATABASE_URL" -c \
  "select status, count(*) from outbox_records group by status order by 2 desc;"

# c) Worker 活着吗？
celery -A apps.api.app.cases.worker:celery_app inspect ping
```

`outbox_records` 里 `PENDING` 持续增长而 `published_at` 为空 → Beat 死了。
`PUBLISHING` 且 `claimed_until` 过期大量堆积 → Worker 死了，重启后租约会自动回收。

### 5.2 所有 Run 都转 ERROR

先看 Gateway/Worker，不是看 API：

```bash
curl -k --cert <api-client.crt> --key <api-client.key> https://gateway:9443/ready
docker logs --tail 200 testpilot-browser-worker
```

Worker 掉线时 Gateway `ready=false`，API 仍然接受创建 Run，随后转 ERROR——这是设计行为，不是 bug。

### 5.3 API 响应变慢、连接池打满

```bash
# 当前连接数
psql "$TESTPILOT_DATABASE_URL" -c \
  "select state, count(*) from pg_stat_activity where datname=current_database() group by 1;"
```

池大小由 `TESTPILOT_DB_POOL_SIZE`（默认 10）+ `TESTPILOT_DB_MAX_OVERFLOW`（默认 20）决定。
单条 SQL 超过 `TESTPILOT_DB_STATEMENT_TIMEOUT_MS`（默认 15000）会被 Postgres 主动中断——
日志里看到 `statement timeout` 就是慢查询，不是连接池问题。

### 5.4 邮件发不出去

验证邮件和密码重置都走 `verification_email_outbox` 表：

```bash
psql "$TESTPILOT_DATABASE_URL" -c \
  "select status, count(*), max(last_error) from verification_email_outbox group by 1;"
```

`DEAD` 且 `last_error=SMTPAuthenticationError` → 凭据错。
`CANCELLED` 且 `TOKEN_EXPIRED` → 积压超过 TTL，说明 Worker 长时间没跑。
平台管理员可在「运营 → 死信」页面查看并处理。

### 5.5 SSE 控制台不动

1. Nginx 必须对该路径 `proxy_buffering off` 且 `proxy_read_timeout` 足够长
2. 客户端必须用 `POST /sse-tickets` 换一次性 cookie，**不能带 Bearer**（直接带 Bearer 是 403，不是权限 bug）
3. 浏览器 DevTools 看 `text/event-stream` 是否有首帧

---

## 6. 密钥轮换

| 密钥 | 轮换影响 | 步骤 |
| --- | --- | --- |
| `TESTPILOT_AUTH_JWT_SIGNING_KEY` | 所有 access token 立即失效 | 先换 key → 用户重新登录（refresh token 仍有效） |
| `TESTPILOT_EMAIL_OUTBOX_ENCRYPTION_KEYS` | 旧密文要能解 | 新 key 放最前，逗号分隔，旧 key 保留至少一个 TTL 周期 |
| `TESTPILOT_SECRETREFS_JSON/FILE` | 集成调用 | 改文件即可，resolver 每次解析都重读文件，无需重启 |
| SMTP / S3 凭据 | 邮件、证据 | 改环境变量 → `pm2 restart testpilot-api testpilot-agent-worker` |

---

## 7. 扩容

- **API**：无状态，`pm2 scale testpilot-api N`。前提是 SSE 粘性由 Nginx 保证（同一连接不跨进程）。
- **Worker**：`TESTPILOT_BROWSER_MAX_SESSIONS` 决定单 Worker 并发浏览会话；横向加 Worker 需在 Gateway 注册，负载分配与会话亲和**尚未在真实环境验证**，扩容前先做一次 2 Worker 的 Run 分流测试。
- **Beat**：不扩。永远 1 个。
- **PostgreSQL**：先加索引和池，再加只读副本（报告导出可走副本）。

---

## 8. 事故响应（15 分钟版）

1. **确认影响面**：`/health/ready`、最近 5 分钟 5xx 率、Run 创建成功率
2. **止血优先**：能回滚就回滚，能重启就重启，别在现场查根因
3. **保证据**：`pm2 logs --lines 2000 > /tmp/incident-$(date +%s).log`，`pg_dump` 一份
4. **通知**：告知影响范围与预计恢复时间，不要给没有依据的时间点
5. **复盘**：写清时间线、根因、为什么没提前发现、加什么监控能提前发现

---

## 9. 上线前必须打勾

- [ ] `deploy/README.md` 第 7 节全部实测通过
- [ ] 备份 cron 已装，且**手动跑过一次恢复演练**
- [ ] `/metrics` 已接入监控，至少配 3 条告警：5xx 率、Run 创建失败率、Outbox 积压
- [ ] 日志能按 `request_id` 检索
- [ ] `TESTPILOT_METRICS_TOKEN` 已设置（若 `/metrics` 暴露在公网）
- [ ] Worker 出口 ACL 已应用（`iptables -L TESTPILOT_WORKER_EGRESS` 非空）
- [ ] 管理员账号已创建，且知道怎么在数据库里重置密码
