import asyncio
import json
import time
import uuid
from enum import Enum
from typing import Dict, Optional

from loguru import logger
from pydantic import BaseModel, Field

from src.autobots.core.redis.async_pool import get_async_redis

REDIS_PTY_SESSION_PREFIX = "terminal:pty_session:"
REDIS_PTY_OUT_PREFIX = "terminal:pty_out:"
PTY_SESSION_TTL = 3600  # 1 hour


async def _r():
    return await get_async_redis()


class PTYSessionStatus(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    ERROR = "error"


class PTYSessionInfo(BaseModel):
    session_id: str
    user_id: str
    consumer_id: str
    agent_id: str
    command: str
    status: PTYSessionStatus = PTYSessionStatus.STARTING
    exit_code: Optional[int] = None
    error: Optional[str] = None
    created_at: float
    platform: str = "posix"
    mode: str = "full"
    allowed_dirs: list[str] = Field(default_factory=list)
    last_command_timed_out: bool = False


class PTYSessionManager:
    """Singleton that tracks PTY sessions and buffers output from CLI agents."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            # session_id -> PTYSessionInfo
            cls._instance._sessions: Dict[str, PTYSessionInfo] = {}
            # agent_id -> {session_id}
            cls._instance._sessions_by_agent: Dict[str, set[str]] = {}
            # session_id -> asyncio.Queue for output chunks
            cls._instance._output_queues: Dict[str, asyncio.Queue] = {}
            # Sessions with an active Redis subscriber
            cls._instance._remote_sessions: set[str] = set()
        return cls._instance

    def _index_session(self, info: PTYSessionInfo) -> None:
        self._sessions[info.session_id] = info
        self._sessions_by_agent.setdefault(info.agent_id, set()).add(info.session_id)
        if info.session_id not in self._output_queues:
            self._output_queues[info.session_id] = asyncio.Queue()

    def create_session(
        self,
        user_id: str,
        consumer_id: str,
        agent_id: str,
        command: str,
    ) -> str:
        """Create a new PTY session and return its session_id."""
        session_id = str(uuid.uuid4())
        info = PTYSessionInfo(
            session_id=session_id,
            user_id=user_id,
            consumer_id=consumer_id,
            agent_id=agent_id,
            command=command,
            created_at=time.time(),
        )
        self._index_session(info)
        self._store_redis_session(session_id, info, sync=True)

        logger.info(
            f"PTY session created: session_id={session_id}, user_id={user_id}, "
            f"consumer_id={consumer_id}, agent_id={agent_id}, command={command}"
        )
        return session_id

    async def mark_started(
        self,
        session_id: str,
        platform: str | None = None,
        mode: str | None = None,
        allowed_dirs: list[str] | None = None,
    ) -> None:
        info = await self._find_session(session_id)
        if info:
            info.status = PTYSessionStatus.RUNNING
            info.last_command_timed_out = False
            if platform:
                info.platform = platform
            if mode:
                info.mode = mode
            if allowed_dirs is not None:
                info.allowed_dirs = allowed_dirs
            self._store_redis_session(session_id, info, sync=True)
            logger.info(f"PTY session started: {session_id}")
        else:
            logger.warning(f"PTY mark_started: session {session_id} not found locally or in Redis")

    async def mark_exited(self, session_id: str, exit_code: int) -> None:
        info = await self._find_session(session_id)
        if info:
            info.status = PTYSessionStatus.EXITED
            info.exit_code = exit_code
            self._store_redis_session(session_id, info, sync=True)
            logger.info(f"PTY session exited: {session_id}, exit_code={exit_code}")

    async def mark_error(self, session_id: str, error: str) -> None:
        info = await self._find_session(session_id)
        if info:
            info.status = PTYSessionStatus.ERROR
            info.error = error
            self._store_redis_session(session_id, info, sync=True)
            logger.warning(f"PTY session error: {session_id}, error={error}")

    async def mark_command_timed_out(self, session_id: str, *, timed_out: bool) -> None:
        info = await self._find_session(session_id)
        if info:
            info.last_command_timed_out = timed_out
            self._store_redis_session(session_id, info, sync=True)

    def enqueue_output(self, session_id: str, data: str) -> None:
        queue = self._output_queues.get(session_id)
        if queue is not None:
            queue.put_nowait(data)
        else:
            logger.warning(f"Output for unknown PTY session: {session_id}")

    def set_remote(self, session_id: str) -> None:
        if session_id in self._remote_sessions:
            return

        self._remote_sessions.add(session_id)

        if session_id not in self._output_queues:
            self._output_queues[session_id] = asyncio.Queue()

        async def _redis_subscriber():
            r = await _r()
            pubsub = r.pubsub()
            channel = f"{REDIS_PTY_OUT_PREFIX}{session_id}"
            queue = self._output_queues.get(session_id)
            try:
                await pubsub.subscribe(channel)
                logger.info(
                    f"PTY session {session_id[:8]} remote subscriber started on {channel}"
                )
                while session_id in self._output_queues:
                    try:
                        msg = await pubsub.get_message(
                            ignore_subscribe_messages=True, timeout=1.0
                        )
                    except Exception:
                        continue

                    if msg and msg["type"] == "message":
                        data = msg["data"]
                        if isinstance(data, bytes):
                            data = data.decode("utf-8")
                        payload = json.loads(data)
                        msg_type = payload.get("type")

                        if msg_type == "pty_output":
                            chunk = payload.get("data", "")
                            if queue is not None:
                                queue.put_nowait(chunk)
                        elif msg_type in ("pty_started", "pty_exited", "pty_error"):
                            self._apply_remote_event(session_id, payload)
                            if msg_type in ("pty_exited", "pty_error"):
                                break

                    await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"PTY remote subscriber error for {session_id[:8]}: {e}")
            finally:
                try:
                    await pubsub.unsubscribe(channel)
                    await pubsub.close()
                except Exception:
                    pass
                logger.info(f"PTY session {session_id[:8]} remote subscriber stopped")

        try:
            asyncio.create_task(_redis_subscriber())
        except RuntimeError:
            logger.warning(f"PTY session {session_id[:8]} no event loop for remote subscriber")

        logger.info(f"PTY session {session_id[:8]} marked as remote")

    async def drain_output(
        self, session_id: str, max_chars: int = 10000, timeout_sec: float = 5.0
    ) -> str:
        queue = self._output_queues.get(session_id)
        if queue is not None:
            return await self._drain_local(queue, max_chars, timeout_sec)
        return await self._drain_remote(session_id, max_chars, timeout_sec)

    async def _drain_local(
        self, queue: asyncio.Queue, max_chars: int, timeout_sec: float
    ) -> str:
        chunks: list[str] = []
        total = 0

        if queue.empty():
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=timeout_sec)
                chunks.append(chunk)
                total += len(chunk)
            except asyncio.TimeoutError:
                return ""

        while not queue.empty() and total < max_chars:
            try:
                chunk = queue.get_nowait()
                chunks.append(chunk)
                total += len(chunk)
            except asyncio.QueueEmpty:
                break

        result = "".join(chunks)
        if len(result) > max_chars:
            result = result[:max_chars]
        return result

    async def _drain_remote(
        self, session_id: str, max_chars: int, timeout_sec: float
    ) -> str:
        r = await _r()
        pubsub = r.pubsub()
        channel = f"{REDIS_PTY_OUT_PREFIX}{session_id}"

        try:
            await pubsub.subscribe(channel)
            chunks: list[str] = []
            total = 0
            deadline = asyncio.get_event_loop().time() + timeout_sec

            while asyncio.get_event_loop().time() < deadline and total < max_chars:
                try:
                    msg = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=0.5
                    )
                except Exception:
                    continue

                if msg and msg["type"] == "message":
                    data = msg["data"]
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")

                    payload = json.loads(data)
                    msg_type = payload.get("type")

                    if msg_type == "pty_output":
                        chunk = payload.get("data", "")
                        chunks.append(chunk)
                        total += len(chunk)
                    elif msg_type in ("pty_started", "pty_exited", "pty_error"):
                        self._apply_remote_event(session_id, payload)
                        if msg_type in ("pty_exited", "pty_error"):
                            break

                await asyncio.sleep(0.05)

            result = "".join(chunks)
            if len(result) > max_chars:
                result = result[:max_chars]
            return result
        except Exception as e:
            logger.warning(f"Remote drain error for session {session_id}: {e}")
            return ""
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.close()
            except Exception:
                pass

    def _apply_remote_event(self, session_id: str, payload: dict) -> None:
        msg_type = payload.get("type")
        if msg_type == "pty_started":
            asyncio.create_task(self.mark_started(
                session_id,
                platform=payload.get("platform"),
                mode=payload.get("mode"),
                allowed_dirs=payload.get("allowed_dirs"),
            ))
        elif msg_type == "pty_exited":
            asyncio.create_task(self.mark_exited(session_id, payload.get("exit_code", -1)))
        elif msg_type == "pty_error":
            asyncio.create_task(self.mark_error(session_id, payload.get("error", "Unknown error")))

    async def get_session(self, session_id: str, refresh: bool = False) -> Optional[PTYSessionInfo]:
        local = self._sessions.get(session_id)
        if local is not None and not refresh:
            return local

        try:
            r = await _r()
            data = await r.get(f"{REDIS_PTY_SESSION_PREFIX}{session_id}")
            if data:
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                info = PTYSessionInfo.model_validate_json(data)
                self._index_session(info)
                return info
        except Exception as e:
            logger.warning(f"Redis error fetching PTY session {session_id}: {e}")

        return local

    def list_sessions_for_agent(self, agent_id: str) -> list[PTYSessionInfo]:
        session_ids = self._sessions_by_agent.get(agent_id, set())
        return [self._sessions[sid] for sid in session_ids if sid in self._sessions]

    def cleanup_agent(self, agent_id: str) -> list[str]:
        session_ids = list(self._sessions_by_agent.pop(agent_id, set()))
        removed: list[str] = []

        async def _do():
            r = await _r()
            for session_id in session_ids:
                try:
                    await r.publish(
                        f"{REDIS_PTY_OUT_PREFIX}{session_id}",
                        json.dumps({"type": "pty_error", "error": "agent disconnected"}),
                    )
                except Exception:
                    pass
                try:
                    await r.delete(f"{REDIS_PTY_SESSION_PREFIX}{session_id}")
                except Exception:
                    pass

        try:
            asyncio.create_task(_do())
        except RuntimeError:
            pass

        for session_id in session_ids:
            self._output_queues.pop(session_id, None)
            self._remote_sessions.discard(session_id)
            if session_id in self._sessions:
                del self._sessions[session_id]
            removed.append(session_id)

        if removed:
            logger.info(f"Cleaned up {len(removed)} PTY sessions for agent_id={agent_id}")
        return removed

    def remove_session(self, session_id: str) -> None:
        info = self._sessions.pop(session_id, None)
        self._output_queues.pop(session_id, None)
        self._remote_sessions.discard(session_id)

        if info is not None:
            session_ids = self._sessions_by_agent.get(info.agent_id)
            if session_ids is not None:
                session_ids.discard(session_id)
                if not session_ids:
                    self._sessions_by_agent.pop(info.agent_id, None)

        async def _do_delete():
            try:
                r = await _r()
                await r.delete(f"{REDIS_PTY_SESSION_PREFIX}{session_id}")
            except Exception as e:
                logger.warning(f"Redis error removing PTY session {session_id}: {e}")

        try:
            asyncio.create_task(_do_delete())
        except RuntimeError:
            pass
        logger.info(f"PTY session removed: {session_id}")

    async def _find_session(self, session_id: str) -> Optional[PTYSessionInfo]:
        info = self._sessions.get(session_id)
        if info is not None:
            return info

        try:
            r = await _r()
            data = await r.get(f"{REDIS_PTY_SESSION_PREFIX}{session_id}")
            if data:
                if isinstance(data, bytes):
                    data = data.decode("utf-8")
                info = PTYSessionInfo.model_validate_json(data)
                self._index_session(info)
                return info
        except Exception as e:
            logger.warning(f"Redis error in _find_session for {session_id}: {e}")

        return None

    def _store_redis_session(
        self, session_id: str, info: PTYSessionInfo, sync: bool = False
    ) -> None:
        async def _do():
            try:
                r = await _r()
                key = f"{REDIS_PTY_SESSION_PREFIX}{session_id}"
                data = info.model_dump_json()
                await r.set(key, data)
                await r.expire(key, PTY_SESSION_TTL)
            except Exception as e:
                logger.warning(f"Redis error storing PTY session {session_id}: {e}")

        try:
            asyncio.create_task(_do())
        except RuntimeError:
            pass


pty_session_manager = PTYSessionManager()