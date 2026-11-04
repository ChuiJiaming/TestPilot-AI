// README 功能截图脚本：依赖本目录下的 readme-mock-server.mjs 与已启动的 Vite dev server
// 用法：node scripts/readme-screenshots.mjs [webUrl]
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const webUrl = process.argv[2] ?? "http://127.0.0.1:5199";
const outDir = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "..", "docs", "images", "screens");
mkdirSync(outDir, { recursive: true });

const viewport = { width: 1600, height: 950 };
const browser = await chromium.launch({ args: ["--no-proxy-server"] });
const shoot = async (name, fn) => {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 2, locale: "zh-CN" });
  const page = await context.newPage();
  await fn(page);
  await page.screenshot({ path: join(outDir, `${name}.png`) });
  console.log("saved", name);
  await context.close();
};

// 1. 登录页（阻断 refresh，强制进入登录界面并填充示例数据）
await shoot("login", async (page) => {
  await page.route("**/api/v1/auth/refresh", (route) => route.abort());
  await page.goto(webUrl, { waitUntil: "networkidle" });
  await page.getByText("欢迎回来").waitFor({ timeout: 15_000 });
  await page.fill("#email", "reese@hoshino.dev");
  await page.fill("#password", "••••••••••••");
  await page.waitForTimeout(400);
});

// 已登录上下文
const authed = async (page) => {
  await page.goto(webUrl, { waitUntil: "networkidle" });
  await page.getByText("为下一次可靠发布做好准备。").waitFor({ timeout: 20_000 });
  await page.waitForTimeout(500);
};
const openNav = async (page, label, waitText) => {
  await page.getByRole("button", { name: label, exact: true }).click();
  if (waitText) await page.getByText(waitText).first().waitFor({ timeout: 15_000 });
  else {
    await page.waitForLoadState("networkidle").catch(() => undefined);
    await page.waitForTimeout(1200);
  }
};

// 2. 工作台
await shoot("overview", authed);
// 3. 组织与项目
await shoot("projects", async (page) => { await authed(page); await openNav(page, "组织与项目", "项目列表"); });
// 4. 测试环境
await shoot("environments", async (page) => { await authed(page); await openNav(page, "测试环境", "不可变运行快照"); });
// 5. 用例与版本
await shoot("cases", async (page) => { await authed(page); await openNav(page, "用例与版本", "登录流程验证"); });
// 6. 生成、套件与调度（套件位于「套件与计划」tab）
await shoot("planning", async (page) => {
  await authed(page);
  await openNav(page, "生成、套件与调度");
  await page.getByRole("button", { name: "套件与计划" }).click();
  await page.getByText("测试套件").first().waitFor({ timeout: 15_000 });
  await page.waitForTimeout(600);
});
// 7. 运行控制台
await shoot("runs", async (page) => { await authed(page); await openNav(page, "运行控制台", "run-a1b2c3d4"); });
// 8. 运行报告
await shoot("reports", async (page) => { await authed(page); await openNav(page, "运行报告"); });
// 9. 成员与权限
await shoot("members", async (page) => { await authed(page); await openNav(page, "成员与权限", "项目成员"); });

await browser.close();
console.log("all screenshots saved to", outDir);
