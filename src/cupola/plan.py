"""Deterministic task graph compilation and validation.

Implements plan.json compilation with JSON schema validation,
Kahn's algorithm for cycle detection, and verification target
enforcement.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class TaskStatus(Enum):
    """Task execution status."""
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class TaskPhase(Enum):
    """Task implementation phase."""
    SCHEMA = "SCHEMA"
    CORE_MATH = "CORE_MATH"
    INTEGRATION = "INTEGRATION"
    TESTS = "TESTS"
    DOCS = "DOCS"


@dataclass
class Task:
    """A single task in the plan DAG."""
    id: str
    title: str
    phase: TaskPhase
    status: TaskStatus = TaskStatus.PENDING
    source_anchors: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    target_files: list[str] = field(default_factory=list)
    verification: Optional[dict] = None
    checkpoint_ref: Optional[str] = None


@dataclass
class Plan:
    """Complete task plan DAG."""
    plan_version: str
    project_name: str
    document_baseline_hash: Optional[str]
    tasks: list[Task] = field(default_factory=list)

    def get_task(self, task_id: str) -> Optional[Task]:
        """Get task by ID."""
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def get_ready_tasks(self) -> list[Task]:
        """Get tasks that are ready to execute (all deps completed)."""
        ready = []
        for task in self.tasks:
            if task.status != TaskStatus.PENDING:
                continue
            # Check all dependencies are completed
            all_deps_done = True
            for dep_id in task.dependencies:
                dep_task = self.get_task(dep_id)
                if dep_task is None or dep_task.status != TaskStatus.COMPLETED:
                    all_deps_done = False
                    break
            if all_deps_done:
                ready.append(task)
        return ready


def validate_plan_schema(plan_data: dict) -> list[str]:
    """Validate plan JSON against schema.

    Returns list of error messages (empty if valid).
    """
    errors = []

    # Required top-level fields
    for field_name in ["plan_version", "project_name", "tasks"]:
        if field_name not in plan_data:
            errors.append(f"Missing required field: {field_name}")

    # Validate tasks
    tasks = plan_data.get("tasks", [])
    if not isinstance(tasks, list):
        errors.append("tasks must be an array")
    else:
        task_ids = set()
        for i, task in enumerate(tasks):
            if not isinstance(task, dict):
                errors.append(f"Task {i} must be an object")
                continue

            # Required task fields
            for field_name in ["id", "title", "phase", "dependencies", "verification"]:
                if field_name not in task:
                    errors.append(f"Task {i} missing required field: {field_name}")

            # Validate task ID uniqueness
            task_id = task.get("id", f"task_{i}")
            if task_id in task_ids:
                errors.append(f"Duplicate task id: {task_id}")
            task_ids.add(task_id)

            # Validate dependencies reference existing tasks
            deps = task.get("dependencies", [])
            if not isinstance(deps, list):
                errors.append(f"Task {task_id} dependencies must be an array")
            else:
                for dep in deps:
                    if dep not in task_ids and dep not in [t.get("id") for t in tasks]:
                        errors.append(f"Task {task_id} depends on unknown task: {dep}")

            # Validate verification target
            verification = task.get("verification", {})
            if not isinstance(verification, dict):
                errors.append(f"Task {task_id} verification must be an object")
            elif "command" not in verification:
                errors.append(f"Task {task_id} missing verification command")

    return errors


def detect_cycles(tasks: list[dict]) -> list[list[str]]:
    """Detect cycles in task dependency graph using Kahn's algorithm.

    Returns list of cycles found (empty if acyclic).
    """
    # Build adjacency list and in-degree map
    graph = defaultdict(list)
    in_degree = defaultdict(int)
    task_ids = set()

    for task in tasks:
        task_id = task["id"]
        task_ids.add(task_id)
        for dep in task.get("dependencies", []):
            graph[dep].append(task_id)
            in_degree[task_id] += 1

    # Kahn's algorithm: start with nodes that have no incoming edges
    queue = deque([tid for tid in task_ids if in_degree[tid] == 0])
    visited = set()

    while queue:
        node = queue.popleft()
        visited.add(node)
        for neighbor in graph[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    # Any node not visited is part of a cycle
    cycle_nodes = task_ids - visited
    if cycle_nodes:
        # Find actual cycles
        cycles = []
        for node in cycle_nodes:
            cycle = [node]
            current = node
            while True:
                # Find next node in cycle
                next_node = None
                for task in tasks:
                    if task["id"] == current:
                        for dep in task.get("dependencies", []):
                            if dep in cycle_nodes:
                                next_node = dep
                                break
                        break
                if next_node is None or next_node == node:
                    break
                cycle.append(next_node)
                current = next_node
            cycles.append(cycle)
        return cycles
    return []


def compile_plan(plan_data: dict) -> tuple[Plan, list[str]]:
    """Compile and validate a plan from JSON data.

    Args:
        plan_data: Raw plan JSON dictionary.

    Returns:
        Tuple of (Plan object, list of errors).
    """
    errors = validate_plan_schema(plan_data)
    if errors:
        return Plan(
            plan_version=plan_data.get("plan_version", "0.0.0"),
            project_name=plan_data.get("project_name", "unknown"),
            document_baseline_hash=plan_data.get("document_baseline_hash"),
        ), errors

    # Check for cycles
    cycles = detect_cycles(plan_data.get("tasks", []))
    if cycles:
        for cycle in cycles:
            errors.append(f"Circular dependency detected: {' -> '.join(cycle)}")
        return Plan(
            plan_version=plan_data.get("plan_version", "0.0.0"),
            project_name=plan_data.get("project_name", "unknown"),
            document_baseline_hash=plan_data.get("document_baseline_hash"),
        ), errors

    # Build Plan object
    plan = Plan(
        plan_version=plan_data.get("plan_version", "1.0.0"),
        project_name=plan_data.get("project_name", "unknown"),
        document_baseline_hash=plan_data.get("document_baseline_hash"),
    )

    for task_data in plan_data.get("tasks", []):
        task = Task(
            id=task_data["id"],
            title=task_data["title"],
            phase=TaskPhase(task_data["phase"]),
            status=TaskStatus(task_data.get("status", "PENDING")),
            source_anchors=task_data.get("source_anchors", []),
            dependencies=task_data.get("dependencies", []),
            target_files=task_data.get("target_files", []),
            verification=task_data.get("verification"),
            checkpoint_ref=task_data.get("checkpoint_ref"),
        )
        plan.tasks.append(task)

    return plan, errors


def write_plan(plan: Plan, output_path: Path) -> None:
    """Write plan to JSON file."""
    plan_data = {
        "plan_version": plan.plan_version,
        "project_name": plan.project_name,
        "document_baseline_hash": plan.document_baseline_hash,
        "tasks": [
            {
                "id": task.id,
                "title": task.title,
                "phase": task.phase.value,
                "status": task.status.value,
                "source_anchors": task.source_anchors,
                "dependencies": task.dependencies,
                "target_files": task.target_files,
                "verification": task.verification,
                "checkpoint_ref": task.checkpoint_ref,
            }
            for task in plan.tasks
        ],
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(plan_data, f, indent=2)


def load_plan(plan_path: Path) -> Optional[Plan]:
    """Load plan from JSON file."""
    try:
        with open(plan_path, "r", encoding="utf-8") as f:
            plan_data = json.load(f)
        plan, errors = compile_plan(plan_data)
        if errors:
            return None
        return plan
    except (OSError, json.JSONDecodeError):
        return None
