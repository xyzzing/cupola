"""Tests for context window packaging."""

import tempfile
from pathlib import Path

import pytest

from cupola.context import (
    PackagedContext,
    build_planning_prompt,
    package_for_planning,
    package_section_for_task,
)
from cupola.manifest import build_manifest


def test_package_for_planning_basic():
    """Basic context packaging."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("# Test\n\nContent\n")
        path = Path(f.name)

    try:
        manifest = build_manifest([path])
        context = package_for_planning(manifest)

        assert isinstance(context, PackagedContext)
        assert context.prompt != ""
        assert context.token_estimate > 0
    finally:
        path.unlink()


def test_package_with_task_description():
    """Context packaging includes task description."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("# Test\n\nContent\n")
        path = Path(f.name)

    try:
        manifest = build_manifest([path])
        context = package_for_planning(
            manifest,
            task_description="Implement the requirements",
        )

        assert "TASK:" in context.prompt
        assert "Implement the requirements" in context.prompt
    finally:
        path.unlink()


def test_package_includes_outline():
    """Context includes document outline with anchor IDs."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("## Section A\n\nContent A\n\n## Section B\n\nContent B\n")
        path = Path(f.name)

    try:
        manifest = build_manifest([path])
        context = package_for_planning(manifest)

        assert "DOCUMENT OUTLINE:" in context.prompt
        # Should include anchor IDs
        assert "sec_" in context.prompt
    finally:
        path.unlink()


def test_package_high_priority_sections():
    """Context includes high-priority sections."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("## Requirements\n\nMust do X and Y.\n")
        path = Path(f.name)

    try:
        manifest = build_manifest([path])
        context = package_for_planning(manifest)

        assert "HIGH-PRIORITY SECTIONS:" in context.prompt
    finally:
        path.unlink()


def test_package_token_limit():
    """Context respects token limit."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        # Create a large document with many sections
        content = ""
        for i in range(100):
            content += f"## Section {i}\n\nThis is content for section {i} with some words.\n\n"
        f.write(content)
        path = Path(f.name)

    try:
        manifest = build_manifest([path])
        # Small token limit
        context = package_for_planning(manifest, max_tokens=50)

        assert context.token_estimate <= 50
        assert context.truncated is True
    finally:
        path.unlink()


def test_package_section_for_task():
    """Package specific section for task execution."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("## My Section\n\nSection content here.\n")
        path = Path(f.name)

    try:
        manifest = build_manifest([path])
        section = manifest["source_files"][0]["sections"][0]
        anchor = section["anchor_id"]

        packaged = package_section_for_task(manifest, anchor)
        assert packaged is not None
        assert anchor in packaged
    finally:
        path.unlink()


def test_build_planning_prompt():
    """Build complete planning prompt."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("# Test\n\nContent\n")
        path = Path(f.name)

    try:
        manifest = build_manifest([path])
        prompt = build_planning_prompt(manifest, "Implement feature X")

        assert "TASK:" in prompt
        assert "DOCUMENT OUTLINE:" in prompt
        assert "INSTRUCTIONS:" in prompt
        assert "plan.json" in prompt
    finally:
        path.unlink()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
