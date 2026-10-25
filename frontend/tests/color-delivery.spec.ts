// Five-stage navigation includes Sound before delivery.
import { expect, test } from "@playwright/test";
import { mkdirSync } from "node:fs";
import type { Artifact, Job, SceneReply } from "../src/types";

test("motion preview requires verified sRGB pixels and tags regardless of filename", async ({
  page,
}) => {
  const session = await (await page.request.get("/api/session")).json();
  async function call<T>(name: string, data: unknown): Promise<T> {
    const response = await page.request.post("/api/operations/" + name, {
      headers: { Authorization: "Bearer " + session.token },
      data,
    });
    expect(response.ok()).toBe(true);
    return response.json();
  }
  const { project } = await call<{ project: { id: string } }>(
    "create_project",
    { name: "运动预览色彩验证 " + Date.now() },
  );
  const scene = await call<SceneReply>("create_scene", {
    project_id: project.id,
    template_id: "gradient",
  });
  const { job } = await call<{ job: Job }>("render_preview", {
    scene_id: scene.record.id,
    settings: { width: 32, height: 32, frames: 2, samples: 1 },
  });
  await expect
    .poll(
      async () =>
        (await call<{ job: Job }>("get_job", { job_id: job.id })).job.status,
      { timeout: 20000 },
    )
    .toBe("completed");
  const completed = await call<{ job: Job; artifacts: Artifact[] }>("get_job", {
    job_id: job.id,
  });
  const video = completed.artifacts.find((a) => a.kind === "video")!;
  const verified = video.metadata.verification as {
    passed: boolean;
    video: Record<string, string>;
    color_encoding: Record<string, string>;
  };
  expect(verified.passed).toBe(true);
  expect(verified.video.color_transfer).toBe("iec61966-2-1");
  expect(verified.color_encoding.gamma).toBe("srgb");

  // Only metadata is varied after a real GPU/encoder run, covering old artifacts
  // and mismatched claims without ever treating a filename as color evidence.
  const variants = [
    { name: "independent-srgb.mp4", verification: verified, playable: true },
    { name: "h264-review.mp4", verification: undefined, playable: false },
    {
      name: "h264-review.mp4",
      verification: {
        ...verified,
        video: { ...verified.video, color_transfer: "bt709" },
        color_encoding: {
          ...verified.color_encoding,
          gamma: "gamma2.4",
          output_transform: "Rec.709",
        },
      },
      playable: false,
    },
    {
      name: video.name,
      verification: {
        ...verified,
        color_encoding: { ...verified.color_encoding, gamma: "gamma2.2" },
      },
      playable: false,
    },
    {
      name: video.name,
      verification: {
        ...verified,
        video: { ...verified.video, color_transfer: "bt709" },
      },
      playable: false,
    },
    {
      name: video.name,
      verification: { ...verified, passed: false },
      playable: false,
    },
  ];
  let variant = variants[0];
  await page.route("**/api/operations/get_job", (route) => {
    if (route.request().postDataJSON().job_id !== job.id)
      return route.continue();
    return route.fulfill({
      json: {
        ...completed,
        artifacts: completed.artifacts.map((a) =>
          a.id === video.id
            ? {
                ...a,
                name: variant.name,
                metadata: { ...a.metadata, verification: variant.verification },
              }
            : a,
        ),
      },
    });
  });
  for (variant of variants) {
    await page.goto("/");
    await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
    await page
      .getByRole("button", { name: "5. 任务与成果", exact: true })
      .click();
    await page
      .getByRole("button", { name: "查看画面预览", exact: true })
      .click();
    const motion = page.getByRole("button", { name: "运动预览", exact: true });
    if (variant.playable) {
      await expect(motion).toBeEnabled();
      await motion.click();
      await expect(page.locator(".preview-stage video")).toHaveAttribute(
        "src",
        "/api/artifacts/" + video.id,
      );
      // Confirm Chrome can decode the real sRGB MP4, beyond merely mounting a URL.
      await expect
        .poll(() =>
          page
            .locator(".preview-stage video")
            .evaluate((element: HTMLVideoElement) => element.readyState),
        )
        .toBeGreaterThanOrEqual(2);
    } else {
      await expect(motion).toBeDisabled();
      await expect(page.locator(".preview-stage video")).toHaveCount(0);
    }
  }
});

test("output color space and gamma stay linked through HDR, narrow layout and retry", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByRole("navigation", { name: "创作流程" })
    .getByRole("button", { name: "4. 设置交付", exact: true })
    .click();
  const target = page.getByRole("group", { name: "交付目标 1", exact: true });
  const space = target.getByLabel("输出色彩空间", { exact: true });
  const gamma = target.getByLabel("传递函数 / Gamma", { exact: true });
  await expect(space).toHaveValue("Rec.709");
  await expect(gamma).toHaveValue("srgb");
  await expect(gamma.locator("option:checked")).toHaveText(
    "sRGB（IEC 61966-2-1）",
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
  await target.getByLabel("数据级别", { exact: true }).scrollIntoViewIfNeeded();
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
  await expect(gamma).toHaveValue("srgb");
  await gamma.selectOption("srgb");
  await expect(space).toHaveValue("Rec.709");

  // A failed capability request preserves the edited gamma until an explicit retry.
  await page.route("**/api/operations/get_delivery_options", (route) =>
    route.abort("failed"),
  );
  await gamma.selectOption("gamma2.2");
  await expect(target.getByRole("alert")).toBeVisible();
  await expect(gamma).toHaveValue("gamma2.2");
  await page.unroute("**/api/operations/get_delivery_options");
  await target
    .getByRole("button", { name: "重试联动选项", exact: true })
    .click();
  await expect(target.getByRole("alert")).not.toBeVisible();
  await expect(gamma).toBeEnabled();
  await expect(gamma).toHaveValue("gamma2.2");
});

test("linear EXR uses no display luminance while image formats retain their encoding", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "4. 设置交付", exact: true }).click();
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

test("image gamut menus keep unavailable choices discoverable", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "4. 设置交付", exact: true }).click();
  const target = page.getByRole("group", { name: "交付目标 1", exact: true });
  for (const viewport of [
    { width: 1440, height: 1000 },
    { width: 390, height: 844 },
  ]) {
    await page.setViewportSize(viewport);
    for (const container of ["png", "tiff", "dpx"]) {
      await target
        .getByLabel("封装 / 图像格式", { exact: true })
        .selectOption(container);
      // Wait for the linked response, rather than inspecting the old menu while pending.
      await expect(
        target.getByLabel("视频 / 图像编码", { exact: true }),
      ).toHaveValue(container);
      const space = target.getByLabel("输出色彩空间", { exact: true });
      await expect(space).toBeEnabled();
      await expect(space.locator("option")).toHaveCount(6);
      await expect(space.locator("option:not([disabled])")).toHaveCount(1);
      await expect(space.locator('option[value="ACEScg"]')).toBeDisabled();
      await expect(space.locator('option[value="ACEScg"]')).toContainText(
        "EXR",
      );
      // Opening a read-oriented native menu must remain possible with mouse/keyboard.
      await space.click();
      await page.keyboard.press("Escape");
      await expect(space).toBeFocused();
      await page.keyboard.press("Space");
      await page.keyboard.press("Escape");
      await expect(space).toBeFocused();
      await expect(space).toHaveValue("Rec.709");
      await expect(
        target.getByLabel("传递函数 / Gamma", { exact: true }),
      ).toBeDisabled();
    }
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
  }
});

test("requested gamuts and Auto Video Full levels are real linked choices", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: "4. 设置交付", exact: true }).click();
  const target = page.getByRole("group", { name: "交付目标 1", exact: true });
  const space = target.getByLabel("输出色彩空间", { exact: true });
  const range = target.getByLabel("数据级别", { exact: true });
  await expect(range).toHaveValue("auto");
  for (const value of ["P3-D65", "Rec.709", "Rec.2020", "ACEScct", "ACEScg"])
    await expect(space.locator(`option[value="${value}"]`)).toHaveCount(1);
  await expect(range.locator('option[value="full"]')).toHaveText(
    "Full（全范围）",
  );
  await expect(range.locator('option[value="limited"]')).toHaveText(
    "Video（Limited，视频范围）",
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
  // AP1/log video lacks native identifiers; the same encoding remains available in EXR.
  await expect(space.locator('option[value="ACEScct"]')).toBeDisabled();
  await target
    .getByLabel("封装 / 图像格式", { exact: true })
    .selectOption("exr");
  await space.selectOption("ACEScct");
  await expect(target.getByLabel("位深", { exact: true })).toHaveValue("16");
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
  await page.getByRole("button", { name: "4. 设置交付", exact: true }).click();
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

test("Resolve-style ProRes delivery keeps P3 gamma independent and freezes Auto levels", async ({
  page,
}) => {
  const session = await (await page.request.get("/api/session")).json();
  const headers = { Authorization: "Bearer " + session.token };
  const created = await page.request.post("/api/operations/create_project", {
    headers,
    data: { name: "Resolve 交付验证 " + Date.now() },
  });
  expect(created.ok()).toBe(true);
  const { project } = await created.json();
  const scene = await page.request.post("/api/operations/create_scene", {
    headers,
    data: { project_id: project.id, template_id: "gradient" },
  });
  expect(scene.ok()).toBe(true);
  await page.goto("/");
  await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
  await page.getByRole("button", { name: "4. 设置交付", exact: true }).click();
  const target = page.getByRole("group", { name: "交付目标 1", exact: true });
  const field = (name: string) => target.getByLabel(name, { exact: true });
  await field("封装 / 图像格式").selectOption("mov");
  await field("视频 / 图像编码").selectOption("prores");
  await expect(field("编码档位").locator("option:checked")).toHaveText(
    "422 HQ",
  );
  await field("输出色彩空间").selectOption("P3-D65");
  const gamma = field("传递函数 / Gamma");
  // Preserve the compatible sRGB transfer and require exact native color tags.
  await expect(gamma).toHaveValue("srgb");
  for (const value of ["gamma2.2", "srgb", "st2084"])
    await expect(gamma.locator(`option[value="${value}"]`)).toBeEnabled();
  await expect(field("色彩空间标签")).toHaveValue("P3-D65");
  // Unspecified TRC and comment declarations cannot satisfy native-tag policy.
  await expect(field("Gamma 标签")).toHaveValue("sRGB");
  await expect(field("Gamma 标签")).toHaveAttribute("readonly", "");
  await expect(field("数据级别")).toHaveValue("auto");
  await expect(target).toContainText(
    "Auto 按当前格式使用 Video（Limited，视频范围）",
  );
  await gamma.focus();
  await page.keyboard.press("Space");
  await page.keyboard.press("Escape");
  await expect(gamma).toBeFocused();
  await expect(gamma.locator('option[value="gamma2.6"]')).toBeDisabled();
  await gamma.selectOption("gamma2.2");
  await expect(field("Gamma 标签")).toHaveValue("Gamma 2.2");
  await expect(field("目标显示峰值亮度")).toHaveValue("100 cd/m²（nit）");
  mkdirSync("../reports", { recursive: true });
  await page.screenshot({
    path: "../reports/resolve-delivery-desktop.png",
    fullPage: true,
  });
  await gamma.selectOption("srgb");
  await expect(target).toContainText("P3-D65 + sRGB 为 Display P3");
  await expect(gamma.locator('option[value="gamma2.4"]')).toBeDisabled();
  await field("封装 / 图像格式").selectOption("mp4");
  await expect(field("输出色彩空间")).toHaveValue("P3-D65");
  await expect(gamma).toHaveValue("srgb");
  await expect(gamma.locator('option[value="gamma2.4"]')).toBeDisabled();
  await expect(gamma.locator('option[value="gamma2.6"]')).toBeDisabled();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await expect(field("色彩空间标签")).toHaveValue("P3-D65");
  // Wait for the codec resolver: the gamut/transfer no longer change as an incidental signal.
  await expect(field("视频 / 图像编码")).toHaveValue("h264");
  await expect(field("编码档位")).toHaveValue("high422");
  // Auto and explicit Video freeze to the same MP4; only Full makes it distinct.
  await page.getByRole("button", { name: "添加交付目标", exact: true }).click();
  const second = page.getByRole("group", { name: "交付目标 2", exact: true });
  // New targets start with their own defaults, so align profile, sampling and color
  // before comparing Auto against the equivalent explicit Video range.
  for (const label of ["编码档位", "位深", "色度采样"])
    await second
      .getByLabel(label, { exact: true })
      .selectOption(await field(label).inputValue());
  await second
    .getByLabel("输出色彩空间", { exact: true })
    .selectOption("P3-D65");
  await second
    .getByLabel("传递函数 / Gamma", { exact: true })
    .selectOption("srgb");
  await second.getByLabel("数据级别", { exact: true }).selectOption("limited");
  await expect(
    page.getByText("交付设置重复，请调整或移除重复目标。", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /^提交交付/ })).toBeDisabled();
  await second.getByLabel("数据级别", { exact: true }).selectOption("full");
  await expect(
    page.getByText("交付设置重复，请调整或移除重复目标。", { exact: true }),
  ).not.toBeVisible();
});
