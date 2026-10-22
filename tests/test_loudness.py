"""Actual final-WAV loudness, undefined measurements and publication failures."""

import json
import subprocess

import numpy as np
import pytest

from chromascript.audio import AudioEvent, AudioSynthesis, measure_loudness, pcm_bytes, pcm_writer
from chromascript.errors import ChromaError
from chromascript.service import Service


def test_tone_silence_short_clip_and_intersample_peaks(tmp_path):
    path = tmp_path / "measure.wav"
    # A one-channel 1 kHz reference at -6 dBFS has approximately -9 LUFS.
    tone = np.zeros((48000, 2))
    tone[:, 0] = np.sin(np.arange(48000) * 2 * np.pi * 1000 / 48000) * 10 ** (-6 / 20)
    with pcm_writer(path) as writer:
        writer.writeframes(pcm_bytes(tone))
    original = path.read_bytes()
    report = measure_loudness(path)
    assert report["integrated_lufs"] == pytest.approx(-9, abs=0.2)
    assert report["true_peak_dbtp"] == pytest.approx(-6, abs=0.1)
    assert report["loudness_range_lu"] == 0
    assert report["ffmpeg_version"].startswith("ffmpeg version ")
    assert path.read_bytes() == original
    for count in (48, 4800, 48000):
        with pcm_writer(path) as writer:
            writer.writeframes(bytes(count * 8))
        silence = measure_loudness(path)
        assert silence["integrated_lufs"] is None and silence["true_peak_dbtp"] is None
        json.dumps(silence, allow_nan=False)
    with pcm_writer(path) as writer:
        writer.writeframes(pcm_bytes(tone[:4800]))
    short = measure_loudness(path)
    assert short["integrated_lufs"] is None
    assert short["true_peak_dbtp"] == pytest.approx(-6, abs=0.1)
    # Phase-offset Fs/4 sine has lower discrete sample peaks than reconstructed peaks.
    tone[:, 0] = 0.8 * np.sin(np.arange(48000) * np.pi / 2 + np.pi / 4)
    with pcm_writer(path) as writer:
        writer.writeframes(pcm_bytes(tone))
    assert measure_loudness(path)["true_peak_dbtp"] > 20 * np.log10(np.max(np.abs(tone))) + 2


@pytest.mark.parametrize("output", ["", "Summary:\n I: -20 LUFS", "Summary:\n Integrated loudness:\n I: nan LUFS"])
def test_missing_or_malformed_summary_is_an_error(tmp_path, monkeypatch, output):
    monkeypatch.setattr("chromascript.audio.shutil.which", lambda _: "/ffmpeg")
    monkeypatch.setattr("chromascript.audio.run_audio_process", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "", output))
    with pytest.raises(ChromaError, match="summary"):
        measure_loudness(tmp_path / "input.wav")


def test_only_final_summary_is_used(tmp_path, monkeypatch):
    # Startup/per-frame/sample-peak diagnostics must never masquerade as true peak.
    output = """ffmpeg version test-build
Summary:
 Integrated loudness:
 I: -70.0 LUFS
 t: 0.1 I: -70 LUFS
Summary:
 Integrated loudness:
 I: -18.3 LUFS
 Threshold: -29.0 LUFS
 Loudness range:
 LRA: 2.1 LU
 Sample peak:
 Peak: -6.0 dBFS
 True peak:
 Peak: +0.2 dBFS
random progress log
"""
    monkeypatch.setattr("chromascript.audio.shutil.which", lambda _: "/ffmpeg")
    monkeypatch.setattr("chromascript.audio.run_audio_process", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "", output))
    report = measure_loudness(tmp_path / "input.wav")
    assert report["integrated_lufs"] == -18.3
    assert report["loudness_range_lu"] == 2.1
    assert report["true_peak_dbtp"] == 0.2
    assert "random progress" not in report["summary"]


def test_failed_measurement_publishes_no_asset(tmp_path, monkeypatch):
    service = Service(tmp_path / "workspace")
    pid = service.create_project("loudness failure")["project"]["id"]
    monkeypatch.setattr("chromascript.audio.run_audio_process", lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", "ebur128 failed"))
    with pytest.raises(ChromaError, match="loudness measurement failed"):
        service.create_audio(pid, "tone", AudioSynthesis(duration_seconds=1, events=[AudioEvent()]))
    assert service.store.list("audio_asset") == service.store.list("artifact") == []
    assert not list(service.store.root.glob("audio-create-*"))
    assert not list(service.store.root.glob("audio/*.wav"))
