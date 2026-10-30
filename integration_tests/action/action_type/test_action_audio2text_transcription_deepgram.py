import pytest

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_audio2text.action_audio2text_transcription_deepgram import (
    ActionAudio2TextTranscriptionDeepgram,
    DeepgramTranscriptionConfig,
)
from src.autobots.action.action_type.action_factory import ActionFactory, RunActionObj
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.utils import gen_uuid
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.user.user_orm_model import UserORM


@pytest.mark.asyncio
async def test_action_audio2text_transcription_deepgram_happy_path(set_test_settings):
    # Updated to use new DeepgramTranscriptionConfig
    config = DeepgramTranscriptionConfig(
        file_url="https://dpgr.am/spacewalk.wav",  # Using Deepgram's sample audio
        model="nova-3",
        punctuate=True,
        diarize=True,
    )
    input = DataBlock(urls=["https://dpgr.am/spacewalk.wav"])

    action_doc = ActionDoc(
        id="1",
        user_id="user_1",
        name="Action_for_deepgram_transcription",
        type=ActionAudio2TextTranscriptionDeepgram.type,
        config=config.model_dump(exclude_none=True),
    )
    user = UserORM(id=str(gen_uuid()))
    db = next(get_mongo_db())
    run_action_object: RunActionObj = await ActionFactory.run_action(
        Context(), AppState(), action_doc, input.model_dump(exclude_none=True), user, db=db
    )
    action_output: DataBlocks = (
        ActionAudio2TextTranscriptionDeepgram.get_output_type().model_validate(
            run_action_object.output_dict
        )
    )
    assert action_output
    assert len(action_output.blocks) > 0
    assert len(action_output.blocks[0].text) > 0
