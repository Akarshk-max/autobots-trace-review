import json
from typing import Any, Dict

from pydantic import BaseModel

from src.autobots.conn.grok.grok_chat.grok_models import CustomToolSpec


# this is just for initializing model config
def pydantic_schema(model: type[BaseModel]) -> Dict[str, Any]:
    schema = model.model_json_schema()
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


# it is always needed for normalizing tool args for grok server side tools calling and
# also custom side tool calling using ToolFactory.run_tool
def normalize_tool_args(args: Any) -> Dict[str, Any]:
    if args is None:
        return {}
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        s = args.strip()
        if not s:
            return {}
        try:
            val = json.loads(s)
            return val if isinstance(val, dict) else {"__raw": val}
        except Exception:
            try:
                start = s.find("{")
                end = s.rfind("}")
                if start != -1 and end != -1 and end > start:
                    maybe = s[start : end + 1]
                    val2 = json.loads(maybe)
                    if isinstance(val2, dict):
                        return val2
            except Exception:
                pass
            return {"__raw": s}
    return {"__raw": args}


# building up the model which will be given to llm
# so that model can decide which tools which will be selected and also
# how it will be called once selected
def make_custom_tool(
    *,
    name: str,
    description: str,
    parameters_schema: Dict[str, Any],
    handler: Any,
) -> CustomToolSpec:
    return CustomToolSpec(
        name=name,
        description=description or "",
        parameters_schema=parameters_schema or {"type": "object", "properties": {}},
        handler=handler,
    )
