"""Unit tests for planner-specific workflow validation."""

from __future__ import annotations

import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import yaml

from juvenal.plan_validation import validate_planned_workflow
from juvenal.workflow import make_command_check_prompt


def _base_structure() -> dict:
    return {
        "linear": True,
        "yaml_source_mode": "inline-only",
        "verifier_encoding": "explicit-phases",
        "phases": [
            {
                "order": 1,
                "id": "prepare",
                "type": "implement",
                "bounce_target": None,
                "required_preexisting_inputs": ["original/"],
            },
            {
                "order": 2,
                "id": "prepare-test",
                "type": "check",
                "bounce_target": "prepare",
                "required_preexisting_inputs": ["original/"],
            },
            {
                "order": 3,
                "id": "prepare-review",
                "type": "check",
                "bounce_target": "prepare",
                "required_preexisting_inputs": ["original/"],
            },
        ],
    }


def _base_workflow() -> dict:
    return {
        "name": "planned",
        "backend": "codex",
        "phases": [
            {
                "id": "prepare",
                "prompt": (
                    "Preexisting Inputs:\n- original/\n\n"
                    "New Outputs:\n- updated tree\n\n"
                    "Commit work to git before yielding."
                ),
            },
            {
                "id": "prepare-test",
                "type": "check",
                "prompt": make_command_check_prompt("pytest -q"),
                "bounce_target": "prepare",
            },
            {
                "id": "prepare-review",
                "type": "check",
                "bounce_target": "prepare",
                "prompt": "Role: Tester.\nRespond with VERDICT: PASS or VERDICT: FAIL: <reason>.",
            },
        ],
    }


def _smoke_structure() -> dict:
    return {
        "linear": True,
        "yaml_source_mode": "inline-only",
        "verifier_encoding": "explicit-phases",
        "required_preexisting_paths": ["original/"],
        "phases": [
            {"id": "analyze-prepared-inputs", "type": "implement"},
            {
                "id": "analyze-prepared-inputs-test",
                "type": "check",
                "bounce_target": "analyze-prepared-inputs",
            },
            {
                "id": "analyze-prepared-inputs-review",
                "type": "check",
                "bounce_target": "analyze-prepared-inputs",
            },
        ],
    }


def _smoke_workflow() -> dict:
    return {
        "name": "smoke",
        "backend": "codex",
        "phases": [
            {
                "id": "analyze-prepared-inputs",
                "prompt": "Inspect the prepared inputs and commit any changes before yielding.",
            },
            {
                "id": "analyze-prepared-inputs-test",
                "type": "check",
                "prompt": make_command_check_prompt("true"),
                "bounce_target": "analyze-prepared-inputs",
            },
            {
                "id": "analyze-prepared-inputs-review",
                "type": "check",
                "bounce_target": "analyze-prepared-inputs",
                "prompt": "Role: Tester.\nRespond with VERDICT: PASS or VERDICT: FAIL: <reason>.",
            },
        ],
    }


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _write_case(tmp_path: Path, structure: dict, workflow: dict) -> tuple[Path, Path]:
    structure_path = tmp_path / "workflow-structure.yaml"
    workflow_path = tmp_path / "workflow.yaml"
    _write_yaml(structure_path, structure)
    _write_yaml(workflow_path, workflow)
    return structure_path, workflow_path


def test_validate_planned_workflow_accepts_valid_linear_workflow(tmp_path):
    structure_path, workflow_path = _write_case(tmp_path, _base_structure(), _base_workflow())

    assert validate_planned_workflow(structure_path, workflow_path) == []


def test_plan_validation_module_entrypoint_accepts_valid_linear_workflow(tmp_path):
    structure_path, workflow_path = _write_case(tmp_path, _smoke_structure(), _smoke_workflow())

    result = subprocess.run(
        [sys.executable, "-m", "juvenal.plan_validation", str(structure_path), str(workflow_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""


def test_validate_planned_workflow_rejects_order_id_type_mismatch(tmp_path):
    structure = _base_structure()
    workflow = _base_workflow()
    workflow["phases"][1]["id"] = "different"

    structure_path, workflow_path = _write_case(tmp_path, structure, workflow)
    errors = validate_planned_workflow(structure_path, workflow_path)

    assert any("does not match structure id" in error for error in errors)


def test_validate_planned_workflow_rejects_phase_level_run_key(tmp_path):
    structure = _base_structure()
    workflow = _base_workflow()
    workflow["phases"][1].pop("prompt")
    workflow["phases"][1]["run"] = "pytest -q"

    structure_path, workflow_path = _write_case(tmp_path, structure, workflow)
    errors = validate_planned_workflow(structure_path, workflow_path)

    assert any("run is not supported" in error for error in errors)


def test_validate_planned_workflow_rejects_invalid_jinja_syntax(tmp_path):
    structure = _base_structure()
    workflow = _base_workflow()
    workflow["phases"][0]["prompt"] = "{{ PROJECT"

    structure_path, workflow_path = _write_case(tmp_path, structure, workflow)
    errors = validate_planned_workflow(structure_path, workflow_path)

    assert any("invalid Jinja2 prompt" in error for error in errors)


def test_plan_validation_module_entrypoint_rejects_invalid_jinja_syntax(tmp_path):
    structure = _smoke_structure()
    workflow = _smoke_workflow()
    workflow["phases"][0]["prompt"] = "{{ PROJECT"
    structure_path, workflow_path = _write_case(tmp_path, structure, workflow)

    result = subprocess.run(
        [sys.executable, "-m", "juvenal.plan_validation", str(structure_path), str(workflow_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "invalid Jinja2 prompt" in result.stdout


def test_validate_planned_workflow_rejects_deferred_verifier(tmp_path):
    structure = {
        "linear": True,
        "yaml_source_mode": "inline-only",
        "verifier_encoding": "explicit-phases",
        "phases": [
            {
                "order": 1,
                "id": "prepare",
                "type": "implement",
                "bounce_target": None,
                "required_preexisting_inputs": ["original/"],
            },
            {
                "order": 2,
                "id": "port",
                "type": "implement",
                "bounce_target": None,
                "required_preexisting_inputs": ["original/"],
            },
            {
                "order": 3,
                "id": "prepare-review",
                "type": "check",
                "bounce_target": "prepare",
                "required_preexisting_inputs": ["original/"],
            },
        ],
    }
    workflow = {
        "name": "planned",
        "backend": "codex",
        "phases": [
            {"id": "prepare", "prompt": "Commit work to git before yielding."},
            {"id": "port", "prompt": "Commit work to git before yielding."},
            {
                "id": "prepare-review",
                "type": "check",
                "bounce_target": "prepare",
                "prompt": "Role: Tester.\nRespond with VERDICT: PASS or VERDICT: FAIL: <reason>.",
            },
        ],
    }

    structure_path, workflow_path = _write_case(tmp_path, structure, workflow)
    errors = validate_planned_workflow(structure_path, workflow_path)

    assert any("immediately preceding implement phase 'port'" in error for error in errors)


def test_validate_planned_workflow_rejects_missing_verifier_bounce_target(tmp_path):
    structure = _base_structure()
    workflow = _base_workflow()
    del workflow["phases"][2]["bounce_target"]

    structure_path, workflow_path = _write_case(tmp_path, structure, workflow)
    errors = validate_planned_workflow(structure_path, workflow_path)

    assert any("must define a single bounce_target" in error for error in errors)


def test_validate_planned_workflow_rejects_bounce_targets_lists(tmp_path):
    structure = _base_structure()
    workflow = _base_workflow()
    workflow["phases"][2]["bounce_targets"] = ["prepare"]

    structure_path, workflow_path = _write_case(tmp_path, structure, workflow)
    errors = validate_planned_workflow(structure_path, workflow_path)

    assert any("bounce_targets lists are not allowed" in error for error in errors)


def test_validate_planned_workflow_rejects_parallel_groups(tmp_path):
    structure = _base_structure()
    workflow = _base_workflow()
    workflow["parallel_groups"] = [{"phases": ["prepare", "prepare-review"]}]

    structure_path, workflow_path = _write_case(tmp_path, structure, workflow)
    errors = validate_planned_workflow(structure_path, workflow_path)

    assert any("parallel_groups is not allowed" in error for error in errors)


def test_validate_planned_workflow_rejects_top_level_include(tmp_path):
    structure = _base_structure()
    workflow = _base_workflow()
    workflow["include"] = ["shared.yaml"]

    structure_path, workflow_path = _write_case(tmp_path, structure, workflow)
    errors = validate_planned_workflow(structure_path, workflow_path)

    assert any("top-level include is not allowed" in error for error in errors)


def test_validate_planned_workflow_rejects_checks_indirection(tmp_path):
    structure = deepcopy(_base_structure())
    structure["phases"] = [structure["phases"][0]]
    workflow = {
        "name": "planned",
        "backend": "codex",
        "phases": [
            {
                "id": "prepare",
                "prompt": "Commit work to git before yielding.",
                "checks": [{"run": "pytest -q"}],
            }
        ],
    }

    structure_path, workflow_path = _write_case(tmp_path, structure, workflow)
    errors = validate_planned_workflow(structure_path, workflow_path)

    assert any("checks is not allowed" in error for error in errors)


def test_validate_planned_workflow_rejects_top_level_checks_key(tmp_path):
    structure = deepcopy(_base_structure())
    structure["phases"] = [structure["phases"][0]]
    workflow = {
        "name": "planned",
        "backend": "codex",
        "checks": [{"run": "pytest -q"}],
        "phases": [
            {
                "id": "prepare",
                "prompt": "Commit work to git before yielding.",
            }
        ],
    }

    structure_path, workflow_path = _write_case(tmp_path, structure, workflow)
    errors = validate_planned_workflow(structure_path, workflow_path)

    assert any("top-level checks is not allowed" in error for error in errors)


def test_validate_planned_workflow_rejects_checks_hidden_via_yaml_merge(tmp_path):
    structure = deepcopy(_base_structure())
    structure["phases"] = [structure["phases"][0]]
    workflow_path = tmp_path / "workflow.yaml"
    structure_path = tmp_path / "workflow-structure.yaml"
    _write_yaml(structure_path, structure)
    workflow_path.write_text(
        """\
name: planned
backend: codex
checker_defaults: &checker_defaults
  checks:
    - run: "pytest -q"
phases:
  - <<: *checker_defaults
    id: prepare
    prompt: "Commit work to git before yielding."
""",
        encoding="utf-8",
    )

    errors = validate_planned_workflow(structure_path, workflow_path)

    assert any("checks is not allowed" in error for error in errors)


def test_validate_planned_workflow_rejects_phase_level_prompt_file(tmp_path):
    structure = deepcopy(_base_structure())
    structure["phases"] = [structure["phases"][0]]
    workflow = {
        "name": "planned",
        "backend": "codex",
        "phases": [
            {
                "id": "prepare",
                "prompt": "Commit work to git before yielding.",
                "prompt_file": "prompt.md",
            }
        ],
    }

    structure_path, workflow_path = _write_case(tmp_path, structure, workflow)
    errors = validate_planned_workflow(structure_path, workflow_path)

    assert any("prompt_file is not allowed" in error for error in errors)


def test_validate_planned_workflow_rejects_phase_level_workflow_file(tmp_path):
    structure = deepcopy(_base_structure())
    structure["phases"] = [structure["phases"][0]]
    workflow = {
        "name": "planned",
        "backend": "codex",
        "phases": [
            {
                "id": "prepare",
                "prompt": "Commit work to git before yielding.",
                "workflow_file": "child.yaml",
            }
        ],
    }

    structure_path, workflow_path = _write_case(tmp_path, structure, workflow)
    errors = validate_planned_workflow(structure_path, workflow_path)

    assert any("workflow_file is not allowed" in error for error in errors)


def test_validate_planned_workflow_rejects_phase_level_workflow_dir(tmp_path):
    structure = deepcopy(_base_structure())
    structure["phases"] = [structure["phases"][0]]
    workflow = {
        "name": "planned",
        "backend": "codex",
        "phases": [
            {
                "id": "prepare",
                "prompt": "Commit work to git before yielding.",
                "workflow_dir": "sub-workflow",
            }
        ],
    }

    structure_path, workflow_path = _write_case(tmp_path, structure, workflow)
    errors = validate_planned_workflow(structure_path, workflow_path)

    assert any("workflow_dir is not allowed" in error for error in errors)
