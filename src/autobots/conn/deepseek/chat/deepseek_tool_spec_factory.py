import inspect
import typing
from typing import Any, Dict, List, Optional, Sequence, Type

from loguru import logger
from pydantic import BaseModel

from src.autobots.conn.deepseek.chat.deepseek_models import DeepSeekToolSpec


def _unwrap_annotation(ann: Any) -> Any:
    if ann is inspect._empty:
        return None
    origin = typing.get_origin(ann)
    args = typing.get_args(ann)

    if origin is getattr(typing, "Annotated", None) and args:
        return _unwrap_annotation(args[0])

    if origin is typing.Union and args:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _unwrap_annotation(non_none[0])

    return ann


def _is_pydantic_model(x: Any) -> bool:
    try:
        return isinstance(x, type) and issubclass(x, BaseModel)
    except TypeError:
        return False


def _get_config_model_from_function(fn: Any) -> Optional[Type[BaseModel]]:
    sig = inspect.signature(fn)
    p = sig.parameters.get("config")
    if not p:
        return None
    ann = _unwrap_annotation(p.annotation)
    if _is_pydantic_model(ann):
        return ann
    return None


def _get_config_model_from_action(action_cls: Any) -> Optional[Type[BaseModel]]:
    getter = getattr(action_cls, "get_config_type", None)
    if not getter:
        return None
    try:
        model = getter()
        if _is_pydantic_model(model):
            return model
    except Exception:
        return None
    return None


def _get_tool_description(tool_obj: Any) -> str:
    get_desc = getattr(tool_obj, "get_description", None)
    if callable(get_desc):
        try:
            d = (get_desc() or "").strip()
            if d:
                return d
        except Exception:
            pass
    return (inspect.getdoc(tool_obj) or "").strip()


def _force_additional_properties_false(schema: Any) -> None:
    if not isinstance(schema, dict):
        return
    t = schema.get("type")
    if t == "object" and "properties" in schema and "additionalProperties" not in schema:
        schema["additionalProperties"] = False
    for k, v in list(schema.items()):
        if isinstance(v, dict):
            _force_additional_properties_false(v)
        elif isinstance(v, list):
            for item in v:
                _force_additional_properties_false(item)


def _force_strict_tool_schema(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    DeepSeek strict-mode tool calls require:
      - JSON schema object
      - additionalProperties=false
      - all keys in properties must appear in required
    """
    schema = dict(schema or {})
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    _force_additional_properties_false(schema)

    props = schema.get("properties") or {}
    if isinstance(props, dict):
        required = list(props.keys())
        schema["required"] = required

    return schema


def build_deepseek_tools(
    *,
    tool_names: Sequence[str],
    tools_map: Dict[str, Any],
    strict: bool = False,
) -> List[DeepSeekToolSpec]:
    """
    strict=True enables DeepSeek strict-mode tool calls (beta).
    """
    out: List[DeepSeekToolSpec] = []

    for name in tool_names:
        tool_obj = tools_map.get(name)
        if tool_obj is None:
            raise KeyError(f"Tool '{name}' not found in tools_map")

        desc = _get_tool_description(tool_obj)

        config_model: Optional[Type[BaseModel]] = None
        if inspect.isfunction(tool_obj):
            config_model = _get_config_model_from_function(tool_obj)
        else:
            config_model = _get_config_model_from_action(tool_obj)

        if config_model:
            schema = config_model.model_json_schema()
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
        else:
            schema = {"type": "object", "properties": {}, "additionalProperties": False, "maxProperties": 0}
            logger.warning("Tool '{}' has no detectable Pydantic config model; using empty schema.", name)

        if strict:
            schema = _force_strict_tool_schema(schema)
        else:
            # still a good idea to reduce hallucinated keys (optionally added)
            _force_additional_properties_false(schema)

        out.append(
            DeepSeekToolSpec(
                type="function",
                function={
                    "name": name,
                    "description": desc or "",
                    "parameters": schema,
                },
                strict=bool(strict),
            )
        )

    return out
