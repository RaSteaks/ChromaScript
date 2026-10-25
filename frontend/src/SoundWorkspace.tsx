import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Archive, ChevronLeft, ChevronRight, Eye, Plus } from "lucide-react";
import { ApiError, call } from "./api";
import { Button, Field, Modal, Notice, Textarea } from "./components";
import { ProjectAudio, type AudioClip } from "./ProjectAudio";
import { AuditionPlayer, type CandidateMark } from "./AuditionPlayer";
import {
  emptyDirection,
  emptyFeedback,
  type Audition,
  type Direction,
  type Feedback,
} from "./auditions";

type Work = {
  work_kind: "wgsl" | "motion";
  scene_id: string;
  revision_id: string;
};
type Asset = { id: string; name: string; duration_seconds: number };
type Page = { items: Audition[]; total: number };
const describe = (error: unknown) =>
  error instanceof Error ? error.message : String(error);
const PAGE = 20;
const PREFERENCES = [
  ["reference", "作为参考"],
  ["unsure", "暂不确定"],
  ["reject", "不采用"],
] as const;
const SCORES = [
  ["reading", "不干扰字幕阅读"],
  ["quality", "品质感"],
  ["emotion", "情绪适配"],
  ["concept", "概念适配"],
  ["repeat", "耐听"],
] as const;
const roundStatus = (round: Audition) =>
  round.archived
    ? (["archived", "已归档"] as const)
    : round.direction_confirmed
      ? (["confirmed", "已确认方向"] as const)
      : (["pending", "待确认方向"] as const);

/** Segmented native radio group: one click to choose, arrow keys within it. */
function Choice<T extends string>({
  label,
  name,
  value,
  options,
  onChange,
  compact = false,
}: {
  label: string;
  name: string;
  value: T;
  options: readonly (readonly [T, string])[];
  onChange: (value: T) => void;
  compact?: boolean;
}) {
  return (
    <div
      role="radiogroup"
      aria-labelledby={name + "-label"}
      className={"choice" + (compact ? " compact" : "")}
    >
      <span id={name + "-label"} className="choice-label">
        {label}
      </span>
      <span className="choice-options">
        {options.map(([option, text]) => (
          <label
            key={option}
            className={value === option ? "selected" : undefined}
          >
            <input
              type="radio"
              name={name}
              value={option}
              checked={value === option}
              onChange={() => onChange(option)}
            />
            <span>{text}</span>
          </label>
        ))}
      </span>
    </div>
  );
}

/** A project session stays mounted across stages. Draft maps belong to rounds
 * and reviewers, not whichever candidate/form happens to be visible. */
export function SoundWorkspace({
  projectId,
  active,
  work,
  onDirty,
}: {
  projectId: string;
  active: boolean;
  work: Work | null;
  onDirty: (dirty: boolean) => void;
}) {
  const [tab, setTab] = useState<"auditions" | "timeline" | "assets">(
    "auditions",
  );
  const [rounds, setRounds] = useState<Page>({ items: [], total: 0 });
  const [records, setRecords] = useState<Record<string, Audition>>({});
  const [selectedRound, setSelectedRound] = useState("");
  const [selected, setSelected] = useState("");
  const [reviewer, setReviewer] = useState("我");
  const [page, setPage] = useState(0);
  const [archived, setArchived] = useState(false);
  const [loading, setLoading] = useState(false);
  const [feedbackDrafts, setFeedbackDrafts] = useState<
    Record<string, Feedback>
  >({});
  const [directionDrafts, setDirectionDrafts] = useState<
    Record<string, Direction>
  >({});
  const [timelineDirty, setTimelineDirty] = useState(false);
  const [create, setCreate] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [notice, setNotice] = useState("");
  const [review, setReview] = useState<"feedback" | "direction">("feedback");
  const [revealAsk, setRevealAsk] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [external, setExternal] = useState(false);
  const [addition, setAddition] = useState<AudioClip | null>(null);
  const [apply, setApply] = useState(false);
  const [historyPreview, setHistoryPreview] = useState<Audition | null>(null);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [applyStart, setApplyStart] = useState("0");
  const [applyGain, setApplyGain] = useState("0");
  const busy = useRef(false);
  const epoch = useRef(0);
  const recordsRef = useRef(records);
  recordsRef.current = records;
  const latest = useRef({ feedbackDrafts, directionDrafts, selectedRound });
  latest.current = { feedbackDrafts, directionDrafts, selectedRound };
  const round = records[selectedRound];
  const candidate = round?.candidates.find((c) => c.id === selected);
  const feedbackKey = `${selectedRound}:${reviewer}:${selected}`;
  const savedFeedback = round?.feedback.find(
    (f) => f.reviewer === reviewer && f.candidate_id === selected,
  );
  const feedback: Feedback =
    feedbackDrafts[feedbackKey] ??
    (savedFeedback
      ? {
          preference: savedFeedback.preference,
          liked: savedFeedback.liked,
          changes: savedFeedback.changes,
          scores: savedFeedback.scores,
        }
      : emptyFeedback());
  const direction =
    directionDrafts[selectedRound] ?? round?.direction ?? emptyDirection();
  const dirty =
    timelineDirty ||
    create ||
    apply ||
    !!Object.keys(feedbackDrafts).length ||
    !!Object.keys(directionDrafts).length;
  const prefix = useId();
  const added = useCallback(() => setAddition(null), []);
  useEffect(() => {
    onDirty(dirty || pending);
  }, [dirty, pending, onDirty]);
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const load = useCallback(
    async (signal?: AbortSignal) => {
      const result = await call<Page>(
        "list_auditions",
        {
          project_id: projectId,
          offset: page * PAGE,
          limit: PAGE,
          include_archived: archived,
        },
        signal,
      );
      if (signal?.aborted) return;
      setRounds(result);
      setLoadError("");
      setSelectedRound((id) => id || result.items[0]?.id || "");
      if (result.total > 0 && page * PAGE >= result.total)
        setPage(Math.floor((result.total - 1) / PAGE));
    },
    [projectId, page, archived],
  );
  useEffect(() => {
    if (!active || tab !== "auditions") return;
    const abort = new AbortController();
    let timer: number;
    const poll = async () => {
      if (busy.current || document.hidden) {
        timer = window.setTimeout(() => void poll(), 3000);
        return;
      }
      setLoading(true);
      try {
        await load(AbortSignal.any([abort.signal, AbortSignal.timeout(10000)]));
      } catch (cause) {
        // Background reads report beside the list, never over a save result.
        if (!abort.signal.aborted) setLoadError(describe(cause));
      } finally {
        if (!abort.signal.aborted) {
          setLoading(false);
          timer = window.setTimeout(() => void poll(), 3000);
        }
      }
    };
    void poll();
    return () => {
      abort.abort();
      window.clearTimeout(timer);
    };
  }, [active, tab, load]);
  useEffect(() => {
    if (!active || tab !== "auditions" || !selectedRound) return;
    const abort = new AbortController();
    let timer: number;
    const read = async () => {
      if (busy.current || document.hidden) {
        timer = window.setTimeout(() => void read(), 3000);
        return;
      }
      const started = epoch.current;
      try {
        const reply = await call<Audition>(
          "get_audition",
          { project_id: projectId, audition_id: selectedRound },
          AbortSignal.any([abort.signal, AbortSignal.timeout(10000)]),
        );
        if (
          !abort.signal.aborted &&
          !busy.current &&
          started === epoch.current
        ) {
          const drafts = latest.current;
          const hasDraft =
            !!drafts.directionDrafts[selectedRound] ||
            Object.keys(drafts.feedbackDrafts).some((key) =>
              key.startsWith(selectedRound + ":"),
            );
          setRecords((old) => {
            if (
              old[selectedRound]?.revision_id === reply.revision_id &&
              JSON.stringify(old[selectedRound]) === JSON.stringify(reply)
            )
              return old;
            if (
              old[selectedRound] &&
              hasDraft &&
              old[selectedRound].revision_id !== reply.revision_id
            )
              return old;
            return { ...old, [selectedRound]: reply };
          });
          if (
            hasDraft &&
            recordsRef.current[selectedRound]?.revision_id !== reply.revision_id
          )
            setExternal(true);
          setSelected((id) =>
            reply.candidates.some((c) => c.id === id) || id === "baseline"
              ? id
              : (reply.candidates[0]?.id ?? ""),
          );
          setLoadError("");
        }
      } catch (cause) {
        if (!abort.signal.aborted) setLoadError(describe(cause));
      } finally {
        if (!abort.signal.aborted)
          timer = window.setTimeout(() => void read(), 3000);
      }
    };
    void read();
    return () => {
      abort.abort();
      window.clearTimeout(timer);
    };
  }, [projectId, selectedRound, active, tab]);

  async function mutate(
    operation: string,
    args: object,
    success: string,
    after?: () => void,
  ) {
    if (!round || busy.current) return;
    busy.current = true;
    epoch.current++;
    setPending(true);
    setError("");
    setNotice("");
    try {
      const reply = await call<Audition>(operation, {
        project_id: projectId,
        audition_id: round.id,
        expected_revision_id: round.revision_id,
        ...args,
      });
      setRecords((old) => ({ ...old, [reply.id]: reply }));
      setConflict(false);
      setExternal(false);
      after?.();
      setNotice(success);
      await load().catch(() =>
        setNotice(success + " 列表刷新失败，可手动刷新。"),
      );
    } catch (cause) {
      setConflict(
        cause instanceof ApiError && cause.code === "revision_conflict",
      );
      setError(describe(cause));
    } finally {
      busy.current = false;
      setPending(false);
    }
  }
  const editFeedback = (patch: Partial<Feedback>) =>
    setFeedbackDrafts((old) => ({
      ...old,
      [feedbackKey]: { ...feedback, ...patch },
    }));
  const editDirection = (patch: Partial<Direction>) =>
    setDirectionDrafts((old) => ({
      ...old,
      [selectedRound]: { ...direction, ...patch },
    }));
  const refreshRevision = async () => {
    if (!round || busy.current) return;
    busy.current = true;
    epoch.current++;
    setPending(true);
    try {
      const reply = await call<Audition>("get_audition", {
        project_id: projectId,
        audition_id: round.id,
      });
      setRecords((old) => ({ ...old, [reply.id]: reply }));
      setConflict(false);
      setExternal(false);
      setError("");
      setNotice(
        "已读取最新版本，本地草稿保留。请核对后再次保存；不会自动提交。",
      );
    } catch (cause) {
      setError(describe(cause));
    } finally {
      busy.current = false;
      setPending(false);
    }
  };
  // The comparison strip shows this reviewer's saved or drafted verdict per candidate.
  const marks: Record<string, CandidateMark> = {};
  const feedbackOf = (candidateId: string) => {
    const draft = feedbackDrafts[`${selectedRound}:${reviewer}:${candidateId}`];
    const saved = round?.feedback.find(
      (f) => f.reviewer === reviewer && f.candidate_id === candidateId,
    );
    return { draft, current: draft ?? saved };
  };
  for (const c of round?.candidates ?? []) {
    const { draft, current } = feedbackOf(c.id);
    marks[c.id] = {
      text: current
        ? (PREFERENCES.find(([v]) => v === current.preference)?.[1] ?? "")
        : "未反馈",
      tone: current?.preference ?? "none",
      draft: !!draft,
    };
  }
  // Mirror the server's direction rules, in form order, so the reason shows
  // before saving instead of a raw validation message.
  const directionProblem =
    direction.outcome === "single" && direction.references.length !== 1
      ? "“沿用一个”需要选 1 个参考候选。"
      : direction.outcome === "combine" && direction.references.length < 2
        ? "“结合多个”需要至少选 2 个参考候选。"
        : !direction.instructions.trim()
          ? "填写制作要求后才能保存。"
          : "";
  const status = round ? roundStatus(round) : null;
  const pictureNote = round?.context
    ? `画面 ${round.context.revision_id.slice(0, 8)} · 第 ${round.context.start_frame} 帧起 · ${round.context.fps.numerator}/${round.context.fps.denominator} fps`
    : "纯音频";
  const firstShown = rounds.total ? page * PAGE + 1 : 0;
  const lastShown = Math.min(rounds.total, (page + 1) * PAGE);
  return (
    <div hidden={!active} className="sound-workspace">
      <nav aria-label="声音工作区分页" className="sound-tabs">
        {(
          [
            ["auditions", "方向试听"],
            ["timeline", "时间线"],
            ["assets", "音频素材"],
          ] as const
        ).map(([id, name]) => (
          <Button
            key={id}
            aria-current={tab === id ? "page" : undefined}
            className={tab === id ? "selected" : ""}
            onClick={() => setTab(id)}
          >
            {name}
            {id === "timeline" && timelineDirty ? " · 未保存" : ""}
          </Button>
        ))}
      </nav>
      <div hidden={tab !== "auditions"} className="audition-layout">
        <aside aria-label="试听轮次" className="audition-rail">
          <div className="audition-rail-heading">
            <h3>
              试听轮次
              {!!rounds.total && (
                <span className="audition-count">{rounds.total}</span>
              )}
            </h3>
            <Button onClick={() => setCreate(true)}>
              <Plus size={15} aria-hidden="true" />
              新建试听
            </Button>
          </div>
          {!!rounds.items.length && (
            <ul className="audition-rounds">
              {rounds.items.map((item) => {
                const [state, text] = roundStatus(item);
                return (
                  <li key={item.id}>
                    <Button
                      aria-current={
                        selectedRound === item.id ? "true" : undefined
                      }
                      className={
                        "audition-round" +
                        (selectedRound === item.id ? " selected" : "")
                      }
                      onClick={() => {
                        setSelectedRound(item.id);
                        setSelected("");
                        setConflict(false);
                        setExternal(false);
                        // Save results belong to the previous round; drafts stay.
                        setError("");
                        setNotice("");
                      }}
                    >
                      <span className="audition-round-title">{item.title}</span>
                      <span className="audition-status" data-state={state}>
                        {text}
                        {directionDrafts[item.id] ? " · 方向未保存" : ""}
                      </span>
                    </Button>
                  </li>
                );
              })}
            </ul>
          )}
          {!rounds.total && (
            <p className="panel-description">
              {loading
                ? "正在读取试听轮次…"
                : "还没有试听轮次。先在“音频素材”导入候选，或由 Agent 提供一轮样曲。"}
            </p>
          )}
          {rounds.total > PAGE && (
            <div className="audition-pager">
              <Button
                aria-label="上一页轮次"
                disabled={page === 0}
                onClick={() => setPage((n) => n - 1)}
              >
                <ChevronLeft size={15} aria-hidden="true" />
              </Button>
              <span>
                {firstShown}–{lastShown} / {rounds.total}
              </span>
              <Button
                aria-label="下一页轮次"
                disabled={(page + 1) * PAGE >= rounds.total}
                onClick={() => setPage((n) => n + 1)}
              >
                <ChevronRight size={15} aria-hidden="true" />
              </Button>
            </div>
          )}
          <label className="audition-archived-toggle">
            <input
              type="checkbox"
              checked={archived}
              onChange={(e) => {
                setArchived(e.target.checked);
                setPage(0);
              }}
            />
            显示已归档轮次
          </label>
          {!!loadError && (
            <>
              <Notice error text={`读取试听失败：${loadError}`} />
              <Button
                disabled={pending}
                onClick={() =>
                  void load().catch((cause) => setLoadError(describe(cause)))
                }
              >
                重新读取
              </Button>
            </>
          )}
        </aside>
        <div className="audition-main">
          {round && status ? (
            <>
              <header className="audition-header">
                <div className="audition-header-text">
                  <div className="audition-title">
                    <h3>{round.title}</h3>
                    <span className="audition-status" data-state={status[0]}>
                      {status[1]}
                    </span>
                  </div>
                  {round.purpose && (
                    <p className="audition-purpose">{round.purpose}</p>
                  )}
                  <p className="technical-note">
                    {round.candidates.length} 个候选 · 片段{" "}
                    {round.duration_seconds.toFixed(1)} 秒 · {pictureNote} ·{" "}
                    {round.blind
                      ? round.revealed_at
                        ? "盲听 · 来源已揭晓"
                        : "盲听 · 来源未揭晓"
                      : "来源可见"}
                  </p>
                </div>
                <div className="audition-header-actions">
                  {round.blind && !round.revealed_at && !round.archived && (
                    <Button
                      disabled={pending}
                      onClick={() => setRevealAsk(true)}
                    >
                      <Eye size={15} aria-hidden="true" />
                      揭晓来源…
                    </Button>
                  )}
                  {round.archived ? (
                    <Button
                      disabled={pending}
                      onClick={() =>
                        void mutate(
                          "archive_audition",
                          { archived: false },
                          "试听轮次已恢复。",
                        )
                      }
                    >
                      恢复此轮试听
                    </Button>
                  ) : (
                    <Button
                      disabled={pending}
                      onClick={() =>
                        void mutate(
                          "archive_audition",
                          {},
                          "试听轮次已归档，历史保留。",
                        )
                      }
                    >
                      <Archive size={15} aria-hidden="true" />
                      归档
                    </Button>
                  )}
                </div>
              </header>
              {round.parent_direction_changed && (
                <Notice text="本轮依据较早的制作方向，请核对最新方向后再决定。" />
              )}
              {round.archived && (
                <Notice text="此轮已归档，只读。恢复后才能继续反馈或修改方向。" />
              )}
              <div className="audition-session">
                <AuditionPlayer
                  key={round.id}
                  round={round}
                  selected={selected}
                  onSelect={setSelected}
                  marks={marks}
                  active={
                    active &&
                    tab === "auditions" &&
                    !create &&
                    !apply &&
                    !revealAsk &&
                    !historyPreview
                  }
                />
                <section className="audition-review" aria-label="评审">
                  <div
                    className="audition-review-tabs"
                    role="group"
                    aria-label="评审内容"
                  >
                    <Button
                      aria-pressed={review === "feedback"}
                      className={review === "feedback" ? "selected" : ""}
                      onClick={() => setReview("feedback")}
                    >
                      1 逐个反馈
                      {Object.keys(feedbackDrafts).some((key) =>
                        key.startsWith(selectedRound + ":"),
                      )
                        ? " · 未保存"
                        : ""}
                    </Button>
                    <Button
                      aria-pressed={review === "direction"}
                      className={review === "direction" ? "selected" : ""}
                      onClick={() => setReview("direction")}
                    >
                      2 本轮方向
                      {directionDrafts[selectedRound] ? " · 未保存" : ""}
                    </Button>
                  </div>
                  <fieldset
                    disabled={pending || round.archived}
                    className="audio-editor-fields"
                  >
                    <div hidden={review !== "feedback"}>
                      <div className="audition-reviewer">
                        <label htmlFor={prefix + "-reviewer"}>评审</label>
                        <input
                          id={prefix + "-reviewer"}
                          maxLength={120}
                          value={reviewer}
                          onChange={(e) => setReviewer(e.target.value)}
                        />
                      </div>
                      {candidate ? (
                        <section aria-label="候选反馈">
                          <h4>
                            候选 {candidate.label}
                            {candidate.name ? ` · ${candidate.name}` : ""}
                            {feedbackDrafts[feedbackKey] ? (
                              <span className="audition-dirty"> · 未保存</span>
                            ) : savedFeedback ? (
                              <span className="audition-saved"> · 已保存</span>
                            ) : null}
                          </h4>
                          <Choice
                            label="候选偏好"
                            name={prefix + "-preference"}
                            value={feedback.preference}
                            options={PREFERENCES}
                            onChange={(preference) =>
                              editFeedback({ preference })
                            }
                          />
                          <Field label="喜欢什么" id={prefix + "-liked"}>
                            <Textarea
                              id={prefix + "-liked"}
                              rows={3}
                              maxLength={2000}
                              value={feedback.liked}
                              onChange={(e) =>
                                editFeedback({ liked: e.target.value })
                              }
                            />
                          </Field>
                          <Field label="希望改什么" id={prefix + "-changes"}>
                            <Textarea
                              id={prefix + "-changes"}
                              rows={3}
                              maxLength={2000}
                              value={feedback.changes}
                              onChange={(e) =>
                                editFeedback({ changes: e.target.value })
                              }
                            />
                          </Field>
                          <details className="audition-scores">
                            <summary>
                              详细评分（可选，5 分最好）
                              {Object.keys(feedback.scores).length
                                ? ` · 已评 ${Object.keys(feedback.scores).length}/5`
                                : ""}
                            </summary>
                            {SCORES.map(([key, label]) => (
                              <Choice
                                key={key}
                                compact
                                label={label}
                                name={prefix + "-score-" + key}
                                value={String(feedback.scores[key] ?? "")}
                                options={[
                                  ["", "–"],
                                  ...[1, 2, 3, 4, 5].map(
                                    (n) => [String(n), String(n)] as const,
                                  ),
                                ]}
                                onChange={(value) => {
                                  const scores = { ...feedback.scores };
                                  if (value) scores[key] = Number(value);
                                  else delete scores[key];
                                  editFeedback({ scores });
                                }}
                              />
                            ))}
                          </details>
                          <div className="audition-review-actions">
                            <Button
                              className="primary"
                              disabled={!reviewer.trim() || conflict}
                              onClick={() =>
                                void mutate(
                                  "save_audition_feedback",
                                  {
                                    candidate_id: candidate.id,
                                    reviewer,
                                    feedback,
                                  },
                                  "反馈已保存，时间线未改变。",
                                  () =>
                                    setFeedbackDrafts((old) => {
                                      const next = { ...old };
                                      delete next[feedbackKey];
                                      return next;
                                    }),
                                )
                              }
                            >
                              保存反馈
                            </Button>
                            {candidate.asset_id ? (
                              <Button
                                onClick={() => {
                                  setApplyStart(
                                    String(
                                      round.context
                                        ? (round.context.start_frame *
                                            round.context.fps.denominator) /
                                            round.context.fps.numerator
                                        : 0,
                                    ),
                                  );
                                  setApplyGain(String(candidate.gain_db));
                                  setApply(true);
                                }}
                              >
                                添加候选到时间线…
                              </Button>
                            ) : (
                              <span className="technical-note">
                                揭晓来源后可添加到时间线
                              </span>
                            )}
                          </div>
                          {savedFeedback?.after_reveal && (
                            <p className="technical-note">
                              此反馈在揭晓后保存。
                            </p>
                          )}
                          {candidate.asset_id && (
                            <details>
                              <summary>来源与客观测量</summary>
                              <p>
                                {candidate.name} · {candidate.source}
                              </p>
                              <p>{candidate.note}</p>
                              <pre>
                                {JSON.stringify(candidate.measurement, null, 2)}
                              </pre>
                            </details>
                          )}
                        </section>
                      ) : (
                        <p className="panel-description">
                          {selected === "baseline"
                            ? "当前在听对照（原配乐或无配乐），不收集反馈。按数字键或点击候选切换后填写。"
                            : "选择一个候选后填写反馈。"}
                        </p>
                      )}
                    </div>
                    <section
                      aria-label="制作方向"
                      hidden={review !== "direction"}
                    >
                      {round.direction_confirmed && (
                        <Notice text="此方向已确认。再次保存或确认会创建新版本，旧决定仍保留。不会自动发起生成。" />
                      )}
                      <Choice
                        label="方向选择"
                        name={prefix + "-outcome"}
                        value={direction.outcome}
                        options={
                          [
                            ["explore", "重新探索"],
                            ["single", "沿用一个"],
                            ["combine", "结合多个"],
                          ] as const
                        }
                        onChange={(outcome) => {
                          // Pre-select candidates this reviewer already marked
                          // 作为参考; the picks stay editable.
                          const marked = round.candidates
                            .filter(
                              (c) =>
                                feedbackOf(c.id).current?.preference ===
                                "reference",
                            )
                            .map((c) => ({ candidate_id: c.id, aspects: "" }));
                          editDirection({
                            outcome,
                            references:
                              outcome === "combine" ||
                              (outcome === "single" && marked.length === 1)
                                ? marked
                                : [],
                          });
                        }}
                      />
                      {direction.outcome !== "explore" && (
                        <div className="audition-references">
                          {round.candidates.map((c) => {
                            const reference = direction.references.find(
                              (r) => r.candidate_id === c.id,
                            );
                            const { current: mine, draft } = feedbackOf(c.id);
                            return (
                              <div
                                key={c.id}
                                className={
                                  "audition-reference" +
                                  (reference ? " selected" : "")
                                }
                              >
                                <label>
                                  <input
                                    type={
                                      direction.outcome === "single"
                                        ? "radio"
                                        : "checkbox"
                                    }
                                    name={prefix + "-reference"}
                                    checked={!!reference}
                                    onChange={(e) =>
                                      editDirection({
                                        references: e.target.checked
                                          ? [
                                              ...(direction.outcome === "single"
                                                ? []
                                                : direction.references),
                                              {
                                                candidate_id: c.id,
                                                aspects: "",
                                              },
                                            ]
                                          : direction.references.filter(
                                              (r) => r.candidate_id !== c.id,
                                            ),
                                      })
                                    }
                                  />
                                  参考候选 {c.label}
                                </label>
                                {/* Saved feedback is the reason for a reference;
                                    the direction never asks to restate it. */}
                                <div className="audition-reference-feedback">
                                  <span>{marks[c.id]?.text}</span>
                                  {mine?.liked && <p>喜欢：{mine.liked}</p>}
                                  {mine?.changes && (
                                    <p>希望改：{mine.changes}</p>
                                  )}
                                  {draft && (
                                    <p className="audition-dirty">
                                      这条反馈尚未保存，Agent 读不到。
                                      <Button
                                        onClick={() => {
                                          setSelected(c.id);
                                          setReview("feedback");
                                        }}
                                      >
                                        去保存
                                      </Button>
                                    </p>
                                  )}
                                </div>
                                {reference && (
                                  <details>
                                    <summary>
                                      补充说明（可选）
                                      {reference.aspects.trim()
                                        ? " · 已填写"
                                        : ""}
                                    </summary>
                                    <Field
                                      label={`参考 ${c.label} 的哪些特点`}
                                      id={prefix + c.id}
                                    >
                                      <input
                                        id={prefix + c.id}
                                        maxLength={2000}
                                        value={reference.aspects}
                                        onChange={(e) =>
                                          editDirection({
                                            references:
                                              direction.references.map((r) =>
                                                r.candidate_id === c.id
                                                  ? {
                                                      ...r,
                                                      aspects: e.target.value,
                                                    }
                                                  : r,
                                              ),
                                          })
                                        }
                                      />
                                    </Field>
                                  </details>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      )}
                      <Field
                        label="制作要求"
                        hint="参考候选已保存的反馈会作为参考理由，这里只写整体要求。"
                        id={prefix + "-instructions"}
                      >
                        <Textarea
                          id={prefix + "-instructions"}
                          rows={5}
                          maxLength={8000}
                          value={direction.instructions}
                          onChange={(e) =>
                            editDirection({ instructions: e.target.value })
                          }
                        />
                      </Field>
                      <Choice
                        label="下一步"
                        name={prefix + "-next"}
                        value={direction.next_step}
                        options={
                          [
                            ["samples", "再来一轮短样"],
                            ["full_score", "制作完整配乐"],
                            ["pause", "暂停"],
                          ] as const
                        }
                        onChange={(next_step) => editDirection({ next_step })}
                      />
                      <div className="audition-review-actions">
                        {[true, false].map((confirm) => (
                          <Button
                            key={String(confirm)}
                            disabled={!!directionProblem || conflict}
                            className={confirm ? "primary" : ""}
                            onClick={() =>
                              void mutate(
                                "save_audition_direction",
                                { direction, confirm },
                                confirm
                                  ? "制作方向已确认；未发起生成，未改变时间线。"
                                  : "方向草稿已保存，尚未确认。",
                                () =>
                                  setDirectionDrafts((old) => {
                                    const next = { ...old };
                                    delete next[selectedRound];
                                    return next;
                                  }),
                              )
                            }
                          >
                            {confirm ? "确认制作方向" : "保存方向草稿"}
                          </Button>
                        ))}
                      </div>
                      <p className="technical-note" role="status">
                        {directionProblem ||
                          "确认只记录决定，不会发起生成，也不会改动时间线。"}
                      </p>
                    </section>
                  </fieldset>
                  <div className="audition-review-status">
                    <Notice error text={error} />
                    <Notice text={notice} />
                    {(conflict || external) && (
                      <>
                        <Notice text="可能有外部更新，反馈草稿保留。读取最新版本后请核对再保存。" />
                        <div className="audition-review-actions">
                          <Button
                            disabled={pending}
                            onClick={() => void refreshRevision()}
                          >
                            读取最新版本并保留草稿
                          </Button>
                          <Button
                            onClick={() =>
                              void navigator.clipboard
                                .writeText(
                                  JSON.stringify(
                                    { feedbackDrafts, directionDrafts },
                                    null,
                                    2,
                                  ),
                                )
                                .then(() => setNotice("草稿已复制。"))
                                .catch((cause) => setError(describe(cause)))
                            }
                          >
                            复制试听草稿
                          </Button>
                        </div>
                      </>
                    )}
                  </div>
                </section>
              </div>
              <details className="audition-records">
                <summary>
                  轮次记录
                  {round.applications?.length
                    ? ` · 已用于时间线 ${round.applications.length} 次`
                    : ""}
                </summary>
                {!!round.applications?.length && (
                  <>
                    <h4>时间线使用记录</h4>
                    {round.applications.map((item) => (
                      <p key={item.id}>
                        候选{" "}
                        {
                          round.candidates.find(
                            (c) => c.id === item.candidate_id,
                          )?.label
                        }{" "}
                        ·{" "}
                        {item.currently_used
                          ? "当前仍在使用"
                          : "曾应用，当前已移除"}{" "}
                        · 音轨版本 {item.timeline_revision_id.slice(0, 8)}
                      </p>
                    ))}
                  </>
                )}
                <h4>轮次历史</h4>
                {round.history?.map((h) => (
                  <p key={h.revision_id} className="audition-history-row">
                    <span>
                      {new Date(h.updated_at).toLocaleString("zh-CN", {
                        timeZone: "Asia/Taipei",
                      })}{" "}
                      · {h.direction_confirmed ? "已确认方向" : "反馈或草稿"} ·{" "}
                      {h.revision_id.slice(0, 8)}
                    </span>
                    <Button
                      disabled={historyBusy}
                      onClick={() => {
                        // Historical reads are isolated from the current editing session.
                        setHistoryBusy(true);
                        void call<Audition>("get_audition", {
                          project_id: projectId,
                          audition_id: round.id,
                          revision_id: h.revision_id,
                        })
                          .then(setHistoryPreview)
                          .catch((cause) => setError(describe(cause)))
                          .finally(() => setHistoryBusy(false));
                      }}
                    >
                      查看此版本
                    </Button>
                  </p>
                ))}
              </details>
            </>
          ) : (
            <div className="audition-empty">
              <p>
                {selectedRound
                  ? "正在读取轮次…"
                  : "选择左侧轮次，或新建一轮试听：挑 1–8 段候选，统一响度后在同一画面下盲听对比。"}
              </p>
              {!selectedRound && (
                <Button className="primary" onClick={() => setCreate(true)}>
                  <Plus size={15} aria-hidden="true" />
                  新建试听
                </Button>
              )}
            </div>
          )}
        </div>
      </div>
      <div hidden={tab === "auditions"}>
        <ProjectAudio
          projectId={projectId}
          embedded
          section={tab === "assets" ? "assets" : "timeline"}
          active={active && tab !== "auditions"}
          addition={addition}
          onAdded={added}
          onDirty={setTimelineDirty}
        />
      </div>
      <Modal
        open={revealAsk}
        title="揭晓候选来源"
        onClose={() => setRevealAsk(false)}
      >
        <p>
          揭晓后会显示每个候选的素材名称与来源，并可添加到时间线。此操作不可撤回；之后保存的反馈会标记为“揭晓后反馈”。
        </p>
        {Object.keys(feedbackDrafts).some((key) =>
          key.startsWith(selectedRound + ":"),
        ) && (
          <Notice text="本轮还有未保存的反馈草稿。若要保持盲听结论，请先保存再揭晓。" />
        )}
        <div className="modal-actions">
          <Button onClick={() => setRevealAsk(false)}>取消</Button>
          <Button
            className="primary"
            disabled={pending}
            onClick={() => {
              setRevealAsk(false);
              void mutate(
                "reveal_audition",
                {},
                "来源已揭晓；之后的反馈会标记为揭晓后反馈。",
              );
            }}
          >
            揭晓来源
          </Button>
        </div>
      </Modal>
      <Modal
        open={!!historyPreview}
        title="试听历史版本"
        onClose={() => setHistoryPreview(null)}
      >
        {historyPreview && (
          <>
            <p>
              {historyPreview.title} · {historyPreview.revision_id.slice(0, 8)}{" "}
              · 只读
            </p>
            <h3>
              {historyPreview.direction_confirmed
                ? "已确认的制作方向"
                : "方向草稿"}
            </h3>
            <p>
              {historyPreview.direction?.instructions ||
                "此版本尚未记录制作方向。"}
            </p>
            {historyPreview.direction?.references.map((reference) => (
              <p key={reference.candidate_id}>
                候选{" "}
                {
                  historyPreview.candidates.find(
                    (c) => c.id === reference.candidate_id,
                  )?.label
                }
                ：{reference.aspects}
              </p>
            ))}
            <h3>反馈</h3>
            {historyPreview.feedback.map((item) => (
              <p key={`${item.reviewer}:${item.candidate_id}`}>
                {item.reviewer} · 候选{" "}
                {
                  historyPreview.candidates.find(
                    (c) => c.id === item.candidate_id,
                  )?.label
                }{" "}
                · {item.after_reveal ? "揭晓后" : "揭晓前或普通试听"}：
                {item.liked}；{item.changes}
              </p>
            ))}
          </>
        )}
      </Modal>
      <Modal
        open={apply}
        title="添加候选到时间线"
        onClose={() => setApply(false)}
      >
        <p>
          添加到现有音轨草稿，其他片段保留。请在时间线核对后点击“保存音轨”。
        </p>
        {timelineDirty && (
          <Notice text="现有时间线有未保存修改，将保留并追加此候选。" />
        )}
        <Field label="时间线起点（秒）" id={prefix + "-apply-start"}>
          <input
            id={prefix + "-apply-start"}
            inputMode="decimal"
            value={applyStart}
            onChange={(e) => setApplyStart(e.target.value)}
          />
        </Field>
        <Field
          label="正式增益（dB）"
          hint="允许 -96 至 12 dB；试听匹配值超出范围时，请先调整。"
          id={prefix + "-apply-gain"}
        >
          <input
            id={prefix + "-apply-gain"}
            inputMode="decimal"
            value={applyGain}
            onChange={(e) => setApplyGain(e.target.value)}
          />
        </Field>
        <p className="technical-note">
          增益初值为试听匹配值，请根据正式混音需求调整。可在时间线继续裁剪。
        </p>
        <Button
          disabled={
            !applyStart.trim() ||
            !applyGain.trim() ||
            !Number.isFinite(Number(applyStart)) ||
            Number(applyStart) < 0 ||
            Number(applyStart) > 86400 ||
            !Number.isFinite(Number(applyGain)) ||
            Number(applyGain) < -96 ||
            Number(applyGain) > 12
          }
          onClick={() => {
            if (!candidate?.asset_id || !round) return;
            setAddition({
              asset_id: candidate.asset_id,
              source_start_seconds: candidate.source_start_seconds ?? 0,
              start_seconds: Number(applyStart),
              duration_seconds: round.duration_seconds,
              gain_db: Number(applyGain),
              audition_id: round.id,
              candidate_id: candidate.id,
            });
            setApply(false);
            setTab("timeline");
          }}
        >
          添加到音轨草稿
        </Button>
      </Modal>
      {create && (
        <CreateAudition
          projectId={projectId}
          work={work}
          onClose={() => setCreate(false)}
          onCreated={(reply) => {
            setRecords((old) => ({ ...old, [reply.id]: reply }));
            setSelectedRound(reply.id);
            setSelected(reply.candidates[0].id);
            setError("");
            setNotice("");
            setCreate(false);
            setPage(0);
            void load().catch((cause) => setError(describe(cause)));
          }}
        />
      )}
    </div>
  );
}

/** Creation is atomic and retryable; the same key survives an uncertain reply. */
function CreateAudition({
  projectId,
  work,
  onClose,
  onCreated,
}: {
  projectId: string;
  work: Work | null;
  onClose: () => void;
  onCreated: (round: Audition) => void;
}) {
  const prefix = useId();
  const [assets, setAssets] = useState<Asset[]>([]);
  const [title, setTitle] = useState("");
  const [purpose, setPurpose] = useState("");
  const [duration, setDuration] = useState("10");
  const [blind, setBlind] = useState(false);
  const [selected, setSelected] = useState<Record<string, string>>({});
  const [withPicture, setWithPicture] = useState(false);
  const [start, setStart] = useState("0");
  const [rate, setRate] = useState({ numerator: 60, denominator: 1 });
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  const [assetPage, setAssetPage] = useState(0);
  const request = useRef<{ recipe: string; key: string } | null>(null);
  const busy = useRef(false);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true);
    setError("");
    void Promise.all([
      call<{ assets: Asset[] }>(
        "get_project_audio",
        { project_id: projectId },
        abort.signal,
      ),
      work?.work_kind === "motion"
        ? call<{ scene: { fps: typeof rate } }>(
            "get_motion_scene",
            { scene_id: work.scene_id, revision_id: work.revision_id },
            abort.signal,
          )
        : Promise.resolve(null),
    ])
      .then(([reply, picture]) => {
        if (!abort.signal.aborted) {
          setAssets(reply.assets);
          if (picture) setRate(picture.scene.fps);
          setLoading(false);
        }
      })
      .catch((cause) => {
        if (!abort.signal.aborted) {
          setError(describe(cause));
          setLoading(false);
        }
      });
    return () => abort.abort();
  }, [projectId, work, retry]);
  async function create() {
    if (busy.current) return;
    busy.current = true;
    setPending(true);
    setError("");
    try {
      const seconds = Number(duration),
        frame = Number(start);
      if (
        !duration.trim() ||
        !Number.isFinite(seconds) ||
        seconds < 0.5 ||
        seconds > 30 ||
        !start.trim() ||
        !Number.isInteger(frame) ||
        frame < 0 ||
        Object.values(selected).some(
          (v) => !v.trim() || !Number.isFinite(Number(v)) || Number(v) < 0,
        )
      )
        throw new Error(
          "请检查片段范围：时长 0.5–30 秒、起始帧为非负整数、素材起点为非负秒数。",
        );
      const args = {
        project_id: projectId,
        title,
        purpose,
        duration_seconds: seconds,
        blind,
        candidates: Object.entries(selected).map(([asset_id, source]) => ({
          asset_id,
          source_start_seconds: Number(source),
        })),
        context:
          withPicture && work
            ? {
                ...work,
                start_frame: frame,
                frames: Math.round(
                  (seconds * rate.numerator) / rate.denominator,
                ),
                fps: rate,
              }
            : null,
      };
      const recipe = JSON.stringify(args);
      if (request.current?.recipe !== recipe)
        request.current = { recipe, key: crypto.randomUUID() };
      const round = await call<Audition>("create_audition", {
        ...args,
        idempotency_key: request.current.key,
      });
      onCreated(round);
    } catch (cause) {
      setError(describe(cause));
    } finally {
      busy.current = false;
      setPending(false);
    }
  }
  return (
    <Modal
      open
      title="新建方向试听"
      onClose={() => {
        if (!pending) onClose();
      }}
    >
      <Notice error text={error} />
      <fieldset disabled={pending || loading} className="audio-editor-fields">
        <Field label="轮次标题" id={prefix + "title"}>
          <input
            id={prefix + "title"}
            maxLength={160}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </Field>
        <Field label="本轮目标" id={prefix + "purpose"}>
          <Textarea
            className="resize-none"
            id={prefix + "purpose"}
            maxLength={4000}
            value={purpose}
            onChange={(e) => setPurpose(e.target.value)}
          />
        </Field>
        <Field label="试听时长（秒）" id={prefix + "duration"}>
          <input
            id={prefix + "duration"}
            inputMode="decimal"
            value={duration}
            onChange={(e) => setDuration(e.target.value)}
          />
        </Field>
        <label>
          <input
            type="checkbox"
            checked={blind}
            onChange={(e) => setBlind(e.target.checked)}
          />
          盲听（创建者可能已知道素材来源）
        </label>
        <label>
          <input
            type="checkbox"
            checked={withPicture}
            disabled={!work}
            onChange={(e) => setWithPicture(e.target.checked)}
          />
          关联当前作品的已保存版本
        </label>
        {withPicture && (
          <>
            <Field label="画面起始帧" id={prefix + "start"}>
              <input
                id={prefix + "start"}
                inputMode="numeric"
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </Field>
            <p>
              固定帧率 {rate.numerator}/{rate.denominator}；WGSL 使用
              60/1，动画使用保存版本的帧率。
            </p>
          </>
        )}
        <h3>选择候选（{Object.keys(selected).length}/8）</h3>
        {assets.slice(assetPage * 10, assetPage * 10 + 10).map((asset) => (
          <div key={asset.id}>
            <label>
              <input
                type="checkbox"
                checked={asset.id in selected}
                disabled={
                  !(asset.id in selected) && Object.keys(selected).length >= 8
                }
                onChange={(e) =>
                  setSelected((old) => {
                    const next = { ...old };
                    if (e.target.checked) next[asset.id] = "0";
                    else delete next[asset.id];
                    return next;
                  })
                }
              />
              {asset.name} · {asset.duration_seconds.toFixed(1)} 秒
            </label>
            {asset.id in selected && (
              <Field
                label={`${asset.name} 素材起点（秒）`}
                id={prefix + asset.id}
              >
                <input
                  id={prefix + asset.id}
                  inputMode="decimal"
                  value={selected[asset.id]}
                  onChange={(e) =>
                    setSelected((old) => ({
                      ...old,
                      [asset.id]: e.target.value,
                    }))
                  }
                />
              </Field>
            )}
          </div>
        ))}
        {!assets.length && (
          <p>请先在“音频素材”导入音频，或让 Agent 提供候选素材。</p>
        )}
        <div className="modal-actions">
          <Button
            disabled={assetPage === 0}
            onClick={() => setAssetPage((n) => n - 1)}
          >
            上一页素材
          </Button>
          <Button
            disabled={(assetPage + 1) * 10 >= assets.length}
            onClick={() => setAssetPage((n) => n + 1)}
          >
            下一页素材
          </Button>
        </div>
        <div className="modal-actions">
          <Button onClick={onClose}>取消</Button>
          <Button
            className="primary"
            disabled={!title.trim() || !Object.keys(selected).length}
            onClick={() => void create()}
          >
            {pending ? "正在准备片段…" : "创建试听轮次"}
          </Button>
        </div>
      </fieldset>
      {loading && <p role="status">正在读取素材…</p>}
      {!!error && !pending && (
        <Button onClick={() => setRetry((n) => n + 1)}>重新读取素材</Button>
      )}
    </Modal>
  );
}
