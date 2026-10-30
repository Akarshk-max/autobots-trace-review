import asyncio
import json
from datetime import datetime, timezone

import supabase_auth
from fastapi import APIRouter, Depends, Request
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from starlette.responses import StreamingResponse
from starlette.status import WS_1008_POLICY_VIOLATION
from starlette.websockets import WebSocket, WebSocketDisconnect

from src.autobots import SettingsProvider
from src.autobots.api.v1.api_server_sent_events.topic_access import (
    assert_topic_readable,
    may_read_topic,
)
from src.autobots.auth.providers.auth_provider_registry import get_auth_provider
from src.autobots.auth.security import (
    get_offline_user_from_access_token,
    get_online_admin_user_from_access_token,
)
from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.redis.async_pool import get_async_redis
from src.autobots.server_sent_events.server_sent_events import Notify

router = APIRouter(
    prefix=SettingsProvider.sget().API_SERVER_SENT_EVENTS,
    tags=[SettingsProvider.sget().API_SERVER_SENT_EVENTS],
)

HEARTBEAT_INTERVAL_SECONDS = 20

STREAM_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def decode_message(message: dict) -> str:
    data = message["data"]
    return data.decode("utf-8") if isinstance(data, bytes) else data


def sse_frame(data: str) -> str:
    """Frame a payload as one SSE event, prefixing every line it contains."""
    return "".join(f"data: {line}\n" for line in data.split("\n")) + "\n"


async def authenticate_websocket(websocket: WebSocket) -> supabase_auth.UserResponse | None:
    raw = websocket.query_params.get("token") or websocket.query_params.get("Authorization")
    if not raw:
        return None
    token = raw.removeprefix("Bearer ").strip()
    try:
        return await get_auth_provider().verify_token_offline(token, audience="authenticated")
    except Exception as e:
        logger.bind(e=str(e)).info("[WS] Token verification failed")
        return None


async def wait_for_disconnect(websocket: WebSocket) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


@router.get("/stream/{topic}")
async def stream_sse(
    request: Request,
    topic: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    ctx = request.state.context
    # Before the generator: a stream that opens and then closes reads as a dropped
    # connection, and the client reconnects every 2s forever.
    await assert_topic_readable(topic, user_res, db)
    logger.bind(ctx=ctx).info(f"Client connecting to topic '{topic}'")

    async def event_generator():
        r = await get_async_redis()
        pubsub = r.pubsub()
        await pubsub.subscribe(topic)
        sent = 0

        try:
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=HEARTBEAT_INTERVAL_SECONDS
                )
                if message is None:
                    yield ":keep-alive\n\n"
                    continue

                sent += 1
                yield sse_frame(decode_message(message))

        except asyncio.CancelledError as e:
            logger.bind(ctx=ctx, e=str(e)).info(
                f"Client for topic '{topic}' disconnected. Cleaning up..."
            )
            raise

        finally:
            logger.bind(ctx=ctx, topic=topic, messages_sent=sent).info("Closing pubsub")
            await pubsub.close()

    return StreamingResponse(
        event_generator(), media_type="text/event-stream", headers=STREAM_HEADERS
    )


@router.get("/streaming/{topic}")
async def streaming_ndjson(
    request: Request,
    topic: str,
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    ctx = request.state.context
    await assert_topic_readable(topic, user_res, db)
    logger.bind(ctx=ctx).info(f"Client (NDJSON) connecting to topic '{topic}'")

    async def event_generator():
        r = await get_async_redis()
        pubsub = r.pubsub()
        await pubsub.subscribe(topic)
        sent = 0

        try:
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=HEARTBEAT_INTERVAL_SECONDS
                )
                if message is None:
                    ping = {"type": "ping", "ts": datetime.now(timezone.utc).isoformat()}
                    yield (json.dumps(ping) + "\n").encode("utf-8")
                    continue

                sent += 1
                payload = {"topic": topic, "data": decode_message(message)}
                yield (json.dumps(payload) + "\n").encode("utf-8")

        except asyncio.CancelledError as e:
            logger.bind(ctx=ctx, e=str(e)).info(
                f"NDJSON client for '{topic}' disconnected. Cleaning up..."
            )
            raise
        finally:
            logger.bind(ctx=ctx, topic=topic, messages_sent=sent).info("[NDJSON] Closing pubsub")
            await pubsub.close()

    return StreamingResponse(
        event_generator(), media_type="application/x-ndjson", headers=STREAM_HEADERS
    )


@router.websocket("/ws/{topic}")
async def websocket_stream(
    websocket: WebSocket,
    topic: str,
    db: AsyncIOMotorDatabase = Depends(get_mongo_db),
):
    user_res = await authenticate_websocket(websocket)
    if user_res is None:
        logger.info(f"[WS] Rejected unauthenticated client on topic '{topic}'")
        await websocket.close(code=WS_1008_POLICY_VIOLATION)
        return

    # Closed rather than raised: the HTTP paths answer 404, but there is no
    # response to write here, and the line above already speaks this protocol.
    if not await may_read_topic(topic, user_res, db):
        logger.info(f"[WS] Rejected client on topic '{topic}' they may not read")
        await websocket.close(code=WS_1008_POLICY_VIOLATION)
        return


    await websocket.accept()
    client = f"{websocket.client.host}:{websocket.client.port}"
    logger.info(f"[WS] Client {client} connecting to topic '{topic}'")

    r = await get_async_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe(topic)

    async def forward_messages() -> None:
        while True:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=HEARTBEAT_INTERVAL_SECONDS
            )
            if message is None:
                continue

            await websocket.send_text(decode_message(message))

    forwarder = asyncio.create_task(forward_messages())
    disconnect = asyncio.create_task(wait_for_disconnect(websocket))

    try:
        done, pending = await asyncio.wait(
            {forwarder, disconnect}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()

    except WebSocketDisconnect:
        logger.info(f"[WS] Client {client} disconnected from topic '{topic}'")

    except asyncio.CancelledError as e:
        logger.bind(e=str(e)).info(f"[WS] Task cancelled for client {client} on '{topic}'")
        forwarder.cancel()
        disconnect.cancel()
        raise

    finally:
        logger.info("[WS] Closing pubsub and websocket")
        await pubsub.close()
        try:
            await websocket.close()
        except Exception:
            pass


@router.post("/test_trigger/{topic}")
async def trigger(
    request: Request,
    topic: str,
    _: supabase_auth.UserResponse = Depends(get_online_admin_user_from_access_token),
):
    """Publish five test frames to a topic.

    Anyone at all could reach this: it took no dependency, so an unauthenticated
    caller could publish to any topic, and a run id is an ObjectId rather than a
    secret. Whoever was watching that run saw frames the run never produced.
    """
    ctx = request.state.context
    notify = Notify(topic)
    for i in range(5):
        notify(ctx, f"Test message {i + 1} at {datetime.now(timezone.utc).isoformat()}")
        await asyncio.sleep(1)
    return {"message": f"Triggered events on topic '{topic}'"}
