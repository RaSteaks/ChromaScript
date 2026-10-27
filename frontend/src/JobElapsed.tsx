import { useEffect, useState } from "react";
import type { Job } from "./types";

/** The worker owns recorded duration; only running jobs interpolate between updates. */
export function JobElapsed({
  job,
  active = true,
}: {
  job: Job;
  active?: boolean;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active || job.status !== "running") return;
    const update = () => {
      if (!document.hidden) setNow(Date.now());
    };
    update();
    const timer = window.setInterval(update, 1000);
    document.addEventListener("visibilitychange", update);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", update);
    };
  }, [active, job.id, job.status]);

  // Retries enter `starting` with the previous attempt's elapsed field still
  // present. Start their visible clock at zero until the worker reports progress.
  const recorded =
    job.status === "running" && job.stage === "starting"
      ? 0
      : (job.elapsed_seconds ?? 0);
  const updated = job.updated_at ? Date.parse(job.updated_at) : NaN;
  const seconds = Math.floor(
    Math.max(0, recorded) +
      (job.status === "running" && Number.isFinite(updated)
        ? Math.max(0, now - updated) / 1000
        : 0),
  );
  const duration = [
    Math.floor(seconds / 3600),
    Math.floor(seconds / 60) % 60,
    seconds % 60,
  ]
    .map((part) => String(part).padStart(2, "0"))
    .join(":");
  return (
    <span
      className="mono job-elapsed"
      role="timer"
      aria-live="off"
      aria-label="任务渲染用时"
      title="本次任务用时，包含渲染、编码与验证，不含排队。运行中实时估算，结束后以任务记录为准。"
    >
      用时 {job.status === "queued" ? "—" : duration}
    </span>
  );
}
