from pydantic import BaseModel


class CollectionSearch(BaseModel):
    query: str


class CollectionDocSearch(CollectionSearch):
    user_id: str
    shared_with: str | None = None
