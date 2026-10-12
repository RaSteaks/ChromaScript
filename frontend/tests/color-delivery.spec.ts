import { expect, test } from "@playwright/test";
import { mkdirSync } from "node:fs";

test("output color space and gamma stay linked through HDR, narrow layout and retry", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByRole("navigation", { name: "创作流程" })
    .getByRole("button", { name: "3. 设置交付", exact: true })
    .click();
  const target = page.getByRole("group", { name: "交付目标 1", exact: true });
  const space = target.getByLabel("输出色彩空间", { exact: true });
  const gamma = target.getByLabel("传递函数 / Gamma", { exact: true });
  await expect(space).toHaveValue("Rec.709");
  await expect(gamma).toHaveValue("gamma2.4");
  await expect(gamma.locator("option:checked")).toHaveText(
    "Gamma 2.4（BT.1886）",
  );
  await expect(
    target.getByLabel("目标显示峰值亮度", { exact: true }),
  ).toHaveValue("100 cd/m²（nit）");
  await expect(target.getByLabel("输出色彩", { exact: true })).toHaveCount(0);
  // Native popup ownership keeps standard focus/Escape behavior.
  await gamma.focus();
  await page.keyboard.press("Space");
  await page.keyboard.press("Escape");
  await expect(gamma).toBeFocused();
  await target
    .getByLabel("视频 / 图像编码", { exact: true })
    .selectOption("hevc");
  await space.selectOption("Rec.2020");
  await expect(gamma).toHaveValue("st2084");
  await expect(gamma.locator("option:checked")).toHaveText(
    "PQ（SMPTE ST 2084）",
  );
  await expect(
    target.getByLabel("目标显示峰值亮度", { exact: true }),
  ).toHaveValue("1000 cd/m²（nit）");
  await expect(
    target.getByLabel("目标显示峰值亮度", { exact: true }),
  ).toHaveAttribute("readonly", "");
  await expect(target.getByLabel("位深", { exact: true })).toHaveValue("10");
  await gamma.selectOption("hlg");
  await expect(space).toHaveValue("Rec.2020");
  await expect(gamma).toBeEnabled();
  await expect(gamma.locator("option:checked")).toHaveText(
    "HLG（ARIB STD-B67）",
  );
  await expect(gamma.locator("option")).not.toContainText(["1000 nit"]);
  mkdirSync("../reports", { recursive: true });
  await page.screenshot({
    path: "../reports/delivery-color-gamma.png",
    fullPage: true,
  });
  // Capture gamut, transfer, target luminance and data range as separate decisions.
  await target.getByLabel("数据范围", { exact: true }).scrollIntoViewIfNeeded();
  const colorRegion = await gamma.evaluate((element) => {
    const group = element.closest(".delivery-fields")!;
    const spaceField = group
      .querySelector('[id$="-color_space"]')!
      .closest(".field")!;
    const rangeField = group
      .querySelector('[id$="-data_range"]')!
      .closest(".field")!;
    const first = spaceField.getBoundingClientRect();
    const last = rangeField.getBoundingClientRect();
    return {
      x: first.x - 12,
      y: first.y - 40,
      width: first.width + 24,
      height: last.bottom - first.top + 52,
    };
  });
  await page.screenshot({
    path: "../reports/delivery-color-gamma-controls.png",
    clip: colorRegion,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(space).toHaveValue("Rec.2020");
  await expect(gamma).toHaveValue("hlg");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "../reports/delivery-color-gamma-mobile.png",
    fullPage: true,
  });
  await space.selectOption("Rec.709");
  await expect(gamma).toHaveValue("gamma2.4");
  await gamma.selectOption("srgb");
  await expect(space).toHaveValue("Rec.709");

  // A failed capability request preserves the edited gamma until an explicit retry.
  await page.route("**/api/operations/get_delivery_options", (route) =>
    route.abort("failed"),
  );
  await gamma.selectOption("gamma2.4");
  await expect(target.getByRole("alert")).toBeVisible();
  await expect(gamma).toHaveValue("gamma2.4");
  await page.unroute("**/api/operations/get_delivery_options");
  await target
    .getByRole("button", { name: "重试联动选项", exact: true })
    .click();
  await expect(target.getByRole("alert")).not.toBeVisible();
  await expect(gamma).toBeEnabled();
  await expect(gamma).toHaveValue("gamma2.4");
});

test("linear EXR uses no display luminance while image formats retain their encoding", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "3. 设置交付", exact: true }).click();
  const target = page.getByRole("group", { name: "交付目标 1", exact: true });
  const container = target.getByLabel("封装 / 图像格式", { exact: true });
  await container.selectOption("exr");
  await expect(
    target.getByLabel("传递函数 / Gamma", { exact: true }),
  ).toHaveValue("linear");
  await expect(
    target.getByLabel("目标显示峰值亮度", { exact: true }),
  ).toHaveValue("不适用（场景线性）");
  await expect(target.locator(".color-encoding-summary")).toContainText("D60");
  await container.selectOption("png");
  await expect(
    target.getByLabel("传递函数 / Gamma", { exact: true }),
  ).toHaveValue("srgb");
  await expect(target.locator(".color-encoding-summary")).toContainText(
    "IEC 61966-2-1",
  );
  await expect(target.locator(".color-encoding-summary")).toContainText("D65");
});

test("requested gamuts and Full Limited ranges are real linked choices", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "3. 设置交付", exact: true }).click();
  const target = page.getByRole("group", { name: "交付目标 1", exact: true });
  const space = target.getByLabel("输出色彩空间", { exact: true });
  const range = target.getByLabel("数据范围", { exact: true });
  for (const value of ["P3-D65", "Rec.709", "Rec.2020", "ACEScct", "ACEScg"])
    await expect(space.locator(`option[value="${value}"]`)).toHaveCount(1);
  await expect(range.locator('option[value="full"]')).toHaveText(
    "Full（全范围）",
  );
  await expect(range.locator('option[value="limited"]')).toHaveText(
    "Limited（视频范围）",
  );
  await space.selectOption("P3-D65");
  await expect(
    target.getByLabel("传递函数 / Gamma", { exact: true }),
  ).toHaveValue("srgb");
  await range.selectOption("full");
  await expect(range).toHaveValue("full");
  // Save the actual P3/Full controls as visual evidence alongside the native file tests.
  await range.scrollIntoViewIfNeeded();
  const region = await range.evaluate((element) => {
    const group = element.closest(".delivery-fields")!;
    const first = group
      .querySelector('[id$="-color_space"]')!
      .closest(".field")!
      .getBoundingClientRect();
    const last = element.closest(".field")!.getBoundingClientRect();
    return {
      x: first.x - 12,
      y: first.y - 40,
      width: first.width + 24,
      height: last.bottom - first.top + 52,
    };
  });
  await page.screenshot({
    path: "../reports/gamut-range-controls.png",
    clip: region,
  });
  await range.selectOption("limited");
  await expect(range).toHaveValue("limited");
  await space.selectOption("ACEScct");
  await expect(target.getByLabel("位深", { exact: true })).toHaveValue("10");
  await expect(
    target.getByLabel("传递函数 / Gamma", { exact: true }),
  ).toHaveValue("acescct");
  await expect(
    target.getByLabel("目标显示峰值亮度", { exact: true }),
  ).toHaveValue("不适用（ACEScct 对数）");
  await target
    .getByLabel("封装 / 图像格式", { exact: true })
    .selectOption("exr");
  await expect(range).toHaveValue("full");
  await expect(range.locator('option[value="limited"]')).toBeDisabled();
  await space.selectOption("ACEScg");
  await expect(
    target.getByLabel("传递函数 / Gamma", { exact: true }),
  ).toHaveValue("linear");
  await page.setViewportSize({ width: 390, height: 844 });
  await space.click();
  await page.keyboard.press("Escape");
  await expect(space).toBeFocused();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("wide gamut delivery retains a separate sRGB browser preview", async ({
  page,
}) => {
  await page.goto("/");
  // Preview owns shared canvas settings; delivery consumes those same values.
  await page
    .getByRole("button", { name: "2. 编辑与预览", exact: true })
    .click();
  await page.getByLabel("宽度", { exact: true }).fill("64");
  await page.getByLabel("高度", { exact: true }).fill("64");
  await page.getByLabel("总帧数", { exact: true }).fill("2");
  await page.getByRole("button", { name: "3. 设置交付", exact: true }).click();
  await page.getByLabel("输出色彩空间", { exact: true }).selectOption("P3-D65");
  await page.getByRole("button", { name: /^提交交付/ }).click();
  await page
    .getByRole("button", { name: "查看画面预览", exact: true })
    .click({ timeout: 20000 });
  await expect(page.locator(".preview-stage img")).toBeVisible();
  await expect(
    page.getByRole("button", { name: "运动预览", exact: true }),
  ).toBeDisabled();
});
