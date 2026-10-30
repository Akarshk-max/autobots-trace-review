# tests/conn/neo4j/test_unit_type_conversion.py

import base64
import json

import pytest
from neo4j import time as neo4j_time

from src.autobots.conn.neo4j.neo4j_client import JSON_MAX_SAFE_INTEGER, _convert_neo4j_types

# Defensively import optional driver types from potential locations.
# These tests will be skipped if the types are not available.
try:
    from neo4j.spatial import Point
except ImportError:
    Point = None

try:
    from neo4j.graph import Vector  # Preferred path for driver >= 5.16
except ImportError:
    try:
        from neo4j.vector import Vector  # Fallback for other versions
    except ImportError:
        Vector = None


class TestNeo4jTypeConversion:
    """
    Unit tests for the _convert_neo4j_types helper function.
    These tests are self-contained and do not require a live Neo4j instance.
    """

    # --- Test Scalar & Specialized Types ---

    def test_datetime_conversion(self):
        """Tests that neo4j.time.DateTime converts to a compliant ISO string."""
        dt = neo4j_time.DateTime(2021, 7, 15, 14, 30, 0)
        result = _convert_neo4j_types(dt)

        assert isinstance(result, str)
        # Use startswith to tolerate different timezone representations (Z, +00:00)
        assert result.startswith("2021-07-15T14:30:00")
        json.dumps(result)  # Final check for JSON serializability

    def test_large_integer_conversion(self):
        """Tests that integers larger than JS's MAX_SAFE_INTEGER convert to strings."""
        large_int = JSON_MAX_SAFE_INTEGER + 100
        result = _convert_neo4j_types(large_int)

        assert isinstance(result, str)
        assert result == str(large_int)
        json.dumps(result)

    def test_binary_data_conversion(self):
        """Tests that bytes and bytearray convert to Base64 strings."""
        raw_bytes = b"Hello, Neo4j!"
        encoded = _convert_neo4j_types(raw_bytes)

        assert isinstance(encoded, str)
        assert base64.b64decode(encoded) == raw_bytes
        json.dumps(encoded)

    @pytest.mark.skipif(Point is None, reason="Point type not available in this driver version")
    def test_point_conversion(self):
        """Tests that Point-like objects are correctly converted via duck-typing."""

        # Create a dummy class that accurately mimics a real Point from the driver:
        # it is a tuple subclass and has .x, .y, and .srid properties.
        class DummyPoint(tuple):
            @property
            def x(self):
                return self[0]

            @property
            def y(self):
                return self[1]

            @property
            def srid(self):
                return 4326  # A sample SRID

        point_2d = DummyPoint((-0.1278, 51.5074))
        result = _convert_neo4j_types(point_2d)

        assert result == {"latitude": 51.5074, "longitude": -0.1278, "srid": 4326}
        json.dumps(result)

    @pytest.mark.skipif(Vector is None, reason="Vector type not available in this driver version")
    def test_vector_conversion(self):
        """Tests that real neo4j.vector.Vector objects convert to native Python lists."""
        try:
            # Use a real, constructible Vector object
            vector = Vector([0.1, 0.5, -0.9])
        except TypeError:
            pytest.skip("Vector constructor not supported in this driver version")

        result = _convert_neo4j_types(vector)
        assert result == [0.1, 0.5, -0.9]
        json.dumps(result)

    # --- Test Container & Nested Types ---

    def test_frozenset_conversion(self):
        """Tests that frozenset (used for node labels) converts to a list."""
        labels = frozenset({"Person", "Employee"})
        result = _convert_neo4j_types(labels)

        assert isinstance(result, list)
        assert set(result) == {"Person", "Employee"}
        json.dumps(result)

    def test_nested_structure(self):
        """Tests that deeply nested structures with mixed types are fully converted."""
        data = {
            "id": "tx-1",
            "timestamp": neo4j_time.DateTime(2023, 1, 1, 12, 0, 0),
            "payload": [
                {"item": "A", "value": 100},
                {"item": "B", "value": frozenset([JSON_MAX_SAFE_INTEGER + 1])},
            ],
        }
        result = _convert_neo4j_types(data)

        assert result["id"] == "tx-1"
        assert isinstance(result["timestamp"], str)
        assert result["payload"][0]["value"] == 100
        assert isinstance(result["payload"][1]["value"][0], str)
        json.dumps(result)

    def test_max_depth_protection(self):
        """Tests that the recursion depth limit prevents stack overflows."""
        deep_data = current = {}
        for _ in range(200):  # Build a dict deeper than the default max_depth
            current["next"] = {}
            current = current["next"]

        result = _convert_neo4j_types(deep_data, max_depth=50)

        traversed = result
        for _ in range(49):
            traversed = traversed["next"]

        assert isinstance(traversed, dict)
        assert isinstance(traversed["next"], str)  # The 50th level should be a string
        json.dumps(result)

    # --- Test Pass-through Behavior ---

    @pytest.mark.parametrize(
        "value",
        [
            "a plain string",
            42,
            3.14159,
            True,
            False,
            None,
        ],
        ids=["string", "integer", "float", "bool_true", "bool_false", "none"],
    )
    def test_standard_types_passthrough(self, value):
        """Tests that standard JSON-safe types are not modified."""
        # CORRECTED: Use 'is' for singletons, '==' for other value types
        if value is None or isinstance(value, bool):
            assert _convert_neo4j_types(value) is value
        else:
            assert _convert_neo4j_types(value) == value
