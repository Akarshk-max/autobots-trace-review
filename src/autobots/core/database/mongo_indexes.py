"""Create, at startup, the indexes each CRUD declares for its collection.

Discovery only sees a CRUD whose module has been imported. In the API that
happens via the routers, before startup; run this anywhere else and it may find
nothing, so an empty result warns.

Re-running is a no-op, except when a declaration's keys change without its name
changing — Mongo refuses that, and the fix is a new name plus a deliberate drop
of the old index.
"""

import typing
from typing import Dict, List, Optional, Set, Tuple, Type

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import IndexModel

from src.autobots.core.database.mongo_base_crud import CRUDBase


def _all_crud_classes() -> List[Type[CRUDBase]]:
    """`__subclasses__` is one level deep, so this recurses."""
    seen: Set[Type[CRUDBase]] = set()
    found: List[Type[CRUDBase]] = []

    def walk(cls: Type[CRUDBase]) -> None:
        for sub in cls.__subclasses__():
            if sub in seen:
                continue
            seen.add(sub)
            found.append(sub)
            walk(sub)

    walk(CRUDBase)
    return found


def _collection_of(cls: Type[CRUDBase]) -> Optional[str]:
    """`CRUDBase[EventResultDoc, ...]` -> `EventResultDoc.__collection__`."""
    for base in getattr(cls, "__orig_bases__", ()):
        args = typing.get_args(base)
        if args:
            return getattr(args[0], "__collection__", None)
    return None


def declared_indexes() -> Dict[str, List[IndexModel]]:
    """Indexes per collection, deduplicated by name — admin variants of a CRUD
    share a collection and usually declare the same indexes."""
    by_collection: Dict[str, List[IndexModel]] = {}
    seen: Set[Tuple[str, str]] = set()

    for cls in _all_crud_classes():
        indexes = cls.__dict__.get("__indexes__") or []
        if not indexes:
            continue
        collection = _collection_of(cls)
        if not collection:
            raise ValueError(
                f"{cls.__name__} declares indexes but its collection cannot be "
                "derived — it must parameterise CRUDBase with its doc model"
            )
        for index in indexes:
            name = index.document.get("name")
            if not name:
                raise ValueError(
                    f"{cls.__name__} declares an index without a name on {collection}"
                )
            if (collection, name) in seen:
                continue
            seen.add((collection, name))
            by_collection.setdefault(collection, []).append(index)

    return by_collection


async def ensure_indexes(db: AsyncIOMotorDatabase) -> Dict[str, List[str]]:
    """Create every declared index, per collection.

    One collection failing is logged rather than raised: a bad declaration
    should not stop the service from starting.
    """
    created: Dict[str, List[str]] = {}
    for collection, indexes in sorted(declared_indexes().items()):
        try:
            names = await db[collection].create_indexes(indexes)
            created[collection] = names
            logger.info(f"Ensured {len(names)} index(es) on {collection}: {', '.join(names)}")
        except Exception as e:  # noqa: BLE001
            logger.exception(f"Could not ensure indexes on {collection}: {e}")
    if not created:
        logger.warning(
            "No CRUD declared any index — check the crud modules are imported before startup"
        )
    return created
