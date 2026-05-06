"""
Minimal @function_tool decorator: exposes name, description, params_json_schema for OpenAI-style tools.
"""
from __future__ import annotations

import inspect
from functools import wraps
from typing import Any, Callable, Optional, Type, Union, get_args, get_origin


def _strip_optional(annotation: Any) -> Any:
    origin = get_origin(annotation)
    if origin is Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _schema_for_type(tp: Any) -> dict[str, Any]:
    tp = _strip_optional(tp)
    if tp is str or tp == "str":
        return {"type": "string"}
    if tp is int or tp == "int":
        return {"type": "integer"}
    if tp is bool or tp == "bool":
        return {"type": "boolean"}
    if tp is float or tp == "float":
        return {"type": "number"}
    return {"type": "string"}


def _build_params_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    sig = inspect.signature(fn)
    try:
        hints = inspect.get_annotations(fn, eval_str=True)
    except Exception:
        hints = getattr(fn, "__annotations__", {})

    properties: dict[str, Any] = {}
    required: list[str] = []

    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        ann = hints.get(pname, str if param.annotation is inspect.Parameter.empty else param.annotation)
        if ann is inspect.Parameter.empty:
            ann = str
        prop = dict(_schema_for_type(ann))
        if param.default is inspect.Parameter.empty:
            required.append(pname)
        properties[pname] = prop

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def function_tool(
    func: Optional[Callable[..., Any]] = None,
    *,
    name_override: Optional[str] = None,
    strict_json_schema: bool = True,
) -> Any:
    """Decorate an async tool function; sets .name, .description, .params_json_schema."""

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name_override or fn.__name__
        description = (inspect.getdoc(fn) or "").strip()

        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await fn(*args, **kwargs)

        wrapper.name = tool_name  # type: ignore[attr-defined]
        wrapper.description = description  # type: ignore[attr-defined]
        wrapper.params_json_schema = _build_params_schema(fn)  # type: ignore[attr-defined]
        wrapper.strict_json_schema = strict_json_schema  # type: ignore[attr-defined]
        wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
        return wrapper

    if func is not None and callable(func):
        return decorator(func)
    return decorator
