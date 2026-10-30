from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.conn.openrouter.chat_model import OpenRouterChatReq
from src.autobots.conn.openrouter.utils import dedupe_urls, is_tool_present, to_plain
from src.autobots.conn.openrouter.vector_store.manifest_manager import VectorStoreManifestManager

RUNTIME_START = "__AUTOBOTS_RUNTIME_CONTEXT_START__"
RUNTIME_END = "__AUTOBOTS_RUNTIME_CONTEXT_END__"


class RuntimeContextContributor(ABC):
    @abstractmethod
    def build(self, action_config: OpenRouterChatReq, action_input: DataBlock) -> str | None:
        raise NotImplementedError


class DaytonaContributor(RuntimeContextContributor):
    def build(self, action_config: OpenRouterChatReq, action_input: DataBlock) -> str | None:
        if not is_tool_present(getattr(action_config, "tools", None), "daytona_sandbox"):
            return None

        input_urls = dedupe_urls(list(getattr(action_input, "urls", None) or []))
        known_urls = dedupe_urls(
            list(getattr(action_config, "current_turn_urls", None) or [])
            + list(getattr(action_config, "all_turn_urls", None) or [])
            + list(getattr(action_config, "current_turn_vector_store_urls", None) or [])
        )

        lines: list[str] = [
            "Daytona code interpreter (daytona_sandbox):",
            "- Run Python code in a remote sandbox.",
            "- Use this for calculations and data processing; ALWAYS prefer this for tabular files (csv/tsv/xls/xlsx).",
            "",
            "How to use:",
            "- Call daytona_sandbox with operation='run' and provide:",
            "  - code: Python code as a string",
            "  - input_urls: URLs to download into the sandbox before running",
            "  - pip_packages: optional deps (e.g., pandas, openpyxl)",
            "  - output_files: filenames you will write; the tool will upload and return their URLs",
            "",
            "Rules:",
            "- For csv/tsv/xls/xlsx and dataframe-like operations: prefer daytona_sandbox over openai_vector_store_tool.",
            "- If the user asks you to create or generate files (e.g., HTML pages, csv , json or any files): use daytona_sandbox to generate the file(s) and return output URLs.",
            "- Do not paste large file contents into the prompt; use URLs + sandbox code.",
            "- Sometimes input code you use in args if it crossed the threshold then it may be converted into public s3 txt file url for prevention of context bloating and in next turn you will see a txt file url instead of giant code so do not halluchunate, you can proceed because it does not affect tool calling at all."
        ]

        if input_urls:
            lines.append("")
            lines.append("Input URLs for this turn (suggested daytona_sandbox.input_urls):")
            for u in input_urls[:10]:
                lines.append(f"- {u}")

        if known_urls:
            lines.append("")
            lines.append("Known URLs (may also be used as input_urls):")
            for u in known_urls[:10]:
                lines.append(f"- {u}")

        return "\n".join(lines)


class VectorStoreContributor(RuntimeContextContributor):
    def build(self, action_config: OpenRouterChatReq, action_input: DataBlock) -> str | None:
        if not getattr(action_config, "vector_store_enabled", True):
            return None
        if not is_tool_present(getattr(action_config, "tools", None), "openai_vector_store_tool"):
            return None

        manifest = to_plain(getattr(action_config, "vector_store_manifest", None) or [])
        c = VectorStoreManifestManager.counts(manifest)

        lines = [
            "Persistent retrieval state:",
            f"- vector_store_enabled: {bool(getattr(action_config, 'vector_store_enabled', True))}",
            f"- vector_store_id: {getattr(action_config, 'vector_store_id', None)}",
            f"- indexed_files_count: {c['indexed']}",
            f"- prepared_files_count: {c['prepared']}",
            f"- pending_files_count: {c['pending']}",
            f"- failed_files_count: {c['failed']}",
            "",
            "Vector-store-ready URLs for this turn:",
        ]

        current_turn_vector_store_urls = list(
            getattr(action_config, "current_turn_vector_store_urls", None) or []
        )
        if current_turn_vector_store_urls:
            for url in current_turn_vector_store_urls[:10]:
                parsed = urlparse(str(url or ""))
                display = unquote(Path(parsed.path or "").name) or str(url or "unknown_file")
                lines.append(f"- {display}: {url}")
        else:
            lines.append("- none")

        lines.extend(
            [
                "",
                "Rules:",
                "- Current turn URLs are automatically prepared and indexed into the vector store when possible.",
                "- Use openai_vector_store_tool to query indexed files or add new URLs discovered mid-conversation.",
                "- Do not use openai_vector_store_tool for csv, tsv, xls, or xlsx files; use daytona_sandbox instead.",
            ]
        )
        return "\n".join(lines)


class InspectContributor(RuntimeContextContributor):
    def build(self, action_config: OpenRouterChatReq, action_input: DataBlock) -> str | None:
        if not is_tool_present(getattr(action_config, "tools", None), "openrouter_inspect"):
            return None

        visible_turns = int(getattr(action_config, "visible_turns", 20))
        include_prev_tool_msgs = bool(getattr(action_config, "include_previous_tool_messages", False))
        tool_turns = min(
            int(getattr(action_config, "previous_tool_messages_turns", 5) or 5),
            visible_turns,
        ) if include_prev_tool_msgs else 0

        tool_msg_policy = (
            f"included for the last {tool_turns} turn(s)" if include_prev_tool_msgs else "NOT included"
        )

        lines: list[str] = [
            "OpenRouter inspect tool (openrouter_inspect):",
            "- The runtime stores full chat history and tool results out-of-band.",
            f"- Only the last {visible_turns} chat turns are included in the model's visible context window.",
            f"- Previous tool-call messages are {tool_msg_policy} in the visible context.",
            "",
            "How to use (only when needed):",
            "- If you need older conversation context: call openrouter_inspect with op='chat_history'.",
            "- To semantically search older turns (may fall outside the visible window), set semantic_query with op='chat_history'.",
            "- If you need outputs of prior tool calls: call openrouter_inspect with op='tool_results'.",
            "",
            "Rule:",
            "- Don't overuse openrouter_inspect; use it only when you truly need older context or prior tool calling related stuff.",
        ]
        return "\n".join(lines)


DEFAULT_CONTRIBUTORS: list[RuntimeContextContributor] = [
    DaytonaContributor(),
    VectorStoreContributor(),
    InspectContributor(),
]


def build_runtime_context(
    action_config: OpenRouterChatReq,
    action_input: DataBlock,
    contributors: list[RuntimeContextContributor] | None = None,
) -> str | None:
    contributors = contributors or DEFAULT_CONTRIBUTORS
    blocks: list[str] = []
    for c in contributors:
        text = c.build(action_config, action_input)
        if text:
            blocks.append(text)
    if not blocks:
        return None
    return "\n\n".join(blocks)


def inject_runtime_context(
    messages: list[dict[str, Any]],
    runtime_text: str,
) -> list[dict[str, Any]]:
    wrapped = f"{RUNTIME_START}\n{runtime_text}\n{RUNTIME_END}"
    base_messages = to_plain(messages)
    if base_messages and isinstance(base_messages[0], dict) and base_messages[0].get("role") == "system":
        base0 = dict(base_messages[0])
        existing = base0.get("content")
        existing_str = existing if isinstance(existing, str) else ("" if existing is None else str(existing))
        base0["content"] = (existing_str.rstrip() + "\n\n" + wrapped).strip()
        base_messages[0] = base0
    else:
        base_messages = [{"role": "system", "content": wrapped}] + base_messages
    return base_messages


def strip_runtime_context(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for item in messages or []:
        try:
            role = str(item.get("role") or "")
            content = item.get("content")
        except Exception:
            cleaned.append(item)
            continue

        if role == "system" and isinstance(content, str):
            if RUNTIME_START in content and RUNTIME_END in content:
                try:
                    before = content.split(RUNTIME_START, 1)[0]
                    after = content.split(RUNTIME_END, 1)[1]
                    content_stripped = (before + after).strip()
                    if not content_stripped:
                        continue
                    item = dict(item)
                    item["content"] = content_stripped
                except Exception:
                    pass

        cleaned.append(item)
    return cleaned
