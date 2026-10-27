"""Real sidecars, rational timestamps and validation without rendering video."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from chromascript.errors import ChromaError
from chromascript.models import MotionRenderSettings
from chromascript.operations import execute
from chromascript.service import Service
from chromascript.subtitles import SubtitleSpec, export_subtitles, freeze_subtitles


def specification(**changes):
    return {
        "language": "zh-en", "fps": {"numerator": 30000, "denominator": 1001},
        "frames": 110000,
        "cues": [
            {"start_frame": 0, "end_frame": 60, "zh": "你好，世界。", "en": "Hello, world."},
            {"start_frame": 108000, "end_frame": 108030, "zh": "再见。", "en": "Goodbye."},
        ],
        **changes,
    }


@pytest.mark.parametrize("language,text", [
    ("zh", "你好，世界。"), ("en", "Hello, world."), ("zh-en", "你好，世界。\nHello, world."),
])
def test_sidecar_language_timing_and_unchanged_scene(tmp_path, language, text):
    service = Service(tmp_path / "workspace")
    project = service.create_project("clean video")["project"]["id"]
    scene = service.create_motion_scene(project)
    target = tmp_path / "delivery" / f"video.{language}.srt"
    result = execute(service, "export_subtitles", {
        "spec": specification(language=language), "destination": str(target),
    })
    content = target.read_text(encoding="utf-8")
    assert content.startswith(f"1\n00:00:00,000 --> 00:00:02,002\n{text}\n\n")
    assert "2\n01:00:03,600 --> 01:00:04,601\n" in content
    assert result["size_bytes"] == len(target.read_bytes())
    assert result["cue_count"] == 2
    assert service.get_motion_scene(scene["record"]["id"]) == scene


@pytest.mark.parametrize("fps,start,end,timing", [
    ({"numerator": 1, "denominator": 2}, 1, 2, "00:00:02,000 --> 00:00:04,000"),
    ({"numerator": 480, "denominator": 1}, 120, 480, "00:00:00,250 --> 00:00:01,000"),
    ({"numerator": 1000001, "denominator": 40000}, 25, 50, "00:00:01,000 --> 00:00:02,000"),
    ({"numerator": 1, "denominator": 100001}, 1, 2, "27:46:41,000 --> 55:33:22,000"),
    ({"numerator": 2147483647, "denominator": 2147483646}, 1, 2, "00:00:01,000 --> 00:00:02,000"),
])
def test_custom_animation_timebases_save_freeze_and_export(tmp_path, fps, start, end, timing):
    # Use an accepted animation timebase unchanged through the public operations;
    # literal SRT expectations independently check rational boundary conversion.
    settings = MotionRenderSettings(fps=fps, frames=end)
    service = Service(tmp_path / "workspace")
    project = service.create_project("custom timing")["project"]["id"]
    spec = specification(language="en", fps=fps, frames=end, cues=[
        {"start_frame": start, "end_frame": end, "en": "Custom timing"},
    ])
    saved = execute(service, "set_project_subtitles", {
        "project_id": project, "expected_revision_id": "0", "subtitles": {"tracks": [spec]},
    })
    expected = f"1\n{timing}\nCustom timing\n\n"
    assert Path(saved["files"][0]["local_path"]).read_text(encoding="utf-8") == expected
    frozen = freeze_subtitles(service.store, project, settings)
    assert frozen["files"] == {"subtitles.en.srt": expected}
    target = tmp_path / "standalone.srt"
    execute(service, "export_subtitles", {"spec": spec, "destination": str(target)})
    assert target.read_text(encoding="utf-8") == expected


@pytest.mark.parametrize("field", ["numerator", "denominator"])
@pytest.mark.parametrize("value", [True, "30", 30.0, 0, -1, 2147483648])
def test_subtitle_rate_requires_positive_int32_integers(field, value):
    fps = {"numerator": 30, "denominator": 1, field: value}
    with pytest.raises(ValidationError):
        SubtitleSpec.model_validate(specification(fps=fps))


@pytest.mark.parametrize("cue", [
    {"start_frame": 3, "end_frame": 3, "zh": "字", "en": "text"},
    {"start_frame": -1, "end_frame": 3, "zh": "字", "en": "text"},
    {"start_frame": 0, "end_frame": 110001, "zh": "字", "en": "text"},
    {"start_frame": True, "end_frame": 3, "zh": "字", "en": "text"},
    {"start_frame": "0", "end_frame": 3, "zh": "字", "en": "text"},
    {"start_frame": 0, "end_frame": 3, "zh": "字"},
    {"start_frame": 0, "end_frame": 3, "zh": "字", "en": " "},
    {"start_frame": 0, "end_frame": 3, "zh": "字", "en": "a\n\nb"},
    {"start_frame": 0, "end_frame": 3, "zh": "字", "en": "a\x00b"},
    {"start_frame": 0, "end_frame": 3, "zh": "字", "en": "a --> b"},
    {"start_frame": 0, "end_frame": 3, "zh": "字", "en": "two\nlines"},
])
def test_invalid_cues(cue):
    with pytest.raises(ValidationError):
        SubtitleSpec.model_validate(specification(cues=[cue]))


@pytest.mark.parametrize("changes", [
    {"cues": []}, {"language": "fr"}, {"frames": 0}, {"unexpected": True},
    {"fps": {"numerator": 0, "denominator": 1}},
])
def test_invalid_spec(changes):
    with pytest.raises(ValidationError):
        SubtitleSpec.model_validate(specification(**changes))


def test_overlap_and_out_of_order():
    cues = specification()["cues"]
    with pytest.raises(ValidationError, match="ordered"):
        SubtitleSpec.model_validate(specification(cues=list(reversed(cues))))
    cues[1]["start_frame"] = 59
    with pytest.raises(ValidationError, match="non-overlapping"):
        SubtitleSpec.model_validate(specification(cues=cues))


def test_adjacent_cues_and_millisecond_rounding(tmp_path):
    spec = SubtitleSpec.model_validate(specification(
        language="en", fps={"numerator": 16, "denominator": 1}, frames=3,
        cues=[
            {"start_frame": 0, "end_frame": 1, "en": "First\r\nline"},
            {"start_frame": 1, "end_frame": 3, "en": "Next"},
        ],
    ))
    target = tmp_path / "rounding.srt"
    export_subtitles(spec, str(target))
    assert target.read_text() == (
        "1\n00:00:00,000 --> 00:00:00,063\nFirst\nline\n\n"
        "2\n00:00:00,063 --> 00:00:00,188\nNext\n\n"
    )


def test_destination_errors_preserve_existing_files(tmp_path):
    spec = SubtitleSpec.model_validate(specification())
    existing = tmp_path / "existing.srt"
    existing.write_text("original")
    link = tmp_path / "link.srt"
    link.symlink_to(existing)
    for target in (existing, link):
        with pytest.raises(ChromaError, match="new subtitle destination"):
            export_subtitles(spec, str(target))
    assert existing.read_text() == "original"
    for target in ("relative.srt", str(tmp_path / "wrong.mp4")):
        with pytest.raises(ChromaError):
            export_subtitles(spec, target)
    assert not (tmp_path / "wrong.mp4").exists()


def test_failed_write_removes_partial_file(tmp_path, monkeypatch):
    spec = SubtitleSpec.model_validate(specification())
    target = tmp_path / "failed.srt"
    real_open = Path.open

    class FailedOutput:
        def __enter__(self):
            self.stream = real_open(target, "xb")
            return self

        def write(self, payload):
            self.stream.write(payload[:4])
            raise OSError("disk full")

        def __exit__(self, *args):
            self.stream.close()

    monkeypatch.setattr(
        Path, "open",
        lambda path, *args, **kwargs: FailedOutput() if path == target else real_open(path, *args, **kwargs),
    )
    with pytest.raises(ChromaError, match="字幕写入失败"):
        export_subtitles(spec, str(target))
    assert not target.exists()
