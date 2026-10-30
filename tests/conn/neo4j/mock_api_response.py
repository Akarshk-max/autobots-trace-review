# tests/conn/neo4j/mock_api_response.py

"""
Enhanced mock Neo4j API response that covers all node types and relationship types
defined in the ontology. This ensures comprehensive test coverage for the visualization.
"""

MOCK_GRAPH_COMPREHENSIVE = [
    {
        "nodes": [
            # Document node (root)
            {
                "id": "doc_001",
                "label": "Document",
                "props": {
                    "filename": "software_license_agreement.pdf",
                    "chunk_count": 8,
                    "created_at": "2024-01-15T10:30:00Z",
                },
            },
            # Chunk nodes with various types and properties
            {
                "id": "chunk_001",
                "label": "Chunk",
                "props": {
                    "text": "This Software License Agreement ('Agreement') is entered into between Microsoft Corporation ('Licensor') and the customer ('Licensee').",
                    "chunk_type": "LegalClause",
                    "order": 0,
                    "section": "Preamble",
                },
            },
            {
                "id": "chunk_002",
                "label": "Chunk",
                "props": {
                    "text": "User: What are the payment terms for this license?\nAI: The payment terms are outlined in Section 4 of the agreement.",
                    "chunk_type": "ChatMessage",
                    "speaker": "User",
                    "order": 1,
                    "timestamp": "2024-01-15T14:22:00Z",
                },
            },
            {
                "id": "chunk_003",
                "label": "Chunk",
                "props": {
                    "text": "The license fee shall be \$50,000 annually, payable in advance on January 1st of each year.",
                    "chunk_type": "PaymentClause",
                    "order": 2,
                    "amount": 50000,
                    "currency": "USD",
                },
            },
            {
                "id": "chunk_004",
                "label": "Chunk",
                "props": {
                    "text": "This agreement shall be governed by the laws of the State of Washington.",
                    "chunk_type": "GoverningLaw",
                    "order": 3,
                    "jurisdiction": "Washington State",
                },
            },
            # Entity nodes - all types covered
            {
                "id": "person_001",
                "label": "Person",
                "props": {
                    "name": "John Smith",
                    "role": "Legal Counsel",
                    "email": "john.smith@microsoft.com",
                },
            },
            {
                "id": "org_001",
                "label": "Organization",
                "props": {
                    "name": "Microsoft Corporation",
                    "type": "Technology Company",
                    "headquarters": "Redmond, WA",
                    "founded": "1975",
                },
            },
            {
                "id": "org_002",
                "label": "Organization",
                "props": {
                    "name": "ACME Industries",
                    "type": "Manufacturing",
                    "revenue": "500M USD",
                },
            },
            {
                "id": "location_001",
                "label": "Location",
                "props": {
                    "name": "Redmond, Washington",
                    "type": "City",
                    "state": "Washington",
                    "country": "United States",
                },
            },
            {
                "id": "location_002",
                "label": "Location",
                "props": {
                    "name": "State of Washington",
                    "type": "State",
                    "country": "United States",
                },
            },
            {
                "id": "legal_001",
                "label": "LegalTerm",
                "props": {
                    "name": "Force Majeure",
                    "definition": "Unforeseeable circumstances that prevent a party from fulfilling a contract",
                    "section": "Section 12",
                },
            },
            {
                "id": "legal_002",
                "label": "LegalTerm",
                "props": {
                    "name": "Intellectual Property Rights",
                    "category": "IP Law",
                    "scope": "Software and Documentation",
                },
            },
            {
                "id": "concept_001",
                "label": "Concept",
                "props": {"name": "Data Privacy", "category": "Compliance", "importance": "High"},
            },
            {
                "id": "concept_002",
                "label": "Concept",
                "props": {
                    "name": "Software Licensing",
                    "category": "Business Model",
                    "complexity": "Medium",
                },
            },
        ],
        "rels": [
            # Document structure relationships
            {"start_node_id": "doc_001", "end_node_id": "chunk_001", "type": "CONTAINS"},
            {"start_node_id": "doc_001", "end_node_id": "chunk_002", "type": "CONTAINS"},
            {"start_node_id": "doc_001", "end_node_id": "chunk_003", "type": "CONTAINS"},
            {"start_node_id": "doc_001", "end_node_id": "chunk_004", "type": "CONTAINS"},
            # Sequential chunk relationships
            {"start_node_id": "chunk_001", "end_node_id": "chunk_002", "type": "NEXT"},
            {"start_node_id": "chunk_002", "end_node_id": "chunk_003", "type": "NEXT"},
            {"start_node_id": "chunk_003", "end_node_id": "chunk_004", "type": "NEXT"},
            # Entity mention relationships
            {"start_node_id": "chunk_001", "end_node_id": "org_001", "type": "MENTIONS"},
            {"start_node_id": "chunk_001", "end_node_id": "org_002", "type": "MENTIONS"},
            {"start_node_id": "chunk_003", "end_node_id": "org_001", "type": "MENTIONS"},
            {"start_node_id": "chunk_004", "end_node_id": "location_002", "type": "MENTIONS"},
            # Reference relationships
            {"start_node_id": "chunk_002", "end_node_id": "chunk_003", "type": "REFERENCES"},
            {"start_node_id": "chunk_004", "end_node_id": "legal_001", "type": "REFERENCES"},
            # Hierarchical relationships
            {"start_node_id": "location_001", "end_node_id": "location_002", "type": "PART_OF"},
            {"start_node_id": "legal_002", "end_node_id": "concept_002", "type": "PART_OF"},
            # Descriptive relationships
            {"start_node_id": "person_001", "end_node_id": "org_001", "type": "DESCRIBES"},
            {"start_node_id": "concept_001", "end_node_id": "legal_002", "type": "DESCRIBES"},
            # Cross-entity relationships
            {"start_node_id": "org_001", "end_node_id": "location_001", "type": "LOCATED_AT"},
            {"start_node_id": "person_001", "end_node_id": "org_001", "type": "WORKS_FOR"},
        ],
    }
]

# Simplified mock for basic testing
MOCK_GRAPH_SIMPLE = [
    {
        "nodes": [
            {
                "id": "chunk_001",
                "label": "Chunk",
                "props": {
                    "text": "Microsoft Corporation is a technology company.",
                    "chunk_type": "Paragraph",
                    "order": 0,
                },
            },
            {"id": "person_001", "label": "Person", "props": {"name": "Bill Gates"}},
            {"id": "org_001", "label": "Organization", "props": {"name": "Microsoft Corporation"}},
        ],
        "rels": [
            {"start_node_id": "chunk_001", "end_node_id": "person_001", "type": "MENTIONS"},
            {"start_node_id": "chunk_001", "end_node_id": "org_001", "type": "MENTIONS"},
        ],
    }
]

# Export the comprehensive version as default
MOCK_GRAPH = MOCK_GRAPH_COMPREHENSIVE
