import re
from datetime import datetime
from enum import Enum
from io import BytesIO
from typing import Any, Dict, List, Literal, Optional

from loguru import logger
from openai.types.beta import AssistantTool
from openai.types.beta.assistant import ToolResources
from pydantic import BaseModel, Field, field_validator

from src.autobots.conn.aws.aws_s3 import get_public_s3
from src.autobots.core.clock.clock import Clock
from src.autobots.core.logging.app_code import AppCode
from src.autobots.core.settings import SettingsProvider
from src.autobots.core.url_utils import extract_urls, mask_urls, unmask_urls
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.context import Context
from src.autobots.files.user_files import UserFiles
from src.autobots.user.user_orm_model import UserORM


class Filter(BaseModel):
    action_type : Literal["Autocode"] | None = None

class FileParams(BaseModel):
    filename: str
    content: str


class RunSource(str, Enum):
    """How a turn was submitted. Slack's `subtype`, in effect.

    Without it a schedule firing and a person typing are indistinguishable in a
    transcript - same shape, same stamp, different meaning.
    """

    api = "api"
    webhook = "webhook"
    schedule = "schedule"


class Metadata(BaseModel):
    process_url_in_text: bool = False  # Always keep default as false, otherwise in Graph run value true causes unintended effects
    process_attachment_url: bool = True
    attachment_requested: int = 0


# NSFW/NSFL words to filter out from text and URLs (moved to module level)
NSFW_WORDS = [
    # NSFW content
    "porn",
    "sex",
    "xxx",
    "adult",
    "nude",
    "naked",
    "erotic",
    "sexual",
    "nsfw",
    "mature",
    "explicit",
    "hardcore",
    "fetish",
    "kinky",
    "boobs",
    "tits",
    "ass",
    "dick",
    "cock",
    "pussy",
    "fuck",
    "shit",
    "bitch",
    "whore",
    "slut",
    "milf",
    "lesbian",
    "gay",
    "anal",
    "orgasm",
    "masturbate",
    "cum",
    "suck",
    "blow",
    "strip",
    "escort",
    "slava",
    "funk",
    # NSFL content
    "terror",
    "terrorist",
    "terrorism",
    "murder",
    "kill",
    "death",
    "dead",
    "suicide",
    "violence",
    "violent",
    "gore",
    "blood",
    "torture",
    "abuse",
    "rape",
    "assault",
    "weapon",
    "gun",
    "bomb",
    "explosion",
    "war",
    "massacre",
    "genocide",
    "execution",
    "hanging",
    "stabbing",
    "shooting",
    "brutal",
    "savage",
    "mutilation",
    "dismember",
    "corpse",
    "victim",
    "nsfl",
    "disturbing",
    "graphic",
    "extreme",
    "shocking",
    "horrific",
]

# Safe replacement words (moved to module level)
SAFE_REPLACEMENTS = {
    # NSFW replacements
    "porn": "content",
    "sex": "romance",
    "xxx": "content",
    "adult": "mature",
    "nude": "art",
    "naked": "natural",
    "erotic": "romantic",
    "sexual": "intimate",
    "nsfw": "content",
    "mature": "adult",
    "explicit": "detailed",
    "hardcore": "intense",
    "fetish": "interest",
    "kinky": "unique",
    "boobs": "chest",
    "tits": "chest",
    "ass": "bottom",
    "dick": "person",
    "cock": "rooster",
    "pussy": "cat",
    "fuck": "mess",
    "shit": "stuff",
    "bitch": "person",
    "whore": "person",
    "slut": "person",
    "milf": "person",
    "lesbian": "person",
    "gay": "happy",
    "anal": "detailed",
    "orgasm": "peak",
    "masturbate": "relax",
    "cum": "come",
    "suck": "draw",
    "blow": "wind",
    "strip": "remove",
    "escort": "guide",
    # NSFL replacements
    "terror": "concern",
    "terrorist": "person",
    "terrorism": "activity",
    "murder": "incident",
    "kill": "stop",
    "death": "end",
    "dead": "inactive",
    "suicide": "crisis",
    "violence": "conflict",
    "violent": "intense",
    "gore": "content",
    "blood": "red",
    "torture": "pressure",
    "abuse": "misuse",
    "rape": "assault",
    "assault": "incident",
    "weapon": "tool",
    "gun": "device",
    "bomb": "device",
    "explosion": "event",
    "war": "conflict",
    "massacre": "event",
    "genocide": "tragedy",
    "execution": "process",
    "hanging": "display",
    "stabbing": "incident",
    "shooting": "filming",
    "brutal": "harsh",
    "savage": "wild",
    "mutilation": "damage",
    "dismember": "separate",
    "corpse": "remains",
    "victim": "person",
    "nsfl": "content",
    "disturbing": "concerning",
    "graphic": "visual",
    "extreme": "intense",
    "shocking": "surprising",
    "horrific": "terrible",
    "slava": "person",
    "funk": "music",
}


def filter_nsfw_content(content: str) -> str:
    """Replace NSFW words in text content with safe alternatives, but preserve URLs"""
    if not content:
        return content
    else:
        return content  # TODO: remove this after nsfw bug has been resolved

    # Policy choice:
    # - allowed_schemes restricted to http/https to match previous behavior.
    # - validate=True so only valid URLs are protected; malformed "URL-ish" tokens
    #   will still be filtered for NSFW words (preferred for a content filter).
    temp_content, url_placeholders = mask_urls(
        content,
        allowed_schemes={"http", "https"},
        decode_json_escapes=False,
        validate=True,
    )

    # Convert to lowercase for case-insensitive matching (but work on original case)
    content_lower = temp_content.lower()
    filtered_content = temp_content

    # Replace NSFW words with safe alternatives (excluding URLs)
    for nsfw_word in NSFW_WORDS:
        if nsfw_word in content_lower:
            safe_word = SAFE_REPLACEMENTS.get(nsfw_word, "content")
            # Use case-insensitive replacement
            pattern = re.compile(re.escape(nsfw_word), re.IGNORECASE)
            filtered_content = pattern.sub(safe_word, filtered_content)
            content_lower = pattern.sub(safe_word, content_lower)

    # Restore the original URL substrings.
    filtered_content = unmask_urls(filtered_content, url_placeholders)

    return filtered_content


def filter_nsfw_from_url(url: str) -> str:
    """Replace NSFW words in a URL with safe alternatives"""
    return filter_nsfw_content(url)


class SourceChunk(BaseModel):
    class Config:
        validate_by_name = True

    chunk_id: str
    document_id: Optional[str] = None
    source: str
    s3_url: Optional[str] = None
    text_preview: str
    score: float
    metadata: Dict[str, Any] = Field(default_factory=dict)


class GraphRAGSearchData(BaseModel):
    sources: List[SourceChunk]
    graph_visualization: Optional[Dict[str, Any]] = None


class DataBlock(BaseModel):
    text: str = ""
    urls: List[str] = []
    file_ids: List[str] = []
    files: List[FileParams] = []
    tools: List[AssistantTool] = []
    tool_resources: ToolResources = ToolResources()
    metadata: Metadata = Metadata()
    search_data: Optional[GraphRAGSearchData] = None
    # Who submitted this input, and when. Server-set at the API boundary via
    # stamp_author()/stamped_by() - never read from a request body, or a caller
    # could forge authorship of a turn in a shared run.
    # Optional so every turn written before attribution existed still validates.
    # Model *output* blocks leave both None: a DataBlock is attributed only if
    # user_id is set.
    user_id: Optional[str] = Field(
        default=None,
        description="Server-set. Who submitted this input; anything sent here is discarded.",
        json_schema_extra={"server_set": True},
    )
    mentions: List[str] = Field(
        default=[],
        description="Server-validated. Anyone the sender cannot see here is dropped.",
    )
    created_at: Optional[datetime] = Field(
        default=None,
        description="Server-set. When this input was submitted; anything sent here is discarded.",
        json_schema_extra={"server_set": True},
    )
    source: Optional[RunSource] = Field(
        default=None,
        description="Server-set. How this input was submitted; anything sent here is discarded.",
        json_schema_extra={"server_set": True},
    )
    # Read off the run body by _extract_consumer_id_from_request. It was always
    # part of the run input contract, just undeclared while the endpoint took a
    # raw dict; declaring it keeps it from being dropped now that the body is
    # typed.
    consumer_id: Optional[str] = None

    def stamped_by(
        self,
        user_id: str,
        *,
        source: "RunSource | None" = None,
        at: datetime | None = None,
    ) -> "DataBlock":
        """Copy of this block attributed to `user_id`, discarding any attribution it carried."""
        return self.model_copy(update=_attribution(user_id=user_id, source=source, at=at))

    @classmethod
    @field_validator("text")
    def filter_nsfw_text(cls, text: str) -> str:
        """Filter NSFW words from text content during initialization"""
        return filter_nsfw_content(text)

    # @field_validator('urls')
    # @classmethod
    # def filter_nsfw_urls(cls, urls: List[str]) -> List[str]:
    #     """Filter NSFW words from URLs during initialization"""
    #     if not urls:
    #         return urls

    #     filtered_urls = []
    #     for url in urls:
    #         filtered_url = filter_nsfw_from_url(url)
    #         filtered_urls.append(filtered_url)

    #     return filtered_urls

    def get_llm_prompt(self, ctx: Context):
        prompt = " "
        if self.text:
            prompt = self.text
        if self.metadata.process_url_in_text:
            self.process_url_in_text(ctx)
        if self.urls:
            prompt += f"\nUser-uploaded file URLs: {self.urls}"

        logger.bind(ctx=ctx, code=AppCode.ACTION, prompt=repr(prompt)[:20]).info("DataBlock to LLMPrompt")
        return prompt

    async def maybe_convert_input_to_file(self, ctx: Context, user: UserORM, prompt: str) -> str:
        """
        Convert long input text to a file if it exceeds the character limit.
        
        Args:
            ctx: Context object
            user: UserORM object
            prompt: The assembled LLM prompt string to check
            
        Returns:
            Either the original prompt or a truncated version with file URL
        """
        settings = SettingsProvider.sget()
        if (
            settings.USER_INPUT_TEXT_TO_FILE_CHAR_LIMIT
            and len(prompt) > settings.USER_INPUT_TEXT_TO_FILE_CHAR_LIMIT
        ):
            try:
                s3 = get_public_s3()
                user_files = UserFiles(s3=s3, user=user)
                filename = str(gen_uuid()) + ".txt"
                logger.bind(ctx=ctx, code=AppCode.ACTION, prompt_length=len(prompt)).info("Input text too long, saving to file")
                file_metas = await user_files.upload_binary(
                    ctx=ctx, file_obj=BytesIO(prompt.encode("utf-8")), filename=filename
                )
                file_meta = file_metas[0]
                
                # Append URL in-place to self.urls
                if str(file_meta.url) not in self.urls:
                    self.urls.append(str(file_meta.url))
                
                truncated_prompt = (
                    f"{prompt[:1000]}...\n\nInput saved to file url: {str(file_meta.url)}"
                )
                logger.bind(
                    ctx=ctx, code=AppCode.ACTION, input_url=file_meta.url, original_length=len(prompt)
                ).info(f"Converted long input to file url: {str(file_meta.url)}")
                return truncated_prompt
            except Exception as e:
                # Fallback: if S3 upload fails, truncate to the configured limit
                truncate_length = settings.USER_INPUT_TEXT_TO_FILE_CHAR_LIMIT
                logger.bind(ctx=ctx, code=AppCode.ACTION, error=str(e), prompt_length=len(prompt), truncate_to=truncate_length).error(
                    f"Failed to upload large input to file, truncating to {truncate_length} chars: {str(e)}"
                )
                truncated_prompt = (
                    f"{prompt[:truncate_length]}...\n\n[Input was truncated due to length. File upload failed.]"
                )
                return truncated_prompt
        else:
            return prompt

    def process_url_in_text(self, ctx: Context):
        """
        Extract explicit-scheme URLs from `self.text` and append them to `self.urls`.

        URL extraction is centralized in `src/autobots/core/url_utils.py`:
        - robust extraction in Markdown/JSON-ish text
        - wrapper-aware delimiter handling
        - validation via urllib.parse.urlsplit()

        We intentionally keep behavior consistent with the previous implementation here:
        - only http/https URLs are extracted from user text
        - no JSON-escape decoding in this pass
        - IPv6 remains rejected by default
        """
        if not self.text:
            return

        found_urls = extract_urls(
            self.text,
            allowed_schemes={"http", "https"},
            unique=True,
            validate=True,
            allow_ipv6=False,
            decode_json_escapes=False,
        )
        if not found_urls:
            return

        logger.bind(ctx=ctx, code=AppCode.ACTION, found_urls=found_urls).info("Found URLs in text")
        for url in found_urls:
            if url not in self.urls:
                # Filter NSFW words before adding to urls (currently a no-op until bug resolved)
                self.urls.append(filter_nsfw_from_url(url))

    def _unique_extend(self, target: list, items: list):
        """
        Append items that are not yet in *target* while keeping order.
        Uses a set for O(1) membership checks.
        """
        seen = set(target)
        for it in items:
            if it not in seen:
                target.append(it)
                seen.add(it)

    def merge(self, blocks: List["DataBlock"]) -> "DataBlock":
        if not blocks:
            return DataBlock()

        text_parts: list[str] = []
        urls: list[str] = []
        files: list[FileParams] = []
        tools: list[AssistantTool] = []

        # collect values
        for b in blocks:
            if b.text:
                text_parts.append(b.text)
            self._unique_extend(urls, b.urls)
            files.extend(b.files or [])
            tools.extend(b.tools or [])

        tool_resources = next(
            (b.tool_resources for b in blocks if b.tool_resources), ToolResources()
        )

        metadata = Metadata(
            process_url_in_text=any(b.metadata.process_url_in_text for b in blocks),
            process_attachment_url=any(b.metadata.process_attachment_url for b in blocks),
            attachment_requested=sum(b.metadata.attachment_requested for b in blocks),
        )

        sources: list[SourceChunk] = []
        graph_vis = None
        for b in blocks:
            if b.search_data:
                sources.extend(b.search_data.sources or [])
                if b.search_data.graph_visualization:
                    graph_vis = b.search_data.graph_visualization
        search_data = (
            GraphRAGSearchData(sources=sources, graph_visualization=graph_vis) if sources else None
        )

        return DataBlock(
            text="\n".join(text_parts),
            urls=urls,
            files=files,
            tools=tools,
            tool_resources=tool_resources,
            metadata=metadata,
            search_data=search_data,
        )

    def is_empty(self):
        if self.text or self.urls or self.files or self.tools:
            return False
        else:
            return True


# Every field the server owns, derived from the fields themselves rather than
# listed by hand. Mark a new field `server_set` and the stamp covers it
# automatically - the boundary cannot drift out of sync with the model.
SERVER_SET_FIELDS: frozenset[str] = frozenset(
    name
    for name, field in DataBlock.model_fields.items()
    if isinstance(field.json_schema_extra, dict)
    and field.json_schema_extra.get("server_set")
)


def _attribution(
    *, user_id: str, source: RunSource | None, at: datetime | None
) -> Dict[str, Any]:
    """The complete set of server-set values.

    Its keys must equal SERVER_SET_FIELDS - pinned by a test, so marking a new
    field `server_set` without handling it here fails loudly.
    """
    return {
        "user_id": user_id,
        "created_at": Clock.datetime_now() if at is None else at,
        "source": source,
    }


def stamp_author(
    input_dict: Dict[str, Any] | None,
    *,
    user_id: str,
    source: RunSource | None = None,
    at: datetime | None = None,
) -> Dict[str, Any]:
    """Attribution is server-set: whatever the caller sent for these keys is discarded.

    The dict form, for the paths where the input is not a DataBlock: a schedule
    fires with the input dict APScheduler stored in its job kwargs. Every HTTP
    entry point takes a DataBlock and uses `stamped_by()` instead.
    """
    # Clear every server-set key first, so a stored or forged value cannot
    # survive as a leftover when this stamp does not set that field.
    stamped = {
        k: v for k, v in (input_dict or {}).items() if k not in SERVER_SET_FIELDS
    }
    stamped.update(
        {
            k: v
            for k, v in _attribution(user_id=user_id, source=source, at=at).items()
            if v is not None
        }
    )
    return stamped


class DataBlocks(BaseModel):
    blocks: List[DataBlock] = []


class TextObj(BaseModel):
    text: str = ""

    @classmethod
    @field_validator("text")
    def filter_nsfw_text(cls, text: str) -> str:
        """Filter NSFW words from text content during initialization"""
        return filter_nsfw_content(text)


class AssistantObj(BaseModel):
    text: str = ""
    urls: Optional[List[str]] = []
    tools: Optional[List[AssistantTool]] = []
    tool_resources: Optional[ToolResources] = None

    @classmethod
    @field_validator("text")
    def filter_nsfw_text(cls, text: str) -> str:
        """Filter NSFW words from text content during initialization"""
        return filter_nsfw_content(text)

    @classmethod
    @field_validator("urls")
    def filter_nsfw_urls(cls, urls: Optional[List[str]]) -> Optional[List[str]]:
        """Filter NSFW words from URLs during initialization"""
        if not urls:
            return urls

        filtered_urls = []
        for url in urls:
            filtered_url = filter_nsfw_from_url(url)
            filtered_urls.append(filtered_url)

        return filtered_urls


class TextObjs(BaseModel):
    texts: List[TextObj] = []


class MultiObj(BaseModel):
    text: str = ""
    urls: Optional[List[str]] = []

    @classmethod
    @field_validator("text")
    def filter_nsfw_text(cls, text: str) -> str:
        """Filter NSFW words from text content during initialization"""
        return filter_nsfw_content(text)

    @classmethod
    @field_validator("urls")
    def filter_nsfw_urls(cls, urls: Optional[List[str]]) -> Optional[List[str]]:
        """Filter NSFW words from URLs during initialization"""
        if not urls:
            return urls

        filtered_urls = []
        for url in urls:
            filtered_url = filter_nsfw_from_url(url)
            filtered_urls.append(filtered_url)

        return filtered_urls


class MultiObjs(BaseModel):
    texts: List[TextObj] = []
    urls: Optional[List[str]] = []

    @classmethod
    @field_validator("urls")
    def filter_nsfw_urls(cls, urls: Optional[List[str]]) -> Optional[List[str]]:
        """Filter NSFW words from URLs during initialization"""
        if not urls:
            return urls

        filtered_urls = []
        for url in urls:
            filtered_url = filter_nsfw_from_url(url)
            filtered_urls.append(filtered_url)

        return filtered_urls


# class ImagesResponse(BaseModel):
#     images: List[ImageRes] = []
