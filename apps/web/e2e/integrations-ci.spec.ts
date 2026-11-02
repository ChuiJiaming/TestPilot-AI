import { expect, test } from "@playwright/test";

const organization = { id: "org-e2e", name: "E2E Workspace", slug: "e2e-workspace", role: "org_admin" };
const project = {
  id: "project-e2e", organization_id: organization.id, name: "E2E Storefront",
  slug: "e2e-storefront", created_by: "user-e2e", role: "owner",
};
const secretRef = {
  id: "secret-ref-e2e", organization_id: organization.id, provider: "vault",
  external_key: "e2e/slack-webhook", rotated_at: null,
  created_at: "2026-11-01T00:00:00Z", active_version: 1,
};
const plan = { id: "plan-e2e", project_id: project.id, name: "Release checks", suite_id: "suite-e2e" };
const environment = {
  id: "environment-e2e", project_id: project.id, name: "Staging",
  base_url: "https://staging.example.test",
};

test("manages an integration, triggers CI, checks the gate, and retries a dead delivery", async ({ page }) => {
  const email = process.env.TESTPILOT_E2E_EMAIL;
  const password = process.env.TESTPILOT_E2E_PASSWORD;
  if (!email || !password) throw new Error("TESTPILOT_E2E_EMAIL and TESTPILOT_E2E_PASSWORD are required");

  let integration: Record<string, unknown> | null = null;
  let integrationEnabled = true;
  await page.route("**/api/v1/organizations", (route) => route.fulfill({ json: { items: [organization], next_cursor: null } }));
  await page.route("**/api/v1/projects", (route) => route.fulfill({ json: [project] }));
  await page.route("**/api/v1/auth/sessions", (route) => route.fulfill({ json: { sessions: [] } }));
  await page.route("**/api/v1/secret-refs", (route) => route.fulfill({ json: [secretRef] }));
  await page.route("**/api/v1/integrations?**", (route) => route.fulfill({ json: { items: integration ? [{ ...integration, enabled: integrationEnabled }] : [], next_cursor: null } }));
  await page.route("**/api/v1/integrations", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    integration = {
      id: "integration-e2e", organization_id: organization.id, kind: "slack",
      enabled: true, config: {}, secret_ref_id: secretRef.id,
      created_at: "2026-11-01T00:00:00Z", ...route.request().postDataJSON(),
    };
    return route.fulfill({ status: 201, json: integration });
  });
  await page.route("**/api/v1/integrations/integration-e2e", async (route) => {
    if (route.request().method() !== "PATCH") return route.continue();
    integrationEnabled = Boolean(route.request().postDataJSON().enabled);
    return route.fulfill({ json: { ...integration, enabled: integrationEnabled } });
  });
  await page.route(`**/api/v1/projects/${project.id}/plans`, (route) => route.fulfill({ json: { items: [plan], next_cursor: null } }));
  await page.route(`**/api/v1/projects/${project.id}/environments?**`, (route) => route.fulfill({ json: { items: [environment], next_cursor: null } }));
  await page.route("**/api/v1/webhooks/integration-e2e/deliveries?**", (route) => route.fulfill({ json: { items: [{
    id: "delivery-e2e", integration_id: "integration-e2e", event_id: "outbound:run.completed",
    status: "DEAD", attempts: 8, response_code: 503, next_attempt_at: null,
    created_at: "2026-11-01T00:00:00Z", attempt_history: [{
      attempt: 8, status: "DEAD", response_status: 503, error_code: "HTTP_503",
      created_at: "2026-11-01T00:00:00Z",
    }],
  }], next_cursor: null } }));
  await page.route("**/api/v1/webhooks/integration-e2e/deliveries/delivery-e2e/retry", (route) => route.fulfill({
    status: 202, json: { id: "delivery-e2e", integration_id: "integration-e2e", event_id: "outbound:run.completed", status: "PENDING", attempts: 0, response_code: null, next_attempt_at: null, created_at: "2026-11-01T00:00:00Z", attempt_history: [], queued: true },
  }));
  await page.route("**/api/v1/ci/trigger", (route) => route.fulfill({ status: 202, json: {
    id: "run-e2e", project_id: project.id, status: "QUEUED", trigger_source: "ci",
  } }));
  await page.route("**/api/v1/test-runs/run-e2e/gate?**", (route) => route.fulfill({ json: {
    run_id: "run-e2e", status: "PASS", blocking_severity: "HIGH",
    counts: { run_cases: 1, assertions_total: 1, assertions_evaluated: 1, assertions_passed: 1, assertions_failed: 0, assertions_pending: 0, blocking_failures: 0, cases_without_assertions: 0, cases_with_unknown_priority: 0 },
  } }));

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "欢迎回来" })).toBeVisible();
  await page.getByLabel("工作邮箱").fill(email);
  await page.getByLabel("密码").fill(password);
  const login = page.waitForResponse((response) => response.url().includes("/api/v1/auth/login"));
  await page.getByRole("button", { name: "登录" }).click();
  expect((await login).status()).toBe(200);

  await page.getByRole("button", { name: "集成与 CI" }).click();
  await expect(page.getByRole("heading", { name: "集成、CI 与 Webhook" })).toBeVisible();
  await page.getByLabel("SecretRef").selectOption(secretRef.id);
  await page.getByRole("button", { name: "添加集成" }).click();
  await expect(page.getByText("vault · e2e/slack-webhook · v1")).toBeVisible();
  await expect(page.getByText(/secret-value/i)).toHaveCount(0);
  await page.getByRole("button", { name: "停用集成" }).click();
  await expect(page.getByText("已停用")).toBeVisible();

  await page.getByLabel("仓库").fill("example/storefront");
  await page.getByLabel("Commit SHA").fill("abcdef1234567");
  await page.getByLabel("触发意图").fill("Validate release candidate");
  await page.getByRole("button", { name: "触发 CI Run" }).click();
  await expect(page.getByText("PASS")).toBeVisible();
  await expect(page.getByText("DEAD")).toBeVisible();
  await expect(page.getByText("HTTP_503")).toBeVisible();
  await page.getByRole("button", { name: "重试投递" }).click();
  await expect(page.getByText("重试已排队；原始尝试记录已保留。")).toBeVisible();
});
