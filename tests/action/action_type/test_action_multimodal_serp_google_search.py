import pytest

from src.autobots.action.action.common_action_models import DataBlock
from src.autobots.action.action_type.action_multimodal.action_multimodal_google_search.action_multimodal_google_search_model import (
    ActionMultimodalGoogleSearchConfig,
)
from src.autobots.action.action_type.action_multimodal.action_multimodal_google_search.action_multimodal_serp_google_search import (
    ActionMultimodalSerpGoogleSearch,
)
from src.autobots.data_model.context import Context
from src.autobots.data_model.app_state import AppState


@pytest.mark.asyncio
@pytest.mark.skip(reason="Google Serp Search API is not working")
async def test_action_multimodal_serp_google_search_happy_path(set_test_settings):
    app_state = AppState()
    config = ActionMultimodalGoogleSearchConfig(
        q="Search coffee shops in ",
    )
    input = DataBlock(text="San Francisco")
    resp = await ActionMultimodalSerpGoogleSearch(action_config=config).run_action(Context(), app_state, input)
    assert resp and len(resp.organic_results) > 0
