from pydantic import BaseModel, Field

from src.autobots.core.utils import gen_uuid


class Context(BaseModel):
    # trace_id: str = str(gen_uuid())
    trace_id: str = Field(default_factory=lambda: str(gen_uuid()))
    user_id: str | None = None
    #: Who asked, when that differs from `user_id`. Added rather than replacing
    #: it, so every existing log line keeps meaning the owner.
    actor_user_id: str | None = None
    consumer_id: str | None = None
    action_run_name: str | None = None

    # def to_dict(self) -> Dict[str, Any]:
    #     return self.model_dump(exclude_none=True)


class ContextData(BaseModel):
    ctx: Context
