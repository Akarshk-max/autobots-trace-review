import json
from copy import deepcopy
from typing import Any, Dict, List, Union

JSON = Union[Dict[str, Any], List[Any], str, int, float, bool, None]


def inline_jsonschema_refs(schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Inline all $ref occurrences that point to '#/$defs/...'.
    Now handles `$ref` inside `anyOf` (flatten, dedupe, and collapse when size==1).
    """
    src = deepcopy(schema)
    defs: Dict[str, Any] = src.get("$defs", {}) or {}
    cache: Dict[str, Any] = {}  # memoized resolved defs

    def resolve_ref_path(ref: str) -> Any:
        if not ref.startswith("#/$defs/"):
            raise ValueError(f"Only '#/$defs/...' refs are supported, got: {ref}")
        name = ref.split("/")[-1]
        if name in cache:
            return deepcopy(cache[name])
        if name not in defs:
            raise KeyError(f"Missing definition for {ref}")
        resolved = walk(defs[name])  # resolve nested refs inside the def
        cache[name] = resolved
        return deepcopy(resolved)

    def merge_dicts(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(a)
        out.update(b)
        return out

    def _dedupe_list(items: List[JSON]) -> List[JSON]:
        """Deduplicate JSON-able items by structural value."""
        seen = set()
        result = []
        for it in items:
            try:
                key = json.dumps(it, sort_keys=True)
            except TypeError:
                # Fallback: keep as-is if not JSON-serializable
                result.append(it)
                continue
            if key not in seen:
                seen.add(key)
                result.append(it)
        return result

    def _normalize_anyof(node: Dict[str, Any]) -> JSON:
        """Flatten, dedupe, and collapse `anyOf` if present."""
        if "anyOf" not in node:
            return node
        items = node["anyOf"]

        # 1) Walk each item (so refs inside items are resolved)
        walked_items = [walk(it) for it in items]

        # 2) Flatten nested anyOf items like {"anyOf": [...]}
        flat: List[JSON] = []
        for it in walked_items:
            if (
                isinstance(it, dict)
                and set(it.keys()) == {"anyOf"}
                and isinstance(it["anyOf"], list)
            ):
                flat.extend(it["anyOf"])
            else:
                flat.append(it)

        # 3) Dedupe
        flat = _dedupe_list(flat)

        # 4) Collapse if single
        if len(flat) == 1:
            # If the node only had anyOf, replace the node with the single item
            if set(node.keys()) == {"anyOf"}:
                return flat[0]
            # else keep the node and store simplified anyOf
            node = dict(node)
            node["anyOf"] = flat
            return node

        # else keep simplified anyOf
        node = dict(node)
        node["anyOf"] = flat
        return node

    def walk(node: JSON) -> JSON:
        if isinstance(node, dict):
            # If the dict is *just* a $ref, replace it entirely.
            if set(node.keys()) == {"$ref"}:
                return resolve_ref_path(node["$ref"])

            # If there's a $ref *plus* other keys, inline the ref then merge the extras.
            if "$ref" in node:
                ref_val = node.pop("$ref")
                resolved = resolve_ref_path(ref_val)
                extras = {k: walk(v) for k, v in node.items()}
                if isinstance(resolved, dict):
                    merged = merge_dicts(resolved, extras)
                    # Handle anyOf created/retained after merge
                    return _normalize_anyof(merged) if "anyOf" in merged else merged
                return resolved  # Non-dict resolved (rare)

            # Regular dict: walk children first
            walked = {k: walk(v) for k, v in node.items()}

            # Normalize anyOf if present
            return _normalize_anyof(walked) if "anyOf" in walked else walked

        if isinstance(node, list):
            return [walk(x) for x in node]

        # Primitive
        return node

    # 1) Pre-resolve every def (fills cache with fully inlined versions)
    for k in list(defs.keys()):
        cache[k] = walk(defs[k])

    # 2) Resolve refs across the whole schema
    result = walk(src)

    # 3) Remove $defs from the final output
    if isinstance(result, dict):
        result.pop("$defs", None)

    return result


def fix_const_fields(schema_dict: dict) -> dict:
    """Convert 'const' fields to 'enum' with a single value for Gemini compatibility."""
    from copy import deepcopy

    schema = deepcopy(schema_dict)

    def walk(node):
        if isinstance(node, dict):
            if "const" in node:
                node["enum"] = [node.pop("const")]
            for k, v in list(node.items()):
                node[k] = walk(v)
        elif isinstance(node, list):
            node = [walk(i) for i in node]
        return node

    return walk(schema)


def strip_unsupported_keys(
    schema_dict: Dict[str, Any],
    keys_to_remove={"examples"},  # add more if needed
) -> Dict[str, Any]:
    """Recursively drop unsupported JSON-Schema keys for Gemini."""
    s = deepcopy(schema_dict)

    def walk(node: JSON) -> JSON:
        if isinstance(node, dict):
            # remove unsupported keys at this level
            for k in list(node.keys()):
                if k in keys_to_remove:
                    node.pop(k, None)
            # recurse
            for k, v in list(node.items()):
                node[k] = walk(v)
            return node
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(s)


def ensure_vertex_compatible_types(schema_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Collapse anyOf/nullable patterns so every schema node has an explicit 'type'.

    Vertex AI's function-calling validator requires an explicit ``type`` field on
    every schema node and does **not** support ``anyOf`` / union types.  The Gemini
    Developer API accepts them silently.

    Transformations applied recursively (bottom-up):

    1. ``anyOf: [T, {"type": "null"}]``        → T  (nullable → just the base type)
    2. ``anyOf: [T1, T2, ..., {"type":"null"}]`` → first concrete non-null entry
    3. ``{}``                                   → ``{"type": "object"}``
    4. ``{properties: …}`` without ``type``     → add ``"type": "object"``
    """
    s = deepcopy(schema_dict)

    def _is_null_schema(node: Any) -> bool:
        return isinstance(node, dict) and node.get("type") == "null"

    def _is_empty_schema(node: Any) -> bool:
        return node == {} or (isinstance(node, dict) and not node)

    def _collapse_anyof(node: Dict[str, Any]) -> Dict[str, Any]:
        """Replace anyOf with the primary non-null, non-empty concrete type."""
        items: List[Any] = node.get("anyOf", [])
        if not items:
            return node

        non_null = [it for it in items if not _is_null_schema(it)]

        if not non_null:
            # All null — shouldn't happen, but be safe
            node.pop("anyOf")
            return {**node, "type": "null"}

        # Pick the best candidate: prefer a concrete typed schema over bare {}
        typed = [it for it in non_null if not _is_empty_schema(it)]
        chosen = typed[0] if typed else non_null[0]

        # Merge any sibling keys on the anyOf node into the chosen schema
        # (e.g. "default", "title" sit alongside "anyOf")
        siblings = {k: v for k, v in node.items() if k != "anyOf"}
        return {**chosen, **siblings}

    def walk(node: JSON) -> JSON:
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node

        # Recurse children first (bottom-up)
        node = {k: walk(v) for k, v in node.items()}

        # Collapse anyOf
        if "anyOf" in node:
            node = _collapse_anyof(node)

        # Bare empty schema → object
        if _is_empty_schema(node):
            return {"type": "object"}

        # Object schema missing its type declaration
        if "type" not in node and "properties" in node:
            node["type"] = "object"

        return node

    return walk(s)