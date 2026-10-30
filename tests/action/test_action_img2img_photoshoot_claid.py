# import pytest
#
# from src.autobots.action.action.action_doc_model import ActionDoc
# from src.autobots.action.action_type.action_factory import (ActionFactory,
#                                                             RunActionObj)
# from src.autobots.action.action_type.action_img2img.action_img2img_photoshoot_claid import (
#     ActionConfigPhotoshootClaid, ActionImg2ImgPhotoshootClaid,
#     ActionInputPhotoshootClaid)
# from src.autobots.conn.claid.claid_model import (PhotoshootObject,
#                                                  PhotoshootScene)
# from src.autobots.core.database.mongo_base import get_mongo_db
# from src.autobots.core.utils import gen_uuid
# from src.autobots.data_model.context import Context
# from src.autobots.user.user_orm_model import UserORM
#
#
# @pytest.mark.asyncio
# @pytest.mark.skip(reason="Below test takes care of this test case")
# async def test_action_img2img_photoshoot_claid_happy_path(set_test_settings):
#     ctx = Context()
#     action_config = ActionConfigPhotoshootClaid()
#     action = ActionImg2ImgPhotoshootClaid(action_config)
#     action_input = ActionInputPhotoshootClaid(
#         object=PhotoshootObject(
#             image_url="https://upload.wikimedia.org/wikipedia/en/9/9c/Archie_1942_issue_1.jpg",
#         ),
#         scene=PhotoshootScene(prompt="Make it Black and White"),
#     )
#     action_output = await action.run_action(ctx, app_state, action_input)
#     assert len(action_output.data) == 1
#
#
# @pytest.mark.asyncio
# @pytest.mark.skip(reason="Expensive to test everytime")
# async def test_action_img2img_photoshoot_claid_happy_path_1():
#     db = next(get_mongo_db())
#     action_config = ActionConfigPhotoshootClaid()
#     action_input = ActionInputPhotoshootClaid(
#         object=PhotoshootObject(
#             image_url="https://upload.wikimedia.org/wikipedia/en/9/9c/Archie_1942_issue_1.jpg",
#         ),
#         scene=PhotoshootScene(prompt="Make it Black and White"),
#     )
#     action_doc = ActionDoc(
#         id="1",
#         user_id="user_1",
#         name="Action_for_celery",
#         type=ActionImg2ImgPhotoshootClaid.type,
#         config=action_config.model_dump(exclude_none=True),
#     )
#     user = UserORM(id=str(gen_uuid()))
#     run_action_object: RunActionObj = await ActionFactory.run_action(
#         Context(), action_doc, action_input.model_dump(exclude_none=True), user, db=db
#     )
#     action_output = ActionImg2ImgPhotoshootClaid.get_output_type().model_validate(
#         run_action_object.output_dict
#     )
#     assert len(action_output.data) == 1
