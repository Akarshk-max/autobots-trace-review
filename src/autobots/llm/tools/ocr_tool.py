from typing import List

from google.genai import types
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_gemini import (
    ActionText2TextLlmChatGemini,
    GeminiChatActionConfig,
)
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

DEFAULT_OCR_PROMPT = "Extract file text verbatim and OCR scanned portions of the file"
OCR_TOOL_OUTPUT_TEXT_TO_FILE_CHAR_LIMIT = 50_000
OCR_STREAM = False
OCR_TEMPERATURE = 0.2


class GeminiOCRToolConfig(BaseModel):
    model: str = "gemini-3.7-flash"
    contents: List[types.Content] = Field(
        default_factory=lambda: [
            types.Content(
                role="user",
                parts=[types.Part(text=DEFAULT_OCR_PROMPT)],
            )
        ]
    )
    file_paths: List[str] = Field(default_factory=list)



def _extract_text_prompt_from_contents(contents: List[types.Content] | None) -> str:
    prompts: list[str] = []
    for content in contents or []:
        parts = getattr(content, "parts", None) or []
        for part in parts:
            text = getattr(part, "text", None)
            if isinstance(text, str) and text.strip():
                prompts.append(text.strip())
    combined = "\n".join(prompts).strip()
    return combined or DEFAULT_OCR_PROMPT



def _build_action_config(config: GeminiOCRToolConfig) -> GeminiChatActionConfig:
    return GeminiChatActionConfig(
        model=config.model,
        tool_output_text_to_file_char_limit=OCR_TOOL_OUTPUT_TEXT_TO_FILE_CHAR_LIMIT,
        stream=OCR_STREAM,
        temperature=OCR_TEMPERATURE,
    )


async def ocr_tool(
    config: GeminiOCRToolConfig,
    ctx: Context,
    app_state: AppState,
    user: UserORM,
    db: AsyncIOMotorDatabase,
    notify: Notify | None = None,
) -> DataBlocks | Exception:
    """
    Description
    -----------
    Performs OCR using Gemini by delegating to the current Gemini chat action.
    The tool accepts a model name, instruction contents, and file URLs, then
    routes the OCR request through the modern Gemini chat/file pipeline.

    Input
    -----
    config : GeminiOCRToolConfig
        model : str
            The Gemini model name to use. Defaults to "gemini-3.7-flash".

        contents : list[types.Content]
            Instruction content used to build the OCR prompt.

        file_paths : list[str]
            File URLs to be processed. These may be images or PDFs.

    Output
    ------
    DataBlocks
        Extracted OCR text returned from the Gemini chat action.
    """
    try:
        prompt = _extract_text_prompt_from_contents(config.contents)
        action_config = _build_action_config(config)

        action = ActionText2TextLlmChatGemini(
            action_config=action_config,
            user=user,
            db=db,
        )
        data_blocks: DataBlocks = await action.run_action(
            ctx=ctx,
            app_state=app_state,
            action_input=DataBlock(
                text=prompt,
                urls=list(config.file_paths or []),
            ),
            notify=notify,
        )
        return data_blocks

    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in ocr_tool: {str(e)}")
        return e
