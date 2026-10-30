# # tests/action/action_type/test_action_multimodal_assistant_openai_stream_update.py

# import json

# import pytest
# from loguru import logger
# from openai import NotFoundError

# # ActionMultimodalAssistantOpenaiStream is currently retired — the implementation
# # in action_multimodal_assistant_openai_stream.py is commented out. Skip this whole
# # module until the action is reinstated. The guarded import keeps collection green.
# pytestmark = pytest.mark.skip(
#     reason="ActionMultimodalAssistantOpenaiStream is retired (source commented out)"
# )

# from src.autobots.action.action.common_action_models import DataBlock, DataBlocks

# try:
#     from src.autobots.action.action_type.action_multimodal.action_multimodal_assistant_openai.action_multimodal_assistant_openai_stream import (
#         ActionMultimodalAssistantOpenaiStream,
#     )
#     from src.autobots.action.action_type.action_multimodal.action_multimodal_assistant_openai.assistant_openai_model import (
#         AssistantOpenaiConfig,
#         AssistantOpenaiConfigCreate,
#         AssistantOpenaiConfigUpdate,
#     )
# except ImportError:
#     ActionMultimodalAssistantOpenaiStream = None
#     AssistantOpenaiConfig = AssistantOpenaiConfigCreate = AssistantOpenaiConfigUpdate = None


# async def consume_stream_and_get_final_result(stream) -> DataBlocks | None:
#     """Helper to consume the async iterator and extract the final result."""
#     final_result = None
#     async for chunk in stream:
#         try:
#             data = json.loads(chunk.decode("utf-8"))
#             event = data.get("event")
#             if event == "action-run":
#                 if data.get("error"):
#                     raise Exception(f"Stream reported an error: {data['error']}")
#                 if "result" in data:
#                     final_result = DataBlocks.model_validate(data["result"])
#         except (json.JSONDecodeError, KeyError) as e:
#             logger.error(f"Failed to parse stream chunk: {chunk.decode('utf-8')}, Error: {e}")
#             continue
#     return final_result


# @pytest.mark.asyncio
# async def test_assistant_update_to_incompatible_model_stream(ctx, db, user, set_test_settings):
#     """
#     Verifies the "re-create" strategy for the STREAMING action.
#     """
#     provider = "openai"
#     created_assistant_config: AssistantOpenaiConfig | None = None
#     original_assistant_id: str | None = None

#     try:
#         # Step 1: Create an assistant with a temperature-compatible model
#         logger.info("--- [STREAM] Step 1: Creating Assistant with gpt-4o and temperature=0.8 ---")
#         initial_config_create = AssistantOpenaiConfigCreate(
#             provider=provider,
#             model="gpt-4o",
#             name="Test Stream Assistant - Original",
#             temperature=0.8,
#         )
#         created_assistant_config = await ActionMultimodalAssistantOpenaiStream.create_config(
#             ctx, initial_config_create, user=user, db=db
#         )
#         original_assistant_id = created_assistant_config.id
#         assert original_assistant_id is not None
#         logger.success(f"--- [STREAM] Assistant {original_assistant_id} created successfully ---")

#         # Step 2: Update to an incompatible model
#         logger.info("--- [STREAM] Step 2: Updating to o3-mini ---")
#         config_update = AssistantOpenaiConfigUpdate(model="o3-mini")
#         updated_config = await ActionMultimodalAssistantOpenaiStream.update_config(
#             ctx, created_assistant_config, config_update, user=user, db=db
#         )
#         created_assistant_config = updated_config  # For cleanup

#         # Step 3: Assert re-creation occurred
#         logger.success(f"--- [STREAM] Assistant successfully re-created as {updated_config.id} ---")
#         assert updated_config.id != original_assistant_id
#         assert updated_config.model == "o3-mini"
#         assert updated_config.temperature == 1.0

#     finally:
#         # Step 4: Clean up
#         if created_assistant_config and created_assistant_config.id:
#             logger.info(
#                 f"--- [STREAM] Step 4: Cleaning up Assistant {created_assistant_config.id} ---"
#             )
#             await ActionMultimodalAssistantOpenaiStream.delete_config(
#                 ctx, config=created_assistant_config, user=user, db=db
#             )
#             logger.info("--- [STREAM] Cleanup complete ---")


# @pytest.mark.asyncio
# async def test_assistant_update_compatible_model_is_inplace_stream(
#     ctx, db, user, set_test_settings
# ):
#     """
#     CONFIRMS: A compatible update is in-place for the STREAMING action.
#     """
#     provider = "openai"
#     created_assistant_config: AssistantOpenaiConfig | None = None

#     try:
#         # Step 1: Create a standard assistant
#         initial_config_create = AssistantOpenaiConfigCreate(
#             provider=provider,
#             model="gpt-4o",
#             name="Test Stream Assistant - In-place",
#             instructions="Original.",
#         )
#         created_assistant_config = await ActionMultimodalAssistantOpenaiStream.create_config(
#             ctx, initial_config_create, user=user, db=db
#         )
#         original_assistant_id = created_assistant_config.id

#         # Step 2: Perform a compatible update
#         config_update = AssistantOpenaiConfigUpdate(instructions="Updated.")
#         updated_config = await ActionMultimodalAssistantOpenaiStream.update_config(
#             ctx, created_assistant_config, config_update, user=user, db=db
#         )

#         # Step 3: Assert the update was in-place
#         assert updated_config.id == original_assistant_id
#         assert updated_config.instructions == "Updated."
#         logger.success(
#             "--- [STREAM] TEST PASSED: Compatible update was correctly performed in-place. ---"
#         )

#     finally:
#         if created_assistant_config and created_assistant_config.id:
#             await ActionMultimodalAssistantOpenaiStream.delete_config(
#                 ctx, config=created_assistant_config, user=user, db=db
#             )


# @pytest.mark.asyncio
# async def test_assistant_update_reverse_incompatibility_recreates_stream(
#     ctx, db, user, set_test_settings
# ):
#     """
#     CONFIRMS: Reverse incompatibility also re-creates for the STREAMING action.
#     """
#     provider = "openai"
#     created_assistant_config: AssistantOpenaiConfig | None = None
#     try:
#         # Step 1: Create with reasoning model
#         initial_config_create = AssistantOpenaiConfigCreate(
#             provider=provider,
#             model="o3-mini",
#             name="Test Stream Assistant - Reverse",
#             reasoning_effort="medium",
#         )
#         created_assistant_config = await ActionMultimodalAssistantOpenaiStream.create_config(
#             ctx, initial_config_create, user, db
#         )
#         original_assistant_id = created_assistant_config.id

#         # Step 2: Update to a model that forbids reasoning_effort
#         config_update = AssistantOpenaiConfigUpdate(model="gpt-4o")
#         updated_config = await ActionMultimodalAssistantOpenaiStream.update_config(
#             ctx, created_assistant_config, config_update, user, db
#         )
#         created_assistant_config = updated_config

#         # Step 3: Assert re-creation occurred
#         assert updated_config.id != original_assistant_id
#         assert updated_config.model == "gpt-4o"
#         assert getattr(updated_config, "reasoning_effort", None) is None
#         logger.success(
#             "--- [STREAM] TEST PASSED: Reverse incompatible update correctly triggered re-creation. ---"
#         )
#     finally:
#         if created_assistant_config and created_assistant_config.id:
#             await ActionMultimodalAssistantOpenaiStream.delete_config(
#                 ctx, created_assistant_config, user, db
#             )


# @pytest.mark.asyncio
# async def test_end_to_end_recreation_and_run_stream(ctx, db, user, set_test_settings):
#     """
#     CONFIRMS: A re-created assistant from the STREAMING action is fully functional.
#     """
#     provider = "openai"
#     original_config: AssistantOpenaiConfig | None = None
#     final_config: AssistantOpenaiConfig | None = None

#     try:
#         # Step 1: Create initial assistant
#         initial_payload = AssistantOpenaiConfigCreate(
#             provider=provider, model="gpt-4o", name="E2E Stream Test - Original", temperature=0.7
#         )
#         original_config = await ActionMultimodalAssistantOpenaiStream.create_config(
#             ctx, initial_payload, user, db
#         )
#         logger.info(f"--- E2E [STREAM]: Created initial assistant {original_config.id} ---")

#         # Step 2: Run baseline prompt
#         action_instance_1 = ActionMultimodalAssistantOpenaiStream(
#             action_config=original_config, user=user, db=db
#         )
#         stream1 = action_instance_1.run_action(
#             ctx, action_input=DataBlock(text="Hello, who are you?")
#         )
#         baseline_result = await consume_stream_and_get_final_result(stream1)
#         assert isinstance(baseline_result, DataBlocks) and len(baseline_result.blocks) > 0
#         logger.success(f"--- E2E [STREAM]: Baseline run on {original_config.id} successful. ---")

#         # Step 3: Trigger incompatible update
#         update_payload = AssistantOpenaiConfigUpdate(model="o3-mini")
#         final_config = await ActionMultimodalAssistantOpenaiStream.update_config(
#             ctx, original_config, update_payload, user, db
#         )
#         assert final_config.id != original_config.id
#         logger.info(f"--- E2E [STREAM]: Assistant re-created as {final_config.id} ---")

#         # Step 4: Run validation prompt on the NEW assistant
#         action_instance_2 = ActionMultimodalAssistantOpenaiStream(
#             action_config=final_config, user=user, db=db
#         )
#         stream2 = action_instance_2.run_action(
#             ctx, action_input=DataBlock(text="What is the color of a ripe apple?")
#         )
#         validation_result = await consume_stream_and_get_final_result(stream2)

#         # Step 5: Assert new assistant is functional
#         assert isinstance(validation_result, DataBlocks) and len(validation_result.blocks) > 0
#         assert validation_result.blocks[0].text is not None
#         logger.success(
#             f"--- E2E [STREAM]: Validation run on new assistant {final_config.id} successful! ---"
#         )
#         logger.success(
#             "--- [STREAM] TEST PASSED: End-to-end recreation and run flow is fully validated. ---"
#         )

#     finally:
#         # Cleanup
#         if original_config and original_config.id:
#             try:
#                 await ActionMultimodalAssistantOpenaiStream.delete_config(
#                     ctx, original_config, user, db
#                 )
#             except NotFoundError:
#                 logger.success(
#                     f"--- E2E [STREAM]: Confirmed original assistant {original_config.id} was deleted/orphaned. ---"
#                 )

#         if final_config and final_config.id:
#             await ActionMultimodalAssistantOpenaiStream.delete_config(ctx, final_config, user, db)
#             logger.info(f"--- E2E [STREAM]: Cleaned up final assistant {final_config.id} ---")


# @pytest.mark.asyncio
# async def test_update_from_implicit_default_triggers_recreation_stream(
#     ctx, db, user, set_test_settings
# ):
#     """
#     This is the critical regression test that reproduces the production bug for the streaming action.

#     It verifies that switching to an incompatible model family triggers a re-creation
#     even when the incompatible parameter (`temperature`) was NOT explicitly set on the
#     original assistant, relying on the API's server-side default.
#     """
#     provider = "openai"
#     created_assistant_config: AssistantOpenaiConfig | None = None
#     original_assistant_id: str | None = None

#     try:
#         # Step 1: Create an assistant WITHOUT explicitly setting temperature.
#         logger.info(
#             "--- REGRESSION [STREAM]: Creating Assistant with gpt-4o and NO temperature ---"
#         )
#         initial_config_create = AssistantOpenaiConfigCreate(
#             provider=provider,
#             model="gpt-4o",
#             name="Test Implicit Default Stream",
#         )
#         created_assistant_config = await ActionMultimodalAssistantOpenaiStream.create_config(
#             ctx, initial_config_create, user=user, db=db
#         )
#         original_assistant_id = created_assistant_config.id

#         # We know the API returns the default value in its response.
#         assert created_assistant_config.temperature == 1.0, (
#             "API should return the default temperature of 1.0"
#         )

#         # Step 2: Update to an incompatible model.
#         logger.info("--- REGRESSION [STREAM]: Updating to o3-mini ---")
#         config_update = AssistantOpenaiConfigUpdate(model="o3-mini")

#         # The "Model Family Switch" logic should correctly detect this and re-create.
#         updated_config = await ActionMultimodalAssistantOpenaiStream.update_config(
#             ctx, created_assistant_config, config_update, user=user, db=db
#         )
#         created_assistant_config = updated_config  # For cleanup

#         # Step 3: Assert that re-creation was correctly triggered.
#         assert updated_config.id != original_assistant_id, (
#             "A new assistant should have been created."
#         )
#         assert updated_config.model == "o3-mini"
#         logger.success(
#             "--- REGRESSION TEST PASSED [STREAM]: Incompatible switch from implicit default correctly triggered re-creation. ---"
#         )

#     finally:
#         if created_assistant_config and created_assistant_config.id:
#             await ActionMultimodalAssistantOpenaiStream.delete_config(
#                 ctx, config=created_assistant_config, user=user, db=db
#             )
