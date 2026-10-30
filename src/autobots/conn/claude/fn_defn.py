import copy
import inspect
import json
import typing
from typing import Any, Dict, get_args, get_origin

from pydantic import BaseModel, TypeAdapter

JsonDict = Dict[str, Any]
class AnthropicFunctionDefinitionGen:
    _INTERNAL_PARAM_NAMES = {"self", "cls", "ctx", "app_state", "db", "notify", "user"}
    _DROP_SCHEMA_KEYS = {
        "$defs",
        "definitions",
        "$schema",
        "examples",
        "example",
        "readOnly",
        "writeOnly",
        "discriminator",
        "deprecated",
    }

    _ALLOWED_SCHEMA_KEYS = {
        "type",
        "title",
        "description",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "default",
        "format",
        "pattern",
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
        "uniqueItems",
        "anyOf",
        "oneOf",
        "allOf",
    }

    @staticmethod
    async def get_defination(func, description: str | None) -> Dict[str, Any]:
        # Backward-compatible alias
        return await AnthropicFunctionDefinitionGen.get_definition(func, description)

    @staticmethod
    async def get_definition(func, description: str | None) -> Dict[str, Any]:
        func_name = getattr(func, "__name__", func.__class__.__name__)
        input_schema = await AnthropicFunctionDefinitionGen._get_func_parameters(func)
        return {
            "name": func_name,
            "description": description or "",
            "input_schema": input_schema,
        }

    @staticmethod
    async def _get_func_parameters(func) -> Dict[str, Any]:
        signature = inspect.signature(func)

        for param_name, param in signature.parameters.items():
            if param_name in AnthropicFunctionDefinitionGen._INTERNAL_PARAM_NAMES:
                continue

            annotation = param.annotation
            if annotation is inspect._empty:
                continue

            raw_schema = AnthropicFunctionDefinitionGen._annotation_to_schema(annotation)
            if raw_schema is None:
                continue

            finalized = AnthropicFunctionDefinitionGen._finalize_input_schema(raw_schema)
            return finalized

        return {"type": "object", "properties": {}}

    @staticmethod
    def _annotation_to_schema(annotation: Any) -> JsonDict | None:
        annotation = AnthropicFunctionDefinitionGen._strip_annotated(annotation)

        # Direct BaseModel subclass
        if AnthropicFunctionDefinitionGen._is_basemodel_subclass(annotation):
            return AnthropicFunctionDefinitionGen._model_json_schema(annotation)

        # Fallback for unions, TypedDict, dataclass-like type hints, etc.
        try:
            return TypeAdapter(annotation).json_schema()
        except Exception:
            return None

    @staticmethod
    def _strip_annotated(annotation: Any) -> Any:
        origin = get_origin(annotation)
        if origin is typing.Annotated:
            args = get_args(annotation)
            if args:
                return AnthropicFunctionDefinitionGen._strip_annotated(args[0])
        return annotation

    @staticmethod
    def _is_basemodel_subclass(annotation: Any) -> bool:
        try:
            return inspect.isclass(annotation) and issubclass(annotation, BaseModel)
        except TypeError:
            return False

    @staticmethod
    def _model_json_schema(model_cls: type[BaseModel]) -> JsonDict:
        # Pydantic v2 preferred path
        if hasattr(model_cls, "model_json_schema"):
            return model_cls.model_json_schema(mode="validation")

        # Fallback for mixed environments
        if hasattr(model_cls, "schema"):
            return model_cls.schema()

        return {"type": "object", "properties": {}}

    @staticmethod
    def _finalize_input_schema(schema: JsonDict) -> JsonDict:
        working = copy.deepcopy(schema)

        defs: dict[str, Any] = {}
        if isinstance(working.get("$defs"), dict):
            defs.update(working.get("$defs", {}))
        if isinstance(working.get("definitions"), dict):
            defs.update(working.get("definitions", {}))

        working = AnthropicFunctionDefinitionGen._inline_refs(working, defs, ref_stack=())
        working.pop("$defs", None)
        working.pop("definitions", None)

        working = AnthropicFunctionDefinitionGen._collapse_schema_unions(working)
        working = AnthropicFunctionDefinitionGen._sanitize_schema(working)

        if not isinstance(working, dict) or working.get("type") != "object":
            working = {
                "type": "object",
                "properties": {
                    "value": working if isinstance(working, dict) else {"type": "string"}
                },
                "required": ["value"],
            }

        working.setdefault("properties", {})
        return working

    @staticmethod
    def _inline_refs(
        node: Any,
        defs: dict[str, Any],
        ref_stack: tuple[str, ...],
    ) -> Any:
        if isinstance(node, list):
            return [
                AnthropicFunctionDefinitionGen._inline_refs(item, defs, ref_stack)
                for item in node
            ]

        if not isinstance(node, dict):
            return node

        if "$ref" in node:
            ref_value = node["$ref"]
            ref_name = ref_value.split("/")[-1]

            # Cycle protection for recursive schemas
            if ref_name in ref_stack:
                fallback = {
                    "type": "object",
                    "description": f"Recursive reference to {ref_name}",
                }
                overlay = {k: v for k, v in node.items() if k != "$ref"}
                merged = AnthropicFunctionDefinitionGen._deep_merge_schema(
                    fallback,
                    overlay,
                )
                return AnthropicFunctionDefinitionGen._inline_refs(
                    merged,
                    defs,
                    ref_stack,
                )

            resolved = defs.get(ref_name)
            if not isinstance(resolved, dict):
                # Broken / unknown ref: keep non-ref data if present
                return {
                    k: AnthropicFunctionDefinitionGen._inline_refs(v, defs, ref_stack)
                    for k, v in node.items()
                    if k != "$ref"
                }

            merged = AnthropicFunctionDefinitionGen._deep_merge_schema(
                resolved,
                {k: v for k, v in node.items() if k != "$ref"},
            )
            return AnthropicFunctionDefinitionGen._inline_refs(
                merged,
                defs,
                ref_stack + (ref_name,),
            )

        out: dict[str, Any] = {}
        for key, value in node.items():
            out[key] = AnthropicFunctionDefinitionGen._inline_refs(value, defs, ref_stack)
        return out

    @staticmethod
    def _deep_merge_schema(base: JsonDict, overlay: JsonDict) -> JsonDict:
        result = copy.deepcopy(base)

        for key, value in overlay.items():
            if key not in result:
                result[key] = copy.deepcopy(value)
                continue

            base_value = result[key]

            if (
                key == "properties"
                and isinstance(base_value, dict)
                and isinstance(value, dict)
            ):
                merged_props = copy.deepcopy(base_value)
                for prop_name, prop_schema in value.items():
                    if (
                        prop_name in merged_props
                        and isinstance(merged_props[prop_name], dict)
                        and isinstance(prop_schema, dict)
                    ):
                        merged_props[prop_name] = AnthropicFunctionDefinitionGen._deep_merge_schema(
                            merged_props[prop_name],
                            prop_schema,
                        )
                    else:
                        merged_props[prop_name] = copy.deepcopy(prop_schema)
                result[key] = merged_props
                continue

            if key == "required" and isinstance(base_value, list) and isinstance(value, list):
                deduped = list(dict.fromkeys([*base_value, *value]))
                result[key] = deduped
                continue

            if isinstance(base_value, dict) and isinstance(value, dict):
                result[key] = AnthropicFunctionDefinitionGen._deep_merge_schema(
                    base_value,
                    value,
                )
                continue

            result[key] = copy.deepcopy(value)

        return result

    @staticmethod
    def _collapse_schema_unions(node: Any) -> Any:
        if isinstance(node, list):
            collapsed_list = [
                AnthropicFunctionDefinitionGen._collapse_schema_unions(item) for item in node
            ]
            return [item for item in collapsed_list if item is not None]

        if not isinstance(node, dict):
            return node

        out = {
            key: AnthropicFunctionDefinitionGen._collapse_schema_unions(value)
            for key, value in node.items()
        }

        for union_key in ("anyOf", "oneOf", "allOf"):
            if union_key not in out or not isinstance(out[union_key], list):
                continue

            variants = [v for v in out[union_key] if v is not None]

            if union_key in {"anyOf", "oneOf"}:
                # Optional[T] -> drop explicit null branch; non-required already models optionality.
                non_null_variants = [
                    v for v in variants
                    if not AnthropicFunctionDefinitionGen._is_null_schema(v)
                ]
                if non_null_variants:
                    variants = non_null_variants

                # If one branch is a catch-all object and another is a richer structured object,
                # keep the richer one. This is the key fix for Model | Dict[str, Any].
                stricter_variants = [
                    v for v in variants
                    if not AnthropicFunctionDefinitionGen._is_catch_all_object_schema(v)
                ]
                if stricter_variants:
                    variants = stricter_variants

            variants = AnthropicFunctionDefinitionGen._dedupe_schema_list(variants)

            if len(variants) == 1:
                overlay = {k: v for k, v in out.items() if k != union_key}
                return AnthropicFunctionDefinitionGen._deep_merge_schema(
                    variants[0],
                    overlay,
                )

            out[union_key] = variants

        return out

    @staticmethod
    def _dedupe_schema_list(items: list[Any]) -> list[Any]:
        seen: set[str] = set()
        deduped: list[Any] = []

        for item in items:
            try:
                key = json.dumps(item, sort_keys=True, default=str)
            except Exception:
                key = repr(item)

            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)

        return deduped

    @staticmethod
    def _is_null_schema(schema: Any) -> bool:
        return isinstance(schema, dict) and schema.get("type") == "null"

    @staticmethod
    def _is_catch_all_object_schema(schema: Any) -> bool:
        if not isinstance(schema, dict):
            return False
        if schema.get("type") != "object":
            return False
        if schema.get("properties"):
            return False
        if schema.get("required"):
            return False

        additional = schema.get("additionalProperties", True)
        return additional is True or additional == {}

    @staticmethod
    def _sanitize_schema(node: Any, parent_key: str | None = None) -> Any:
        if isinstance(node, list):
            return [
                AnthropicFunctionDefinitionGen._sanitize_schema(item, parent_key=parent_key)
                for item in node
                if item is not None
            ]

        if not isinstance(node, dict):
            return node

        cleaned: dict[str, Any] = {}

        for key, value in node.items():
            if value is None:
                continue
            if key in AnthropicFunctionDefinitionGen._DROP_SCHEMA_KEYS:
                continue

            if parent_key == "properties":
                cleaned[key] = AnthropicFunctionDefinitionGen._sanitize_schema(value)
                continue

            if key not in AnthropicFunctionDefinitionGen._ALLOWED_SCHEMA_KEYS:
                continue

            if key == "properties" and isinstance(value, dict):
                props = AnthropicFunctionDefinitionGen._sanitize_schema(
                    value,
                    parent_key="properties",
                )
                if props:
                    cleaned[key] = props
                continue

            if key in {"anyOf", "oneOf", "allOf"} and isinstance(value, list):
                variants = [
                    AnthropicFunctionDefinitionGen._sanitize_schema(item)
                    for item in value
                    if item is not None
                ]
                variants = [v for v in variants if v not in ({}, [], None)]
                if variants:
                    cleaned[key] = variants
                continue

            if key in {"items", "additionalProperties"} and isinstance(value, dict):
                cleaned[key] = AnthropicFunctionDefinitionGen._sanitize_schema(value)
                continue

            cleaned[key] = AnthropicFunctionDefinitionGen._sanitize_schema(value)

        return cleaned