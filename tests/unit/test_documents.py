"""Tests for document parsing."""

import tempfile
from pathlib import Path

import pytest

from cupola.documents import (
    parse_csv_file,
    parse_document,
    parse_markdown,
    parse_python_file,
)


def test_parse_markdown_sections():
    """Markdown file parsed into sections based on headers."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("# Title\n\nIntro text\n\n## Section 1\n\nContent 1\n\n## Section 2\n\nContent 2\n")
        path = Path(f.name)

    try:
        sections = parse_markdown(path)
        assert len(sections) == 3
        assert sections[0].title == "Title"
        assert sections[1].title == "Section 1"
        assert sections[2].title == "Section 2"
        assert "Content 1" in sections[1].content
    finally:
        path.unlink()


def test_parse_markdown_anchor_ids():
    """Sections get stable anchor IDs."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("## My Section\n\nContent\n")
        path = Path(f.name)

    try:
        sections = parse_markdown(path)
        assert len(sections) == 1
        assert sections[0].anchor_id.startswith("sec_")
        assert "my-section" in sections[0].anchor_id
    finally:
        path.unlink()


def test_parse_python_classes_and_functions():
    """Python file parsed into class and function sections."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("""class MyClass:
    def method(self):
        pass

def standalone_function():
    pass
""")
        path = Path(f.name)

    try:
        sections = parse_python_file(path)
        # Should find class and function
        titles = [s.title for s in sections]
        assert any("MyClass" in t for t in titles)
        assert any("standalone_function" in t for t in titles)
    finally:
        path.unlink()


def test_parse_csv_schema():
    """CSV file parsed into schema and data sections."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
        f.write("name,age,city\nAlice,30,NYC\nBob,25,LA\n")
        path = Path(f.name)

    try:
        sections = parse_csv_file(path)
        assert len(sections) == 2
        assert "Schema" in sections[0].title
        assert "Data" in sections[1].title
        assert "name" in sections[0].content
        assert "age" in sections[0].content
    finally:
        path.unlink()


def test_parse_document_dispatch():
    """parse_document dispatches based on file extension."""
    # Markdown
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("# Test\n\nContent\n")
        md_path = Path(f.name)

    # Python
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("def foo(): pass\n")
        py_path = Path(f.name)

    try:
        md_sections = parse_document(md_path)
        assert len(md_sections) > 0

        py_sections = parse_document(py_path)
        assert len(py_sections) > 0
    finally:
        md_path.unlink()
        py_path.unlink()


def test_section_token_count():
    """Sections have estimated token counts."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("## Test\n\nThis is some content with several words.\n")
        path = Path(f.name)

    try:
        sections = parse_markdown(path)
        assert sections[0].token_count > 0
    finally:
        path.unlink()


def test_section_summary_extraction():
    """Sections have extracted summaries."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("## Test\n\nThis is the first line of content.\nSecond line.\n")
        path = Path(f.name)

    try:
        sections = parse_markdown(path)
        assert sections[0].summary != ""
        assert "first line" in sections[0].summary
    finally:
        path.unlink()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
