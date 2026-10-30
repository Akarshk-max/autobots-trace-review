from typing import Any

from src.autobots.conn.gemini.code_execution.code_execution_models import (
    GeminiCodeExecutionRes,
)
from src.autobots.conn.gemini.message_normalizer_gemini import GeminiMessageNormalizer


class GeminiCodeExecutionParser:
    @staticmethod
    def _part_text(part: Any) -> str | None:
        if isinstance(part, dict):
            text = part.get("text")
            return text if isinstance(text, str) and text else None
        text = getattr(part, "text", None)
        return text if isinstance(text, str) and text else None

    @staticmethod
    def _part_executable_code(part: Any) -> str | None:
        executable_code = None
        if isinstance(part, dict):
            executable_code = part.get("executable_code") or part.get("executableCode")
        else:
            executable_code = getattr(part, "executable_code", None) or getattr(part, "executableCode", None)

        if executable_code is None:
            return None

        if isinstance(executable_code, dict):
            code = executable_code.get("code")
            return code if isinstance(code, str) and code else None
        code = getattr(executable_code, "code", None)
        return code if isinstance(code, str) and code else None

    @staticmethod
    def _part_execution_result(part: Any) -> dict[str, Any] | None:
        result = None
        if isinstance(part, dict):
            result = part.get("code_execution_result") or part.get("codeExecutionResult")
        else:
            result = getattr(part, "code_execution_result", None) or getattr(part, "codeExecutionResult", None)

        if result is None:
            return None
        if isinstance(result, dict):
            return result
        if hasattr(result, "model_dump"):
            return result.model_dump(exclude_none=True)
        return None

    @classmethod
    def parse_response(cls, response: Any) -> GeminiCodeExecutionRes:
        plain = GeminiMessageNormalizer.normalize_response(response)
        candidate_content = GeminiMessageNormalizer.first_candidate_content(plain)
        parts = candidate_content.get("parts") if candidate_content else []
        if not isinstance(parts, list):
            parts = []

        text_parts: list[str] = []
        executed_code: list[str] = []
        execution_outputs: list[str] = []
        execution_result_outcomes: list[str] = []
        raw_inline_parts: list[dict[str, Any]] = []
        raw_execution_results: list[dict[str, Any]] = []

        for part in parts:
            text = cls._part_text(part)
            if text:
                text_parts.append(text)

            code = cls._part_executable_code(part)
            if code:
                executed_code.append(code)

            result = cls._part_execution_result(part)
            if result:
                raw_execution_results.append(result)
                output = result.get("output")
                if isinstance(output, str) and output:
                    execution_outputs.append(output)
                outcome = result.get("outcome")
                if isinstance(outcome, str) and outcome:
                    execution_result_outcomes.append(outcome)

            if isinstance(part, dict) and (part.get("inline_data") or part.get("inlineData")):
                raw_inline_parts.append(part)

        summary = (
            f"Gemini code execution returned {len(executed_code)} code block(s), "
            f"{len(execution_outputs)} execution output block(s), and {len(raw_inline_parts)} artifact part(s)."
        )

        return GeminiCodeExecutionRes(
            summary=summary,
            text="\n\n".join(text_parts).strip() or None,
            executed_code=executed_code,
            execution_outputs=execution_outputs,
            execution_result_outcomes=execution_result_outcomes,
            meta={
                "raw_response": plain,
                "artifact_parts": raw_inline_parts,
                "execution_results": raw_execution_results,
                "finish_reason": GeminiMessageNormalizer.first_finish_reason(plain),
                "parts_count": len(parts),
            },
        )
