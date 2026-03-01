"""Unit tests for workflow loading."""

import pytest

from juvenal.workflow import load_workflow


class TestYAMLLoading:
    def test_load_basic_yaml(self, sample_yaml):
        wf = load_workflow(sample_yaml)
        assert wf.name == "test-workflow"
        assert wf.backend == "claude"
        assert wf.max_retries == 3
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
