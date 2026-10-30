# tests/ingestion/scripts/e2e_graphrag_action_runner.py

import asyncio
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

import httpx
from jose import jwt
from loguru import logger

# --- Setup Project Path ---
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ruff: noqa: E402
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_multimodal.action_multimodal_graph_rag_query import (
    ActionGraphRAGQuery,
    GraphRAGQueryConfig,
)
from src.autobots.conn.aws.aws_s3 import get_aws_s3
from src.autobots.conn.weaviate.weaviate_client import WeaviateVectorDBClient
from src.autobots.core.database.mongo_base import close_mongo_client, get_mongo_db
from src.autobots.core.logging.setup_logger import setup_logger
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.context import Context
from src.autobots.ingestion.neo4j_loader import Neo4jManager
from src.autobots.user.user_orm_model import UserORM

# ==========================================================
# === TEST CONFIGURATION & ASSETS ==========================
# ==========================================================

# --- STRATEGY 2b: Fixed, Persistent Resources with Pre-Test Cleanup ---
# We use a fixed user ID and collection name to sidestep creation race conditions.
# A robust cleanup function is run before the tests to ensure isolation.
# FIXED_USER_ID = "e2e_fixed_user_for_graphrag"
FIXED_USER_ID = "56dd105c-606e-4f6d-8e88-8001d8f4f552"
FIXED_COLLECTION_NAME = "E2eSharedGraphragCollection"

TEST_CASES = [
    # {
    #     "name": "[GraphRAG] 'A Poem for Mother' E2E Test",
    #     "difficulty": "easy",
    #     "url": "https://www.elearning.panchakotmv.ac.in/files/D2B9982B15893790540.pdf",
    #     "filename": "a_poem_for_mother.pdf",
    #     "question": "Based on the poem, what are the mother's hardships and what are the son's feelings towards her?",
    #     "expected_concepts": {
    #         "mother_hardship": [
    #             "toil",
    #             "hardship",
    #             "fingers to the bone",
    #             "burdens",
    #             "labor",
    #             "struggles",
    #         ],
    #         "son_remorse": [
    #             "sorry",
    #             "forgive",
    #             "guilt",
    #             "regret",
    #             "small man",
    #             "inherited nothing",
    #         ],
    #     },
    # },
    {
        "name": "[GraphRAG] E2E Multimedia Ingestion and Query (Image)",
        "difficulty": "medium",
        "url": "https://theoryofmaterials.com/wp-content/uploads/2025/05/AndrewBurgessPoster_DFT2024-scaled.jpg",
        "filename": "dft_poster.jpg",
        "question": "What is the 'flat plane condition' and what problem do approximate exchange-correlation functionals face?",
        "expected_keywords": [
            "flat plane",
            "linearity",
            "delocalisation",
            "self-interaction",
            "exchange-correlation",
        ],
    },
    # {
    #     "name": "[GraphRAG] Energy Policy Chapter E2E Test",
    #     "difficulty": "medium",
    #     "url": "https://personal.ems.psu.edu/~radovic/Chapter1.pdf",
    #     "filename": "energy_policy_chapter1.pdf",
    #     "question": "According to the text, what is the author's main argument regarding energy policy, and what key factors does the author say are involved?",
    #     "expected_concepts": {
    #         "main_argument": ["energy policy", "national priority", "energy crises", "informed stand"],
    #         "key_factors": ["environment", "pollution", "economic", "politics", "tradeoffs"]
    #     }
    # },
    # {
    #     "name": "[GraphRAG] Attention Paper E2E Test",
    #     "difficulty": "hard",
    #     "url": "https://arxiv.org/pdf/1706.03762",
    #     "filename": "attention_is_all_you_need.pdf",
    #     "question": "What is the relationship between the Transformer architecture and Recurrent Neural Networks (RNNs)?",
    #     "expected_keywords": ["transformer", "recurrent neural networks", "rnn", "self-attention", "replaces", "avoids", "sequential"]
    # },
]

# ==========================================================
# === HELPER FUNCTIONS =====================================
# ==========================================================


async def download_file(url: str, dest_path: Path):
    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
            dest_path.write_bytes(response.content)
            logger.success(f"Successfully downloaded '{url}' to '{dest_path}'")
            return dest_path
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to download {url}. Status: {e.response.status_code}")
            return None


def assert_contains_keywords(answer: str, keywords: List[str], required_percentage: float = 0.6):
    answer_lower = answer.lower()
    found_keywords = [kw for kw in keywords if kw.lower() in answer_lower]
    required_count = int(len(keywords) * required_percentage)

    assert len(found_keywords) >= required_count, (
        f"Assertion Failed: Expected at least {required_count} keywords, but only found {len(found_keywords)}: {found_keywords}"
    )
    logger.success(f"Assertion Passed: Found {len(found_keywords)}/{len(keywords)} keywords.")


def assert_concepts_covered(answer: str, concepts: Dict[str, List[str]]):
    """
    A more robust assertion that checks for concepts rather than exact keywords.
    It verifies that at least one keyword from EACH concept group is present in the answer.
    """
    answer_lower = answer.lower()
    missing_concepts = []
    for concept, keywords in concepts.items():
        if not any(keyword.lower() in answer_lower for keyword in keywords):
            missing_concepts.append(concept)

    assert not missing_concepts, (
        f"Assertion Failed: The answer did not cover the following required concepts: {missing_concepts}"
    )
    logger.success("Assertion Passed: All required concepts were covered in the answer.")


async def global_setup_and_cleanup():
    """
    A robust function to run before the test suite to ensure a clean state.
    It aggressively deletes any leftover data from previous runs.
    """
    logger.info("--- Performing Global E2E Cleanup to ensure a clean state ---")
    settings = SettingsProvider.sget()

    # Cleanup Weaviate
    weaviate_client = WeaviateVectorDBClient(
        url=settings.WEAVIATE_URL, api_key=settings.WEAVIATE_API_KEY, skip_init_checks=True
    )
    try:
        # We ensure the collection AND the tenant are gone.
        # First, try to remove the tenant if the collection exists.
        if await asyncio.to_thread(
            weaviate_client.client.collections.exists, FIXED_COLLECTION_NAME
        ):
            collection = weaviate_client.client.collections.get(FIXED_COLLECTION_NAME)
            if await asyncio.to_thread(collection.tenants.exists, FIXED_USER_ID):
                await asyncio.to_thread(collection.tenants.remove, [FIXED_USER_ID])
                logger.info(
                    f"Removed tenant '{FIXED_USER_ID}' from collection '{FIXED_COLLECTION_NAME}'."
                )
        # For maximum cleanliness, we could also delete and recreate the collection.
        # await weaviate_client.delete_class(FIXED_COLLECTION_NAME)

    except Exception as e:
        logger.warning(
            f"Could not perform Weaviate cleanup (this is okay if it's the first run): {e}"
        )
    finally:
        await weaviate_client.close()

    # Cleanup Neo4j
    try:
        neo4j_client = await Neo4jManager.get_client()
        await asyncio.to_thread(neo4j_client.delete_all_tenant_data, tenant_id=FIXED_USER_ID)
        logger.info(f"Cleaned up Neo4j data for tenant '{FIXED_USER_ID}'.")
    except Exception as e:
        logger.warning(f"Could not perform Neo4j cleanup: {e}")


# ==========================================================
# === MAIN TEST EXECUTION LOGIC ============================
# ==========================================================


async def run_single_e2e_test(test_case: Dict[str, Any]) -> Dict[str, Any]:
    test_name = test_case["name"]
    logger.info(f"--- Starting Test Case: {test_name} ---")

    # Use the fixed, predictable user and collection names
    ctx = Context()
    user = UserORM(id=FIXED_USER_ID)
    collection_name = FIXED_COLLECTION_NAME
    temp_dir = Path(tempfile.mkdtemp())
    s3_client = None

    try:
        # --- ARRANGE ---
        settings = SettingsProvider.sget()
        s3_client = get_aws_s3(
            settings.AWS_S3_BUCKET_REGION,
            settings.AWS_ACCESS_KEY_ID,
            settings.AWS_SECRET_ACCESS_KEY,
            settings.AWS_S3_PUBLIC_BUCKET_NAME,
            object_prefix=f"e2e-action-testing/{user.id}/",
        )
        local_file_path = await download_file(test_case["url"], temp_dir / test_case["filename"])

        with open(local_file_path, "rb") as f:
            s3_url = str(await s3_client.upload_fileobj(ctx, f, local_file_path.name))

        # Create mock token and context once for the whole test
        expire = datetime.utcnow() + timedelta(minutes=5)
        to_encode = {"sub": str(user.id), "exp": int(expire.timestamp())}
        mock_token = jwt.encode(
            to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
        )
        user.access_token = mock_token
        ctx = Context(user_id=str(user.id))
        db_conn = next(get_mongo_db())

        # --- STEPS 1, 2, 3: EXECUTE INGEST-AND-QUERY INTENT ---
        # This single call now tests the entire fixed workflow:
        # 1. Action sends HTTP request to queue the job.
        # 2. Action polls for PROCESSED state.
        # 3. Action runs its own readiness_probe.
        # 4. Action sends HTTP request to perform the search.
        logger.info(f"Executing 'Ingest-and-Query' intent for file '{s3_url}'...")
        action_config = GraphRAGQueryConfig(
            collection_name=collection_name, query=test_case["question"]
        )
        action_instance = ActionGraphRAGQuery(action_config=action_config, user=user, db=db_conn)
        action_input = DataBlock(urls=[s3_url], text=test_case["question"])

        query_result = await action_instance.run_action(ctx=ctx, action_input=action_input)

        # --- ASSERT ---
        assert isinstance(query_result, DataBlocks), f"Query action failed: {query_result}"
        final_answer = query_result.blocks[0].text.lower()

        # Add a stronger assertion to ensure we didn't get an empty RAG response
        assert "couldn't find" not in final_answer, (
            "The RAG query returned an empty result, indicating data was not found."
        )

        logger.info(f"Question: {test_case['question']}")
        logger.info(f"Final Answer:\n{final_answer}")

        if "expected_concepts" in test_case:
            assert_concepts_covered(final_answer, test_case["expected_concepts"])

        return {"name": test_name, "status": "PASSED", "reason": None}

    except (AssertionError, Exception) as e:
        logger.error(f"Test case '{test_name}' FAILED.")
        logger.exception(e)
        return {"name": test_name, "status": "FAILED", "reason": str(e)}

    finally:
        # --- CLEANUP ---
        logger.info("Cleaning up test resources...")
        settings = SettingsProvider.sget()

        # Cleanup S3 (delete the entire prefix for this test run)
        if s3_client:
            await s3_client.delete_prefix(s3_client.object_prefix)

        # The main cleanup is now handled by the global function at the start/end of the suite.
        # We only need to clean up the local temp directory here.

        # Cleanup local files
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
            logger.info(f"Removed temporary directory: {temp_dir}")

    logger.success(f"--- Test Case Finished: {test_name} ---")


async def main():
    setup_logger()
    SettingsProvider.set_env_file(".env.local")
    SettingsProvider.set()
    settings = SettingsProvider.sget()

    # Runtime check for required secrets
    if not all(
        [
            settings.NEO4J_URI,
            settings.NEO4J_USERNAME,
            settings.NEO4J_PASSWORD,
            settings.WEAVIATE_URL,
            settings.OPENAI_API_KEY,
        ]
    ):
        logger.error(
            "Required secrets (NEO4J_*, WEAVIATE_URL, OPENAI_API_KEY) are not set. Aborting E2E test."
        )
        sys.exit(1)

    logger.info("Starting E2E GraphRAG Action Test Runner...")

    # Run cleanup before the tests start to ensure a clean slate
    await global_setup_and_cleanup()

    try:
        results = [await run_single_e2e_test(tc) for tc in TEST_CASES]
    finally:
        # Also run cleanup at the end to leave the system clean for manual testing
        await global_setup_and_cleanup()
        Neo4jManager.shutdown()  # Shutdown the client once at the very end
        close_mongo_client()  # Clean up the MongoDB client

    logger.info("--- E2E Test Summary ---")
    passed_count = sum(1 for res in results if res["status"] == "PASSED")
    for res in results:
        status = res["status"]
        log_func = logger.success if status == "PASSED" else logger.error
        log_func(
            f"[{status}] {res['name']}"
            + (f" - Reason: {res['reason']}" if status == "FAILED" else "")
        )

    logger.info(f"--- Summary: {passed_count} / {len(results)} tests passed. ---")

    if passed_count != len(results):
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
