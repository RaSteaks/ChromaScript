"""Independent UTF-8 subtitle delivery; never enters the image/render pipeline."""

from __future__ import annotations

import hashlib
import json
import shutil
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .errors import ChromaError
from .exports import export_directory
from .models import Model, MotionRate, RenderSettings
from .store import Store, canonical, identifier


class SubtitleCue(Model):
    start_frame: int = Field(strict=True, ge=0)
    end_frame: int = Field(strict=True, gt=0)
    zh: str | None = Field(default=None, max_length=2000)
    en: str | None = Field(default=None, max_length=2000)

    @field_validator("zh", "en")
    @classmethod
    def text_lines(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # Empty lines terminate an SRT cue; normalize line endings at the boundary.
        text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not text or any(not line.strip() for line in text.split("\n")):
            raise ValueError("Subtitle text must contain non-empty lines")
        if "-->" in text or any(ord(char) < 32 and char != "\n" for char in text):
            raise ValueError("Subtitle text contains a timestamp delimiter or control character")
        return text


class SubtitleSpec(Model):
    language: Literal["zh", "en", "zh-en"]
    # Keep source frame boundaries valid for custom animation rates as well as
    # WGSL rates; the narrower Rational contract would force subtitle retiming.
    fps: MotionRate
    frames: int = Field(strict=True, gt=0)
    cues: list[SubtitleCue] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def timeline(self) -> SubtitleSpec:
        previous_end = 0
        required = ("zh", "en") if self.language == "zh-en" else (self.language,)
        for cue in self.cues:
            if not previous_end <= cue.start_frame < cue.end_frame <= self.frames:
                raise ValueError("Cues must be ordered, non-overlapping and within the source duration")
            for language in required:
                text = getattr(cue, language)
                if text is None:
                    raise ValueError(f"Missing {language} subtitle text")
                limit = 1 if self.language == "zh-en" else 2
                if len(text.split("\n")) > limit:
                    raise ValueError("Use at most two lines per cue; bilingual uses one per language")
            previous_end = cue.end_frame
        return self


class ProjectSubtitles(Model):
    tracks: list[SubtitleSpec] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def unique_languages(self) -> ProjectSubtitles:
        languages = [track.language for track in self.tracks]
        if len(languages) != len(set(languages)):
            raise ValueError("Select at most one subtitle track per language mode")
        return self


def subtitle_text(spec: SubtitleSpec, start: Fraction = Fraction(0), end: Fraction | None = None) -> str:
    # Clip in rational seconds, then shift to delivery zero before rounding to ms.
    # Retiming output fps alone never changes subtitle presentation times.
    languages = ("zh", "en") if spec.language == "zh-en" else (spec.language,)
    blocks: list[str] = []
    for cue in spec.cues:
        cue_start = max(start, Fraction(cue.start_frame * spec.fps.denominator, spec.fps.numerator))
        cue_end = Fraction(cue.end_frame * spec.fps.denominator, spec.fps.numerator)
        if end is not None:
            cue_end = min(cue_end, end)
        if cue_start >= cue_end:
            continue
        text = "\n".join(getattr(cue, language) for language in languages)
        blocks.append(
            f"{len(blocks) + 1}\n{timestamp(cue_start - start)} --> "
            f"{timestamp(cue_end - start)}\n{text}\n\n"
        )
    return "".join(blocks)


def export_subtitles(spec: SubtitleSpec, destination: str) -> dict[str, str | int]:
    raw_target = Path(destination).expanduser()
    if not raw_target.is_absolute() or "\x00" in destination:
        raise ChromaError("export_directory", "请输入字幕文件的绝对路径。")
    if raw_target.suffix.lower() != ".srt":
        raise ChromaError("subtitle_format", "字幕单独导出为 UTF-8 .srt 文件。")
    target = export_directory(str(raw_target.parent)) / raw_target.name
    payload = subtitle_text(spec).encode("utf-8")
    created = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation preserves existing deliveries, including symlinks.
        with target.open("xb") as output:
            created = True
            output.write(payload)
    except FileExistsError as exc:
        raise ChromaError("file_exists", "Choose a new subtitle destination") from exc
    except OSError as exc:
        if created:
            target.unlink(missing_ok=True)
        raise ChromaError("export_failed", "字幕写入失败，请检查磁盘空间和目录权限。") from exc
    return {
        "destination": str(target), "format": "srt", "encoding": "utf-8",
        "language": spec.language, "cue_count": len(spec.cues), "size_bytes": len(payload),
    }


def timestamp(time: Fraction) -> str:
    # Round each absolute rational boundary to the nearest millisecond (half up).
    # Never accumulate rounded cue durations: NTSC rates would drift over time.
    milliseconds = (2 * time.numerator * 1000 + time.denominator) // (2 * time.denominator)
    seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def read_subtitles(store: Store, project_id: str) -> dict[str, Any]:
    try:
        return store.get("subtitle_timeline", project_id)
    except ChromaError as exc:
        if exc.code != "not_found":
            raise
        return {"project_id": project_id, "revision_id": "0", "subtitles": {"tracks": []}, "files": []}


def save_subtitles(store: Store, project_id: str, expected_revision_id: str, subtitles: ProjectSubtitles) -> dict[str, Any]:
    revision = identifier()
    logical = f"subtitles/{project_id}/{revision}"
    directory = store.project_file(project_id, logical)
    created = False
    try:
        with store.connect(project_id) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT data FROM records WHERE kind='subtitle_timeline' AND id=?", (project_id,)).fetchone()
            current = json.loads(row[0])["revision_id"] if row else "0"
            if current != expected_revision_id:
                raise ChromaError("revision_conflict", "Read the latest project subtitles; preserve your draft", current_revision_id=current)
            directory.mkdir(parents=True, exist_ok=False)
            created = True
            files = []
            for spec in subtitles.tracks:
                name = f"subtitles.{spec.language}.srt"
                payload = subtitle_text(spec).encode("utf-8")
                (directory / name).write_bytes(payload)
                files.append({"language": spec.language, "relative_path": f"{logical}/{name}",
                              "sha256": hashlib.sha256(payload).hexdigest()})
            record = {"project_id": project_id, "revision_id": revision,
                      "subtitles": subtitles.model_dump(), "files": files}
            (directory / "subtitles.json").write_text(canonical(record), encoding="utf-8")
            connection.execute("INSERT INTO records VALUES ('subtitle_revision',?,?)", (revision, canonical(record)))
            connection.execute(
                "INSERT INTO records VALUES ('subtitle_timeline',?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data",
                (project_id, canonical(record)),
            )
    except BaseException:
        # Roll back only this new version, never an existing source or delivery.
        if created:
            shutil.rmtree(directory)
        raise
    return record


def freeze_subtitles(store: Store, project_id: str, settings: RenderSettings) -> dict[str, Any] | None:
    record = read_subtitles(store, project_id)
    selected = ProjectSubtitles.model_validate(record["subtitles"])
    if not selected.tracks:
        return None
    start = Fraction(settings.start_frame * settings.fps.denominator, settings.fps.numerator)
    end = start + Fraction(settings.frames * settings.fps.denominator, settings.fps.numerator)
    files = {}
    for spec, source in zip(selected.tracks, record["files"], strict=True):
        try:
            payload = store.file(source["relative_path"]).read_bytes()
        except OSError as exc:
            raise ChromaError("subtitle_missing", "项目字幕文件缺失，请恢复后重新导出。") from exc
        if hashlib.sha256(payload).hexdigest() != source["sha256"]:
            raise ChromaError("subtitle_changed", "项目字幕文件已被外部修改，请通过字幕保存接口提交新版本。")
        duration = Fraction(spec.frames * spec.fps.denominator, spec.fps.numerator)
        if end > duration:
            raise ChromaError("subtitle_duration", "字幕时间轴未覆盖此次导出范围，请先更新项目字幕。")
        text = subtitle_text(spec, start, end)
        # An empty trim window has no active subtitles, so omit an empty SRT.
        if text:
            files[f"subtitles.{spec.language}.srt"] = text
    # Freeze bytes as text in the hashed job snapshot. Recovery needs neither
    # the current project selection nor the original file to remain available.
    return {"revision_id": record["revision_id"], "files": files}


def write_subtitles(frozen: dict[str, Any] | None, directory: Path) -> dict[str, Path]:
    if frozen is None:
        return {}
    files = {}
    for name, text in frozen["files"].items():
        path = directory / name
        path.write_text(text, encoding="utf-8")
        files[name] = path
    return files
