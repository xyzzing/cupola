"""Tests for plan compilation and validation."""

import tempfile
from pathlib import Path

import pytest

from cupola.plan import (
    Plan,
    Task,
    TaskPhase,
    TaskStatus,
    compile_plan,
    detect_cycles,
    load_plan,
    validate_plan_schema,
    write_plan,
)


def test_validate_plan_schema_valid():
    """Valid plan passes schema validation."""
    plan_data = {
        "plan_version": "1.0.0",
        "project_name": "test-project",
        "tasks": [
            {
                "id": "task-1",
                "title": "Task 1",
                "phase": "SCHEMA",
                "dependencies": [],
                "verification": {"command": "echo test"},
            }
        ],
    }

    errors = validate_plan_schema(plan_data)
    assert errors == []


def test_validate_plan_schema_missing_fields():
    """Missing required fields are detected."""
    plan_data = {
        "plan_version": "1.0.0",
    }

    errors = validate_plan_schema(plan_data)
    assert any("project_name" in e for e in errors)
    assert any("tasks" in e for e in errors)


def test_validate_plan_schema_duplicate_ids():
    """Duplicate task IDs are detected."""
    plan_data = {
        "plan_version": "1.0.0",
        "project_name": "test",
        "tasks": [
            {"id": "task-1", "title": "T1", "phase": "SCHEMA",
             "dependencies": [], "verification": {}},
            {"id": "task-1", "title": "T2", "phase": "SCHEMA",
             "dependencies": [], "verification": {}},
        ],
    }

    errors = validate_plan_schema(plan_data)
    assert any("Duplicate" in e for e in errors)


def test_detect_cycles_no_cycles():
    """Acyclic graph passes cycle detection."""
    tasks = [
        {"id": "a", "dependencies": []},
        {"id": "b", "dependencies": ["a"]},
        {"id": "c", "dependencies": ["b"]},
    ]

    cycles = detect_cycles(tasks)
    assert cycles == []


def test_detect_cycles_circular_dependency():
    """Circular dependencies are detected."""
    tasks = [
        {"id": "a", "dependencies": ["b"]},
        {"id": "b", "dependencies": ["a"]},
    ]

    cycles = detect_cycles(tasks)
    assert len(cycles) > 0


def test_compile_plan_valid():
    """Valid plan compiles successfully."""
    plan_data = {
        "plan_version": "1.0.0",
        "project_name": "test-project",
        "document_baseline_hash": "abc123",
        "tasks": [
            {
                "id": "task-1",
                "title": "Task 1",
                "phase": "SCHEMA",
                "dependencies": [],
                "verification": {"command": "echo test"},
            }
        ],
    }

    plan, errors = compile_plan(plan_data)
    assert errors == []
    assert plan.project_name == "test-project"
    assert len(plan.tasks) == 1
    assert plan.tasks[0].id == "task-1"


def test_compile_plan_with_cycles():
    """Plan with cycles fails compilation."""
    plan_data = {
        "plan_version": "1.0.0",
        "project_name": "test",
        "tasks": [
            {"id": "a", "title": "A", "phase": "SCHEMA",
             "dependencies": ["b"], "verification": {"command": "test"}},
            {"id": "b", "title": "B", "phase": "SCHEMA",
             "dependencies": ["a"], "verification": {"command": "test"}},
        ],
    }

    plan, errors = compile_plan(plan_data)
    assert len(errors) > 0
    # Error should mention cycle or circular
    assert any("cycle" in e.lower() or "circular" in e.lower() for e in errors)


def test_write_and_load_plan():
    """Plan can be written and loaded."""
    with tempfile.TemporaryDirectory() as tmpdir:
        plan_data = {
            "plan_version": "1.0.0",
            "project_name": "test",
            "tasks": [
                {
                    "id": "task-1",
                    "title": "Task 1",
                    "phase": "SCHEMA",
                    "dependencies": [],
                    "verification": {"command": "echo test"},
                }
            ],
        }

        plan, errors = compile_plan(plan_data)
        assert errors == []

        plan_path = Path(tmpdir) / "plan.json"
        write_plan(plan, plan_path)

        loaded = load_plan(plan_path)
        assert loaded is not None
        assert loaded.project_name == "test"
        assert len(loaded.tasks) == 1


def test_get_ready_tasks():
    """Ready tasks are identified correctly."""
    plan = Plan(
        plan_version="1.0.0",
        project_name="test",
        document_baseline_hash=None,
    )

    # Task A has no dependencies (ready)
    task_a = Task(id="a", title="A", phase=TaskPhase.SCHEMA)
    # Task B depends on A (not ready)
    task_b = Task(id="b", title="B", phase=TaskPhase.SCHEMA, dependencies=["a"])

    plan.tasks = [task_a, task_b]

    ready = plan.get_ready_tasks()
    assert len(ready) == 1
    assert ready[0].id == "a"

    # After completing A, B becomes ready
    task_a.status = TaskStatus.COMPLETED
    ready = plan.get_ready_tasks()
    assert len(ready) == 1
    assert ready[0].id == "b"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
