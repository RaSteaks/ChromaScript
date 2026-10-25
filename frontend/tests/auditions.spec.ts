import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

async function setup(page: Page, blind = true, picture = false) {
  const session = await (await page.request.get("/api/session")).json();
  async function call(name: string, data: unknown) {
    const reply = await page.request.post(`/api/operations/${name}`, {
      headers: { Authorization: `Bearer ${session.token}` },
      data,
    });
    expect(reply.ok(), await reply.text()).toBe(true);
    return reply.json();
  }
  const { project } = await call("create_project", {
    name: `试听 ${Date.now()}`,
  });
  const assets = [];
  for (const [name, frequency] of [
    ["来源一", 330],
    ["来源二", 660],
  ] as const) {
    assets.push(
      (
        await call("create_audio", {
          project_id: project.id,
          name,
          spec: {
            duration_seconds: 4,
            events: [
              { duration_seconds: 4, frequency_hz: frequency, gain_db: -18 },
            ],
          },
        })
      ).asset,
    );
  }
  const motion = picture
    ? await call("create_motion_scene", {
        project_id: project.id,
        spec: {
          engine: "remotion-sdr-v1",
          name: "配画面试听",
          width: 480,
          height: 270,
          frames: 180,
          fps: { numerator: 30, denominator: 1 },
          // A frame-dependent picture proves the external clock actually advances rendering.
          code: 'import {useCurrentFrame} from "remotion"; export default function Scene(){ const f=useCurrentFrame(); return <div style={{background:"#20282e",color:"#e3e6eb",width:"100%",height:"100%",display:"flex",flexDirection:"column",justifyContent:"center",padding:36,boxSizing:"border-box",fontFamily:"sans-serif"}}><div style={{fontSize:12,color:"#88bbc8"}}>第 4 章 · 配乐方向</div><div style={{fontSize:28,marginTop:12}}>留出阅读的空间</div><div style={{height:3,width:160+Math.sin(f/18)*40,background:"#88bbc8",marginTop:24}} /></div>; }',
        },
      })
    : null;
  const round = await call("create_audition", {
    project_id: project.id,
    title: "第一轮方向",
    purpose: "字幕阅读时保持安静",
    candidates: assets.map((a) => ({ asset_id: a.id })),
    duration_seconds: 3,
    blind,
    idempotency_key: crypto.randomUUID(),
    context: motion
      ? {
          work_kind: "motion",
          scene_id: motion.record.id,
          revision_id: motion.revision_id,
          start_frame: 30,
          frames: 90,
          fps: motion.scene.fps,
        }
      : null,
  });
  await page.goto("/");
  await page.getByLabel("当前项目", { exact: true }).selectOption(project.id);
  await page.getByRole("button", { name: "3. 声音", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "第一轮方向", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".audition-header .audition-status")).toHaveText(
    "待确认方向",
  );
  return { call, project, round, assets };
}

test("audition feedback conflict, drafts, direction and explicit timeline application", async ({
  page,
}, info) => {
  const { call, project, round } = await setup(page);
  const before = await call("get_project_audio", { project_id: project.id });
  const a = page.getByRole("button", { name: /^1 · A/ });
  const b = page.getByRole("button", { name: /^2 · B/ });
  await expect(a).toBeEnabled();
  await expect(b).toBeEnabled();
  await page.getByLabel("喜欢什么", { exact: true }).fill("保留音色，降低密度");
  // The comparison strip reflects the unsaved verdict of each candidate.
  await expect(a).toContainText("未保存");
  await b.click();
  await page.getByLabel("希望改什么", { exact: true }).fill("减少鼓组");
  await a.click();
  await expect(page.getByLabel("喜欢什么", { exact: true })).toHaveValue(
    "保留音色，降低密度",
  );
  await page.getByRole("button", { name: "4. 设置交付", exact: true }).click();
  await page.getByRole("button", { name: "3. 声音", exact: true }).click();
  await expect(page.getByLabel("喜欢什么", { exact: true })).toHaveValue(
    "保留音色，降低密度",
  );
  const common = { project_id: project.id, audition_id: round.id };
  const external = await call("save_audition_feedback", {
    ...common,
    expected_revision_id: round.revision_id,
    candidate_id: round.candidates[0].id,
    reviewer: "其他评审",
    feedback: { liked: "另一份反馈" },
  });
  await page.getByRole("button", { name: "保存反馈", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("草稿已保留");
  await expect(page.getByLabel("喜欢什么", { exact: true })).toHaveValue(
    "保留音色，降低密度",
  );
  expect((await call("get_audition", common)).revision_id).toBe(
    external.revision_id,
  );
  await page.getByRole("button", { name: "读取最新版本并保留草稿" }).click();
  await expect(
    page.getByRole("button", { name: "保存反馈", exact: true }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "保存反馈", exact: true }).click();
  await expect(
    page.getByText("反馈已保存，时间线未改变。", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: /本轮方向/ }).click();
  await page.getByLabel("结合多个", { exact: true }).check();
  await page.getByLabel("参考候选 A", { exact: true }).check();
  await expect(
    page.getByRole("button", { name: "确认制作方向", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByText("“结合多个”需要至少选 2 个参考候选。"),
  ).toBeVisible();
  // Per-reference notes are optional; saved feedback is the default reason.
  await page.getByText("补充说明（可选）").first().click();
  await page.getByLabel("参考 A 的哪些特点", { exact: true }).fill("音色");
  await page.getByLabel("参考候选 B", { exact: true }).check();
  await page
    .getByLabel("制作要求", { exact: true })
    .fill("保留 A 的音色、B 的节奏，降低打击乐密度。");
  await page.getByRole("button", { name: "确认制作方向", exact: true }).click();
  await expect(
    page.getByText("制作方向已确认；未发起生成，未改变时间线。", {
      exact: true,
    }),
  ).toBeVisible();
  expect((await call("get_audition", common)).direction_confirmed).toBe(true);
  expect(await call("get_project_audio", { project_id: project.id })).toEqual(
    before,
  );
  await page.getByRole("button", { name: "揭晓来源…", exact: true }).click();
  await page
    .getByRole("dialog", { name: "揭晓候选来源" })
    .getByRole("button", { name: "揭晓来源", exact: true })
    .click();
  await page.getByRole("button", { name: /逐个反馈/ }).click();
  await expect(
    page.getByRole("button", { name: "添加候选到时间线…" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "添加候选到时间线…" }).click();
  await page.getByRole("button", { name: "添加到音轨草稿" }).click();
  await expect(page.getByLabel("增益（dB）", { exact: true })).toBeVisible();
  expect(
    (await call("get_project_audio", { project_id: project.id })).revision_id,
  ).toBe(before.revision_id);
  await page.getByRole("button", { name: "保存音轨", exact: true }).click();
  await expect(
    page.getByText("音轨已保存，将用于此项目的新交付任务。", { exact: true }),
  ).toBeVisible();
  expect(
    (await call("get_project_audio", { project_id: project.id })).timeline
      .clips[0].audition_id,
  ).toBe(round.id);
  await page.getByRole("button", { name: "方向试听", exact: true }).click();
  await page.getByRole("button", { name: /^2 · B/ }).click();
  await expect(page.getByLabel("希望改什么", { exact: true })).toHaveValue(
    "减少鼓组",
  );
  await page.screenshot({
    path: info.outputPath("audition-desktop.png"),
    fullPage: true,
  });
});

test("audition device clock survives candidate switches, keyboard and stage changes", async ({
  page,
}, info) => {
  await page.addInitScript(() => {
    const counts = { starts: 0, frames: [] as number[] };
    window.addEventListener("message", (event) => {
      if (event.data?.type === "frame") counts.frames.push(event.data.value);
    });
    Object.assign(window, { auditionCounts: counts });
    const original = AudioBufferSourceNode.prototype.start;
    AudioBufferSourceNode.prototype.start = function (...args) {
      counts.starts++;
      return original.apply(this, args);
    };
  });
  const { call, project } = await setup(page, true, true);
  const a = page.getByRole("button", { name: /^1 · A/ });
  const b = page.getByRole("button", { name: /^2 · B/ });
  await expect(a).toBeEnabled();
  await expect(b).toBeEnabled();
  const iframe = page.locator('iframe[title="动画画面"]');
  await expect(iframe).toBeVisible({ timeout: 25000 });
  const channel = await iframe.getAttribute("srcdoc");
  await page.getByRole("button", { name: "播放试听", exact: true }).click();
  const time = page.getByLabel("试听位置（秒）", { exact: true });
  await expect.poll(() => time.inputValue()).not.toBe("0");
  await expect
    .poll(() =>
      page.evaluate(
        () =>
          new Set(
            (window as unknown as { auditionCounts: { frames: number[] } })
              .auditionCounts.frames,
          ).size,
      ),
    )
    .toBeGreaterThan(2);
  let previousTime = Number(await time.inputValue());
  await expect
    .poll(
      async () => {
        const value = Number(await time.inputValue());
        const wrapped = value < previousTime;
        previousTime = value;
        return wrapped;
      },
      { timeout: 4000 },
    )
    .toBe(true);
  const starts = await page.evaluate(
    () =>
      (window as unknown as { auditionCounts: { starts: number } })
        .auditionCounts.starts,
  );
  await b.click();
  await a.click();
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { auditionCounts: { starts: number } })
          .auditionCounts.starts,
    ),
  ).toBe(starts);
  expect(await iframe.getAttribute("srcdoc")).toBe(channel);
  await page.getByLabel("试听播放器", { exact: true }).focus();
  await page.keyboard.press("2");
  await expect(b).toHaveAttribute("aria-pressed", "true");
  // Space toggles the shared transport only while focus is on the player area.
  await page.keyboard.press(" ");
  await expect(
    page.getByRole("button", { name: "播放试听", exact: true }),
  ).toBeVisible();
  await page.getByLabel("喜欢什么", { exact: true }).fill("1");
  await expect(b).toHaveAttribute("aria-pressed", "true");
  await page.getByRole("button", { name: "1. 选择作品", exact: true }).click();
  await page.getByRole("button", { name: "3. 声音", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "播放试听", exact: true }),
  ).toBeVisible();
  expect(
    (await call("get_project_audio", { project_id: project.id })).revision_id,
  ).toBe("0");
  await page.screenshot({
    path: info.outputPath("audition-picture.png"),
    fullPage: true,
  });
});

test("audition accessibility, narrow layout, failed save and creation", async ({
  page,
}, info) => {
  await setup(page, false);
  await page.getByLabel("喜欢什么", { exact: true }).fill("断线保留");
  await page.route("**/api/operations/save_audition_feedback", (route) =>
    route.fulfill({
      status: 503,
      json: { error: { message: "保存暂时不可用" } },
    }),
  );
  await page.getByRole("button", { name: "保存反馈", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("保存暂时不可用");
  await expect(page.getByLabel("喜欢什么", { exact: true })).toHaveValue(
    "断线保留",
  );
  await page.unroute("**/api/operations/save_audition_feedback");
  await page.getByRole("button", { name: "新建试听", exact: true }).click();
  const modal = page.getByRole("dialog", { name: "新建方向试听" });
  await modal.getByLabel("轮次标题", { exact: true }).fill("界面创建轮次");
  await modal.getByLabel("试听时长（秒）", { exact: true }).fill("1");
  await modal.getByLabel(/来源一 ·/).check();
  await modal
    .getByRole("button", { name: "创建试听轮次", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "界面创建轮次", exact: true }),
  ).toBeVisible();
  // Preference is a native radio group: arrow keys move the choice.
  await expect(
    page.getByRole("radiogroup", { name: "候选偏好" }),
  ).toBeVisible();
  await page.getByLabel("暂不确定", { exact: true }).focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByLabel("不采用", { exact: true })).toBeChecked();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    (await new AxeBuilder({ page }).include(".sound-workspace").analyze())
      .violations,
  ).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath("audition-mobile.png"),
    fullPage: true,
  });
});
