from pydantic import BaseModel, Field


class ID(BaseModel):
    id: str = Field(..., alias="id")


class OptionalID(BaseModel):
    id: str | None = None
