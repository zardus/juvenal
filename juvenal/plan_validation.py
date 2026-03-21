"""Planner-specific validation for generated workflow YAML."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

_VERIFIER_TYPES = {"check", "script"}
_ALLOWED_PHASE_TYPES = {"implement", "check", "script"}
_INLINE_ONLY_FORBIDDEN_PHASE_KEYS = ("prompt_file", "workflow_file", "workflow_dir")


def _read_yaml(path: Path, label: str) -> tuple[Any | None, list[str]]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, [f"{label} file not found: {path}"]
    except OSError as exc:
        return None, [f"{label} file is unreadable: {path}: {exc}"]
    except yaml.YAMLError as exc:
        return None, [f"{label} file is not valid YAML: {path}: {exc}"]
    return raw, []


def _phase_type(phase_data: dict[str, Any]) -> Any:
    return phase_data.get("type", "implement")


def validate_planned_workflow(structure_path: Path, workflow_path: Path) -> list[str]:
    """Validate planner-produced workflow rules that generic workflow validation does not cover."""

    errors: list[str] = []

    structure_raw, structure_errors = _read_yaml(structure_path, "Structure")
    workflow_raw, workflow_errors = _read_yaml(workflow_path, "Workflow")
    errors.extend(structure_errors)
    errors.extend(workflow_errors)
    if structure_errors or workflow_errors:
        return errors

    if not isinstance(structure_raw, dict):
        return ["Structure file must be a YAML mapping"]
    if not isinstance(workflow_raw, dict):
        return ["Workflow file must be a YAML mapping"]

    structure_phases = structure_raw.get("phases")
    workflow_phases = workflow_raw.get("phases")
    if not isinstance(structure_phases, list):
        errors.append("Structure file must define phases as a list")
    if not isinstance(workflow_phases, list):
        errors.append("Workflow file must define phases as a list")
    if errors:
        return errors

    linear = structure_raw.get("linear")
    if linear is not True:
        errors.append("Structure file must set linear: true")

    yaml_source_mode = structure_raw.get("yaml_source_mode")
    if yaml_source_mode != "inline-only":
        errors.append("Structure file must set yaml_source_mode: inline-only")

    verifier_encoding = structure_raw.get("verifier_encoding")
    if verifier_encoding != "explicit-phases":
        errors.append("Structure file must set verifier_encoding: explicit-phases")

    if linear is True and "parallel_groups" in workflow_raw:
        errors.append("parallel_groups is not allowed when linear: true")

    if yaml_source_mode == "inline-only" and "include" in workflow_raw:
        errors.append("top-level include is not allowed when yaml_source_mode is inline-only")

    expected_count = len(structure_phases)
    actual_count = len(workflow_phases)
    if actual_count != expected_count:
        errors.append(
            f"Workflow phase count {actual_count} does not match structure phase count {expected_count}"
        )

    shared_count = min(expected_count, actual_count)
    for index in range(shared_count):
        structure_phase = structure_phases[index]
        workflow_phase = workflow_phases[index]
        phase_number = index + 1

        if not isinstance(structure_phase, dict):
            errors.append(f"Structure phase {phase_number} must be a YAML mapping")
            continue
        if not isinstance(workflow_phase, dict):
            errors.append(f"Workflow phase {phase_number} must be a YAML mapping")
            continue

        order = structure_phase.get("order")
        if order != phase_number:
            errors.append(f"Structure phase {phase_number} must set order: {phase_number}")

        structure_id = structure_phase.get("id")
        workflow_id = workflow_phase.get("id")
        if not isinstance(structure_id, str) or not structure_id:
            errors.append(f"Structure phase {phase_number} must define a non-empty id")
        if structure_id != workflow_id:
            errors.append(
                f"Workflow phase {phase_number} id {workflow_id!r} does not match structure id {structure_id!r}"
            )

        structure_type = structure_phase.get("type")
        workflow_type = _phase_type(workflow_phase)
        if structure_type not in _ALLOWED_PHASE_TYPES:
            errors.append(
                f"Structure phase {structure_id!r}: invalid type {structure_type!r} "
                f"(must be one of {sorted(_ALLOWED_PHASE_TYPES)})"
            )
        if workflow_type != structure_type:
            errors.append(
                f"Workflow phase {workflow_id!r}: type {workflow_type!r} does not match structure type "
                f"{structure_type!r}"
            )

        required_inputs = structure_phase.get("required_preexisting_inputs")
        if not isinstance(required_inputs, list) or any(not isinstance(item, str) for item in required_inputs):
            errors.append(
                "Structure phase "
                f"{structure_id or phase_number!r}: required_preexisting_inputs must be a list of strings"
            )

        structure_bounce = structure_phase.get("bounce_target")
        if (
            "bounce_target" in structure_phase
            and structure_bounce is not None
            and not isinstance(structure_bounce, str)
        ):
            errors.append(f"Structure phase {structure_id!r}: bounce_target must be a string or null")

        workflow_bounce = workflow_phase.get("bounce_target")
        if workflow_bounce != structure_bounce:
            errors.append(
                f"Workflow phase {workflow_id!r}: bounce_target {workflow_bounce!r} does not match structure "
                f"bounce_target {structure_bounce!r}"
            )

        if linear is True and "bounce_targets" in workflow_phase:
            errors.append(f"Workflow phase {workflow_id!r}: bounce_targets lists are not allowed when linear: true")

        if verifier_encoding == "explicit-phases" and "checks" in workflow_phase:
            errors.append(
                f"Workflow phase {workflow_id!r}: checks is not allowed when verifier_encoding is explicit-phases"
            )

        if yaml_source_mode == "inline-only":
            for forbidden_key in _INLINE_ONLY_FORBIDDEN_PHASE_KEYS:
                if forbidden_key in workflow_phase:
                    errors.append(
                        f"Workflow phase {workflow_id!r}: {forbidden_key} is not allowed when "
                        "yaml_source_mode is inline-only"
                    )

    last_implement_id: str | None = None
    for phase_data in workflow_phases:
        if not isinstance(phase_data, dict):
            continue
        phase_id = phase_data.get("id", "<unknown>")
        phase_type = _phase_type(phase_data)
        if phase_type == "implement":
            last_implement_id = phase_id if isinstance(phase_id, str) and phase_id else None
            continue
        if phase_type not in _VERIFIER_TYPES:
            continue
        bounce_target = phase_data.get("bounce_target")
        if not isinstance(bounce_target, str) or not bounce_target:
            errors.append(f"Workflow verifier phase {phase_id!r} must define a single bounce_target")
        if last_implement_id is None:
            errors.append(f"Workflow verifier phase {phase_id!r} must follow an implement phase")
        elif bounce_target != last_implement_id:
            errors.append(
                f"Workflow verifier phase {phase_id!r} must bounce to the immediately preceding implement phase "
                f"{last_implement_id!r}, got {bounce_target!r}"
            )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate planner-generated workflow structure and topology.")
    parser.add_argument("structure", help="Path to .plan/workflow-structure.yaml")
    parser.add_argument("workflow", help="Path to generated workflow.yaml")
    args = parser.parse_args(argv)

    errors = validate_planned_workflow(Path(args.structure), Path(args.workflow))
    if errors:
        for error in errors:
            print(error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
