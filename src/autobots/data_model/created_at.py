from pydantic import BaseModel, Field
from pydantic_extra_types.pendulum_dt import DateTime

from src.autobots.core.clock.clock import Clock


class CreatedAt(BaseModel):
    created_at: DateTime = Field(default_factory=lambda: Clock.datetime_now())
