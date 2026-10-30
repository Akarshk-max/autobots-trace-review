from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class SanitizerPolicy(BaseModel):
    """
    Policy knobs to keep this module extensible without a pipeline framework.

    - clear_output_text_annotations:
        If True, always clear `output_text.annotations` in outgoing payloads.
        Rationale: annotations returned by OpenAI are output-only metadata (citations, container files)
        and OpenAI rejects replaying them back in `input` (e.g. unknown_parameter for container_id,
        missing_required_parameter for index). The model does not need these to "remember" context;
        the conversation text/tool calls are sufficient.

    - clear_annotations_if_text_missing:
        If output_text.text is empty/None and annotations exist, clear annotations list.
        Safety-first default.

    - enforce_bounds:
        If True, require 0 <= index < len(text) when text is present.
        If False, require only that index is an int (bounds not checked).
        NOTE: disabling bounds may reintroduce future risk if OpenAI enforces bounds later.
    """

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
    )

    clear_output_text_annotations: bool = True
    clear_annotations_if_text_missing: bool = True
    enforce_bounds: bool = True


DEFAULT_POLICY = SanitizerPolicy()


class SanitizerReport(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
    )

    repaired: int = 0
    dropped: int = 0
    cleared_lists: int = 0

    def to_dict(self) -> dict[str, int]:
        return self.model_dump()


def _to_dict(x: Any) -> Any:
    return x.model_dump(exclude_none=True) if isinstance(x, BaseModel) else x


def _to_list(x: Any) -> list[Any] | None:
    """
    Normalize common container shapes into a list.
    Returns None if it can't be interpreted as a list-like container.
    """
    x = _to_dict(x)
    if x is None:
        return None
    if isinstance(x, list):
        return x
    if isinstance(x, tuple):
        return list(x)
    return None


def _to_int(x: Any) -> int | None:
    if isinstance(x, int):
        return x
    if isinstance(x, str):
        s = x.strip()
        if s.isdigit():
            try:
                return int(s)
            except Exception:
                return None
    return None


def _in_bounds(idx: int, text: str) -> bool:
    return 0 <= idx < len(text)


def _is_valid_index(idx: int | None, text: str, *, enforce_bounds: bool) -> bool:
    """
    Valid if:
      - idx is not None, and
      - if enforce_bounds and text is present, idx must be in bounds
      - if enforce_bounds and text is empty, bounds cannot be checked => accept idx (policy may clear earlier)
    """
    if idx is None:
        return False
    if not enforce_bounds:
        return True
    if not text:
        return True
    return _in_bounds(idx, text)


def _derive_index_from_text(ann: dict[str, Any], text: str) -> int | None:
    """
    Derive index from *current* output_text.text (after sandbox->S3 rewrite).
    Prefer markdown label [filename](...) to avoid matching inside URL first.
    """
    if not text:
        return None

    fn = ann.get("filename")
    if isinstance(fn, str) and fn:
        # Prefer label match first (avoid matching inside URL first)
        for needle, off in ((f"[{fn}](", 1), (f"[{fn}]", 1)):
            p = text.find(needle)
            if p != -1:
                return p + off

        p = text.find(fn)
        if p != -1:
            return p

        for needle in (f"sandbox:/mnt/data/{fn}", f"/mnt/data/{fn}"):
            p = text.find(needle)
            if p != -1:
                return p + (len(needle) - len(fn))

    url = ann.get("url")
    if isinstance(url, str) and url:
        p = text.find(url)
        if p != -1:
            return p
        p = text.find(url.rstrip("/"))
        if p != -1:
            return p

    snippet = ann.get("text")
    if isinstance(snippet, str) and snippet:
        p = text.find(snippet)
        if p != -1:
            return p

    return None


def sanitize_outgoing_payload(
    payload: dict[str, Any],
    *,
    policy: SanitizerPolicy | None = None,
) -> tuple[dict[str, Any], SanitizerReport]:
    """
    Sanitize the outgoing wire-shape payload dict to avoid replay 400s like:
      Missing required parameter: input[i].content[j].annotations[k].index
      Unknown parameter: input[i].content[j].annotations[k].container_id

    Scope:
      - Only touches items where item.type == "message"
      - Only touches content parts where part.type == "output_text"
      - Only touches part.annotations

    Behavior (hybrid repair-or-drop + B2 fallback):
      - Normalize annotations to a list (else set to [])
      - If policy.clear_output_text_annotations: clear annotations (most robust)
      - If text missing and policy says so: clear annotations
      - For each annotation:
          * keep if existing index is valid
          * else derive from current text (filename/url/snippet)
          * else use start_index if valid
          * else drop
      - If any remaining annotation violates invariants => clear annotations for that part

    NOTE: Mutates `payload` in-place. Intended usage: pass a fresh dict from model_dump().
    """
    policy = policy or DEFAULT_POLICY
    rep = SanitizerReport()

    inp = _to_list(payload.get("input"))
    if inp is None:
        return payload, rep
    payload["input"] = inp

    for i in range(len(inp)):
        item = _to_dict(inp[i])
        inp[i] = item
        if not isinstance(item, dict) or item.get("type") != "message":
            continue

        content = _to_list(item.get("content"))
        if content is None:
            continue
        item["content"] = content

        for j in range(len(content)):
            part = _to_dict(content[j])
            content[j] = part
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue

            text = part.get("text")
            text_str = text if isinstance(text, str) else ""

            anns = _to_list(part.get("annotations"))
            if anns is None:
                part["annotations"] = []
                continue
            part["annotations"] = anns  # normalize

            # Most robust: never replay output annotations back to OpenAI.
            if policy.clear_output_text_annotations and anns:
                part["annotations"] = []
                rep.cleared_lists += 1
                continue

            if policy.clear_annotations_if_text_missing and not text_str and anns:
                part["annotations"] = []
                rep.cleared_lists += 1
                continue

            new_anns: list[dict[str, Any]] = []
            for ann in anns:
                ann = _to_dict(ann)
                if not isinstance(ann, dict):
                    rep.dropped += 1
                    continue

                idx = _to_int(ann.get("index"))
                if _is_valid_index(idx, text_str, enforce_bounds=policy.enforce_bounds):
                    ann["index"] = idx
                    new_anns.append(ann)
                    continue

                derived = _derive_index_from_text(ann, text_str)
                if derived is not None and _is_valid_index(
                    derived, text_str, enforce_bounds=policy.enforce_bounds
                ):
                    ann["index"] = derived
                    rep.repaired += 1
                    new_anns.append(ann)
                    continue

                start_idx = _to_int(ann.get("start_index"))
                if _is_valid_index(start_idx, text_str, enforce_bounds=policy.enforce_bounds):
                    ann["index"] = start_idx
                    rep.repaired += 1
                    new_anns.append(ann)
                    continue

                rep.dropped += 1

            part["annotations"] = new_anns

            # B2 fallback: enforce invariants on what remains
            bad = False
            for ann in part["annotations"]:
                if not isinstance(ann, dict):
                    bad = True
                    break
                idx = _to_int(ann.get("index"))
                if idx is None:
                    bad = True
                    break
                # Bounds only meaningful if there's text and policy enforces bounds.
                if policy.enforce_bounds and text_str and not _in_bounds(idx, text_str):
                    bad = True
                    break
            if bad:
                part["annotations"] = []
                rep.cleared_lists += 1

    return payload, rep
