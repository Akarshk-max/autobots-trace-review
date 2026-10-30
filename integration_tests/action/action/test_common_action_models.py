import pytest

from src.autobots.action.action.common_action_models import DataBlock


@pytest.mark.asyncio
async def test_datablock_process_url_in_text_happy_path(ctx, db, user, set_test_settings):
    file_url = "https://app-myautobots-public-dev.s3.amazonaws.com/files/a3b34145-7ef3-440c-9388-48e067f62036/Ech_-_broke_records_(offer)_PE_[encrypted]__(1).pdf"
    datablock = DataBlock(text=f"file url: {file_url} hello world .pdf")
    datablock.process_url_in_text(ctx)
    assert len(datablock.urls) == 1
    assert datablock.urls[0] == file_url
