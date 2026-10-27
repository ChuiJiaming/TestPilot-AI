import { expect, test } from "@playwright/test";

test("login stores refresh credential as a Secure HttpOnly SameSite=Lax cookie", async ({ page, context }) => {
  const email = process.env.TESTPILOT_E2E_EMAIL;
  const password = process.env.TESTPILOT_E2E_PASSWORD;
  if (!email || !password) throw new Error("TESTPILOT_E2E_EMAIL and TESTPILOT_E2E_PASSWORD are required");

  await page.goto("/");
  await expect(page.getByRole("heading", { name: "欢迎回来" })).toBeVisible();
  await page.getByLabel("工作邮箱").fill(email);
  await page.getByLabel("密码").fill(password);

  const loginResponse = page.waitForResponse((response) =>
    response.url().includes("/api/v1/auth/login") && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "登录" }).click();
  const response = await loginResponse;
  expect(response.status()).toBe(200);
  const body = await response.json() as Record<string, unknown>;
  expect(body).toHaveProperty("access_token");
  expect(body).not.toHaveProperty("refresh_token");

  const refreshCookie = (await context.cookies()).find((cookie) => cookie.name === "__Host-testpilot_refresh");
  expect(refreshCookie).toBeDefined();
  expect(refreshCookie?.httpOnly).toBe(true);
  expect(refreshCookie?.secure).toBe(true);
  expect(refreshCookie?.sameSite).toBe("Lax");
  expect(refreshCookie?.path).toBe("/");

  const refreshResponse = page.waitForResponse((candidate) =>
    candidate.url().includes("/api/v1/auth/refresh") && candidate.request().method() === "POST",
  );
  await page.reload();
  expect((await refreshResponse).status()).toBe(200);
  const rotatedCookie = (await context.cookies()).find((cookie) => cookie.name === "__Host-testpilot_refresh");
  expect(rotatedCookie).toBeDefined();
  expect(rotatedCookie?.value).not.toBe(refreshCookie?.value);
});
