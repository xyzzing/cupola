"""Document structural parsing and chunking.

Parses markdown, source code, and CSV files into hierarchical
trees with anchor IDs for context-window packaging.
"""

from __future__ import annotations

import ast
import csv
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Section:
    """A parsed document section."""
    anchor_id: str
    title: str
    content: str
    token_count: int
    summary: str = ""
    children: list["Section"] = field(default_factory=list)
    byte_offset: int = 0


def _generate_anchor_id(title: str, index: int) -> str:
    """Generate a stable anchor ID from a title."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
    return f"sec_{index:02d}_{slug}"


def _estimate_tokens(text: str) -> int:
    """Estimate token count (rough heuristic: ~4 chars per token)."""
    return max(1, len(text) // 4)


def _extract_summary(content: str, max_chars: int = 200) -> str:
    """Extract a brief summary from content."""
    # Take first non-empty line or first few words
    lines = content.strip().split("\n")
    for line in lines:
        line = line.strip()
        if line and len(line) > 10:
            if len(line) <= max_chars:
                return line
            return line[:max_chars] + "..."
    return content[:max_chars]


def parse_markdown(path: Path) -> list[Section]:
    """Parse a markdown file into a hierarchical section tree.

    Uses # headers to identify sections.
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    sections = []
    current_section = None
    section_index = 0

    lines = content.split("\n")
    for i, line in enumerate(lines):
        # Check for header
        header_match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if header_match:
            title = header_match.group(2).strip()

            # Start new section
            section_index += 1
            section = Section(
                anchor_id=_generate_anchor_id(title, section_index),
                title=title,
                content="",
                token_count=0,
                byte_offset=i,
            )

            if current_section is not None:
                sections.append(current_section)

            current_section = section
        elif current_section is not None:
            # Add line to current section
            current_section.content += line + "\n"

    # Don't forget the last section
    if current_section is not None:
        sections.append(current_section)

    # Compute token counts and summaries
    for section in sections:
        section.token_count = _estimate_tokens(section.content)
        section.summary = _extract_summary(section.content)

    return sections


def parse_python_file(path: Path) -> list[Section]:
    """Parse a Python file into sections (classes, functions)."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    sections = []
    try:
        tree = ast.parse(content, filename=str(path))
    except SyntaxError:
        # Fall back to treating entire file as one section
        section = Section(
            anchor_id="sec_01_file",
            title=path.name,
            content=content,
            token_count=_estimate_tokens(content),
            summary=_extract_summary(content),
        )
        return [section]

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            title = f"class {node.name}"
            anchor = _generate_anchor_id(title, len(sections) + 1)
            # Get the class source (rough approximation)
            start_line = node.lineno
            end_line = node.lineno
            for child in ast.walk(node):
                if hasattr(child, "lineno") and child.lineno > end_line:
                    end_line = child.lineno
            lines = content.split("\n")
            class_content = "\n".join(lines[start_line - 1 : end_line])

            section = Section(
                anchor_id=anchor,
                title=title,
                content=class_content,
                token_count=_estimate_tokens(class_content),
                summary=f"Class {node.name}",
            )
            sections.append(section)

        elif isinstance(node, ast.FunctionDef):
            title = f"def {node.name}"
            anchor = _generate_anchor_id(title, len(sections) + 1)
            start_line = node.lineno
            end_line = node.lineno
            for child in ast.walk(node):
                if hasattr(child, "lineno") and child.lineno > end_line:
                    end_line = child.lineno
            lines = content.split("\n")
            func_content = "\n".join(lines[start_line - 1 : end_line])

            section = Section(
                anchor_id=anchor,
                title=title,
                content=func_content,
                token_count=_estimate_tokens(func_content),
                summary=f"Function {node.name}",
            )
            sections.append(section)

    return sections


def parse_code_file(path: Path) -> list[Section]:
    """Parse a source code file based on extension."""
    if path.suffix == ".py":
        return parse_python_file(path)

    # For other languages, use regex to find function/class signatures
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    sections = []
    # Look for function-like patterns
    patterns = [
        (r"(?:public|private|protected)?\s*(?:static)?\s*\w+\s+(\w+)\s*\([^)]*\)", "function"),
        (r"fn\s+(\w+)\s*\([^)]*\)", "function"),
        (r"(?:class|struct|interface)\s+(\w+)", "type"),
    ]

    for pattern, kind in patterns:
        for match in re.finditer(pattern, content):
            name = match.group(1)
            title = f"{kind} {name}"
            anchor = _generate_anchor_id(title, len(sections) + 1)

            section = Section(
                anchor_id=anchor,
                title=title,
                content=match.group(0),
                token_count=_estimate_tokens(match.group(0)),
                summary=f"{kind.capitalize()} {name}",
            )
            sections.append(section)

    if not sections:
        # No patterns found, treat entire file as one section
        section = Section(
            anchor_id="sec_01_file",
            title=path.name,
            content=content,
            token_count=_estimate_tokens(content),
            summary=_extract_summary(content),
        )
        sections = [section]

    return sections


def parse_csv_file(path: Path) -> list[Section]:
    """Parse a CSV file into schema and data sections."""
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        try:
            headers = next(reader)
        except StopIteration:
            return []

    # Schema section
    schema_section = Section(
        anchor_id="sec_01_schema",
        title=f"Schema: {path.name}",
        content=", ".join(headers),
        token_count=_estimate_tokens(", ".join(headers)),
        summary=f"CSV with {len(headers)} columns: {', '.join(headers[:5])}",
    )

    # Count rows
    with open(path, "r", encoding="utf-8") as f:
        row_count = sum(1 for _ in f) - 1

    # Data summary section
    data_section = Section(
        anchor_id="sec_02_data",
        title=f"Data: {path.name}",
        content=f"{row_count} rows",
        token_count=_estimate_tokens(f"{row_count} rows"),
        summary=f"{row_count} data rows",
    )

    return [schema_section, data_section]


def parse_document(path: Path) -> list[Section]:
    """Parse any document based on file extension."""
    suffix = path.suffix.lower()

    if suffix in (".md", ".markdown"):
        return parse_markdown(path)
    elif suffix == ".py":
        return parse_python_file(path)
    elif suffix in (".js", ".ts", ".c", ".cpp", ".h", ".rs", ".go"):
        return parse_code_file(path)
    elif suffix in (".csv", ".tsv"):
        return parse_csv_file(path)
    else:
        # Fall back to treating as plain text
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        section = Section(
            anchor_id="sec_01_file",
            title=path.name,
            content=content,
            token_count=_estimate_tokens(content),
            summary=_extract_summary(content),
        )
        return [section]
