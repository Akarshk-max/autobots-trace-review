import json

from loguru import logger
from pydantic import BaseModel
from redis.asyncio import Redis

from src.autobots.core.redis.async_pool import get_async_redis
from src.autobots.data_model.app_state import AppState

INSTRUCTION_TTL_SECONDS = 3600


class RunInstruction(BaseModel):
    text: str

    def as_user_message(self) -> str:
        return f"User instruction received while you were working: {self.text}"


def instruction_key(run_id: str) -> str:
    return f"run_instructions:{run_id}"


async def push_instruction(
    run_id: str, instruction: RunInstruction, redis_client: Redis | None = None
) -> None:
    redis_client = redis_client or await get_async_redis()
    key = instruction_key(run_id)
    async with redis_client.pipeline(transaction=True) as pipe:
        pipe.rpush(key, instruction.model_dump_json())
        pipe.expire(key, INSTRUCTION_TTL_SECONDS)
        await pipe.execute()


async def drain_instructions(
    app_state: AppState, redis_client: Redis | None = None
) -> list[RunInstruction]:
    """Read and clear the instructions waiting for this run. Never raises."""
    cancellation = app_state.cancellation
    if cancellation is None:
        return []
    key = instruction_key(cancellation.run_id)
    try:
        redis_client = redis_client or await get_async_redis()
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.lrange(key, 0, -1)
            pipe.delete(key)
            raw_items, _ = await pipe.execute()
    except Exception as e:
        logger.bind(run_id=cancellation.run_id).warning(f"Reading run instructions failed: {e}")
        return []

    instructions: list[RunInstruction] = []
    for raw in raw_items or []:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            instructions.append(RunInstruction.model_validate(json.loads(raw)))
        except Exception as e:
            logger.bind(run_id=cancellation.run_id).warning(f"Dropping bad run instruction: {e}")
    return instructions
