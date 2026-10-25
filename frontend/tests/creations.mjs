import { expect } from "@playwright/test";

export async function exerciseArtLibrary(page,base){
  await page.goto(base+'/tasks?view=assets');
  await expect(page.getByRole('heading',{name:'美术素材',exact:true})).toBeVisible();
  const encoded=await page.evaluate(()=>{const c=document.createElement('canvas');c.width=c.height=32;const ctx=c.getContext('2d');ctx.fillStyle='#2378bd';ctx.fillRect(8,8,16,16);return c.toDataURL('image/png').split(',')[1]});
  await page.getByLabel('素材文件',{exact:true}).setInputFiles({name:'geometry.png',mimeType:'image/png',buffer:Buffer.from(encoded,'base64')});
  await page.getByLabel('素材名称',{exact:true}).fill('隔离测试素材');
  await page.getByLabel('素材类别',{exact:true}).selectOption('decoration');
  await page.getByLabel('绑定角色',{exact:true}).selectOption('');
  await page.getByLabel('素材用途与检索词',{exact:true}).fill('轻松心情点缀');
  await page.getByRole('button',{name:'加入素材',exact:true}).click();
  let card=page.locator('article').filter({has:page.getByRole('heading',{name:'隔离测试素材',exact:true})});
  await expect(card).toBeVisible();
  const toggle=page.getByRole('checkbox',{name:'启用 隔离测试素材',exact:true});
  const saved=()=>page.waitForResponse(r=>r.url().includes('/creation-assets/')&&r.request().method()==='PUT'&&r.ok());
  await Promise.all([saved(),toggle.uncheck()]);
  await page.reload();await expect(toggle).not.toBeChecked();await Promise.all([saved(),toggle.check()]);
  await card.getByRole('button',{name:'编辑说明'}).click();
  const edit=page.getByRole('dialog',{name:'编辑美术素材'});
  await edit.getByLabel('名称',{exact:true}).fill('隔离素材已修改');
  await edit.getByRole('button',{name:'保存素材说明'}).click();await expect(edit).toHaveCount(0);
  await page.reload();card=page.locator('article').filter({has:page.getByRole('heading',{name:'隔离素材已修改',exact:true})});
  await expect(card).toBeVisible();await card.getByRole('button',{name:'删除素材',exact:true}).click();
  await page.getByRole('dialog',{name:'删除素材'}).getByRole('button',{name:'删除',exact:true}).click();
  await expect(card).toHaveCount(0);
}

export async function exerciseCreations(page, base) {
  await page.goto(base + "/tasks?view=creations");
  await expect(
    page.getByText("中文对比图 · sent", { exact: true }),
  ).toBeVisible();
  await page
    .locator("article")
    .filter({ hasText: "中文对比图 · sent" })
    .getByRole("button", { name: "查看作品" })
    .click();
  const dialog = page.getByRole("dialog", { name: "作品制作详情" });
  await expect(dialog.getByRole("img")).toBeVisible();
  await dialog.getByText("制作资料与源码（仅文本）", { exact: true }).click();
  await expect(dialog.locator("pre")).toContainText(
    "window.creationSourceExecuted=true",
  );
  expect(
    await page.evaluate(() => window.creationSourceExecuted),
  ).toBeUndefined();
  await dialog.getByRole("button", { name: "收藏作品", exact: true }).click();
  await expect(
    dialog.getByRole("button", { name: "取消收藏", exact: true }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "关闭", exact: true }).last().click();
  await page
    .locator("article")
    .filter({ hasText: "中文对比图 · queued" })
    .getByRole("button", { name: "取消制作", exact: true })
    .click();
  await expect(
    page.locator("article").filter({ hasText: "中文对比图 · queued" }),
  ).toContainText("已取消");
  await page.getByRole("button", { name: "制作设置", exact: true }).click();
  await page.getByLabel("每群每小时请求次数", { exact: true }).fill("24");
  for (const [label,value] of [
    ["制作上下文总预算","256000"],
    ["作品总配额（MiB）","10240"],["收藏配额（MiB）","4096"],
    ["等待队列容量","8"],["最长排队时间（秒）","600"],["最长制作时间（秒）","600"],
    ["每群每小时主动制作次数","4"],["主动制作间隔（秒）","300"],
  ]) await page.getByLabel(label,{exact:true}).fill(value);
  await page.getByRole("button", { name: "保存制作设置", exact: true }).click();
  await expect(page.getByRole("region", { name: "未保存修改" })).toHaveCount(0);
  await page.reload();
  await page.getByRole("button", { name: "制作设置", exact: true }).click();
  await expect(
    page.getByLabel("每群每小时请求次数", { exact: true }),
  ).toHaveValue("24");
  await expect(page.getByLabel("最长制作时间（秒）",{exact:true})).toHaveValue("600");
  await expect(page.getByLabel("制作上下文总预算",{exact:true})).toHaveValue("256000");
  await expect(page.getByLabel("等待队列容量",{exact:true})).toHaveValue("8");
  await page.getByLabel("每群每小时请求次数", { exact: true }).fill("21");
  await page.getByRole("link", { name: "定时任务", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  await expect(page).toHaveURL(/view=creations/);
  await page.getByRole("button", { name: "保存制作设置", exact: true }).click();
}
