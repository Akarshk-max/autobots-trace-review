# import pytest
# from unittest.mock import Mock, patch
# from src.autobots.conn.opensearch.opensearch import (
#     OpenSearchClient,
#     SearchQuery,
#     get_opensearch
# )
# from src.autobots.core.settings import Settings


# @pytest.fixture
# def mock_opensearch():
#     with patch('src.autobots.conn.opensearch.opensearch.OpenSearch') as mock:
#         # Create a mock instance
#         mock_instance = Mock()
#         mock.return_value = mock_instance
#         yield mock_instance


# @pytest.fixture
# def client(mock_opensearch):
#     return OpenSearchClient(
#         host="localhost",
#         port=9200,
#         username="test_user",
#         password="test_pass"
#     )


# @pytest.fixture
# def mock_settings():
#     return Settings(
#         OPENSEARCH_HOST="localhost",
#         OPENSEARCH_PORT=9200,
#         OPENSEARCH_USERNAME="test_user",
#         OPENSEARCH_PASSWORD="test_pass"
#     )


# async def test_index_document_success(client, mock_opensearch):
#     # Arrange
#     document = {"title": "Test", "content": "Test content"}
#     expected_response = {"_id": "123", "result": "created"}
#     mock_opensearch.index.return_value = expected_response

#     # Act
#     response = await client.index_document("test-index", document)

#     # Assert
#     assert response == expected_response
#     mock_opensearch.index.assert_called_once_with(
#         index="test-index",
#         body=document
#     )


# async def test_index_document_failure(client, mock_opensearch):
#     # Arrange
#     document = {"title": "Test"}
#     mock_opensearch.index.side_effect = Exception("Connection error")

#     # Act & Assert
#     with pytest.raises(Exception) as exc_info:
#         await client.index_document("test-index", document)
#     assert "Failed to index document" in str(exc_info.value)


# async def test_search_success(client, mock_opensearch):
#     # Arrange
#     search_query = SearchQuery(
#         index="test-index",
#         query={"query": {"match": {"content": "test"}}}
#     )
#     mock_response = {
#         "hits": {
#             "hits": [
#                 {"_source": {"title": "Test", "content": "test content"}}
#             ]
#         }
#     }
#     mock_opensearch.search.return_value = mock_response

#     # Act
#     results = await client.search(search_query)

#     # Assert
#     assert results == mock_response["hits"]["hits"]
#     mock_opensearch.search.assert_called_once_with(
#         body=search_query.query,
#         index=search_query.index
#     )


# async def test_search_failure(client, mock_opensearch):
#     # Arrange
#     search_query = SearchQuery(
#         index="test-index",
#         query={"query": {"match": {"content": "test"}}}
#     )
#     mock_opensearch.search.side_effect = Exception("Search failed")

#     # Act & Assert
#     with pytest.raises(Exception) as exc_info:
#         await client.search(search_query)
#     assert "Search failed" in str(exc_info.value)


