from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GeminiCodeExecutionInputFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    gemini_file_name: str | None = None
    display_name: str | None = None
    mime_type: str | None = None

    @model_validator(mode="after")
    def validate_source(self) -> "GeminiCodeExecutionInputFile":
        if not self.url and not self.gemini_file_name:
            raise ValueError("Either url or gemini_file_name is required")
        return self


class GeminiCodeExecutionPreparedInputFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_url: str | None = None
    gemini_file_name: str
    file_uri: str | None = None
    display_name: str | None = None
    mime_type: str | None = None


class GeminiCodeExecutionOutputMode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    return_text_summary: bool = True
    capture_generated_files: bool = True
    include_executed_code: bool = True
    include_raw_parts: bool = False


class GeminiCodeExecutionReq(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: str
    files: list[GeminiCodeExecutionInputFile | dict[str, Any]] = Field(default_factory=list)
    model: str = "gemini-2.5-pro"
    output_mode: GeminiCodeExecutionOutputMode | dict[str, Any] = Field(
        default_factory=GeminiCodeExecutionOutputMode
    )


class GeminiCodeExecutionArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_name: str
    mime_type: str | None = None
    url: str | None = None
    source_part_index: int | None = None


class GeminiCodeExecutionRes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool = True
    summary: str | None = None
    text: str | None = None
    input_files: list[GeminiCodeExecutionPreparedInputFile | dict[str, Any]] = Field(default_factory=list)
    executed_code: list[str] = Field(default_factory=list)
    execution_outputs: list[str] = Field(default_factory=list)
    execution_result_outcomes: list[str] = Field(default_factory=list)
    generated_files: list[GeminiCodeExecutionArtifact | dict[str, Any]] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
