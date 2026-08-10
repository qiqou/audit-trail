import fs from "node:fs";
import path from "node:path";

import { expect, test } from "@playwright/test";

test("V3 工作台保留三栏、分类持久化、复制和单份项目备份", async ({ page }, testInfo) => {
  test.setTimeout(45_000);
  const projectPath = path.join(testInfo.outputDir, "专项审计项目");
  fs.mkdirSync(testInfo.outputDir, { recursive: true });

  await page.goto("/");
  await expect(page.getByText("AUDIT TRAIL 1.0")).toBeVisible();
  await page.getByPlaceholder("使用人姓名").fill("自动化测试员");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await page.getByPlaceholder("项目文件夹完整路径").fill(projectPath);
  await page.getByPlaceholder("新建项目名称（打开已有项目时可留空）").fill("V3 自动化项目");
  await page.getByRole("button", { name: "新建项目" }).click();

  await expect(page.getByRole("heading", { name: "问题列表" })).toBeVisible();
  await expect(page.getByText("一页三栏工作方式")).toBeVisible();
  await expect(page.getByRole("button", { name: "📊 项目汇总" })).toBeVisible();
  await page.getByRole("button", { name: "新增单位" }).click();
  await page.locator(".el-message-box input").fill("测试单位");
  await page.getByRole("button", { name: "新增", exact: true }).click();
  await expect(page.getByText("测试单位", { exact: false })).toBeVisible();
  await page.getByTitle("在该单位新建底稿").click();
  await expect(page.getByText("底稿详情")).toBeVisible();
  await expect(page.getByText("本单位资料库（0）")).toBeVisible();
  await expect(page.getByRole("button", { name: "收起" })).toBeVisible();

  await page.getByRole("button", { name: "项目菜单 ▾" }).click();
  await page.getByRole("menuitem", { name: /版块与问题分类预设/ }).click();
  const settings = page.getByRole("dialog", { name: "版块与问题分类预设" });
  await settings.getByLabel("新增问题分类预设").fill("经营管理");
  await settings.getByRole("button", { name: "添加分类" }).click();
  await expect(settings.getByText("经营管理", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");

  // 刷新后重新打开同一项目，验证分类来自项目数据库，而非仅存在页面内存。
  await page.reload();
  await page.getByPlaceholder("项目文件夹完整路径").fill(projectPath);
  await page.getByRole("button", { name: "打开已有项目" }).click();
  await page.getByRole("button", { name: "项目菜单 ▾" }).click();
  await page.getByRole("menuitem", { name: /版块与问题分类预设/ }).click();
  await expect(page.getByRole("dialog", { name: "版块与问题分类预设" }).getByText("经营管理", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");

  await page.getByRole("button", { name: /复制/ }).click();
  const copyDialog = page.getByRole("dialog", { name: "复制底稿字段" });
  await expect(copyDialog.getByRole("radio", { name: /当前单位/ })).toBeVisible();
  await expect(copyDialog.getByRole("radio", { name: /当前底稿/ })).toBeVisible();
  await expect(copyDialog.getByRole("checkbox", { name: "被审计单位" })).toBeVisible();
  await expect(copyDialog.locator("pre")).toContainText("测试单位");
  await page.keyboard.press("Escape");

  const backupDir = path.dirname(projectPath);
  const before = fs.readdirSync(backupDir).filter((name) => name.endsWith(".auditbak"));
  await page.getByRole("button", { name: "项目菜单 ▾" }).click();
  await page.getByText("💾 创建项目备份", { exact: true }).click();
  const noDownload = page.waitForEvent("download", { timeout: 2_000 }).then(() => false).catch(() => true);
  await page.getByRole("button", { name: "创建 .auditbak 备份" }).click();
  await page.getByRole("button", { name: "创建备份", exact: true }).click();
  await expect(page.locator(".el-message__content")).toContainText("项目备份已创建");
  expect(await noDownload).toBe(true);
  const after = fs.readdirSync(backupDir).filter((name) => name.endsWith(".auditbak"));
  expect(after).toHaveLength(before.length + 1);
});
