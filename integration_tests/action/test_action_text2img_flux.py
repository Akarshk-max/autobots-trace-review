import pytest

from src.autobots.action.action.common_action_models import TextObj
from src.autobots.action.action_type.action_text2img.action_text2img_flux import ActionGenImageFlux
from src.autobots.conn.flux.flux_images.image_model import FluxImageReq, FluxImagesResponse
from src.autobots.data_model.context import Context


@pytest.mark.skip("Expense related")
@pytest.mark.asyncio
async def test_action_gen_image_flux():
    action_config = FluxImageReq(prompt="A beautiful landscape", model_name="flux-dev")
    action = ActionGenImageFlux(action_config=action_config)
    context = Context()
    input_text = TextObj(text="with mountains")

    result = await action.run_action(context, input_text)

    assert isinstance(result, FluxImagesResponse)
    assert len(result.images) > 0
    assert result.prompt == "A beautiful landscape with mountains"
