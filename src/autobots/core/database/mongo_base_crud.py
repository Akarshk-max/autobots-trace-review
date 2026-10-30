import re
from typing import Any, Dict, Generic, List, Type, TypeVar

from bson import ObjectId
from fastapi import HTTPException
from loguru import logger
from motor.motor_asyncio import AsyncIOMotorCollection
from pydantic import BaseModel
from pymongo import DESCENDING, IndexModel, ReturnDocument
from pymongo.results import DeleteResult

DocType = TypeVar("DocType", bound=BaseModel)
# LiteDocType is minimal version of DocType, used in listing and search. Takes less memory.
LiteDocType = TypeVar("LiteDocType", bound=BaseModel)
DocCreateType = TypeVar("DocCreateType", bound=BaseModel)
DocFindType = TypeVar("DocFindType", bound=BaseModel)
DocUpdateType = TypeVar("DocUpdateType", bound=BaseModel)
DocSearchType = TypeVar("DocSearchType", bound=BaseModel)


class DocFindPage(BaseModel):
    docs: List[LiteDocType]
    total_count: int
    limit: int
    offset: int


class CRUDBase(
    Generic[DocType, LiteDocType, DocCreateType, DocFindType, DocUpdateType, DocSearchType]
):
    #: Indexes for this CRUD's collection, created at startup by
    #: `mongo_indexes.ensure_indexes`. Declared here so an index lives beside the
    #: queries it serves. Class-level so startup can read it without building a
    #: CRUD; the collection comes from the first generic parameter.
    __indexes__: List[IndexModel] = []

    def __init__(
        self,
        doc_model: Type[DocType],
        lite_doc_model: Type[LiteDocType],
        collection: AsyncIOMotorCollection,
        search_path: List[str] | None = None,
        vector_path: List[str] | None = None,
    ):
        self.doc_model = doc_model
        self.lite_doc_model = lite_doc_model
        self.document: AsyncIOMotorCollection = collection  # db[DocType.__collection__]
        self.search_index_name = f"search_{self.document.name.lower()}"
        if search_path is None:
            # self.search_path = ["name", "description"]
            self.search_path = ["name"]
        self.vector_index_name = f"vector_{self.document.name.lower()}"
        if vector_path is None:
            # self.vector_path = ["name", "description"]
            self.search_path = ["name"]

    async def insert_one(self, create_doc: DocCreateType) -> DocType | Exception:
        try:
            create_doc_dict = create_doc.model_dump()
            if create_doc_dict.get("id"):
                id = create_doc_dict.pop("id")
                create_doc_dict["_id"] = id
            insert_result = await self.document.insert_one(create_doc_dict)
            inserted_result = await self._find_by_id(insert_result.inserted_id)
            return inserted_result
        except Exception as e:
            logger.bind(create_doc=create_doc).error(str(e))
            return e

    async def _find_by_id(self, id: str) -> DocType:
        try:
            object_id = ObjectId(id)
        except Exception:
            object_id = id
        doc = await self.document.find_one({"_id": object_id})
        doc["_id"] = str(doc.get("_id"))
        return self.doc_model.model_validate(doc)

    async def find_one(
        self, doc_find: DocFindType, or_find_queries: List[DocFindType] | None = None
    ) -> DocType | None:
        # try:
        assert getattr(doc_find, "user_id", None) or getattr(doc_find, "space_id", None) or getattr(doc_find, "id", None), "At least user_id, space_id, or id must be provided in doc_find"
        find_params = await self._build_filter(doc_find, or_find_queries)
        if isinstance(find_params, Exception):
            return None
        doc = await self.document.find_one(find_params, sort=[("updated_at", -1)])
        if doc is None:
            return None
        doc["_id"] = str(doc.get("_id"))
        return self.doc_model.model_validate(doc)
        # except Exception as e:
        #     logger.bind().error(f"CRUDBase find_one error: {str(e)}")

    async def get_by_name(
        self,
        doc_find: DocFindType,
        or_find_queries: List[DocFindType] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> DocFindPage:
        find_params = await self._build_filter(doc_find, or_find_queries)
        if isinstance(find_params, Exception):
            return DocFindPage(docs=[], total_count=0, limit=limit, offset=offset)

        total_count = await self.count(find_params=find_params)

        cursor = self.document.find(find_params).skip(offset).limit(limit)
        docs: List[DocType] = []
        async for doc in cursor:
            doc["_id"] = str(doc.get("_id"))
            docs.append(self.doc_model.model_validate(doc))
        return DocFindPage(docs=docs, total_count=total_count, limit=limit, offset=offset)

    async def find_page(
        self,
        doc_find: DocFindType,
        or_find_queries: List[DocFindType] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> DocFindPage:
        assert getattr(doc_find, "user_id", None) or getattr(doc_find, "space_id", None) or getattr(doc_find, "id", None), "At least user_id, space_id, or id must be provided in doc_find"
        if or_find_queries:
            for or_find_query in or_find_queries:
                assert (
                    or_find_query.user_id
                    or or_find_query.is_published
                    or or_find_query.shared_with
                    or ("space_id" in type(or_find_query).model_fields
                        and or_find_query.space_id)
                )

        find_params: Dict[str, Any] | Exception = await self._build_filter(
            doc_find, or_find_queries
        )
        page: DocFindPage = await self._find_raw_page(
            find_params=find_params, limit=limit, offset=offset
        )
        return page
        # if isinstance(find_params, Exception) or len(find_params) == 0:
        #     return DocFindPage(docs=[], total_count=0, limit=limit, offset=offset)
        # docs = await self.find(doc_find=doc_find, or_find_queries=or_find_queries, limit=limit, offset=offset)
        # total_count = await self.count(find_params=find_params)
        # return DocFindPage(docs=docs, total_count=total_count, limit=limit, offset=offset)

    async def search_text_page(
        self,
        *,
        doc_find: DocFindType,
        or_find_queries: List[DocFindType] | None = None,
        q: str,
        searchable_paths: List[str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> DocFindPage:
        """Text search with scope filtering via doc_find + or_find_queries.

        Combines the standard scope clause (user_id, shared_with, space sharing)
        built from doc_find/or_find_queries with tokenized regex matching on
        searchable_paths. Eliminates the need for per-resource custom_search files.
        """
        assert getattr(doc_find, "user_id", None) or getattr(doc_find, "space_id", None) or getattr(doc_find, "id", None), "At least user_id, space_id, or id must be provided in doc_find"
        q = (q or "").strip()
        if not q:
            return await self.find_page(
                doc_find=doc_find, or_find_queries=or_find_queries, limit=limit, offset=offset
            )

        if searchable_paths is None:
            searchable_paths = ["name"]

        # Build scope filter from doc_find + or_find_queries
        scope_filter = await self._build_filter(doc_find, or_find_queries)
        if isinstance(scope_filter, Exception):
            return DocFindPage(docs=[], total_count=0, limit=limit, offset=offset)

        # Build text search clauses: each token must match at least one searchable path
        _HEX_RE = re.compile(r"^[0-9a-fA-F]{24}$")
        tokens = [t for t in re.split(r"\s+", q) if t]
        and_clauses: List[Dict[str, Any]] = [scope_filter]

        for token in tokens:
            escaped = re.escape(token)
            or_clauses: List[Dict[str, Any]] = [
                {path: {"$regex": escaped, "$options": "i"}} for path in searchable_paths
            ]
            if _HEX_RE.match(token):
                try:
                    or_clauses.append({"_id": ObjectId(token)})
                except Exception:
                    pass
            and_clauses.append({"$or": or_clauses})

        find_params = {"$and": and_clauses}
        return await self._find_raw_page(find_params=find_params, limit=limit, offset=offset)

    async def _find_raw_page(
        self, find_params: Dict[str, Any], limit: int = 100, offset: int = 0
    ) -> DocFindPage:
        if isinstance(find_params, Exception):
            return DocFindPage(docs=[], total_count=0, limit=limit, offset=offset)
        docs = await self._find_raw(find_params=find_params, limit=limit, offset=offset)
        total_count = await self.count(find_params=find_params)
        return DocFindPage(docs=docs, total_count=total_count, limit=limit, offset=offset)

    async def find(
        self,
        doc_find: DocFindType,
        or_find_queries: List[DocFindType] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[LiteDocType]:
        find_params = await self._build_filter(doc_find, or_find_queries)
        if isinstance(find_params, Exception) or len(find_params) == 0:
            return []

        docs: List[LiteDocType] = await self._find_raw(
            find_params=find_params, limit=limit, offset=offset
        )
        return docs

    async def _find_raw(
        self, find_params: Dict[str, Any], limit: int = 5, offset: int = 0
    ) -> List[LiteDocType]:
        # if len(find_params) == 0:
        #     logger.bind(find_params=find_params).error("Empty find params returning empty")
        #     return []

        model_fields = self.lite_doc_model.model_fields.keys()
        fields_to_select = {}
        for field in model_fields:
            if isinstance(field, dict):
                for k, v in field.items():
                    fields_to_select[f"{k}.{field}"] = 1
            else:
                fields_to_select[field] = 1

        cursor = self.document.find(find_params, fields_to_select)
        cursor.allow_disk_use(True)
        cursor.sort([("updated_at", DESCENDING)]).skip(offset * limit).limit(limit)

        docs = []
        async for doc in cursor:
            try:
                # Mongo Result field _id has ObjectId, converting it to str for pydantic model
                doc["_id"] = str(doc.get("_id"))
                doc_type = self.lite_doc_model.model_validate(doc)
                docs.append(doc_type)
            except Exception as e:
                logger.bind(find_params=find_params, doc=doc).exception(
                    f"Error in CRUDBase: {str(e)}"
                )
        return docs

    async def count(self, find_params: Dict[str, Any]) -> int:
        count = await self.document.count_documents(find_params)
        return count

    async def count_for_search(self, search_doc: DocSearchType) -> int:
        """Copy changes from search function to keep search_doc handling same, otherwise refactor"""
        search_doc_dict = search_doc.model_dump()
        assert search_doc_dict.__contains__("query")
        query = search_doc_dict.get("query")
        assert search_doc_dict.__contains__("user_id")
        assert search_doc_dict.__contains__("shared_with")

        model_fields = self.lite_doc_model.model_fields.keys()
        fields_to_select = {}
        for field in model_fields:
            if isinstance(field, dict):
                for k, v in field.items():
                    fields_to_select[f"{k}.{field}"] = 1
            else:
                fields_to_select[field] = 1

        pipeline_match_clauses: Dict[str, Any] = {}

        pipeline_match_user_clauses = []
        pipeline_match_user_clauses.append({"user_id": search_doc_dict.get("user_id")})
        pipeline_match_user_clauses.append({"shared_with": search_doc_dict.get("shared_with")})
        assert len(pipeline_match_user_clauses) > 0

        pipeline_match_clauses["$or"] = pipeline_match_user_clauses

        if search_doc_dict.__contains__("type") and search_doc_dict.get("type"):
            pipeline_match_clauses["type"] = search_doc_dict.get("type")

        pipeline = [
            {
                "$search": {
                    "index": self.search_index_name,  # Atlas Search index name
                    "text": {
                        "query": query,
                        "path": self.search_path,
                        "fuzzy": {"maxEdits": 2, "prefixLength": 1},
                    },
                }
            },
            {"$match": pipeline_match_clauses},
            {"$count": "total"},
        ]

        cursor = self.document.aggregate(pipeline=pipeline, allowDiskUse=True)
        count_result = await cursor.to_list(length=1)
        total_count = 0
        if len(count_result) > 0:
            total_count = count_result[0].get("total")
        return total_count

    async def delete_many(self, doc_find: DocFindType) -> DeleteResult | Exception:
        find_params = await self._build_filter(doc_find)
        if isinstance(find_params, Exception):
            return find_params

        delete_result = await self.document.delete_many(find_params)
        return delete_result

    async def upsert(
        self,
        *,
        match: Dict[str, Any],
        set_fields: Dict[str, Any] | None = None,
        set_on_insert: Dict[str, Any] | None = None,
    ) -> DocType:
        """Update the doc `match` selects, or create it, and return it either way.

        `match` is used verbatim, not through `_build_filter` - that injects
        `space_id: None` when the Find model declares it, which on a write would
        silently retarget the upsert to a different record.

        Nothing is filled in for you - no model is built here, so `CreatedAt`
        and `UpdatedAt` do not fire. Pass their values in.

        Needs a unique index on the `match` keys; without one, concurrent
        first-writes each insert.
        """
        set_fields = dict(set_fields or {})
        set_on_insert = dict(set_on_insert or {})

        # Mongo rejects this too, but without naming the field.
        clashes = set(set_fields) & set(set_on_insert)
        if clashes:
            raise ValueError(
                f"{sorted(clashes)} given to both set_fields and set_on_insert"
            )

        update: Dict[str, Any] = {}
        if set_fields:
            update["$set"] = set_fields
        if set_on_insert:
            update["$setOnInsert"] = set_on_insert

        doc = await self.document.find_one_and_update(
            filter=match,
            update=update,
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        doc["_id"] = str(doc.get("_id"))
        return self.doc_model.model_validate(doc)

    async def update_one(self, doc_update: DocUpdateType) -> DocType | Exception:
        try:
            if not doc_update.id or not doc_update.user_id:
                raise HTTPException(405, "Cannot find Doc to update")

            update_params = {}
            for key, value in doc_update.model_dump().items():
                if value is not None:
                    if key == "id":
                        try:
                            object_id = ObjectId(value)
                        except Exception:
                            object_id = value
                        update_params["_id"] = ObjectId(object_id)
                    else:
                        update_params[key] = value

            updated_action_doc = await self.document.find_one_and_update(
                filter={"_id": update_params.get("_id"), "user_id": update_params.get("user_id")},
                update={"$set": update_params},
                return_document=ReturnDocument.AFTER,
            )
            if updated_action_doc is None:
                raise HTTPException(405, "Unable to update doc")

            updated_action_doc["_id"] = str(updated_action_doc.get("_id"))
            doc_type = self.doc_model.model_validate(updated_action_doc)
            return doc_type
        except Exception as e:
            logger.bind(doc_update=doc_update).exception(str(e))
            return e

    async def _build_filter(
        self, doc_find: DocFindType, or_find_queries: List[DocFindType] | None = None
    ) -> Dict[str, Any] | Exception:
        query_filter = {}
        filter1 = await self._build_find_params(doc_find.model_dump(exclude_none=True), {})
        if doc_find and (not or_find_queries or len(or_find_queries) == 0):
            query_filter = filter1
        elif or_find_queries and len(or_find_queries) > 0:
            query_filter["$or"] = [filter1]
            for query in or_find_queries:
                filter2 = await self._build_find_params(query.model_dump(exclude_none=True), {})
                query_filter["$or"] += [filter2]

        # If the DocFind model declares space_id but the filter doesn't contain it,
        # inject space_id=None to scope to personal context (exclude workspace resources).
        if "space_id" in type(doc_find).model_fields and not self._filter_contains_space_id(
            query_filter
        ):
            if "$or" in query_filter:
                # Pushed into every branch rather than wrapped around them.
                # Logically identical, but an $and around an $or leaves each
                # branch unable to use an index that includes space_id: Mongo
                # picks a partial index for one branch, scans it wholesale and
                # filters space_id in a FETCH afterwards. On the owned-or-shared
                # runs query that meant examining 20,000 documents to return
                # 341, and a count that took 19s instead of 90ms.
                query_filter = {
                    "$or": [
                        {**branch, "space_id": None} for branch in query_filter["$or"]
                    ]
                }
            else:
                query_filter["space_id"] = None

        return query_filter

    @staticmethod
    def _filter_contains_space_id(query_filter: Dict[str, Any]) -> bool:
        """Check if space_id appears anywhere in the query filter (top-level or inside $or branches)."""
        if "space_id" in query_filter or "shared_with.space_id" in query_filter:
            return True
        for or_clause in query_filter.get("$or", []):
            if "space_id" in or_clause or "shared_with.space_id" in or_clause:
                return True
        return False

    async def _build_find_params(
        self,
        model_params: Dict[str, Any],
        mongo_params: Dict[str, Any],
        mongo_param_prefix: str = "",
        level: int = 0,
    ) -> Dict[str, Any] | Exception:
        try:
            find_params = mongo_params
            for key, value in model_params.items():
                if value is not None:
                    if key == "id" and level == 0:
                        try:
                            object_id = ObjectId(value)
                        except Exception:
                            object_id = value
                        find_params[f"{mongo_param_prefix}_id"] = object_id
                    else:
                        match value:
                            case dict():
                                new_find_params = await self._build_find_params(
                                    value, {}, f"{mongo_param_prefix}{key}.", level + 1
                                )
                                for param_key in new_find_params.keys():
                                    new_key = f"{mongo_param_prefix}{param_key}"
                                    find_params[new_key] = new_find_params[param_key]
                            case _:
                                find_params[f"{mongo_param_prefix}{key}"] = value
            return find_params
        except Exception as e:
            logger.exception(str(e))
            return e

    async def search(
        self, search_doc: DocSearchType, limit: int = 5, offset: int = 0
    ) -> List[LiteDocType]:
        """Copy changes to count_for_search function to keep search_doc handling same, otherwise refactor"""
        search_doc_dict = search_doc.model_dump()
        assert search_doc_dict.__contains__("query")
        query = search_doc_dict.get("query")
        assert search_doc_dict.__contains__("user_id")
        assert search_doc_dict.__contains__("shared_with")

        model_fields = self.lite_doc_model.model_fields.keys()
        fields_to_select = {}
        for field in model_fields:
            if isinstance(field, dict):
                for k, v in field.items():
                    fields_to_select[f"{k}.{field}"] = 1
            else:
                fields_to_select[field] = 1

        pipeline_match_clauses: Dict[str, Any] = {}

        pipeline_match_user_clauses = []
        pipeline_match_user_clauses.append({"user_id": search_doc_dict.get("user_id")})
        pipeline_match_user_clauses.append({"shared_with": search_doc_dict.get("shared_with")})
        assert len(pipeline_match_user_clauses) > 0

        pipeline_match_clauses["$or"] = pipeline_match_user_clauses

        if search_doc_dict.__contains__("type") and search_doc_dict.get("type"):
            pipeline_match_clauses["type"] = search_doc_dict.get("type")

        pipeline = [
            {
                "$search": {
                    "index": self.search_index_name,  # Atlas Search index name
                    "text": {
                        "query": query,
                        "path": self.search_path,
                        "fuzzy": {"maxEdits": 2, "prefixLength": 1},
                    },
                }
            },
            {"$addFields": {"score": {"$meta": "searchScore"}}},
            {"$match": pipeline_match_clauses},
            {
                "$sort": {
                    "score": DESCENDING,
                    "updated_at": DESCENDING,
                }  # Optional: sort by updated_at
            },
            {
                "$skip": offset  # Apply offset here
            },
            {
                "$limit": limit  # Optional: limit results
            },
        ]

        cursor = self.document.aggregate(pipeline=pipeline, allowDiskUse=True)
        # cursor.sort([("updated_at", DESCENDING)]).skip(offset * limit).limit(limit)

        docs = []
        async for doc in cursor:
            try:
                # Mongo Result field _id has ObjectId, converting it to str for pydantic model
                doc["_id"] = str(doc.get("_id"))
                doc_type = self.lite_doc_model.model_validate(doc)
                docs.append(doc_type)
            except Exception as e:
                logger.bind(pipeline=pipeline, doc=doc).exception(f"Error in CRUDBase: {str(e)}")
        return docs

    async def search_page(
        self, search_doc: DocSearchType, limit: int = 100, offset: int = 0
    ) -> DocFindPage:
        docs = await self.search(search_doc=search_doc, limit=limit, offset=offset)
        total_count = await self.count_for_search(search_doc=search_doc)
        return DocFindPage(docs=docs, total_count=total_count, limit=limit, offset=offset)
