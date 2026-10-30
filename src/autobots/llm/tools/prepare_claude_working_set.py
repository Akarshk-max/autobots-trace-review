import json
from typing import List

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.action.action.common_action_models import DataBlock, DataBlocks
from src.autobots.data_model.context import Context
from src.autobots.server_sent_events.server_sent_events import Notify
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState

async def claude_container_file_attach(
    urls: List[str],
    reason: str | None = None,
    ctx: Context | None = None,
    app_state: AppState | None = None,
    user: UserORM | None = None,
    db: AsyncIOMotorDatabase | None = None,
    notify: Notify | None = None,
) -> DataBlocks | Exception:
    """
    Prepare selected conversation-known file URLs for Claude's working set.

    Use this tool when specific files need to be attached into Claude's current
    working container/code-execution environment. Pass only URLs already known
    in the conversation context.

    Args:
        urls: Conversation-known URLs to attach.
        reason: Optional short explanation for why these files are needed.

    Returns:
        DataBlocks with:
        - text: JSON summary of selected URLs
        - urls: the selected URLs, which Claude runtime can transform into
          container uploads / file attachments through ClaudeFilePipeline
    """
    selected_urls: list[str] = []
    seen: set[str] = set()

    for raw_url in urls or []:
        url = str(raw_url or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        selected_urls.append(url)

    payload = {
        "ok": True,
        "reason": reason,
        "selected_urls": selected_urls,
        "count": len(selected_urls),
    }
    return DataBlocks(
        blocks=[
            DataBlock(
                text=json.dumps(payload, indent=2, ensure_ascii=False),
                urls=selected_urls,
            )
        ]
    )