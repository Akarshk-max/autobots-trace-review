# src/autobots/conn/neo4j/ontology.py
import json
from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field


class BaseNode(BaseModel):
    id: str = Field(
        description="A unique, document-local identifier for the node. For global entities like 'Microsoft', use the canonical name. For text chunks, use a sequential ID like 'chunk_0', 'chunk_1'. For structural elements like 'Section 1', use that name."
    )


class ChunkNode(BaseNode):
    label: Literal["Chunk"] = "Chunk"
    text: str = Field(description="The raw text content of the chunk.")
    chunk_type: str = Field(
        description="The semantic type of the chunk (e.g., 'Obligation', 'Prohibition', 'ChatMessage', 'SectionHeader')."
    )
    speaker: Optional[Literal["User", "AI"]] = Field(default=None)
    properties: Dict[str, Any] = Field(
        default_factory=dict, description="Extracted parameters like dates, amounts, etc."
    )
    order: int = Field(
        description="The sequential order of this chunk within the document, starting from 0."
    )


class EntityNode(BaseNode):
    label: Literal["Person", "Organization", "Location", "LegalTerm", "Concept"]
    name: str = Field(description="The canonical name of the entity.")


class Relationship(BaseModel):
    source_id: str = Field(description="The ID of the source node.")
    target_id: str = Field(description="The ID of the target node.")
    type: str = Field(
        description="The type of the relationship (e.g., MENTIONS, PART_OF, REFERENCES, NEXT, DESCRIBES)."
    )
    properties: Dict[str, Any] = Field(default_factory=dict)


class AliasDefinition(BaseModel):
    alias: str = Field(description="The alias or shorthand name (e.g., 'Party A').")
    refers_to: str = Field(
        description="The canonical entity name the alias refers to (e.g., 'Microsoft')."
    )
    scope: Literal["document", "global"] = Field(
        description="Determine the scope. 'document' for contextual aliases like 'The Tenant', 'global' for common colloquialisms like 'MSFT' for 'Microsoft'."
    )


class PartyGroupDefinition(BaseModel):
    group_name: str = Field(description="The name of the collective group (e.g., 'The Alliance').")
    members: List[str] = Field(description="A list of the canonical names of the members.")
    scope: Literal["document", "global"] = Field(
        description="Determine the scope. 'document' for contextual groups like 'the Parties', 'global' for formal entities like 'The Software Alliance Inc.'."
    )


class ExtractedGraph(BaseModel):
    nodes: List[Union[ChunkNode, EntityNode]] = Field(default_factory=list)
    relationships: List[Relationship] = Field(default_factory=list)
    aliases: List[AliasDefinition] = Field(default_factory=list)
    party_groups: List[PartyGroupDefinition] = Field(default_factory=list)


def get_ontology_json_string() -> str:
    schema = {"output_format": ExtractedGraph.model_json_schema()}
    return json.dumps(schema, indent=2)
