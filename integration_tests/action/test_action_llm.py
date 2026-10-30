from typing import Literal

import pytest

from src.autobots.action.action.action_doc_model import ActionDoc
from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.action.action_type.action_factory import ActionFactory
from src.autobots.action.action_type.action_llm.action_llm import ActionLlm, LlmConfig
from src.autobots.action.action_type.action_map import ACTION_MAP
from src.autobots.action.action_type.action_types import ActionType
from src.autobots.data_model.app_state import AppState

# Test data
CHEAPEST_MODELS = {
    "openai": "gpt-5-nano",
    "gemini": "gemini-2.5-flash-lite",
    "claude": "claude-haiku-4-5",
    "grok": "grok-4.3",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "gemini", "grok"])
async def test_action_llm_via_action_factory_happy_path(
    ctx, db, user, provider: Literal["openai", "gemini", "claude", "grok"], set_test_settings
):
    """Test running ActionLlm via ActionFactory for each provider."""
    action_type = ActionType.action_llm
    action = ACTION_MAP.get(action_type)

    action_config = LlmConfig(
        provider=provider,
        model=CHEAPEST_MODELS[provider],
        prompt="You are a helpful assistant. Answer in one sentence.",
    )

    action_input_dict = {"text": "What is the capital of France?"}
    action.get_input_type().model_validate(action_input_dict)

    action_doc = ActionDoc(
        id="id_test_action_llm_factory",
        user_id=user.id,
        name="name_test_action_llm_factory",
        type=action_type,
        config=action_config.model_dump(exclude_none=True),
    )
    app_state = AppState()
    run_action_obj = await ActionFactory.run_action(ctx, app_state, action_doc, action_input_dict, user, db=db)
    run_output = action.get_output_type().model_validate(run_action_obj.output_dict)

    assert isinstance(run_output, DataBlocks)
    assert len(run_output.blocks) == 1
    assert run_output.blocks[0].text is not None
    assert len(run_output.blocks[0].text) > 0
    # Should contain answer about Paris
    assert "paris" in run_output.blocks[0].text.lower()


@pytest.mark.asyncio
async def test_action_llm_via_action_factory_config_prompt_only(ctx, db, user, set_test_settings):
    """Test ActionLlm with only config prompt (no input text)."""
    action_type = ActionType.action_llm
    action = ACTION_MAP.get(action_type)

    action_config = LlmConfig(
        provider="openai", model="gpt-5-nano", prompt="What is 2+2? Answer with just the number."
    )

    action_input_dict = {}  # No text input
    action.get_input_type().model_validate(action_input_dict)

    action_doc = ActionDoc(
        id="id_test_action_llm_config_only",
        user_id=user.id,
        name="name_test_action_llm_config_only",
        type=action_type,
        config=action_config.model_dump(exclude_none=True),
    )
    app_state = AppState()
    run_action_obj = await ActionFactory.run_action(ctx, app_state, action_doc, action_input_dict, user, db=db)
    run_output = action.get_output_type().model_validate(run_action_obj.output_dict)

    assert isinstance(run_output, DataBlocks)
    assert len(run_output.blocks) == 1
    assert run_output.blocks[0].text is not None
    # Should contain "4"
    assert "4" in run_output.blocks[0].text


@pytest.mark.asyncio
async def test_action_llm_via_action_factory_input_text_only(ctx, db, user, set_test_settings):
    """Test ActionLlm with only input text (no config prompt)."""
    action_type = ActionType.action_llm
    action = ACTION_MAP.get(action_type)

    action_config = LlmConfig(
        provider="openai",
        model="gpt-5-nano",
        # No prompt
    )

    action_input_dict = {"text": "What is 3+3? Answer with just the number."}
    action.get_input_type().model_validate(action_input_dict)

    action_doc = ActionDoc(
        id="id_test_action_llm_input_only",
        user_id=user.id,
        name="name_test_action_llm_input_only",
        type=action_type,
        config=action_config.model_dump(exclude_none=True),
    )
    app_state = AppState()
    run_action_obj = await ActionFactory.run_action(ctx, app_state, action_doc, action_input_dict, user, db=db)
    run_output = action.get_output_type().model_validate(run_action_obj.output_dict)

    assert isinstance(run_output, DataBlocks)
    assert len(run_output.blocks) == 1
    assert run_output.blocks[0].text is not None
    # Should contain "6"
    assert "6" in run_output.blocks[0].text


@pytest.mark.asyncio
async def test_action_llm_via_action_factory_prompt_combination(ctx, db, user, set_test_settings):
    """Test that config prompt and input text are combined correctly."""
    action_type = ActionType.action_llm
    action = ACTION_MAP.get(action_type)

    action_config = LlmConfig(
        provider="openai",
        model="gpt-5-nano",
        prompt="You are a math expert. Always start with 'Answer:'",
    )

    action_input_dict = {"text": "What is 5+5?"}
    action.get_input_type().model_validate(action_input_dict)

    action_doc = ActionDoc(
        id="id_test_action_llm_combination",
        user_id=user.id,
        name="name_test_action_llm_combination",
        type=action_type,
        config=action_config.model_dump(exclude_none=True),
    )
    app_state = AppState()
    run_action_obj = await ActionFactory.run_action(ctx, app_state, action_doc, action_input_dict, user, db=db)
    run_output = action.get_output_type().model_validate(run_action_obj.output_dict)

    assert isinstance(run_output, DataBlocks)
    assert len(run_output.blocks) == 1
    assert run_output.blocks[0].text is not None
    # Should start with "Answer:" and contain "10"
    output_text = run_output.blocks[0].text.lower()
    assert "answer:" in output_text
    assert "10" in output_text


@pytest.mark.asyncio
async def test_action_llm_via_action_factory_no_prompt_duplication(
    ctx, db, user, set_test_settings
):
    """Test that duplicate prompts are avoided when config.prompt == input.text."""
    action_type = ActionType.action_llm
    action = ACTION_MAP.get(action_type)

    prompt = "What is 7+7? Answer with just the number."
    action_config = LlmConfig(provider="openai", model="gpt-5-nano", prompt=prompt)

    action_input_dict = {"text": prompt}  # Same as config prompt
    action.get_input_type().model_validate(action_input_dict)

    action_doc = ActionDoc(
        id="id_test_action_llm_no_duplication",
        user_id=user.id,
        name="name_test_action_llm_no_duplication",
        type=action_type,
        config=action_config.model_dump(exclude_none=True),
    )
    app_state = AppState()
    run_action_obj = await ActionFactory.run_action(ctx, app_state, action_doc, action_input_dict, user, db=db)
    run_output = action.get_output_type().model_validate(run_action_obj.output_dict)

    assert isinstance(run_output, DataBlocks)
    assert len(run_output.blocks) == 1
    assert run_output.blocks[0].text is not None
    # Should contain "14"
    assert "14" in run_output.blocks[0].text


@pytest.mark.asyncio
async def test_action_llm_basic_openai(ctx, db, user, set_test_settings):
    """Test ActionLlm directly with OpenAI provider."""
    action_config = LlmConfig(
        provider="openai", model="gpt-5-nano", prompt="You are a helpful assistant."
    )
    action = ActionLlm(action_config, user=user, db=db)

    action_input = DataBlock(text="Say 'Hello World'")
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    if isinstance(action_output, Exception):
        pytest.fail(f"Action returned exception: {action_output}")

    assert isinstance(action_output, DataBlocks)
    assert len(action_output.blocks) == 1
    assert action_output.blocks[0].text is not None
    assert len(action_output.blocks[0].text) > 0
    assert "hello world" in action_output.blocks[0].text.lower()


@pytest.mark.asyncio
async def test_action_llm_basic_gemini(ctx, db, user, set_test_settings):
    """Test ActionLlm directly with Gemini provider."""
    action_config = LlmConfig(
        provider="gemini", model="gemini-2.5-flash-lite", prompt="You are a helpful assistant."
    )
    action = ActionLlm(action_config, user=user, db=db)

    action_input = DataBlock(text="Say 'Hello Gemini'")
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    if isinstance(action_output, Exception):
        pytest.fail(f"Action returned exception: {action_output}")

    assert isinstance(action_output, DataBlocks)
    assert len(action_output.blocks) == 1
    assert action_output.blocks[0].text is not None
    assert len(action_output.blocks[0].text) > 0
    assert "hello gemini" in action_output.blocks[0].text.lower()


@pytest.mark.asyncio
@pytest.mark.skip(reason="Claude provider not configured in test environment")
async def test_action_llm_basic_claude(ctx, db, user, set_test_settings):
    """Test ActionLlm directly with Claude provider."""
    action_config = LlmConfig(
        provider="claude", model="claude-haiku-4-5", prompt="You are a helpful assistant."
    )
    action = ActionLlm(action_config, user=user, db=db)

    action_input = DataBlock(text="Say 'Hello Claude'")
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    if isinstance(action_output, Exception):
        pytest.fail(f"Action returned exception: {action_output}")

    assert isinstance(action_output, DataBlocks)
    assert len(action_output.blocks) == 1
    assert action_output.blocks[0].text is not None
    assert len(action_output.blocks[0].text) > 0
    assert "hello claude" in action_output.blocks[0].text.lower()


@pytest.mark.asyncio
async def test_action_llm_basic_grok(ctx, db, user, set_test_settings):
    """Test ActionLlm directly with Grok provider."""
    action_config = LlmConfig(
        provider="grok", model="grok-4.3", prompt="You are a helpful assistant."
    )
    action = ActionLlm(action_config, user=user, db=db)

    action_input = DataBlock(text="Say 'Hello Grok'")
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    if isinstance(action_output, Exception):
        pytest.fail(f"Action returned exception: {action_output}")

    assert isinstance(action_output, DataBlocks)
    assert len(action_output.blocks) == 1
    assert action_output.blocks[0].text is not None
    assert len(action_output.blocks[0].text) > 0
    assert "hello grok" in action_output.blocks[0].text.lower()


@pytest.mark.asyncio
async def test_action_llm_run_tool(ctx, db, user, set_test_settings):
    """Test running ActionLlm via run_tool method."""
    action_config = LlmConfig(provider="openai", model="gpt-5-nano")
    action = ActionLlm(action_config, user=user, db=db)

    tool_config = LlmConfig(
        provider="openai", model="gpt-5-nano", prompt="What is 8+8? Answer with just the number."
    )
    app_state = AppState()
    action_output = await action.run_tool(tool_config, ctx, app_state)

    if isinstance(action_output, Exception):
        pytest.fail(f"Action returned exception: {action_output}")

    # run_tool returns DataBlock, not DataBlocks
    assert isinstance(action_output, DataBlock)
    assert action_output.text is not None
    assert len(action_output.text) > 0
    # Should contain "16"
    assert "16" in action_output.text


@pytest.mark.asyncio
async def test_action_llm_run_tool_no_prompt(ctx, db, user, set_test_settings):
    """Test error handling for run_tool without prompt."""
    action_config = LlmConfig(provider="openai", model="gpt-5-nano")
    action = ActionLlm(action_config, user=user, db=db)

    tool_config = LlmConfig(
        provider="openai",
        model="gpt-5-nano",
        # No prompt - should error
    )
    app_state = AppState()
    action_output = await action.run_tool(tool_config, ctx, app_state)

    assert isinstance(action_output, Exception)
    assert "prompt is required" in str(action_output)


@pytest.mark.asyncio
async def test_action_llm_empty_input_and_prompt(ctx, db, user, set_test_settings):
    """Test error handling when neither config.prompt nor action_input.text is provided."""
    action_config = LlmConfig(
        provider="openai",
        model="gpt-5-nano",
        # No prompt
    )
    action = ActionLlm(action_config, user=user, db=db)

    action_input = DataBlock(text="")  # Empty text
    app_state = AppState()
    action_output = await action.run_action(ctx, app_state, action_input)

    assert isinstance(action_output, Exception)
    assert "Either config.prompt or action_input.text must be provided" in str(action_output)


@pytest.mark.asyncio
async def test_action_llm_get_description(ctx, db, user, set_test_settings):
    """Test that get_description returns a non-empty string."""
    description = ActionLlm.get_description()
    assert isinstance(description, str)
    assert len(description) > 0
    assert "LLM" in description
    assert "provider" in description


@pytest.mark.asyncio
async def test_action_llm_get_types(ctx, db, user, set_test_settings):
    """Test that get_*_type methods return correct types."""
    assert ActionLlm.get_config_create_type() == LlmConfig
    assert ActionLlm.get_config_update_type() == LlmConfig
    assert ActionLlm.get_config_type() == LlmConfig
    assert ActionLlm.get_input_type() == DataBlock
    assert ActionLlm.get_output_type() == DataBlocks
    assert ActionLlm.type == ActionType.action_llm
