import asyncio
import json

import supabase_auth
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from loguru import logger

from src.autobots import SettingsProvider
from src.autobots.auth.providers.auth_provider_registry import get_auth_provider
from src.autobots.auth.security import get_offline_user_from_access_token
from src.autobots.core.redis.async_pool import get_async_redis
from src.autobots.terminal.pty_session_manager import pty_session_manager
from src.autobots.terminal.terminal_connection_manager import REDIS_PTY_OUT_PREFIX, terminal_manager

router = APIRouter(
    prefix=SettingsProvider.sget().API_TERMINAL,
    tags=[SettingsProvider.sget().API_TERMINAL],
)


def _publish_pty_event_fire_and_forget(session_id: str, event: dict) -> None:
    async def _do_publish():
        try:
            r = await get_async_redis()
            await r.publish(
                f"{REDIS_PTY_OUT_PREFIX}{session_id}",
                json.dumps(event),
            )
        except Exception as e:
            logger.warning(f"Redis publish error for PTY event on {session_id}: {e}")

    try:
        asyncio.create_task(_do_publish())
    except RuntimeError:
        pass


@router.websocket("/ws/connect")
async def terminal_ws_connect(ws: WebSocket):
    """WebSocket endpoint for CLI agents to connect and receive terminal commands."""
    await ws.accept()
    user_id = None
    agent_id = None

    try:
        auth_msg = await ws.receive_json()
        if auth_msg.get("type") != "auth" or not auth_msg.get("token"):
            await ws.send_json({"type": "error", "message": "First message must be auth"})
            await ws.close(code=4001)
            return

        if not auth_msg.get("agent_id"):
            await ws.send_json({"type": "error", "message": "agent_id is required"})
            await ws.close(code=4002)
            return

        try:
            user_res = await get_auth_provider().verify_token_online(auth_msg["token"])
            user_id = user_res.user.id
        except Exception:
            await ws.send_json({"type": "error", "message": "Invalid or expired token"})
            await ws.close(code=4003)
            return
        agent_id = str(auth_msg["agent_id"])

        info = await terminal_manager.connect(
            user_id=user_id,
            agent_id=agent_id,
            ws=ws,
            platform=auth_msg.get("platform"),
            mode=auth_msg.get("mode", "restricted"),
            allowed_dirs=auth_msg.get("allowed_dirs") or [],
        )


        # Optional deterministic pinning: if the CLI runtime supplies a consumer_id,
        # force the chat/run to pair with this specific agent (bypass FIFO).
        consumer_id = auth_msg.get("consumer_id")
        if consumer_id:
            try:
                _replaced_agent, _replaced_tail, err = await terminal_manager.pin_consumer_to_agent(
                    user_id=user_id,
                    consumer_id=str(consumer_id),
                    agent_id=agent_id,
                )
                if err == "conflict_pinned_alive":
                    await ws.send_json(
                        {
                            "type": "error",
                            "message": "consumer_id is already paired to a pinned, live CLI agent",
                        }
                    )
                    await ws.close(code=4090)
                    return
                if err:
                    logger.warning(
                        f"Terminal pin request failed: user_id={user_id}, agent_id={agent_id}, consumer_id={consumer_id}, err={err}"
                    )
            except Exception as e:
                logger.warning(
                    f"Terminal pin request error: user_id={user_id}, agent_id={agent_id}, consumer_id={consumer_id}, error={e}"
                )

        await ws.send_json(
            {
                "type": "auth_ok",
                "user_id": user_id,
                "agent_id": agent_id,
                "connected_at": info.connected_at,
            }
        )

        while True:
            msg = await ws.receive_json()
            msg_type = msg.get("type")

            if msg_type == "result":
                terminal_manager.resolve_response(
                    agent_id=agent_id,
                    request_id=msg.get("request_id", ""),
                    stdout=msg.get("stdout", ""),
                    stderr=msg.get("stderr", ""),
                    exit_code=msg.get("exit_code", -1),
                )

            elif msg_type == "fs_result":
                terminal_manager.resolve_request(
                    agent_id=agent_id,
                    request_id=msg.get("request_id", ""),
                    payload=msg,
                )

            elif msg_type == "pty_started":
                session_id = msg.get("session_id", "")
                cli_platform = msg.get("platform")
                cli_mode = msg.get("mode")
                cli_allowed_dirs = msg.get("allowed_dirs")
                await pty_session_manager.mark_started(
                    session_id,
                    platform=cli_platform,
                    mode=cli_mode,
                    allowed_dirs=cli_allowed_dirs,
                )
                _publish_pty_event_fire_and_forget(
                    session_id,
                    {
                        "type": "pty_started",
                        "platform": cli_platform,
                        "mode": cli_mode,
                        "allowed_dirs": cli_allowed_dirs,
                    },
                )

            elif msg_type == "pty_output":
                session_id = msg.get("session_id", "")
                data = msg.get("data", "")
                pty_session_manager.enqueue_output(session_id, data)
                _publish_pty_event_fire_and_forget(session_id, {"type": "pty_output", "data": data})

            elif msg_type == "pty_exited":
                session_id = msg.get("session_id", "")
                exit_code = msg.get("exit_code", -1)
                await pty_session_manager.mark_exited(session_id, exit_code)
                _publish_pty_event_fire_and_forget(
                    session_id, {"type": "pty_exited", "exit_code": exit_code}
                )

            elif msg_type == "pty_error":
                session_id = msg.get("session_id", "")
                error = msg.get("error", "Unknown error")
                await pty_session_manager.mark_error(session_id, error)
                _publish_pty_event_fire_and_forget(
                    session_id, {"type": "pty_error", "error": error}
                )

            elif msg_type == "read_files_result":
                terminal_manager.resolve_generic_response(
                    agent_id=agent_id,
                    request_id=msg.get("request_id", ""),
                    data={
                        "urls": msg.get("urls", []),
                        "errors": msg.get("errors", []),
                    },
                )

            elif msg_type == "pong":
                pass

            else:
                logger.warning(f"Unknown message type from terminal CLI: {msg_type}")

    except WebSocketDisconnect:
        logger.info(
            f"Terminal CLI agent WebSocket disconnected for user_id={user_id}, agent_id={agent_id}"
        )
    except Exception as e:
        logger.exception(f"Terminal WebSocket error: {str(e)}")
    finally:
        if agent_id:
            pty_session_manager.cleanup_agent(agent_id)
            await terminal_manager.disconnect(agent_id)


@router.get("/status")
async def terminal_status(
    user_res: supabase_auth.UserResponse = Depends(get_offline_user_from_access_token),
):
    """Debug endpoint: returns connected CLI agents and current pairings."""
    user_id = user_res.user.id
    return {"agents": await terminal_manager.get_connected_agents(user_id=user_id)}
