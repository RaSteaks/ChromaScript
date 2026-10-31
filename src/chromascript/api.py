"""Local HTTP/UI transport with a mounted, equally capable MCP endpoint."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from collections.abc import Generator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import Receive, Scope, Send

from .artifacts import archive_members, artifact_path, stream_zip
from .cloud_privacy import validation_fields
from .dispatcher import Dispatcher
from .errors import ChromaError
from .job_events import JobEvents
from .mcp_server import create_mcp
from .operations import REGISTRY, catalogue, execute
from .service import Service


class ArchiveResponse(StreamingResponse):
    """Always close the bounded ZIP generator, including ASGI disconnect/cancellation."""

    def __init__(self, chunks: Generator[bytes, None, None], name: str):
        self.chunks = chunks
        super().__init__(
            chunks, media_type="application/zip",
            headers={
                "Content-Disposition": f"attachment; filename*=utf-8''{quote(name)}",
                "Accept-Ranges": "none",
                "Cache-Control": "no-store",
            },
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # No producer thread/queue continues reading the sequence after download ends.
            self.chunks.close()


def create_app(root: Path, port: int = 8765, export_roots: list[Path] | None = None) -> FastAPI:
    root = root.resolve()
    service = Service(root, export_roots)
    job_events = JobEvents()
    token = secrets.token_urlsafe(32)
    # Cookies are host-scoped, not port-scoped. Distinct workspaces on localhost
    # must not overwrite each other's media authentication when opened in two tabs.
    media_cookie = f"chromascript_{port}"
    mcp = create_mcp(root)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/", stateless_http=True, host="127.0.0.1"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        loop = asyncio.get_running_loop()

        def publish_job(job: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(job_events.publish, job)

        service.store.on_job_change = publish_job
        dispatcher = Dispatcher(service.store)
        info = {
            "url": f"http://127.0.0.1:{port}",
            "workspace": str(root),
            "token": token,
            "pid": os.getpid(),
            "job_events": True,
        }
        path = root / "server.json"
        path.write_text(json.dumps(info))
        path.chmod(0o600)
        dispatcher.start()
        try:
            async with mcp_app.router.lifespan_context(mcp_app):
                yield
        finally:
            dispatcher.close()
            service.store.on_job_change = None
            from .motion_sampling import close_samples
            close_samples(service.store)
            # Preview encoders are transient and must not outlive this daemon.
            from .hdr_encoder import close_encoder
            close_encoder()

    app = FastAPI(title="ChromaScript", version="0.1.0", lifespan=lifespan)
    @app.exception_handler(RequestValidationError)
    async def private_cloud_validation(request: Request, exc: RequestValidationError):
        # FastAPI validates before execute(); redact this boundary too, including
        # unknown api_key fields and malformed JSON that could contain secrets.
        if request.url.path in {"/api/operations/generate_speech", "/api/operations/get_speech_config", "/api/operations/get_speech_request", "/api/operations/generate_music", "/api/operations/analyze_media"}:
            return JSONResponse({"error": {"code": "invalid_arguments", "message": "Invalid cloud audio arguments", "fields": validation_fields(exc.errors())}}, status_code=422)
        return await request_validation_exception_handler(request, exc)

    app.state.service = service
    app.state.job_events = job_events
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    @app.middleware("http")
    async def local_access(request: Request, call_next):
        origin = request.headers.get("origin")
        permitted = {
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        }
        if origin and origin not in permitted:
            return JSONResponse(
                {"error": {"code": "origin", "message": "Origin is not allowed"}}, status_code=403
            )
        protected = request.url.path.startswith(("/api/operations/", "/api/artifacts/", "/api/job-events", "/mcp"))
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        # Same-site HttpOnly cookies permit native media range requests; all mutations
        # still require the explicit in-memory bearer token.
        if request.method == "GET" and request.url.path.startswith(("/api/artifacts/", "/api/job-events")):
            supplied = supplied or request.cookies.get(media_cookie, "")
        if protected and not secrets.compare_digest(supplied, token):
            return JSONResponse(
                {"error": {"code": "authentication", "message": "Local access token required"}},
                status_code=401,
            )
        return await call_next(request)

    @app.get("/health")
    def health():
        return {"version": "0.1.0", "workspace": str(root)}

    @app.get("/api/session")
    def session():
        response = JSONResponse({"token": token, "workspace": str(root)})
        response.set_cookie(media_cookie, token, httponly=True, samesite="strict")
        return response

    @app.get("/api/operations")
    def operations():
        return {"operations": catalogue()}

    @app.get("/api/job-events")
    async def subscribe_jobs():
        # This transport only invalidates existing shared list_jobs/get_job reads;
        # it introduces no parallel product operation or task state.
        return StreamingResponse(
            job_events.stream(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/job-events")
    async def notify_jobs(request: Request):
        try:
            model = REGISTRY["get_job"].input_model.model_validate(await request.json())
            job = await run_in_threadpool(service.store.get, "job", model.model_dump()["job_id"])
            job_events.publish(job)
            return {"notified": True}
        except ChromaError as exc:
            return JSONResponse({"error": exc.as_dict()}, status_code=400)
        except (ValidationError, ValueError) as exc:
            return JSONResponse({"error": {"code": "invalid_arguments", "message": str(exc)}}, status_code=400)

    @app.post("/api/operations/encode_motion_hdr_preview/binary")
    async def binary_hdr_preview(request: Request):
        from .motion_hdr_preview import HDRPreviewHeader, encode_hdr_preview_binary

        # This is a transport adapter for the shared encoding operation. The
        # existing bearer/origin middleware protects it identically to JSON calls.
        try:
            declaration = request.headers.get("x-chroma-hdr", "")
            if len(declaration) > 4096:
                raise ValueError("HDR 预览声明过长")
            header = HDRPreviewHeader.model_validate_json(declaration)
            expected = header.frame_bytes * header.frame_count
            payload = bytearray()
            async for block in request.stream():
                if len(payload) + len(block) > expected:
                    raise ValueError("HDR 预览片段长度超过声明")
                payload.extend(block)
            return await run_in_threadpool(encode_hdr_preview_binary, header, payload)
        except ChromaError as exc:
            return JSONResponse({"error": exc.as_dict()}, status_code=400)
        except (ValidationError, ValueError) as exc:
            return JSONResponse({"error": {"code": "invalid_arguments", "message": str(exc)}}, status_code=400)

    def register(name: str) -> None:
        model = REGISTRY[name].input_model

        def handler(request):
            try:
                # Preserve omitted fields across revalidation so format defaults
                # cannot override an explicit gamut/transfer request.
                return execute(service, name, request.model_dump(mode="json", exclude_unset=True))
            except ChromaError as exc:
                return JSONResponse({"error": exc.as_dict()}, status_code=400)
            except (ValidationError, ValueError) as exc:
                return JSONResponse(
                    {"error": {"code": "invalid_arguments", "message": str(exc), "details": {}}},
                    status_code=400,
                )

        handler.__name__ = name
        handler.__annotations__ = {"request": model, "return": Any}
        app.add_api_route("/api/operations/" + name, handler, methods=["POST"], name=name)

    for name in REGISTRY:
        register(name)

    @app.get("/api/artifacts/{artifact_id}")
    def artifact_file(artifact_id: str, request: Request, download: bool = False):
        try:
            artifact = service.store.get("artifact", artifact_id)
            if artifact.get("representation", "file") == "zip-stream":
                if request.headers.get("range"):
                    return JSONResponse(
                        {"error": {"code": "archive_range", "message": "按需 ZIP 不支持断点下载，请重新下载。"}},
                        status_code=416,
                    )
                members = archive_members(service.store, artifact)
                return ArchiveResponse(stream_zip(members), artifact["name"])
            path = artifact_path(service.store, artifact)
            if not path.is_file():
                raise ChromaError(
                    "sequence_directory", "Export this image sequence or read its manifest"
                )
            return FileResponse(
                path,
                media_type=artifact["mime_type"],
                filename=artifact["name"],
                content_disposition_type="attachment" if download else "inline",
            )
        except ChromaError as exc:
            return JSONResponse({"error": exc.as_dict()}, status_code=404)

    app.mount("/mcp", mcp_app)
    frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend.exists():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    else:

        @app.get("/")
        def index():
            return {
                "name": "ChromaScript",
                "message": "Build frontend or use MCP/CLI",
                "docs": "/docs",
            }

    return app
