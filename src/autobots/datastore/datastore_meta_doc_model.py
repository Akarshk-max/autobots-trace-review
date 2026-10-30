from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from src.autobots.core.clock.clock import Clock
from src.autobots.data_model.space_id import OptionalSpaceId


class DatastoreMetaFind(BaseModel):
    """
    base class to be utilized to find datastore_meta
    """

    id: Optional[str] = Field(default=None)  # , alias='_id')
    datastore_id: Optional[str] = None
    name: Optional[str] = None


class DatastoreMetaDocFind(DatastoreMetaFind, OptionalSpaceId):
    """
    Add in user id to enforce multi-tenancy
    """

    user_id: str


class DatastoreMetaUpdate(BaseModel):
    """
    base class to be utilized to update datastore_meta
    """

    name: Optional[str] = None


class DatastoreMetaDocUpdate(DatastoreMetaUpdate):
    """
    Add in user id to enforce multi-tenancy
    """

    user_id: str


class DatastoreMetaCreate(BaseModel):
    """
    base class to be utilized to create datastore_meta
    """

    name: str


class DatastoreMetaDocCreate(OptionalSpaceId, DatastoreMetaCreate):
    """
    Add in user id to enforce multi-tenancy
    """

    datastore_id: str
    user_id: str
    created_at: datetime = Clock.datetime_now()


class DatastoreMetaDoc(DatastoreMetaDocCreate):
    __collection__ = "DatastoreMetas"

    id: str = Field(..., alias="_id")

    model_config = ConfigDict(populate_by_name=True)


class VectorStoreMetaDoc(DatastoreMetaDocCreate):
    __collection__ = "VectorstoreMetas"

    id: str = Field(..., alias="_id")

    model_config = ConfigDict(populate_by_name=True)
