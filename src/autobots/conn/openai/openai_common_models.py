from typing import List, Literal, Mapping, TypeAlias

from pydantic import BaseModel, ConfigDict


class OpenaiExtraValues(BaseModel):
    extra_headers: Mapping[str, str] | None = None
    extra_query: Mapping[str, object] | None = None
    extra_body: object | None = None
    timeout: float | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)


OPENAI_MODELS: TypeAlias = Literal[
    "model-router",
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4.1-nano",
    "gpt-4o",
    "gpt-4o-1120",
    "gpt-4o-0806",
    "gpt-4o-0513",
    "gpt-4o-mini",
    "o1",
    "o1-mini",
    "o3",
    "o3-pro",
    "o3-mini",
    "o4-mini",
    "gpt-4-32k",
    "computer-use-preview",
    "deepseek-r1",
]

OPENAI_ASST_MODELS: TypeAlias = Literal[
    # "gpt-4-32k",
    "gpt-4o",
    "gpt-4o-mini",
]


# Supported file formats for file processing
# https://platform.openai.com/docs/guides/tools-file-search
OPENAI_FILE_FORMATS: List[str] = [
    ".c",
    ".cpp",
    ".cs",
    ".css",
    ".doc",
    ".docx",
    ".go",
    ".html",
    ".java",
    ".js",
    ".json",
    ".md",
    ".pdf",
    ".php",
    ".pptx",
    ".py",
    ".rb",
    ".sh",
    ".tex",
    ".ts",
    ".txt",
]

# https://learn.microsoft.com/en-us/azure/ai-services/openai/how-to/code-interpreter?tabs=python#supported-file-types
OPENAI_FILE_FORMATS_CODE_INTERPRETER = [
    "c",
    "cpp",
    "csv",
    "docx",
    "html",
    "java",
    "json",
    "md",
    "pdf",
    "php",
    "pptx",
    "py",
    "rb",
    "tex",
    "txt",
    "css",
    "jpeg",
    "jpg",
    "js",
    "gif",
    "png",
    "tar",
    "ts",
    "xlsx",
    "xml",
    "zip",
]
