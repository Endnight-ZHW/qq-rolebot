import { expect } from "@playwright/test";
import path from "node:path";

export async function exerciseLogin(browser, base, output) {
  let context = await browser.newContext(), page = await context.newPage();
  const password = "test-password-123456";
  const cookie = async () => (await context.cookies()).find(c => c.name === "rolebot_session");
  try {
    await page.goto(base + "/login?next=/settings");
    await expect(page.getByRole("checkbox", {name:"记住登录 30 天"})).not.toBeChecked();
    await expect(page.getByLabel("密码", {exact:true})).toHaveAttribute("autocomplete", "current-password");
    expect(await page.locator('input[name="username"]').inputValue()).toBe("admin");
    await expect(page.locator('input[name="username"]')).toBeHidden();
    for (const width of [1440,390]) {
      await page.setViewportSize({width,height:900});
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
      await page.screenshot({path:path.join(output,`login-${width}.png`),fullPage:true});
    }
    await page.getByLabel("密码", {exact:true}).fill("incorrect-password");
    await page.getByRole("button", {name:"登录工作台 →"}).click();
    await expect(page.locator('form').getByRole("alert")).toBeVisible();
    expect(await cookie()).toBeUndefined();
    await page.getByLabel("密码", {exact:true}).fill(password);
    const shortStart = Date.now()/1000;
    await page.getByRole("button", {name:"登录工作台 →"}).click();
    await expect(page).toHaveURL(base + "/settings");
    const short = await cookie();
    expect(short.expires-shortStart).toBeGreaterThan(43190);
    expect(short.expires-shortStart).toBeLessThan(43210);
    const me = await (await context.request.get(base + "/api/v1/auth/me")).json();
    await context.request.post(base + "/api/v1/auth/logout", {data:{},headers:{"X-CSRF-Token":me.csrf}});
    await page.goto(base + "/login?next=/observe/traces");
    await page.getByLabel("密码", {exact:true}).fill(password);
    await page.getByRole("checkbox", {name:"记住登录 30 天"}).check();
    const start = Date.now()/1000;
    await page.getByRole("button", {name:"登录工作台 →"}).click();
    await expect(page).toHaveURL(base + "/observe/traces");
    const long = await cookie();
    expect(long.expires-start).toBeGreaterThan(2591990);
    expect(long.expires-start).toBeLessThan(2592010);
    expect(long.httpOnly).toBe(true);expect(long.sameSite).toBe("Strict");
    const state = await context.storageState();
    expect(state.origins.every(o => o.localStorage.length===0)).toBe(true);
    await context.close();
    context = await browser.newContext({storageState:state});page = await context.newPage();
    await page.goto(base + "/observe/traces");
    await expect(page.getByRole("heading", {name:"观测中心",exact:true})).toBeVisible();
    await page.getByRole("button", {name:"退出",exact:true}).click();
    await expect(page).toHaveURL(/\/login/);
    expect(await cookie()).toBeUndefined();
    const copy = await browser.newContext({storageState:state});
    try {expect((await copy.request.get(base + "/api/v1/auth/me")).status()).toBe(401);}
    finally {await copy.close();}
    await page.getByLabel("密码", {exact:true}).fill(password);
    await page.getByRole("checkbox", {name:"记住登录 30 天"}).check();
    await page.getByRole("button", {name:"登录工作台 →"}).click();
    await expect(page.getByRole("button", {name:"退出",exact:true})).toBeVisible();
    const beforeChange = await context.storageState();
    const changePassword = async (current, next) => {
      await page.goto(base + "/settings?section=security");
      await page.getByLabel("当前密码", {exact:true}).fill(current);
      await page.getByLabel("新密码", {exact:true}).fill(next);
      await page.getByRole("button", {name:"更新密码",exact:true}).click();
      await expect(page).toHaveURL(/\/login/);
    };
    await changePassword(password,"ui-login-changed-123456");
    const revoked = await browser.newContext({storageState:beforeChange});
    try {expect((await revoked.request.get(base + "/api/v1/auth/me")).status()).toBe(401);}
    finally {await revoked.close();}
    await page.getByLabel("密码", {exact:true}).fill(password);
    await page.getByRole("button", {name:"登录工作台 →"}).click();
    await expect(page.locator('form').getByRole("alert")).toContainText("密码不正确");
    await page.getByLabel("密码", {exact:true}).fill("ui-login-changed-123456");
    await page.getByRole("button", {name:"登录工作台 →"}).click();
    await expect(page.getByRole("button", {name:"退出",exact:true})).toBeVisible();
    await changePassword("ui-login-changed-123456",password);
  } finally {await context.close();}
}

export async function exerciseResilience(context, base, scope) {
  const page = await context.newPage();
  page.setDefaultTimeout(12000);
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const go = async (path) => {
    await page.goto(base + path);
    await page.waitForLoadState("networkidle");
  };
  try {
    await go("/overview");
    await page.route("**/api/v1/dashboard?**", async (r) => {
      const response = await r.fetch();
      const body = await response.json();
      if (new URL(r.request().url()).searchParams.get("scope") === scope) {
        await new Promise((resolve) => setTimeout(resolve, 500));
        body.metrics.calls = 987654;
      }
      await r.fulfill({ response, json: body }).catch(() => {});
    });
    await Promise.all([
      page.waitForRequest(
        (r) =>
          r.url().includes("/dashboard?") &&
          new URL(r.url()).searchParams.get("scope") === scope,
      ),
      page.getByLabel("统计会话范围", { exact: true }).selectOption(scope),
    ]);
    await page
      .getByLabel("统计会话范围", { exact: true })
      .selectOption("10000001:g:20000002");
    await expect(page.getByLabel("统计会话范围", { exact: true })).toHaveValue(
      "10000001:g:20000002",
    );
    await expect(page.locator(".metric-card").first()).toBeVisible();
    await page.waitForTimeout(650);
    await expect(page.locator(".metric-card").first()).not.toContainText("987");
    await page.unroute("**/api/v1/dashboard?**");
    const before = await page
      .locator(".metric-card")
      .first()
      .locator("strong")
      .textContent();
    await page.route("**/api/v1/dashboard?**", (r) =>
      r.fulfill({ status: 503, json: { detail: "模拟统计服务暂不可用" } }),
    );
    await page.getByRole("button", { name: "刷新统计", exact: true }).click();
    await expect(page.getByRole("alert")).toContainText("模拟统计服务暂不可用");
    await expect(
      page.locator(".metric-card").first().locator("strong"),
    ).toHaveText(before);
    await page.unroute("**/api/v1/dashboard?**");
    await page.route("**/api/v1/dashboard?**", async (r) => {
      const response = await r.fetch(),
        body = await response.json();
      Object.assign(body.metrics, {
        calls: 0,
        success: 0,
        failed: 0,
        success_rate: null,
        p95_ms: null,
        costs: [],
        excluded: {},
        input_tokens: 0,
        cached_tokens: 0,
        output_tokens: 0,
      });
      body.series = body.series.map((s) => ({
        ...s,
        calls: null,
        success: null,
        failed: null,
        partial: true,
      }));
      body.purposes = [];
      body.alerts = [];
      body.tasks = [];
      body.groups = [];
      await r.fulfill({ response, json: body });
    });
    await page.getByRole("button", { name: "刷新统计", exact: true }).click();
    await expect(page.locator(".chart-empty")).toHaveCount(3);
    await expect(
      page.locator(".metric-card").first().locator("strong"),
    ).toContainText("—");
    await page.unroute("**/api/v1/dashboard?**");
    await page.setViewportSize({ width: 390, height: 844 });
    await go("/tasks?scope=" + scope);
    await page
      .getByRole("button", { name: "＋ 创建任务", exact: true })
      .click();
    const dialog = page.getByRole("dialog", { name: "创建任务", exact: true });
    await expect(dialog).toBeFocused();
    await page.keyboard.press("Shift+Tab");
    await expect(
      dialog.getByRole("button", { name: "创建任务", exact: true }),
    ).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(
      dialog.getByRole("button", { name: "关闭", exact: true }),
    ).toBeFocused();
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    expect(await page.evaluate(() => document.body.style.overflow)).not.toBe(
      "hidden",
    );
    await go("/groups/" + encodeURIComponent(scope) + "/settings");
    await page.clock.install();
    await page
      .getByLabel("显示名称", { exact: true })
      .fill("轮询期间保留的草稿");
    await page.clock.fastForward(16000);
    await expect(page.getByLabel("显示名称", { exact: true })).toHaveValue(
      "轮询期间保留的草稿",
    );
    await page.getByRole("button", { name: "放弃修改", exact: true }).click();
    expect(errors).toEqual([]);
  } finally {
    await page.close();
  }
}
