"""Document manifest generation.

Creates document_manifest.json with SHA-256 hashes, token budgets,
and section indexing for context-window packaging.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cupola.documents import parse_document


@dataclass
class SectionInfo:
    """Metadata about a document section."""
    anchor_id: str
    title: str
    token_count: int
    summary: str
    byte_offset: int = 0


@dataclass
class SourceFileInfo:
    """Metadata about a source file."""
    path: str
    sha256: str
    total_tokens_est: int
    sections: list[SectionInfo]


def compute_sha256(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(64 * 1024)  # 64KB chunks
            if not chunk:
                break
            sha256.update(chunk)
    return sha256.hexdigest()


def build_manifest(source_files: list[Path]) -> dict:
    """Build document manifest from source files.

    Args:
        source_files: List of file paths to include in manifest.

    Returns:
        Manifest dictionary conforming to PRD section 5.1 schema.
    """
    manifest = {
        "manifest_version": "1.0.0",
        "source_files": [],
    }

    for file_path in source_files:
        if not file_path.exists():
            continue

        # Parse document into sections
        sections = parse_document(file_path)

        # Build section info list
        section_infos = []
        for section in sections:
            section_info = SectionInfo(
                anchor_id=section.anchor_id,
                title=section.title,
                token_count=section.token_count,
                summary=section.summary,
                byte_offset=section.byte_offset,
            )
            section_infos.append(section_info)

        # Compute total tokens
        total_tokens = sum(s.token_count for s in sections)

        # Compute SHA-256
        sha256 = compute_sha256(file_path)

        # Build source file info
        source_info = SourceFileInfo(
            path=str(file_path),
            sha256=sha256,
            total_tokens_est=total_tokens,
            sections=section_infos,
        )

        # Convert to dict for JSON serialization
        source_dict = {
            "path": source_info.path,
            "sha256": source_info.sha256,
            "total_tokens_est": source_info.total_tokens_est,
            "sections": [
                {
                    "anchor_id": s.anchor_id,
                    "title": s.title,
                    "token_count": s.token_count,
                    "summary": s.summary,
                }
                for s in source_info.sections
            ],
        }

        manifest["source_files"].append(source_dict)

    return manifest


def write_manifest(manifest: dict, output_path: Path) -> None:
    """Write manifest to JSON file."""
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)


def load_manifest(manifest_path: Path) -> Optional[dict]:
    """Load manifest from JSON file."""
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def get_section_by_anchor(manifest: dict, anchor_id: str) -> Optional[dict]:
    """Look up a section by its anchor ID."""
    for source in manifest.get("source_files", []):
        for section in source.get("sections", []):
            if section["anchor_id"] == anchor_id:
                return section
    return None


def get_high_priority_sections(manifest: dict) -> list[dict]:
    """Extract high-priority sections (Objectives, Requirements, etc.)."""
    priority_keywords = [
        "objective",
        "requirement",
        "specification",
        "data structure",
        "interface",
        "api",
        "schema",
        "contract",
    ]

    high_priority = []
    for source in manifest.get("source_files", []):
        for section in source.get("sections", []):
            title_lower = section["title"].lower()
            if any(kw in title_lower for kw in priority_keywords):
                high_priority.append(section)

    return high_priority
