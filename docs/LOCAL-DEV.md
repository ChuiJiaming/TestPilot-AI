# 本地开发运行指南（Windows 单机）

结论先行：**可以在这台机器上跑起来并做真实测试。** 本机已具备 PostgreSQL 18；
Redis 没有安装，由 `dev_server` 自动换成进程内 fakeredis 兜底；S3 未配置，
截图/报告等对象存储功能会显示「未就绪」，其余功能不受影响。

## 一次性状态（已完成）

- `testpilot` 库已应用全部迁移 001–019（`schema_migrations` 19 条）。
  应用前的备份在 `.local-backup/testpilot-before-019-*.sql`。
- `.env.local` 已创建（被 `.gitignore` 覆盖，不会进仓库），内含本地 JWT 签名
  密钥与 Outbox Fernet 密钥。换机器请重新生成这两个密钥。
- venv 额外安装了 `fakeredis[lua]`（限流器走 `EVAL`，不带 Lua 支持会 500）。

## 启动

```bash
# 终端 1：后端（自动读 .env.local；没有 Redis 时打印 fallback 提示）
./.venv/Scripts/python.exe -m apps.api.scripts.dev_server --port 8000

# 终端 2：前端（Vite 把 /api 代理到 127.0.0.1:8000）
cd apps/web && npm run dev
```

打开 http://localhost:5173 ，注册账号（密码 ≥ 12 位）。本地没有 SMTP，
验证邮件停在 Outbox 里，不阻塞注册；用冒烟脚本可以直接走完整个闭环：

```bash
PYTHONPATH=. ./.venv/Scripts/python.exe -m apps.api.scripts.smoke_local
```

它会验证：liveness → 注册 → 从 Outbox 取令牌并解密 → 验证邮箱 → 登录 →
带 JWT 调用受保护接口。六项全 PASS 即链路正常。

## 已知限制（本地）

| 功能 | 状态 | 原因 |
|---|---|---|
| 注册 / 验证 / 登录 / 会话 | 可用 | PostgreSQL + fakeredis |
| 验证 / 重置邮件实际投递 | 不可用 | 无 SMTP，邮件积压在 Outbox |
| 截图 / 运行报告 / 对象存储 | 未就绪 | 未配置 S3/MinIO，`/health/ready` 会如实报 `object_storage: unavailable` |
| 真实执行测试用例 | 未验证 | 需要 browser_worker + browser_gateway + Celery worker 一并启动，且 AI 生成依赖真实 LLM Key |
| 多进程一致性 | 不适用 | fakeredis 是进程内状态，只能跑单实例，生产必须用真 Redis |

## 运行测试

```bash
# 全量（包含此前一直被跳过的 PostgreSQL 集成测试）
TESTPILOT_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/testpilot \
PYTHONPATH=. ./.venv/Scripts/python.exe -m pytest tests -q
```

集成测试限制了库名必须是 `testpilot` 且 host 必须是本机回环
（见 `tests/test_postgresql_phase1_integration.py:43`），不要指向远程库。
