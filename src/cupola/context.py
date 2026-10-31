"""Context window packaging for planning.

Packages document manifest into a prompt that fits within the
16,384 token context window, prioritizing structural outline and
high-priority sections.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from cupola.manifest import get_high_priority_sections, get_section_by_anchor


@dataclass
class PackagedContext:
    """Packaged context for LLM planning."""
    prompt: str
    token_estimate: int
    included_anchors: list[str]
    truncated: bool


def _estimate_tokens(text: str) -> int:
    """Estimate token count (rough heuristic: ~4 chars per token)."""
    return max(1, len(text) // 4)


def package_for_planning(
    manifest: dict,
    max_tokens: int = 16384,
    task_description: Optional[str] = None,
) -> PackagedContext:
    """Package document manifest for planning prompt.

    Includes:
    1. Complete structural outline (TOC + signatures)
    2. High-priority requirement sections
    3. Task description (if provided)

    Uses anchor ID references instead of raw text to save tokens.
    """
    prompt_parts = []
    included_anchors = []

    # Task description (highest priority)
    if task_description:
        prompt_parts.append(f"TASK: {task_description}\n")

    # Document outline
    prompt_parts.append("DOCUMENT OUTLINE:\n")
    for source in manifest.get("source_files", []):
        prompt_parts.append(f"\nFile: {source['path']}\n")
        for section in source.get("sections", []):
            prompt_parts.append(
                f"  [{section['anchor_id']}] {section['title']} "
                f"({section['token_count']} tokens)\n"
            )

    # High-priority sections
    high_priority = get_high_priority_sections(manifest)
    if high_priority:
        prompt_parts.append("\nHIGH-PRIORITY SECTIONS:\n")
        for section in high_priority:
            prompt_parts.append(f"\n[{section['anchor_id']}] {section['title']}\n")
            prompt_parts.append(f"{section['summary']}\n")
            included_anchors.append(section["anchor_id"])

    # Build full prompt
    prompt = "\n".join(prompt_parts)

    # Estimate tokens
    token_estimate = _estimate_tokens(prompt)

    # Check if we need to truncate
    truncated = False
    if token_estimate > max_tokens:
        # Truncate to fit within budget
        # Keep task description and outline, drop some high-priority sections
        chars_to_keep = max_tokens * 4  # Rough estimate
        prompt = prompt[:chars_to_keep]
        truncated = True
        token_estimate = max_tokens

    return PackagedContext(
        prompt=prompt,
        token_estimate=token_estimate,
        included_anchors=included_anchors,
        truncated=truncated,
    )


def package_section_for_task(
    manifest: dict,
    anchor_id: str,
    max_tokens: int = 8192,
) -> Optional[str]:
    """Package a specific section for task execution.

    Returns the section content or None if anchor not found.
    """
    section = get_section_by_anchor(manifest, anchor_id)
    if section is None:
        return None

    # For task execution, include the full section content
    # (not just the summary)
    return f"[{anchor_id}] {section['title']}\n{section['summary']}"


def build_planning_prompt(
    manifest: dict,
    task_description: str,
) -> str:
    """Build a complete planning prompt from manifest and task description."""
    context = package_for_planning(manifest, task_description=task_description)

    prompt = f"""{context.prompt}

INSTRUCTIONS:
Based on the document outline and high-priority sections above, compile a
deterministic task DAG (plan.json) for implementing the requirements.

Requirements:
1. Each task must have a unique id, title, phase, and verification target
2. Tasks must be leaf-level (concrete, implementable units)
3. Dependencies must form a DAG (no cycles)
4. Every task must reference source anchor IDs
5. Include checkpoint_ref for each task

Output the plan as JSON conforming to the plan.json schema."""

    return prompt
