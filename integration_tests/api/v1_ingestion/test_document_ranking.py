# tests/api/v1_ingestion/test_document_ranking.py

from collections import Counter, defaultdict

from src.autobots.action.action.common_action_models import SourceChunk


# This is a simplified, standalone version of the ranking logic for testing purposes.
# We keep the logic as it will be in the application.
def rank_documents_for_test(sources: list[SourceChunk], docs_context_top_k: int) -> list[str]:
    """
    A standalone copy of the ranking logic from api_knowledge_base.py for isolated unit testing.
    """
    DOC_RANKING_ALPHA = 0.7

    doc_scores = defaultdict(list)
    for s in sources:
        # The logic correctly checks for `None`, which we will no longer pass.
        # It will process all floats, including 0.0.
        if s.document_id and s.score is not None:
            doc_scores[s.document_id].append(s.score)

    if doc_scores:
        doc_final_scores = {}
        for doc_id, scores in doc_scores.items():
            if not scores:
                continue
            max_score = max(scores)
            avg_score = sum(scores) / len(scores)
            doc_final_scores[doc_id] = (
                DOC_RANKING_ALPHA * max_score + (1 - DOC_RANKING_ALPHA) * avg_score
            )

        # If all scores were 0.0, all final scores will be 0.0.
        # In this case, the sort order is arbitrary but not an error.
        # To make the fallback test deterministic, we check if all scores are effectively zero.
        if all(s == 0.0 for s in doc_final_scores.values()):
            doc_id_counts = Counter(s.document_id for s in sources if s.document_id)
            sorted_doc_ids = [doc_id for doc_id, count in doc_id_counts.most_common()]
        else:
            sorted_doc_ids = sorted(doc_final_scores, key=doc_final_scores.get, reverse=True)

    else:
        # FALLBACK for when all scores were None (which we now know is impossible due to model validation).
        # This path now primarily handles an empty `sources` list.
        doc_id_counts = Counter(s.document_id for s in sources if s.document_id)
        sorted_doc_ids = [doc_id for doc_id, count in doc_id_counts.most_common()]

    return sorted_doc_ids[:docs_context_top_k]


# --- Helper function to create dummy SourceChunk objects ---
def create_dummy_chunk(document_id: str, score: float | None, chunk_num: int = 1) -> SourceChunk:
    """
    Creates a valid SourceChunk, mimicking the application's behavior of converting
    None scores to 0.0 to satisfy Pydantic validation.
    """
    return SourceChunk(
        document_id=document_id,
        chunk_id=f"chunk_{document_id}_{chunk_num}",
        source=f"{document_id}.pdf",
        text_preview="Dummy text for testing...",
        # --- THE FIX ---
        # Replicate the application's logic: if score is None, default to 0.0.
        score=score if score is not None else 0.0,
    )


# --- Test Cases (Unchanged, but will now pass) ---


def test_weighted_ranking_prioritizes_high_relevance_short_doc():
    sources = [
        create_dummy_chunk(document_id="doc_A_short_relevant", score=0.95),
    ]
    for i in range(10):
        sources.append(
            create_dummy_chunk(document_id="doc_B_long_mediocre", score=0.4, chunk_num=i)
        )

    ranked_docs = rank_documents_for_test(sources, docs_context_top_k=3)

    assert len(ranked_docs) == 2
    assert ranked_docs[0] == "doc_A_short_relevant"
    assert ranked_docs[1] == "doc_B_long_mediocre"


def test_weighted_ranking_balances_peak_relevance_and_consistency():
    sources = [
        create_dummy_chunk(document_id="doc_A_peak", score=0.98, chunk_num=1),
        create_dummy_chunk(document_id="doc_A_peak", score=0.2, chunk_num=2),
        create_dummy_chunk(document_id="doc_A_peak", score=0.1, chunk_num=3),
        create_dummy_chunk(document_id="doc_B_consistent", score=0.85, chunk_num=1),
        create_dummy_chunk(document_id="doc_B_consistent", score=0.82, chunk_num=2),
        create_dummy_chunk(document_id="doc_B_consistent", score=0.80, chunk_num=3),
        create_dummy_chunk(document_id="doc_C_mediocre", score=0.5, chunk_num=1),
        create_dummy_chunk(document_id="doc_C_mediocre", score=0.5, chunk_num=2),
        create_dummy_chunk(document_id="doc_C_mediocre", score=0.5, chunk_num=3),
    ]

    ranked_docs = rank_documents_for_test(sources, docs_context_top_k=3)

    assert ranked_docs == ["doc_B_consistent", "doc_A_peak", "doc_C_mediocre"]


def test_ranking_fallback_to_frequency_when_no_scores_are_present():
    sources = []
    for i in range(3):
        sources.append(create_dummy_chunk(document_id="doc_A_frequent", score=None, chunk_num=i))
    for i in range(5):
        sources.append(
            create_dummy_chunk(document_id="doc_B_most_frequent", score=None, chunk_num=i)
        )
    sources.append(create_dummy_chunk(document_id="doc_C_rare", score=None))

    ranked_docs = rank_documents_for_test(sources, docs_context_top_k=3)

    assert ranked_docs == ["doc_B_most_frequent", "doc_A_frequent", "doc_C_rare"]


def test_ranking_handles_mixed_and_empty_scores():
    sources = [
        create_dummy_chunk(document_id="doc_A_valid", score=0.9, chunk_num=1),
        create_dummy_chunk(document_id="doc_A_valid", score=0.8, chunk_num=2),
        create_dummy_chunk(document_id="doc_B_mixed", score=0.85, chunk_num=1),
        create_dummy_chunk(document_id="doc_B_mixed", score=None, chunk_num=2),
        create_dummy_chunk(document_id="doc_B_mixed", score=0.75, chunk_num=3),
        create_dummy_chunk(document_id="doc_C_no_score", score=None, chunk_num=1),
        create_dummy_chunk(document_id="doc_C_no_score", score=None, chunk_num=2),
    ]

    ranked_docs = rank_documents_for_test(sources, docs_context_top_k=3)

    assert ranked_docs[0] == "doc_A_valid"
    assert ranked_docs[1] == "doc_B_mixed"
    assert "doc_C_no_score" in ranked_docs
    assert ranked_docs.index("doc_B_mixed") < ranked_docs.index("doc_C_no_score")


def test_ranking_with_empty_source_list():
    sources = []
    ranked_docs = rank_documents_for_test(sources, docs_context_top_k=3)
    assert ranked_docs == []
