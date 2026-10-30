from pydantic import BaseModel, Field


class MongoID(BaseModel):
    id: str = Field(..., alias="_id")


class OptionalMongoID(MongoID):
    id: str | None = None
