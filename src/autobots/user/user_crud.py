from typing import Any, Dict, Iterable, List, Optional

from fastapi import Depends
from motor.motor_asyncio import AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo.collation import Collation

from src.autobots.core.database.mongo_base import get_mongo_db
from src.autobots.core.database.mongo_base_crud import CRUDBase
from src.autobots.core.database.mongo_base_model import CollectionDocSearch
from src.autobots.user.user_orm_model import UserDoc, UserDocCreate, UserDocRead, UserDocUpdate

# Matches the endpoint's own cap, so the two cannot drift apart.
MAX_PROFILE_LOOKUPS = 1000

# strength=2 compares case-insensitively (and accent-insensitively). Only ever
# applied to the email query - an id that differs by case is a different id.
EMAIL_COLLATION = Collation(locale="en", strength=2)


def norm_email(email: str | None) -> str | None:
    """The one place email casing is decided, shared with the directory."""
    return email.strip().lower() if email and email.strip() else None


def find_params_for_ids(user_ids: List[str]) -> Optional[Dict[str, Any]]:
    """`None` when there is nothing to ask for, so no query is issued.

    Both keys are searched because `Users._id` is a string here, unlike
    `EventResults._id`, and older rows may carry the id in either field.
    """
    if not user_ids:
        return None
    return {"$or": [{"_id": {"$in": user_ids}}, {"user_id": {"$in": user_ids}}]}


def find_params_for_emails(emails: List[str]) -> Optional[Dict[str, Any]]:
    """Normalised, never as-typed.

    Querying the caller's spelling as well would only match a stored address
    that happens to share it, which is the half-measure this replaces.
    """
    normalised = sorted({e for e in map(norm_email, emails) if e})
    if not normalised:
        return None
    return {"email": {"$in": normalised}}


def _as_user_doc(doc: Dict[str, Any]) -> UserDoc:
    return UserDoc.model_construct(
        user_id=str(doc.get("user_id") or doc.get("_id")),
        email=doc.get("email"),
        user_metadata=doc.get("user_metadata"),
    )


def _emails_not_found(emails: List[str], found: Iterable[UserDoc]) -> List[str]:
    """Requested addresses with no user yet, normalised."""
    have = {norm_email(u.email) for u in found}
    return sorted({e for e in map(norm_email, emails) if e and e not in have})


class UserCRUD(
    CRUDBase[UserDoc, UserDoc, UserDocCreate, UserDocRead, UserDocUpdate, CollectionDocSearch]
):
    def __init__(self, db: AsyncIOMotorDatabase = Depends(get_mongo_db)):
        self.document: AsyncIOMotorCollection = db[UserDoc.__collection__]
        super().__init__(UserDoc, UserDoc, self.document)

    async def find_for_profiles(
        self, user_ids: List[str], emails: List[str], limit: int = MAX_PROFILE_LOOKUPS
    ) -> List[UserDoc]:
        """The few fields a profile is built from, for a batch of ids and emails.

        Projected rather than read whole: a UserDoc is the entire Supabase user,
        and a batch of these renders a page, it does not load accounts.

        Ids and emails are two queries rather than one `$or`, because the email
        half needs a case-insensitive collation and the id half must not have
        one - ids differing only in case are different ids.
        """
        found: dict[str, UserDoc] = {}
        for params, collation in (
            (find_params_for_ids(user_ids), None),
            (find_params_for_emails(emails), None),
        ):
            if params is None:
                continue
            async for doc in self._profile_cursor(params, limit, collation):
                found[str(doc.get("user_id") or doc.get("_id"))] = _as_user_doc(doc)

        # Stored emails are lower-cased by the auth provider, so the query above
        # resolves them. Anything still missing is a row that predates that or
        # came in another way: retry just those, case-insensitively. Bounded by
        # the miss count, which under the normal invariant is zero - so the
        # collation scan does not run on the common path.
        missed = _emails_not_found(emails, found.values())
        if missed:
            async for doc in self._profile_cursor(
                {"email": {"$in": missed}}, limit, EMAIL_COLLATION
            ):
                found[str(doc.get("user_id") or doc.get("_id"))] = _as_user_doc(doc)

        return list(found.values())

    def _profile_cursor(self, find_params: dict, limit: int, collation):
        projection = {"_id": 1, "user_id": 1, "email": 1, "user_metadata": 1}
        cursor = self.document.find(find_params, projection)
        if collation is not None:
            cursor = cursor.collation(collation)
        return cursor.limit(limit)

    async def set_identity_fields(self, user_id: str, fields: Dict[str, Any]) -> int:
        """Not CRUDBase's `update_one`: that `$set`s `_id`, which Mongo rejects."""
        if not fields:
            return 0
        result = await self.document.update_one({"user_id": user_id}, {"$set": fields})
        return result.modified_count
