"""Per-turn attribution on DataBlock: field shapes, the stamp, and the traps.

Attribution is server-set. These pin the two properties the design leans on -
a stamp overwrites whatever the caller sent, and a stamped block survives the
queue round trip - plus the three places a naive implementation gets it wrong:
stored config, merged output, and documents written before any of this existed.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from src.autobots.action.action.action_doc_model import ActionResult
from src.autobots.action.action.common_action_models import (
    SERVER_SET_FIELDS,
    DataBlock,
    RunSource,
    _attribution,
    stamp_author,
)
from src.autobots.action.action_result.action_result_doc_model import QueuedInputs
from src.autobots.data_model.context import Context

# --- field shapes ---------------------------------------------------------------


def test_both_fields_default_to_none():
    """Optional is what makes this shippable without a migration."""
    block = DataBlock(text="hello")
    assert block.user_id is None
    assert block.created_at is None


def test_documents_written_before_attribution_still_validate():
    """Every turn already in Mongo and S3 has neither key."""
    legacy = {"text": "an old turn", "urls": [], "file_ids": []}
    block = DataBlock.model_validate(legacy)
    assert block.user_id is None
    assert block.created_at is None

    # And it round-trips back out without inventing them.
    assert "user_id" not in block.model_dump(exclude_none=True)
    assert "created_at" not in block.model_dump(exclude_none=True)


# --- the boundary is derived, not hand-listed -----------------------------------


def test_the_server_set_set_is_derived_from_the_model():
    """Mark a field `server_set` and it is covered; no second list to maintain."""
    assert SERVER_SET_FIELDS == {"user_id", "created_at", "source"}
    for name in SERVER_SET_FIELDS:
        assert name in DataBlock.model_fields


def test_every_server_set_field_is_actually_stamped():
    """The drift guard.

    Marking a new field `server_set` without adding it to `_attribution` would
    leave it client-settable - a forgeable field that looks protected. This is
    the test that fails when that happens.
    """
    written = set(_attribution(user_id="u1", source=None, at=None))
    assert written == set(SERVER_SET_FIELDS)


def test_the_stamp_clears_every_server_set_field_it_does_not_set():
    """Overwriting is the whole mechanism - there is no separate strip.

    A stamp that only assigned the fields it had a value for would leave a
    stored or forged value behind in the ones it skipped. Clearing the derived
    set first is what makes one mechanism sufficient.
    """
    dirty = {"text": "keep me"} | {name: "forged" for name in SERVER_SET_FIELDS}

    # A stamp with no source: source must still be cleared, not left "forged".
    stamped = stamp_author(dirty, user_id="the-caller")

    assert stamped["text"] == "keep me"
    assert stamped["user_id"] == "the-caller"
    assert "source" not in stamped
    assert stamped["created_at"] != "forged"


# --- the stamp ------------------------------------------------------------------


def test_stamp_discards_a_forged_author():
    """The whole point: a caller cannot author a turn as someone else."""
    forged = {
        "text": "transfer the funds",
        "user_id": "the-owner",
        "created_at": "1999-01-01",
        "source": "webhook",
    }
    stamped = stamp_author(forged, user_id="the-caller", source=RunSource.api)

    assert stamped["user_id"] == "the-caller"
    assert stamped["source"] is RunSource.api
    assert isinstance(stamped["created_at"], datetime)
    # The content itself is untouched.
    assert stamped["text"] == "transfer the funds"


def test_stamp_does_not_mutate_the_caller_s_dict():
    """The API handlers rebind `input`; the original must not change underneath."""
    original = {"text": "hi"}
    stamp_author(original, user_id="u1")
    assert original == {"text": "hi"}


def test_stamp_handles_a_missing_input():
    assert stamp_author(None, user_id="u1")["user_id"] == "u1"


def test_stamped_by_matches_stamp_author():
    """Two shapes, one meaning.

    HTTP entry points take a DataBlock and use `stamped_by`; a schedule fires
    with the input dict APScheduler stored, and uses `stamp_author`.
    """
    at = datetime(2026, 1, 1, 12, 0, 0)
    block = DataBlock(text="hi", user_id="forged").stamped_by("u1", at=at)
    dict_form = stamp_author({"text": "hi", "user_id": "forged"}, user_id="u1", at=at)

    assert block.user_id == dict_form["user_id"] == "u1"
    assert block.created_at == dict_form["created_at"] == at
    assert block.text == "hi"


# --- what the typed run body keeps, and what it drops ----------------------------


def test_a_forged_author_in_a_request_body_is_discarded():
    """The run endpoints take a DataBlock, so this is the shape of the attack.

    A caller posts `user_id` in the JSON body hoping to author a turn as
    somebody else; the handler stamps over it before the input goes anywhere.
    """
    posted = DataBlock.model_validate(
        {"text": "approve it", "user_id": "the-owner", "created_at": "1999-01-01"}
    )
    stored = posted.stamped_by("the-caller").model_dump(exclude_none=True)

    assert stored["user_id"] == "the-caller"
    assert stored["created_at"].year != 1999


def test_consumer_id_survives_the_typed_body():
    """`_extract_consumer_id_from_request` reads it off the run body.

    It rode through as an undeclared key while the endpoint took a raw dict.
    Declaring it on DataBlock is what keeps the typed body from dropping it.
    """
    posted = DataBlock.model_validate({"text": "hi", "consumer_id": "tenant-42"})
    assert posted.consumer_id == "tenant-42"
    assert posted.model_dump(exclude_none=True)["consumer_id"] == "tenant-42"


def test_the_typed_body_drops_fields_datablock_does_not_declare():
    """The accepted cost of typing the run endpoints, pinned so it stays known.

    Two registered action types declare input models with fields outside
    DataBlock - `text2text_io_mapper` and `video2video_opus` - and those fields
    no longer survive the request body. Tracked separately; this test exists so
    the behaviour is deliberate rather than discovered.
    """
    posted = DataBlock.model_validate({"text": "hi", "input_action_output": {"a": 1}})
    assert not hasattr(posted, "input_action_output")
    assert "input_action_output" not in posted.model_dump()


# --- the property the queue leans on ---------------------------------------------


def test_a_queued_block_stays_attributed_through_the_dequeue():
    """`_process_queued_inputs` dumps the block straight into ActionResult(input=...).

    Stamping once at enqueue is what makes every downstream path work without
    threading an author through it, so this is the load-bearing test.
    """
    queue = QueuedInputs(inputs=[DataBlock(text="second turn").stamped_by("participant-1")])

    # Exactly what activity.py:142-148 does.
    next_input = queue.inputs.pop(0).model_dump(exclude_none=True)
    turn = ActionResult(input=next_input)

    assert turn.input["user_id"] == "participant-1"
    assert isinstance(turn.input["created_at"], datetime)


def test_reorder_keeps_other_people_s_authors():
    """PUT /queue replaces the whole list; only new blocks may be stamped.

    Mirrors the handler's list comprehension. Without this, dragging one item
    re-attributes everyone else's queued messages to whoever moved it.
    """
    theirs = DataBlock(text="theirs").stamped_by("participant-1")
    mine_new = DataBlock(text="mine")

    caller = "participant-2"
    result = [b if b.user_id else b.stamped_by(caller) for b in [theirs, mine_new]]

    assert result[0].user_id == "participant-1"
    assert result[1].user_id == "participant-2"


# --- stored configuration --------------------------------------------------------


def test_a_stored_config_needs_no_stripping_because_the_fire_overwrites():
    """Webhook and schedule inputs are saved once and replayed on every fire.

    Saving them clean was a second mechanism guarding the same property the
    stamp already guarantees. Whatever the stored block carries - stale, forged
    or absent - the fire replaces all of it.
    """
    stored_with_junk = {
        "text": "daily digest",
        "user_id": "whoever-configured-this",
        "created_at": "2020-01-01",
        "source": "api",
    }

    fired = stamp_author(
        stored_with_junk, user_id="the-owner", source=RunSource.schedule
    )

    assert fired["user_id"] == "the-owner"
    assert fired["source"] is RunSource.schedule
    assert isinstance(fired["created_at"], datetime)
    assert fired["created_at"].year != 2020
    # And the stored config is untouched, ready for the next fire.
    assert stored_with_junk["user_id"] == "whoever-configured-this"


def test_source_distinguishes_a_schedule_from_a_person():
    """Without it the transcript renders a cron firing as somebody talking."""
    typed = DataBlock(text="hi").stamped_by("u1", source=RunSource.api)
    fired = DataBlock(text="daily digest").stamped_by("u1", source=RunSource.schedule)

    assert typed.user_id == fired.user_id  # same owner
    assert typed.source is not fired.source  # different origin


def test_a_replayed_config_block_is_re_stamped_per_fire():
    """Two fires of one schedule are two turns, at two times, not one repeated."""
    stored = DataBlock(text="daily digest").model_dump(exclude_none=True)

    first = stamp_author(stored, user_id="owner", at=datetime(2026, 1, 1, 9, 0))
    second = stamp_author(stored, user_id="owner", at=datetime(2026, 1, 2, 9, 0))

    assert first["created_at"] != second["created_at"]
    assert second["created_at"] - first["created_at"] == timedelta(days=1)
    # And the stored config is still clean for the next fire.
    assert "user_id" not in stored


# --- semantics -------------------------------------------------------------------


def test_merge_produces_an_unattributed_block():
    """merge() assembles output. A merged block has no single author, so it has none."""
    merged = DataBlock().merge(
        [
            DataBlock(text="a").stamped_by("u1"),
            DataBlock(text="b").stamped_by("u2"),
        ]
    )
    assert merged.text == "a\nb"
    assert merged.user_id is None
    assert merged.created_at is None


def test_the_prompt_is_unchanged_by_attribution():
    """No new tokens, no prompt-format change, nothing for the NSFW filter to see."""
    ctx = Context(user_id="test-user")
    plain = DataBlock(text="what is the status?")
    stamped = plain.stamped_by("u1")

    assert stamped.get_llm_prompt(ctx) == plain.get_llm_prompt(ctx)


def test_model_output_blocks_are_never_attributed():
    """The connectors return DataBlock(text=...) for generated content.

    A DataBlock is attributed only if user_id is set - nothing may infer
    authorship from the type alone.
    """
    from_a_model = DataBlock(text="here is the answer")
    assert from_a_model.user_id is None
