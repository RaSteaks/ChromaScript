/** One shared SSE connection; events invalidate the existing operation reads. */
import { connect } from "./api";

type JobChange = { project_id: string; job_id: string; kind: string } | null;
type Listener = { change: (event: JobChange) => void; error: () => void };
const listeners = new Set<Listener>();
let source: EventSource | null = null;
let retryTimer: number | undefined;
let connecting = false;

function open() {
  if (listeners.size && !source) {
    // Same-site HttpOnly session cookies authenticate SSE without URL tokens.
    source = new EventSource("/api/job-events");
    source.onmessage = (event) => {
      const change = JSON.parse(event.data) as JobChange;
      for (const entry of listeners) entry.change(change);
    };
    source.onerror = () => {
      for (const entry of listeners) entry.error();
      source?.close();
      source = null;
      reconnect();
    };
  }
}

function reconnect() {
  if (!listeners.size || retryTimer !== undefined || connecting) return;
  retryTimer = window.setTimeout(async () => {
    retryTimer = undefined;
    connecting = true;
    try {
      // A daemon restart rotates credentials. Refresh the HttpOnly cookie and
      // operation token before reopening, then reconcile on the initial signal.
      await connect(AbortSignal.timeout(10000));
      open();
    } catch {
      // Only the failed notification connection retries; there is no list poll.
    } finally {
      connecting = false;
      if (!source) reconnect();
    }
  }, 1500);
}

function subscribe(listener: Listener) {
  listeners.add(listener);
  if (retryTimer === undefined && !connecting) open();
  return () => {
    listeners.delete(listener);
    if (!listeners.size) {
      source?.close();
      source = null;
      window.clearTimeout(retryTimer);
      retryTimer = undefined;
    }
  };
}

export function observeJobs(
  filter: { projectId: string; kind?: string; jobId?: string },
  read: (signal: AbortSignal) => Promise<void>,
  onError: (error: Error) => void,
): () => void {
  const controller = new AbortController();
  let timer: number | undefined;
  let reading = false;
  let pending = false;
  const schedule = (delay = 100) => {
    if (controller.signal.aborted) return;
    pending = true;
    if (!reading && timer === undefined)
      timer = window.setTimeout(() => void refresh(), delay);
  };
  const refresh = async () => {
    timer = undefined;
    reading = true;
    pending = false;
    let failed = false;
    try {
      await read(
        AbortSignal.any([controller.signal, AbortSignal.timeout(10000)]),
      );
    } catch (cause) {
      if (!controller.signal.aborted) {
        failed = true;
        onError(cause instanceof Error ? cause : new Error(String(cause)));
      }
    } finally {
      reading = false;
      // Coalesce progress bursts and changes arriving during a read. Timers
      // retry failed reads only; a healthy idle queue issues no repeat requests.
      if (failed) schedule(1500);
      else if (pending) schedule();
    }
  };
  const unsubscribe = subscribe({
    change: (event) => {
      if (
        event === null ||
        (event.project_id === filter.projectId &&
          (!filter.kind || event.kind === filter.kind) &&
          (!filter.jobId || event.job_id === filter.jobId))
      )
        schedule();
    },
    error: () => onError(new Error("任务通知连接中断，正在重新连接。")),
  });
  // Initial/page reads and reconnect reconciliation close the subscription race.
  schedule(0);
  return () => {
    controller.abort();
    window.clearTimeout(timer);
    unsubscribe();
  };
}
