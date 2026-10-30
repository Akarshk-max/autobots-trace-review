import asyncio
import copy
import json
import time
import uuid
from typing import Any, Dict

from fastapi import WebSocket
from loguru import logger
from pydantic import BaseModel, Field

from src.autobots.core.redis.async_pool import get_async_redis
from src.autobots.data_model.context import Context

# Unique ID for this worker instance
_INSTANCE_ID = str(uuid.uuid4())


async def _r():
    """Return shared async Redis client for terminal operations."""
    return await get_async_redis()


REDIS_CONNECTED_AGENTS_SET = "terminal:connected_agents"
REDIS_USER_AGENTS_PREFIX = "terminal:user_agents:"
REDIS_AGENT_INFO_PREFIX = "terminal:agent:"
REDIS_AGENT_HEARTBEAT_PREFIX = "terminal:heartbeat:agent:"
REDIS_AVAILABLE_AGENTS_PREFIX = "terminal:available_agents:"
REDIS_PAIR_CONSUMER_PREFIX = "terminal:pair:consumer:"
REDIS_PAIR_AGENT_PREFIX = "terminal:pair:agent:"
REDIS_CMD_AGENT_PREFIX = "terminal:cmd:agent:"
REDIS_RES_PREFIX = "terminal:res:"
REDIS_PTY_OUT_PREFIX = "terminal:pty_out:"
REDIS_PINNED_AGENT_PREFIX = "terminal:pinned_agent:"


HEARTBEAT_TTL = 30
HEARTBEAT_INTERVAL = 15
PAIRING_TTL = 86400  # 24 hours


def _user_agents_key(user_id: str) -> str:
    return f"{REDIS_USER_AGENTS_PREFIX}{user_id}"


def _agent_info_key(agent_id: str) -> str:
    return f"{REDIS_AGENT_INFO_PREFIX}{agent_id}"


def _agent_heartbeat_key(agent_id: str) -> str:
    return f"{REDIS_AGENT_HEARTBEAT_PREFIX}{agent_id}"


def _available_agents_key(user_id: str) -> str:
    return f"{REDIS_AVAILABLE_AGENTS_PREFIX}{user_id}"


def _pair_consumer_key(user_id: str, consumer_id: str) -> str:
    return f"{REDIS_PAIR_CONSUMER_PREFIX}{user_id}:{consumer_id}"


def _pair_agent_key(agent_id: str) -> str:
    return f"{REDIS_PAIR_AGENT_PREFIX}{agent_id}"

def _pinned_agent_key(agent_id: str) -> str:
    return f"{REDIS_PINNED_AGENT_PREFIX}{agent_id}"



def _cmd_agent_channel(agent_id: str) -> str:
    return f"{REDIS_CMD_AGENT_PREFIX}{agent_id}"


_CLAIM_OLDEST_AVAILABLE_AGENT_LUA = """
local consumer_key = KEYS[1]
local available_key = KEYS[2]
local pair_agent_prefix = ARGV[1]
local ttl = tonumber(ARGV[2])

local existing = redis.call("GET", consumer_key)
if existing then
  redis.call("EXPIRE", consumer_key, ttl)
  local agent_pair_key = pair_agent_prefix .. existing
  redis.call("EXPIRE", agent_pair_key, ttl)
  return {"existing", existing}
end

local candidates = redis.call("ZRANGE", available_key, 0, -1)
for _, agent_id in ipairs(candidates) do
  local agent_pair_key = pair_agent_prefix .. agent_id
  local already_paired = redis.call("GET", agent_pair_key)
  if not already_paired then
    redis.call("SET", consumer_key, agent_id, "EX", ttl)
    redis.call("SET", agent_pair_key, string.sub(consumer_key, string.len("terminal:pair:consumer:") + 1), "EX", ttl)
    redis.call("ZREM", available_key, agent_id)
    return {"assigned", agent_id}
  else
    -- self-heal stale "available" entries for already-paired agents
    redis.call("ZREM", available_key, agent_id)
  end
end

return {"none", ""}
"""

_PIN_AGENT_FOR_CONSUMER_LUA = """
local consumer_key = KEYS[1]
local available_key = KEYS[2]
local pair_agent_key_new = KEYS[3]

local agent_id = ARGV[1]
local desired_tail = ARGV[2]
local ttl = tonumber(ARGV[3])
local pair_agent_prefix = ARGV[4]
local heartbeat_prefix = ARGV[5]
local consumer_prefix = ARGV[6]
local pinned_prefix = ARGV[7]
local agent_info_prefix = ARGV[8]

-- If this agent is already paired to a different consumer, delete that consumer's pairing.
local existing_tail = redis.call("GET", pair_agent_key_new)
if existing_tail and existing_tail ~= desired_tail then
  redis.call("DEL", consumer_prefix .. existing_tail)
end

-- If this consumer is already paired to a different agent, release it only when:
-- - the existing agent is not pinned, OR
-- - the existing agent is not alive (no heartbeat).
local existing_agent = redis.call("GET", consumer_key)
if existing_agent and existing_agent ~= agent_id then
  local hb = redis.call("GET", heartbeat_prefix .. existing_agent)
  local pinned_existing = redis.call("GET", pinned_prefix .. existing_agent)

  if hb and pinned_existing then
    -- A pinned, live agent already owns this consumer. Do not override.
    -- Also ensure this new agent doesn't get claimed via FIFO while we disconnect it.
    redis.call("ZREM", available_key, agent_id)
    return {"conflict_pinned_alive", existing_agent, pinned_existing}
  end

  -- Safe to release old pairing.
  redis.call("DEL", pair_agent_prefix .. existing_agent)
  redis.call("DEL", pinned_prefix .. existing_agent)
  if hb and (not pinned_existing) then
    local score = nil
    local raw_info = redis.call("GET", agent_info_prefix .. existing_agent)
    if raw_info then
      local ok, info = pcall(cjson.decode, raw_info)
      if ok and info and info["connected_at"] then
        score = tonumber(info["connected_at"])
      end
    end
    if not score then
      score = tonumber(redis.call("TIME")[1])
    end
    redis.call("ZADD", available_key, score, existing_agent)
  else
    redis.call("ZREM", available_key, existing_agent)
  end
end

redis.call("SET", consumer_key, agent_id, "EX", ttl)
redis.call("SET", pair_agent_key_new, desired_tail, "EX", ttl)
redis.call("SET", pinned_prefix .. agent_id, desired_tail, "EX", ttl)
redis.call("ZREM", available_key, agent_id)

return {"ok", existing_agent or "", existing_tail or ""}
"""



class AgentConnInfo(BaseModel):
    agent_id: str
    user_id: str
    instance_id: str
    connected_at: float
    platform: str | None = None
    mode: str = "restricted"
    allowed_dirs: list[str] = Field(default_factory=list)


class RemoteWebSocketProxy:
    """Duck-types WebSocket.send_json() by publishing to Redis instead.

    This allows server code to transparently send messages to a CLI agent
    connected to a different worker instance.
    """

    def __init__(self, agent_id: str):
        self.agent_id = agent_id

    async def send_json(self, data: Any) -> None:
        channel = _cmd_agent_channel(self.agent_id)
        payload = json.dumps({
            "type": "relay_ws_send",
            "data": data,
            "instance_id": _INSTANCE_ID,
        })
        r = await _r()
        await r.publish(channel, payload)


class TerminalConnectionManager:
    """Singleton that tracks many CLI agents per user and sticky chat pairings."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            # agent_id -> WebSocket
            cls._instance._agent_connections: Dict[str, WebSocket] = {}
            # agent_id -> AgentConnInfo
            cls._instance._agent_info: Dict[str, AgentConnInfo] = {}
            # user_id -> set[agent_id]
            cls._instance._user_agents: Dict[str, set[str]] = {}
            # agent_id -> {request_id -> Future}
            cls._instance._pending_futures: Dict[str, Dict[str, asyncio.Future]] = {}
            # agent_id -> list[Task]
            cls._instance._background_tasks: Dict[str, list[asyncio.Task]] = {}
        return cls._instance

    def resolve_consumer_id(self, ctx: Context) -> str:
        """Return the stable per-chat identifier used for sticky pairing."""
        for attr in ("consumer_id", "conversation_id", "chat_id", "session_id"):
            value = getattr(ctx, attr, None)
            if value is None:
                continue
            value = str(value).strip()
            if value:
                return value

        raise ValueError(
            "A stable chat/consumer identifier is required for terminal pairing. "
            "Set ctx.consumer_id (preferred) or ctx.conversation_id/chat_id/session_id."
        )

    async def connect(
        self,
        user_id: str,
        agent_id: str,
        ws: WebSocket,
        *,
        platform: str | None = None,
        mode: str = "restricted",
        allowed_dirs: list[str] | None = None,
    ) -> AgentConnInfo:
        """Register a new CLI agent connection."""
        if agent_id in self._agent_connections:
            logger.info(f"Replacing existing agent connection for agent_id={agent_id}")
            await self.disconnect(agent_id)

        info = AgentConnInfo(
            agent_id=agent_id,
            user_id=user_id,
            instance_id=_INSTANCE_ID,
            connected_at=time.time(),
            platform=platform,
            mode=mode,
            allowed_dirs=allowed_dirs or [],
        )

        self._agent_connections[agent_id] = ws
        self._agent_info[agent_id] = info
        self._user_agents.setdefault(user_id, set()).add(agent_id)
        self._pending_futures[agent_id] = {}

        r = await _r()
        pipe = r.pipeline()
        pipe.sadd(REDIS_CONNECTED_AGENTS_SET, agent_id)
        pipe.sadd(_user_agents_key(user_id), agent_id)
        pipe.set(_agent_info_key(agent_id), info.model_dump_json())
        pipe.zadd(_available_agents_key(user_id), {agent_id: info.connected_at})
        pipe.set(_agent_heartbeat_key(agent_id), _INSTANCE_ID)
        pipe.expire(_agent_heartbeat_key(agent_id), HEARTBEAT_TTL)
        await pipe.execute()

        cmd_task = asyncio.create_task(self._command_listener(agent_id))
        hb_task = asyncio.create_task(self._heartbeat_loop(agent_id))
        self._background_tasks[agent_id] = [cmd_task, hb_task]

        logger.info(
            f"Terminal CLI agent connected: user_id={user_id}, agent_id={agent_id}, "
            f"mode={mode}, instance={_INSTANCE_ID}"
        )
        return info

    async def disconnect(self, agent_id: str) -> None:
        """Remove one CLI agent and release its pairing."""
        info = self._agent_info.get(agent_id)
        if info is None:
            info = await self.get_agent_info(agent_id)

        for task in self._background_tasks.pop(agent_id, []):
            task.cancel()

        self._agent_connections.pop(agent_id, None)
        self._cancel_pending(agent_id)
        self._pending_futures.pop(agent_id, None)

        if info is not None:
            user_agents = self._user_agents.get(info.user_id)
            if user_agents is not None:
                user_agents.discard(agent_id)
                if not user_agents:
                    self._user_agents.pop(info.user_id, None)

            await self._delete_pairing_for_agent(
                user_id=info.user_id,
                agent_id=agent_id,
                requeue=False,
            )

            r = await _r()
            pipe = r.pipeline()
            pipe.srem(REDIS_CONNECTED_AGENTS_SET, agent_id)
            pipe.srem(_user_agents_key(info.user_id), agent_id)
            pipe.zrem(_available_agents_key(info.user_id), agent_id)
            pipe.delete(_agent_info_key(agent_id))
            pipe.delete(_agent_heartbeat_key(agent_id))
            pipe.delete(_pair_agent_key(agent_id))
            pipe.delete(_pinned_agent_key(agent_id))
            await pipe.execute()

        self._agent_info.pop(agent_id, None)

        logger.info(f"Terminal CLI agent disconnected: agent_id={agent_id}")

    async def get_ws(self, agent_id: str) -> WebSocket | RemoteWebSocketProxy | None:
        """Return the WebSocket for one agent (local or remote proxy), or None."""
        ws = self._agent_connections.get(agent_id)
        if ws is not None:
            return ws

        try:
            r = await _r()
            heartbeat = await r.get(_agent_heartbeat_key(agent_id))
            if heartbeat is not None:
                return RemoteWebSocketProxy(agent_id)
        except Exception as e:
            logger.warning(f"Redis error in get_ws for agent_id={agent_id}: {e}")

        return None

    async def is_agent_connected(self, agent_id: str) -> bool:
        if agent_id in self._agent_connections:
            return True
        r = await _r()
        hb = await r.get(_agent_heartbeat_key(agent_id))
        return hb is not None

    async def get_agent_info(self, agent_id: str) -> AgentConnInfo | None:
        local = self._agent_info.get(agent_id)
        if local is not None:
            return local

        r = await _r()
        raw = await r.get(_agent_info_key(agent_id))
        if raw is None:
            return None

        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            info = AgentConnInfo.model_validate_json(raw)
            self._agent_info[agent_id] = info
            self._user_agents.setdefault(info.user_id, set()).add(agent_id)
            return info
        except Exception as e:
            logger.warning(f"Failed to parse agent info for agent_id={agent_id}: {e}")
            return None

    async def resolve_agent_for_consumer(self, ctx: Context) -> str | None:
        """Sticky pairing:
        - reuse existing paired agent if alive
        - otherwise assign oldest unpaired agent for this user
        """
        user_id = ctx.user_id
        consumer_id = self.resolve_consumer_id(ctx)
        r = await _r()

        consumer_key = _pair_consumer_key(user_id, consumer_id)
        existing_agent = await r.get(consumer_key)
        if isinstance(existing_agent, bytes):
            existing_agent = existing_agent.decode("utf-8")

        if existing_agent:
            if await self.is_agent_connected(existing_agent):
                await self._notify_paired(existing_agent, consumer_id, ctx)
                try:
                    await self._refresh_pairing_ttl(
                        user_id=user_id, consumer_id=consumer_id, agent_id=existing_agent
                    )
                except Exception as e:
                    logger.debug(f"Failed to refresh pairing TTL: {e}")
                return existing_agent

            await self._delete_pairing_for_consumer(
                user_id=user_id,
                consumer_id=consumer_id,
                expected_agent_id=existing_agent,
            )
            await self._purge_stale_agent(existing_agent, user_id_hint=user_id)

        for _ in range(64):
            result = await r.eval(
                _CLAIM_OLDEST_AVAILABLE_AGENT_LUA,
                2,
                consumer_key,
                _available_agents_key(user_id),
                REDIS_PAIR_AGENT_PREFIX,
                PAIRING_TTL,
            )

            if not result:
                return None

            status = result[0].decode("utf-8") if isinstance(result[0], bytes) else str(result[0]) if result else "none"
            agent_id = result[1].decode("utf-8") if isinstance(result[1], bytes) else str(result[1]) if len(result) > 1 and result[1] else None

            if status == "none" or not agent_id:
                return None

            if await self.is_agent_connected(agent_id):
                logger.info(
                    f"Resolved terminal agent for user_id={user_id}, "
                    f"consumer_id={consumer_id}, agent_id={agent_id}, status={status}"
                )
                await self._notify_paired(agent_id, consumer_id, ctx)
                return agent_id

            # Claimed a dead/stale agent -> clean it and try again
            await self._delete_pairing_for_consumer(
                user_id=user_id,
                consumer_id=consumer_id,
                expected_agent_id=agent_id,
            )
            await self._purge_stale_agent(agent_id, user_id_hint=user_id)

        return None

    async def pin_consumer_to_agent(
        self, *, user_id: str, consumer_id: str, agent_id: str
    ) -> tuple[str | None, str | None, str | None]:
        """Explicitly pin a consumer/chat/run to a specific CLI agent.

        Used when the CLI runtime connects with an explicit consumer_id (e.g.,
        kiwi-runtime started with --runID). This bypasses FIFO agent selection and
        ensures deterministic pairing.

        Conflict policy (ideal): if the consumer is already paired to a pinned, live
        agent, do NOT override it. In that case return error="conflict_pinned_alive"
        so the caller can reject the new pin attempt.

        Returns: (replaced_agent_id, replaced_consumer_tail, error_message)
        """
        cid = str(consumer_id or "").strip()
        if not cid:
            return None, None, "missing consumer_id"

        consumer_key = _pair_consumer_key(user_id, cid)
        available_key = _available_agents_key(user_id)
        pair_agent_key_new = _pair_agent_key(agent_id)
        desired_tail = f"{user_id}:{cid}"

        r = await _r()
        result = await r.eval(
            _PIN_AGENT_FOR_CONSUMER_LUA,
            3,
            consumer_key,
            available_key,
            pair_agent_key_new,
            agent_id,
            desired_tail,
            PAIRING_TTL,
            REDIS_PAIR_AGENT_PREFIX,
            REDIS_AGENT_HEARTBEAT_PREFIX,
            REDIS_PAIR_CONSUMER_PREFIX,
            REDIS_PINNED_AGENT_PREFIX,
            REDIS_AGENT_INFO_PREFIX,
        )

        if not result:
            return None, None, "pin eval returned empty"

        def _decode(v):
            if v is None:
                return None
            if isinstance(v, bytes):
                return v.decode("utf-8")
            return str(v)

        status = _decode(result[0]) if len(result) > 0 else None
        if status == "conflict_pinned_alive":
            existing_agent = _decode(result[1]) if len(result) > 1 else None
            pinned_tail = _decode(result[2]) if len(result) > 2 else None
            logger.info(
                f"Pin rejected (pinned+alive already owns consumer): user_id={user_id}, consumer_id={cid}, "
                f"agent_id={agent_id}, existing_agent={existing_agent}, existing_tail={pinned_tail}"
            )
            return existing_agent, pinned_tail, "conflict_pinned_alive"

        if status != "ok":
            return None, None, f"unexpected pin status: {status}"

        replaced_agent = _decode(result[1]) if len(result) > 1 else None
        replaced_tail = _decode(result[2]) if len(result) > 2 else None
        logger.info(
            f"Pinned terminal agent: user_id={user_id}, consumer_id={cid}, agent_id={agent_id}, "
            f"replaced_agent={replaced_agent}, replaced_tail={replaced_tail}"
        )
        return replaced_agent, replaced_tail, None



    async def _refresh_pairing_ttl(self, *, user_id: str, consumer_id: str, agent_id: str) -> None:
        consumer_key = _pair_consumer_key(user_id, consumer_id)
        pair_agent_key = _pair_agent_key(agent_id)
        pinned_key = _pinned_agent_key(agent_id)

        r = await _r()
        pipe = r.pipeline()
        pipe.expire(consumer_key, PAIRING_TTL)
        pipe.expire(pair_agent_key, PAIRING_TTL)
        pipe.expire(pinned_key, PAIRING_TTL)
        await pipe.execute()


    async def _notify_paired(
        self, agent_id: str, consumer_id: str, ctx: Context
    ) -> None:
        """Send a 'paired' notification to the CLI agent (fire-and-forget)."""
        try:
            ws = await self.get_ws(agent_id)
            if ws is None:
                return
            await ws.send_json({
                "type": "paired",
                "consumer_id": consumer_id,
                "action_run_name": ctx.action_run_name,
            })
        except Exception as e:
            logger.debug(f"Failed to send paired notification to agent {agent_id}: {e}")

    async def release_consumer_pairing(
        self,
        user_id: str,
        consumer_id: str,
    ) -> str | None:
        """Manual release hook. Requeues the agent if it is still connected."""
        consumer_key = _pair_consumer_key(user_id, consumer_id)
        r = await _r()
        agent_id = await r.get(consumer_key)
        if isinstance(agent_id, bytes):
            agent_id = agent_id.decode("utf-8")
        if not agent_id:
            return None

        info = await self.get_agent_info(agent_id)
        connected = await self.is_agent_connected(agent_id)

        pipe = r.pipeline()
        pipe.delete(consumer_key)
        pipe.delete(_pair_agent_key(agent_id))
        pipe.delete(_pinned_agent_key(agent_id))
        if connected and info is not None:
            pipe.zadd(_available_agents_key(user_id), {agent_id: info.connected_at})
        await pipe.execute()
        logger.info(
            f"Released terminal pairing: user_id={user_id}, consumer_id={consumer_id}, "
            f"agent_id={agent_id}, requeued={connected and info is not None}"
        )
        return agent_id

    async def get_connected_agents(self, user_id: str | None = None) -> list[dict]:
        r = await _r()
        if user_id:
            raw_ids = await r.smembers(_user_agents_key(user_id))
        else:
            raw_ids = await r.smembers(REDIS_CONNECTED_AGENTS_SET)

        if not raw_ids:
            return []

        agent_ids = [
            item.decode("utf-8") if isinstance(item, bytes) else str(item)
            for item in raw_ids
        ]
        results = []

        for agent_id in sorted(agent_ids):
            info = await self.get_agent_info(agent_id)
            if info is None:
                continue

            paired_consumer = await r.get(_pair_agent_key(agent_id))
            if isinstance(paired_consumer, bytes):
                paired_consumer = paired_consumer.decode("utf-8")

            results.append({
                "agent_id": info.agent_id,
                "user_id": info.user_id,
                "connected_at": info.connected_at,
                "platform": info.platform,
                "mode": info.mode,
                "allowed_dirs": info.allowed_dirs,
                "paired_consumer": paired_consumer,
                "connected": await self.is_agent_connected(agent_id),
                "local_to_this_worker": agent_id in self._agent_connections,
            })

        return sorted(results, key=lambda x: x["connected_at"])

    async def send_command_to_agent(
        self, ctx: Context, agent_id: str, command: str, timeout_sec: int = 120,
        msg_type: str = "command",
    ) -> dict:

        command_timeout_sec = timeout_sec
        response_timeout_sec = timeout_sec + 5

        return await self.send_ws_request_to_agent(
            ctx=ctx,
            agent_id=agent_id,
            message={
                "type": msg_type,
                "request_id": ctx.trace_id,
                "command": command,
                "timeout_sec": command_timeout_sec,
            },
            timeout_sec=response_timeout_sec,
        )

    async def send_read_files_to_agent(
        self, ctx: Context, agent_id: str, paths: list[str], timeout_sec: int = 300
    ) -> dict:
        return await self.send_ws_request_to_agent(
            ctx=ctx,
            agent_id=agent_id,
            message={
                "type": "read_files",
                "request_id": ctx.trace_id,
                "paths": paths,
            },
            timeout_sec=timeout_sec,
        )

    async def send_ws_request_to_agent(
        self, ctx: Context, agent_id: str, message: dict, timeout_sec: int = 120
    ) -> dict:
        """Send an arbitrary JSON request to one specific CLI agent."""
        if not await self.is_agent_connected(agent_id):
            return {
                "success": False,
                "message": "Assigned terminal agent is no longer connected.",
            }

        request = copy.deepcopy(message)
        request_id = f"{request.get('request_id') or ctx.trace_id}:{uuid.uuid4().hex}"
        request["request_id"] = request_id

        if agent_id in self._agent_connections:
            return await self._send_ws_request_local(
                ctx=ctx,
                agent_id=agent_id,
                request=request,
                timeout_sec=timeout_sec,
            )

        return await self._send_ws_request_remote(
            ctx=ctx,
            agent_id=agent_id,
            request=request,
            timeout_sec=timeout_sec,
        )

    async def _send_ws_request_local(
        self, ctx: Context, agent_id: str, request: dict, timeout_sec: int
    ) -> dict:
        request_id = request["request_id"]
        ws = self._agent_connections[agent_id]
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_futures.setdefault(agent_id, {})[request_id] = future

        try:
            await ws.send_json(request)
            logger.bind(ctx=ctx).info(
                f"Sent WS request to agent_id={agent_id}: "
                f"type={request.get('type')} request_id={request_id}"
            )
            return await asyncio.wait_for(future, timeout=timeout_sec)
        except asyncio.TimeoutError:
            logger.bind(ctx=ctx).warning(
                f"WS request timed out after {timeout_sec}s for agent_id={agent_id}, "
                f"type={request.get('type')}, request_id={request_id}"
            )
            return {
                "success": False,
                "message": f"Request timed out after {timeout_sec} seconds",
            }
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error sending WS request: {str(e)}")
            return {"success": False, "message": str(e)}
        finally:
            self._pending_futures.get(agent_id, {}).pop(request_id, None)

    async def _send_ws_request_remote(
        self, ctx: Context, agent_id: str, request: dict, timeout_sec: int
    ) -> dict:
        request_id = request["request_id"]
        res_channel = f"{REDIS_RES_PREFIX}{request_id}"
        r = await _r()
        pubsub = r.pubsub()

        try:
            await pubsub.subscribe(res_channel)

            payload = json.dumps({
                "type": "relay_request",
                "request": request,
                "response_channel": res_channel,
                "timeout_sec": timeout_sec,
            })

            await r.publish(_cmd_agent_channel(agent_id), payload)

            logger.bind(ctx=ctx).info(
                f"Sent remote WS request to agent_id={agent_id}: "
                f"type={request.get('type')} request_id={request_id}"
            )

            deadline = asyncio.get_event_loop().time() + timeout_sec
            while asyncio.get_event_loop().time() < deadline:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg["type"] == "message":
                    data = msg["data"]
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    return json.loads(data)
                await asyncio.sleep(0.05)

            return {
                "success": False,
                "message": f"Request timed out after {timeout_sec} seconds",
            }
        except Exception as e:
            logger.bind(ctx=ctx).exception(f"Error in remote WS request: {str(e)}")
            return {"success": False, "message": str(e)}
        finally:
            try:
                await pubsub.unsubscribe(res_channel)
                await pubsub.close()
            except Exception:
                pass

    def resolve_request(self, agent_id: str, request_id: str, payload: dict) -> None:
        futures = self._pending_futures.get(agent_id, {})
        future = futures.get(request_id)
        if future and not future.done():
            future.set_result(payload)
        else:
            logger.warning(
                f"No pending future for request_id={request_id}, agent_id={agent_id}"
            )

    def resolve_response(
        self, agent_id: str, request_id: str, stdout: str, stderr: str, exit_code: int
    ) -> None:
        self.resolve_request(
            agent_id=agent_id,
            request_id=request_id,
            payload={
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
            },
        )

    def resolve_generic_response(self, agent_id: str, request_id: str, data: dict) -> None:
        self.resolve_request(agent_id=agent_id, request_id=request_id, payload=data)

    async def _command_listener(self, agent_id: str) -> None:
        r = await _r()
        pubsub = r.pubsub()
        channel = _cmd_agent_channel(agent_id)

        try:
            await pubsub.subscribe(channel)
            logger.info(f"Command listener started for agent_id={agent_id} on {channel}")

            while agent_id in self._agent_connections:
                try:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                except Exception:
                    continue

                if msg and msg["type"] == "message":
                    data = msg["data"]
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    await self._handle_relayed_message(agent_id, json.loads(data))
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f"Command listener error for agent_id={agent_id}: {e}")
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.close()
            except Exception:
                pass
            logger.info(f"Command listener stopped for agent_id={agent_id}")

    async def _handle_relayed_message(self, agent_id: str, payload: dict) -> None:
        ws = self._agent_connections.get(agent_id)
        if ws is None:
            return

        msg_type = payload.get("type")

        if msg_type == "relay_request":
            request = payload["request"]
            request_id = request["request_id"]
            response_channel = payload["response_channel"]
            relay_timeout = payload.get("timeout_sec", 120)

            future: asyncio.Future = asyncio.get_event_loop().create_future()
            self._pending_futures.setdefault(agent_id, {})[request_id] = future

            try:
                await ws.send_json(request)
                result = await asyncio.wait_for(future, timeout=relay_timeout)
                r = await _r()
                await r.publish(response_channel, json.dumps(result))
            except asyncio.TimeoutError:
                r = await _r()
                await r.publish(
                    response_channel,
                    json.dumps({
                        "success": False,
                        "message": "Request timed out on relay",
                    }),
                )
            except Exception as e:
                r = await _r()
                await r.publish(
                    response_channel,
                    json.dumps({
                        "success": False,
                        "message": str(e),
                    }),
                )
            finally:
                self._pending_futures.get(agent_id, {}).pop(request_id, None)

        elif msg_type == "relay_ws_send":
            data = payload.get("data")
            if data:
                await ws.send_json(data)

    async def _heartbeat_loop(self, agent_id: str) -> None:
        try:
            while agent_id in self._agent_connections:
                r = await _r()
                await r.set(_agent_heartbeat_key(agent_id), _INSTANCE_ID)
                await r.expire(_agent_heartbeat_key(agent_id), HEARTBEAT_TTL)
                await asyncio.sleep(HEARTBEAT_INTERVAL)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f"Heartbeat loop error for agent_id={agent_id}: {e}")

    async def _delete_pairing_for_consumer(
        self,
        user_id: str,
        consumer_id: str,
        expected_agent_id: str | None = None,
    ) -> None:
        consumer_key = _pair_consumer_key(user_id, consumer_id)
        r = await _r()
        agent_id = await r.get(consumer_key)
        if isinstance(agent_id, bytes):
            agent_id = agent_id.decode("utf-8")
        if not agent_id:
            return
        if expected_agent_id is not None and agent_id != expected_agent_id:
            return

        pipe = r.pipeline()
        pipe.delete(consumer_key)
        pipe.delete(_pair_agent_key(agent_id))
        pipe.delete(_pinned_agent_key(agent_id))
        await pipe.execute()

    async def _delete_pairing_for_agent(
        self,
        user_id: str,
        agent_id: str,
        *,
        requeue: bool,
    ) -> None:
        r = await _r()
        consumer_key_tail = await r.get(_pair_agent_key(agent_id))
        if isinstance(consumer_key_tail, bytes):
            consumer_key_tail = consumer_key_tail.decode("utf-8")
        if not consumer_key_tail:
            return

        try:
            stored_user_id, consumer_id = consumer_key_tail.split(":", 1)
        except ValueError:
            stored_user_id = user_id
            consumer_id = consumer_key_tail

        if stored_user_id != user_id:
            consumer_id = consumer_key_tail.split(":", 1)[-1]

        info = await self.get_agent_info(agent_id)
        connected = await self.is_agent_connected(agent_id)

        pipe = r.pipeline()
        pipe.delete(_pair_consumer_key(stored_user_id, consumer_id))
        pipe.delete(_pair_agent_key(agent_id))
        pipe.delete(_pinned_agent_key(agent_id))
        if requeue and connected and info is not None:
            pipe.zadd(_available_agents_key(user_id), {agent_id: info.connected_at})
        await pipe.execute()

    async def _purge_stale_agent(self, agent_id: str, user_id_hint: str | None = None) -> None:
        info = await self.get_agent_info(agent_id)
        user_id = user_id_hint or (info.user_id if info else None)

        r = await _r()
        pipe = r.pipeline()
        pipe.srem(REDIS_CONNECTED_AGENTS_SET, agent_id)
        if user_id:
            pipe.srem(_user_agents_key(user_id), agent_id)
            pipe.zrem(_available_agents_key(user_id), agent_id)
        pipe.delete(_agent_info_key(agent_id))
        pipe.delete(_agent_heartbeat_key(agent_id))
        pipe.delete(_pair_agent_key(agent_id))
        pipe.delete(_pinned_agent_key(agent_id))
        await pipe.execute()

    def _cancel_pending(self, agent_id: str) -> None:
        for future in self._pending_futures.get(agent_id, {}).values():
            if not future.done():
                future.set_exception(ConnectionError("Terminal CLI agent disconnected"))


terminal_manager = TerminalConnectionManager()
