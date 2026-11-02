import { expect, test } from "@playwright/test";

test("creates a report export, downloads it through the authenticated API, and labels expired evidence", async ({
  page,
}) => {
  const email = process.env.TESTPILOT_E2E_EMAIL;
  const password = process.env.TESTPILOT_E2E_PASSWORD;
  if (!email || !password) {
    throw new Error("TESTPILOT_E2E_EMAIL and TESTPILOT_E2E_PASSWORD are required");
  }

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "欢迎回来" })).toBeVisible();
  await page.getByLabel("工作邮箱").fill(email);
  await page.getByLabel("密码").fill(password);
  const loginResponse = page.waitForResponse((response) =>
    response.url().includes("/api/v1/auth/login") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "登录" }).click();
  expect((await loginResponse).status()).toBe(200);

  await page.route(/\/api\/v1\/test-runs\/[^/]+\/report$/, async (route) => {
    const response = await route.fetch();
    if (!response.ok()) return route.fulfill({ response });
    const payload = await response.json() as {
      artifact_references?: Array<Record<string, unknown>>;
    };
    const references = payload.artifact_references ?? [];
    references.push({
      id: "expired-trace-reference", artifact_type: "trace",
      status: "EXPIRED_ARTIFACT",
    });
    return route.fulfill({ response, json: { ...payload, artifact_references: references } });
  });

  await page.getByRole("button", { name: "运行报告" }).click();
  await expect(page.getByRole("heading", { name: "运行报告与导出" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "报告摘要" })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/EXPIRED_ARTIFACT/).first()).toBeVisible();
  await page.getByLabel("导出格式").selectOption("pdf");
  await page.getByRole("button", { name: "生成导出" }).click();
  await expect(page.getByText("导出已完成")).toBeVisible({ timeout: 120_000 });

  const downloadRequest = page.waitForRequest((request) =>
    request.url().includes("/api/v1/artifacts/") && request.url().endsWith("/download"),
  );
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载报告" }).click();
  const [request, download] = await Promise.all([downloadRequest, downloadEvent]);
  expect(request.headers()["authorization"]).toMatch(/^Bearer\s+\S+/);
  expect(download.suggestedFilename()).toMatch(/\.pdf$/);
});
