"""Tests for document manifest generation."""

import tempfile
from pathlib import Path

import pytest

from cupola.manifest import (
    build_manifest,
    get_high_priority_sections,
    get_section_by_anchor,
    load_manifest,
    write_manifest,
)


def test_build_manifest_basic():
    """Build manifest from source files."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("# Test Document\n\nContent here.\n")
        path = Path(f.name)

    try:
        manifest = build_manifest([path])
        assert manifest["manifest_version"] == "1.0.0"
        assert len(manifest["source_files"]) == 1
        assert manifest["source_files"][0]["path"] == str(path)
        assert "sha256" in manifest["source_files"][0]
    finally:
        path.unlink()


def test_manifest_sha256_computation():
    """SHA-256 hash is computed correctly."""
    with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
        f.write("test content")
        path = Path(f.name)

    try:
        manifest = build_manifest([path])
        sha = manifest["source_files"][0]["sha256"]
        assert len(sha) == 64  # SHA-256 hex length
    finally:
        path.unlink()


def test_write_and_load_manifest():
    """Manifest can be written and loaded."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create source file
        source = Path(tmpdir) / "source.md"
        source.write_text("# Test\n\nContent\n")

        # Build and write manifest
        manifest = build_manifest([source])
        manifest_path = Path(tmpdir) / "manifest.json"
        write_manifest(manifest, manifest_path)

        # Load and verify
        loaded = load_manifest(manifest_path)
        assert loaded is not None
        assert loaded["manifest_version"] == "1.0.0"
        assert len(loaded["source_files"]) == 1


def test_get_section_by_anchor():
    """Look up section by anchor ID."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("## My Section\n\nContent\n")
        path = Path(f.name)

    try:
        manifest = build_manifest([path])
        # Get first section's anchor
        section = manifest["source_files"][0]["sections"][0]
        anchor = section["anchor_id"]

        # Look it up
        found = get_section_by_anchor(manifest, anchor)
        assert found is not None
        assert found["anchor_id"] == anchor
    finally:
        path.unlink()


def test_get_high_priority_sections():
    """High-priority sections are identified."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("""# Overview

This is an overview.

## Requirements

These are the requirements.

## Implementation Details

How to implement.
""")
        path = Path(f.name)

    try:
        manifest = build_manifest([path])
        high_priority = get_high_priority_sections(manifest)
        # "Requirements" should be high priority
        assert len(high_priority) >= 1
        assert any("Requirements" in s["title"] for s in high_priority)
    finally:
        path.unlink()


def test_manifest_token_estimation():
    """Manifest includes token estimates."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("# Test\n\nSome content here with words.\n")
        path = Path(f.name)

    try:
        manifest = build_manifest([path])
        source = manifest["source_files"][0]
        assert source["total_tokens_est"] > 0
    finally:
        path.unlink()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
