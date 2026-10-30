from src.autobots.data_model.app_state import AppState
from src.autobots.data_model.context import Context
from src.autobots.event_result.event_result_model import EventResultStatus
from src.autobots.token_usage.token_usage_doc_model import LlmProvider, TokenUsageSource
from src.autobots.token_usage.token_usage_service import LlmSurface, TokenUsageService


def _app_state() -> AppState:
    return AppState(
        action_id="action-1",
        action_run_id="run-1",
        action_name="Research agent",
        action_graph_id="graph-1",
        action_graph_result_id="graph-result-1",
        action_graph_node_id="node-1",
    )


class TestRecording:
    def test_records_are_ignored_without_a_collector(self):
        app_state = _app_state()

        TokenUsageService.record(
            surface=LlmSurface.claude,
            ctx=Context(),
            app_state=app_state,
            usage={"input_tokens": 10, "output_tokens": 5},
            model="claude-opus-5",
            loop=1,
        )

        assert app_state.token_usage_collector is None

    def test_records_are_ignored_without_an_app_state(self):
        TokenUsageService.record(
            surface=LlmSurface.openrouter,
            ctx=Context(),
            app_state=None,
            usage={"prompt_tokens": 10, "completion_tokens": 5},
            model="openai/gpt-4o",
            loop=1,
        )

    def test_a_call_the_provider_reported_no_usage_for_is_not_recorded(self):
        app_state = _app_state()
        TokenUsageService.start_collection(app_state)

        TokenUsageService.record(
            surface=LlmSurface.openai_responses,
            ctx=Context(),
            app_state=app_state,
            usage=None,
            model="gpt-5.1",
            loop=1,
        )

        assert app_state.token_usage_collector.is_empty()

    def test_every_loop_of_a_run_is_collected(self):
        app_state = _app_state()
        TokenUsageService.start_collection(app_state)

        for loop in range(1, 4):
            TokenUsageService.record(
                surface=LlmSurface.claude,
                ctx=Context(),
                app_state=app_state,
                usage={"input_tokens": 100, "output_tokens": 10},
                model="claude-opus-5",
                loop=loop,
            )

        calls = app_state.token_usage_collector.calls_by_surface()[LlmSurface.claude]
        assert [call.loop for call in calls] == [1, 2, 3]


class TestBuildDocCreates:
    def test_one_document_carries_the_whole_tool_loop(self):
        app_state = _app_state()
        TokenUsageService.start_collection(app_state)
        for loop in range(1, 4):
            TokenUsageService.record(
                surface=LlmSurface.claude,
                ctx=Context(),
                app_state=app_state,
                usage={
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 50,
                },
                model="claude-opus-5",
                loop=loop,
            )

        docs = TokenUsageService.build_doc_creates(
            collector=app_state.token_usage_collector,
            app_state=app_state,
            user_id="user-1",
            status=EventResultStatus.success,
            turn=2,
        )

        assert len(docs) == 1
        doc = docs[0]
        assert doc.provider == LlmProvider.claude
        assert doc.api_call_count == 3
        assert doc.input_tokens == 300
        assert doc.cached_input_tokens == 150
        assert doc.output_tokens == 30
        assert doc.total_tokens == 480
        assert len(doc.calls) == 3

    def test_run_identity_is_carried_onto_the_document(self):
        app_state = _app_state()
        TokenUsageService.start_collection(app_state)
        TokenUsageService.record(
            surface=LlmSurface.claude,
            ctx=Context(),
            app_state=app_state,
            usage={"input_tokens": 1, "output_tokens": 1},
            model="claude-opus-5",
            loop=1,
        )

        doc = TokenUsageService.build_doc_creates(
            collector=app_state.token_usage_collector,
            app_state=app_state,
            user_id="user-1",
            status=EventResultStatus.error,
            turn=5,
        )[0]

        assert doc.user_id == "user-1"
        assert doc.action_id == "action-1"
        assert doc.action_run_id == "run-1"
        assert doc.action_graph_result_id == "graph-result-1"
        assert doc.action_graph_node_id == "node-1"
        assert doc.turn == 5
        assert doc.status == EventResultStatus.error

    def test_each_provider_gets_its_own_document(self):
        app_state = _app_state()
        TokenUsageService.start_collection(app_state)
        TokenUsageService.record(
            surface=LlmSurface.claude,
            ctx=Context(),
            app_state=app_state,
            usage={"input_tokens": 10, "output_tokens": 2},
            model="claude-opus-5",
            loop=1,
        )
        TokenUsageService.record(
            surface=LlmSurface.openrouter,
            ctx=Context(),
            app_state=app_state,
            usage={"prompt_tokens": 8, "completion_tokens": 3},
            model="openai/gpt-4o",
            loop=1,
        )

        docs = TokenUsageService.build_doc_creates(
            collector=app_state.token_usage_collector,
            app_state=app_state,
            user_id="user-1",
            status=EventResultStatus.success,
        )

        assert {doc.provider for doc in docs} == {LlmProvider.claude, LlmProvider.openrouter}


class TestChildRunsAreSeparate:
    def test_a_child_app_state_does_not_inherit_the_collector(self):
        app_state = _app_state()
        TokenUsageService.start_collection(app_state)

        child = app_state.child_context()

        assert child.token_usage_collector is None


class TestSourceAttribution:
    def test_an_action_run_is_attributed_to_its_run_id(self):
        app_state = _app_state()
        TokenUsageService.start_collection(app_state)
        TokenUsageService.record(
            surface=LlmSurface.claude,
            ctx=Context(),
            app_state=app_state,
            usage={"input_tokens": 1, "output_tokens": 1},
            model="claude-opus-5",
            loop=1,
        )

        doc = TokenUsageService.build_doc_creates(
            collector=app_state.token_usage_collector,
            app_state=app_state,
            user_id="user-1",
            status=EventResultStatus.success,
        )[0]

        assert doc.source == TokenUsageSource.action
        assert doc.source_id == "run-1"

    def test_a_non_action_source_is_attributed_to_its_own_id(self):
        app_state = AppState(
            source=TokenUsageSource.ingestion,
            source_id="document-abc",
        )
        TokenUsageService.start_collection(app_state)
        TokenUsageService.record(
            surface=LlmSurface.gemini,
            ctx=Context(),
            app_state=app_state,
            usage={"prompt_token_count": 10, "candidates_token_count": 2},
            model="gemini-3.5-flash",
            loop=1,
        )

        doc = TokenUsageService.build_doc_creates(
            collector=app_state.token_usage_collector,
            app_state=app_state,
            user_id="user-1",
            status=EventResultStatus.success,
        )[0]

        assert doc.source == TokenUsageSource.ingestion
        assert doc.source_id == "document-abc"
        assert doc.action_run_id is None

    def test_a_team_leader_is_attributed_to_the_team_run(self):
        app_state = AppState(
            action_id="leader-action",
            action_name="Leader",
            action_team_id="team-1",
            action_team_result_id="team-run-1",
        )
        TokenUsageService.start_collection(app_state)
        TokenUsageService.record(
            surface=LlmSurface.openai_responses,
            ctx=Context(),
            app_state=app_state,
            usage={"input_tokens": 10, "output_tokens": 2},
            model="gpt-5.2",
            loop=1,
        )

        doc = TokenUsageService.build_doc_creates(
            collector=app_state.token_usage_collector,
            app_state=app_state,
            user_id="user-1",
            status=EventResultStatus.success,
        )[0]

        assert doc.action_run_id is None
        assert doc.source_id == "team-run-1"

    def test_a_graph_node_is_attributed_to_the_graph_run(self):
        app_state = AppState(
            action_id="node-action",
            action_graph_id="graph-1",
            action_graph_result_id="graph-run-1",
            action_graph_node_id="node-1",
        )
        TokenUsageService.start_collection(app_state)
        TokenUsageService.record(
            surface=LlmSurface.claude,
            ctx=Context(),
            app_state=app_state,
            usage={"input_tokens": 5, "output_tokens": 1},
            model="claude-opus-5",
            loop=1,
        )

        doc = TokenUsageService.build_doc_creates(
            collector=app_state.token_usage_collector,
            app_state=app_state,
            user_id="user-1",
            status=EventResultStatus.success,
        )[0]

        assert doc.action_run_id is None
        assert doc.source_id == "graph-run-1"

    def test_a_child_stays_in_the_same_source(self):
        app_state = AppState(
            source=TokenUsageSource.knowledge_base,
            source_id="tenant-1",
        )

        child = app_state.child_context()

        assert child.source == TokenUsageSource.knowledge_base
        assert child.source_id == "tenant-1"


class TestChildRunContext:
    def test_team_ids_reach_a_child_only_when_passed(self):
        app_state = AppState(action_team_id="team-1", action_team_result_id="team-run-1")

        assert app_state.child_context().action_team_result_id is None

        carried = app_state.child_context(
            action_team_id=app_state.action_team_id,
            action_team_result_id=app_state.action_team_result_id,
        )
        assert carried.action_team_id == "team-1"
        assert carried.action_team_result_id == "team-run-1"

    def test_graph_ids_reach_a_child_only_when_passed(self):
        app_state = AppState(action_graph_id="graph-1", action_graph_result_id="graph-run-1")

        assert app_state.child_context().action_graph_result_id is None

        carried = app_state.child_context(
            action_graph_id=app_state.action_graph_id,
            action_graph_result_id=app_state.action_graph_result_id,
        )
        assert carried.action_graph_result_id == "graph-run-1"


class TestSurfaceIsRecorded:
    def test_the_two_openai_surfaces_are_told_apart(self):
        app_state = _app_state()
        TokenUsageService.start_collection(app_state)
        TokenUsageService.record(
            ctx=Context(),
            app_state=app_state,
            surface=LlmSurface.openai_responses,
            usage={"input_tokens": 100, "output_tokens": 10},
            model="gpt-5.1",
            loop=1,
        )
        TokenUsageService.record(
            ctx=Context(),
            app_state=app_state,
            surface=LlmSurface.openai_chat,
            usage={"prompt_tokens": 40, "completion_tokens": 4},
            model="gpt-4.1",
            loop=1,
        )

        docs = TokenUsageService.build_doc_creates(
            collector=app_state.token_usage_collector,
            app_state=app_state,
            user_id="user-1",
            status=EventResultStatus.success,
        )

        by_surface = {doc.surface: doc for doc in docs}
        assert set(by_surface) == {LlmSurface.openai_responses, LlmSurface.openai_chat}
        assert all(doc.provider == LlmProvider.openai for doc in docs)
        assert by_surface[LlmSurface.openai_responses].total_tokens == 110
        assert by_surface[LlmSurface.openai_chat].total_tokens == 44

    def test_the_surface_reaches_the_stored_document(self):
        app_state = _app_state()
        TokenUsageService.start_collection(app_state)
        TokenUsageService.record(
            ctx=Context(),
            app_state=app_state,
            surface=LlmSurface.claude,
            usage={"input_tokens": 1, "output_tokens": 1},
            model="claude-opus-5",
            loop=1,
        )

        doc = TokenUsageService.build_doc_creates(
            collector=app_state.token_usage_collector,
            app_state=app_state,
            user_id="user-1",
            status=EventResultStatus.success,
        )[0]

        assert doc.surface == LlmSurface.claude
        assert doc.provider == LlmProvider.claude
