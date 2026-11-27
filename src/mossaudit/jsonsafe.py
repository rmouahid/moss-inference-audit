"""Turn SDK reply objects into plain JSON values, so they can be logged next to raw HTTP replies."""

from __future__ import annotations

import dataclasses
from typing import Any


def json_safe(obj: Any) -> Any:
    """JSON-safe copy of an SDK object: pydantic models, (nested) dataclasses or plain objects.

    Anything else falls back to its repr(), so logging never fails on an unexpected type.
    """
    if obj is None or isinstance(obj, str | int | float | bool):
        return obj
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [json_safe(v) for v in obj]
    if hasattr(obj, "model_dump"):
        return json_safe(obj.model_dump())
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return json_safe(dataclasses.asdict(obj))
    if hasattr(obj, "__dict__"):
        return json_safe(vars(obj))
    return repr(obj)
