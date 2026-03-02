"""Unit tests for workflow validation."""

from __future__ import annotations

from juvenal.cli import build_parser, cmd_validate
from juvenal.workflow import Phase, Workflow, validate_workflow


class TestValidateWorkflow:
    def test_valid_workflow(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Set up."),
                Phase(id="check", type="script", run="true"),
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

    def test_script_missing_run(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="script"),
            ],
        )
        errors = validate_workflow(wf)
        assert any("no run command" in e for e in errors)

    def test_invalid_role(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="review", type="check", role="invalid-role"),
            ],
        )
        errors = validate_workflow(wf)
        assert any("unknown role" in e for e in errors)

    def test_parallel_group_invalid_phase(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Set up."),
            ],
            parallel_groups=[["setup", "nonexistent"]],
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
            parallel_groups=[["a", "b"]],
        )
        assert validate_workflow(wf) == []

    def test_multiple_errors(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="invalid"),
                Phase(id="a", type="script"),
                Phase(id="b", type="check"),
            ],
        )
        errors = validate_workflow(wf)
        assert len(errors) >= 3  # invalid type, duplicate ID, missing run, missing prompt/role


class TestTimeoutField:
    def test_timeout_in_yaml(self, tmp_path):
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
    timeout: 120
  - id: check
    type: script
    run: "true"
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

    def test_env_in_script_phase(self, tmp_path):
        """Script phase with env passes variables to the script."""
        from juvenal.checkers import run_script

        result = run_script("echo $TEST_VAR", str(tmp_path), env={"TEST_VAR": "hello123"})
        assert result.exit_code == 0
        assert "hello123" in result.output


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
        assert any("workflow phase has no prompt" in e for e in errors)

    def test_workflow_phase_with_run_is_invalid(self):
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="dynamic", type="workflow", prompt="Do it.", run="echo hi"),
            ],
        )
        errors = validate_workflow(wf)
        assert any("must not have 'run'" in e for e in errors)

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


class TestValidateCLI:
    def test_validate_command_parsing(self):
        parser = build_parser()
        args = parser.parse_args(["validate", "workflow.yaml"])
        assert args.command == "validate"
        assert args.workflow == "workflow.yaml"

    def test_validate_valid_workflow(self, sample_yaml, capsys):
        parser = build_parser()
        args = parser.parse_args(["validate", str(sample_yaml)])
        args.plain = False
        result = cmd_validate(args)
        assert result == 0
        captured = capsys.readouterr()
        assert "valid" in captured.out

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
        args.plain = False
        result = cmd_validate(args)
        assert result == 1
        captured = capsys.readouterr()
        assert "error" in captured.out
