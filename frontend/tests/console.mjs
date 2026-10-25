import { chromium, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { exerciseWorkflows, exerciseMedia } from "./workflows.mjs";
import { exerciseResilience, exerciseLogin } from "./resilience.mjs";
import { exercisePermissionScopes } from "./permission-scopes.mjs";
import { exerciseCreations, exerciseArtLibrary } from "./creations.mjs";
const publicMode = process.argv.includes("--public");
const screenshots = process.argv.includes("--screenshots");
const fields = publicMode ? {URL:process.env.PUBLIC_BASE_URL,Password:process.env.ADMIN_PASSWORD} : {};
if(publicMode && (!fields.URL || !fields.Password || !process.env.QQROLEBOT_TEST_SCOPE))
  throw Error('Set PUBLIC_BASE_URL, ADMIN_PASSWORD and QQROLEBOT_TEST_SCOPE for explicit public checks');
const base = publicMode ? fields.URL : "http://127.0.0.1:5173";
const scope = publicMode ? process.env.QQROLEBOT_TEST_SCOPE : "10000001:g:20000001";
const output = path.resolve("../artifacts/browser");
fs.mkdirSync(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  viewport: { width: 1440, height: 1050 },
});
const page = await context.newPage(),
  errors = [];
page.setDefaultTimeout(12000);
page.setDefaultNavigationTimeout(publicMode ? 45000 : 20000);
page.on("pageerror", (e) => errors.push(e.message));
page.on("console", (message) => {
  // Login discovery and deliberately rejected local requests are expected.
  if (
    message.type() === "error" &&
    !/Failed to load resource: the server responded with a status of (401|409|503)/.test(
      message.text(),
    )
  )
    errors.push("console: " + message.text());
});
try {
  await page.goto(base + "/groups/" + encodeURIComponent(scope) + "/summary");
  await expect(page.getByRole("textbox", { name: "用户名" })).toHaveCount(0);
  await expect(page.getByRole("checkbox", { name: "记住登录 30 天" })).not.toBeChecked();
  await page
    .getByLabel("密码", { exact: true })
    .fill(fields.Password || "test-password-123456");
  await page.getByRole("button", { name: "登录工作台 →" }).click();
  await expect(
    page.getByRole("switch", { name: "群聊启用", exact: true }),
  ).toBeVisible();
  await expect(page).toHaveURL(/\/groups\/.+\/summary$/);
  await page.goto(base + "/overview");
  await expect(
    page.getByRole("heading", { name: "总览", exact: true }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("navigation", { name: "主导航", exact: true })
      .getByRole("link"),
  ).toHaveCount(7);
  if (!publicMode) await exerciseWorkflows(page, base, scope);
  if (!publicMode) await exercisePermissionScopes(page, base, scope);
  if (!publicMode) await exerciseResilience(page.context(), base, scope);
  if (!publicMode) await exerciseCreations(page,base);
  if (!publicMode) await exerciseArtLibrary(page,base);
  await exerciseMedia(page, base, publicMode);
  await page.goto(base + "/settings");
  await expect(page.getByLabel("长会话压缩触发预算")).toHaveCount(0);
  await page.goto(base + "/groups/" + encodeURIComponent(scope) + "/settings");
  await expect(page.getByLabel("会话流程")).toHaveCount(0);
  const group = "/groups/" + encodeURIComponent(scope);
  const pages = [
    "/overview",
    "/groups",
    ...[
      "summary",
      "settings",
      "members",
      "memory",
      "context",
      "stickers",
      "tools",
      "permissions",
    ].map((s) => group + "/" + s),
    "/personas",
    "/tasks",
    ...["traces", "calls", "logs", "usage", "diagnostics"].map(
      (s) => "/observe/" + s,
    ),
    "/configuration/image-search",
    "/configuration/models",
    "/configuration/models?panel=providers",
    "/configuration/models?panel=models",
    "/configuration/prompts",
    "/settings",
    "/settings?section=context",
    "/settings?section=images",
    ...["tools", "members", "roles", "rules", "diagnostics"].map(
      (view) => group + "/access?view=" + view,
    ),
    "/settings?section=retention",
    "/settings?section=pricing",
    "/settings?section=security",
  ];
  for (const viewport of [
    { width: 1440, height: 1050 },
    { width: 1024, height: 900 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    for (const target of pages) {
      await page.goto(base + target);
      await page.waitForLoadState("networkidle");
      if (errors.length) throw Error(errors.join("\n"));
      if (
        await page.evaluate(
          () => document.documentElement.scrollWidth > innerWidth + 1,
        )
      )
        throw Error("Page overflow: " + viewport.width + " " + target);
    }
    if (viewport.width === 390) {
      await page.getByRole("button", { name: "打开导航" }).click();
      const nav = page.getByRole("navigation", { name: "移动导航" });
      await expect(nav.getByRole("link")).toHaveCount(7);
      await nav.getByRole("link", { name: "任务中心", exact: true }).click();
      await expect(page.getByRole("dialog")).toHaveCount(0);
      await page.goto(base + "/tasks?scope=" + scope);
      await page.waitForLoadState("networkidle");
      if (!publicMode) {
        await page.getByRole("button", { name: "＋ 创建任务" }).click();
        const box = await page
          .getByRole("dialog", { name: "创建任务", exact: true })
          .boundingBox();
        expect(box.width).toBe(390);
        await page.keyboard.press("Escape");
        await expect(page.getByRole("dialog")).toHaveCount(0);
      }
    }
    for (const [name, target] of screenshots
      ? [
          ["overview", "/overview"],
          ["groups", "/groups"],
          ["group-summary", group + "/summary"],
          ["group-settings", group + "/settings"],
          ["access", group + "/access"],
          ["system-context", "/settings?section=context"],
          ["models", "/configuration/models"],
          ["stickers", group + "/stickers"],
          ["tasks", "/tasks?scope=" + scope],
        ]
      : []) {
      await page.goto(base + target);
      await page.waitForLoadState("networkidle");
      await page.screenshot({
        path: path.join(
          output,
          `${publicMode ? "public" : "local"}-${name}-${viewport.width}.png`,
        ),
        fullPage: true,
      });
    }
  }
  if (!publicMode) await exerciseLogin(browser, base, output);
  if (errors.length) throw Error(errors.join("\n"));
  console.log(
    JSON.stringify({
      ok: true,
      public_readonly: publicMode,
      workflows: !publicMode,
      screenshots,
      pages: pages.length,
      widths: [1440, 1024, 390],
      errors,
    }),
  );
} catch (e) {
  await page.screenshot({
    path: path.join(output, "failure.png"),
    fullPage: true,
  });
  throw e;
} finally {
  await browser.close();
}
