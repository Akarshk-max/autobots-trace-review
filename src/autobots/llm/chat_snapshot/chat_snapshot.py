from dataclasses import dataclass, replace
from typing import Any, Callable

from loguru import logger

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify

CANCELLED_NOTICE = "Run cancelled by user."

AppendNotice = Callable[[list[Any], str], list[Any]]
ApplyConfig = Callable[[dict[str, Any], list[Any], dict[str, Any]], dict[str, Any]]


@dataclass
class ChatSnapshot:
    """Conversation captured at an agentic loop boundary.

    Only the message list is held. The connector loops work on a provider request
    object, not the action config, so persisting that object wholesale would
    overwrite action-level fields the request never carried.
    """

    provider: str
    apply_config: ApplyConfig
    meta: dict[str, Any]
    messages: list[Any]
    output_text: str
    loop_index: int
    append_notice: AppendNotice

    def finalized(self, notice: str = CANCELLED_NOTICE) -> "ChatSnapshot":
        messages = self.append_notice(list(self.messages), notice)
        output_text = f"{self.output_text}\n\n{notice}".strip()
        return replace(self, messages=messages, output_text=output_text)


def store(
    ctx: Context,
    app_state: AppState,
    provider: str,
    apply_config: ApplyConfig,
    meta: dict[str, Any],
    messages: list[Any],
    append_notice: AppendNotice,
    loop_index: int,
    output_text: str = "",
) -> None:
    cancellation = app_state.cancellation
    if cancellation is None:
        return

    try:
        cancellation.set_snapshot(
            ChatSnapshot(
                provider=provider,
                apply_config=apply_config,
                meta=meta,
                messages=messages,
                output_text=output_text,
                loop_index=loop_index,
                append_notice=append_notice,
            )
        )
    except Exception as e:
        logger.bind(ctx=ctx, provider=provider, loop_index=loop_index).warning(
            f"Chat snapshot capture failed, continuing run without it: {e}"
        )
        return

    logger.bind(ctx=ctx, provider=provider, loop_index=loop_index).debug("Chat snapshot stored")


def apply_to_result(
    ctx: Context,
    action_result_doc,
    app_state: AppState,
    notify: Notify | None = None,
    notice: str = CANCELLED_NOTICE,
) -> bool:
    """Merge the last loop-boundary snapshot into a cancelled result doc."""
    cancellation = app_state.cancellation
    snapshot = cancellation.snapshot if cancellation is not None else None

    output_text = notice
    if snapshot is not None:
        try:
            finalized = snapshot.finalized(notice)
            config = dict(action_result_doc.result.config or {})
            action_result_doc.result.config = finalized.apply_config(
                config, finalized.messages, finalized.meta
            )
            output_text = finalized.output_text
        except Exception as e:
            logger.bind(ctx=ctx, action_run_id=action_result_doc.id).warning(
                f"Chat snapshot restore failed, marking cancelled without it: {e}"
            )
            snapshot = None

    output = DataBlocks(blocks=[DataBlock(text=output_text)]).model_dump(
        exclude_none=True, mode="json"
    )
    action_result_doc.result.output = output
    if action_result_doc.result.results:
        action_result_doc.result.results[-1].output = output

    if snapshot is None:
        logger.bind(ctx=ctx, action_run_id=action_result_doc.id).info(
            "Cancelled run has no chat snapshot to restore"
        )
        if notify:
            notify(ctx, notice)
        return False

    logger.bind(
        ctx=ctx,
        provider=snapshot.provider,
        loop_index=snapshot.loop_index,
        action_run_id=action_result_doc.id,
    ).info("Cancelled run restored from chat snapshot")
    if notify:
        notify(ctx, f"Cancelled — kept work through step {snapshot.loop_index}")
    return True
