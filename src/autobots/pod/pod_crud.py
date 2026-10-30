from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase

from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.pod.pod_doc_model import PodDoc, PodDocCreate, PodDocFind, PodDocUpdate


class PodCRUD(
    CRUDBase[PodDoc, PodDoc, PodDocCreate, PodDocFind, PodDocUpdate, CollectionDocSearch]
):
    def __init__(self, db: AsyncIOMotorDatabase):
        self.document: AsyncIOMotorCollection = db[PodDoc.__collection__]
        super().__init__(PodDoc, PodDoc, self.document)
