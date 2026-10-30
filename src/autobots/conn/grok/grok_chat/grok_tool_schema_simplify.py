from typing import Any, Dict, List, Optional, Set, Tuple, Union

from pydantic import BaseModel, ConfigDict, model_validator

JSON = Union[Dict[str, Any], List[Any], str, int, float, bool, None]

class SimplifyOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Hard safety brakes
    max_depth: int = 10
    max_nodes: int = 800
    max_properties_per_object: int = 60
    max_required: int = 40
    max_enum: int = 100
    max_union_branches: int = 2  # for anyOf/oneOf/allOf

    prefer_objects_over_arrays: bool = True
    prefer_non_null_branch: bool = True

    keep_titles: bool = False
    keep_descriptions: bool = False
    keep_examples: bool = False
    keep_defaults: bool = False

    top_level_additional_properties: bool = False
    nested_additional_properties: bool = True

    degrade_to_type: str = "string" 
    resolve_local_refs: bool = True
    prefer_string_fallback: bool = True

    @model_validator(mode="after")
    def _validate_degrade(self) -> "SimplifyOptions":
        if self.degrade_to_type not in ("string", "object"):
            raise ValueError("degrade_to_type must be 'string' or 'object'")
        if self.max_depth < 1:
            raise ValueError("max_depth must be >= 1")
        if self.max_nodes < 50:
            raise ValueError("max_nodes must be >= 50")
        if self.max_union_branches < 1:
            raise ValueError("max_union_branches must be >= 1")
        return self


class _Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_nodes: int
    nodes: int = 0

    def bump(self, n: int = 1) -> None:
        self.nodes += int(n)
        if self.nodes > int(self.max_nodes):
            raise RuntimeError(f"schema node budget exceeded: {self.nodes}/{self.max_nodes}")


def simplify_schema(schema: Any, *, opts: Optional[SimplifyOptions] = None) -> Dict[str, Any]:
    opts = opts or SimplifyOptions()

    if not isinstance(schema, dict):
        return _force_object_root(_degrade(opts), opts)

    defs = _collect_defs(schema) if opts.resolve_local_refs else {}
    budget = _Budget(max_nodes=int(opts.max_nodes))
    seen_refs: Set[str] = set()

    try:
        out = _simplify_node(
            schema,
            defs=defs,
            opts=opts,
            budget=budget,
            depth=0,
            is_top=True,
            seen_refs=seen_refs,
        )
    except Exception:
        out = _degrade(opts)

    return _force_object_root(out, opts)


def _force_object_root(node: Dict[str, Any], opts: SimplifyOptions) -> Dict[str, Any]:
    if not isinstance(node, dict):
        node = _degrade(opts)

    t = node.get("type")
    if t != "object":
        return {
            "type": "object",
            "properties": {"payload": node},
            "required": ["payload"],
            "additionalProperties": bool(opts.top_level_additional_properties),
        }

    node.setdefault("properties", {})
    node["additionalProperties"] = bool(opts.top_level_additional_properties)
    return node


def _degrade(opts: SimplifyOptions) -> Dict[str, Any]:
    if opts.degrade_to_type == "object":
        return {"type": "object", "properties": {}, "additionalProperties": True}
    return {"type": "string"}


def _collect_defs(schema: Dict[str, Any]) -> Dict[str, Any]:
    defs: Dict[str, Any] = {}
    if isinstance(schema.get("$defs"), dict):
        defs.update(schema["$defs"])
    if isinstance(schema.get("definitions"), dict):
        defs.update(schema["definitions"])
    return defs


def _simplify_node(
    node: Any,
    *,
    defs: Dict[str, Any],
    opts: SimplifyOptions,
    budget: _Budget,
    depth: int,
    is_top: bool,
    seen_refs: Set[str],
) -> Dict[str, Any]:
    budget.bump()

    if depth >= int(opts.max_depth):
        return _degrade(opts)

    if not isinstance(node, dict):
        return _degrade(opts)

    # local ref resolution
    if opts.resolve_local_refs and "$ref" in node:
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            if ref in seen_refs:
                return _degrade(opts)  # cycle
            target = _resolve_local_ref(ref, defs)
            if target is None:
                return _degrade(opts)
            seen_refs.add(ref)
            try:
                return _simplify_node(
                    target,
                    defs=defs,
                    opts=opts,
                    budget=budget,
                    depth=depth + 1,
                    is_top=is_top,
                    seen_refs=seen_refs,
                )
            finally:
                seen_refs.remove(ref)

    # collapse unions early
    for uk in ("anyOf", "oneOf", "allOf"):
        if uk in node and isinstance(node[uk], list) and node[uk]:
            return _simplify_union(
                node[uk],
                defs=defs,
                opts=opts,
                budget=budget,
                depth=depth,
                is_top=is_top,
                seen_refs=seen_refs,
            )

    #  enum handling (trim) 
    if "enum" in node and isinstance(node["enum"], list):
        enum_vals = node["enum"][: int(opts.max_enum)]
        out: Dict[str, Any] = {"type": _infer_enum_type(enum_vals) or "string"}
        # keep enum only if small (too large enum inflates grammar a lot)
        if len(node["enum"]) <= min(int(opts.max_enum), 30):
            out["enum"] = enum_vals
        return out

    # Infer type if missing
    t = node.get("type")
    if not t and "properties" in node:
        t = "object"
    if isinstance(t, list):
        t = _pick_type_from_list(t, opts)

    # Primitive
    if t in ("string", "number", "integer", "boolean", "null"):
        return {"type": t}

    # Array
    if t == "array":
        items = node.get("items", {})
        return {
            "type": "array",
            "items": _simplify_node(
                items,
                defs=defs,
                opts=opts,
                budget=budget,
                depth=depth + 1,
                is_top=False,
                seen_refs=seen_refs,
            ),
        }

    # Object 
    if t == "object" or "properties" in node:
        props = node.get("properties") if isinstance(node.get("properties"), dict) else {}
        required = node.get("required") if isinstance(node.get("required"), list) else []

        prop_items = [(k, v) for k, v in props.items() if isinstance(k, str)]
        if len(prop_items) > int(opts.max_properties_per_object):
            prop_items = prop_items[: int(opts.max_properties_per_object)]

        out_props: Dict[str, Any] = {}
        for k, v in prop_items:
            out_props[k] = _simplify_node(
                v,
                defs=defs,
                opts=opts,
                budget=budget,
                depth=depth + 1,
                is_top=False,
                seen_refs=seen_refs,
            )

        out: Dict[str, Any] = {"type": "object", "properties": out_props}

        req = [x for x in required if isinstance(x, str) and x in out_props]
        if len(req) > int(opts.max_required):
            req = req[: int(opts.max_required)]
        if req:
            out["required"] = req

        # additionalProperties strategy
        out["additionalProperties"] = (
            bool(opts.top_level_additional_properties) if is_top else bool(opts.nested_additional_properties)
        )

        # Optional metadata (usually OFF)
        if opts.keep_titles and isinstance(node.get("title"), str):
            out["title"] = node["title"]
        if opts.keep_descriptions and isinstance(node.get("description"), str):
            out["description"] = node["description"]
        if opts.keep_examples and "examples" in node:
            out["examples"] = node["examples"]
        if opts.keep_defaults and "default" in node:
            out["default"] = node["default"]

        return out

    return _degrade(opts)


def _simplify_union(
    branches: List[Any],
    *,
    defs: Dict[str, Any],
    opts: SimplifyOptions,
    budget: _Budget,
    depth: int,
    is_top: bool,
    seen_refs: Set[str],
) -> Dict[str, Any]:
    # Score , sort branches
    scored: List[Tuple[int, Any]] = [(_score_branch(b, opts), b) for b in branches]
    scored.sort(key=lambda x: x[0], reverse=True)

    keep_n = max(1, int(opts.max_union_branches))
    keep = [b for _, b in scored[:keep_n]]

    best = keep[0]
    return _simplify_node(
        best,
        defs=defs,
        opts=opts,
        budget=budget,
        depth=depth + 1,
        is_top=is_top,
        seen_refs=seen_refs,
    )


def _score_branch(branch: Any, opts: SimplifyOptions) -> int:
    """
    Higher score = more preferred branch when collapsing unions.

    Heuristics:
    - Prefer non-null
    - Prefer object (or array) depending on opts
    - Prefer branches with properties/items (more structured)
    """
    if branch is None:
        return -100

    if isinstance(branch, dict):
        t = branch.get("type")

        # list types like ["object","null"]
        if isinstance(t, list):
            types = [x for x in t if isinstance(x, str)]
            if opts.prefer_non_null_branch and "null" in types and len(types) > 1:
                types = [x for x in types if x != "null"]
            picked = _pick_type_from_list(types, opts)
            base = _type_score(picked, opts)
            if "null" in t:
                base -= 10
            return base

        # explicit null
        if t == "null":
            return -50

        # object-like
        if t == "object" or "properties" in branch:
            bonus = 5 if isinstance(branch.get("properties"), dict) and branch["properties"] else 0
            return _type_score("object", opts) + bonus

        # array-like
        if t == "array" or "items" in branch:
            bonus = 3 if "items" in branch else 0
            return _type_score("array", opts) + bonus

        # primitives
        if t in ("string", "number", "integer", "boolean"):
            return _type_score(t, opts)

        # refs: mild preference
        if "$ref" in branch:
            return 10

    return 1


def _type_score(t: Optional[str], opts: SimplifyOptions) -> int:
    if t == "object":
        return 100 if opts.prefer_objects_over_arrays else 70
    if t == "array":
        return 90 if not opts.prefer_objects_over_arrays else 60
    if t == "string":
        return 50
    if t in ("integer", "number"):
        return 40
    if t == "boolean":
        return 30
    if t == "null":
        return -50
    return 10


def _pick_type_from_list(types: List[Any], opts: SimplifyOptions) -> str:
    ts = [t for t in types if isinstance(t, str)]
    if not ts:
        return "string"

    # Prefer non-null
    if opts.prefer_non_null_branch and "null" in ts and len(ts) > 1:
        ts = [t for t in ts if t != "null"]

    # Order preference
    if opts.prefer_objects_over_arrays:
        order = ("object", "array", "string", "integer", "number", "boolean", "null")
    else:
        order = ("array", "object", "string", "integer", "number", "boolean", "null")

    for t in order:
        if t in ts:
            return t
    return ts[0]


def _resolve_local_ref(ref: str, defs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Only supports:
      - "#/$defs/Name"
      - "#/definitions/Name"
    """
    parts = ref.lstrip("#/").split("/")
    if len(parts) >= 2 and parts[0] in ("$defs", "definitions"):
        key = parts[1]
        target = defs.get(key)
        if isinstance(target, dict):
            return target
    return None


def _infer_enum_type(vals: List[Any]) -> Optional[str]:
    if not vals:
        return None
    kinds = {type(v) for v in vals}
    if len(kinds) != 1:
        return None
    t = next(iter(kinds))
    if t is str:
        return "string"
    if t is bool:
        return "boolean"
    if t is int:
        return "integer"
    if t is float:
        return "number"
    return None