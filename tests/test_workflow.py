"""Unit tests for workflow loading."""

import pytest

from juvenal.workflow import Phase, Workflow, inject_checkers, load_workflow, parse_checker_string


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


class TestBareFileLoading:
    def test_load_bare_md(self, bare_md):
        wf = load_workflow(bare_md)
        assert len(wf.phases) == 2
        assert wf.phases[0].id == "task"
        assert wf.phases[0].type == "implement"
        assert wf.phases[0].prompt == "Implement a hello world program."
        assert wf.phases[1].id == "task-check"
        assert wf.phases[1].type == "check"
        assert wf.phases[1].role == "tester"


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
    checkers:
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
    checkers:
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
    checkers:
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
    checkers:
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
    checkers:
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
    checkers:
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
    checkers:
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
    checkers:
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
    checkers:
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
    checkers:
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
    checkers:
      - prompt_file: check.md
"""
        yaml_path = tmp_path / "workflow.yaml"
        yaml_path.write_text(yaml_content)
        wf = load_workflow(yaml_path)
        assert wf.phases[1].prompt == "Check everything works."


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

    def test_yaml_empty(self, tmp_path):
        """Empty YAML file should raise."""
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text("")
        with pytest.raises(ValueError, match="expected a mapping"):
            load_workflow(bad_yaml)


class TestParseCheckerString:
    def test_role(self):
        assert parse_checker_string("tester") == "tester"

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
