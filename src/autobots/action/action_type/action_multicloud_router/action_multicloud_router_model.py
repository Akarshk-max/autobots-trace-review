from typing import Any, List, Literal

from anthropic.types import ModelParam
from pydantic import BaseModel

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.conn.openai.openai_common_models import OPENAI_MODELS


class AWS_LLM(BaseModel):
    cloud: Literal["aws"] = "aws"
    model: ModelParam = "claude-3-5-sonnet-20240620"


class Azure_LLM(BaseModel):
    cloud: Literal["azure"] = "azure"
    model: OPENAI_MODELS = "gpt-4o-mini"


class GCP_LLM(BaseModel):
    cloud: Literal["gcp"] = "gcp"
    model: Literal[
        # Latest Gemini 2.5 models with thinking capabilities (January 2025)
        "gemini-2.5-pro",  # Most intelligent thinking model
        "gemini-2.5-pro-002",  # Latest stable Gemini 2.5 Pro
        "gemini-2.5-flash",  # Hybrid reasoning with thinking budget
        "gemini-2.5-flash-002",  # Latest stable Gemini 2.5 Flash
        "gemini-2.5-flash-8b",  # Lightweight Gemini 2.5 model
        "gemini-2.5-flash-lite",  # Most cost-efficient with thinking
        # Gemini 2.0 models - GA and experimental
        "gemini-2.0-flash",  # GA version of Gemini 2.0 Flash
        "gemini-2.0-flash-exp",  # Experimental Gemini 2.0 Flash
        "gemini-2.0-flash-thinking-exp-1219",  # Flash with thinking mode (Dec 2024)
        # Legacy Gemini 1.5 models (maintained for compatibility)
        "gemini-1.5-pro",  # Alias for latest 1.5 Pro
        "gemini-1.5-pro-002",  # Latest stable Gemini 1.5 Pro
        "gemini-1.5-flash",  # Alias for latest 1.5 Flash
        "gemini-1.5-flash-002",  # Latest stable Gemini 1.5 Flash
        "gemini-1.5-flash-8b",  # Lightweight model
        # Embedding and specialized models
        "text-embedding-004",  # Latest text embedding model
        "text-multilingual-embedding-002",  # Multilingual embedding
        # Aliases for latest models
        "gemini-pro",  # Points to latest Pro model
        "gemini-flash",  # Points to latest Flash model
    ] = "gemini-2.5-flash"


class LlmMsg(BaseModel):
    # role: Literal["system", "user", "assistant"] | GeminiRole = "system"
    role: str = "system"
    content: DataBlock


class RouterConfig(BaseModel):
    engine: AWS_LLM | Azure_LLM | GCP_LLM = AWS_LLM
    instructions: List[LlmMsg] = []
    tools: List[str] = []
    thread: List[Any] = []
