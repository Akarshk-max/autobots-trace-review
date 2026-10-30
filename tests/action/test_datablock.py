import pytest

from src.autobots.action.action.common_action_models import DataBlock, Metadata
from src.autobots.data_model.context import Context


@pytest.mark.asyncio
async def test_datablock_happy_path_1(set_test_settings):
    d = DataBlock(text="Search latest news", urls=["https://www.google.com"])
    prompt = d.get_llm_prompt(ctx=Context())
    assert prompt == "Search latest news\nUser-uploaded file URLs: ['https://www.google.com']"


@pytest.mark.asyncio
async def test_datablock_happy_path_2(set_test_settings):
    d = DataBlock(text="Search latest news")
    prompt = d.get_llm_prompt(ctx=Context())
    assert prompt == "Search latest news"


@pytest.mark.asyncio
async def test_datablock_happy_path_3(set_test_settings):
    text = "how can i assist you today?"
    d = DataBlock(text=text)
    assert text == d.text


@pytest.mark.asyncio
async def test_datablock_happy_path_4(set_test_settings):
    # Test URL extraction from markdown link text exactly as provided
    text = (
        "Today’s date/time (America/Los_Angeles): *Wednesday, 21-Jan-2026 10:31:59 PST*.\n\n"
        "I ran Salesforce report *00OEY000000MHik2AG* via the Analytics API and pulled *all non‑TOS opportunities* "
        "in the report’s *last full prior week (PST): 1/11/2026–1/17/2026, then retrieved the required "
        "Opportunity/Account/Division fields from Salesforce and produced a **single-sheet spreadsheet* with columns "
        "in the exact order requested (blank where Salesforce did not provide a value; Spotify Artist ID only when "
        "the link is an artist URL).\n\n"
        "[Download the spreadsheet](https://app-myautobots-public-dev.s3.amazonaws.com/files/"
        "a3b34145-7ef3-440c-9388-48e067f62036/86cb09d9-4659-475a-94e2-82c6641f69fd-last_week_closed_won_deals_pst.xlsx)"
    )

    expected_url = (
        "https://app-myautobots-public-dev.s3.amazonaws.com/files/"
        "a3b34145-7ef3-440c-9388-48e067f62036/86cb09d9-4659-475a-94e2-82c6641f69fd-last_week_closed_won_deals_pst.xlsx"
    )

    d = DataBlock(text=text, metadata=Metadata(process_url_in_text=True))
    d.get_llm_prompt(ctx=Context())

    assert len(d.urls) == 1
    assert d.urls[0] == expected_url
