# tests/ingestion/scripts/e2e_sandbox_query_runner.py

import asyncio
import shutil
import sys
import tempfile
import uuid
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
from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_multimodal.action_multimodal_sandbox_query import (
    SandboxQueryConfig,
)
from src.autobots.conn.aws.aws_s3 import get_aws_s3
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.logging.setup_logger import setup_logger
from src.autobots.core.settings import SettingsProvider
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.ingestion.neo4j_loader import Neo4jManager
from src.autobots.user.user_orm_model import UserORM

# ==========================================================
# === TEST CONFIGURATION & ASSETS ==========================
# ==========================================================

TEST_CASES = [
    # {
    #     "name": "[Sandbox] Standard Digital PDF (Attention Paper)",
    #     "urls": ["https://arxiv.org/pdf/1706.03762"],
    #     "filename": "attention_is_all_you_need.pdf",
    #     "question": "What is the primary innovation of the Transformer architecture described in the paper?",
    #     "expected_keywords": ["transformer", "self-attention", "recurrence", "convolutions", "encoder-decoder"]
    # },
    # {
    #     "name": "[Sandbox] Scanned PDF that needs OCR (Quantum)",
    #     "urls": ["https://static.aminer.org/pdf/PDF/000/120/546/quantum_cryptography_or_unforgeable_subway_tokens.pdf"],
    #     "filename": "quantum_cryptography.pdf",
    #     "question": "Based on the scanned document, what is the concept of 'quantum subway tokens' and how do they relate to unforgeability?",
    #     "expected_keywords": ["quantum", "cryptography", "unforgeable", "tokens", "physical", "security", "no-cloning"]
    # },
    # {
    #     "name": "[Ephemeral] DOCX File Query (Visitor Use Management)",
    #     "urls": ["http://www.umt.edu/media/wilderness/NWPS/documents/courses/virtual-notebook-sunbelt/IVUMC%20JPRA%20Research%20Note-01March2021-cc.docx"], # New DOCX URL
    #     "filename": "visitor_use_management.docx",
    #     "question": "According to the document, how can public health, public engagement, and Justice, Equity, Diversity, and Inclusion (JEDI) be integrated into the Visitor Use Management Framework?", # New question
    #     "expected_keywords": ["Visitor Use Management Framework", "public health", "public engagement", "JEDI", "equity", "inclusion", "sustainable recreation", "IVUMC"] # New keywords
    # },
    {
        "name": "[Sandbox] Image with Text that needs OCR (Poster image)",
        "urls": [
            "https://theoryofmaterials.com/wp-content/uploads/2025/05/AndrewBurgessPoster_DFT2024-scaled.jpg"
        ],
        "filename": "dft_poster.jpg",
        "question": "What is the 'flat plane condition' and what problem do approximate exchange-correlation functionals face?",
        "expected_keywords": [
            "flat plane",
            "exchange-correlation",
            "delocalisation",
            "linearity",
            "many-body",
            "self-interaction",
        ],
    },
    # {
    #     "name": "[Sandbox] Poster in Digital PDF Format (Quantum Walks)",
    #     "urls": ["https://static.rainfocus.com/nvidia/gtcs25/sess/1727840503940001SVD7/poster/P72789-Adaptive%20Distribution%20Generator-Powering%20Split-Step%20Quantum%20Walks%20with%20CUDA-Q_1742360590370001iKPw.pdf"],
    #     "filename": "quantum_walks_poster.pdf",
    #     "question": "What is the purpose of the 'Classical Optimizer' in the Parameterized SSQW layer diagram?",
    #     "expected_keywords": ["classical", "optimizer", "update", "parameters", "loss", "function", "feedback"]
    # },
    # {
    #     "name": "[Sandbox] Audio Transcription Query (MP3)",
    #     "urls": ["https://www.nasa.gov/wp-content/uploads/2015/01/586447main_JFKwechoosemoonspeech.mp3"],
    #     "filename": "jfk_moon_speech.mp3",
    #     "question": "What are the main reasons President Kennedy gives for choosing to go to the moon?",
    #     "expected_keywords": ["choose", "moon", "decade", "easy", "hard", "challenge", "peace"]
    # },
    # {
    #     "name": "[Sandbox] Multi-File Ingestion and Query",
    #     "urls": [
    #         "https://arxiv.org/pdf/1706.03762",
    #         "https://arxiv.org/pdf/2406.04284"
    #     ],
    #     "filenames": ["attention_is_all_you_need.pdf", "agentic_systems.pdf"],
    #     "question": "Compare the core concepts of the 'Transformer' architecture with the definition of a 'Vertical AI Agent'.",
    #     "expected_keywords": ["transformer", "self-attention", "vertical ai", "saas", "domain-specific", "agent"]
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


def assert_contains_keywords(answer: str, keywords: List[str], required_percentage: float = 0.66):
    answer_lower = answer.lower()
    found_keywords = [kw for kw in keywords if kw.lower() in answer_lower]
    required_count = int(len(keywords) * required_percentage)

    assert len(found_keywords) >= required_count, (
        f"Assertion Failed: Expected at least {required_count} keywords from {keywords}, but only found {len(found_keywords)}: {found_keywords}"
    )
    logger.success(f"Assertion Passed: Found {len(found_keywords)}/{len(keywords)} keywords.")


# ==========================================================
# === MAIN TEST EXECUTION LOGIC ============================
# ==========================================================


async def run_single_e2e_test(test_case: Dict[str, Any]) -> Dict[str, Any]:
    logger.info(f"--- Starting Test Case: {test_case['name']} ---")

    ctx = Context()
    user = UserORM(id=str(uuid.uuid4()))
    settings = SettingsProvider.sget()

    s3_client = get_aws_s3(
        settings.AWS_S3_BUCKET_REGION,
        settings.AWS_ACCESS_KEY_ID,
        settings.AWS_SECRET_ACCESS_KEY,
        settings.AWS_S3_PUBLIC_BUCKET_NAME,
        object_prefix=f"files/{user.id}/",
    )
    temp_dir = tempfile.mkdtemp()
    s3_urls = []
    final_answer = "Test did not produce an answer."

    try:
        # --- ARRANGE ---
        # 1. Prepare file lists for download and upload
        urls_to_download = test_case["urls"]
        filenames_for_s3 = test_case.get("filenames") or [test_case.get("filename")]
        if len(urls_to_download) != len(filenames_for_s3):
            raise ValueError(
                "The number of URLs must match the number of filenames in the test case."
            )

        # 2. Download all assets
        download_tasks = [
            download_file(url, Path(temp_dir) / filename)
            for url, filename in zip(urls_to_download, filenames_for_s3)
        ]
        local_file_paths = await asyncio.gather(*download_tasks)

        # 3. Upload all assets to S3
        for file_path in local_file_paths:
            if file_path:
                with open(file_path, "rb") as f:
                    s3_url_obj = await s3_client.upload_fileobj(ctx, f, file_path.name)
                    s3_urls.append(str(s3_url_obj))

        if len(s3_urls) != len(urls_to_download):
            raise RuntimeError("Failed to download or upload one or more test assets.")

        # 4. Prepare the action using the new SandboxQueryConfig
        # Instantiate the config by providing all required fields.
        # The query comes from the test case, the URLs come from the S3 upload,
        # (Note: SandboxQueryConfig no longer supports a 'provider' field; provider selection is handled server-side.)
        action_config = SandboxQueryConfig(query=test_case["question"], file_urls=s3_urls)

        from src.autobots.action.action_type.action_types import ActionType

        action_doc = ActionDoc(
            id=str(uuid.uuid4()),
            type=ActionType.multimodal_sandbox_query,  # Use the new action type
            config=action_config.model_dump(),
            user_id=str(user.id),
            name=test_case["name"],
        )

        # 5. Create a mock JWT token and context
        expire = datetime.utcnow() + timedelta(minutes=5)
        to_encode = {"sub": str(user.id), "exp": int(expire.timestamp())}
        mock_token = jwt.encode(
            to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM
        )
        user.access_token = mock_token
        ctx = Context(user_id=str(user.id))
        db = next(get_mongo_db())

        # --- ACT ---
        action_input = DataBlock(text=test_case["question"], urls=s3_urls)
        logger.info("Executing action in sandbox mode...")
        result = await ActionFactory.run_action(
            ctx=ctx, app_state=AppState(),
            action_doc=action_doc,
            action_input_dict=action_input.model_dump(),
            user=user,
            db=db,
        )

        # --- ASSERT ---
        # The result from ActionFactory is a RunActionObj. Check its status first.
        assert result.status == "success", f"Action failed with status: {result.status}"
        # The actual output we need to check is inside the output_dict.
        final_answer = result.output_dict["blocks"][0]["text"]
        assert_contains_keywords(final_answer, test_case["expected_keywords"])

        logger.info(f"Question: {test_case['question']}")
        logger.info(f"Final Answer:\n{final_answer}")

        return {"name": test_case["name"], "status": "PASSED", "reason": None}

    except (AssertionError, Exception) as e:
        logger.error(f"Test case '{test_case['name']}' FAILED.")
        logger.exception(e)
        return {"name": test_case["name"], "status": "FAILED", "reason": str(e)}

    finally:
        # --- CLEANUP ---
        # The sandbox endpoint on the worker cleans up its own Weaviate resources.
        # We only need to clean up the S3 file and local temp directory.
        logger.info("Cleaning up test resources...")
        if s3_urls:
            delete_s3_tasks = [s3_client.delete_https_url(url) for url in s3_urls]
            await asyncio.gather(*delete_s3_tasks)
            logger.info(f"Deleted {len(s3_urls)} S3 object(s).")

        if Path(temp_dir).exists():
            shutil.rmtree(temp_dir)
            logger.info(f"Removed temporary directory: {temp_dir}")
        # Ensure Neo4j client is always shut down at the end of a test run
        Neo4jManager.shutdown()

    logger.success(f"--- Test Case Finished: {test_case['name']} ---")


async def main():
    setup_logger()
    SettingsProvider.set_env_file(".env.local")
    SettingsProvider.set()

    logger.info("Starting E2E Sandbox Query Test Runner...")

    results = []
    for test_case in TEST_CASES:
        result = await run_single_e2e_test(test_case)
        results.append(result)
        await asyncio.sleep(1)  # Small delay between tests if needed

    logger.info("--- E2E Test Summary ---")
    passed_count = 0
    for res in results:
        if res["status"] == "PASSED":
            logger.success(f"[PASSED] {res['name']}")
            passed_count += 1
        else:
            logger.error(f"[FAILED] {res['name']} - Reason: {res['reason']}")

    logger.info(f"--- Summary: {passed_count} / {len(results)} tests passed. ---")

    if passed_count != len(results):
        logger.error("One or more E2E tests failed.")
        sys.exit(1)  # Exit with a non-zero code to indicate failure for CI/CD systems
    else:
        logger.success("All E2E tests passed successfully!")


if __name__ == "__main__":
    asyncio.run(main())
