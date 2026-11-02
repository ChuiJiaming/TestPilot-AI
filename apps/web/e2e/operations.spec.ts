import { expect, test, type Page } from "@playwright/test";

async function signIn(page: Page, email: string, password: string) {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "欢迎回来" })).toBeVisible();
  await page.getByLabel("工作邮箱").fill(email);
  await page.getByLabel("密码").fill(password);
  const response = page.waitForResponse((item) =>
    item.url().includes("/api/v1/auth/login") && item.request().method() === "POST",
  );
  await page.getByRole("button", { name: "登录" }).click();
  expect((await response).status()).toBe(200);
  await expect(page.getByRole("button", { name: "运营与审计" })).toBeVisible();
}

async function openOperations(page: Page) {
  await page.getByRole("button", { name: "运营与审计" }).click();
  await expect(page.getByRole("heading", { name: "组织运营与平台治理" })).toBeVisible();
}

test("shows quota exhaustion and exercises notifications, audit filters, verification, and export", async ({ page }) => {
  const email = process.env.TESTPILOT_E2E_EMAIL;
  const password = process.env.TESTPILOT_E2E_PASSWORD;
  if (!email || !password) throw new Error("TESTPILOT_E2E_EMAIL and TESTPILOT_E2E_PASSWORD are required");

  await signIn(page, email, password);
  await openOperations(page);
  await expect(page.getByText("配额策略")).toBeVisible({ timeout: 15_000 });

  const tokenLimit = page.getByLabel("每月模型 Token 额度");
  const originalLimit = await tokenLimit.inputValue();
  await tokenLimit.fill("0");
  const exhaustionPolicy = page.waitForResponse((response) =>
    response.url().includes("/api/v1/organizations/") && response.url().endsWith("/quotas") && response.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "保存配额策略" }).click();
  expect((await exhaustionPolicy).status()).toBe(200);
  await expect(page.getByRole("status", { name: "模型调用额度已耗尽" })).toBeVisible();
  if (originalLimit !== "0") {
    await tokenLimit.fill(originalLimit);
    const restorePolicy = page.waitForResponse((response) =>
      response.url().includes("/api/v1/organizations/") && response.url().endsWith("/quotas") && response.request().method() === "PUT",
    );
    await page.getByRole("button", { name: "保存配额策略" }).click();
    expect((await restorePolicy).status()).toBe(200);
  }

  await page.getByRole("tab", { name: "通知与投递" }).click();
  await expect(page.getByRole("heading", { name: "通知偏好" })).toBeVisible();
  const preferenceToggle = page.getByRole("checkbox").first();
  const hasPreference = await preferenceToggle.count() > 0;
  const originalPreference = hasPreference ? await preferenceToggle.isChecked() : false;
  if (hasPreference) await preferenceToggle.click();
  const preferenceSave = page.waitForResponse((response) =>
    response.url().endsWith("/api/v1/notifications/preferences") && response.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "保存通知偏好" }).click();
  expect((await preferenceSave).status()).toBe(200);
  if (hasPreference) {
    await preferenceToggle.click();
    const restorePreference = page.waitForResponse((response) =>
      response.url().endsWith("/api/v1/notifications/preferences") && response.request().method() === "PUT",
    );
    await page.getByRole("button", { name: "保存通知偏好" }).click();
    expect((await restorePreference).status()).toBe(200);
    await expect(preferenceToggle).toBeChecked({ checked: originalPreference });
  }

  await page.getByRole("tab", { name: "审计日志" }).click();
  await page.getByLabel("操作类型").fill("case.created");
  const auditFilter = page.waitForResponse((response) =>
    response.url().includes("/api/v1/organizations/")
      && response.url().includes("/audit-logs?") && response.url().includes("action=case.created"),
  );
  await page.getByRole("button", { name: "筛选" }).click();
  expect((await auditFilter).status()).toBe(200);
  const verification = page.waitForResponse((response) => response.url().includes("/api/v1/audit-logs/verify"));
  await page.getByRole("button", { name: "验证审计链" }).click();
  expect((await verification).status()).toBe(200);
  const exportResponse = page.waitForResponse((response) =>
    response.url().endsWith("/api/v1/audit-logs/export") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "导出审计日志" }).click();
  expect((await exportResponse).status()).toBe(200);
});

test("a second platform administrator approves a setting change", async ({ page, browser }) => {
  const requesterEmail = process.env.TESTPILOT_E2E_PLATFORM_ADMIN_EMAIL;
  const requesterPassword = process.env.TESTPILOT_E2E_PLATFORM_ADMIN_PASSWORD;
  const approverEmail = process.env.TESTPILOT_E2E_APPROVER_EMAIL;
  const approverPassword = process.env.TESTPILOT_E2E_APPROVER_PASSWORD;
  if (!requesterEmail || !requesterPassword || !approverEmail || !approverPassword) {
    throw new Error("TESTPILOT_E2E_PLATFORM_ADMIN_EMAIL/PASSWORD and TESTPILOT_E2E_APPROVER_EMAIL/PASSWORD are required");
  }

  await signIn(page, requesterEmail, requesterPassword);
  await openOperations(page);
  await page.getByRole("tab", { name: "平台设置" }).waitFor({ state: "visible", timeout: 15_000 });
  await page.getByRole("tab", { name: "平台设置" }).click();
  await page.getByLabel("设置项").selectOption("notification_retry_limit");
  await page.getByLabel("申请值").fill("2");
  const requestReason = `M15 approval review ${Date.now()}`;
  await page.getByLabel("申请理由").fill(requestReason);
  const requestResponse = page.waitForResponse((response) =>
    response.url().endsWith("/api/v1/system-settings") && response.request().method() === "PUT",
  );
  await page.getByRole("button", { name: "提交审批申请" }).click();
  expect((await requestResponse).status()).toBe(200);

  const approverContext = await browser.newContext({
    baseURL: process.env.TESTPILOT_E2E_BASE_URL ?? "http://localhost:5173",
  });
  try {
    const approverPage = await approverContext.newPage();
    await signIn(approverPage, approverEmail, approverPassword);
    await openOperations(approverPage);
    await approverPage.getByRole("tab", { name: "平台设置" }).waitFor({ state: "visible", timeout: 15_000 });
    await approverPage.getByRole("tab", { name: "平台设置" }).click();
    const pendingChange = approverPage.locator(".operations-change-card").filter({ hasText: requestReason });
    await expect(pendingChange).toBeVisible({ timeout: 15_000 });
    await pendingChange.getByLabel("审批理由").fill("Reviewed under the two-person control");
    const decisionResponse = approverPage.waitForResponse((response) =>
      response.url().includes("/admin/system-setting-changes/") && response.request().method() === "POST",
    );
    await pendingChange.getByRole("button", { name: "批准变更" }).click();
    expect((await decisionResponse).status()).toBe(200);
    await expect(approverPage.getByRole("status").filter({ hasText: "设置变更已批准并生效" })).toBeVisible();
  } finally {
    await approverContext.close();
  }
});
