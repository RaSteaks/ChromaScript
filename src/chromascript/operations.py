"""One typed operation registry is the contract for HTTP, CLI, MCP and UI."""

from __future__ import annotations

import importlib
import inspect
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable, get_type_hints

from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from .cloud_privacy import validation_fields
from .errors import ChromaError


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)


@dataclass(frozen=True)
class Operation:
    name: str
    description: str
    input_model: type[BaseModel]
    serialized: bool = True


REGISTRY: dict[str, Operation] = {}


def operation(description: str, *, serialized: bool = True) -> Callable[..., Any]:
    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        hints = get_type_hints(function)
        # Reflection is the dynamic boundary; Pydantic validates the derived contracts.
        fields: dict[str, Any] = {}
        for name, parameter in inspect.signature(function).parameters.items():
            if name != "self":
                default = ... if parameter.default is inspect.Parameter.empty else parameter.default
                fields[name] = (hints.get(name, Any), default)
        model = create_model(
            function.__name__.title().replace("_", "") + "Input", __base__=Input, **fields
        )
        REGISTRY[function.__name__] = Operation(function.__name__, description, model, serialized)
        return function

    return decorate


def execute(service: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    op = REGISTRY.get(name.removeprefix("chromascript."))
    if not op:
        raise ChromaError("unknown_operation", f"Unknown operation: {name}")
    try:
        request = op.input_model.model_validate(arguments)
    except ValidationError as exc:
        if op.name in {"generate_speech", "get_speech_config", "get_speech_request", "generate_music", "analyze_media"}:
            # Do not echo private scripts or credentials mistakenly supplied as fields.
            raise ChromaError("invalid_arguments", "Invalid cloud audio arguments",
                              fields=validation_fields(exc.errors(include_input=False, include_url=False, include_context=False))) from None
        raise ChromaError("invalid_arguments", str(exc),
                          fields=exc.errors(include_url=False, include_context=False)) from exc
    # Serialize product transitions across HTTP, CLI and model clients. Workers
    # still update their own job records through independent SQLite transactions.
    # Read-only native dialogs may wait for user input; they must not freeze job
    # polling, saving or other clients while ordinary product transitions serialize.
    from .project_storage import workspace_guard
    # Migration owns the exclusive lease itself; all other transports retain a
    # shared lease through both file preparation and the final database commit.
    lease = nullcontext() if op.name == 'migrate_workspace_storage' else workspace_guard(service.store.root)
    with lease, service.operation_lock if op.serialized else nullcontext():
        return getattr(service, op.name)(
            **{key: getattr(request, key) for key in op.input_model.model_fields}
        )


def catalogue() -> list[dict[str, Any]]:
    # Load registration lazily so schema discovery also works without the HTTP app.
    importlib.import_module("chromascript.service")
    return [
        {
            "name": "chromascript." + op.name,
            "description": op.description,
            "inputSchema": op.input_model.model_json_schema(),
        }
        for op in REGISTRY.values()
    ]
