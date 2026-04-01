"""Unit tests for workflow validation."""

from __future__ import annotations

import argparse

import pytest

from juvenal.cli import build_parser, cmd_validate
from juvenal.workflow import (
    ParallelGroup,
    Phase,
    Workflow,
    expand_multi_vars,
    make_command_check_prompt,
    validate_workflow,
)


class TestValidateWorkflow:
    def test_valid_workflow(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Set up."),
                Phase(id="check", type="check", prompt=make_command_check_prompt("true")),
            ],
        )
        assert validate_workflow(wf) == []

    def test_duplicate_phase_ids(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Set up."),
                Phase(id="setup", type="implement", prompt="Again."),
            ],
        )
        errors = validate_workflow(wf)
        assert any("Duplicate phase ID" in e for e in errors)

    def test_invalid_phase_type(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="invalid", prompt="Set up."),
            ],
        )
        errors = validate_workflow(wf)
        assert any("invalid type" in e for e in errors)

    def test_invalid_bounce_target(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Set up.", bounce_target="nonexistent"),
            ],
        )
        errors = validate_workflow(wf)
        assert any("bounce_target" in e and "nonexistent" in e for e in errors)

    def test_valid_bounce_target(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Set up."),
                Phase(id="review", type="check", role="tester", bounce_target="setup"),
            ],
        )
        assert validate_workflow(wf) == []

    def test_implement_missing_prompt(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement"),
            ],
        )
        errors = validate_workflow(wf)
        assert any("has no prompt" in e for e in errors)

    def test_check_missing_prompt_and_role(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="review", type="check"),
            ],
        )
        errors = validate_workflow(wf)
        assert any("no prompt or role" in e for e in errors)

    def test_check_with_role_is_valid(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="review", type="check", role="tester"),
            ],
        )
        assert validate_workflow(wf) == []

    def test_check_with_security_engineer_role_is_valid(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="review", type="check", role="security-engineer"),
            ],
        )
        assert validate_workflow(wf) == []

    def test_check_missing_all_verification_inputs(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="check"),
            ],
        )
        errors = validate_workflow(wf)
        assert any("no prompt or role" in e for e in errors)

    def test_invalid_role(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="review", type="check", role="invalid-role"),
            ],
        )
        errors = validate_workflow(wf)
        assert any("unknown role" in e for e in errors)

    def test_unknown_role_still_fails_after_security_engineer_added(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="review", type="check", role="security-reviewer"),
            ],
        )
        errors = validate_workflow(wf)
        assert any("unknown role" in e and "security-reviewer" in e for e in errors)

    def test_parallel_group_invalid_phase(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Set up."),
            ],
            parallel_groups=[ParallelGroup(phases=["setup", "nonexistent"])],
        )
        errors = validate_workflow(wf)
        assert any("nonexistent" in e for e in errors)

    def test_parallel_group_valid(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
                Phase(id="b", type="implement", prompt="B."),
            ],
            parallel_groups=[ParallelGroup(phases=["a", "b"])],
        )
        assert validate_workflow(wf) == []

    def test_multiple_errors(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="invalid"),
                Phase(id="a", type="check"),
                Phase(id="b", type="check"),
            ],
        )
        errors = validate_workflow(wf)
        assert len(errors) >= 3  # invalid type, duplicate ID, missing verification inputs


class TestTimeoutField:
    def test_timeout_in_yaml(self, tmp_path):
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
    timeout: 120
  - id: check
    type: check
    prompt: "Review the build and emit VERDICT."
    timeout: 30
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        from juvenal.workflow import load_workflow

        wf = load_workflow(yaml_path)
        assert wf.phases[0].timeout == 120
        assert wf.phases[1].timeout == 30

    def test_timeout_default_none(self):
        phase = Phase(id="test", prompt="Test.")
        assert phase.timeout is None

    def test_timeout_in_validation(self):
        """Timeout field shouldn't cause validation errors."""
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="Build.", timeout=60),
            ],
        )
        assert validate_workflow(wf) == []


class TestEnvField:
    def test_env_in_yaml(self, tmp_path):
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
    env:
      NODE_ENV: production
      DEBUG: "true"
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        from juvenal.workflow import load_workflow

        wf = load_workflow(yaml_path)
        assert wf.phases[0].env == {"NODE_ENV": "production", "DEBUG": "true"}

    def test_env_default_empty(self):
        phase = Phase(id="test", prompt="Test.")
        assert phase.env == {}

    def test_env_in_check_phase(self):
        """Check phases remain valid with env metadata."""
        phase = Phase(
            id="review",
            type="check",
            prompt=make_command_check_prompt("echo $TEST_VAR"),
            env={"TEST_VAR": "hello123"},
        )
        assert phase.env == {"TEST_VAR": "hello123"}


class TestWorkflowPhaseValidation:
    def test_workflow_phase_with_prompt_is_valid(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="dynamic", type="workflow", prompt="Build a REST API."),
            ],
        )
        assert validate_workflow(wf) == []

    def test_workflow_phase_missing_prompt(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="dynamic", type="workflow"),
            ],
        )
        errors = validate_workflow(wf)
        assert any("workflow phase needs prompt, workflow_file, or workflow_dir" in e for e in errors)

    def test_workflow_phase_with_role_is_invalid(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="dynamic", type="workflow", prompt="Do it.", role="tester"),
            ],
        )
        errors = validate_workflow(wf)
        assert any("must not have 'role'" in e for e in errors)

    def test_max_depth_less_than_1_is_invalid(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="dynamic", type="workflow", prompt="Do it.", max_depth=0),
            ],
        )
        errors = validate_workflow(wf)
        assert any("max_depth must be >= 1" in e for e in errors)

    def test_max_depth_negative_is_invalid(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="dynamic", type="workflow", prompt="Do it.", max_depth=-1),
            ],
        )
        errors = validate_workflow(wf)
        assert any("max_depth must be >= 1" in e for e in errors)

    def test_max_depth_valid(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="dynamic", type="workflow", prompt="Do it.", max_depth=2),
            ],
        )
        assert validate_workflow(wf) == []

    def test_max_depth_on_non_workflow_phase_invalid(self):
        """max_depth < 1 is invalid regardless of phase type."""
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it.", max_depth=0),
            ],
        )
        errors = validate_workflow(wf)
        assert any("max_depth must be >= 1" in e for e in errors)

    def test_workflow_file_valid(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="sub", type="workflow", workflow_file="/some/path.yaml")],
        )
        assert validate_workflow(wf) == []

    def test_workflow_dir_valid(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="sub", type="workflow", workflow_dir="/some/dir")],
        )
        assert validate_workflow(wf) == []

    def test_workflow_file_and_dir_both_invalid(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="sub", type="workflow", workflow_file="a.yaml", workflow_dir="b/")],
        )
        errors = validate_workflow(wf)
        assert any("mutually exclusive" in e for e in errors)

    def test_workflow_file_on_non_workflow_invalid(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build.", workflow_file="sub.yaml")],
        )
        errors = validate_workflow(wf)
        assert any("only allowed on workflow phases" in e for e in errors)


class TestTemplateVarValidation:
    def test_undefined_var_in_prompt(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build {{PROJECT}}.")],
        )
        errors = validate_workflow(wf)
        assert any("PROJECT" in e and "no value defined" in e for e in errors)

    def test_undefined_var_in_jinja_control_block(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="{% if PROJECT %}Build it.{% endif %}")],
        )
        errors = validate_workflow(wf)
        assert any("PROJECT" in e and "no value defined" in e for e in errors)

    def test_undefined_var_in_check_prompt(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="Build."),
                Phase(
                    id="test",
                    type="check",
                    prompt=make_command_check_prompt("pytest {{DIR}}"),
                    bounce_target="build",
                ),
            ],
        )
        errors = validate_workflow(wf)
        assert any("DIR" in e and "no value defined" in e for e in errors)

    def test_defined_var_passes(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build {{PROJECT}}.")],
            vars={"PROJECT": "myapp"},
        )
        errors = validate_workflow(wf)
        assert not any("no value defined" in e for e in errors)

    def test_multiple_undefined_vars(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Deploy {{APP}} to {{ENV}}.")],
        )
        errors = validate_workflow(wf)
        undefined = [e for e in errors if "no value defined" in e]
        assert len(undefined) == 2

    def test_some_defined_some_not(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Deploy {{APP}} to {{ENV}}.")],
            vars={"APP": "myservice"},
        )
        errors = validate_workflow(wf)
        undefined = [e for e in errors if "no value defined" in e]
        assert len(undefined) == 1
        assert "ENV" in undefined[0]

    def test_no_vars_no_placeholders_passes(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build it.")],
        )
        assert validate_workflow(wf) == []

    def test_duplicate_var_references_single_error(self):
        """Same undefined var referenced multiple times only produces one error per phase."""
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="{{X}} and {{X}} and {{X}}.")],
        )
        errors = validate_workflow(wf)
        undefined = [e for e in errors if "no value defined" in e]
        assert len(undefined) == 1

    def test_invalid_jinja_syntax_in_prompt(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="{{ PROJECT")],
        )
        errors = validate_workflow(wf)
        assert any("invalid Jinja2 prompt" in e for e in errors)

    def test_builtin_jinja_globals_are_not_treated_as_defined(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="{{ cycler }}")],
        )
        errors = validate_workflow(wf)
        assert any("{{cycler}}" in e and "no value defined" in e for e in errors)

    def test_default_filter_allows_undefined_var(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt='{{ missing|default("fallback") }}')],
        )
        assert validate_workflow(wf) == []

    def test_defined_test_allows_guarded_undefined_var(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="{% if missing is defined %}{{ missing }}{% endif %}")],
        )
        assert validate_workflow(wf) == []

    def test_short_circuit_defined_guard_allows_nested_access(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="{% if missing is defined and missing.foo %}x{% endif %}")
            ],
        )
        assert validate_workflow(wf) == []

    def test_elif_branch_missing_var_is_still_validated(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="{% if ok %}A{% elif missing %}B{% endif %}")],
            vars={"ok": False},
        )
        errors = validate_workflow(wf)
        assert any("{{missing}}" in e and "no value defined" in e for e in errors)

    def test_unreachable_else_branch_missing_var_is_ignored(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="{% if ok %}A{% else %}{{ missing }}{% endif %}")],
            vars={"ok": True},
        )
        assert validate_workflow(wf) == []

    def test_validate_workflow_reports_render_error(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="{{ 1 / 0 }}")],
        )
        errors = validate_workflow(wf)
        assert any("Jinja2 render error in prompt for phase 'build'" in e for e in errors)

    def test_validate_workflow_reports_render_error_for_check_prompt_with_role(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="review", type="check", role="tester", prompt="{{ 1 / 0 }}"),
            ],
        )
        errors = validate_workflow(wf)
        assert any("Jinja2 render error in checker prompt for phase 'review'" in e for e in errors)

    def test_expand_multi_vars_preserves_filtered_var_name_for_validation(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="deploy", type="implement", prompt="Deploy {{ app|title }} to {{ ENV }}.")],
            vars={"App": "svc"},
        )
        expanded = expand_multi_vars(wf, {"ENV": ["prod"]})
        errors = validate_workflow(expanded)
        assert any("{{app}}" in e and "no value defined" in e for e in errors)


class TestLaneValidation:
    def test_lane_phase_existence(self):
        """Lane phase IDs must exist in the workflow."""
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
            ],
            parallel_groups=[ParallelGroup(lanes=[["a", "nonexistent"]])],
        )
        errors = validate_workflow(wf)
        assert any("nonexistent" in e for e in errors)

    def test_lane_bounce_target_containment(self):
        """Bounce targets in a lane must stay within that lane."""
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
                Phase(id="check_a", type="check", role="tester", bounce_target="b"),
                Phase(id="b", type="implement", prompt="B."),
                Phase(id="check_b", type="check", role="tester", bounce_target="b"),
            ],
            parallel_groups=[ParallelGroup(lanes=[["a", "check_a"], ["b", "check_b"]])],
        )
        errors = validate_workflow(wf)
        assert any("outside its lane" in e for e in errors)

    def test_lane_empty(self):
        """Empty lanes are invalid."""
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
            ],
            parallel_groups=[ParallelGroup(lanes=[["a"], []])],
        )
        errors = validate_workflow(wf)
        assert any("empty" in e for e in errors)

    def test_lane_duplicate_phase(self):
        """A phase cannot appear in multiple lanes."""
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
                Phase(id="b", type="implement", prompt="B."),
            ],
            parallel_groups=[ParallelGroup(lanes=[["a", "b"], ["b"]])],
        )
        errors = validate_workflow(wf)
        assert any("multiple lanes" in e for e in errors)

    def test_lane_allows_workflow_type(self):
        """Lane groups may contain workflow phases."""
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
                Phase(id="dyn", type="workflow", prompt="Dynamic."),
            ],
            parallel_groups=[ParallelGroup(lanes=[["a", "dyn"]])],
        )
        assert validate_workflow(wf) == []

    def test_expanded_workflow_phase_lane_group_is_valid(self):
        """Multi-var expansion must not manufacture an invalid lane group for workflow phases."""
        wf = Workflow(
            name="test",
            phases=[Phase(id="dyn", type="workflow", prompt="Plan {{ENV}}.")],
        )
        expanded = expand_multi_vars(wf, {"ENV": ["prod"]})
        assert validate_workflow(expanded) == []

    def test_valid_lane_group(self):
        """A valid lane group passes validation."""
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
                Phase(id="check_a", type="check", role="tester", bounce_target="a"),
                Phase(id="b", type="implement", prompt="B."),
                Phase(id="check_b", type="check", role="tester", bounce_target="b"),
            ],
            parallel_groups=[ParallelGroup(lanes=[["a", "check_a"], ["b", "check_b"]])],
        )
        assert validate_workflow(wf) == []


class TestValidateCLI:
    def test_validate_command_parsing(self):
        parser = build_parser()
        args = parser.parse_args(["validate", "workflow.yaml"])
        assert args.command == "validate"
        assert args.workflow == "workflow.yaml"

    def test_validate_accepts_run_flags(self):
        parser = build_parser()
        args = parser.parse_args(["validate", "workflow.yaml", "-D", "ENV=prod", "--checker", "tester"])
        assert args.defines == ["ENV=prod"]
        assert args.checker == ["tester"]

    def test_validate_valid_workflow(self, sample_yaml, capsys):
        parser = build_parser()
        args = parser.parse_args(["validate", str(sample_yaml)])
        args.plain = True
        result = cmd_validate(args)
        assert result == 0
        captured = capsys.readouterr()
        assert "Validation: OK" in captured.out

    def test_validate_shows_execution_plan(self, sample_yaml, capsys):
        parser = build_parser()
        args = parser.parse_args(["validate", str(sample_yaml)])
        args.plain = True
        cmd_validate(args)
        captured = capsys.readouterr()
        assert "Execution plan:" in captured.out
        assert "Phase summary:" in captured.out

    def test_validate_undefined_template_var(self, tmp_path, capsys):
        yaml_content = """\
name: test
phases:
  - id: deploy
    prompt: "Deploy to {{ENV}} in {{REGION}}."
"""
        yaml_path = tmp_path / "bad.yaml"
        yaml_path.write_text(yaml_content)
        parser = build_parser()
        args = parser.parse_args(["validate", str(yaml_path)])
        args.plain = True
        result = cmd_validate(args)
        assert result == 1
        captured = capsys.readouterr()
        assert "ENV" in captured.out
        assert "REGION" in captured.out

    def test_validate_with_defines_resolves_vars(self, tmp_path, capsys):
        yaml_content = """\
name: test
phases:
  - id: deploy
    prompt: "Deploy to {{ENV}}."
"""
        yaml_path = tmp_path / "ok.yaml"
        yaml_path.write_text(yaml_content)
        parser = build_parser()
        args = parser.parse_args(["validate", str(yaml_path), "-D", "ENV=prod"])
        args.plain = True
        result = cmd_validate(args)
        assert result == 0

    def test_validate_invalid_workflow(self, tmp_path, capsys):
        yaml_content = """\
name: bad
phases:
  - id: a
    type: invalid
    prompt: "whatever"
"""
        yaml_path = tmp_path / "bad.yaml"
        yaml_path.write_text(yaml_content)
        parser = build_parser()
        args = parser.parse_args(["validate", str(yaml_path)])
        args.plain = True
        result = cmd_validate(args)
        assert result == 1
        captured = capsys.readouterr()
        assert "error" in captured.out

    def test_validate_invalid_jinja_syntax_clean_error(self, tmp_path, capsys):
        """Invalid Jinja syntax prints a clean validation error, no traceback."""
        yaml_content = """\
name: bad
phases:
  - id: build
    prompt: "{{ PROJECT"
"""
        yaml_path = tmp_path / "bad-jinja.yaml"
        yaml_path.write_text(yaml_content)
        parser = build_parser()
        args = parser.parse_args(["validate", str(yaml_path)])
        args.plain = True
        result = cmd_validate(args)
        assert result == 1
        captured = capsys.readouterr()
        assert "invalid Jinja2 prompt" in captured.out
        assert "Traceback" not in captured.out

    def test_validate_jinja_render_error_clean_error(self, tmp_path, capsys):
        """Render-time Jinja errors print a clean validation error, no traceback."""
        yaml_content = """\
name: bad
phases:
  - id: build
    prompt: "{{ 1 / 0 }}"
"""
        yaml_path = tmp_path / "bad-jinja-runtime.yaml"
        yaml_path.write_text(yaml_content)
        parser = build_parser()
        args = parser.parse_args(["validate", str(yaml_path)])
        args.plain = True
        result = cmd_validate(args)
        assert result == 1
        captured = capsys.readouterr()
        assert "Jinja2 render error in prompt for phase 'build'" in captured.out
        assert "Traceback" not in captured.out

    def test_validate_check_prompt_with_role_jinja_render_error_clean_error(self, tmp_path, capsys):
        """Role-backed check prompts still surface render-time Jinja errors during validation."""
        yaml_content = """\
name: bad
phases:
  - id: review
    type: check
    role: tester
    prompt: "{{ 1 / 0 }}"
"""
        yaml_path = tmp_path / "bad-check-jinja-runtime.yaml"
        yaml_path.write_text(yaml_content)
        parser = build_parser()
        args = parser.parse_args(["validate", str(yaml_path)])
        args.plain = True
        result = cmd_validate(args)
        assert result == 1
        captured = capsys.readouterr()
        assert "Jinja2 render error in checker prompt for phase 'review'" in captured.out
        assert "Traceback" not in captured.out

    def test_validate_nested_lookup_missing_clean_error(self, tmp_path, capsys):
        """Missing nested lookups print a clean validation error, no traceback."""
        yaml_content = """\
name: bad
vars:
  config: {}
phases:
  - id: build
    prompt: "{{ config.env }}"
"""
        yaml_path = tmp_path / "bad-jinja-nested.yaml"
        yaml_path.write_text(yaml_content)
        parser = build_parser()
        args = parser.parse_args(["validate", str(yaml_path)])
        args.plain = True
        result = cmd_validate(args)
        assert result == 1
        captured = capsys.readouterr()
        assert "Jinja2 render error in prompt for phase 'build'" in captured.out
        assert "env" in captured.out
        assert "Traceback" not in captured.out

    def test_validate_missing_id_clean_error(self, tmp_path, capsys):
        """Missing phase ID prints a clean error, no stack trace."""
        yaml_content = """\
name: test
phases:
  - prompt: "no id here"
"""
        yaml_path = tmp_path / "bad.yaml"
        yaml_path.write_text(yaml_content)
        with pytest.raises(SystemExit) as exc_info:
            cmd_validate(
                argparse.Namespace(
                    workflow=str(yaml_path),
                    plain=True,
                    defines=[],
                    checker=[],
                    implementer=None,
                    backend="codex",
                    max_bounces=999,
                    working_dir=None,
                    backoff=None,
                    notify=[],
                )
            )
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "missing required 'id' field" in captured.out
        assert "Traceback" not in captured.out

    def test_validate_nonexistent_file_clean_error(self, capsys):
        """Nonexistent workflow file prints a clean error."""
        with pytest.raises(SystemExit) as exc_info:
            cmd_validate(
                argparse.Namespace(
                    workflow="/nonexistent/workflow.yaml",
                    plain=True,
                    defines=[],
                    checker=[],
                    implementer=None,
                    backend="codex",
                    max_bounces=999,
                    working_dir=None,
                    backoff=None,
                    notify=[],
                )
            )
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "Error:" in captured.out
