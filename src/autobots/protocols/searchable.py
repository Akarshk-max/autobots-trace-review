from typing import Any

from src.autobots.crud.crud_protocol import (
    CRUDProtocol,
    DataCreateType,
    DataFindType,
    DataType,
    DataUpdateType,
    LiteDataType,
)


class Searchable(
    CRUDProtocol[DataType, LiteDataType, DataCreateType, DataFindType, DataUpdateType],
):
    @staticmethod
    async def vector_search(**kwargs) -> Any: ...
