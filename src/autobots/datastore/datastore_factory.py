from src.autobots.conn.aws.s3 import S3, get_s3
from src.autobots.conn.pinecone.pinecone import Pinecone, get_pinecone
from src.autobots.conn.unstructured_io.unstructured_io import UnstructuredIO
from src.autobots.core.settings import Settings, SettingsProvider
from src.autobots.datastore.datastore import Datastore


class DatastoreFactory:
    def __init__(self, s3: S3, pinecone: Pinecone, unstructured: UnstructuredIO):
        # s3 conn
        self.s3 = s3
        # pinecone conn
        self.pinecone = pinecone
        self.unstructured = unstructured

    async def list_datastore(self):
        datastore_s3 = await self.s3.list("")
        return datastore_s3

    async def create_datastore(self, name: str) -> Datastore:
        datastore = Datastore(
            s3=self.s3, pinecone=self.pinecone, unstructured=self.unstructured
        ).init(name=name)
        return datastore

    async def hydrate_datastore(self, datastore_id: str) -> Datastore:
        datastore = Datastore(
            s3=self.s3, pinecone=self.pinecone, unstructured=self.unstructured
        ).hydrate(datastore_id=datastore_id)
        return datastore


def get_datastore_factory(
    unstructured: UnstructuredIO,
    pinecone=get_pinecone(),
    settings: Settings = SettingsProvider.sget(),
) -> DatastoreFactory:
    return DatastoreFactory(s3=get_s3(), pinecone=pinecone, unstructured=unstructured)
