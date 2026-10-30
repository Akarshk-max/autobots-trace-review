from typing import List, Optional

from pydantic import BaseModel

from src.autobots.data_model.space_id import SpaceId


class Shareable(BaseModel):
    is_published: bool = False
    shared_with: List[SpaceId | str] = []


class OptionalShareable(BaseModel):
    is_published: Optional[bool] = None
    shared_with: Optional[List[SpaceId | str]] = None

class SingularShareable(BaseModel):
    is_published: bool = False
    shared_with: Optional[SpaceId | str] = None

