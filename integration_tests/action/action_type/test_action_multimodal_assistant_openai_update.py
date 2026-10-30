# tests/action/action_type/test_action_multimodal_assistant_openai_update.py

import pytest
from loguru import logger
from openai import NotFoundError

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_multimodal.action_multimodal_assistant_openai.action_multimodal_assistant_openai import (
    ActionMultimodalAssistantOpenai,
)
from src.autobots.action.action_type.action_multimodal.action_multimodal_assistant_openai.assistant_openai_model import (
    AssistantOpenaiConfig,
    AssistantOpenaiConfigCreate,
    AssistantOpenaiConfigUpdate,
)
from src.autobots.data_model.app_state import AppState


@pytest.mark.asyncio
async def test_assistant_update_to_incompatible_model(ctx, db, user, set_test_settings):
    """
    This test verifies the "re-create on incompatibility" strategy.

    - Step 1: Create an assistant with a model that supports `temperature` (gpt-4o).
    - Step 2: Attempt to update that assistant to a model that does NOT support `temperature` (o3-mini).
    - Expected Behavior: The `update_config` method should detect the incompatibility,
      create a *new* assistant with the new model, delete the old one, and return
      the new assistant's config. The test should pass without a `BadRequestError`.
    """
    provider = "openai"  # This issue is most clearly reproducible with the OpenAI provider.
    created_assistant_config: AssistantOpenaiConfig | None = None
    original_assistant_id: str | None = None
    app_state = AppState()
    try:
        # --- Step 1: Create an assistant with a temperature-compatible model ---
        logger.info("--- Step 1: Creating Assistant with gpt-4o and temperature=0.8 ---")

        initial_config_create = AssistantOpenaiConfigCreate(
            provider=provider,
            model="gpt-4o",
            name="Test Assistant - Original",
            instructions="You are a creative assistant.",
            temperature=0.8,  # Explicitly set a parameter that o3-mini doesn't support
        )

        created_assistant_config = await ActionMultimodalAssistantOpenai.create_config(
            ctx, app_state, initial_config_create, user=user, db=db
        )
        original_assistant_id = created_assistant_config.id

        assert created_assistant_config.id is not None
        assert created_assistant_config.model == "gpt-4o"
        assert created_assistant_config.temperature == 0.8
        logger.success(f"--- Assistant {original_assistant_id} created successfully ---")

        # --- Step 2: Attempt to update the model to an incompatible one ---
        logger.info(
            "--- Step 2: Updating Assistant to o3-mini (which does not support temperature) ---"
        )

        config_update = AssistantOpenaiConfigUpdate(
            model="o3-mini",
            # Note: We are NOT providing 'temperature' here. It's "unset".
        )

        updated_config = await ActionMultimodalAssistantOpenai.update_config(
            ctx, app_state, created_assistant_config, config_update, user=user, db=db
        )

        # CRITICAL: Update the variable so the finally block cleans up the NEW assistant
        created_assistant_config = updated_config

        # --- Step 3: Assert the new behavior ---
        logger.success(f"--- Assistant successfully re-created as {updated_config.id} ---")
        assert updated_config.id != original_assistant_id, (
            "A new assistant ID should have been created."
        )
        assert updated_config.model == "o3-mini"
        # The API's default behavior is to set temperature to 1.0 if not provided.
        # Our "re-create" logic correctly omits it, so we expect the default value.
        assert updated_config.temperature == 1.0, (
            "Temperature should be reset to the API default of 1.0."
        )

    finally:
        # --- Step 4: Clean up the final, live assistant ---
        if created_assistant_config and created_assistant_config.id:
            logger.info(f"--- Step 4: Cleaning up Assistant {created_assistant_config.id} ---")
            await ActionMultimodalAssistantOpenai.delete_config(
                ctx, app_state, config=created_assistant_config, user=user, db=db
            )
            logger.info("--- Cleanup complete ---")


@pytest.mark.asyncio
async def test_assistant_update_compatible_model_is_inplace(ctx, db, user, set_test_settings):
    """
    CONFIRMS: A compatible update (e.g., changing instructions on the same model)
    is performed in-place and does NOT change the assistant_id.
    """
    provider = "openai"
    created_assistant_config: AssistantOpenaiConfig | None = None
    app_state = AppState()
    try:
        # --- Step 1: Create a standard assistant ---
        initial_config_create = AssistantOpenaiConfigCreate(
            provider=provider,
            model="gpt-4o",
            name="Test Assistant - In-place",
            instructions="Original instructions.",
        )
        created_assistant_config = await ActionMultimodalAssistantOpenai.create_config(
            ctx, app_state, initial_config_create, user=user, db=db
        )
        original_assistant_id = created_assistant_config.id
        assert original_assistant_id is not None

        # --- Step 2: Perform a compatible update ---
        config_update = AssistantOpenaiConfigUpdate(instructions="Updated instructions.")

        updated_config = await ActionMultimodalAssistantOpenai.update_config(
            ctx, app_state, created_assistant_config, config_update, user=user, db=db
        )

        # --- Step 3: Assert the update was in-place ---
        assert updated_config.id == original_assistant_id, "Assistant ID should NOT have changed."
        assert updated_config.instructions == "Updated instructions."
        logger.success("--- TEST PASSED: Compatible update was correctly performed in-place. ---")

    finally:
        if created_assistant_config and created_assistant_config.id:
            await ActionMultimodalAssistantOpenai.delete_config(
                ctx, app_state, config=created_assistant_config, user=user, db=db
            )


@pytest.mark.asyncio
async def test_assistant_update_reverse_incompatibility_recreates(ctx, db, user, set_test_settings):
    """
    CONFIRMS: The re-create logic also triggers correctly when switching from a reasoning
    model (with `reasoning_effort`) back to a standard model (which forbids it).
    """
    provider = "openai"
    created_assistant_config: AssistantOpenaiConfig | None = None
    app_state = AppState()
    try:
        # --- Step 1: Create an assistant with a reasoning model ---
        initial_config_create = AssistantOpenaiConfigCreate(
            provider=provider,
            model="o3-mini",
            name="Test Assistant - Reverse",
            reasoning_effort="medium",
        )
        created_assistant_config = await ActionMultimodalAssistantOpenai.create_config(
            ctx, app_state, initial_config_create, user, db
        )
        original_assistant_id = created_assistant_config.id

        # --- Step 2: Update to a model that forbids reasoning_effort ---
        config_update = AssistantOpenaiConfigUpdate(model="gpt-4o")
        updated_config = await ActionMultimodalAssistantOpenai.update_config(
            ctx, app_state, created_assistant_config, config_update, user, db
        )
        created_assistant_config = updated_config  # For cleanup

        # --- Step 3: Assert re-creation occurred ---
        assert updated_config.id != original_assistant_id, (
            "A new assistant ID should have been created."
        )
        assert updated_config.model == "gpt-4o"
        assert getattr(updated_config, "reasoning_effort", None) is None, (
            "reasoning_effort should be unset."
        )
        logger.success(
            "--- TEST PASSED: Reverse incompatible update correctly triggered re-creation. ---"
        )
    finally:
        if created_assistant_config and created_assistant_config.id:
            await ActionMultimodalAssistantOpenai.delete_config(
                ctx, app_state, created_assistant_config, user, db
            )


@pytest.mark.asyncio
async def test_end_to_end_recreation_and_run(ctx, db, user, set_test_settings):
    """
    CONFIRMS: An assistant that undergoes the re-creation process is fully functional
    and can successfully execute a run immediately after being updated.
    """
    provider = "openai"
    original_config: AssistantOpenaiConfig | None = None
    final_config: AssistantOpenaiConfig | None = None
    app_state = AppState()
    try:
        # --- Step 1: Create the initial assistant ---
        initial_payload = AssistantOpenaiConfigCreate(
            provider=provider, model="gpt-4o", name="E2E Test - Original", temperature=0.7
        )
        original_config = await ActionMultimodalAssistantOpenai.create_config(
            ctx, app_state, initial_payload, user, db
        )
        logger.info(f"--- E2E: Created initial assistant {original_config.id} ---")

        # --- Step 2: Run a baseline prompt to ensure it's working ---
        action_instance_1 = ActionMultimodalAssistantOpenai(
            action_config=original_config, user=user, db=db
        )
        baseline_result = await action_instance_1.run_action(
            ctx, app_state, action_input=DataBlock(text="Hello, who are you?")
        )
        assert isinstance(baseline_result, DataBlocks)
        assert len(baseline_result.blocks) > 0
        assert baseline_result.blocks[0].text is not None
        logger.success(f"--- E2E: Baseline run on {original_config.id} successful. ---")

        # --- Step 3: Trigger the incompatible update ---
        update_payload = AssistantOpenaiConfigUpdate(model="o3-mini")
        final_config = await ActionMultimodalAssistantOpenai.update_config(
            ctx, app_state, original_config, update_payload, user, db
        )

        assert final_config.id != original_config.id, "Assistant should have been re-created."
        assert final_config.model == "o3-mini"
        logger.info(f"--- E2E: Assistant re-created as {final_config.id} ---")

        # --- Step 4: Run a validation prompt on the NEW assistant ---
        action_instance_2 = ActionMultimodalAssistantOpenai(
            action_config=final_config, user=user, db=db
        )
        validation_result = await action_instance_2.run_action(
            ctx, app_state, action_input=DataBlock(text="What is the color of a ripe apple?")
        )

        # --- Step 5: Assert the new assistant is fully functional ---
        assert isinstance(validation_result, DataBlocks)
        assert len(validation_result.blocks) > 0
        assert validation_result.blocks[0].text is not None
        logger.success(
            f"--- E2E: Validation run on new assistant {final_config.id} successful! ---"
        )
        logger.success(
            "--- TEST PASSED: End-to-end recreation and run flow is fully validated. ---"
        )

    finally:
        # Cleanup both assistants in case of failure mid-test
        # The original_config is expected to be deleted by the re-create logic,
        # so we anticipate a NotFoundError and handle it gracefully.
        if original_config and original_config.id:
            try:
                await ActionMultimodalAssistantOpenai.delete_config(ctx, app_state, original_config, user, db)
                logger.info(
                    f"--- E2E: Cleaned up original assistant {original_config.id} (This should not happen in a successful re-create test). ---"
                )
            except NotFoundError:
                # This is the expected and correct outcome for this test.
                logger.success(
                    f"--- E2E: Confirmed original assistant {original_config.id} was successfully deleted during re-creation. ---"
                )

        # The final_config is the new, live assistant that must always be cleaned up.
        if final_config and final_config.id:
            await ActionMultimodalAssistantOpenai.delete_config(ctx, app_state, final_config, user, db)
            logger.info(f"--- E2E: Cleaned up final assistant {final_config.id} ---")


@pytest.mark.asyncio
async def test_update_from_implicit_default_triggers_recreation(ctx, db, user, set_test_settings):
    """
    This is the critical regression test that reproduces the production bug.

    It verifies that switching to an incompatible model family triggers a re-creation
    even when the incompatible parameter (`temperature`) was NOT explicitly set on the
    original assistant, relying on the API's server-side default.
    """
    provider = "openai"
    created_assistant_config: AssistantOpenaiConfig | None = None
    original_assistant_id: str | None = None
    app_state = AppState()
    try:
        # Step 1: Create an assistant WITHOUT explicitly setting temperature.
        # This mirrors the production scenario where config.temperature would be None.
        logger.info("--- REGRESSION: Creating Assistant with gpt-4o and NO temperature ---")
        initial_config_create = AssistantOpenaiConfigCreate(
            provider=provider,
            model="gpt-4o",
            name="Test Implicit Default",
        )
        created_assistant_config = await ActionMultimodalAssistantOpenai.create_config(
            ctx, app_state, initial_config_create, user=user, db=db
        )
        original_assistant_id = created_assistant_config.id

        # We know the API returns the default value in its response.
        assert created_assistant_config.temperature == 1.0, (
            "API should return the default temperature of 1.0"
        )

        # Step 2: Update to an incompatible model.
        logger.info("--- REGRESSION: Updating to o3-mini ---")
        config_update = AssistantOpenaiConfigUpdate(model="o3-mini")

        # The "Model Family Switch" logic should correctly detect this and re-create.
        updated_config = await ActionMultimodalAssistantOpenai.update_config(
            ctx, app_state, created_assistant_config, config_update, user=user, db=db
        )
        created_assistant_config = updated_config  # For cleanup

        # Step 3: Assert that re-creation was correctly triggered.
        assert updated_config.id != original_assistant_id, (
            "A new assistant should have been created."
        )
        assert updated_config.model == "o3-mini"
        logger.success(
            "--- REGRESSION TEST PASSED: Incompatible switch from implicit default correctly triggered re-creation. ---"
        )

    finally:
        if created_assistant_config and created_assistant_config.id:
            await ActionMultimodalAssistantOpenai.delete_config(
                ctx, app_state, config=created_assistant_config, user=user, db=db
            )


@pytest.mark.asyncio
async def test_provider_switch_triggers_recreation_and_handles_payload_correctly(
    ctx, db, user, set_test_settings
):
    """
    This test is designed to fail before the fix and pass after.
    It specifically triggers the `TypeError` for a duplicate 'provider' keyword argument
    and also validates that the 'model' key is correctly carried over during a
    provider-only switch.
    """
    original_provider = "openai"
    # We use "azure" as a string to trigger the logic branch. The test will still use the configured
    # OpenAI client, but the `provider` attribute of the final object will be correctly updated.
    target_provider = "azure"

    original_config: AssistantOpenaiConfig | None = None
    final_config: AssistantOpenaiConfig | None = None
    app_state = AppState()
    try:
        # --- Step 1: Create an assistant on the original provider ---
        logger.info(f"--- Step 1: Creating Assistant on provider '{original_provider}' ---")
        initial_config_create = AssistantOpenaiConfigCreate(
            provider=original_provider,
            model="gpt-4o",
            name="Test Provider Switch Original",
        )
        original_config = await ActionMultimodalAssistantOpenai.create_config(
            ctx, app_state, initial_config_create, user=user, db=db
        )
        assert original_config.id is not None
        assert original_config.provider == original_provider
        logger.success(
            f"--- Assistant {original_config.id} created successfully on '{original_provider}' ---"
        )

        # --- Step 2: Update ONLY the provider, triggering the re-create logic ---
        logger.info(f"--- Step 2: Updating provider to '{target_provider}' ---")
        config_update = AssistantOpenaiConfigUpdate(provider=target_provider)

        # BEFORE FIX: This line will raise a TypeError.
        # AFTER FIX: This line will succeed.
        final_config = await ActionMultimodalAssistantOpenai.update_config(
            ctx, app_state, original_config, config_update, user=user, db=db
        )

        # --- Step 3: Assert that the re-creation was successful ---
        logger.success(f"--- Assistant successfully re-created as {final_config.id} ---")

        assert final_config.id != original_config.id, "A new assistant ID should have been created."
        assert final_config.provider == target_provider, (
            "The new assistant should be on the target provider."
        )
        assert final_config.model == original_config.model, (
            "The model should have been carried over from the original config."
        )
        assert final_config.name != original_config.name, (
            "The new 'forked' assistant should have a new name."
        )

    finally:
        # --- Step 4: Clean up both the original and the new assistant ---
        # The original assistant is orphaned by the re-create logic, so we clean it up manually.
        if original_config and original_config.id:
            try:
                logger.info(f"--- Cleaning up original assistant {original_config.id} ---")
                await ActionMultimodalAssistantOpenai.delete_config(ctx, app_state, original_config, user, db)
            except NotFoundError:
                # This can happen if the test fails after re-creation but before assignment
                logger.warning(f"Original assistant {original_config.id} not found for cleanup.")

        # The final_config is the new, live assistant that must always be cleaned up.
        if final_config and final_config.id:
            logger.info(f"--- Cleaning up final assistant {final_config.id} ---")
            await ActionMultimodalAssistantOpenai.delete_config(ctx, app_state, final_config, user, db)
