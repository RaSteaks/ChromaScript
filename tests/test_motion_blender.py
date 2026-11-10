"""Real Blender exports retain their rig, morphs, material semantics and identities."""

import base64
import io
import json
from pathlib import Path

import pytest
from PIL import Image
from pydantic import ValidationError

from chromascript.errors import ChromaError
from chromascript.motion import MotionSpec
from chromascript.motion_assets import describe_gltf, import_gltf
from chromascript.operations import catalogue, execute
from chromascript.service import Service

FIXTURE = Path(__file__).parent / "fixtures" / "motion-assets" / "blender"


@pytest.mark.parametrize("extension", ["glb", "gltf"])
def test_real_blender_export_preserves_rig_morph_material_and_transform(extension):
    source = import_gltf(FIXTURE / f"character.{extension}")
    doc = json.loads(source)
    info = describe_gltf(source)
    assert info["asset"]["generator"] == "Khronos glTF Blender I/O v3.6.28"
    assert info["animations"][0]["name"] == "BendAndSquash"
    assert info["animations"][0]["keyframe_range"] == pytest.approx({
        "start_seconds": 1 / 24, "end_seconds": 25 / 24, "span_seconds": 1,
    })
    assert {channel["target"]["path"] for channel in info["animations"][0]["channels"]} == {"translation", "rotation", "scale", "weights"}
    assert len(info["skins"][0]["joints"]) == 2
    assert info["meshes"][0]["morph_target_names"] == ["Squash"]
    material = info["materials"][0]
    assert material["alphaMode"] == "BLEND" and material["doubleSided"] is True
    assert material["extensions"]["KHR_materials_clearcoat"]["clearcoatFactor"] == pytest.approx(0.8)
    assert "baseColorTexture" in material["pbrMetallicRoughness"]
    assert "metallicRoughnessTexture" in material["pbrMetallicRoughness"]
    assert "normalTexture" in material and info["texture_count"] == 3
    image = doc["images"][doc["textures"][material["pbrMetallicRoughness"]["metallicRoughnessTexture"]["index"]]["source"]]
    if "uri" in image:
        encoded = base64.b64decode(image["uri"].split(",", 1)[1])
    else:
        view = doc["bufferViews"][image["bufferView"]]
        buffer = base64.b64decode(doc["buffers"][view["buffer"]]["uri"].split(",", 1)[1])
        offset = view.get("byteOffset", 0)
        encoded = buffer[offset:offset + view["byteLength"]]
    with Image.open(io.BytesIO(encoded)) as decoded:
        # Verify actual Blender channel packing: G=roughness .42, B=metalness .1.
        assert decoded.getpixel((0, 0)) == (255, 107, 26)
    assert "KHR_lights_punctual" in info["extensions_used"]
    root = next(node for node in info["nodes"] if node.get("name") == "ModelRoot")
    # The exporter already maps Blender Z-up into glTF Y-up. Preserve its meters,
    # quaternion and hierarchy instead of applying another rotation or normalization.
    assert root["translation"] == pytest.approx([0.1, 0.2, 0])
    assert root["scale"] == [1.25, 1.25, 1.25]
    assert abs(root["rotation"][1]) > 0.09
    if extension == "glb":
        assert all("bufferView" in image for image in doc["images"])
    else:
        assert all(image["uri"].startswith("data:image/png;base64,") for image in doc["images"])
        original = json.loads((FIXTURE / "character.gltf").read_text())
        assert any("%" in image["uri"] for image in original["images"])
    assert "base64," not in json.dumps(info)


def test_asset_inspection_is_revision_bound_read_only_and_discoverable(tmp_path):
    service = Service(tmp_path)
    project = service.create_project("Blender metadata")["project"]["id"]
    source = import_gltf(FIXTURE / "character.glb")
    spec = MotionSpec(engine="remotion-sdr-v1", code="export default function(){return null;}", assets={"character": source})
    saved = service.create_motion_scene(project, spec=spec)
    args = {"scene_id": saved["record"]["id"], "name": "character"}
    before = service.store.list("motion_revision")
    info = execute(service, "get_motion_asset_info", args)
    assert info["revision_id"] == saved["revision_id"]
    assert info["metadata"]["animations"][0]["name"] == "BendAndSquash"
    assert info["size_bytes"] == len(source.encode())
    assert service.store.list("motion_revision") == before
    assert "chromascript.get_motion_asset_info" in {op["name"] for op in catalogue()}
    service.update_motion_scene(args["scene_id"], spec.model_copy(update={"assets": {}}), saved["revision_id"])
    with pytest.raises(ChromaError, match="不存在"):
        execute(service, "get_motion_asset_info", args)
    assert execute(service, "get_motion_asset_info", {**args, "revision_id": saved["revision_id"]}) == info


def test_animation_ranges_are_decoded_instead_of_trusting_accessor_metadata():
    source = import_gltf(FIXTURE / "character.glb")
    original = describe_gltf(source)["animations"]
    doc = json.loads(source)
    for sampler in doc["animations"][0]["samplers"]:
        accessor = doc["accessors"][sampler["input"]]
        accessor["min"], accessor["max"] = [100], [200]
    assert describe_gltf(json.dumps(doc))["animations"] == original
    doc["accessors"][doc["animations"][0]["samplers"][0]["input"]]["byteOffset"] = 1000000
    with pytest.raises(ValueError, match="超出"):
        describe_gltf(json.dumps(doc))


@pytest.mark.parametrize("field", ["nodes", "animations", "meshes", "skins"])
def test_inspection_fields_are_validated_at_the_spec_boundary(field):
    doc = json.loads(import_gltf(FIXTURE / "character.glb"))
    doc[field] = [None]
    with pytest.raises(ValidationError):
        MotionSpec(code="export {};", assets={"broken": json.dumps(doc)})
