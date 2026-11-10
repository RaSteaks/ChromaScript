"""Freeze local glTF dependencies into portable, revision-owned JSON documents."""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import struct
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

MAX_ASSET_BYTES = 64 * 1024 * 1024
MAX_ASSETS = 16
ASSET_NAME = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
# These need extra decoders/workers. Fail at import instead of downloading code
# or silently dropping compressed geometry/textures during an offline render.
UNSUPPORTED_EXTENSIONS = {
    "KHR_draco_mesh_compression", "EXT_meshopt_compression", "KHR_meshopt_compression", "KHR_texture_basisu",
}
# Required extensions must match the pinned Three.js 0.186.1 loader and image policy.
SUPPORTED_EXTENSIONS = {
    "KHR_lights_punctual", "KHR_materials_clearcoat", "KHR_materials_dispersion",
    "KHR_materials_ior", "KHR_materials_sheen", "KHR_materials_specular",
    "KHR_materials_transmission", "KHR_materials_iridescence", "KHR_materials_anisotropy",
    "KHR_materials_unlit", "KHR_materials_volume", "KHR_texture_transform",
    "KHR_mesh_quantization", "KHR_materials_emissive_strength", "EXT_materials_bump",
    "EXT_texture_webp", "EXT_mesh_gpu_instancing",
}
IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp"}


def gltf_document(source: str) -> dict[str, Any]:
    try:
        value = json.loads(source)
    except (ValueError, RecursionError) as exc:
        raise ValueError("glTF JSON 无效。") from exc
    if not isinstance(value, dict) or not isinstance(value.get("asset"), dict):
        raise ValueError("资源必须是 glTF 2.0 文档。")
    if value["asset"].get("version") != "2.0":
        raise ValueError("仅支持 glTF 2.0 / GLB 2。")
    for field in ("extensionsUsed", "extensionsRequired"):
        extensions = value.get(field, [])
        if not isinstance(extensions, list) or any(not isinstance(e, str) for e in extensions):
            raise ValueError(f"glTF {field} 必须是字符串数组。")
        unsupported = UNSUPPORTED_EXTENSIONS.intersection(extensions)
        if unsupported:
            raise ValueError(f"请导出未压缩 glTF/GLB 和 PNG/JPEG/WebP 贴图；暂不支持 {', '.join(sorted(unsupported))}。")
        if field == "extensionsRequired" and set(extensions) - SUPPORTED_EXTENSIONS:
            raise ValueError("glTF 包含当前本地加载器不支持的必需扩展。")
    for field in ("buffers", "images", "nodes", "meshes", "materials", "textures", "skins", "animations", "cameras", "scenes", "accessors"):
        entries = value.get(field, [])
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise ValueError(f"glTF {field} 必须是对象数组。")
    return value


def describe_gltf(source: str) -> dict[str, Any]:
    """Expose declared names/links, not invented bounds or a visual certification."""
    document = gltf_document(source)
    buffers: dict[int, bytes] = {}
    animations = []
    for index, animation in enumerate(document.get("animations", [])):
        samplers = animation.get("samplers", [])
        if not isinstance(samplers, list) or any(not isinstance(s, dict) for s in samplers):
            raise ValueError("glTF animation samplers 必须是对象数组。")
        ranges = [animation_time_range(document, buffers, sampler.get("input")) for sampler in samplers]
        complete = bool(ranges) and all(item is not None for item in ranges)
        known = [item for item in ranges if item is not None]
        start = min(item[0] for item in known) if complete else None
        end = max(item[1] for item in known) if complete else None
        animations.append({
            "index": index, "name": animation.get("name", ""), "channels": animation.get("channels", []),
            "keyframe_range": {"start_seconds": start, "end_seconds": end, "span_seconds": end - start}
            if start is not None and end is not None else None,
            "timing_basis": "decoded FLOAT SCALAR input accessors" if complete else "unavailable: sparse, implicit or absent time accessor",
        })
    # Blender uses NLA/action names and node indices to connect rigs and morphs.
    # Keep those identities intact so callers can select clips without guessing.
    return {
        "asset": document["asset"],
        "default_scene": document.get("scene", 0),
        "scenes": document.get("scenes", []),
        "nodes": [
            {"index": index, **{key: node[key] for key in (
                "name", "mesh", "skin", "camera", "children", "translation", "rotation", "scale", "matrix",
            ) if key in node}}
            for index, node in enumerate(document.get("nodes", []))
        ],
        "animations": animations,
        "skins": document.get("skins", []),
        "meshes": [
            {"index": index, "name": mesh.get("name", ""),
             "morph_target_names": mesh.get("extras", {}).get("targetNames", []) if isinstance(mesh.get("extras", {}), dict) else []}
            for index, mesh in enumerate(document.get("meshes", []))
        ],
        "materials": document.get("materials", []),
        "images": [{key: image[key] for key in ("name", "mimeType", "bufferView") if key in image}
                   for image in document.get("images", [])],
        "texture_count": len(document.get("textures", [])),
        "extensions_used": document.get("extensionsUsed", []),
        "unsupported_optional_extensions": sorted(set(document.get("extensionsUsed", [])) - SUPPORTED_EXTENSIONS),
        "inspection": "declared names/structure and decoded keyframe ranges; bounds and visual appearance require runtime inspection",
    }


def animation_time_range(
    document: dict[str, Any], buffers: dict[int, bytes], index: Any,
) -> tuple[float, float] | None:
    """Read actual sampler times, not accessor min/max or Blender frame numbers."""
    accessors = document.get("accessors", [])
    if type(index) is not int or not 0 <= index < len(accessors):
        raise ValueError("glTF 动画输入 accessor 索引无效。")
    accessor = accessors[index]
    if accessor.get("componentType") != 5126 or accessor.get("type") != "SCALAR":
        raise ValueError("glTF 动画时间须使用 FLOAT SCALAR accessor。")
    if "sparse" in accessor or "bufferView" not in accessor:
        return None  # Do not invent a range for a storage layout not decoded here.
    views = document.get("bufferViews", [])
    view_index = accessor["bufferView"]
    if type(view_index) is not int or not 0 <= view_index < len(views):
        raise ValueError("glTF 动画时间 bufferView 索引无效。")
    view = views[view_index]
    count, offset, stride = accessor.get("count"), accessor.get("byteOffset", 0), view.get("byteStride", 4)
    if (type(count) is not int or count < 1 or type(offset) is not int or offset < 0
            or type(stride) is not int or stride < 4 or stride % 4
            or offset + (count - 1) * stride + 4 > view["byteLength"]):
        raise ValueError("glTF 动画时间 accessor 超出 bufferView 范围。")
    buffer_index = view["buffer"]
    if buffer_index not in buffers:
        buffers[buffer_index] = data_uri(document["buffers"][buffer_index]["uri"], False)
    contents = buffers[buffer_index]
    offset += view.get("byteOffset", 0)
    first = last = 0.0
    for sample in range(count):
        value = struct.unpack_from("<f", contents, offset + sample * stride)[0]
        if not math.isfinite(value) or value < 0 or (sample and value <= last):
            raise ValueError("glTF 动画时间必须有限、非负且严格递增。")
        if sample == 0:
            first = value
        last = value
    return first, last


def data_uri(uri: str, image: bool) -> bytes:
    header, separator, encoded = uri.partition(",")
    mime = header.removeprefix("data:").removesuffix(";base64")
    allowed = IMAGE_MIMES if image else {"application/octet-stream", "application/gltf-buffer"}
    if not separator or not header.startswith("data:") or not header.endswith(";base64") or mime not in allowed:
        raise ValueError("glTF 内嵌资源须使用受支持 MIME 类型的 Base64 data URI。")
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("glTF 资源的 Base64 数据无效。") from exc


def validate_assets(assets: dict[str, str]) -> None:
    """Validate the public spec boundary, including specs submitted without import."""
    if len(assets) > MAX_ASSETS or sum(len(s.encode()) for s in assets.values()) > MAX_ASSET_BYTES:
        raise ValueError("最多支持 16 个 glTF 资源，内嵌 JSON 合计最多 64 MiB。")
    names: set[str] = set()
    for name, source in assets.items():
        if not ASSET_NAME.fullmatch(name) or name.lower() in names:
            raise ValueError("资源名须为 1–80 位英文字母、数字、下划线或连字符，且不能仅大小写不同。")
        names.add(name.lower())
        document = gltf_document(source)
        buffers = document.get("buffers", [])
        for buffer in buffers:
            uri, length = buffer.get("uri"), buffer.get("byteLength")
            if not isinstance(uri, str) or not uri.startswith("data:"):
                raise ValueError("glTF buffer 必须内嵌；请用 import_motion_gltf 导入本地依赖。")
            if type(length) is not int or length < 1 or len(data_uri(uri, False)) != length:
                raise ValueError("glTF buffer 的实际字节数与 byteLength 不符。")
        views = document.get("bufferViews", [])
        if not isinstance(views, list):
            raise ValueError("glTF bufferViews 必须是数组。")
        for view in views:
            if not isinstance(view, dict):
                raise ValueError("glTF bufferView 无效。")
            index, offset, length = view.get("buffer"), view.get("byteOffset", 0), view.get("byteLength")
            if (type(index) is not int or not 0 <= index < len(buffers)
                    or type(offset) is not int or offset < 0 or type(length) is not int or length < 1
                    or offset + length > buffers[index]["byteLength"]):
                raise ValueError("glTF bufferView 超出内嵌 buffer 范围。")
        for image in document.get("images", []):
            if "uri" in image:
                if "bufferView" in image or not isinstance(image["uri"], str):
                    raise ValueError("glTF image 必须使用唯一的 uri 或 bufferView。")
                data_uri(image["uri"], True)
            else:
                index = image.get("bufferView")
                if type(index) is not int or not 0 <= index < len(views) or image.get("mimeType") not in IMAGE_MIMES:
                    raise ValueError("glTF image 的 bufferView 或 mimeType 无效。")


def import_gltf(path: Path) -> str:
    """Read only the selected model and dependencies inside its containing folder."""
    path = path.expanduser().resolve()
    if path.suffix.lower() not in {".gltf", ".glb"}:
        raise ValueError("请选择本地 .gltf 或 .glb 文件。")

    def read(file: Path) -> bytes:
        if not file.is_file():
            raise ValueError(f"缺少 glTF 文件或依赖：{file.name}")
        if file.stat().st_size > MAX_ASSET_BYTES:
            raise ValueError("单个 glTF 文件或依赖不得超过 64 MiB。")
        return file.read_bytes()

    raw = read(path)
    binary: bytes | None = None
    if path.suffix.lower() == ".glb":
        if len(raw) < 20 or struct.unpack_from("<4sII", raw) != (b"glTF", 2, len(raw)):
            raise ValueError("GLB 2 文件头或声明长度无效。")
        offset, chunks = 12, []
        while offset < len(raw):
            if offset + 8 > len(raw):
                raise ValueError("GLB chunk 头不完整。")
            length, kind = struct.unpack_from("<II", raw, offset)
            offset += 8
            if length % 4 or offset + length > len(raw):
                raise ValueError("GLB chunk 长度或对齐无效。")
            chunks.append((kind, raw[offset:offset + length]))
            offset += length
        if not chunks or chunks[0][0] != 0x4E4F534A or len(chunks) > 2 or (len(chunks) == 2 and chunks[1][0] != 0x004E4942):
            raise ValueError("GLB 须包含 JSON chunk 和可选的 BIN chunk。")
        raw = chunks[0][1]
        binary = chunks[1][1] if len(chunks) == 2 else None
    document = gltf_document(raw.decode("utf-8"))
    total = len(raw)
    for field in ("buffers", "images"):
        for index, item in enumerate(document.get(field, [])):
            uri = item.get("uri")
            if uri is None:
                if field == "images":
                    continue  # Embedded image bufferViews are checked with the complete graph below.
                length = item.get("byteLength")
                if index != 0 or binary is None or type(length) is not int or not 0 < length <= len(binary) or len(binary) - length > 3:
                    raise ValueError("GLB BIN chunk 缺失或长度与首个 buffer 不符。")
                contents, mime = binary[:length], "application/octet-stream"
            elif not isinstance(uri, str):
                raise ValueError("glTF uri 必须是字符串。")
            elif uri.startswith("data:"):
                continue
            else:
                parts = urlsplit(uri)
                relative = Path(unquote(parts.path))
                if parts.scheme or parts.netloc or parts.query or parts.fragment or relative.is_absolute() or "\\" in str(relative):
                    raise ValueError("glTF 依赖须为模型目录内的相对路径；不能引用网络或绝对路径。")
                dependency = (path.parent / relative).resolve()
                if not dependency.is_relative_to(path.parent):
                    raise ValueError("glTF 依赖不能越出模型目录（含符号链接）。")
                contents = read(dependency)
                mime = "application/octet-stream"
                if field == "images":
                    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(dependency.suffix.lower(), "")
                    if not mime:
                        raise ValueError("glTF 贴图仅支持 PNG、JPEG 和 WebP。")
            total += len(contents)
            if total > MAX_ASSET_BYTES:
                raise ValueError("glTF 及其依赖合计超过 64 MiB。")
            item["uri"] = f"data:{mime};base64," + base64.b64encode(contents).decode("ascii")
    source = json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    validate_assets({"imported": source})
    return source
