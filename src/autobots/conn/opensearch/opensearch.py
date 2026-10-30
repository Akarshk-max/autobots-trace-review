from functools import lru_cache
from typing import Any, Dict, List

from opensearchpy import OpenSearch
from pydantic import BaseModel

from src.autobots.core.settings import Settings, SettingsProvider


class SearchQuery(BaseModel):
    """Base model for search queries"""

    index: str
    query: Dict[str, Any]


class OpenSearchClient:
    def __init__(self, host: str, port: int, username: str, password: str, use_ssl: bool = True):
        """
        Initialize OpenSearch client with connection parameters.

        :param host: OpenSearch host address
        :param port: OpenSearch port number
        :param username: Authentication username
        :param password: Authentication password
        :param use_ssl: Whether to use SSL for connection
        """
        self.client = OpenSearch(
            hosts=[{"host": host, "port": port}],
            http_auth=(username, password),
            use_ssl=use_ssl,
            verify_certs=False,
        )

    async def index_document(self, index: str, document: Dict[str, Any]) -> Dict[str, Any]:
        """
        Index a document in OpenSearch.

        :param index: Name of the index
        :param document: Document to be indexed
        :return: Response from OpenSearch
        """
        try:
            response = self.client.index(index=index, body=document)
            return response
        except Exception as e:
            raise Exception(f"Failed to index document: {str(e)}")

    async def search(self, search_query: SearchQuery) -> List[Dict[str, Any]]:
        """
        Search for documents in OpenSearch.

        :param search_query: SearchQuery object containing index and query
        :return: List of search results
        """
        try:
            response = self.client.search(body=search_query.query, index=search_query.index)
            return response["hits"]["hits"]
        except Exception as e:
            raise Exception(f"Search failed: {str(e)}")


@lru_cache
def get_opensearch(settings: Settings = SettingsProvider.sget()) -> OpenSearchClient:
    """
    Create and return an instance of OpenSearchClient using settings from the settings provider.

    :param settings: An instance of SettingsProvider
    :return: An instance of OpenSearchClient
    :raises ValueError: If required OpenSearch settings are not found
    """
    host = settings.OPENSEARCH_HOST
    port = settings.OPENSEARCH_PORT
    username = settings.OPENSEARCH_USERNAME
    password = settings.OPENSEARCH_PASSWORD

    if not all([host, port, username, password]):
        raise ValueError("Required OpenSearch settings are not found")

    return OpenSearchClient(host=host, port=port, username=username, password=password)
