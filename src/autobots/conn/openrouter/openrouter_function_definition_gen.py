from typing import Any, Dict, List

from loguru import logger
from openai.types import beta
from openai.types.chat import ChatCompletionToolParam
from openai.types.responses import FunctionToolParam
from openai.types.shared_params import FunctionDefinition, FunctionParameters
from pydantic import BaseModel

from src.autobots.core.logging.app_code import AppCode


class OpenRouterFunctionDefinitionGen:

    @staticmethod
    async def get_responses_function_tool_param(func, description: str) -> FunctionToolParam:
        func_name = func.__name__
        parameters_obj = await OpenRouterFunctionDefinitionGen._get_func_parameters(func)
        function_schema = FunctionToolParam(
            type="function",
            name=func_name,
            parameters=parameters_obj,
            strict=False,
            description=description,
        )
        # logger.bind(func_name=func_name, func_definition=function_schema).debug(
        #     f"Function definition generated (OpenRouter responses): {func_name}"
        # )
        return function_schema

    @staticmethod
    async def get_function_tool_param(func, description: str) -> beta.FunctionToolParam:
        func_name = func.__name__
        parameters_obj = await OpenRouterFunctionDefinitionGen._get_func_parameters(func)
        function_obj = FunctionDefinition(
            name=func_name, description=description, parameters=parameters_obj
        )
        function_schema = beta.FunctionToolParam(type="function", function=function_obj)
        # logger.bind(func_name=func_name, func_definition=function_schema).debug(
        #     f"Function definition generated (OpenRouter beta): {func_name}"
        # )
        return function_schema

    @staticmethod
    async def get_chat_completion_tool_param(func, description: str) -> ChatCompletionToolParam:
        func_name = func.__name__
        parameters_obj = await OpenRouterFunctionDefinitionGen._get_func_parameters(func)
        function_obj = FunctionDefinition(
            name=func_name, description=description, parameters=parameters_obj
        )
        function_schema = ChatCompletionToolParam(type="function", function=function_obj)
        # logger.bind(func_name=func_name, func_definition=function_schema).debug(
        #     f"Function definition generated (OpenRouter chat): {func_name}"
        # )
        return function_schema

    @staticmethod
    async def get_chat_completion_tool_param_no_refs(
        func, description: str
    ) -> ChatCompletionToolParam:
        func_name = func.__name__
        parameters_obj = await OpenRouterFunctionDefinitionGen._get_func_parameters_no_refs(func)
        function_obj = FunctionDefinition(
            name=func_name, description=description, parameters=parameters_obj
        )
        function_schema = ChatCompletionToolParam(type="function", function=function_obj)
        # logger.bind(func_name=func_name, func_definition=function_schema).debug(
        #     f"Function definition generated (OpenRouter chat; no refs): {func_name}"
        # )
        return function_schema

    @staticmethod
    async def _get_func_parameters(func) -> FunctionParameters:
        func_parameters: FunctionParameters = {}
        func_parameter_names = await OpenRouterFunctionDefinitionGen._get_func_parameter_names(func)

        for func_parameter_name in func_parameter_names:
            if issubclass(func.__annotations__[func_parameter_name], BaseModel):
                model_json_schema = func.__annotations__[func_parameter_name].model_json_schema()
                func_param = await OpenRouterFunctionDefinitionGen._get_func_parameter(
                    model_json_schema
                )
                # OpenAI-style tools allow only a single object parameter; we keep the same rule.
                func_parameters = func_param
                logger.bind(code=AppCode.TOOL_FACTORY_OPENROUTER).warning(
                    "Breaking after 1st param of function while building function definition. "
                    "Currently the OpenAI-compatible tool format expects a single object argument."
                )
                break
        return func_parameters

    @staticmethod
    async def _get_func_parameters_no_refs(func) -> FunctionParameters:
        func_parameters: FunctionParameters = {}
        func_parameter_names = await OpenRouterFunctionDefinitionGen._get_func_parameter_names(func)

        for func_parameter_name in func_parameter_names:
            if issubclass(func.__annotations__[func_parameter_name], BaseModel):
                model_json_schema = func.__annotations__[func_parameter_name].model_json_schema()
                expanded_schema = OpenRouterFunctionDefinitionGen._expand_schema_no_refs(model_json_schema)
                func_param = await OpenRouterFunctionDefinitionGen._get_func_parameter_no_refs(expanded_schema)
                # OpenAI-style tools allow only a single object parameter; we keep the same rule.
                func_parameters = func_param
                logger.bind(code=AppCode.TOOL_FACTORY_OPENROUTER).warning(
                    "Breaking after 1st param of function while building function definition. "
                    "Currently the OpenAI-compatible tool format expects a single object argument."
                )
                break
        return func_parameters


    @staticmethod
    async def _get_func_parameter(model_json_schema: Dict[str, Any]) -> FunctionParameters:
        param_type = model_json_schema.get("type")
        properties = await OpenRouterFunctionDefinitionGen._get_properties(model_json_schema)
        required = model_json_schema.get("required") if "required" in model_json_schema else None

        parameters_dict: FunctionParameters = {}
        if param_type:
            parameters_dict["type"] = param_type
        if properties:
            parameters_dict["properties"] = properties
        if required:
            parameters_dict["required"] = required
        else:
            parameters_dict["required"] = []

        parameters_dict["additionalProperties"] = False
        return parameters_dict

    @staticmethod
    async def _get_func_parameter_no_refs(model_json_schema: Dict[str, Any]) -> FunctionParameters:
        param_type = model_json_schema.get("type")
        properties = model_json_schema.get("properties") or {}
        required = model_json_schema.get("required") if "required" in model_json_schema else None

        parameters_dict: FunctionParameters = {}
        if param_type:
            parameters_dict["type"] = param_type
        if properties and isinstance(properties, dict):
            parameters_dict["properties"] = properties
        if required and isinstance(required, list):
            parameters_dict["required"] = required
        else:
            parameters_dict["required"] = []

        parameters_dict["additionalProperties"] = False
        return parameters_dict

    @staticmethod
    def _expand_schema_no_refs(model_json_schema: Dict[str, Any]) -> Dict[str, Any]:
        defs = model_json_schema.get("$defs") if isinstance(model_json_schema, dict) else None
        defs = defs if isinstance(defs, dict) else {}

        def _walk(node: Any, depth: int) -> Any:
            if depth > 80:
                return node

            if isinstance(node, dict):
                current: dict[str, Any] = dict(node)
                for _ in range(50):
                    if "$ref" in current:
                        ref_value = str(current.get("$ref") or "").split("/")[-1]
                        target = defs.get(ref_value)
                        wrapper_meta = {k: v for k, v in current.items() if k != "$ref"}
                        if isinstance(target, dict):
                            current = {**target, **wrapper_meta}
                            continue
                        current = dict(wrapper_meta)
                        continue

                    if "anyOf" in current:
                        current = OpenRouterFunctionDefinitionGen._flatten_optional_union(current)
                        continue

                    if "allOf" in current:
                        current = OpenRouterFunctionDefinitionGen._flatten_allof(current)
                        continue

                    break

                out: dict[str, Any] = {}
                for k, v in current.items():
                    if k == "$defs":
                        continue
                    out[k] = _walk(v, depth + 1)
                return out

            if isinstance(node, list):
                return [_walk(x, depth + 1) for x in node]

            return node

        expanded = _walk(model_json_schema, 0)
        if isinstance(expanded, dict):
            expanded.pop("$defs", None)
        return expanded


    @staticmethod
    async def _get_func_parameter_names(func) -> List[str]:
        params: List[str] = []
        annotations: Dict[str, Any] = func.__annotations__
        for key in annotations.keys():
            if key == "return":
                continue
            params.append(key)
        return params

    @staticmethod
    async def _get_properties(model_json_schema: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
        properties: Dict[str, Dict[str, Any]] = {}
        properties_initial_state = model_json_schema.get("properties", {})

        references: Dict[str, Any] = {}
        if "$defs" in model_json_schema:
            references = model_json_schema["$defs"]

        for property_name in properties_initial_state:
            property_value = properties_initial_state[property_name]
            de_referenced_property = await OpenRouterFunctionDefinitionGen._get_de_referenced_property(
                references, property_value, property_name
            )
            properties[property_name] = de_referenced_property

        return properties

    @staticmethod
    def _flatten_optional_union(schema: Dict[str, Any]) -> Dict[str, Any]:
        """Flatten common Optional[T] shapes into a single non-null schema.

        Pydantic typically emits Optional fields as:
          {"anyOf": [<T schema>, {"type":"null"}], "default": null, ...}

        Many tool-calling models do better when they see a simple schema like
        {"type": "string"} with the field not being in `required`.
        """
        if "anyOf" not in schema:
            return schema

        options = schema.get("anyOf")
        if not isinstance(options, list) or not options:
            return {k: v for k, v in schema.items() if k != "anyOf"}

        # Choose first option that is not explicitly null.
        chosen = None
        for opt in options:
            if not isinstance(opt, dict):
                continue
            if opt.get("type") == "null":
                continue
            chosen = opt
            break

        # If everything is null, just keep a null type (should be rare).
        if chosen is None:
            chosen = {"type": "null"}

        base = {k: v for k, v in schema.items() if k != "anyOf"}
        merged = {**base, **chosen}

        # Avoid biasing some providers/models toward emitting `null` for Optional fields.
        if merged.get("default") is None:
            merged.pop("default", None)

        return merged

    @staticmethod
    def _flatten_allof(schema: Dict[str, Any]) -> Dict[str, Any]:
        if "allOf" not in schema:
            return schema

        options = schema.get("allOf")
        if not isinstance(options, list) or not options:
            return {k: v for k, v in schema.items() if k != "allOf"}

        # Pydantic often uses allOf for $ref wrappers; we merge the wrapper metadata
        # with the first non-null option.
        chosen = None
        for opt in options:
            if not isinstance(opt, dict):
                continue
            if opt.get("type") == "null":
                continue
            chosen = opt
            break

        if chosen is None:
            chosen = options[0] if isinstance(options[0], dict) else {"type": "object"}

        base = {k: v for k, v in schema.items() if k != "allOf"}
        merged = {**base, **chosen}

        if merged.get("default") is None:
            merged.pop("default", None)

        return merged
    @staticmethod
    async def _get_de_referenced_property(
        references: Dict[str, Any], property_value: Dict[str, Any], property_name: str
    ) -> Dict[str, Any]:
        expanded_property: Dict[str, Any] = property_value

        # Iterate until we remove wrappers and resolve $ref.
        for _ in range(50):
            if not isinstance(expanded_property, dict):
                return {"type": "string"}

            if "anyOf" in expanded_property:
                expanded_property = OpenRouterFunctionDefinitionGen._flatten_optional_union(
                    expanded_property
                )
                continue

            if "allOf" in expanded_property:
                expanded_property = OpenRouterFunctionDefinitionGen._flatten_allof(expanded_property)
                continue

            if "$ref" not in expanded_property:
                return expanded_property

            # Resolve $ref while preserving wrapper metadata (title/description/default/constraints).
            ref_value = str(expanded_property["$ref"]).split("/")[-1]
            ref_schema = references.get(ref_value)
            if not isinstance(ref_schema, dict):
                # Defensive: if we can't resolve, drop $ref and return what we have.
                out = dict(expanded_property)
                out.pop("$ref", None)
                return out

            wrapper_meta = dict(expanded_property)
            wrapper_meta.pop("$ref", None)
            expanded_property = {**ref_schema, **wrapper_meta}

        logger.bind(code=AppCode.TOOL_FACTORY_OPENROUTER, property_name=property_name).warning(
            "Exceeded max iterations while dereferencing property schema; returning last expansion"
        )
        return expanded_property