from typing import Any, List, Optional

from google.genai.types import GenerateContentConfig, ToolUnion
from pydantic import BaseModel, Field

from src.autobots.conn.gemini.model_registry_gemini import GeminiModelRegistry


class ChatConfigGemini(GenerateContentConfig):
    http_options: None = None
    tools: Optional[List[ToolUnion | str]] = Field(default=None) # type: ignore


class ChatReqGemini(BaseModel):
    model: str = GeminiModelRegistry.default_model()
    contents: List[Any] = Field(default_factory=list)
    config: ChatConfigGemini | None = None
