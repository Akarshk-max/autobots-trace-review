from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM


async def llm_func_check_retry_urls(
    ctx: Context,
    user: UserORM,
    db: AsyncIOMotorDatabase,
    assistant_output: str,
    notify: Notify | None = None,
) -> str:
    # Lazy imports to avoid circular dependency with openai_responses
    from openai.types.chat import ChatCompletionDeveloperMessageParam

    from src.autobots.action.action_type.action_text2text.action_text2text_llm_chat_openai_v2 import (
        ActionText2TextLlmChatOpenai,
        LlmChatOpenaiConfig,
    )

    ret = ""
    SANDBOX_PATH_PREFIX = "sandbox:/mnt/data/"
    SANDBOX_URLS_NOT_FOUND_MESSAGE = "No valid sandbox URLs found in the assistant output."
    SANDBOX_URLS_FOUND_MESSAGE = "sandbox URLs: "
    try:
        prompt_req = f"""
            You are a strict extractor.

            Your job is to read a piece of text and identify only the real downloadable sandbox file URLs present in it.

            A valid sandbox file URL must satisfy all of the following:
            1. It starts exactly with: {SANDBOX_PATH_PREFIX}
            2. It contains an actual file path after that prefix
            3. It ends with a concrete filename
            4. It is not a placeholder, template, example, or incomplete path

            Do NOT include any of the following:
            - {SANDBOX_PATH_PREFIX}
            - {SANDBOX_PATH_PREFIX}/...
            - placeholders such as `<filename>`, `{{filename}}`, or similar
            - example paths mentioned only for explanation
            - incomplete paths without a filename
            - informal mentions of the sandbox path that are not actual file URLs

            Important:
            - Extract URLs exactly as they appear in the input text
            - Do not correct, rewrite, or normalize them
            - Do not add URLs that are not explicitly present
            - If the same valid URL appears multiple times, include it only once
            - Ignore markdown formatting, surrounding punctuation, and explanatory text unless they are part of the actual URL

            Return output in exactly one of these two formats:

            If one or more valid sandbox URLs are found:
            {SANDBOX_URLS_FOUND_MESSAGE}(url1, url2, url3, ...)

            If no valid sandbox URLs are found:
            {SANDBOX_URLS_NOT_FOUND_MESSAGE}

            Return nothing except the final answer in one of the two formats above.
        """
        action_config = LlmChatOpenaiConfig(
        provider="openai",
        messages=[
            ChatCompletionDeveloperMessageParam(
                role="developer",
                content=prompt_req,
            )
        ],
        model="gpt-5.2",
    )
        logger.bind(ctx=ctx).info(f"config is {action_config} and type is {type(action_config)}")
        action_config = await ActionText2TextLlmChatOpenai.create_config(
            ctx=ctx, config_create=action_config, user=user, db=db
        )
        action = ActionText2TextLlmChatOpenai(action_config=action_config, user=user, db=db)
        text=f"Below is the Output from the LLM that may contain the sandbox URLs: {assistant_output}"
        output = await action.run_action(
            ctx=ctx, action_input=DataBlock(text=text)
        )
        match output:
            case DataBlocks():
                for block in output.blocks:
                    ret += block.text + "\n"
            case Exception() as exc:
                raise exc
            
        return ret
    except Exception as e:
        logger.bind(ctx=ctx).exception(f"Error in run tool of LLM FUNC CHECK RETRY URLS: {str(e)}")
        raise