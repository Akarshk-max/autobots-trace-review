# tests/conn/neo4j/generate_frontend_demo.py

"""
Simple demo generator that converts mock API responses into HTML files for testing.
This is just a utility to quickly see how the visualization looks with our test data.
"""

from pathlib import Path

from src.autobots.conn.neo4j.visualization_utils import generate_html_visualization
from tests.conn.neo4j.mock_api_response import MOCK_GRAPH_COMPREHENSIVE, MOCK_GRAPH_SIMPLE


def generate_demo_files():
    """Generates demo HTML files from our mock API responses."""

    output_dir = Path("demo_output")
    output_dir.mkdir(exist_ok=True)

    # Demo 1: Comprehensive graph with all node types
    print("Generating comprehensive demo from mock data...")
    generate_html_visualization(
        graph_records=MOCK_GRAPH_COMPREHENSIVE,
        output_path=output_dir / "graph_demo_comprehensive.html",
    )

    # Demo 2: Simple graph for basic testing
    print("Generating simple demo from mock data...")
    generate_html_visualization(
        graph_records=MOCK_GRAPH_SIMPLE, output_path=output_dir / "graph_demo_simple.html"
    )

    print(f"Demo files generated in {output_dir.absolute()}")
    print("Open the HTML files in your browser to test the visualization.")


if __name__ == "__main__":
    generate_demo_files()
