"""Tests for space sharing via unified shared_with field with SpaceId objects."""

from src.autobots.data_model.space_id import SpaceId
from src.autobots.space.space_sharing import (
    get_emails_from_shared_with,
    get_space_ids_from_shared_with,
)

# --- Extraction helpers ---


def test_get_space_ids_from_shared_with():
    shared_with = ["alice@example.com", SpaceId(space_id="sp_1"), "bob@test.com", SpaceId(space_id="sp_2")]
    assert get_space_ids_from_shared_with(shared_with) == ["sp_1", "sp_2"]


def test_get_emails_from_shared_with():
    shared_with = ["alice@example.com", SpaceId(space_id="sp_1"), "bob@test.com", SpaceId(space_id="sp_2")]
    assert get_emails_from_shared_with(shared_with) == ["alice@example.com", "bob@test.com"]


def test_get_space_ids_empty():
    assert get_space_ids_from_shared_with([]) == []
    assert get_space_ids_from_shared_with(None) == []


def test_get_emails_empty():
    assert get_emails_from_shared_with([]) == []
    assert get_emails_from_shared_with(None) == []


# --- SpaceId in shared_with on doc models ---


def test_action_create_shared_with_mixed():
    from src.autobots.action.action.action_doc_model import ActionCreate

    ac = ActionCreate(
        name="test",
        type="text2text_llm_chat_openai",
        shared_with=["alice@example.com", {"space_id": "sp_1"}],
    )
    emails = get_emails_from_shared_with(ac.shared_with)
    space_ids = get_space_ids_from_shared_with(ac.shared_with)
    assert emails == ["alice@example.com"]
    assert space_ids == ["sp_1"]


def test_action_update_shared_with_mixed():
    from src.autobots.action.action.action_doc_model import ActionUpdate

    au = ActionUpdate(shared_with=["bob@test.com", {"space_id": "sp_2"}])
    emails = get_emails_from_shared_with(au.shared_with)
    space_ids = get_space_ids_from_shared_with(au.shared_with)
    assert emails == ["bob@test.com"]
    assert space_ids == ["sp_2"]


def test_action_find_shared_with_email():
    from src.autobots.action.action.action_doc_model import ActionFind

    af = ActionFind(shared_with="alice@example.com")
    assert af.shared_with == "alice@example.com"


def test_action_find_shared_with_space_id():
    from src.autobots.action.action.action_doc_model import ActionFind

    af = ActionFind(shared_with=SpaceId(space_id="sp_1"))
    assert isinstance(af.shared_with, SpaceId)
    assert af.shared_with.space_id == "sp_1"


def test_action_find_shared_with_space_id_dict():
    """Verify dict input is deserialized to SpaceId."""
    from src.autobots.action.action.action_doc_model import ActionFind

    af = ActionFind.model_validate({"shared_with": {"space_id": "sp_1"}})
    assert isinstance(af.shared_with, SpaceId)
    assert af.shared_with.space_id == "sp_1"


def test_action_lite_doc_shared_with_roundtrip():
    """Verify SpaceId objects survive model_dump → model_validate roundtrip."""
    from src.autobots.action.action.action_doc_model import ActionLiteDoc

    doc = ActionLiteDoc(
        _id="test_id",
        type="text2text_llm_chat_openai",
        name="test",
        user_id="u1",
        created_at="2024-01-01T00:00:00",
        shared_with=["alice@example.com", {"space_id": "sp_1"}],
    )
    dumped = doc.model_dump()
    assert dumped["shared_with"][0] == "alice@example.com"
    assert dumped["shared_with"][1] == {"space_id": "sp_1"}

    restored = ActionLiteDoc.model_validate(dumped)
    assert get_emails_from_shared_with(restored.shared_with) == ["alice@example.com"]
    assert get_space_ids_from_shared_with(restored.shared_with) == ["sp_1"]
