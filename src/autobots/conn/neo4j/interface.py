# src/autobots/conn/neo4j/interface.py
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from src.autobots.conn.neo4j.ontology import ExtractedGraph


class GraphDBInterface(ABC):
    @abstractmethod
    def add_graph(
        self,
        graph: ExtractedGraph,
        document_id: str,
        tenant_id: str,
        uuid_map: Dict[str, str],
        s3_url: Optional[str] = None,
    ):
        pass

    @abstractmethod
    def update_document_graph(
        self,
        document_id: str,
        new_graph: ExtractedGraph,
        tenant_id: str,
        uuid_map: Dict[str, str],
        s3_url: Optional[str] = None,
    ):
        pass

    @abstractmethod
    def delete_document_graph(
        self,
        document_id: str,
        tenant_id: str,
        chunk_ids: Optional[List[str]] = None,
    ):
        """
        Delete graph data for one document, scoped to the given tenant.

        If chunk_ids are supplied, parent Document nodes are discovered before
        deleting the chunks. This supports deterministic spreadsheet ingestion,
        which creates derived Document nodes for sheet/table row and metadata
        groups.

        LLM-extracted entity nodes (Person, Organization, Location, LegalTerm,
        Concept) connected to the target chunks are also discovered before
        deletion. Associated sheet nodes are removed when disconnected. Entity
        nodes are removed when no surviving Chunk in the tenant still MENTIONS
        them, preserving entities referenced by another document.

        Entity-to-entity relationships between otherwise shared entities are
        retained conservatively because relationship-level document provenance
        is not currently available.

        The operation is idempotent.
        """
        pass

    @abstractmethod
    def query(
        self, query: str, tenant_id: str, params: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        pass
