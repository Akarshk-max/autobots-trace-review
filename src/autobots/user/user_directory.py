"""Turning user ids into display names, scoped to what the caller can already see.

The rule: a client never needs to resolve an arbitrary id, only ids it has just
been shown. So a request names a scope, the server authorizes the scope, and
only ids that occur inside it resolve. Ids outside it come back as placeholders,
however many are asked for - which is what stops the endpoint being an
email-enumeration oracle.

Authorization is a property of the scope, not of a "can A see B" relation
between users, which would be far more expensive to compute and is not a
question anything actually needs answered.
"""

from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Set

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from src.autobots.core.logging.app_code import AppCode
from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_crud import EventResultCRUD
from src.autobots.event_result.event_result_model import EventResultLiteDoc
from src.autobots.space.space_crud import SpaceMemberCRUD
from src.autobots.space.space_sharing import (
    get_emails_from_shared_with,
    get_space_ids_from_shared_with,
)
from src.autobots.user.user_crud import MAX_PROFILE_LOOKUPS, UserCRUD, norm_email
from src.autobots.user.user_orm_model import UserDoc, UserORM

# MAX_PROFILE_LOOKUPS is defined next to the query it bounds, in user_crud, and
# re-exported here because the endpoint enforces it.


class ProfileScopeType(str, Enum):
    runs = "runs"
    spaces = "spaces"


class ProfileScope(BaseModel):
    """Where the caller saw these ids.

    Plural by design: a run list shows fifty runs owned by fifty people, and one
    scope per request would make that fifty requests.
    """

    type: ProfileScopeType
    ids: List[str] = Field(default_factory=list)


class Profile(BaseModel):
    """What a caller may know about someone in scope.

    No email. Rendering a person needs a name, not an address, and the two are
    different disclosures - a grant list already holds the emails it was written
    with, so nothing needs this endpoint to hand them back.

    `user_id` is absent when the subject did not resolve: an id outside the
    scope, an email with no account behind it, a deleted user. `display_name` is
    always present, so no caller has to render a blank.
    """

    user_id: Optional[str] = None
    display_name: str
    avatar_url: Optional[str] = None


class Resolvable(BaseModel):
    """The subjects a scope entitles the caller to resolve.

    Emails are held normalised; build one through the helpers below rather than
    populating `emails` directly.
    """

    user_ids: Set[str] = Field(default_factory=set)
    emails: Set[str] = Field(default_factory=set)

    def allows(self, *, user_id: str | None, email: str | None) -> bool:
        normalised = norm_email(email)
        return bool(
            (user_id and user_id in self.user_ids)
            or (normalised and normalised in self.emails)
        )

    def union(self, other: "Resolvable") -> "Resolvable":
        return Resolvable(
            user_ids=self.user_ids | other.user_ids,
            emails=self.emails | other.emails,
        )


# --- pure logic ------------------------------------------------------------------


def display_name_for(
    *, user_metadata: Dict[str, Any] | None, email: str | None, user_id: str | None
) -> str:
    """One fallback chain, resolved here so no client reimplements it.

    Ends at something stable rather than blank: an unknown subject renders as
    `User 6f2a1b`, not an empty chip.
    """
    metadata = user_metadata or {}
    for key in ("full_name", "name"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if email and "@" in email:
        local = email.split("@", 1)[0].strip()
        if local:
            return local
    if email and email.strip():
        return email.strip()
    if user_id:
        return f"User {user_id[:6]}"
    return "Unknown user"


def can_read_run(
    run: EventResultLiteDoc,
    *,
    caller_id: str,
    caller_email: str | None,
    caller_space_ids: Set[str],
) -> bool:
    """Owner, email grant, or space grant.

    Not identical to the run endpoints: those also scope the owner branch by the
    caller's current space context, which this has no access to. The difference
    only ever lets an owner resolve people they themselves shared their own run
    with, so it discloses nothing they did not already choose.
    """
    if run.user_id == caller_id:
        return True
    caller = norm_email(caller_email)
    if caller and caller in {
        norm_email(email) for email in get_emails_from_shared_with(run.shared_with)
    }:
        return True
    return bool(caller_space_ids & set(get_space_ids_from_shared_with(run.shared_with)))


def resolvable_from_runs(
    runs: Iterable[EventResultLiteDoc],
    *,
    caller_id: str,
    caller_email: str | None,
    caller_space_ids: Set[str],
) -> Resolvable:
    """Everyone who could have spoken in runs the caller can read.

    Deliberately does not look at the turns. Only the owner or a grantee can post
    to a run, so owner-plus-grants is already a superset of the turn authors -
    and both live on the Mongo document, where reading the turns would mean
    fetching the run's payload out of S3 on every profile lookup.
    """
    resolvable = Resolvable()
    for run in runs:
        if not can_read_run(
            run,
            caller_id=caller_id,
            caller_email=caller_email,
            caller_space_ids=caller_space_ids,
        ):
            continue
        resolvable.user_ids.add(run.user_id)
        resolvable.emails.update(
            email
            for email in map(norm_email, get_emails_from_shared_with(run.shared_with))
            if email
        )
    return resolvable


def self_only(*, caller_id: str, caller_email: str | None) -> Resolvable:
    """The floor. A scopeless request resolves the caller and nobody else."""
    normalised = norm_email(caller_email)
    return Resolvable(
        user_ids={caller_id},
        emails={normalised} if normalised else set(),
    )


def profile_for(user: UserDoc) -> Profile:
    metadata = user.user_metadata or {}
    avatar = metadata.get("avatar_url")
    return Profile(
        user_id=user.user_id,
        display_name=display_name_for(
            user_metadata=metadata, email=user.email, user_id=user.user_id
        ),
        avatar_url=avatar if isinstance(avatar, str) and avatar else None,
    )


def placeholder_for(key: str) -> Profile:
    """What an unresolved subject renders as.

    Identical whether the subject is out of scope, has no account, or was
    deleted - so the response cannot be used to tell those cases apart.

    An email key renders as its local part, which the caller supplied and
    already holds; an id renders as `User 6f2a1b` rather than echoing the raw
    id back as if it were somebody's name.
    """
    is_email = "@" in key
    return Profile(
        display_name=display_name_for(
            user_metadata=None,
            email=key if is_email else None,
            user_id=None if is_email else key,
        )
    )


def build_response(
    *,
    requested_user_ids: List[str],
    requested_emails: List[str],
    users: Iterable[UserDoc],
    resolvable: Resolvable,
) -> Dict[str, Profile]:
    """Keyed by whatever was asked, with an entry for every key.

    Candidates are fetched before this runs and filtered here: deciding whether
    an id is in scope needs that user's email, so the lookup necessarily happens
    before the authorization decision. What is gated is the response, not the
    query.
    """
    by_id: Dict[str, Profile] = {}
    by_email: Dict[str, Profile] = {}
    for user in users:
        if not resolvable.allows(user_id=user.user_id, email=user.email):
            continue
        profile = profile_for(user)
        if user.user_id:
            by_id[user.user_id] = profile
        if user.email:
            by_email[user.email.lower()] = profile

    resolved: Dict[str, Profile] = {}
    for user_id in requested_user_ids:
        resolved[user_id] = by_id.get(user_id) or placeholder_for(user_id)
    for email in requested_emails:
        resolved[email] = by_email.get(email.lower()) or placeholder_for(email)
    return resolved


# --- orchestration over the CRUD classes -----------------------------------------


async def _resolvable_for_scope(
    db: AsyncIOMotorDatabase,
    *,
    scope: ProfileScope | None,
    caller_id: str,
    caller_email: str | None,
) -> Resolvable:
    """Turn a scope into the set of subjects it entitles the caller to resolve."""
    floor = self_only(caller_id=caller_id, caller_email=caller_email)
    if scope is None or not scope.ids:
        return floor

    member_crud = SpaceMemberCRUD(db)
    caller_space_ids = set(await member_crud.distinct_space_ids_for_user(caller_id))

    if scope.type is ProfileScopeType.runs:
        runs = await EventResultCRUD(db).find_by_ids(
            scope.ids, limit=MAX_PROFILE_LOOKUPS
        )
        return floor.union(
            resolvable_from_runs(
                runs,
                caller_id=caller_id,
                caller_email=caller_email,
                caller_space_ids=caller_space_ids,
            )
        )

    # spaces: only the ones the caller is actually in, then everyone in them.
    mine = sorted(caller_space_ids & set(scope.ids))
    if not mine:
        return floor
    return floor.union(
        Resolvable(user_ids=set(await member_crud.distinct_user_ids_for_spaces(mine)))
    )


async def resolve_profiles(
    db: AsyncIOMotorDatabase,
    *,
    caller_id: str,
    caller_email: str | None,
    user_ids: List[str],
    emails: List[str],
    scope: ProfileScope | None,
) -> Dict[str, Profile]:
    if not user_ids and not emails:
        return {}

    resolvable = await _resolvable_for_scope(
        db, scope=scope, caller_id=caller_id, caller_email=caller_email
    )
    users = await UserCRUD(db).find_for_profiles(
        user_ids=user_ids, emails=emails, limit=MAX_PROFILE_LOOKUPS
    )
    return build_response(
        requested_user_ids=user_ids,
        requested_emails=emails,
        users=users,
        resolvable=resolvable,
    )


__all__ = [
    "MAX_PROFILE_LOOKUPS",
    "Profile",
    "ProfileScope",
    "ProfileScopeType",
    "Resolvable",
    "build_response",
    "can_read_run",
    "display_name_for",
    "norm_email",
    "placeholder_for",
    "profile_for",
    "resolvable_from_runs",
    "resolve_profiles",
    "self_only",
]

async def keep_mentionable(
    db: AsyncIOMotorDatabase,
    *,
    ctx: Context,
    app_state: AppState,
    sender: UserORM,
    run_id: str | None,
    user_ids: List[str],
) -> Set[str]:
    """Which of these people the sender may address in this run.

    Grants are held as addresses rather than ids, so deciding this for an id
    means resolving it first - which is the same question as whose name the
    sender may see here, and so the same call. A profile comes back without a
    `user_id` exactly when the subject did not resolve.
    """
    if not user_ids:
        return set()
    sender_id = str(sender.id)
    if run_id is None:
        return {uid for uid in user_ids if uid == sender_id}

    resolved = await resolve_profiles(
        db,
        caller_id=sender_id,
        caller_email=sender.email,
        user_ids=list(dict.fromkeys(user_ids)),
        emails=[],
        scope=ProfileScope(type=ProfileScopeType.runs, ids=[run_id]),
    )
    allowed = {uid for uid, profile in resolved.items() if profile.user_id}

    refused = set(user_ids) - allowed
    if refused:
        logger.bind(
            ctx=ctx,
            code=AppCode.USER,
            action_run_id=app_state.action_run_id,
            run_id=run_id,
            refused=len(refused),
        ).info("Mentions dropped: not addressable in this run")
    return allowed
