# tests/conn/unstructured/test_unstructured_io_remote.py


import pytest

# Add these imports
from src.autobots.conn.unstructured_io.unstructured_io import (
    PartitionParametersParams,
    get_unstructured_io,
)
from src.autobots.core.settings import SettingsProvider

TEST_REMOTE_PDF_URL = "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"


@pytest.mark.asyncio
async def test_partition_from_remote_url_happy_path(ctx, db, user, set_test_settings):
    """Tests the successful partitioning of a file from a valid remote URL."""
    settings = SettingsProvider.sget()
    # ctx = Context()
    # user_id = uuid.UUID("4d5d5063-36fb-422e-a811-cac8c2003d37")
    # user = UserORM(id=str(user_id))
    unstructured_client = get_unstructured_io(
        ctx, user, settings.UNSTRUCTURED_API_KEY, settings.UNSTRUCTURED_URL
    )

    partition_params = PartitionParametersParams(
        strategy="hi_res",
        pdf_infer_table_structure=True,
        languages=["eng"],
    )

    elements = await unstructured_client.partition_from_remote_url(
        remote_url=TEST_REMOTE_PDF_URL, partition_parameters=partition_params
    )

    assert elements is not None
    assert isinstance(elements, list)
    assert len(elements) > 0

    found_text = False
    for element in elements:
        if "Dummy PDF file" in element.get("text", ""):
            found_text = True
            break

    assert found_text, (
        "The expected text 'Dummy PDF file' was not found in the partitioned elements."
    )


@pytest.mark.asyncio
async def test_partition_from_remote_url_invalid_url_with_limited_retries(
    ctx, db, user, set_test_settings, monkeypatch
):
    """
    Tests the failure case of an invalid URL with a live connection,
    but speeds up the test by reducing the number of retries.
    """
    settings = SettingsProvider.sget()
    # ctx = Context()
    # user_id = uuid.UUID("4d5d5063-36fb-422e-a811-cac8c2003d37")
    # user = UserORM(id=str(user_id))
    unstructured_client = get_unstructured_io(
        ctx, user, settings.UNSTRUCTURED_API_KEY, settings.UNSTRUCTURED_URL
    )

    partition_params = PartitionParametersParams(strategy="fast")

    # We patch the max_attempts variable inside the _stream_download_file method to a small number.
    # We patch it on the object instance to avoid affecting other tests.
    monkeypatch.setattr(
        unstructured_client,
        "_stream_download_file",
        # Use a lambda to wrap the original method but call it with a modified kwarg
        lambda *args, **kwargs: unstructured_client.__class__._stream_download_file(
            unstructured_client, *args, **kwargs, max_attempts=2
        ),
    )

    # ACT
    result = await unstructured_client.partition_from_remote_url(
        remote_url="https://example.com/this/path/does/not/exist.pdf",
        partition_parameters=partition_params,  # Pass the Pydantic model instance
    )

    # ASSERT: The API will try to fetch this invalid URL and should return an error.
    # Our code will catch the error and return None after 2 retries.
    assert result is None
