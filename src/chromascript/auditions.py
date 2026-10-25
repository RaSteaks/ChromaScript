"""Revisioned listening rounds: immutable PCM excerpts, advisory feedback, no timeline writes."""
from __future__ import annotations

import copy
import hashlib
import json
import random
import shutil
import wave
from fractions import Fraction
from pathlib import Path
from typing import Annotated, Any, Callable, Literal

from pydantic import Field

from .audio import (
    RATE,
    freeze_audio,
    measure_loudness,
    mix_audio,
    pcm_writer,
    read_timeline,
    samples,
)
from .errors import ChromaError
from .images import file_hash
from .models import Artifact, Model, Rational, RenderSettings
from .operations import operation
from .store import Store, canonical, identifier, now


class AuditionCandidate(Model):
    asset_id: str = Field(min_length=1, max_length=64)
    source_start_seconds: float = Field(default=0, ge=0, le=600)
    note: str = Field(default="", max_length=2000)


class AuditionContext(Model):
    work_kind: Literal["wgsl", "motion"]
    scene_id: str = Field(min_length=1, max_length=64)
    revision_id: str = Field(min_length=1, max_length=64)
    start_frame: int = Field(default=0, ge=0, le=216000, strict=True)
    frames: int = Field(ge=1, le=7200, strict=True)
    fps: Rational = Field(default_factory=Rational)


class AuditionFeedback(Model):
    preference: Literal["reference", "reject", "unsure"] = "unsure"
    liked: str = Field(default="", max_length=2000)
    changes: str = Field(default="", max_length=2000)
    # Optional scores have a fixed, documented direction: 5 is always better.
    scores: dict[Literal["reading", "quality", "emotion", "concept", "repeat"], Annotated[int, Field(ge=1, le=5, strict=True)]] = Field(default_factory=dict)


class DirectionReference(Model):
    candidate_id: str = Field(min_length=1, max_length=64)
    # Optional supplement. Empty means "carry over this candidate's saved
    # reviewer feedback" (liked/changes at the same round revision), so the
    # decision never forces reviewers to restate what they already wrote.
    aspects: str = Field(default="", max_length=2000)


class AuditionDirection(Model):
    outcome: Literal["single", "combine", "explore"] = "explore"
    references: list[DirectionReference] = Field(default_factory=list, max_length=8)
    instructions: str = Field(min_length=1, max_length=8000)
    next_step: Literal["samples", "full_score", "pause"] = "samples"


def public_round(record: dict[str, Any]) -> dict[str, Any]:
    """One allowlisted projection for every transport, including mutation responses."""
    result = {k: copy.deepcopy(record[k]) for k in (
        "id", "project_id", "revision_id", "title", "purpose", "context", "duration_seconds",
        "created_at", "updated_at", "blind", "revealed_at", "archived", "feedback", "direction",
        "direction_confirmed", "direction_revision_id", "parent", "loudness_target_lufs",
        "effective_target_lufs", "baseline", "status",
    )}
    revealed = not record["blind"] or record["revealed_at"] is not None
    result["candidates"] = []
    for candidate in record["candidates"]:
        item = {k: candidate[k] for k in ("id", "label", "playback_artifact_id", "gain_db", "normalization_available")}
        if revealed:
            item.update({k: candidate[k] for k in ("asset_id", "name", "source_start_seconds", "note", "source", "measurement")})
        result["candidates"].append(item)
    # A baseline is a materialized anonymous excerpt, not the live project timeline.
    result["baseline"] = {k: result["baseline"][k] for k in ("playback_artifact_id", "gain_db", "normalization_available")}
    return result


class AuditionOperations:
    store: Store

    def _round(self, project_id: str, audition_id: str) -> dict[str, Any]:
        record = self.store.get("audition_round", audition_id)
        if record["project_id"] != project_id:
            raise ChromaError("audition_project", "试听轮次不属于当前项目。")
        return record

    def _revise_audition(self, project_id: str, audition_id: str, expected: str,
                         change: Callable[[dict[str, Any]], None], *, allow_archived: bool = False) -> dict[str, Any]:
        if self.store.get("project", project_id)["archived"]:
            raise ChromaError("project_archived", "请先恢复项目。")
        # The revision check and history insertion share a transaction across daemons.
        with self.store.connect(project_id) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM records WHERE kind='audition_round' AND id=?", (audition_id,)).fetchone()
            if not row:
                raise ChromaError("not_found", "试听轮次不存在。")
            record = json.loads(row[0])
            if record["project_id"] != project_id:
                raise ChromaError("audition_project", "试听轮次不属于当前项目。")
            if record["revision_id"] != expected:
                raise ChromaError("revision_conflict", "试听轮次已更新；草稿已保留，请读取最新版本后重新核对。", current_revision_id=record["revision_id"])
            if record["archived"] and not allow_archived:
                raise ChromaError("audition_archived", "请先恢复此轮试听。")
            record["revision_id"] = identifier()
            change(record)
            record["updated_at"] = now()
            db.execute("UPDATE records SET data=? WHERE kind='audition_round' AND id=?", (canonical(record), audition_id))
            db.execute("INSERT INTO records VALUES ('audition_revision',?,?)", (record["revision_id"], canonical(record)))
        return public_round(record)

    @operation("Create a ready listening round from 1–8 project assets. Freezes <=30s PCM excerpts, rational picture revision and original soundtrack. Matches excerpt LUFS with shared peak headroom. Never changes audio timeline. Reuse idempotency_key after an uncertain response.")
    def create_audition(self, project_id: str, title: str, candidates: list[AuditionCandidate],
                        idempotency_key: str, purpose: str = "", duration_seconds: float = 10,
                        context: AuditionContext | None = None, blind: Annotated[bool, Field(strict=True)] = False,
                        loudness_target_lufs: float = -23, parent_audition_id: str | None = None,
                        parent_direction_revision_id: str | None = None) -> dict[str, Any]:
        project = self.store.get("project", project_id)
        if project["archived"]:
            raise ChromaError("project_archived", "请先恢复项目。")
        if not title.strip() or len(title) > 160 or len(purpose) > 4000 or not 1 <= len(candidates) <= 8:
            raise ChromaError("audition_input", "请输入标题和 1–8 个候选，标题最多 160 字，目标最多 4000 字。")
        if not 0.5 <= duration_seconds <= 30 or not -40 <= loudness_target_lufs <= -10 or not 1 <= len(idempotency_key) <= 200:
            raise ChromaError("audition_input", "试听时长须为 0.5–30 秒，目标响度须为 -40 至 -10 LUFS，并提供幂等键。")
        request = dict(project_id=project_id, title=title, purpose=purpose, candidates=[c.model_dump() for c in candidates], context=context.model_dump() if context else None, duration_seconds=duration_seconds, blind=blind, loudness_target_lufs=loudness_target_lufs, parent_audition_id=parent_audition_id, parent_direction_revision_id=parent_direction_revision_id)
        digest = hashlib.sha256(canonical(request).encode()).hexdigest()
        rid = hashlib.sha256((project_id + ":" + idempotency_key).encode()).hexdigest()[:32]
        try:
            old = self._round(project_id, rid)
        except ChromaError as exc:
            if exc.code != "not_found":
                raise
        else:
            if old["request_hash"] != digest:
                raise ChromaError("idempotency_conflict", "此幂等键已用于另一轮参数。")
            return public_round(old)
        parent = None
        if parent_audition_id:
            source = self._round(project_id, parent_audition_id)
            version = self.store.get("audition_revision", parent_direction_revision_id or source["direction_revision_id"] or "")
            if version["id"] != source["id"] or not version["direction_confirmed"] or version["revision_id"] != version["direction_revision_id"]:
                raise ChromaError("audition_parent", "请选择已确认的制作方向版本。")
            parent = {"audition_id": source["id"], "direction_revision_id": version["revision_id"]}
        elif parent_direction_revision_id:
            raise ChromaError("audition_parent", "方向版本必须关联上一轮。")
        fps = context.fps if context else Rational(numerator=60)
        frames = context.frames if context else round(duration_seconds * fps.value)
        length = Fraction(frames * fps.denominator, fps.numerator)
        if not Fraction(1, 2) <= length <= 30:
            raise ChromaError("audition_range", "画面片段须为 0.5–30 秒。")
        if context:
            kind, revision_kind = ("scene", "revision") if context.work_kind == "wgsl" else ("motion_scene", "motion_revision")
            scene = self.store.get(kind, context.scene_id)
            revision = self.store.get(revision_kind, context.revision_id)
            if scene["project_id"] != project_id or revision["scene_id"] != context.scene_id:
                raise ChromaError("audition_context", "画面版本不属于此作品或项目。")
            if context.work_kind == "motion":
                spec = revision["scene"]
                if Rational.model_validate(spec["fps"]) != fps or context.start_frame + frames > spec["frames"]:
                    raise ChromaError("audition_range", "片段超出动画范围或帧率与固定版本不一致。")
        settings = RenderSettings(width=480, height=270, frames=frames, start_frame=context.start_frame if context else 0, fps=fps, samples=1)
        frozen = freeze_audio(self.store, project_id, settings)
        count = samples(length)
        # Random path prevents concurrent creation attempts from deleting another attempt's media.
        directory = self.store.project_file(project_id, f"auditions/{identifier()}")
        directory.mkdir(parents=True)
        artifacts: list[dict[str, Any]] = []
        def media(path: Path) -> tuple[str, dict[str, Any]]:
            aid = identifier()
            artifact = Artifact(id=aid, project_id=project_id, job_id="", name="试听片段.wav", kind="audition-audio", relative_path=self.store.relative_file(path), mime_type="audio/wav", size_bytes=path.stat().st_size, created_at=now(), metadata={"sha256": file_hash(path)})
            artifacts.append(artifact.model_dump())
            return aid, measure_loudness(path)
        try:
            prepared = []
            for i, candidate in enumerate(candidates):
                asset = self.store.get("audio_asset", candidate.asset_id)
                if asset["project_id"] != project_id:
                    raise ChromaError("audio_project", "候选素材不属于此项目。")
                path = self.store.file(asset["relative_path"])
                if not path.is_file() or file_hash(path) != asset["sha256"]:
                    raise ChromaError("audio_asset_changed", "候选音频缺失或已变更。")
                start = samples(candidate.source_start_seconds)
                with wave.open(str(path), "rb") as reader:
                    if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (RATE, 2, 4) or start + count > reader.getnframes():
                        raise ChromaError("audition_range", "候选素材长度不足或 PCM 格式不正确。")
                    reader.setpos(start)
                    data = reader.readframes(count)
                if len(data) != count * 8 or file_hash(path) != asset["sha256"]:
                    raise ChromaError("audio_asset_changed", "候选在准备期间发生变化。")
                excerpt = directory / f"{i}.wav"
                with pcm_writer(excerpt) as writer:
                    writer.writeframes(data)
                aid, measured = media(excerpt)
                prepared.append({"id": identifier(), "asset_id": asset["id"], "name": asset["name"], "source_start_seconds": start / RATE, "note": candidate.note, "source": asset.get("metadata", {}).get("source", "unknown"), "measurement": measured, "playback_artifact_id": aid})
            baseline_path = directory / "baseline.wav"
            baseline: dict[str, Any] = {"snapshot": frozen, "playback_artifact_id": None, "gain_db": 0, "normalization_available": False}
            if frozen:
                mix_audio(self.store, frozen, settings, baseline_path)
                aid, measured = media(baseline_path)
                baseline.update(playback_artifact_id=aid, measurement=measured)
            measured_items = prepared + ([baseline] if frozen else [])
            # Use linear gain only. Lower the common target for every candidate if any
            # excerpt would exceed -1 dBTP; never alter dynamics with a hidden limiter.
            target = loudness_target_lufs
            for item in measured_items:
                m = item["measurement"]
                if m["integrated_lufs"] is not None and m["true_peak_dbtp"] is not None:
                    target = min(target, m["integrated_lufs"] - 1 - m["true_peak_dbtp"])
            for item in measured_items:
                lufs = item["measurement"]["integrated_lufs"]
                item.update(gain_db=target - lufs if lufs is not None else 0, normalization_available=lufs is not None)
            seed = random.SystemRandom().randrange(2**53)
            if blind:
                random.Random(seed).shuffle(prepared)
            for i, item in enumerate(prepared):
                item["label"] = chr(65 + i)
            stamp = now()
            record = {"id": rid, "project_id": project_id, "revision_id": identifier(), "request_hash": digest, "title": title.strip(), "purpose": purpose, "context": context.model_dump() if context else None, "duration_seconds": count / RATE, "created_at": stamp, "updated_at": stamp, "blind": blind, "seed": seed, "revealed_at": None, "archived": False, "candidates": prepared, "baseline": baseline, "loudness_target_lufs": loudness_target_lufs, "effective_target_lufs": target, "feedback": [], "direction": None, "direction_confirmed": False, "direction_revision_id": None, "parent": parent, "status": "feedback"}
            with self.store.connect(project_id) as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT data FROM records WHERE kind='audition_round' AND id=?", (rid,)).fetchone()
                if row:
                    old = json.loads(row[0])
                    if old["request_hash"] != digest:
                        raise ChromaError("idempotency_conflict", "此幂等键已被其他参数使用。")
                    shutil.rmtree(directory)
                    return public_round(old)
                db.executemany("INSERT INTO records VALUES (?,?,?)", [("artifact", a["id"], canonical(a)) for a in artifacts] + [("audition_round", rid, canonical(record)), ("audition_revision", record["revision_id"], canonical(record))])
            return public_round(record)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

    @operation("Read a listening round or its immutable history. Blind sources remain hidden until the current round is revealed. No timeline mutation.")
    def get_audition(self, project_id: str, audition_id: str, revision_id: str | None = None) -> dict[str, Any]:
        current = self._round(project_id, audition_id)
        record = current
        if revision_id:
            record = self.store.get("audition_revision", revision_id)
            if record["id"] != audition_id:
                raise ChromaError("audition_revision", "修订不属于此轮试听。")
            record["revealed_at"] = current["revealed_at"]
        result = public_round(record)
        timeline = read_timeline(self.store, project_id)
        result["applications"] = [{**r, "currently_used": any(c.get("audition_id") == audition_id and c.get("candidate_id") == r["candidate_id"] for c in timeline["timeline"]["clips"])} for r in self.store.list("audition_application") if r["project_id"] == project_id and r["audition_id"] == audition_id][:100]
        result["history"] = [{"revision_id": r["revision_id"], "updated_at": r["updated_at"], "direction_confirmed": r["direction_confirmed"]} for r in self.store.list("audition_revision") if r["id"] == audition_id][:100]
        if record["parent"]:
            parent = self._round(project_id, record["parent"]["audition_id"])
            result["parent_direction_changed"] = parent["direction_revision_id"] != record["parent"]["direction_revision_id"] or not parent["direction_confirmed"]
        return result

    @operation("List bounded project listening rounds. No source mapping is returned for blind rounds.")
    def list_auditions(self, project_id: str, limit: int = 20, offset: int = 0, include_archived: Annotated[bool, Field(strict=True)] = False) -> dict[str, Any]:
        self.store.get("project", project_id)
        if not 1 <= limit <= 100 or offset < 0:
            raise ChromaError("pagination", "分页范围无效。")
        records = [r for r in self.store.list("audition_round") if r["project_id"] == project_id and (include_archived or not r["archived"])]
        return {"items": [public_round(r) for r in records[offset:offset + limit]], "total": len(records), "limit": limit, "offset": offset}

    @operation("Save one reviewer's candidate feedback with a round revision check. Names identify local reviewers, not authenticated identities. Scores: reading/quality/emotion/concept/repeat, 1–5, higher is better.")
    def save_audition_feedback(self, project_id: str, audition_id: str, expected_revision_id: str, candidate_id: str, reviewer: str, feedback: AuditionFeedback) -> dict[str, Any]:
        if not reviewer.strip() or len(reviewer) > 120:
            raise ChromaError("audition_reviewer", "请输入评审名称（最多 120 字）。")
        def change(record: dict[str, Any]) -> None:
            if candidate_id not in {c["id"] for c in record["candidates"]}:
                raise ChromaError("audition_candidate", "候选不属于此轮试听。")
            if len(record["feedback"]) >= 1000 and not any(f["candidate_id"] == candidate_id and f["reviewer"] == reviewer.strip() for f in record["feedback"]):
                raise ChromaError("audition_reviewers", "此轮反馈已达 1000 条，请创建新轮次。")
            entry = {"candidate_id": candidate_id, "reviewer": reviewer.strip(), **feedback.model_dump(), "created_at": now(), "after_reveal": record["revealed_at"] is not None}
            record["feedback"] = [f for f in record["feedback"] if (f["candidate_id"], f["reviewer"]) != (candidate_id, reviewer.strip())] + [entry]
        return self._revise_audition(project_id, audition_id, expected_revision_id, change)

    @operation("Save a proposed direction or explicitly confirm one. Never starts generation, incurs charges, or applies music to the timeline. Confirmation can reference one, several, or no candidates; a reference's aspects are optional and, when empty, defer to that candidate's saved reviewer feedback in the same revision.")
    def save_audition_direction(self, project_id: str, audition_id: str, expected_revision_id: str, direction: AuditionDirection, confirm: Annotated[bool, Field(strict=True)] = False) -> dict[str, Any]:
        refs = [r.candidate_id for r in direction.references]
        if not direction.instructions.strip() or len(set(refs)) != len(refs) or (direction.outcome == "single" and len(refs) != 1) or (direction.outcome == "combine" and len(refs) < 2) or (direction.outcome == "explore" and refs):
            raise ChromaError("audition_direction", "请填写制作要求；沿用需 1 个参考，结合需至少 2 个，重新探索不选参考。")
        def change(record: dict[str, Any]) -> None:
            if set(refs) - {c["id"] for c in record["candidates"]}:
                raise ChromaError("audition_candidate", "参考候选不属于此轮试听。")
            record.update(direction=direction.model_dump(), direction_confirmed=confirm, direction_revision_id=record["revision_id"], status="decided" if confirm else "feedback")
        return self._revise_audition(project_id, audition_id, expected_revision_id, change)

    @operation("Explicitly reveal a blind round's source mapping. Subsequent feedback is marked after reveal. Revealing cannot be undone.")
    def reveal_audition(self, project_id: str, audition_id: str, expected_revision_id: str) -> dict[str, Any]:
        def change(record: dict[str, Any]) -> None:
            record["revealed_at"] = record["revealed_at"] or now()
        return self._revise_audition(project_id, audition_id, expected_revision_id, change)

    @operation("Archive a listening round without deleting audio, feedback or direction history. Never changes the timeline.")
    def archive_audition(self, project_id: str, audition_id: str, expected_revision_id: str, archived: Annotated[bool, Field(strict=True)] = True) -> dict[str, Any]:
        def change(record: dict[str, Any]) -> None:
            record.update(archived=archived, status="archived" if archived else "decided" if record["direction_confirmed"] else "feedback")
        return self._revise_audition(project_id, audition_id, expected_revision_id, change, allow_archived=True)
