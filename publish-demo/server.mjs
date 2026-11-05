// Sureshot 演示站服务器：托管前端静态产物 + 演示数据 API（单端口）
// 仅包含只读演示数据，不含任何真实业务数据或密钥。
import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { extname, join, normalize, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const DIST = join(dirname(fileURLToPath(import.meta.url)), "webroot");
const PORT = Number(process.env.PORT || 8801);
const HOST = "0.0.0.0";

const now = Date.now();
const iso = (offsetMinutes) => new Date(now - offsetMinutes * 60_000).toISOString();

const user = { id: "9f1c2a34-5b6d-4e7f-8a90-b1c2d3e4f506", email: "reese@hoshino.dev", display_name: "崔海东", status: "active" };
const authResult = { access_token: "demo-access-token", expires_in: 900, user };
const org = { id: "org-0001-hoshino", name: "星野科技", slug: "hoshino", role: "org_admin" };
const projectMain = { id: "prj-0001-ecom", organization_id: org.id, name: "电商中台回归测试", slug: "ecom-regression", created_by: user.id, role: "project_admin" };
const projectPay = { id: "prj-0002-pay", organization_id: org.id, name: "支付网关冒烟", slug: "pay-gateway-smoke", created_by: user.id, role: null };
const envs = [
  { id: "env-0001-staging", project_id: projectMain.id, name: "Staging 环境", base_url: "https://staging.shop.hoshino.dev", secret_ref_id: "sref-0001-vault", network_policy: { allowed_domains: ["staging.shop.hoshino.dev"], deny_private_network: true }, created_at: iso(60 * 24 * 30) },
  { id: "env-0002-preview", project_id: projectMain.id, name: "预发环境", base_url: "https://pre.shop.hoshino.dev", secret_ref_id: null, network_policy: { allowed_domains: ["pre.shop.hoshino.dev"], deny_private_network: true }, created_at: iso(60 * 24 * 12) },
];
const cases = [
  { id: "tc-0001-login", project_id: projectMain.id, title: "登录流程验证（密码 + 验证码）", priority: "P0", status: "active", published_version_id: "tcv-0003", created_at: iso(60 * 24 * 40) },
  { id: "tc-0002-cart", project_id: projectMain.id, title: "购物车结算链路（含优惠券叠加）", priority: "P0", status: "active", published_version_id: "tcv-0006", created_at: iso(60 * 24 * 33) },
  { id: "tc-0003-search", project_id: projectMain.id, title: "商品搜索联想与纠错", priority: "P1", status: "active", published_version_id: "tcv-0008", created_at: iso(60 * 24 * 21) },
  { id: "tc-0004-points", project_id: projectMain.id, title: "会员积分兑换商品", priority: "P2", status: "draft", published_version_id: null, created_at: iso(60 * 24 * 3) },
];
const versions = (caseId) => [
  { id: `tcv-${caseId}-v2`, case_id: caseId, version: 2, version_name: "补齐失败分支断言", review_status: "APPROVED", dsl: { schema_version: 2, case_id: caseId, title: cases[0].title, steps: [], assertions: [] }, changelog: "补齐登录失败分支断言，修正验证码等待策略", created_at: iso(60 * 24 * 8) },
  { id: `tcv-${caseId}-v1`, case_id: caseId, version: 1, version_name: null, review_status: "SUPERSEDED", dsl: { schema_version: 2, case_id: caseId, title: cases[0].title, steps: [], assertions: [] }, changelog: "初始版本", created_at: iso(60 * 24 * 40) },
];
const suites = [
  { id: "suite-0001-core", project_id: projectMain.id, name: "核心交易回归套件", description: "登录、购物车、结算主链路", created_at: iso(60 * 24 * 20) },
  { id: "suite-0002-smoke", project_id: projectMain.id, name: "每日冒烟", description: "P0 用例 10 分钟冒烟", created_at: iso(60 * 24 * 10) },
];
const suiteCases = { cases: [{ case_id: cases[0].id, sequence: 1, title: cases[0].title }, { case_id: cases[1].id, sequence: 2, title: cases[1].title }] };
const plans = [
  { id: "plan-0001-nightly", project_id: projectMain.id, name: "每晚全量回归", suite_id: suites[0].id, policy: { concurrency_mode: "serial", max_concurrent_runs: 1 }, created_at: iso(60 * 24 * 15) },
];
const schedules = [
  { id: "sch-0001-nightly", plan_id: plans[0].id, cron_expression: "0 2 * * *", timezone: "Asia/Shanghai", environment_id: envs[0].id, status: "ACTIVE", created_at: iso(60 * 24 * 15) },
];
const runs = [
  { id: "run-a1b2c3d4", project_id: projectMain.id, environment_id: envs[0].id, environment_snapshot_id: "snap-0001", parent_run_id: null, status: "RUNNING", branch: "main", commit_sha: "3fa85c1", trigger_source: "schedule", browser_matrix: ["chromium"], created_at: iso(6), started_at: iso(5), finished_at: null, latest_event_id: 142, event_replay_floor: 0 },
  { id: "run-b936931f", project_id: projectMain.id, environment_id: envs[0].id, environment_snapshot_id: "snap-0001", parent_run_id: null, status: "FAILED", branch: "feat/checkout-v2", commit_sha: "e51d07a", trigger_source: "ci", browser_matrix: ["chromium", "firefox"], created_at: iso(90), started_at: iso(89), finished_at: iso(88), latest_event_id: 87, event_replay_floor: 0 },
  { id: "run-61429b21", project_id: projectMain.id, environment_id: envs[0].id, environment_snapshot_id: "snap-0001", parent_run_id: null, status: "PASSED", branch: "main", commit_sha: "3fa85c1", trigger_source: "manual", browser_matrix: ["chromium"], created_at: iso(60 * 25), started_at: iso(60 * 25 - 1), finished_at: iso(60 * 25 - 2), latest_event_id: 64, event_replay_floor: 0 },
  { id: "run-0d4e5f60", project_id: projectMain.id, environment_id: envs[0].id, environment_snapshot_id: "snap-0002", parent_run_id: null, status: "PASSED", branch: "main", commit_sha: "3fa85c1", trigger_source: "ci", browser_matrix: ["chromium"], created_at: iso(60 * 49), started_at: iso(60 * 49 - 1), finished_at: iso(60 * 49 - 2), latest_event_id: 58, event_replay_floor: 0 },
];
const steps = () => ({
  items: [
    { id: "st-001", run_case_id: "rc-001", action_id: "act-open-home", sequence: 1, action_type: "open", status: "PASSED", replay_policy: "FAIL_FAST", browser: "chromium", started_at: iso(5), finished_at: iso(4.9) },
    { id: "st-002", run_case_id: "rc-001", action_id: "act-fill-account", sequence: 2, action_type: "fill", status: "PASSED", replay_policy: "FAIL_FAST", browser: "chromium", started_at: iso(4.9), finished_at: iso(4.8) },
    { id: "st-003", run_case_id: "rc-001", action_id: "act-click-submit", sequence: 3, action_type: "click", status: "PASSED", replay_policy: "FAIL_FAST", browser: "chromium", started_at: iso(4.8), finished_at: iso(4.7) },
    { id: "st-004", run_case_id: "rc-001", action_id: "asr-dashboard-visible", sequence: 4, action_type: "assert", status: "PASSED", replay_policy: "FAIL_FAST", browser: "chromium", started_at: iso(4.7), finished_at: iso(4.6) },
    { id: "st-005", run_case_id: "rc-001", action_id: "asr-order-count", sequence: 5, action_type: "assert", status: "PASSED", replay_policy: "FAIL_FAST", browser: "chromium", started_at: iso(4.6), finished_at: iso(4.5) },
  ],
});
const report = {
  id: "rep-0001-run61429b21", run_id: "run-61429b21", branch: "main", commit_sha: "3fa85c1",
  summary: { run: { status: "PASSED", branch: "main", commit_sha: "3fa85c1" }, summary: { counts: { total: 12, passed: 11, failed: 0, skipped: 1 } } },
  artifact_references: [
    { id: "art-0001-html", artifact_type: "report_html", content_type: "text/html", status: "SUCCEEDED", size_bytes: 204833 },
    { id: "art-0002-pdf", artifact_type: "report_pdf", content_type: "application/pdf", status: "SUCCEEDED", size_bytes: 418120 },
    { id: "art-0003-json", artifact_type: "report_json", content_type: "application/json", status: "SUCCEEDED", size_bytes: 15874 },
    { id: "art-0004-junit", artifact_type: "report_junit", content_type: "application/xml", status: "SUCCEEDED", size_bytes: 9642 },
    { id: "art-0005-shot", artifact_type: "screenshot", content_type: "image/png", status: "SUCCEEDED", size_bytes: 38229 },
  ],
  next_cursor: null,
};
const sessions = [
  { id: "sess-0001-chrome", device_label: "Web browser · Chrome 140 / Windows 11", created_at: iso(320), last_used_at: iso(1), revoked_at: null },
  { id: "sess-0002-edge", device_label: "Web browser · Edge 139 / Windows 11", created_at: iso(60 * 26 * 3), last_used_at: iso(60 * 30), revoked_at: null },
  { id: "sess-0003-old", device_label: "Web browser · Safari 18 / macOS", created_at: iso(60 * 24 * 9), last_used_at: iso(60 * 24 * 2), revoked_at: iso(60 * 24 * 2) },
];
const secretRefs = [
  { id: "sref-0001-vault", organization_id: org.id, provider: "vault", external_key: "staging/admin-account", rotated_at: iso(60 * 24 * 5), created_at: iso(60 * 24 * 30), active_version: 3 },
  { id: "sref-0002-vault", organization_id: org.id, provider: "vault", external_key: "staging/payment-secret", rotated_at: iso(60 * 24 * 11), created_at: iso(60 * 24 * 25), active_version: 2 },
];

const json = (res, code, body) => {
  const data = JSON.stringify(body);
  res.writeHead(code, { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(data) });
  res.end(data);
};

const MIME = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".json": "application/json", ".woff2": "font/woff2" };

async function serveStatic(res, pathname) {
  let filePath = join(DIST, normalize(pathname).replace(/^([.][.][/\\])+/, ""));
  try {
    const info = await stat(filePath);
    if (info.isDirectory()) filePath = join(filePath, "index.html");
  } catch {
    filePath = join(DIST, "index.html"); // SPA fallback
  }
  try {
    const data = await readFile(filePath);
    res.writeHead(200, { "Content-Type": MIME[extname(filePath)] ?? "application/octet-stream", "Cache-Control": filePath.includes(`${DIST.slice(0, 3)}assets`) || /[/\\]assets[/\\]/.test(filePath) ? "public, max-age=31536000, immutable" : "no-cache" });
    res.end(data);
  } catch {
    res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    res.end("Not Found");
  }
}

createServer((req, res) => {
  const url = new URL(req.url, "http://localhost");
  const p = url.pathname;

  if (!p.startsWith("/api/")) return void serveStatic(res, p === "/" ? "/index.html" : p);

  if (p === "/api/v1/auth/refresh") return json(res, 200, authResult);
  if (p === "/api/v1/auth/sessions") return json(res, 200, { sessions });
  if (p === "/api/v1/health/ready") return json(res, 200, { status: "ready", checks: { database: "ok", redis: "ok", smtp: "ok", object_storage: "ok" } });
  if (p === "/api/v1/organizations") return json(res, 200, { items: [org], next_cursor: null });
  if (p === "/api/v1/projects") return json(res, 200, [projectMain, projectPay]);
  if (p === "/api/v1/secret-refs") return json(res, 200, secretRefs);
  if (/\/api\/v1\/projects\/[^/]+\/environments$/.test(p)) return json(res, 200, { items: envs, next_cursor: null });
  if (/\/api\/v1\/projects\/[^/]+\/test-cases$/.test(p)) return json(res, 200, { items: cases, next_cursor: null });
  if (/\/api\/v1\/projects\/[^/]+\/suites$/.test(p)) return json(res, 200, { items: suites });
  if (/\/api\/v1\/projects\/[^/]+\/plans$/.test(p)) return json(res, 200, { items: plans });
  if (/\/api\/v1\/projects\/[^/]+\/test-runs$/.test(p)) return json(res, 200, { items: runs, next_cursor: null });
  if (/\/api\/v1\/test-cases\/[^/]+\/versions$/.test(p)) return json(res, 200, { items: versions(p.split("/")[4]) });
  if (/\/api\/v1\/suites\/[^/]+\/cases$/.test(p)) return json(res, 200, suiteCases);
  if (/\/api\/v1\/plans\/[^/]+\/schedules$/.test(p)) return json(res, 200, { items: schedules });
  if (/\/api\/v1\/test-runs\/[^/]+\/steps$/.test(p)) return json(res, 200, steps(p.split("/")[4]));
  if (/\/api\/v1\/test-runs\/[^/]+\/report$/.test(p)) return json(res, 200, report);
  if (/\/api\/v1\/test-runs\/[^/]+$/.test(p)) return json(res, 200, runs.find((r) => r.id === p.split("/")[4]) ?? runs[0]);
  if (p === "/api/v1/sse-tickets") return json(res, 200, { run_id: "run-a1b2c3d4", expires_in: 300 });
  if (/\/api\/v1\/test-runs\/[^/]+\/events$/.test(p)) {
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
    res.write(": keepalive\n\n");
    const timer = setInterval(() => res.write(": keepalive\n\n"), 5000);
    req.on("close", () => clearInterval(timer));
    return;
  }
  if (/\/api\/v1\/organizations\/[^/]+\/quotas$/.test(p)) return json(res, 200, { monthly_token_limit: 2000000, monthly_tokens_committed: 743820, monthly_tokens_reserved: 120000, monthly_tokens_remaining: 1136180, period_month: "2026-11", max_concurrent_runs: 4, quota_exhausted: false });
  if (/\/api\/v1\/organizations\/[^/]+\/usage$/.test(p)) return json(res, 200, { items: [ { metric_name: "模型调用次数", value: 1284, unit: "次" }, { metric_name: "输入 tokens", value: 512600, unit: "tokens" }, { metric_name: "输出 tokens", value: 231220, unit: "tokens" }, { metric_name: "浏览器执行时长", value: 96, unit: "分钟" } ] });
  if (p === "/api/v1/admin/model-config" && req.method === "GET") return json(res, 200, {
    source: "database",
    base_url: "https://open.bigmodel.cn/api/paas/v4",
    api_key_env: "TESTPILOT_LLM_API_KEY",
    api_key_configured: true,
    planner_model: "glm-4-plus",
    run_agent_model: "glm-4-flash",
    pricing: [
      { model: "glm-4-flash", input_usd_per_million: 0.014, output_usd_per_million: 0.014 },
      { model: "glm-4-plus", input_usd_per_million: 0.69, output_usd_per_million: 0.69 },
    ],
    ready: true,
    issues: [],
    updated_at: "2026-11-02T10:20:00Z",
  });
  if (p === "/api/v1/admin/model-config/test" && req.method === "POST") return json(res, 200, {
    ok: true, model: "glm-4-flash", latency_ms: 412, error_code: "", message: "连接成功。",
  });
  if (p === "/api/v1/model-options" && req.method === "GET") return json(res, 200, {
    items: ["glm-4-flash", "glm-4-plus"], default: "glm-4-plus",
  });
  if (p === "/api/v1/system-settings" && req.method === "GET") return json(res, 200, {
    items: [
      { key: "maintenance_mode", value_hash: "sha256:3f7c1a92e4b8d056", updated_at: "2026-10-28T09:12:00Z" },
      { key: "max_runs_per_day", value_hash: "sha256:ab41d0f6c9237e18", updated_at: "2026-11-01T15:40:00Z" },
    ],
    pending_changes: [
      { id: "chg-9001", key: "maintenance_mode", status: "PENDING", previous_value_hash: "sha256:0a11c4d2e8b7f609", proposed_value_hash: "sha256:3f7c1a92e4b8d056", requested_by: "崔海东", approved_by: null, reason: "发布窗口期暂停外部流量", created_at: "2026-11-04T08:30:00Z", decided_at: null },
    ],
  });
  if (p === "/api/v1/system-settings" && req.method === "PUT") return json(res, 200, { id: "chg-" + Date.now(), key: "maintenance_mode", status: "PENDING", previous_value_hash: "sha256:0a11c4d2e8b7f609", proposed_value_hash: "sha256:6b52e7d1a9f480c3", requested_by: "崔海东", approved_by: null, reason: "演示申请", created_at: new Date().toISOString(), decided_at: null });
  if (/^\/api\/v1\/admin\/system-setting-changes\/[^/]+\/decision$/.test(p)) { let body = ""; req.on("data", (c) => { body += c; }); req.on("end", () => { let decision = "APPROVED"; try { decision = JSON.parse(body).decision === "reject" ? "REJECTED" : "APPROVED"; } catch {} return json(res, 200, { id: "chg-9001", key: "maintenance_mode", status: decision, previous_value_hash: "sha256:0a11c4d2e8b7f609", proposed_value_hash: "sha256:3f7c1a92e4b8d056", requested_by: "崔海东", approved_by: "崔海东", reason: "发布窗口期暂停外部流量", created_at: "2026-11-04T08:30:00Z", decided_at: new Date().toISOString() }); }); return; }
  if (req.method === "GET" && p.startsWith("/api/v1/")) return json(res, 200, { items: [], next_cursor: null });
  return json(res, 404, { detail: "demo: not implemented" });
}).listen(PORT, HOST, () => console.log(`Sureshot demo listening on http://${HOST}:${PORT}`));
