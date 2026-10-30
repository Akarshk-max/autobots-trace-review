import pytest

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_audio2text.action_audio2text_transcription_assemblyai import (
    ActionAudio2TextTranscriptionAssemblyai,
)
from src.autobots.action.action_type.action_factory import ActionFactory, RunActionObj
from src.autobots.conn.assembly.assemblyai import TranscriptionReq
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.skip("Not successful")
@pytest.mark.asyncio
async def test_action_audio2text_transcription_assemblyai_happy_path(set_test_settings):
    config = TranscriptionReq()
    app_state = AppState()
    input = DataBlock(
        urls=[
            "https://meetkiwiinc.zendesk.com/attachments/token/DV6HIgMxsVG8rINvioXo6u0ZW/?name=recording+316829.mp3"
        ]
    )
    # transcription = await ActionAudio2TextTranscriptionAssemblyai(action_config=config).run_action(input)
    action_doc = ActionDoc(
        id="1",
        user_id="user_1",
        name="Action_for_celery",
        type=ActionAudio2TextTranscriptionAssemblyai.type,
        config=config.model_dump(exclude_none=True),
    )
    user = UserORM(id=str(gen_uuid()))
    db = next(get_mongo_db())
    run_action_object: RunActionObj = await ActionFactory.run_action(
        Context(), app_state, action_doc, input.model_dump(exclude_none=True), user, db=db
    )
    action_output: DataBlocks = (
        ActionAudio2TextTranscriptionAssemblyai.get_output_type().model_validate(
            run_action_object.output_dict
        )
    )
    assert action_output
    assert len(action_output.texts) > 0
    assert len(action_output.texts[0].text) > 0
    # assert transcription.text!=""
