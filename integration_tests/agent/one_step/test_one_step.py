import pytest

from src.autobots.agent.one_step import AgentData, OneStepAgent
from src.autobots.data_model.context import Context


# TODO: Make Selenium work, currently unable to install chromedriver in image
@pytest.mark.skip(reason="One step agent stalling")
@pytest.mark.asyncio
async def test_one_step_happy_path_1(ctx, db, user, set_test_settings):
    ctx = Context()
    agent_data = AgentData(
        goal="Act as an effective google search advert maker. Create advert for Nike shoes"
    )
    await OneStepAgent().run(ctx=ctx, db=db, user=user, agent_data=agent_data)
    print(f"test_one_step_happy_path_1: {agent_data.context[-1]['content']}")
    assert True
