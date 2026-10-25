"""Large project listings remain compact while explicit metadata and cloning stay lossless."""

import json

from chromascript.audio import AudioSynthesis
from chromascript.operations import execute
from chromascript.service import Service


def test_summary_omits_probe_score_and_raw_logs_and_keeps_revision(tmp_path):
    service = Service(tmp_path / "workspace")
    pid = service.create_project("Large audio")["project"]["id"]
    asset = service.create_audio(pid, "Original", AudioSynthesis(duration_seconds=0.1))["asset"]
    meta = asset["metadata"]
    meta["probe"]["large"] = "x" * 100000
    meta["generation_report"] = {"score": "y" * 100000}
    meta["clipped_samples"] = 3
    meta["loudness"]["raw_summary"] = "z" * 100000
    service.store.put("audio_asset", asset["id"], asset)
    for n in range(12):
        duplicate = {**asset, "id": f"asset-{n}"}
        service.store.put("audio_asset", duplicate["id"], duplicate)
    summary = execute(service, "get_project_audio", {"project_id": pid})
    full = execute(service, "get_project_audio", {"project_id": pid, "include_metadata": True})
    assert len(summary["assets"]) == len(full["assets"]) == 13
    assert summary["timeline"] == full["timeline"] and summary["revision_id"] == full["revision_id"]
    assert not summary["includes_metadata"] and full["includes_metadata"]
    assert summary["assets"][0]["metadata"]["clipped_samples"] == 3
    assert "probe" not in summary["assets"][0]["metadata"]
    assert "generation_report" not in summary["assets"][0]["metadata"]
    assert set(summary["assets"][0]["loudness"]) == {
        "integrated_lufs",
        "loudness_range_lu",
        "true_peak_dbtp",
    }
    assert len(json.dumps(summary)) < len(json.dumps(full)) / 100
    assert full["assets"][0]["metadata"] == meta
