"""
Tests for read_files_from_user_system tool.

Unit tests run without any external dependencies.
Integration tests require:
  1. Server running locally (http://localhost:8000)
  2. CLI agent connected (cli/autobots_cli.py or cli/autobots_cli_login.py)

Run unit tests:
    poetry run pytest tests/llm/tools/test_read_files_tool.py -v -k "not integration"

Run all (including integration):
    poetry run pytest tests/llm/tools/test_read_files_tool.py -v -s --timeout=300 -m "integration or not integration"
"""


import httpx
import pytest
import pytest_asyncio

from src.autobots.data_model.context import Context
from src.autobots.llm.tools.read_files_tool import (
    ReadFilesConfig,
    ReadFilesOutput,
    read_files_from_user_system,
)

LOCAL_SERVER = "http://localhost:8000"


# ---------------------------------------------------------------------------
# Helper: discover the connected user_id from the running server
# ---------------------------------------------------------------------------
@pytest.mark.skip("For now")
async def get_connected_user_id() -> str | None:
    """Hit /v1/terminal/status on the local server to find a connected user."""
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{LOCAL_SERVER}/v1/terminal/status")
            if resp.status_code == 200:
                users = resp.json().get("connected_users", [])
                if users:
                    return users[0]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
# @pytest.mark.skip("For now")
@pytest_asyncio.fixture
async def connected_user_id():
    """Get the user_id of the CLI agent connected to the local server.
    Skips the test if no agent is connected or server is not running."""
    uid = await get_connected_user_id()
    if uid is None:
        pytest.skip(
            "No CLI agent connected. Start the server and run "
            "'python cli/autobots_cli.py connect --token <token> --server local'"
        )
    return uid

# @pytest.mark.skip("For now")
@pytest.fixture
def test_file(tmp_path):
    """Create a temporary test file for upload tests."""
    f = tmp_path / "test_upload.txt"
    f.write_text("This is test content for upload.")
    return str(f)


# ===========================================================================
# 1. UNIT TESTS (no server needed — just config/model validation)
# ===========================================================================
@pytest.mark.skip("For now")
def test_config_defaults():
    """ReadFilesConfig has correct defaults."""
    config = ReadFilesConfig(paths=["/a.txt"])
    assert config.timeout_sec == 300
    assert config.paths == ["/a.txt"]

@pytest.mark.skip("For now")
def test_config_custom_timeout():
    config = ReadFilesConfig(paths=["/a.txt", "/b.txt"], timeout_sec=60)
    assert config.timeout_sec == 60
    assert len(config.paths) == 2

@pytest.mark.skip("For now")
def test_output_model():
    out = ReadFilesOutput(urls=["https://example.com/f.pdf"], errors=[], success=True)
    assert out.success is True
    assert len(out.urls) == 1

    out2 = ReadFilesOutput(urls=[], errors=["not found"], success=False)
    assert out2.success is False

@pytest.mark.skip("For now")
def test_output_model_defaults_are_independent():
    """Verify mutable default lists are not shared between instances."""
    out1 = ReadFilesOutput()
    out2 = ReadFilesOutput()
    out1.urls.append("url1")
    assert len(out2.urls) == 0


# ===========================================================================
# 2. INTEGRATION TESTS (server + CLI agent must be running locally)
# ===========================================================================
@pytest.mark.skip("For now")
@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_single_file(set_test_settings, connected_user_id, test_file):
    """Upload a single real file and verify we get back an S3 URL."""
    ctx = Context(user_id=connected_user_id)
    config = ReadFilesConfig(
        paths=[test_file],
        timeout_sec=120,
    )

    result = await read_files_from_user_system(config=config, ctx=ctx)

    assert isinstance(result, ReadFilesOutput)
    print(f"\n[single_file] success={result.success}")
    print(f"[single_file] urls: {result.urls}")
    print(f"[single_file] errors: {result.errors}")

    assert result.success is True, f"Failed: {result.errors}"
    assert len(result.urls) == 1
    assert result.urls[0].startswith("http")
    assert len(result.errors) == 0

@pytest.mark.skip("For now")
@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_nonexistent_file(set_test_settings, connected_user_id):
    """Requesting a file that doesn't exist returns an error, not a crash."""
    ctx = Context(user_id=connected_user_id)
    config = ReadFilesConfig(
        paths=["/nonexistent/path/ghost_file.pdf"],
        timeout_sec=30,
    )

    result = await read_files_from_user_system(config=config, ctx=ctx)

    assert isinstance(result, ReadFilesOutput)
    print(f"\n[not_found] success={result.success}")
    print(f"[not_found] errors: {result.errors}")

    assert result.success is False
    assert len(result.errors) >= 1
    assert "not found" in result.errors[0].lower()

@pytest.mark.skip("For now")
@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_mixed_valid_and_invalid(set_test_settings, connected_user_id, test_file):
    """One valid file + one missing path -> partial success."""
    ctx = Context(user_id=connected_user_id)
    config = ReadFilesConfig(
        paths=[test_file, "/nonexistent/missing.txt"],
        timeout_sec=120,
    )

    result = await read_files_from_user_system(config=config, ctx=ctx)

    assert isinstance(result, ReadFilesOutput)
    print(f"\n[mixed] success={result.success}")
    print(f"[mixed] urls: {result.urls}")
    print(f"[mixed] errors: {result.errors}")

    assert result.success is True
    assert len(result.urls) == 1
    assert len(result.errors) == 1
    assert "not found" in result.errors[0].lower()

@pytest.mark.skip("For now")
@pytest.mark.integration
@pytest.mark.asyncio
async def test_read_folder_zips_and_uploads(set_test_settings, connected_user_id, tmp_path):
    """Reading a directory should zip it and upload the .zip."""
    # Create a small test directory with files
    (tmp_path / "file1.txt").write_text("content1")
    (tmp_path / "file2.txt").write_text("content2")
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "file3.txt").write_text("content3")

    ctx = Context(user_id=connected_user_id)
    config = ReadFilesConfig(
        paths=[str(tmp_path)],
        timeout_sec=120,
    )

    result = await read_files_from_user_system(config=config, ctx=ctx)

    assert isinstance(result, ReadFilesOutput)
    print(f"\n[folder] success={result.success}")
    print(f"[folder] urls: {result.urls}")
    print(f"[folder] errors: {result.errors}")

    assert result.success is True, f"Failed: {result.errors}"
    assert len(result.urls) == 1
    assert result.urls[0].endswith(".zip") or ".zip" in result.urls[0]

@pytest.mark.skip("For now")
@pytest.mark.asyncio
async def test_no_cli_agent_returns_error(set_test_settings):
    """When no CLI agent is connected for a user, returns a clear error."""
    ctx = Context(user_id="definitely_not_connected_user_12345")
    config = ReadFilesConfig(paths=["/any/path.txt"])

    result = await read_files_from_user_system(config=config, ctx=ctx)

    assert isinstance(result, ReadFilesOutput)
    print(f"\n[no_agent] success={result.success}")
    print(f"[no_agent] errors: {result.errors}")

    assert result.success is False
    assert any("not connected" in e.lower() or "no terminal" in e.lower() for e in result.errors)
