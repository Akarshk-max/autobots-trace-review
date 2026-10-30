# tests/conn/weaviate/test_weaviate_client.py

import asyncio
import uuid

import pytest
import pytest_asyncio
import weaviate.classes as wvc

from src.autobots.conn.weaviate.weaviate_client import WeaviateVectorDBClient
from src.autobots.core.settings import SettingsProvider

# --- Weaviate Test Config ---
TEST_CLASS_NAME = f"TestLowLevelClient_{uuid.uuid4().hex[:8]}"
TEST_CLASS_SCHEMA = {
    "properties": [
        {"name": "content", "data_type": wvc.config.DataType.TEXT},
        {"name": "source", "data_type": wvc.config.DataType.TEXT},
        {"name": "id_list", "data_type": wvc.config.DataType.TEXT_ARRAY},
    ]
}


@pytest.fixture(scope="module")
def settings():
    return SettingsProvider.sget()


@pytest_asyncio.fixture(scope="function")
async def weaviate_client(settings):
    if not all([settings.WEAVIATE_URL, settings.WEAVIATE_API_KEY]):
        pytest.skip("WEAVIATE_URL or WEAVIATE_API_KEY not set in environment.")

    client = WeaviateVectorDBClient(
        url=settings.WEAVIATE_URL,
        api_key=settings.WEAVIATE_API_KEY,
        batch_size=50,
        skip_init_checks=True,
    )
    await client.ensure_class_exists(TEST_CLASS_NAME, TEST_CLASS_SCHEMA)
    yield client
    await client.delete_class(TEST_CLASS_NAME)
    await client.close()


@pytest.mark.asyncio
async def test_client_upsert_and_get(weaviate_client: WeaviateVectorDBClient):
    doc_uuid = str(uuid.uuid4())
    test_vector = [0.1] * 1536

    objects_to_add = [{"uuid": doc_uuid, "content": "This is a test.", "source": "client-test"}]

    await weaviate_client.upsert_objects(TEST_CLASS_NAME, objects_to_add, vectors=[test_vector])
    await asyncio.sleep(2)

    retrieved_doc = await weaviate_client.get_by_ids(
        TEST_CLASS_NAME, [doc_uuid], ["content", "source"]
    )

    assert retrieved_doc is not None and len(retrieved_doc) == 1
    assert retrieved_doc[0]["properties"]["source"] == "client-test"
    assert retrieved_doc[0]["uuid"] == doc_uuid


@pytest.mark.asyncio
async def test_client_query_with_enhanced_filter(weaviate_client: WeaviateVectorDBClient):
    doc_uuid = str(uuid.uuid4())
    test_vector = [0.2] * 1536
    id_to_find = "abc-123"
    objects_to_add = [
        {
            "uuid": doc_uuid,
            "content": "Another test for filtering.",
            "source": "filter-source",
            "id_list": [id_to_find, "def-456"],
        }
    ]

    await weaviate_client.upsert_objects(TEST_CLASS_NAME, objects_to_add, vectors=[test_vector])
    await asyncio.sleep(2)

    query_vector = [0.201] * 1536
    filter_dictionary = {"id_list__in": [id_to_find]}

    query_results = await weaviate_client.search(
        class_name=TEST_CLASS_NAME,
        query_vector=query_vector,
        limit=1,
        properties=["source"],
        filter_dict=filter_dictionary,
    )

    assert len(query_results) == 1
    assert query_results[0]["properties"]["source"] == "filter-source"


@pytest.mark.asyncio
async def test_client_generative_search(settings):
    """Tests the low-level generative search client method."""
    if not all([settings.WEAVIATE_URL, settings.WEAVIATE_API_KEY, settings.OPENAI_API_KEY]):
        pytest.skip("Test requires Weaviate, Weaviate API Key, and OpenAI API Key.")

    class_name = f"TestGenerative_{uuid.uuid4().hex[:8]}"
    schema = {
        "properties": [{"name": "content", "data_type": wvc.config.DataType.TEXT}],
        "vectorizer_config": wvc.config.Configure.Vectorizer.text2vec_openai(),
        "generative_config": wvc.config.Configure.Generative.openai(),
    }

    client = WeaviateVectorDBClient(
        url=settings.WEAVIATE_URL, api_key=settings.WEAVIATE_API_KEY, skip_init_checks=True
    )
    await client.ensure_class_exists(class_name, schema)

    objects = [
        {
            "uuid": str(uuid.uuid4()),
            "content": "Photosynthesis is the process by which plants use sunlight to create food.",
        }
    ]
    # Let Weaviate create the vector
    await client.upsert_objects(class_name, objects)
    await asyncio.sleep(2)

    results = await client.generative_search(
        class_name=class_name,
        query_text="What is photosynthesis?",
        generative_prompt="Explain {content} in one sentence.",
    )

    assert len(results) > 0
    assert "generated" in results[0]["_additional"]
    assert "sunlight" in results[0]["_additional"]["generated"].lower()

    await client.delete_class(class_name)
    await client.close()


# ADDED: Test for the new upsert_objects return value.
@pytest.mark.asyncio
async def test_upsert_objects_return_value(weaviate_client: WeaviateVectorDBClient):
    """Tests that upsert_objects returns a tuple of (successful_ids, failed_objects)."""
    doc_uuid = str(uuid.uuid4())
    objects_to_add = [{"uuid": doc_uuid, "content": "Test return value."}]

    successful, failed = await weaviate_client.upsert_objects(
        TEST_CLASS_NAME, objects_to_add, vectors=[[0.3] * 1536]
    )

    assert isinstance(successful, list)
    assert isinstance(failed, list)
    assert doc_uuid in successful
    assert len(failed) == 0


# ADDED: Test for hybrid search at the client level.
@pytest.mark.asyncio
async def test_client_hybrid_search(settings):
    """Tests the low-level hybrid search client method."""
    if not all([settings.WEAVIATE_URL, settings.WEAVIATE_API_KEY]):
        pytest.skip("Test requires Weaviate connection.")

    class_name = f"TestClientHybrid_{uuid.uuid4().hex[:8]}"
    schema = {
        "properties": [{"name": "content", "data_type": wvc.config.DataType.TEXT}],
        # Use native vectorizer for hybrid search
        "vectorizer_config": wvc.config.Configure.Vectorizer.text2vec_openai(),
    }

    client = WeaviateVectorDBClient(
        url=settings.WEAVIATE_URL, api_key=settings.WEAVIATE_API_KEY, skip_init_checks=True
    )
    await client.ensure_class_exists(class_name, schema)

    objects = [
        {
            "uuid": str(uuid.uuid4()),
            "content": "The Eiffel Tower is a famous landmark in Paris, France.",
        }
    ]
    await client.upsert_objects(class_name, objects)
    await asyncio.sleep(2)

    # This query uses a strong keyword "Paris"
    results = await client.search(
        class_name=class_name,
        query_text="What is the most famous landmark in Paris?",
        search_type="hybrid",
        alpha=0.5,
    )

    assert len(results) > 0
    assert "Eiffel Tower" in results[0]["properties"]["content"]

    await client.delete_class(class_name)
    await client.close()
