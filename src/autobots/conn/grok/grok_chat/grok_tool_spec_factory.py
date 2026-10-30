import inspect
import typing
from typing import Any, Dict, List, Optional, Sequence, Type

from loguru import logger
from pydantic import BaseModel

from src.autobots.conn.grok.grok_chat.grok_models import CustomToolSpec
from src.autobots.conn.grok.grok_chat.grok_tool_schema_simplify import (
    SimplifyOptions,
    simplify_schema,
)
from src.autobots.llm.tools.tools_map import TOOLS_MAP


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


def _schema_from_pydantic_model(
    model: Type[BaseModel], *, forbid_additional: bool
) -> Dict[str, Any]:
    schema = model.model_json_schema()
    if forbid_additional:
        _force_additional_properties_false(schema)

    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def _force_additional_properties_false(schema: Any) -> None:
    """
    Recursively sets additionalProperties=False on object schemas that have properties.
    This usually increases tool-call reliability (model stops inventing keys).
    """
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
    # ActionABC style: get_config_type() -> Pydantic model class
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
    # .get_description() is for only action tools
    # Prefer Action.get_description() when present
    get_desc = getattr(tool_obj, "get_description", None)
    if callable(get_desc):
        try:
            d = (get_desc() or "").strip()
            if d:
                return d
        except Exception:
            pass

    return (inspect.getdoc(tool_obj) or "").strip()


def build_custom_tools_from_tools_map(
    tool_names: Sequence[str],
    *,
    forbid_additional: bool = True,
    on_missing: str = "skip",  # "skip" | "error"
) -> List[CustomToolSpec]:
    out: List[CustomToolSpec] = []

    # Tune defaults for Grok grammar stability
    simp_opts = SimplifyOptions(
        max_depth=5,
        max_nodes=800,
        max_properties_per_object=60,
        max_required=40,
        max_union_branches=2,
        top_level_additional_properties=False,
        nested_additional_properties=True,
        resolve_local_refs=True,
        degrade_to_type="string",
    )

    for name in tool_names:
        tool_obj = TOOLS_MAP.get(name)
        if tool_obj is None:
            if on_missing == "error":
                raise KeyError(f"Tool '{name}' not found in TOOLS_MAP")
            continue

        desc = _get_tool_description(tool_obj)

        config_model: Optional[Type[BaseModel]] = None
        if inspect.isfunction(tool_obj):
            config_model = _get_config_model_from_function(tool_obj)
        else:
            config_model = _get_config_model_from_action(tool_obj)

        if config_model:
            raw_schema = _schema_from_pydantic_model(config_model, forbid_additional=forbid_additional)
            try:
                schema = simplify_schema(raw_schema, opts=simp_opts)
            except Exception:
                # Safety fallback: never fail the request because of one schema
                schema = {"type": "object", "properties": {}, "additionalProperties": True}
        else:
            schema = {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
                "maxProperties": 0,
            }
            logger.warning(
                f"[grok_tool_spec_factory] Tool '{name}' has no detectable Pydantic config model. "
                f"Falling back to empty schema."
            )

        out.append(
            CustomToolSpec(
                name=name,
                description=desc,
                parameters_schema=schema,
                handler=tool_obj,
            )
        )

    return out