# src/autobots/ingestion/neo4j_transformer.py
import uuid
from typing import Any, Dict, Optional

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorDatabase

from src.autobots.conn.chroma.chroma import Document
from src.autobots.conn.neo4j.ontology import ChunkNode, EntityNode, ExtractedGraph, Relationship
from src.autobots.data_model.context import Context
from src.autobots.ingestion.graphrag_processor import GraphRAGProcessor
from src.autobots.user.user_orm_model import UserORM
from src.autobots.data_model.app_state import AppState


def transform_action_doc_to_graph(doc: Dict[str, Any]) -> Optional[ExtractedGraph]:
    """Transforms an Action document into a simple graph with one node."""
    action_id = str(doc.get("_id"))
    action_node = EntityNode(
        id=action_id,
        label="Concept",  # Using a generic label for an Action definition
        name=str(doc.get("name", "Unnamed Action")),
    )
    return ExtractedGraph(nodes=[action_node])


def transform_action_graph_doc_to_graph(doc: Dict[str, Any]) -> Optional[ExtractedGraph]:
    """Transforms an ActionGraph document into a structured knowledge graph."""
    graph_id = str(doc.get("_id"))
    graph_name = doc.get("name", "Unnamed Workflow")

    # The main node for the workflow itself
    workflow_node = EntityNode(id=graph_id, label="Concept", name=graph_name)

    nodes = [workflow_node]
    relationships = []
    step_counter = 0  # Use an incremental counter for order

    node_details = doc.get("node_details", {})
    graph_connections = doc.get("graph", {})

    # Create nodes for each step in the workflow
    for node_id, details in node_details.items():
        # FIX: Use ChunkNode instead of EntityNode to support custom properties
        # and avoid violating the strict 'label' Literal in EntityNode.
        # This respects the constraint of not modifying the core ontology.py file.
        step_node = ChunkNode(
            id=f"{graph_id}_{node_id}",  # Unique ID for the step instance
            text=details.get("data", {}).get("label", node_id),
            chunk_type="WorkflowStep",  # Use chunk_type to store the semantic label
            speaker=None,
            order=step_counter,
            properties={"name": details.get("data", {}).get("label", node_id)},
        )
        step_counter += 1
        nodes.append(step_node)

        # Link the workflow to its step
        relationships.append(
            Relationship(source_id=graph_id, target_id=step_node.id, type="HAS_STEP")
        )

        # Link the step to the underlying Action it implements (avoids supernodes)
        action_id = details.get("data", {}).get("actionId")
        if action_id:
            relationships.append(
                Relationship(source_id=step_node.id, target_id=action_id, type="IMPLEMENTS")
            )

    # Create relationships for the connections between steps
    for source_node_id, target_node_ids in graph_connections.items():
        for target_node_id in target_node_ids:
            relationships.append(
                Relationship(
                    source_id=f"{graph_id}_{source_node_id}",
                    target_id=f"{graph_id}_{target_node_id}",
                    type="NEXT",
                )
            )

    return ExtractedGraph(nodes=nodes, relationships=relationships)


async def transform_event_result_doc_to_graph(
    doc: Dict[str, Any], user: UserORM, db: AsyncIOMotorDatabase, ctx: Context, app_state: AppState,
) -> Optional[ExtractedGraph]:
    """Transforms a successful EventResult document into a knowledge graph representing its execution."""
    if doc.get("status") != "success":
        return None

    run_id = str(doc.get("_id"))
    run_name = doc.get("name", "Unnamed Run")
    run_type = doc.get("type")

    # --- STRATEGY: Create ONE canonical ChunkNode that will exist in BOTH Weaviate and Neo4j ---
    # Its ID will be the deterministic UUID that the query layer receives from Weaviate.
    # For an action_run, the document_id is simply the Mongo _id.
    canonical_document_id = str(doc.get("_id"))
    canonical_uuid = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{user.id}:EventResults:{canonical_document_id}")
    )

    run_node = ChunkNode(
        id=canonical_uuid,  # <-- CRITICAL FIX: Use the canonical UUID as the node ID.
        text=f"Execution log for run '{run_name}'",
        chunk_type="ExecutionRun",
        order=0,
        properties={"name": run_name, "status": "success", "type": run_type},
    )

    nodes = [run_node]
    relationships = []

    # --- RICH GRAPH EXTRACTION FOR TEXT-HEAVY ACTION RUNS ---
    if run_type == "action_run":
        # For a simple action run, link the run to the action that was executed.
        action_doc = doc.get("result", {}) or {}
        # FIX: Safely get the action ID and guard against None before creating the relationship.
        action_id = action_doc.get("id") if isinstance(action_doc, dict) else None
        if action_id:
            action_id = str(action_id)
            relationships.append(
                Relationship(source_id=canonical_uuid, target_id=action_id, type="INSTANCE_OF")
            )

        # --- FIX: Process multi-turn history from result.results array ---
        all_blocks = []
        # 1. Collect blocks from the historical `results` array
        results_array = action_doc.get("results", []) or []
        for turn in results_array:
            if isinstance(turn, dict):
                turn_output = turn.get("output", {}) or {}
                all_blocks.extend(turn_output.get("blocks", []) or [])

        # 2. Add blocks from the top-level `output` for backward compatibility
        #    and to include the final turn if it's duplicated there.
        top_level_output = action_doc.get("output", {}) or {}
        all_blocks.extend(top_level_output.get("blocks", []) or [])

        # 3. De-duplicate blocks based on their text content to avoid indexing the last turn twice.
        seen_texts = set()
        unique_blocks = []
        for block in all_blocks:
            if isinstance(block, dict) and block.get("text") and block["text"] not in seen_texts:
                unique_blocks.append(block)
                seen_texts.add(block["text"])

        output_text = "\n\n---\n\n".join(
            [b.get("text", "") for b in unique_blocks if b.get("text")]
        ).strip()

        # If there's substantial text, run the full GraphRAG processor on it
        if len(output_text) > 100:  # Threshold to avoid running on simple "OK" messages
            logger.bind(ctx=ctx).info(
                f"Detected substantial text in EventResult {run_id}. Running GraphRAGProcessor."
            )
            try:
                processor = GraphRAGProcessor()
                documents_for_graphing = [Document(page_content=output_text)]
                entity_graph = await processor.process(
                    elements=documents_for_graphing,
                    metadata={
                        "document_id": run_id,
                        # Pass a placeholder URI to satisfy the loader's contract for an s3_url
                        "s3_url": f"autobots://event-result/{run_id}",
                    },
                    user=user,
                    db=db,
                    ctx=ctx,
                    app_state=app_state,
                )
                if entity_graph:
                    # --- STRATEGY B: Strip internal chunks and re-anchor relationships ---
                    # Get the IDs of the temporary chunks created by the processor (e.g., "chunk_0").
                    internal_chunk_ids = {
                        n.id for n in entity_graph.nodes if isinstance(n, ChunkNode)
                    }

                    # 1. Add only the NEW entities (non-ChunkNodes) to our main node list.
                    existing_node_ids = {n.id for n in nodes}
                    for n in entity_graph.nodes:
                        if not isinstance(n, ChunkNode) and n.id not in existing_node_ids:
                            nodes.append(n)

                    # 2. Re-write the relationships, replacing any source/target that was an
                    #    internal chunk with our single, canonical run_node.
                    for rel in entity_graph.relationships:
                        source_id = (
                            canonical_uuid if rel.source_id in internal_chunk_ids else rel.source_id
                        )
                        target_id = (
                            canonical_uuid if rel.target_id in internal_chunk_ids else rel.target_id
                        )
                        # Avoid self-loops if both were internal chunks
                        if source_id != target_id:
                            relationships.append(
                                Relationship(
                                    source_id=source_id, target_id=target_id, type=rel.type
                                )
                            )
            except Exception as e:
                logger.bind(ctx=ctx).error(
                    f"GraphRAGProcessor failed for EventResult {run_id}: {e}"
                )

    elif run_type == "action_graph_run":
        graph_run_result = doc.get("result", {})
        action_graph_id = str(graph_run_result.get("id"))  # The ID of the ActionGraph definition
        node_outputs = graph_run_result.get("output", {}) or {}
        graph_definition = graph_run_result.get("graph", {})

        # FIX: The root run_node's ID must match the document_id of the Weaviate summary document.
        # Re-calculate the canonical UUID for the summary document.
        summary_doc_id = f"{canonical_document_id}_summary"
        summary_uuid = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{user.id}:EventResults:{summary_doc_id}")
        )

        run_node.id = summary_uuid
        run_node.text = f"Execution Summary for Workflow: {run_name}"

        # Link the run back to the workflow definition it is an instance of, using the new summary_id
        if action_graph_id:
            relationships.append(
                Relationship(source_id=summary_uuid, target_id=action_graph_id, type="INSTANCE_OF")
            )

        node_id_to_first_turn_uuid = {}

        if isinstance(node_outputs, dict):
            step_order_counter = 1
            for order, node_id_key in enumerate(sorted(node_outputs.keys()), start=1):
                node_run_data = node_outputs[node_id_key]
                if not isinstance(node_run_data, dict):
                    continue

                # FIX: Safely get the action ID, guarding against None.
                action_definition_id = node_run_data.get("id")

                # --- NEW: Collect and de-duplicate blocks for this node ---
                all_blocks = []
                historic_turns = node_run_data.get("results", []) or []
                for turn in historic_turns:
                    if isinstance(turn, dict):
                        all_blocks.extend(turn.get("output", {}).get("blocks", []) or [])
                all_blocks.extend(node_run_data.get("output", {}).get("blocks", []) or [])

                seen_texts = set()
                unique_blocks = []
                for block in all_blocks:
                    text_content = block.get("text")
                    if isinstance(block, dict) and text_content and text_content not in seen_texts:
                        unique_blocks.append(block)
                        seen_texts.add(text_content)

                # --- NEW: Create one ChunkNode per turn ---
                for i, block in enumerate(unique_blocks):
                    turn_doc_id = f"{canonical_document_id}_{node_id_key}_turn_{i}"
                    turn_uuid = str(
                        uuid.uuid5(uuid.NAMESPACE_URL, f"{user.id}:EventResults:{turn_doc_id}")
                    )

                    turn_node = ChunkNode(
                        id=turn_uuid,
                        text=f"Execution of step '{node_run_data.get('name', node_id_key)}', Turn {i + 1}",
                        chunk_type="ExecutionStepTurn",
                        order=step_order_counter,
                        properties={
                            "node_name": node_run_data.get("name", node_id_key),
                            "turn_index": i,
                        },
                    )
                    nodes.append(turn_node)
                    step_order_counter += 1

                    # Relationships should point to the FIRST turn of a step to represent the logical flow
                    if i == 0:
                        node_id_to_first_turn_uuid[node_id_key] = turn_uuid
                        # Link: Run -> First Turn of Step
                        relationships.append(
                            Relationship(
                                source_id=summary_uuid, target_id=turn_uuid, type="HAS_STEP"
                            )
                        )
                        # Link: First Turn of Step -> Action Definition
                        if action_definition_id:
                            relationships.append(
                                Relationship(
                                    source_id=turn_uuid,
                                    target_id=str(action_definition_id),
                                    type="EXECUTED_ACTION",
                                )
                            )
                    else:
                        # Link previous turn to current turn
                        prev_turn_uuid = str(
                            uuid.uuid5(
                                uuid.NAMESPACE_URL,
                                f"{user.id}:EventResults:{canonical_document_id}_{node_id_key}_turn_{i - 1}",
                            )
                        )
                        relationships.append(
                            Relationship(
                                source_id=prev_turn_uuid, target_id=turn_uuid, type="NEXT_TURN"
                            )
                        )

            # Link steps sequentially based on the original graph definition
            if isinstance(graph_definition, dict):
                for source_node_id, target_node_ids in graph_definition.items():
                    source_uuid = node_id_to_first_turn_uuid.get(source_node_id)
                    if source_uuid:
                        for target_node_id in target_node_ids:
                            target_uuid = node_id_to_first_turn_uuid.get(target_node_id)
                            if target_uuid:
                                relationships.append(
                                    Relationship(
                                        source_id=source_uuid,
                                        target_id=target_uuid,
                                        type="NEXT_STEP",
                                    )
                                )

    return ExtractedGraph(nodes=nodes, relationships=relationships)


# Add transformers for Chats here if needed in the future
