from dataclasses import dataclass
from typing import List, Literal, Optional, Set

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.autobots.conn.grok.grok_chat.grok_models import GrokRole, ModelRegistry
from src.autobots.conn.grok.grok_chat.grok_user_config import (
    CompiledPlan,
    GrokUserConfig,
    compile_grok_plan,
)


class CollectionAugmentUserConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True

    # Retrieval sources
    collection_ids: List[str] = Field(default_factory=list)

    # Retrieval settings
    search_limit: int = 50
    retrieval_mode: Literal["hybrid", "semantic", "keyword"] = "hybrid"
    instructions: Optional[str] = None

    add_existing_file_ids: List[str] = Field(default_factory=list)
    upload_urls: List[str] = Field(default_factory=list)
    upload_from_action_input_urls: bool = True
    wait_for_indexing: bool = False

    # Context injection controls
    inject_role: Literal["system", "user"] = "system"
    inject_title: str = "Retrieved context from Grok Collections"
    max_chunks: int = 50
    max_chars_per_chunk: int = 1500
    max_total_chars: int = 12000
    include_scores: bool = False
    include_file_ids: bool = True

    attach_retrieval_citations: bool = True

    @model_validator(mode="after")
    def _validate_limits(self):
        if self.search_limit < 1:
            self.search_limit = 1
        if self.max_chunks < 1:
            self.max_chunks = 1
        if self.max_chars_per_chunk < 200:
            self.max_chars_per_chunk = 200
        if self.max_total_chars < 1000:
            self.max_total_chars = 1000
        return self


class GrokCollectionUserConfig(GrokUserConfig):
    model_config = ConfigDict(extra="forbid")

    collection: CollectionAugmentUserConfig = Field(default_factory=CollectionAugmentUserConfig)


@dataclass
class CompiledCollectionAugmentation:
    enabled: bool
    collection_ids: List[str]
    search_limit: int
    retrieval_mode: str
    instructions: Optional[str]

    add_existing_file_ids: List[str]
    upload_urls: List[str]
    wait_for_indexing: bool
    inject_role: GrokRole

    inject_title: str
    max_chunks: int
    max_chars_per_chunk: int
    max_total_chars: int
    include_scores: bool
    include_file_ids: bool
    attach_retrieval_citations: bool


@dataclass
class CompiledCollectionPlan:
    chat_plan: CompiledPlan
    collection: CompiledCollectionAugmentation
    custom_tool_names: Set[str]


def compile_grok_collection_plan(
    user_cfg: GrokCollectionUserConfig, registry: ModelRegistry
) -> CompiledCollectionPlan:
    chat_plan = compile_grok_plan(user_cfg, registry)

    c = user_cfg.collection
    if c.enabled and not c.collection_ids:
        raise ValueError("collection.enabled=true but collection_ids is empty")

    inject_role = GrokRole.system if c.inject_role == "system" else GrokRole.user

    compiled_collection = CompiledCollectionAugmentation(
        enabled=bool(c.enabled),
        collection_ids=list(c.collection_ids or []),
        search_limit=int(c.search_limit),
        retrieval_mode=str(c.retrieval_mode),
        instructions=c.instructions,
        add_existing_file_ids=list(c.add_existing_file_ids or []),
        upload_urls=list(c.upload_urls or []),
        wait_for_indexing=bool(c.wait_for_indexing),
        inject_role=inject_role,
        inject_title=c.inject_title,
        max_chunks=int(c.max_chunks),
        max_chars_per_chunk=int(c.max_chars_per_chunk),
        max_total_chars=int(c.max_total_chars),
        include_scores=bool(c.include_scores),
        include_file_ids=bool(c.include_file_ids),
        attach_retrieval_citations=bool(c.attach_retrieval_citations),
    )

    return CompiledCollectionPlan(
        chat_plan=chat_plan,
        collection=compiled_collection,
        custom_tool_names=set(chat_plan.custom_tool_names or set()),
    )
