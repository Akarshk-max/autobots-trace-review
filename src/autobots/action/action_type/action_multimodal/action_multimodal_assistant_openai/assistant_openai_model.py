from typing import Iterable, Literal

from openai.types import ChatModel
from openai.types.beta import Assistant, AssistantToolParam

from src.autobots.conn.openai.openai_assistants.assistant_model import AssistantCreate


class AssistantOpenaiConfigCreate(AssistantCreate):
    name: str | None = None
    tools: Iterable[AssistantToolParam | str] | None = None
    provider: Literal["azure", "openai"]
    output_text_to_file_char_limit: int | None = 75000


# TODO: being this back, with parent field assistant_id removed
# class AssistantOpenaiConfigUpdate(AssistantUpdate):
#     pass


class AssistantOpenaiConfigUpdate(AssistantCreate):
    name: str | None = None
    tools: Iterable[AssistantToolParam | str] | None = None
    provider: Literal["azure", "openai"] | None = None
    output_text_to_file_char_limit: int | None = None


class AssistantOpenai(Assistant):
    model: ChatModel = "gpt-4o"
    pass


class AssistantOpenaiConfig(AssistantOpenai):
    thread_id: str | None = None
    provider: Literal["azure", "openai"]
    output_text_to_file_char_limit: int | None = None
