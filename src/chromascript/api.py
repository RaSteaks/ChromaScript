"""Local HTTP/UI transport with a mounted, equally capable MCP endpoint."""

from __future__ import annotations

import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .dispatcher import Dispatcher
from .errors import ChromaError
from .mcp_server import create_mcp
from .operations import REGISTRY, catalogue, execute
from .service import Service


def create_app(root: Path, port: int = 8765, export_roots: list[Path] | None = None) -> FastAPI:
    root = root.resolve()
    service = Service(root, export_roots)
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
        dispatcher = Dispatcher(service.store)
        info = {
            "url": f"http://127.0.0.1:{port}",
            "workspace": str(root),
            "token": token,
            "pid": os.getpid(),
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

    app = FastAPI(title="ChromaScript", version="0.1.0", lifespan=lifespan)
    app.state.service = service
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
        protected = request.url.path.startswith(("/api/operations/", "/api/artifacts/", "/mcp"))
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        # Same-site HttpOnly cookies permit native media range requests; all mutations
        # still require the explicit in-memory bearer token.
        if request.method == "GET" and request.url.path.startswith("/api/artifacts/"):
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
    def artifact_file(artifact_id: str, download: bool = False):
        try:
            artifact = service.store.get("artifact", artifact_id)
            path = service.store.file(artifact["relative_path"])
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
