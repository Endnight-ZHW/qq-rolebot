import { expect } from "@playwright/test";

export async function exercisePermissionScopes(page, base, scope) {
  const group = "/groups/" + encodeURIComponent(scope),
    url = base + "/api/v1/groups/" + scope + "/access";
  await page.goto(base + group + "/tools");
  await expect(page).toHaveURL(/access\?view=tools/);
  const original = await (await page.request.get(url)).json();
  const save = async () => {
    await page.getByRole("button", { name: "保存修改", exact: true }).click();
    await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(
      0,
    );
  };
  const open = async (title) => {
    await page
      .getByRole("button", { name: "配置" + title, exact: true })
      .click();
    return page.getByRole("dialog", {
      name: title + " · 工具配置",
      exact: true,
    });
  };
  // Pair hints never grant the companion tool, even in an unsaved draft.
  let muteDrawer = await open("成员禁言");
  await muteDrawer
    .getByRole("switch", { name: "启用成员禁言", exact: true })
    .check();
  await muteDrawer
    .getByRole("button", { name: "普通成员仅自己", exact: true })
    .click();
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("note", { name: "禁言与解禁配置提示" }),
  ).toContainText("解禁入口已关闭");
  let unmuteDrawer = await open("解除禁言");
  await expect(
    unmuteDrawer.getByRole("switch", { name: "启用解除禁言", exact: true }),
  ).not.toBeChecked();
  await unmuteDrawer
    .getByRole("switch", { name: "启用解除禁言", exact: true })
    .check();
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("note", { name: "禁言与解禁配置提示" }),
  ).toContainText("解禁仍沿用账号授权");
  expect(
    (await (await page.request.get(url)).json()).tools.member_mute,
  ).toEqual(original.tools.member_mute);
  for (const title of ["主页点赞", "群内戳一戳"]) {
    const drawer = await open(title);
    await expect(
      drawer.getByRole("switch", { name: "启用" + title, exact: true }),
    ).not.toBeChecked();
    await expect(
      drawer.getByRole("switch", { name: "允许机器人自主使用", exact: true }),
    ).toHaveCount(0);
    if (title === "主页点赞") {
      await drawer.getByLabel("单次点赞上限", { exact: true }).fill("30");
      await drawer.getByLabel("每日累计点赞上限", { exact: true }).fill("50");
      await expect(drawer).toContainText("超过建议值");
    }
    await page.keyboard.press("Escape");
  }
  let drawer = await open("设置群专属头衔");
  await drawer
    .getByRole("button", { name: "普通成员仅自己", exact: true })
    .click();
  await drawer.getByLabel("每小时最多调用", { exact: true }).fill("600");
  await expect(drawer.getByLabel("普通成员", { exact: true })).toHaveValue(
    "self",
  );
  await expect(drawer.getByLabel("管理员", { exact: true })).toHaveValue("all");
  await page.keyboard.press("Escape");
  expect(
    (await (await page.request.get(url)).json()).tools.member_title
      .request_access,
  ).toBeNull();
  // One draft survives internal navigation and saves tools, roles and members atomically.
  await page.getByRole("link", { name: "权限组", exact: true }).click();
  await page.getByLabel("权限组标识", { exact: true }).fill("console_review");
  await page.getByLabel("权限组名称", { exact: true }).fill("测试审核组");
  await page.getByRole("button", { name: "创建草稿", exact: true }).click();
  await page.getByLabel("查看状态", { exact: true }).check();
  await page.getByRole("link", { name: "成员授权", exact: true }).click();
  await page.getByLabel("添加 QQ 授权", { exact: true }).fill("30000002");
  await page.getByRole("button", { name: "添加到草稿", exact: true }).click();
  await page.getByLabel("测试审核组", { exact: true }).check();
  await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(1);
  await save();
  let snapshot = await (await page.request.get(url)).json();
  expect(snapshot.tools.member_title.max_per_hour).toBe(600);
  expect(snapshot.tools.profile_like.like_daily_limit).toBe(50);
  expect(snapshot.permissions.members["30000002"].roles).toContain(
    "console_review",
  );
  await page.getByRole("link", { name: "权限诊断", exact: true }).click();
  await page.getByLabel("诊断 QQ 号", { exact: true }).fill("30000002");
  await page.getByRole("button", { name: "检查权限", exact: true }).click();
  const row = page
    .locator(".diagnostic-row")
    .filter({ has: page.getByText("设置群专属头衔", { exact: true }) });
  await expect(row).toContainText("仅限自己");
  await row.locator("summary").click();
  await expect(row).toContainText("执行时核对");
  await expect(
    page.getByRole("note", { name: "禁言与解禁诊断提示" }),
  ).toContainText("解禁权限或范围不足");
  const muteRow = page
    .locator(".diagnostic-row")
    .filter({ has: page.getByText("成员禁言", { exact: true }) });
  await muteRow.locator("summary").click();
  await expect(muteRow).toContainText("有效最长禁言：600 秒");
  await expect(muteRow).toContainText("默认管理保护：90000001");
  await page.getByRole("link", { name: "工具授权", exact: true }).click();
  await page.getByLabel("搜索工具", { exact: true }).fill("头衔");
  await expect(page.locator(".access-row")).toHaveCount(1);
  await page.getByLabel("搜索工具", { exact: true }).fill("");
  drawer = await open("设置群专属头衔");
  await drawer.getByLabel("管理员", { exact: true }).selectOption("self");
  await page.keyboard.press("Escape");
  // Immediate control must not save the permission draft and must refresh its revision.
  await page
    .getByRole("button", { name: "紧急停用管理工具", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "恢复管理工具", exact: true }),
  ).toBeVisible();
  snapshot = await (await page.request.get(url)).json();
  expect(snapshot.tools.member_title.request_access.admin).toBe("all");
  await page.getByRole("button", { name: "恢复管理工具", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "紧急停用管理工具", exact: true }),
  ).toBeVisible();
  await save();
  // A real concurrent writer invalidates the group revision. Neither side is overwritten.
  drawer = await open("设置群专属头衔");
  await drawer.getByLabel("每小时最多调用", { exact: true }).fill("650");
  await page.keyboard.press("Escape");
  const csrf = (await (await page.request.get(base + "/api/v1/auth/me")).json())
    .csrf;
  snapshot = await (await page.request.get(url)).json();
  const changed = await page.request.patch(base + "/api/v1/groups/" + scope, {
    headers: { "X-CSRF-Token": csrf },
    data: { expected_revision: snapshot.group_revision, paused: true },
  });
  expect(changed.ok()).toBeTruthy();
  await page.getByRole("button", { name: "保存修改", exact: true }).click();
  await expect(page.locator(".conflict-panel")).toBeVisible();
  await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(1);
  drawer = await open("设置群专属头衔");
  await expect(
    drawer.getByLabel("每小时最多调用", { exact: true }),
  ).toHaveValue("650");
  await page.keyboard.press("Escape");
  await page
    .getByRole("button", { name: "放弃草稿并载入服务器版本", exact: true })
    .click();
  // Restore only the synthetic fixture with current revisions.
  snapshot = await (await page.request.get(url)).json();
  const restored = await page.request.put(url, {
    headers: { "X-CSRF-Token": csrf },
    data: {
      ...original,
      group_revision: snapshot.group_revision,
      permissions: {
        ...original.permissions,
        revision: snapshot.permissions.revision,
      },
    },
  });
  expect(restored.ok()).toBeTruthy();
  snapshot = await restored.json();
  await page.request.patch(base + "/api/v1/groups/" + scope, {
    headers: { "X-CSRF-Token": csrf },
    data: { expected_revision: snapshot.group_revision, paused: false },
  });
  await page.reload();
}
