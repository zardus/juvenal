"""Unit tests for workflow loading."""

from pathlib import Path

import pytest

from juvenal.workflow import (
    ParallelGroup,
    Phase,
    Workflow,
    apply_vars,
    expand_multi_vars,
    inject_checkers,
    inject_implementer,
    load_workflow,
    parse_checker_string,
    scaffold_workflow,
)


class TestYAMLLoading:
    def test_load_basic_yaml(self, sample_yaml):
        wf = load_workflow(sample_yaml)
        assert wf.name == "test-workflow"
        assert wf.backend == "claude"
        assert wf.max_bounces == 3
        assert len(wf.phases) == 5

    def test_yaml_phases(self, sample_yaml):
        wf = load_workflow(sample_yaml)
        assert wf.phases[0].id == "setup"
        assert wf.phases[0].type == "implement"
        assert wf.phases[0].prompt == "Set up the project scaffolding."

    def test_yaml_phase_types(self, sample_yaml):
        wf = load_workflow(sample_yaml)
        assert wf.phases[0].type == "implement"
        assert wf.phases[1].type == "script"
        assert wf.phases[1].run == "echo ok"
        assert wf.phases[2].type == "implement"
        assert wf.phases[3].type == "script"
        assert wf.phases[4].type == "check"
        assert wf.phases[4].role == "tester"

    def test_yaml_bounce_target(self, sample_yaml):
        wf = load_workflow(sample_yaml)
        assert wf.phases[2].id == "implement"
        assert wf.phases[2].bounce_target == "setup"
        # Phases without bounce_target should be None
        assert wf.phases[0].bounce_target is None

    def test_yaml_type_defaults_to_implement(self, tmp_path):
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        wf = load_workflow(yaml_path)
        assert wf.phases[0].type == "implement"


class TestDirectoryLoading:
    def test_load_directory(self, tmp_workflow):
        wf = load_workflow(tmp_workflow)
        assert len(wf.phases) == 4
        assert wf.phases[0].id == "01-setup"
        assert wf.phases[1].id == "02-check-build"
        assert wf.phases[2].id == "03-implement"
        assert wf.phases[3].id == "04-check-review"

    def test_directory_phase_types(self, tmp_workflow):
        wf = load_workflow(tmp_workflow)
        assert wf.phases[0].type == "implement"
        assert wf.phases[1].type == "script"
        assert wf.phases[2].type == "implement"
        assert wf.phases[3].type == "check"

    def test_directory_prompts(self, tmp_workflow):
        wf = load_workflow(tmp_workflow)
        assert wf.phases[0].prompt == "Set up the project."
        assert wf.phases[2].prompt == "Implement the feature."
        assert wf.phases[3].prompt == "Review the implementation.\nVERDICT: PASS or FAIL"


class TestDirectoryInlineCheckers:
    def test_check_md_in_phase_dir(self, tmp_path):
        """check.md alongside prompt.md creates a check phase with bounce_target."""
        phases_dir = tmp_path / "phases"
        phases_dir.mkdir()

        phase = phases_dir / "01-build"
        phase.mkdir()
        (phase / "prompt.md").write_text("Build the feature.")
        (phase / "check.md").write_text("Verify the feature.\nVERDICT: PASS or FAIL")

        wf = load_workflow(tmp_path)
        assert len(wf.phases) == 2
        assert wf.phases[0].id == "01-build"
        assert wf.phases[0].type == "implement"
        assert wf.phases[0].prompt == "Build the feature."
        assert wf.phases[1].id == "01-build~check-1"
        assert wf.phases[1].type == "check"
        assert wf.phases[1].prompt == "Verify the feature.\nVERDICT: PASS or FAIL"
        assert wf.phases[1].bounce_target == "01-build"

    def test_sh_in_phase_dir(self, tmp_path):
        """.sh file alongside prompt.md creates a script phase with bounce_target."""
        phases_dir = tmp_path / "phases"
        phases_dir.mkdir()

        phase = phases_dir / "01-build"
        phase.mkdir()
        (phase / "prompt.md").write_text("Build it.")
        script = phase / "tests.sh"
        script.write_text("#!/bin/bash\npytest -x\n")
        script.chmod(0o755)

        wf = load_workflow(tmp_path)
        assert len(wf.phases) == 2
        assert wf.phases[1].id == "01-build~script-1"
        assert wf.phases[1].type == "script"
        assert wf.phases[1].bounce_target == "01-build"

    def test_multiple_checkers_in_phase_dir(self, tmp_path):
        """Multiple .md and .sh files create numbered check/script phases."""
        phases_dir = tmp_path / "phases"
        phases_dir.mkdir()

        phase = phases_dir / "01-build"
        phase.mkdir()
        (phase / "prompt.md").write_text("Build it.")
        (phase / "check-quality.md").write_text("Quality review.")
        (phase / "check-tests.md").write_text("Test review.")
        script = phase / "lint.sh"
        script.write_text("#!/bin/bash\nruff check\n")
        script.chmod(0o755)

        wf = load_workflow(tmp_path)
        assert len(wf.phases) == 4
        assert wf.phases[0].id == "01-build"
        assert wf.phases[0].type == "implement"
        # Sorted: check-quality.md, check-tests.md, lint.sh
        assert wf.phases[1].id == "01-build~check-1"
        assert wf.phases[1].prompt == "Quality review."
        assert wf.phases[2].id == "01-build~check-2"
        assert wf.phases[2].prompt == "Test review."
        assert wf.phases[3].id == "01-build~script-1"
        assert wf.phases[3].type == "script"

    def test_check_dir_ignores_extra_files(self, tmp_path):
        """check- prefixed dirs only use prompt.md, extra files are ignored."""
        phases_dir = tmp_path / "phases"
        phases_dir.mkdir()

        phase = phases_dir / "01-check-review"
        phase.mkdir()
        (phase / "prompt.md").write_text("Review it.")
        (phase / "extra.md").write_text("Should be ignored.")

        wf = load_workflow(tmp_path)
        assert len(wf.phases) == 1
        assert wf.phases[0].type == "check"

    def test_mixed_sequential_with_inline_checkers(self, tmp_path):
        """Sequential phases where some have inline checkers and some don't."""
        phases_dir = tmp_path / "phases"
        phases_dir.mkdir()

        p1 = phases_dir / "01-setup"
        p1.mkdir()
        (p1 / "prompt.md").write_text("Set up.")

        p2 = phases_dir / "02-build"
        p2.mkdir()
        (p2 / "prompt.md").write_text("Build it.")
        (p2 / "check.md").write_text("Verify build.")

        p3 = phases_dir / "03-deploy"
        p3.mkdir()
        (p3 / "prompt.md").write_text("Deploy it.")

        wf = load_workflow(tmp_path)
        assert len(wf.phases) == 4
        assert [p.id for p in wf.phases] == ["01-setup", "02-build", "02-build~check-1", "03-deploy"]


class TestBareFileLoading:
    def test_load_bare_md(self, bare_md):
        wf = load_workflow(bare_md)
        assert len(wf.phases) == 1
        assert wf.phases[0].id == "task"
        assert wf.phases[0].type == "implement"
        assert wf.phases[0].prompt == "Implement a hello world program."


class TestPhaseRendering:
    def test_render_prompt_no_failure(self, sample_yaml):
        wf = load_workflow(sample_yaml)
        rendered = wf.phases[0].render_prompt()
        assert rendered == "Set up the project scaffolding."

    def test_render_prompt_with_failure(self, sample_yaml):
        wf = load_workflow(sample_yaml)
        rendered = wf.phases[0].render_prompt(failure_context="Tests failed")
        assert "IMPORTANT: A previous attempt failed verification" in rendered
        assert "Tests failed" in rendered


class TestPromptFile:
    def test_check_phase_prompt_file(self, tmp_path):
        """Check phase with prompt_file loads prompt from the referenced file."""
        prompt_dir = tmp_path / "prompts"
        prompt_dir.mkdir()
        (prompt_dir / "my-checker.md").write_text("Check everything.\nVERDICT: PASS")

        yaml_content = """\
name: test-prompt-file
phases:
  - id: build
    prompt: "Build the thing."
  - id: review
    type: check
    prompt_file: prompts/my-checker.md
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)

        wf = load_workflow(yaml_path)
        assert len(wf.phases) == 2
        check_phase = wf.phases[1]
        assert check_phase.type == "check"
        assert check_phase.prompt == "Check everything.\nVERDICT: PASS"

    def test_phase_prompt_file(self, tmp_path):
        """Phase with prompt_file loads prompt from the referenced file."""
        prompt_dir = tmp_path / "prompts"
        prompt_dir.mkdir()
        (prompt_dir / "build.md").write_text("Build the project.")

        yaml_content = """\
name: test-prompt-file
phases:
  - id: build
    prompt_file: prompts/build.md
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)

        wf = load_workflow(yaml_path)
        assert wf.phases[0].prompt == "Build the project."


class TestBounceTargets:
    def test_bounce_targets_loaded(self, tmp_path):
        """bounce_targets list is loaded from YAML."""
        yaml_content = """\
name: test
phases:
  - id: phase-a
    prompt: "Do A."
  - id: phase-b
    prompt: "Do B."
  - id: review
    type: check
    role: tester
    bounce_targets:
      - phase-a
      - phase-b
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        wf = load_workflow(yaml_path)
        assert wf.phases[2].bounce_targets == ["phase-a", "phase-b"]
        assert wf.phases[2].bounce_target is None

    def test_bounce_target_and_bounce_targets_mutually_exclusive(self, tmp_path):
        """Setting both bounce_target and bounce_targets raises ValueError."""
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build."
  - id: review
    type: check
    role: tester
    bounce_target: build
    bounce_targets:
      - build
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        with pytest.raises(ValueError, match="mutually exclusive"):
            load_workflow(yaml_path)

    def test_empty_bounce_targets_defaults(self, tmp_path):
        """Phase without bounce_targets gets empty list."""
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build."
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        wf = load_workflow(yaml_path)
        assert wf.phases[0].bounce_targets == []


class TestWorkflowPhaseLoading:
    def test_workflow_type_with_max_depth(self, tmp_path):
        """type: workflow with max_depth loads correctly from YAML."""
        yaml_content = """\
name: test
phases:
  - id: dynamic-feature
    type: workflow
    prompt: "Build a REST API with user authentication"
    max_depth: 2
    bounce_target: setup
  - id: setup
    prompt: "Set up."
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        wf = load_workflow(yaml_path)
        assert wf.phases[0].type == "workflow"
        assert wf.phases[0].prompt == "Build a REST API with user authentication"
        assert wf.phases[0].max_depth == 2
        assert wf.phases[0].bounce_target == "setup"

    def test_workflow_type_default_max_depth(self, tmp_path):
        """type: workflow without max_depth defaults to None."""
        yaml_content = """\
name: test
phases:
  - id: dynamic
    type: workflow
    prompt: "Build something."
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        wf = load_workflow(yaml_path)
        assert wf.phases[0].type == "workflow"
        assert wf.phases[0].max_depth is None


class TestCheckersShorthand:
    def test_checkers_role(self, tmp_path):
        """role checker expands to check phase."""
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
    checks:
      - role: tester
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        wf = load_workflow(yaml_path)
        assert len(wf.phases) == 2
        assert wf.phases[1].id == "build~check-1"
        assert wf.phases[1].type == "check"
        assert wf.phases[1].role == "tester"

    def test_checkers_prompt(self, tmp_path):
        """prompt checker expands to check phase."""
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
    checks:
      - prompt: "Verify REST endpoints work."
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        wf = load_workflow(yaml_path)
        assert len(wf.phases) == 2
        assert wf.phases[1].id == "build~check-1"
        assert wf.phases[1].type == "check"
        assert wf.phases[1].prompt == "Verify REST endpoints work."

    def test_checkers_script(self, tmp_path):
        """run checker expands to script phase."""
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
    checks:
      - run: "pytest tests/ -x"
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        wf = load_workflow(yaml_path)
        assert len(wf.phases) == 2
        assert wf.phases[1].id == "build~script-1"
        assert wf.phases[1].type == "script"
        assert wf.phases[1].run == "pytest tests/ -x"

    def test_checkers_bare_string(self, tmp_path):
        """bare string role shorthand."""
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
    checks:
      - tester
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        wf = load_workflow(yaml_path)
        assert len(wf.phases) == 2
        assert wf.phases[1].id == "build~check-1"
        assert wf.phases[1].type == "check"
        assert wf.phases[1].role == "tester"

    def test_checkers_mixed(self, tmp_path):
        """all types together, verify order and IDs."""
        yaml_content = """\
name: test
phases:
  - id: build-feature
    prompt: "Build the thing."
    checks:
      - role: tester
      - prompt: "Verify REST endpoints."
      - run: "pytest tests/ -x"
      - tester
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        wf = load_workflow(yaml_path)
        assert len(wf.phases) == 5
        assert wf.phases[1].id == "build-feature~check-1"
        assert wf.phases[1].role == "tester"
        assert wf.phases[2].id == "build-feature~check-2"
        assert wf.phases[2].prompt == "Verify REST endpoints."
        assert wf.phases[3].id == "build-feature~script-1"
        assert wf.phases[3].run == "pytest tests/ -x"
        assert wf.phases[4].id == "build-feature~check-3"
        assert wf.phases[4].role == "tester"

    def test_checkers_bounce_target(self, tmp_path):
        """all synthetic phases bounce to parent."""
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
    checks:
      - role: tester
      - run: "make test"
      - prompt: "Check it."
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        wf = load_workflow(yaml_path)
        for phase in wf.phases[1:]:
            assert phase.bounce_target == "build"

    def test_checkers_with_timeout_env(self, tmp_path):
        """timeout/env passthrough."""
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
    checks:
      - role: tester
        timeout: 120
        env:
          CI: "true"
      - run: "pytest"
        timeout: 60
        env:
          FAST: "1"
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        wf = load_workflow(yaml_path)
        assert wf.phases[1].timeout == 120
        assert wf.phases[1].env == {"CI": "true"}
        assert wf.phases[2].timeout == 60
        assert wf.phases[2].env == {"FAST": "1"}

    def test_checkers_invalid_role(self, tmp_path):
        """bare string not in VALID_ROLES errors."""
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
    checks:
      - nonexistent-role
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        with pytest.raises(ValueError, match="unknown role"):
            load_workflow(yaml_path)

    def test_checkers_invalid_role_dict(self, tmp_path):
        """dict with invalid role errors."""
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
    checks:
      - role: bogus
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        with pytest.raises(ValueError, match="unknown role"):
            load_workflow(yaml_path)

    def test_checkers_invalid_entry(self, tmp_path):
        """dict with no recognized key errors."""
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
    checks:
      - foo: bar
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        with pytest.raises(ValueError, match="must have"):
            load_workflow(yaml_path)

    def test_checkers_prompt_file(self, tmp_path):
        """prompt_file checker loads from file."""
        (tmp_path / "check.md").write_text("Check everything works.")
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
    checks:
      - prompt_file: check.md
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        wf = load_workflow(yaml_path)
        assert wf.phases[1].prompt == "Check everything works."


class TestInjectImplementer:
    def test_prepends_role_prompt(self):
        """inject_implementer prepends the role prompt to implement phases."""
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build the feature.")],
        )
        result = inject_implementer(wf, "software-engineer")
        assert result.phases[0].prompt.endswith("Build the feature.")
        assert "expert software engineer" in result.phases[0].prompt

    def test_only_affects_implement_phases(self):
        """Non-implement phases are left untouched."""
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="Build it."),
                Phase(id="verify", type="check", role="tester"),
                Phase(id="lint", type="script", run="ruff check"),
            ],
        )
        result = inject_implementer(wf, "software-engineer")
        assert "expert software engineer" in result.phases[0].prompt
        assert result.phases[1].role == "tester"
        assert result.phases[1].prompt == ""
        assert result.phases[2].run == "ruff check"

    def test_multiple_implement_phases(self):
        """All implement phases get the preamble."""
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Set up."),
                Phase(id="build", type="implement", prompt="Build it."),
            ],
        )
        result = inject_implementer(wf, "software-engineer")
        assert "expert software engineer" in result.phases[0].prompt
        assert result.phases[0].prompt.endswith("Set up.")
        assert "expert software engineer" in result.phases[1].prompt
        assert result.phases[1].prompt.endswith("Build it.")

    def test_invalid_role_raises(self):
        """Invalid role name raises ValueError."""
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build it.")],
        )
        with pytest.raises(ValueError, match="Invalid --implementer role"):
            inject_implementer(wf, "nonexistent-role")

    def test_preserves_workflow_fields(self):
        """inject_implementer preserves all other workflow fields."""
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build it.", bounce_target="setup", timeout=120)],
            backend="claude",
            max_bounces=5,
            backoff=2.0,
            max_backoff=30.0,
            notify=["https://example.com/hook"],
        )
        result = inject_implementer(wf, "software-engineer")
        assert result.backend == "claude"
        assert result.max_bounces == 5
        assert result.backoff == 2.0
        assert result.max_backoff == 30.0
        assert result.notify == ["https://example.com/hook"]
        assert result.phases[0].bounce_target == "setup"
        assert result.phases[0].timeout == 120


class TestErrors:
    def test_nonexistent_path(self):
        with pytest.raises(FileNotFoundError):
            load_workflow("/nonexistent/path")

    def test_unsupported_file_type(self, tmp_path):
        bad_file = tmp_path / "workflow.txt"
        bad_file.write_text("hello")
        with pytest.raises(ValueError, match="Unsupported file type"):
            load_workflow(bad_file)

    def test_yaml_with_string_content(self, tmp_path):
        """YAML that parses to a string instead of a dict should raise."""
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("just a string\n")
        with pytest.raises(ValueError, match="expected a mapping"):
            load_workflow(bad_yaml)

    def test_yaml_with_list_content(self, tmp_path):
        """YAML that parses to a list instead of a dict should raise."""
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("- item1\n- item2\n")
        with pytest.raises(ValueError, match="expected a mapping"):
            load_workflow(bad_yaml)

    def test_yaml_phase_missing_id(self, tmp_path):
        """Phase without 'id' field should raise with a clear message."""
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("name: test\nphases:\n  - prompt: 'no id here'\n")
        with pytest.raises(ValueError, match="missing required 'id' field"):
            load_workflow(bad_yaml)

    def test_yaml_phase_missing_id_second_phase(self, tmp_path):
        """Second phase without 'id' gives the right index."""
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("name: test\nphases:\n  - id: ok\n    prompt: fine\n  - prompt: 'no id'\n")
        with pytest.raises(ValueError, match="Phase 1"):
            load_workflow(bad_yaml)

    def test_yaml_phase_not_a_dict(self, tmp_path):
        """Phase that is a bare string instead of a dict should raise."""
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("name: test\nphases:\n  - just a string\n")
        with pytest.raises((ValueError, TypeError, AttributeError)):
            load_workflow(bad_yaml)

    def test_yaml_empty(self, tmp_path):
        """Empty YAML file should raise."""
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("")
        with pytest.raises(ValueError, match="expected a mapping"):
            load_workflow(bad_yaml)


class TestParseCheckerString:
    def test_role(self):
        assert parse_checker_string("tester") == "tester"

    def test_role_security_engineer(self):
        assert parse_checker_string("security-engineer") == "security-engineer"

    def test_role_senior(self):
        assert parse_checker_string("senior-tester") == "senior-tester"

    def test_run(self):
        assert parse_checker_string("run:pytest -x") == {"run": "pytest -x"}

    def test_prompt(self):
        assert parse_checker_string("prompt:Verify the API") == {"prompt": "Verify the API"}

    def test_invalid_spec(self):
        with pytest.raises(ValueError, match="Invalid --checker spec"):
            parse_checker_string("not-a-valid-thing")


class TestInjectCheckers:
    def test_no_specs_is_noop(self):
        """Empty spec list returns the workflow unchanged."""
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build it.")],
        )
        result = inject_checkers(wf, [])
        assert len(result.phases) == 1

    def test_single_implement_role_checker(self):
        """Inject a role checker onto a single implement phase."""
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build it.")],
        )
        result = inject_checkers(wf, ["tester"])
        assert len(result.phases) == 2
        assert result.phases[1].id == "build~check-1"
        assert result.phases[1].type == "check"
        assert result.phases[1].role == "tester"
        assert result.phases[1].bounce_target == "build"

    def test_single_implement_security_engineer_checker(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build it.")],
        )
        result = inject_checkers(wf, ["security-engineer"])
        assert len(result.phases) == 2
        assert result.phases[1].id == "build~check-1"
        assert result.phases[1].type == "check"
        assert result.phases[1].role == "security-engineer"
        assert result.phases[1].bounce_target == "build"

    def test_single_implement_script_checker(self):
        """Inject a script checker onto a single implement phase."""
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build it.")],
        )
        result = inject_checkers(wf, ["run:pytest -x"])
        assert len(result.phases) == 2
        assert result.phases[1].id == "build~script-1"
        assert result.phases[1].type == "script"
        assert result.phases[1].run == "pytest -x"
        assert result.phases[1].bounce_target == "build"

    def test_single_implement_prompt_checker(self):
        """Inject a prompt checker onto a single implement phase."""
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build it.")],
        )
        result = inject_checkers(wf, ["prompt:Verify the API"])
        assert len(result.phases) == 2
        assert result.phases[1].id == "build~check-1"
        assert result.phases[1].type == "check"
        assert result.phases[1].prompt == "Verify the API"

    def test_with_existing_inline_checkers(self):
        """Injected checkers get offset numbering to avoid ID collisions."""
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="Build it."),
                Phase(id="build~check-1", type="check", role="tester", bounce_target="build"),
                Phase(id="build~script-1", type="script", run="make test", bounce_target="build"),
            ],
        )
        result = inject_checkers(wf, ["senior-tester", "run:pytest"])
        assert len(result.phases) == 5
        # Existing checkers preserved
        assert result.phases[1].id == "build~check-1"
        assert result.phases[2].id == "build~script-1"
        # Injected with offsets
        assert result.phases[3].id == "build~check-2"
        assert result.phases[3].role == "senior-tester"
        assert result.phases[4].id == "build~script-2"
        assert result.phases[4].run == "pytest"

    def test_multiple_implement_phases(self):
        """Checkers are injected after each implement phase."""
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Set up."),
                Phase(id="build", type="implement", prompt="Build it."),
            ],
        )
        result = inject_checkers(wf, ["tester"])
        assert len(result.phases) == 4
        assert result.phases[0].id == "setup"
        assert result.phases[1].id == "setup~check-1"
        assert result.phases[1].bounce_target == "setup"
        assert result.phases[2].id == "build"
        assert result.phases[3].id == "build~check-1"
        assert result.phases[3].bounce_target == "build"

    def test_non_implement_phases_untouched(self):
        """Check and script phases don't get checkers injected."""
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="Build it."),
                Phase(id="verify", type="check", role="tester"),
                Phase(id="lint", type="script", run="ruff check"),
            ],
        )
        result = inject_checkers(wf, ["senior-tester"])
        assert len(result.phases) == 4
        assert result.phases[0].id == "build"
        assert result.phases[1].id == "build~check-1"
        assert result.phases[2].id == "verify"
        assert result.phases[3].id == "lint"

    def test_multiple_checkers_per_implement(self):
        """Multiple --checker specs all get injected."""
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build it.")],
        )
        result = inject_checkers(wf, ["tester", "run:pytest -x", "prompt:Check it"])
        assert len(result.phases) == 4
        assert result.phases[1].id == "build~check-1"
        assert result.phases[1].role == "tester"
        assert result.phases[2].id == "build~script-1"
        assert result.phases[2].run == "pytest -x"
        assert result.phases[3].id == "build~check-2"
        assert result.phases[3].prompt == "Check it"


class TestDirectoryParallelLanes:
    def test_simple_parallel_lanes(self, tmp_path):
        """Parallel dir with simple lanes: prompt.md + check.md per lane."""
        phases_dir = tmp_path / "phases"
        phases_dir.mkdir()

        par_dir = phases_dir / "02-parallel"
        par_dir.mkdir()

        # Lane a: implement + check
        lane_a = par_dir / "a"
        lane_a.mkdir()
        (lane_a / "prompt.md").write_text("Build feature A.")
        (lane_a / "check.md").write_text("Review A.\nVERDICT: PASS or FAIL")

        # Lane b: implement + check
        lane_b = par_dir / "b"
        lane_b.mkdir()
        (lane_b / "prompt.md").write_text("Build feature B.")
        (lane_b / "check.md").write_text("Review B.\nVERDICT: PASS or FAIL")

        wf = load_workflow(tmp_path)
        assert len(wf.parallel_groups) == 1
        pg = wf.parallel_groups[0]
        assert pg.is_lane_group()
        assert len(pg.lanes) == 2
        assert pg.lanes[0] == ["a", "a~check-1"]
        assert pg.lanes[1] == ["b", "b~check-1"]

        # Check phases exist with correct types
        phase_map = {p.id: p for p in wf.phases}
        assert phase_map["a"].type == "implement"
        assert phase_map["a~check-1"].type == "check"
        assert phase_map["b"].type == "implement"
        assert phase_map["b~check-1"].type == "check"

    def test_auto_bounce_target(self, tmp_path):
        """Check/script phases in a lane auto-get bounce_target to the lane's implement phase."""
        phases_dir = tmp_path / "phases"
        phases_dir.mkdir()

        par_dir = phases_dir / "01-parallel"
        par_dir.mkdir()

        lane = par_dir / "feat"
        lane.mkdir()
        (lane / "prompt.md").write_text("Build it.")
        (lane / "check.md").write_text("Verify it.")

        wf = load_workflow(tmp_path)
        phase_map = {p.id: p for p in wf.phases}
        assert phase_map["feat~check-1"].bounce_target == "feat"

    def test_lane_with_script_checker(self, tmp_path):
        """Lane with a .sh script checker."""
        phases_dir = tmp_path / "phases"
        phases_dir.mkdir()

        par_dir = phases_dir / "01-parallel"
        par_dir.mkdir()

        lane = par_dir / "x"
        lane.mkdir()
        (lane / "prompt.md").write_text("Build X.")
        script = lane / "tests.sh"
        script.write_text("#!/bin/bash\nexit 0\n")
        script.chmod(0o755)

        wf = load_workflow(tmp_path)
        pg = wf.parallel_groups[0]
        assert pg.lanes == [["x", "x~script-1"]]

        phase_map = {p.id: p for p in wf.phases}
        assert phase_map["x~script-1"].type == "script"
        assert phase_map["x~script-1"].bounce_target == "x"

    def test_complex_lane_subdirectories(self, tmp_path):
        """Lane with subdirectories instead of root prompt.md."""
        phases_dir = tmp_path / "phases"
        phases_dir.mkdir()

        par_dir = phases_dir / "01-parallel"
        par_dir.mkdir()

        lane = par_dir / "a"
        lane.mkdir()

        impl = lane / "01-implement"
        impl.mkdir()
        (impl / "prompt.md").write_text("Build A.")

        check = lane / "02-check-review"
        check.mkdir()
        (check / "prompt.md").write_text("Review A.")

        wf = load_workflow(tmp_path)
        pg = wf.parallel_groups[0]
        assert pg.lanes == [["a~01-implement", "a~02-check-review"]]

        phase_map = {p.id: p for p in wf.phases}
        assert phase_map["a~01-implement"].type == "implement"
        assert phase_map["a~02-check-review"].type == "check"
        # Auto bounce target to first implement
        assert phase_map["a~02-check-review"].bounce_target == "a~01-implement"

    def test_parallel_mixed_with_sequential(self, tmp_path):
        """Parallel dir coexists with regular sequential phases."""
        phases_dir = tmp_path / "phases"
        phases_dir.mkdir()

        # Sequential phase before
        setup = phases_dir / "01-setup"
        setup.mkdir()
        (setup / "prompt.md").write_text("Set up.")

        # Parallel group
        par_dir = phases_dir / "02-parallel"
        par_dir.mkdir()
        lane_a = par_dir / "a"
        lane_a.mkdir()
        (lane_a / "prompt.md").write_text("Feature A.")
        lane_b = par_dir / "b"
        lane_b.mkdir()
        (lane_b / "prompt.md").write_text("Feature B.")

        # Sequential phase after
        finish = phases_dir / "03-finish"
        finish.mkdir()
        (finish / "prompt.md").write_text("Finish up.")

        wf = load_workflow(tmp_path)
        phase_ids = [p.id for p in wf.phases]
        assert phase_ids == ["01-setup", "a", "b", "03-finish"]
        assert len(wf.parallel_groups) == 1
        assert wf.parallel_groups[0].lanes == [["a"], ["b"]]

    def test_empty_parallel_dir(self, tmp_path):
        """Parallel dir with no lane subdirectories produces empty group."""
        phases_dir = tmp_path / "phases"
        phases_dir.mkdir()
        par_dir = phases_dir / "01-parallel"
        par_dir.mkdir()

        wf = load_workflow(tmp_path)
        assert len(wf.parallel_groups) == 1
        assert wf.parallel_groups[0].lanes == []

    def test_multiple_checks_in_lane(self, tmp_path):
        """Lane with multiple check files gets numbered check IDs."""
        phases_dir = tmp_path / "phases"
        phases_dir.mkdir()

        par_dir = phases_dir / "01-parallel"
        par_dir.mkdir()

        lane = par_dir / "a"
        lane.mkdir()
        (lane / "prompt.md").write_text("Build A.")
        (lane / "check-quality.md").write_text("Quality check.")
        (lane / "check-tests.md").write_text("Test check.")

        wf = load_workflow(tmp_path)
        pg = wf.parallel_groups[0]
        # Two check files sorted alphabetically
        assert pg.lanes == [["a", "a~check-1", "a~check-2"]]


class TestTemplateVars:
    def test_apply_vars_basic(self):
        assert apply_vars("Hello {{NAME}}", {"NAME": "world"}) == "Hello world"

    def test_apply_vars_multiple(self):
        result = apply_vars("{{A}} and {{B}}", {"A": "foo", "B": "bar"})
        assert result == "foo and bar"

    def test_apply_vars_unrecognized_passthrough(self):
        assert apply_vars("Hello {{UNKNOWN}}", {"NAME": "world"}) == "Hello {{UNKNOWN}}"

    def test_apply_vars_empty_dict(self):
        assert apply_vars("Hello {{NAME}}", {}) == "Hello {{NAME}}"

    def test_apply_vars_no_placeholders(self):
        assert apply_vars("no vars here", {"NAME": "world"}) == "no vars here"

    def test_apply_vars_jinja_rendering_and_sandbox(self):
        assert apply_vars("{{NAME|upper}}", {"NAME": "svc"}) == "SVC" and apply_vars("{{ 'ok'|upper }}", {}) == "OK" and apply_vars("{% for key, value in D.items() %}{{ key }}={{ value }}{% endfor %}", {"D": {"a": "b"}}) == "a=b"  # noqa: E501  # fmt: skip
        assert any(s in str(pytest.raises(ValueError, apply_vars, "{{P.read_text()}}", {"P": Path("README.md")}).value) for s in ("unsafe", "callable"))  # noqa: E501  # fmt: skip
        assert any(s in str(pytest.raises(ValueError, apply_vars, "{{ cycler.__init__.__globals__.os.popen('echo pwned').read() }}", {}).value) for s in ("undefined", "callable"))  # noqa: E501  # fmt: skip
        assert (vars := {"L": [1]}) and any(s in str(pytest.raises(ValueError, apply_vars, "{{ L.append(2) }}", vars).value) for s in ("unsafe", "callable")) and "callable" in str(pytest.raises(ValueError, apply_vars, "{{ danger() }}", {"danger": lambda: "executed"}).value) and vars == {"L": [1]}  # noqa: E501  # fmt: skip

    def test_render_prompt_with_vars(self):
        phase = Phase(id="build", prompt="Build {{PROJECT}} in {{LANG}}.")
        result = phase.render_prompt(vars={"PROJECT": "myapp", "LANG": "Python"})
        assert result == "Build myapp in Python."

    def test_render_prompt_vars_with_failure_context(self):
        phase = Phase(id="build", prompt="Build {{PROJECT}}.")
        result = phase.render_prompt(failure_context="tests failed", vars={"PROJECT": "myapp"})
        assert "Build myapp." in result
        assert "tests failed" in result

    def test_render_check_prompt_with_vars(self):
        phase = Phase(id="check", type="check", prompt="Verify {{COMPONENT}}.")
        result = phase.render_check_prompt(vars={"COMPONENT": "auth"})
        assert result == "Verify auth."

    def test_render_prompt_none_vars(self):
        phase = Phase(id="build", prompt="Build {{PROJECT}}.")
        result = phase.render_prompt(vars=None)
        assert result == "Build {{PROJECT}}."

    def test_yaml_loads_vars(self, tmp_path):
        yaml_content = """\
name: test
vars:
  ENV: staging
  REGION: us-east-1
phases:
  - id: deploy
    prompt: "Deploy to {{ENV}} in {{REGION}}."
"""
        (tmp_path / "workflow.yaml").write_text(yaml_content)
        wf = load_workflow(tmp_path / "workflow.yaml")
        assert wf.vars == {"ENV": "staging", "REGION": "us-east-1"}

    def test_yaml_vars_default_empty(self, tmp_path):
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
"""
        (tmp_path / "workflow.yaml").write_text(yaml_content)
        wf = load_workflow(tmp_path / "workflow.yaml")
        assert wf.vars == {}

    def test_inject_checkers_preserves_vars(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build it.")],
            vars={"ENV": "prod"},
        )
        result = inject_checkers(wf, ["tester"])
        assert result.vars == {"ENV": "prod"}

    def test_inject_implementer_preserves_vars(self):
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build it.")],
            vars={"ENV": "prod"},
        )
        result = inject_implementer(wf, "software-engineer")
        assert result.vars == {"ENV": "prod"}

    def test_yaml_includes_merge_vars(self, tmp_path):
        """Included workflow vars are defaults; including workflow overrides."""
        (tmp_path / "base.yaml").write_text("""\
name: base
vars:
  A: from-base
  B: from-base
phases:
  - id: base-phase
    prompt: "Base."
""")
        (tmp_path / "main.yaml").write_text("""\
name: main
include:
  - base.yaml
vars:
  B: overridden
  C: from-main
phases:
  - id: main-phase
    prompt: "Main."
""")
        wf = load_workflow(tmp_path / "main.yaml")
        assert wf.vars == {"A": "from-base", "B": "overridden", "C": "from-main"}

    def test_directory_convention_loads_vars(self, tmp_path):
        """Directory convention picks up vars from workflow.yaml overrides."""
        phases_dir = tmp_path / "phases"
        phases_dir.mkdir()
        p1 = phases_dir / "01-build"
        p1.mkdir()
        (p1 / "prompt.md").write_text("Build {{PROJECT}}.")
        (tmp_path / "workflow.yaml").write_text("""\
vars:
  PROJECT: myapp
""")
        wf = load_workflow(tmp_path)
        assert wf.vars == {"PROJECT": "myapp"}


class TestWorkflowFileDir:
    def test_workflow_file_parsed(self, tmp_path):
        sub = tmp_path / "sub.yaml"
        sub.write_text("name: sub\nphases:\n  - id: x\n    prompt: X.\n")
        yaml_content = """\
name: test
phases:
  - id: auth
    type: workflow
    workflow_file: sub.yaml
"""
        (tmp_path / "workflow.yaml").write_text(yaml_content)
        wf = load_workflow(tmp_path / "workflow.yaml")
        assert wf.phases[0].workflow_file == str(sub.resolve())
        assert wf.phases[0].workflow_dir is None

    def test_workflow_dir_parsed(self, tmp_path):
        sub_dir = tmp_path / "sub"
        sub_dir.mkdir()
        (sub_dir / "phases").mkdir()
        p = sub_dir / "phases" / "01-build"
        p.mkdir()
        (p / "prompt.md").write_text("Build.")
        yaml_content = """\
name: test
phases:
  - id: frontend
    type: workflow
    workflow_dir: sub
"""
        (tmp_path / "workflow.yaml").write_text(yaml_content)
        wf = load_workflow(tmp_path / "workflow.yaml")
        assert wf.phases[0].workflow_dir == str(sub_dir.resolve())
        assert wf.phases[0].workflow_file is None

    def test_workflow_file_and_dir_mutually_exclusive(self, tmp_path):
        yaml_content = """\
name: test
phases:
  - id: both
    type: workflow
    workflow_file: a.yaml
    workflow_dir: b/
"""
        (tmp_path / "workflow.yaml").write_text(yaml_content)
        with pytest.raises(ValueError, match="mutually exclusive"):
            load_workflow(tmp_path / "workflow.yaml")

    def test_workflow_file_resolved_relative_to_yaml(self, tmp_path):
        nested = tmp_path / "nested"
        nested.mkdir()
        sub = nested / "sub.yaml"
        sub.write_text("name: sub\nphases:\n  - id: x\n    prompt: X.\n")
        yaml_content = """\
name: test
phases:
  - id: auth
    type: workflow
    workflow_file: sub.yaml
"""
        (nested / "workflow.yaml").write_text(yaml_content)
        wf = load_workflow(nested / "workflow.yaml")
        assert wf.phases[0].workflow_file == str(sub.resolve())


class TestExpandMultiVars:
    def test_single_var_two_values(self):
        """Phase with {{TARGET}} and TARGET=[linux, windows] creates two parallel lanes."""
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build for {{TARGET}}.")],
        )
        result = expand_multi_vars(wf, {"TARGET": ["linux", "windows"]})
        assert len(result.phases) == 2
        assert result.phases[0].id == "build~TARGET=linux"
        assert result.phases[0].prompt == "Build for linux."
        assert result.phases[1].id == "build~TARGET=windows"
        assert result.phases[1].prompt == "Build for windows."
        assert len(result.parallel_groups) == 1
        assert result.parallel_groups[0].lanes == [["build~TARGET=linux"], ["build~TARGET=windows"]]

    def test_phase_with_children_duplicates_group(self):
        """Phase + checker group is duplicated together as a lane group."""
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="deploy", type="implement", prompt="Deploy to {{ENV}}."),
                Phase(id="deploy~check-1", type="check", prompt="Verify {{ENV}} deploy.", bounce_target="deploy"),
            ],
        )
        result = expand_multi_vars(wf, {"ENV": ["staging", "prod"]})
        assert len(result.phases) == 4
        assert result.phases[0].id == "deploy~ENV=staging"
        assert result.phases[1].id == "deploy~check-1~ENV=staging"
        assert result.phases[1].bounce_target == "deploy~ENV=staging"
        assert result.phases[2].id == "deploy~ENV=prod"
        assert result.phases[3].id == "deploy~check-1~ENV=prod"
        assert result.phases[3].bounce_target == "deploy~ENV=prod"
        pg = result.parallel_groups[0]
        assert pg.lanes == [
            ["deploy~ENV=staging", "deploy~check-1~ENV=staging"],
            ["deploy~ENV=prod", "deploy~check-1~ENV=prod"],
        ]

    def test_unaffected_phases_preserved(self):
        """Phases that don't reference multi-value vars are unchanged."""
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Set up."),
                Phase(id="build", type="implement", prompt="Build for {{TARGET}}."),
                Phase(id="finish", type="implement", prompt="Done."),
            ],
        )
        result = expand_multi_vars(wf, {"TARGET": ["a", "b"]})
        ids = [p.id for p in result.phases]
        assert ids == ["setup", "build~TARGET=a", "build~TARGET=b", "finish"]

    def test_script_run_templated(self):
        """Script run commands are also expanded."""
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="test", type="implement", prompt="Build."),
                Phase(id="test~script-1", type="script", run="pytest {{DIR}} -x", bounce_target="test"),
            ],
        )
        result = expand_multi_vars(wf, {"DIR": ["tests/a", "tests/b"]})
        scripts = [p for p in result.phases if p.type == "script"]
        assert scripts[0].run == "pytest tests/a -x"
        assert scripts[1].run == "pytest tests/b -x"

    def test_cartesian_product(self):
        """Multiple multi-value vars produce cartesian product."""
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build {{A}} on {{B}}.")],
        )
        result = expand_multi_vars(wf, {"A": ["x", "y"], "B": ["1", "2"]})
        assert len(result.phases) == 4
        prompts = {p.prompt for p in result.phases}
        assert prompts == {"Build x on 1.", "Build x on 2.", "Build y on 1.", "Build y on 2."} and (wf := expand_multi_vars(Workflow(name="test", phases=[Phase(id="build", prompt="{% if ENABLED %}Build {{TARGET}}{% endif %}\n")]), {"TARGET": ["linux", "win"], "ENABLED": [True, False]})) and [p.prompt for p in wf.phases] == ["Build linux\n", "Build win\n"] and not __import__("juvenal.workflow", fromlist=["validate_workflow"]).validate_workflow(wf)  # noqa: E501  # fmt: skip

    def test_empty_multi_vars_is_noop(self):
        """Empty multi_vars returns workflow unchanged."""
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build it.")],
        )
        result = expand_multi_vars(wf, {})
        assert len(result.phases) == 1
        assert result.phases[0].id == "build"

    def test_phases_in_existing_parallel_group_skipped(self):
        """Phases already in a parallel group are not expanded."""
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="Build {{TARGET}}."),
                Phase(id="b", type="implement", prompt="Build {{TARGET}}."),
            ],
            parallel_groups=[ParallelGroup(phases=["a", "b"])],
        )
        result = expand_multi_vars(wf, {"TARGET": ["x", "y"]})
        # Original phases unchanged, no new parallel groups
        assert [p.id for p in result.phases] == ["a", "b"]
        assert len(result.parallel_groups) == 1

    def test_preserves_workflow_fields(self):
        """expand_multi_vars preserves all workflow fields."""
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build {{X}}.")],
            backend="claude",
            max_bounces=5,
            backoff=2.0,
            vars={"EXISTING": "val"},
        )
        result = expand_multi_vars(wf, {"X": ["a", "b"]})
        assert result.backend == "claude"
        assert result.max_bounces == 5
        assert result.backoff == 2.0
        assert result.vars == {"EXISTING": "val"}


class TestScaffoldWorkflow:
    def test_scaffold_creates_files(self, tmp_path):
        target = str(tmp_path / "my-workflow")
        scaffold_workflow(target)

        t = Path(target)
        assert t.exists()
        assert (t / "workflow.yaml").exists()
        assert (t / "phases" / "01-implement" / "prompt.md").exists()

    def test_scaffold_nonexistent_template_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Template not found"):
            scaffold_workflow(str(tmp_path / "out"), template="nonexistent")

    def test_scaffold_creates_parent_dirs(self, tmp_path):
        target = str(tmp_path / "deep" / "nested" / "workflow")
        scaffold_workflow(target)

        assert Path(target).exists()


class TestLoadRolePrompt:
    def test_valid_role(self):
        from juvenal.workflow import _load_role_prompt

        prompt = _load_role_prompt("tester")
        assert len(prompt) > 0
        assert "VERDICT" in prompt

    def test_security_engineer_role(self):
        from juvenal.workflow import _load_role_prompt

        prompt = _load_role_prompt("security-engineer")
        assert "Security Engineer REVIEWING" in prompt
        assert "Input handling and trust boundaries" in prompt
        assert "SSRF" in prompt
        assert "Software Tester REVIEWING" not in prompt

    def test_invalid_role_raises(self):
        from juvenal.workflow import _load_role_prompt

        with pytest.raises(FileNotFoundError, match="Built-in role prompt not found"):
            _load_role_prompt("nonexistent-role")


class TestYAMLChecksKey:
    def test_checks_key_loads_checkers(self, tmp_path):
        """The 'checks:' YAML key creates check phases."""
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
    checks:
      - role: tester
"""
        (tmp_path / "workflow.yaml").write_text(yaml_content)
        wf = load_workflow(tmp_path / "workflow.yaml")
        assert len(wf.phases) == 2
        assert wf.phases[1].id == "build~check-1"
        assert wf.phases[1].type == "check"
        assert wf.phases[1].role == "tester"

    def test_checks_with_inline_prompt(self, tmp_path):
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
    checks:
      - prompt: "Verify the build."
"""
        (tmp_path / "workflow.yaml").write_text(yaml_content)
        wf = load_workflow(tmp_path / "workflow.yaml")
        assert wf.phases[1].prompt == "Verify the build."

    def test_checks_with_run(self, tmp_path):
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
    checks:
      - run: "pytest -x"
"""
        (tmp_path / "workflow.yaml").write_text(yaml_content)
        wf = load_workflow(tmp_path / "workflow.yaml")
        assert wf.phases[1].type == "script"
        assert wf.phases[1].run == "pytest -x"


class TestInteractiveField:
    def test_interactive_parsed_from_yaml(self, tmp_path):
        yaml_content = """\
name: test
phases:
  - id: refine
    prompt: "Refine."
    interactive: true
  - id: build
    prompt: "Build."
"""
        (tmp_path / "workflow.yaml").write_text(yaml_content)
        wf = load_workflow(tmp_path / "workflow.yaml")
        assert wf.phases[0].interactive is True
        assert wf.phases[1].interactive is False

    def test_interactive_defaults_to_false(self, tmp_path):
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build."
"""
        (tmp_path / "workflow.yaml").write_text(yaml_content)
        wf = load_workflow(tmp_path / "workflow.yaml")
        assert wf.phases[0].interactive is False

    def test_interactive_on_non_implement_is_error(self, tmp_path):
        from juvenal.workflow import validate_workflow

        wf = Workflow(
            name="test",
            phases=[
                Phase(id="check-it", type="check", prompt="Check.\nVERDICT: PASS", interactive=True),
            ],
        )
        errors = validate_workflow(wf)
        assert any("interactive" in e for e in errors)

    def test_interactive_not_flagged_as_unknown_key(self, tmp_path):
        yaml_content = """\
name: test
phases:
  - id: refine
    prompt: "Refine."
    interactive: true
"""
        (tmp_path / "workflow.yaml").write_text(yaml_content)
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            load_workflow(tmp_path / "workflow.yaml")
            assert len(w) == 0


class TestUnknownKeyWarning:
    def test_unknown_key_warns(self, tmp_path):
        yaml_content = """\
name: test
phases:
  - id: build
    prompt: "Build it."
    typo_key: "oops"
"""
        (tmp_path / "workflow.yaml").write_text(yaml_content)
        import warnings

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            load_workflow(tmp_path / "workflow.yaml")
            assert len(w) == 1
            assert "typo_key" in str(w[0].message)


class TestPlannerWorkflowAssets:
    def test_plan_workflow_runs_both_post_write_validators_before_review(self):
        repo_root = Path(__file__).resolve().parents[1]
        workflow = load_workflow(repo_root / "juvenal" / "workflows" / "plan.yaml")
        phase_ids = [phase.id for phase in workflow.phases]
        write_index = phase_ids.index("write-workflow")
        yaml_validate_index = phase_ids.index("yaml-validate")
        planned_validate_index = phase_ids.index("planned-workflow-validate")
        review_index = phase_ids.index("workflow-review")

        assert yaml_validate_index == write_index + 1
        assert planned_validate_index == yaml_validate_index + 1
        assert review_index == planned_validate_index + 1

        assert phase_ids[write_index + 1 : write_index + 4] == [
            "yaml-validate",
            "planned-workflow-validate",
            "workflow-review",
        ]

        phases = {phase.id: phase for phase in workflow.phases}
        assert phases["yaml-validate"].type == "script"
        assert phases["yaml-validate"].bounce_target == "write-workflow"
        assert phases["planned-workflow-validate"].type == "script"
        assert phases["planned-workflow-validate"].bounce_target == "write-workflow"
        assert (
            phases["planned-workflow-validate"].run
            == "python -m juvenal.plan_validation .plan/workflow-structure.yaml workflow.yaml"
        )
        assert phases["workflow-review"].bounce_target == "write-workflow"

    def test_cleanup_prompts_use_snapshot_instead_of_git_history(self):
        repo_root = Path(__file__).resolve().parents[1]
        cleanup_prompt = (repo_root / "juvenal" / "workflows" / "plan-phases" / "05-plan-cleanup.md").read_text()
        review_prompt = (repo_root / "juvenal" / "workflows" / "plan-phases" / "06-plan-cleanup-review.md").read_text()

        assert ".plan/plan-before-cleanup.md" in cleanup_prompt
        assert ".plan/plan-before-cleanup.md" in review_prompt
        assert ".plan/plan.md" in review_prompt
        assert "Compare the concrete snapshot file against the rewritten plan directly." in review_prompt
        assert "Do not use `git diff`, `git log`, or git history." in review_prompt

    def test_workflow_writer_and_review_prompts_require_structure_contract(self):
        repo_root = Path(__file__).resolve().parents[1]
        writer_prompt = (repo_root / "juvenal" / "workflows" / "plan-phases" / "09-write-workflow.md").read_text()
        review_prompt = (repo_root / "juvenal" / "workflows" / "plan-phases" / "10-workflow-review.md").read_text()

        assert ".plan/workflow-structure.yaml" in writer_prompt
        assert ".plan/workflow-structure.yaml" in review_prompt
        assert "fixed `bounce_target` values" in writer_prompt
        assert "no agent-guided `bounce_targets` lists" in writer_prompt
        assert "no phase-level `prompt_file`, `workflow_file`, `workflow_dir`, or `checks`" in writer_prompt
        assert (
            "no phase-level `prompt_file`, `workflow_file`, `workflow_dir`, `checks`, or `bounce_targets`"
            in review_prompt
        )
