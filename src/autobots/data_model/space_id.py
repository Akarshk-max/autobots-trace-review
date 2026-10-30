from typing import Optional

from pydantic import BaseModel


class OptionalSpaceId(BaseModel):
    space_id: Optional[str] = None


class SpaceId(BaseModel):
    space_id: str
