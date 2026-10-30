from datetime import datetime

import pytest

from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.run_metrics.run_metrics_scope import RunMetricsScope, ScopeKind
from src.autobots.space.space_doc_model import SpaceMemberDoc, SpaceMemberRole

SPACE = "space-1"
NOW = datetime(2026, 9, 1)


def _member(role: SpaceMemberRole) -> SpaceMemberDoc:
    return SpaceMemberDoc(
        _id="m1", space_id=SPACE, user_id="alice", role=role,
        invited_by="owner", created_at=NOW, updated_at=NOW,
    )


class TestResolve:
    def test_no_space_is_personal(self):
        assert RunMetricsScope.resolve("alice", None).kind is ScopeKind.personal

    def test_member_sees_only_their_own_work(self):
        assert RunMetricsScope.resolve(
            "alice", _member(SpaceMemberRole.member)
        ).kind is ScopeKind.own_in_space

    @pytest.mark.parametrize("role", [SpaceMemberRole.owner, SpaceMemberRole.admin])
    def test_owner_and_admin_see_the_whole_space(self, role):
        assert RunMetricsScope.resolve("alice", _member(role)).kind is ScopeKind.whole_space


class TestMatch:
    """The filter is the only thing standing between one member's runs and another's."""

    def test_personal_excludes_space_runs(self):
        match = RunMetricsScope(ScopeKind.personal, "alice").match(NOW, NOW)
        assert match["user_id"] == "alice"
        assert match["space_id"] is None

    def test_member_is_pinned_to_their_own_user_id(self):
        match = RunMetricsScope(ScopeKind.own_in_space, "alice", SPACE).match(NOW, NOW)
        assert match["user_id"] == "alice"
        assert match["space_id"] == SPACE

    def test_whole_space_carries_no_user_filter(self):
        match = RunMetricsScope(ScopeKind.whole_space, "alice", SPACE).match(NOW, NOW)
        assert match["space_id"] == SPACE
        assert "user_id" not in match

    def test_every_scope_is_bounded_by_status_and_window(self):
        for scope in (
            RunMetricsScope(ScopeKind.personal, "alice"),
            RunMetricsScope(ScopeKind.own_in_space, "alice", SPACE),
            RunMetricsScope(ScopeKind.whole_space, "alice", SPACE),
        ):
            match = scope.match(NOW, NOW)
            assert match["status"]["$in"] == [
                EventResultStatus.success.value,
                EventResultStatus.error.value,
                EventResultStatus.cancelled.value,
            ]
            assert "created_at" in match


class TestCacheKey:
    def test_scopes_never_share_a_key(self):
        keys = {
            RunMetricsScope(ScopeKind.personal, "alice").cache_key("last_7_days", "daily"),
            RunMetricsScope(ScopeKind.own_in_space, "alice", SPACE).cache_key("last_7_days", "daily"),
            RunMetricsScope(ScopeKind.whole_space, "alice", SPACE).cache_key("last_7_days", "daily"),
        }
        assert len(keys) == 3

    def test_two_members_of_one_space_do_not_share_a_key(self):
        alice = RunMetricsScope(ScopeKind.own_in_space, "alice", SPACE)
        bob = RunMetricsScope(ScopeKind.own_in_space, "bob", SPACE)
        assert alice.cache_key("last_7_days", "daily") != bob.cache_key("last_7_days", "daily")

    def test_ranges_and_granularities_do_not_collide(self):
        scope = RunMetricsScope(ScopeKind.personal, "alice")
        assert scope.cache_key("last_7_days", "daily") != scope.cache_key("last_30_days", "daily")
        assert scope.cache_key("last_7_days", "daily") != scope.cache_key("last_7_days", "weekly")
