import { expect } from "@playwright/test";

export async function exerciseWorkflows(page, base, scope) {
  const group = "/groups/" + encodeURIComponent(scope);
  const go = async (path) => {
    await page.goto(base + path);
    await page.waitForLoadState("networkidle");
  };
  const mainNav = (name) =>
    page
      .getByRole("navigation", { name: "主导航", exact: true })
      .getByRole("link", { name, exact: true });
  const dialog = (name) => page.getByRole("dialog", { name, exact: true });
  await go(group + "/summary");
  const enabled = page.getByRole("switch", { name: "群聊启用", exact: true });
  await expect(enabled).toBeChecked();
  await enabled.uncheck();
  await page.route("**/api/v1/groups/" + scope, async (route) =>
    route.request().method() === "PATCH"
      ? route.fulfill({ status: 503, json: { detail: "模拟开关保存失败" } })
      : route.continue(),
  );
  await page.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(page.getByRole("region", { name: "未保存修改" })).toContainText(
    "模拟开关保存失败",
  );
  await expect(page.locator(".saved-switch-state")).toContainText("已启用");
  await expect(enabled).not.toBeChecked();
  await page.unroute("**/api/v1/groups/" + scope);
  await expect(page.locator(".group-enable-panel")).toContainText(
    "待保存：将关闭此会话",
  );
  const beforeSave = await (
    await page.request.get(base + "/api/v1/groups")
  ).json();
  expect(beforeSave.find((g) => g.id === scope).enabled).toBe(true);
  await enabled.focus();
  await page.keyboard.press("Space");
  await expect(enabled).toBeChecked();
  await enabled.uncheck();
  await page.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(page.locator(".saved-switch-state")).toContainText("未启用");
  await enabled.check();
  await page.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(page.locator(".saved-switch-state")).toContainText("已启用");
  await go("/settings?section=context");
  await expect(
    page.getByLabel("工作上下文初始消息条数", { exact: true }),
  ).toHaveValue("40");
  await expect(
    page.getByLabel("工作上下文消息条数上限", { exact: true }),
  ).toHaveValue("80");
  await expect(
    page.getByLabel("工作上下文软预算", { exact: true }),
  ).toHaveValue("32000");
  await expect(
    page.getByLabel("停止参与后等待时间", { exact: true }),
  ).toHaveValue("600");
  await expect(
    page.getByLabel("持续参与整理间隔", { exact: true }),
  ).toHaveValue("7200");
  await page.getByLabel("停止参与后等待时间", { exact: true }).fill("900");
  await page.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(0);
  await page.reload();
  await expect(
    page.getByLabel("停止参与后等待时间", { exact: true }),
  ).toHaveValue("900");
  await go(group + "/memory");
  await expect(
    page.getByRole("heading", { name: "参与会话后的记忆整理", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "当前没有待整理的参与会话。沉默期间的新群消息仅保留为可查询历史。",
      { exact: true },
    ),
  ).toBeVisible();
  await go("/settings?section=context");
  await page.getByLabel("停止参与后等待时间", { exact: true }).fill("600");
  await page.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(0);
  await go(group + "/settings");
  await page.getByRole("button", { name: "接话节奏", exact: true }).click();
  const listeningProbability = page.getByLabel("旁听判定概率", {
    exact: true,
  });
  await expect(listeningProbability).toHaveValue("1");
  await listeningProbability.fill("0.5");
  await page.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(0);
  await page.reload();
  await page.getByRole("button", { name: "接话节奏", exact: true }).click();
  await expect(listeningProbability).toHaveValue("0.5");
  await listeningProbability.fill("1");
  await page.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(0);
  await expect(
    page.getByRole("switch", { name: "群聊启用", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "基本配置", exact: true }).click();
  const original = await page
    .getByLabel("显示名称", { exact: true })
    .inputValue();
  await page
    .getByLabel("显示名称", { exact: true })
    .fill(original + " · 已调整");
  await mainNav("角色库").click();
  await dialog("保存当前修改？")
    .getByRole("button", { name: "继续编辑" })
    .click();
  await expect(page.getByLabel("显示名称", { exact: true })).toHaveValue(
    original + " · 已调整",
  );
  await page
    .getByLabel("切换会话", { exact: true })
    .selectOption("10000001:g:20000002");
  await dialog("保存当前修改？")
    .getByRole("button", { name: "继续编辑" })
    .click();
  await expect(page.getByLabel("切换会话", { exact: true })).toHaveValue(scope);
  await mainNav("角色库").click();
  await dialog("保存当前修改？")
    .getByRole("button", { name: "保存并离开" })
    .click();
  await expect(page).toHaveURL(/\/personas$/);
  await go(group + "/settings");
  await expect(page.getByLabel("显示名称", { exact: true })).toHaveValue(
    original + " · 已调整",
  );
  await page
    .getByLabel("显示名称", { exact: true })
    .fill(original + " · 未保存");
  await page.route("**/api/v1/groups/" + scope, async (route) =>
    route.request().method() === "PATCH"
      ? route.fulfill({ status: 409, json: { detail: "模拟保存冲突" } })
      : route.continue(),
  );
  await mainNav("角色库").click();
  await dialog("保存当前修改？")
    .getByRole("button", { name: "保存并离开" })
    .click();
  await expect(dialog("保存当前修改？")).toBeVisible();
  await expect(dialog("保存当前修改？")).toContainText("尚有修改未保存");
  await dialog("保存当前修改？")
    .getByRole("button", { name: "继续编辑" })
    .click();
  await page.unroute("**/api/v1/groups/" + scope);
  await page.getByRole("button", { name: "放弃修改", exact: true }).click();
  await page.getByLabel("显示名称", { exact: true }).fill(original);
  await page.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(0);

  await go("/personas");
  await page
    .getByLabel("一句话简介", { exact: true })
    .fill("浏览器回归测试角色简介");
  await page.getByRole("button", { name: "保存角色", exact: true }).click();
  await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(0);
  await page.getByRole("button", { name: "查看历史版本", exact: true }).click();
  await expect(page.locator(".version-row").first()).toBeVisible();
  const exportPersona = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出", exact: true }).click();
  expect((await exportPersona).suggestedFilename()).toMatch(/\.json$/);
  await page
    .locator(".version-row")
    .last()
    .getByRole("button", { name: "恢复此版本" })
    .click();
  await dialog("恢复角色版本")
    .getByRole("button", { name: "恢复版本", exact: true })
    .click();
  await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(0);

  await go(group + "/memory");
  await page.getByRole("button", { name: "＋ 添加记忆", exact: true }).click();
  const memory = "浏览器测试：测试成员喜欢蓝莓蛋糕。" + Date.now();
  await page.getByLabel("记忆内容", { exact: true }).fill(memory);
  await page.getByLabel("相关成员 QQ", { exact: true }).fill("30000001");
  await dialog("新增记忆")
    .getByRole("button", { name: "保存记忆", exact: true })
    .click();
  const card = page.locator(".memory-card").filter({ hasText: memory });
  await expect(card).toBeVisible();
  await card.getByRole("link", { name: "来源与版本 →" }).click();
  await dialog("记忆详情").getByText("来源与版本", { exact: true }).click();
  await expect(dialog("记忆详情")).toContainText("管理员");
  await page.reload();
  await expect(dialog("记忆详情")).toBeVisible();
  await dialog("记忆详情")
    .getByRole("button", { name: "关闭", exact: true })
    .click();
  await card.getByRole("button", { name: "编辑", exact: true }).click();
  await page
    .getByLabel("记忆内容", { exact: true })
    .fill(memory + "（已核对）");
  await dialog("修正记忆")
    .getByRole("button", { name: "关闭", exact: true })
    .click();
  await dialog("保存当前修改？")
    .getByRole("button", { name: "保存并离开" })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(await page.evaluate(() => document.body.style.overflow)).not.toBe(
    "hidden",
  );
  await expect(
    page.locator(".memory-card").filter({ hasText: memory + "（已核对）" }),
  ).toBeVisible();

  await go("/tasks?scope=" + scope);
  await page.getByRole("button", { name: "＋ 创建任务", exact: true }).click();
  const task = "浏览器任务 " + Date.now();
  await page.getByLabel("任务内容", { exact: true }).fill(task);
  await expect(dialog("创建任务").locator(".schedule-preview")).toContainText(
    "预计",
  );
  await dialog("创建任务")
    .getByRole("button", { name: "创建任务", exact: true })
    .click();
  const taskCard = page.locator(".task-card").filter({ hasText: task });
  await expect(taskCard).toBeVisible();
  for (const [action, status] of [
    ["暂停", "已暂停"],
    ["恢复", "待执行"],
  ]) {
    await taskCard.getByRole("button", { name: action, exact: true }).click();
    await expect(taskCard).toContainText(status);
  }
  await taskCard.getByRole("button", { name: "编辑", exact: true }).click();
  await dialog("编辑任务")
    .getByLabel("任务内容", { exact: true })
    .fill(task + " 更新");
  await dialog("编辑任务")
    .getByRole("button", { name: "保存任务", exact: true })
    .click();
  await expect(taskCard).toContainText("更新");
  await taskCard.getByRole("button", { name: "取消", exact: true }).click();
  await dialog("取消任务")
    .getByRole("button", { name: "取消任务", exact: true })
    .click();
  await expect(taskCard).toContainText("已取消");

  await go(group + "/stickers");
  await page
    .locator(".sticker-card")
    .first()
    .getByRole("button", { name: "编辑详情 →" })
    .click();
  await dialog("表情详情")
    .getByLabel("允许聊天使用", { exact: true })
    .uncheck();
  await dialog("表情详情")
    .getByRole("button", { name: "保存", exact: true })
    .click();
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(page.locator(".sticker-card").first()).toContainText("已禁用");

  await expect(
    page.getByRole("heading", { name: "共享表情库", exact: true }),
  ).toBeVisible();
  await page.getByText("共享表情库容量设置", { exact: true }).click();
  await expect(page.getByLabel("共享收藏上限", { exact: true })).toHaveValue(
    "100",
  );
  await page.getByLabel("共享收藏上限", { exact: true }).fill("120");
  await page.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(0);
  const existingStickers = await (
    await page.request.get(base + "/api/v1/stickers")
  ).json();
  const animation = await (
    await page.request.get(
      base + "/api/v1/stickers/" + existingStickers[0].id + "/image",
    )
  ).body();
  const sharedCount = existingStickers.length;
  const upload = page.locator('.upload-button input[type="file"]');
  await upload.setInputFiles({
    name: "重复动画.gif",
    mimeType: "image/gif",
    buffer: animation,
  });
  await expect(page.getByRole("status")).toContainText(
    "表情已在共享库中，仍保持禁用",
  );
  await expect(page.locator(".sticker-card")).toHaveCount(sharedCount);
  await expect(page.locator(".sticker-card").first()).toContainText("已禁用");
  await upload.setInputFiles({
    name: "共享上传测试.gif",
    mimeType: "image/gif",
    buffer: Buffer.concat([animation, Buffer.from("shared-ui-test")]),
  });
  await expect(page.locator(".sticker-card")).toHaveCount(sharedCount + 1);
  await go(
    "/groups/" + encodeURIComponent("10000001:g:20000002") + "/stickers",
  );
  await page.getByText("共享表情库容量设置", { exact: true }).click();
  await expect(page.getByLabel("共享收藏上限", { exact: true })).toHaveValue(
    "120",
  );
  await expect(page.locator(".sticker-card")).toHaveCount(sharedCount + 1);
  const sharedCard = page
    .locator(".sticker-card")
    .filter({ hasText: "共享上传测试.gif" });
  await sharedCard.getByRole("button", { name: "删除", exact: true }).click();
  await expect(dialog("删除表情")).toContainText("所有群将无法再使用");
  await dialog("删除表情")
    .getByRole("button", { name: "删除表情", exact: true })
    .click();
  await expect(page.locator(".sticker-card")).toHaveCount(sharedCount);
  await go(group + "/stickers");
  await expect(page.locator(".sticker-card")).toHaveCount(sharedCount);
  await expect(page.locator(".sticker-card").first()).toContainText("已禁用");
  await page.getByText("共享表情库容量设置", { exact: true }).click();
  await page.getByLabel("共享收藏上限", { exact: true }).fill("100");
  await page.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(0);

  await go("/configuration/models");
  await page.getByRole("link", { name: "供应商连接", exact: true }).click();
  await page
    .getByLabel("API Key", { exact: true })
    .fill("local-test-no-paid-call");
  await page
    .getByLabel("接口协议", { exact: true })
    .selectOption("anthropic-messages");
  await page.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(
    page.getByText("模型配置已保存，下次请求生效", { exact: true }),
  ).toBeVisible();
  await go("/configuration/prompts?scope=" + scope);
  await page.getByLabel("编辑当前群覆盖", { exact: true }).check();
  await page.getByLabel("搜索模板", { exact: true }).fill("task.execute");
  await page.locator(".template-item").click();
  const prompt = page.getByLabel("提示词草稿", { exact: true });
  await expect(prompt).toHaveValue(/不能管理群/);
  await prompt.fill((await prompt.inputValue()) + "\n仅用于本地测试。");
  await page.getByRole("button", { name: "渲染预览", exact: true }).click();
  await expect(page.getByText(/预览.*未调用模型/)).toBeVisible();
  await mainNav("总览").click();
  await dialog("保存当前修改？")
    .getByRole("button", { name: "继续编辑" })
    .click();
  await page.getByRole("button", { name: "发布到当前群", exact: true }).click();
  await expect(
    page.getByText("已发布，从下一轮开始生效", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(0);

  await go("/observe/calls?scope=10000001:g:20000002&call=ui-call-141");
  await expect(dialog("模型调用详情")).toContainText(
    "今晚一起聊聊最近读过的书吧。",
  );
  await dialog("模型调用详情")
    .getByText("原始请求 JSON", { exact: true })
    .click();
  await expect(dialog("模型调用详情")).toContainText("max_tokens");
  await page.reload();
  await expect(dialog("模型调用详情")).toBeVisible();
  const exported = await page.request.get(
    base + "/api/v1/model-calls/ui-call-141/export",
  );
  expect(exported.ok()).toBeTruthy();
  expect((await exported.json()).request.messages).toHaveLength(2);
  await dialog("模型调用详情")
    .getByRole("button", { name: "关闭", exact: true })
    .click();
  await page.getByLabel("状态", { exact: true }).selectOption("failure");
  await expect(page).toHaveURL(/status=failure/);
  await expect(page.locator(".call-item").first()).toContainText("接口错误");
  await go("/observe/traces?trace=ui-trace-141");
  await expect(dialog("请求过程")).toContainText("上下文组装");
  await dialog("请求过程")
    .getByRole("button", { name: "查看本轮完整模型输入与返回" })
    .click();
  await expect(page).toHaveURL(/\/observe\/calls\?trace=ui-trace-141/);
  await expect(page.locator(".call-item")).toHaveCount(1);

  await go(group + "/access?view=rules");
  await page
    .getByText("账号授权模式下，普通成员默认可用工具", { exact: true })
    .click();
  const ownRecall = page.getByLabel("撤回本人消息（仍需开启群工具）", {
    exact: true,
  });
  await expect(ownRecall).toBeChecked();
  await ownRecall.uncheck();
  await page.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(0);
  await page.reload();
  await page
    .getByText("账号授权模式下，普通成员默认可用工具", { exact: true })
    .click();
  await expect(ownRecall).not.toBeChecked();
  await ownRecall.check();
  await page.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(0);
  await page.getByRole("link", { name: "成员授权", exact: true }).click();
  await page.getByLabel("添加 QQ 授权", { exact: true }).fill("30000001");
  await page.getByRole("button", { name: "添加到草稿", exact: true }).click();
  await page
    .getByRole("switch", { name: "禁止该账号使用工具", exact: true })
    .check();
  await page.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(0);
  await page.getByRole("button", { name: "恢复默认继承", exact: true }).click();
  await page.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(0);
  await go(group + "/members");
  const member = page.locator(".member-row").filter({ hasText: "小林" });
  await member.getByRole("button", { name: "停止并遗忘" }).click();
  await dialog("停止记忆并遗忘成员")
    .getByRole("button", { name: "停止并遗忘", exact: true })
    .click();
  await expect(member).toContainText("停止记忆");
  await member.getByRole("button", { name: "恢复记忆" }).click();
  await expect(member).toContainText("正常记忆");

  await go("/observe/diagnostics?scope=" + scope);
  await page.getByRole("button", { name: "运行固定样例", exact: true }).click();
  await expect(page.locator(".replay-score")).toContainText("7 / 7");
  // Expiry must redirect even when an editor contains a draft; no unauthenticated save attempt.
  await go(group + "/settings");
  await page.getByLabel("显示名称", { exact: true }).fill("会话过期草稿");
  await page.route("**/api/v1/groups/" + scope, (route) =>
    route.fulfill({ status: 401, json: { detail: "请重新登录" } }),
  );
  await page.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(page).toHaveURL(/\/login/);
  await page.unroute("**/api/v1/groups/" + scope);
  await page.getByLabel("密码", { exact: true }).fill("test-password-123456");
  await page.getByRole("button", { name: "登录工作台 →" }).click();
  await expect(page).toHaveURL(new RegExp("/groups/.+/settings"));
}

export async function exerciseMedia(page, base, publicMode = false) {
  let tests = 0;
  await page.goto(base + "/personas?persona=default");
  await expect(
    page.getByRole("heading", { name: "角色库", exact: true }),
  ).toBeVisible();
  await page.locator("summary").filter({ hasText: "角色参考图片" }).click();
  await expect(
    page.getByText("＋ 上传角色参考图", { exact: false }),
  ).toBeVisible();
  if (!publicMode) {
    const rows = await page.evaluate(async () =>
      (await fetch("/api/v1/personas/default/images")).json(),
    );
    const response = await page.request.get(
      base + `/api/v1/personas/default/images/${rows[0].id}/file`,
    );
    const buffer = await response.body();
    await page
      .getByRole("button", { name: "停用参考图", exact: true })
      .first()
      .click();
    await expect(
      page.getByRole("button", { name: "启用参考图", exact: true }),
    ).toBeVisible();
    await page.getByRole("button", { name: "启用参考图", exact: true }).click();
    await page
      .getByLabel("替换参考图 " + rows[0].label, { exact: true })
      .setInputFiles({
        name: "replacement.jpg",
        mimeType: "image/jpeg",
        buffer,
      });
    await expect(
      page.getByRole("button", { name: "停用参考图", exact: true }),
    ).toBeEnabled();
    await page
      .getByRole("button", { name: "删除参考图", exact: true })
      .first()
      .click();
    await expect(
      page.getByText("尚未上传角色参考图。", { exact: true }),
    ).toBeVisible();
    await page
      .getByLabel("上传角色参考图", { exact: true })
      .setInputFiles({ name: "setting.jpg", mimeType: "image/jpeg", buffer });
    await expect(
      page.getByRole("button", { name: "停用参考图", exact: true }),
    ).toBeVisible();
  }
  await page.goto(base + "/settings?section=images");
  const settings = await (
    await page.request.get(base + "/api/v1/settings")
  ).json();
  const imageLimit = page.getByLabel("单次请求图片上限", { exact: true });
  for (const [label, field] of [
    ["单次请求图片上限", "image_max_count"],
    ["视觉输入预算", "image_input_budget"],
    ["模型请求体上限", "image_request_mib"],
  ]) {
    await expect(page.getByLabel(label, { exact: true })).toHaveValue(
      String(settings[field]),
    );
  }
  if (!publicMode) {
    const changedLimit = settings.image_max_count === 5 ? "4" : "5";
    await imageLimit.fill(changedLimit);
    await page.getByRole("button", { name: "保存修改", exact: true }).click();
    await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(
      0,
    );
    await page.reload();
    await expect(imageLimit).toHaveValue(changedLimit);
    await imageLimit.fill(String(settings.image_max_count));
    await page.getByRole("button", { name: "保存修改", exact: true }).click();
    await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(
      0,
    );
  }
  await page.goto(base + "/configuration/image-search");
  const key = page.getByLabel("SerpApi API 密钥", { exact: true });
  await expect(key).toBeVisible();
  await expect(key).toHaveAttribute("type", "password");
  await expect(key).toHaveValue("");
  await expect(
    page.getByText("普通看图不自动搜图。", { exact: false }),
  ).toBeVisible();
  if (!publicMode) {
    const token = "synthetic-ui-serpapi-123456";
    await key.fill(token);
    await expect(
      page.getByRole("button", { name: "检查免费额度", exact: true }),
    ).toBeDisabled();
    await page.getByRole("button", { name: "保存修改", exact: true }).click();
    await expect(key).toHaveValue("");
    await expect(page.getByText("密钥已配置", { exact: true })).toBeVisible();
    const config = await (
      await page.request.get(base + "/api/v1/image-search/config")
    ).json();
    if (JSON.stringify(config).includes(token))
      throw Error("Secret was exposed");
    await page.reload();
    await expect(key).toHaveValue("");
    await expect(page.getByText("密钥已配置", { exact: true })).toBeVisible();
    await page.route("**/api/v1/image-search/account", (route) =>
      route.fulfill({
        json: {
          plan_name: "Free Plan",
          plan_searches_left: 250,
          this_month_usage: 0,
          free_eligible: true,
          checked_at: Date.now() / 1000,
        },
      }),
    );
    await page.route("**/api/v1/image-search/test", (route) => {
      tests++;
      return route.fulfill({
        json: {
          status: "results_found",
          ts: Date.now() / 1000,
          search_requests_this_run: 1,
          cached: false,
          notice: "合成测试结果，仅用于界面验证；不表示实际搜图命中。",
          matches: [
            {
              title: "合成画作来源",
              source: "测试页面",
              kind: "visual_matches",
              url: "https://example.org/art",
            },
          ],
          overview_status: "not_requested",
          overview_notice: "本次只发起一次搜索，未另行获取 AI 概览",
          overview: { text: "", references: [] },
        },
      });
    });
    await page
      .getByRole("button", { name: "检查免费额度", exact: true })
      .click();
    await expect(page.getByText("Free Plan", { exact: true })).toBeVisible();
    await page.getByLabel("选择测试图片", { exact: true }).setInputFiles({
      name: "synthetic.png",
      mimeType: "image/png",
      buffer: Buffer.from(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aOioAAAAASUVORK5CYII=",
        "base64",
      ),
    });
    await page
      .getByRole("button", { name: "使用免费额度验证图片", exact: true })
      .click();
    await expect(
      page.getByRole("link", { name: "合成画作来源", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("本次只发起一次搜索，未另行获取 AI 概览", { exact: true }),
    ).toBeVisible();
    if (tests !== 1) throw Error("Unexpected UI submission count");
  }
  if (!publicMode) {
    await page.getByLabel("清除已保存的 SerpApi 密钥", { exact: true }).check();
    await page.getByRole("button", { name: "保存修改", exact: true }).click();
    await expect(page.getByText("尚未配置密钥", { exact: true })).toBeVisible();
    await expect(
      page.getByRole("button", { name: "使用免费额度验证图片", exact: true }),
    ).toBeDisabled();
  }
  await page.unroute("**/api/v1/image-search/account");
  await page.unroute("**/api/v1/image-search/test");
}
