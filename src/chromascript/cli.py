"""CLI convenience commands and a generic typed call cover the same MCP operations."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Annotated

import typer

from .errors import ChromaError
from .runtime import call, ensure_daemon

app = typer.Typer(no_args_is_help=True)
workspace = Path.cwd() / "workspace"


@app.callback()
def configure(
    directory: Annotated[Path, typer.Option("--workspace", "-w")] = Path("workspace"),
) -> None:
    global workspace
    workspace = directory.expanduser().resolve()


@app.command()
def serve(
    port: int = 8765,
    export_root: Annotated[list[Path] | None, typer.Option("--export-root")] = None,
) -> None:
    """Start the persistent local UI/API/MCP daemon."""
    import uvicorn

    from .api import create_app

    if port == 0:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
    uvicorn.run(
        create_app(workspace, port, export_root), host="127.0.0.1", port=port, log_level="info"
    )


@app.command()
def mcp() -> None:
    """Expose every capability through stdio; jobs remain owned by the daemon."""
    from .mcp_server import create_mcp

    ensure_daemon(workspace)
    create_mcp(workspace).run(transport="stdio")


@app.command("call")
def invoke(operation: str, arguments: str = "{}") -> None:
    """Call any operation with JSON (or @path/to/input.json)."""
    try:
        value = json.loads(
            Path(arguments[1:]).read_text() if arguments.startswith("@") else arguments
        )
        typer.echo(json.dumps(call(workspace, operation, value), ensure_ascii=False, indent=2))
    except ChromaError as exc:
        typer.echo(json.dumps({"error": exc.as_dict()}, ensure_ascii=False), err=True)
        raise typer.Exit(1) from exc


@app.command()
def doctor() -> None:
    """Check native rendering and encoding environment."""
    typer.echo(json.dumps(call(workspace, "check_environment", {}), ensure_ascii=False, indent=2))


@app.command()
def operations() -> None:
    """Print all operation schemas without starting the daemon."""
    from .operations import catalogue

    typer.echo(json.dumps(catalogue(), ensure_ascii=False, indent=2))


@app.command()
def preview(scene_id: str, frame: int = 0, width: int = 960, height: int = 540) -> None:
    """Queue a frame preview."""
    typer.echo(
        json.dumps(
            call(
                workspace,
                "render_frame",
                {"scene_id": scene_id, "frame": frame, "width": width, "height": height},
            ),
            indent=2,
        )
    )


@app.command()
def render(
    scene_id: str,
    container: str = "mp4",
    codec: str | None = None,
    profile: str | None = None,
    bit_depth: int | None = None,
    sampling: str | None = None,
    color_output: str | None = None,
    color_space: str | None = None,
    gamma: str | None = None,
    alpha_mode: str | None = None,
    data_range: str | None = None,
    compression: str | None = None,
    crf: int | None = None,
    bitrate_kbps: int | None = None,
    preset: Annotated[
        list[str] | None, typer.Option("--preset", help="Legacy preset-ID compatibility")
    ] = None,
    width: int = 3840,
    height: int = 2160,
    frames: int = 300,
    fps_numerator: int = 60,
    fps_denominator: int = 1,
    key: Annotated[str, typer.Option("--key")] = "",
) -> None:
    """Queue directly configured output; unspecified fields use linked compatible defaults."""
    from .store import identifier

    args = {
        "scene_id": scene_id,
        "idempotency_key": key or identifier(),
        "settings": {
            "width": width,
            "height": height,
            "frames": frames,
            "fps": {"numerator": fps_numerator, "denominator": fps_denominator},
        },
    }
    fields = {
        "profile": profile,
        "bit_depth": bit_depth,
        "sampling": sampling,
        "color_output": color_output,
        "color_space": color_space,
        "gamma": gamma,
        "alpha_mode": alpha_mode,
        "data_range": data_range,
        "compression": compression,
    }
    from .models import normalize_delivery_field

    # Human aliases compare against the same canonical values stored by the service.
    explicit = {k: normalize_delivery_field(k, v) for k, v in fields.items() if v is not None}
    try:
        if preset:
            if (
                explicit
                or codec is not None
                or container != "mp4"
                or crf is not None
                or bitrate_kbps is not None
            ):
                raise ChromaError("delivery_arguments", "不能将旧预设与直接交付参数混用。")
            result = call(workspace, "submit_delivery", {**args, "preset_ids": preset})
        else:
            # A legacy output alias is an alternative to the new gamut/gamma pair.
            if color_output is not None and (color_space is not None or gamma is not None):
                raise ChromaError(
                    "delivery_arguments", "不能将 --color-output 与 --color-space / --gamma 混用。"
                )
            if crf is not None and bitrate_kbps is not None:
                raise ChromaError("delivery_arguments", "请选择 CRF 或目标码率。")
            initial = {"container": container, **({"codec": codec} if codec is not None else {})}
            base = call(
                workspace,
                "get_delivery_options",
                {"configuration": initial, "changed_field": "codec" if codec else "container"},
            )["configuration"]
            if codec is not None and base["codec"] != codec:
                raise ChromaError("delivery_arguments", "该封装不支持指定编码，请修改封装或编码。")
            if color_output is not None:
                from .models import OUTPUT_ENCODINGS

                if color_output in OUTPUT_ENCODINGS:
                    base["color_space"], base["gamma"] = OUTPUT_ENCODINGS[color_output]
            if color_space is not None or gamma is not None:
                # Let the shared operation infer missing color components and
                # report invalid values through the same structured error path.
                base = {
                    k: v
                    for k, v in {**base, **explicit}.items()
                    if k not in {"color_output", "color_space", "gamma"} or k in explicit
                }
            if crf is not None:
                explicit.update(quality_mode="crf", crf=crf)
            if bitrate_kbps is not None:
                explicit.update(quality_mode="bitrate", bitrate_kbps=bitrate_kbps)
            resolved = call(
                workspace,
                "get_delivery_options",
                {
                    "configuration": {**base, **explicit},
                    "changed_field": next(reversed(explicit), None),
                },
            )["configuration"]
            # Defaults may adapt, but an explicitly supplied CLI value must not silently change.
            conflicts = [field for field, value in explicit.items() if resolved[field] != value]
            if conflicts:
                raise ChromaError(
                    "delivery_arguments",
                    "指定参数互不兼容。",
                    fields=conflicts,
                    suggested_configuration=resolved,
                )
            result = call(
                workspace, "submit_configured_delivery", {**args, "configurations": [resolved]}
            )
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    except ChromaError as exc:
        typer.echo(json.dumps({"error": exc.as_dict()}, ensure_ascii=False), err=True)
        raise typer.Exit(1) from exc


@app.command()
def inspect(artifact_id: str) -> None:
    """Read original frame statistics or actual media stream properties."""
    value = call(workspace, "get_artifact", {"artifact_id": artifact_id})["artifact"]
    operation = "inspect_frame" if value["mime_type"].startswith("image/") else "probe_media"
    typer.echo(
        json.dumps(
            call(workspace, operation, {"artifact_id": artifact_id}), ensure_ascii=False, indent=2
        )
    )


@app.command()
def resume(job_id: str) -> None:
    """Continue an interrupted render from its verified frame cache."""
    typer.echo(json.dumps(call(workspace, "resume_job", {"job_id": job_id}), indent=2))


if __name__ == "__main__":
    app()
