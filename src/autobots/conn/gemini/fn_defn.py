import copy
import inspect
import typing
from typing import Any, get_args, get_origin

from google.genai import types
from google.genai.types import JSONSchema, Schema
from pydantic import BaseModel, TypeAdapter

from src.autobots.conn.gemini.inline_json_schema import (
    ensure_vertex_compatible_types,
    fix_const_fields,
    inline_jsonschema_refs,
    strip_unsupported_keys,
)


class GeminiFunctionDefinitionGen:
    _INTERNAL_PARAM_NAMES = {"self", "cls", "ctx", "app_state", "db", "notify", "user"}
    _DEFAULT_EMPTY_SCHEMA = {"type": "object", "properties": {}}
    _PRIMARY_DROP_KEYS = {"examples", "control_type", "placeholder"}
    _FALLBACK_DROP_KEYS = {
        "examples",
        "control_type",
        "placeholder",
        "title",
        "default",
        "format",
    }

    @staticmethod
    async def get_defination(func, description: str | None) -> types.FunctionDeclaration:
        return await GeminiFunctionDefinitionGen.get_definition(func, description)

    @staticmethod
    async def get_definition(func, description: str | None) -> types.FunctionDeclaration:
        func_name = getattr(func, "__name__", func.__class__.__name__)
        parameters = await GeminiFunctionDefinitionGen._get_func_parameters(func)
        return types.FunctionDeclaration(
            description=description or "",
            name=func_name,
            parameters=parameters,
        )

    @staticmethod
    async def _get_func_parameters(func) -> Schema | None:
        signature = inspect.signature(func)
        for param_name, param in signature.parameters.items():
            if param_name in GeminiFunctionDefinitionGen._INTERNAL_PARAM_NAMES:
                continue

            annotation = param.annotation
            if annotation is inspect._empty:
                continue

            raw_schema = GeminiFunctionDefinitionGen._annotation_to_schema(annotation)
            if raw_schema is None:
                continue

            return GeminiFunctionDefinitionGen._finalize_input_schema(raw_schema)

        return GeminiFunctionDefinitionGen._schema_from_json(
            GeminiFunctionDefinitionGen._DEFAULT_EMPTY_SCHEMA,
            fallback=False,
        )

    @staticmethod
    def _annotation_to_schema(annotation: Any) -> dict[str, Any] | None:
        annotation = GeminiFunctionDefinitionGen._strip_annotated(annotation)

        if GeminiFunctionDefinitionGen._is_basemodel_subclass(annotation):
            return GeminiFunctionDefinitionGen._model_json_schema(annotation)

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
                return GeminiFunctionDefinitionGen._strip_annotated(args[0])
        return annotation

    @staticmethod
    def _is_basemodel_subclass(annotation: Any) -> bool:
        try:
            return inspect.isclass(annotation) and issubclass(annotation, BaseModel)
        except TypeError:
            return False

    @staticmethod
    def _model_json_schema(model_cls: type[BaseModel]) -> dict[str, Any]:
        if hasattr(model_cls, "model_json_schema"):
            return model_cls.model_json_schema(mode="validation")
        if hasattr(model_cls, "schema"):
            return model_cls.schema()
        return copy.deepcopy(GeminiFunctionDefinitionGen._DEFAULT_EMPTY_SCHEMA)

    @staticmethod
    def _normalize_root_schema(schema_dict: dict[str, Any]) -> dict[str, Any]:
        working = copy.deepcopy(schema_dict)
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
    def _schema_from_json(schema_dict: dict[str, Any], *, fallback: bool) -> Schema:
        json_schema = JSONSchema.model_validate(schema_dict)
        return types.Schema.from_json_schema(
            json_schema=json_schema,
            api_option="GEMINI_API",
            raise_error_on_unsupported_field=not fallback,
        )

    @staticmethod
    def _finalize_input_schema(schema_dict: dict[str, Any]) -> Schema:
        working = inline_jsonschema_refs(schema_dict)
        working = fix_const_fields(working)
        working = strip_unsupported_keys(
            working,
            keys_to_remove=GeminiFunctionDefinitionGen._PRIMARY_DROP_KEYS,
        )
        working = ensure_vertex_compatible_types(working)
        working = GeminiFunctionDefinitionGen._normalize_root_schema(working)

        try:
            return GeminiFunctionDefinitionGen._schema_from_json(working, fallback=False)
        except Exception:
            fallback_schema = strip_unsupported_keys(
                working,
                keys_to_remove=GeminiFunctionDefinitionGen._FALLBACK_DROP_KEYS,
            )
            fallback_schema = GeminiFunctionDefinitionGen._normalize_root_schema(fallback_schema)
            return GeminiFunctionDefinitionGen._schema_from_json(
                fallback_schema,
                fallback=True,
            )
