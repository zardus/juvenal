"""Unit tests for CLI argument parsing and commands."""

import subprocess
import sys
import time

import pytest

from juvenal.cli import _parse_defines, _parse_phased_implementer, build_parser, cmd_plan, cmd_run, cmd_status
from juvenal.state import PipelineState


class TestArgumentParsing:
    def test_run_basic(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml"])
        assert args.command == "run"
        assert args.workflow == "workflow.yaml"
        assert args.backend == "codex"
        assert args.max_bounces == 999
        assert not args.resume

    def test_run_all_flags(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "run",
                "workflow.yaml",
                "--resume",
                "--phase",
                "implement",
                "--max-bounces",
                "5",
                "--backend",
                "codex",
                "--working-dir",
                "/tmp",
                "--state-file",
                "custom-state.json",
            ]
        )
        assert args.resume
        assert args.phase == "implement"
        assert args.max_bounces == 5
        assert args.backend == "codex"
        assert args.working_dir == "/tmp"
        assert args.state_file == "custom-state.json"

    def test_run_rewind(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "--rewind", "2"])
        assert args.rewind == 2
        assert args.rewind_to is None

    def test_run_rewind_to(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "--rewind-to", "phase-a"])
        assert args.rewind_to == "phase-a"
        assert args.rewind is None

    def test_run_defaults_no_rewind(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml"])
        assert args.rewind is None
        assert args.rewind_to is None

    def test_run_state_file_default(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml"])
        assert args.state_file is None

    def test_plan(self):
        parser = build_parser()
        args = parser.parse_args(["plan", "build a web app"])
        assert args.command == "plan"
        assert args.goal == "build a web app"
        assert args.output == "workflow.yaml"

    def test_plan_output(self):
        parser = build_parser()
        args = parser.parse_args(["plan", "build a web app", "-o", "my.yaml"])
        assert args.output == "my.yaml"

    def test_do(self):
        parser = build_parser()
        args = parser.parse_args(["do", "build a web app"])
        assert args.command == "do"
        assert args.goal == "build a web app"

    def test_status(self):
        parser = build_parser()
        args = parser.parse_args(["status"])
        assert args.command == "status"

    def test_status_with_state_file(self):
        parser = build_parser()
        args = parser.parse_args(["status", "--state-file", "custom.json"])
        assert args.state_file == "custom.json"

    def test_init_default(self):
        parser = build_parser()
        args = parser.parse_args(["init"])
        assert args.command == "init"
        assert args.directory == "."
        assert args.template == "default"

    def test_init_custom(self):
        parser = build_parser()
        args = parser.parse_args(["init", "myproject", "--template", "basic"])
        assert args.directory == "myproject"
        assert args.template == "basic"

    def test_validate(self):
        parser = build_parser()
        args = parser.parse_args(["validate", "workflow.yaml"])
        assert args.command == "validate"
        assert args.workflow == "workflow.yaml"

    def test_run_checker_single(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "--checker", "tester"])
        assert args.checker == ["tester"]

    def test_run_checker_multiple(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "--checker", "tester", "--checker", "run:pytest -x"])
        assert args.checker == ["tester", "run:pytest -x"]

    def test_run_checker_default_empty(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml"])
        assert args.checker == []

    def test_plan_checker(self):
        parser = build_parser()
        args = parser.parse_args(["plan", "build an API", "--checker", "senior-tester"])
        assert args.checker == ["senior-tester"]

    def test_plan_checker_default_empty(self):
        parser = build_parser()
        args = parser.parse_args(["plan", "build an API"])
        assert args.checker == []

    def test_do_checker(self):
        parser = build_parser()
        args = parser.parse_args(["do", "build a thing", "--checker", "tester", "--checker", "run:make lint"])
        assert args.checker == ["tester", "run:make lint"]

    def test_do_checker_default_empty(self):
        parser = build_parser()
        args = parser.parse_args(["do", "build a thing"])
        assert args.checker == []

    def test_run_standard_checkers(self):
        from juvenal.cli import STANDARD_CHECKERS, _expand_standard_checkers

        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "--standard-checkers"])
        assert args.standard_checkers is True
        _expand_standard_checkers(args)
        assert args.checker == list(STANDARD_CHECKERS)

    def test_run_standard_checkers_with_extra(self):
        from juvenal.cli import STANDARD_CHECKERS, _expand_standard_checkers

        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "--standard-checkers", "--checker", "run:pytest -x"])
        _expand_standard_checkers(args)
        assert args.checker == list(STANDARD_CHECKERS) + ["run:pytest -x"]

    def test_standard_checkers_not_set(self):
        from juvenal.cli import _expand_standard_checkers

        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "--checker", "tester"])
        _expand_standard_checkers(args)
        assert args.checker == ["tester"]

    def test_plan_standard_checkers(self):
        from juvenal.cli import STANDARD_CHECKERS, _expand_standard_checkers

        parser = build_parser()
        args = parser.parse_args(["plan", "build an API", "--standard-checkers"])
        _expand_standard_checkers(args)
        assert args.checker == list(STANDARD_CHECKERS)

    def test_do_standard_checkers(self):
        from juvenal.cli import STANDARD_CHECKERS, _expand_standard_checkers

        parser = build_parser()
        args = parser.parse_args(["do", "build a thing", "--standard-checkers"])
        _expand_standard_checkers(args)
        assert args.checker == list(STANDARD_CHECKERS)

    def test_run_implementer(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "--implementer", "software-engineer"])
        assert args.implementer == ["software-engineer"]

    def test_run_implementer_multiple(self):
        parser = build_parser()
        args = parser.parse_args(
            [
                "run",
                "--implementer",
                'software-engineer:"do X"',
                "--implementer",
                'software-engineer:"do Y"',
            ]
        )
        assert args.implementer == ['software-engineer:"do X"', 'software-engineer:"do Y"']
        assert args.workflow is None

    def test_run_implementer_default_empty(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml"])
        assert args.implementer == []

    def test_run_phased_implementer(self):
        parser = build_parser()
        args = parser.parse_args(["run", "--phased-implementer", 'software-engineer:"build a thing"'])
        assert args.phased_implementer == 'software-engineer:"build a thing"'

    def test_parse_implementer_strips_wrapping_quotes(self):
        from juvenal.cli import _parse_implementer

        assert _parse_implementer('software-engineer:"do X"') == ("software-engineer", "do X")
        assert _parse_implementer("software-engineer:'do Y'") == ("software-engineer", "do Y")

    def test_parse_phased_implementer_strips_wrapping_quotes(self):
        assert _parse_phased_implementer('software-engineer:"build a thing"') == ("software-engineer", "build a thing")

    def test_parse_phased_implementer_keeps_plain_goal_colons(self):
        assert _parse_phased_implementer("build API: auth flow") == (None, "build API: auth flow")

    def test_plan_implementer(self):
        parser = build_parser()
        args = parser.parse_args(["plan", "build an API", "--implementer", "software-engineer"])
        assert args.implementer == "software-engineer"

    def test_do_implementer(self):
        parser = build_parser()
        args = parser.parse_args(["do", "build a thing", "--implementer", "software-engineer"])
        assert args.implementer == "software-engineer"

    def test_run_clear_context_on_bounce(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "--clear-context-on-bounce"])
        assert args.clear_context_on_bounce is True

    def test_run_clear_context_on_bounce_default(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml"])
        assert args.clear_context_on_bounce is False

    def test_do_clear_context_on_bounce(self):
        parser = build_parser()
        args = parser.parse_args(["do", "build a thing", "--clear-context-on-bounce"])
        assert args.clear_context_on_bounce is True

    def test_run_interactive_flag(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "--interactive"])
        assert args.interactive is True

    def test_run_interactive_short_flag(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "-i"])
        assert args.interactive is True

    def test_run_defines_single(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "-D", "ENV=prod"])
        assert args.defines == ["ENV=prod"]

    def test_run_defines_multiple(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "-D", "ENV=prod", "-D", "REGION=us-east-1"])
        assert args.defines == ["ENV=prod", "REGION=us-east-1"]

    def test_run_defines_default_empty(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml"])
        assert args.defines == []

    def test_run_defines_with_equals_in_value(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "-D", "CMD=a=b=c"])
        assert args.defines == ["CMD=a=b=c"]

    def test_do_defines(self):
        parser = build_parser()
        args = parser.parse_args(["do", "build a thing", "-D", "ENV=prod"])
        assert args.defines == ["ENV=prod"]

    def test_run_defines_multi_value(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "-D", "TARGET=linux", "-D", "TARGET=windows"])
        assert args.defines == ["TARGET=linux", "TARGET=windows"]

    def test_run_serialize(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "--serialize"])
        assert args.serialize is True

    def test_run_serialize_default(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml"])
        assert args.serialize is False

    def test_do_serialize(self):
        parser = build_parser()
        args = parser.parse_args(["do", "build a thing", "--serialize"])
        assert args.serialize is True

    def test_rich_flag(self):
        parser = build_parser()
        args = parser.parse_args(["--rich", "run", "workflow.yaml"])
        assert args.rich is True

    def test_default_is_plain(self):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml"])
        assert args.rich is False

    def test_no_command(self):
        parser = build_parser()
        args = parser.parse_args([])
        assert args.command is None

    def test_version(self, capsys):
        parser = build_parser()
        try:
            parser.parse_args(["--version"])
        except SystemExit:
            pass
        captured = capsys.readouterr()
        from juvenal import __version__

        assert __version__ in captured.out


class TestParseDefines:
    def test_single_value(self):
        assert _parse_defines(["FOO=bar"]) == {"FOO": ["bar"]}

    def test_multi_value_same_key(self):
        result = _parse_defines(["T=linux", "T=windows"])
        assert result == {"T": ["linux", "windows"]}

    def test_mixed_single_and_multi(self):
        result = _parse_defines(["ENV=prod", "T=a", "T=b"])
        assert result == {"ENV": ["prod"], "T": ["a", "b"]}

    def test_equals_in_value(self):
        result = _parse_defines(["CMD=a=b=c"])
        assert result == {"CMD": ["a=b=c"]}


class TestCmdRun:
    def test_phased_implementer_reuses_planner_and_replaces_planned_checkers(self, monkeypatch, tmp_path):
        import juvenal.engine
        from juvenal.engine import PlanResult

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        workflow_path = project_dir / "workflow.yaml"
        workflow_path.write_text(
            """\
name: planned
phases:
  - id: analyze
    prompt: "Analyze the problem."
  - id: analyze-review
    type: check
    role: architect
    bounce_target: analyze
  - id: build
    prompt: "Build the solution."
  - id: build-review
    type: check
    role: pm
    bounce_target: build
""",
        )

        planned_calls = {}
        engine_calls = {}

        def mock_plan_workflow_internal(
            goal,
            backend_name="codex",
            plain=False,
            interactive=False,
            resume=False,
            project_dir=None,
            serialize=False,
            **_,
        ):
            planned_calls["goal"] = goal
            planned_calls["backend_name"] = backend_name
            planned_calls["interactive"] = interactive
            planned_calls["resume"] = resume
            planned_calls["project_dir"] = project_dir
            planned_calls["serialize"] = serialize
            return PlanResult(success=True, workflow_yaml_path=str(workflow_path))

        class DummyEngine:
            def __init__(self, workflow, **kwargs):
                engine_calls["workflow"] = workflow
                engine_calls["kwargs"] = kwargs

            def run(self):
                return 0

        monkeypatch.setattr(juvenal.engine, "_plan_workflow_internal", mock_plan_workflow_internal)
        monkeypatch.setattr(juvenal.engine, "Engine", DummyEngine)

        parser = build_parser()
        args = parser.parse_args(
            [
                "run",
                "--phased-implementer",
                'software-engineer:"Build a multi-step feature"',
                "--checker",
                "tester",
                "--working-dir",
                str(project_dir),
                "--interactive",
            ]
        )
        args.plain = True

        assert cmd_run(args) == 0
        assert planned_calls == {
            "goal": "Build a multi-step feature",
            "backend_name": "codex",
            "interactive": True,
            "resume": False,
            "project_dir": str(project_dir),
            "serialize": False,
        }

        workflow = engine_calls["workflow"]
        assert [phase.id for phase in workflow.phases] == ["analyze", "analyze~check-1", "build", "build~check-1"]
        assert workflow.phases[1].role == "tester"
        assert workflow.phases[3].role == "tester"
        assert "expert software engineer" in workflow.phases[0].prompt
        assert "expert software engineer" in workflow.phases[2].prompt
        assert workflow.working_dir == str(project_dir)
        assert engine_calls["kwargs"]["interactive"] is True

    def test_phased_implementer_rejects_workflow_path(self, capsys):
        parser = build_parser()
        args = parser.parse_args(["run", "workflow.yaml", "--phased-implementer", "build a thing"])
        args.plain = True

        assert cmd_run(args) == 1
        captured = capsys.readouterr()
        assert "--phased-implementer cannot be used with a workflow path" in captured.out

    def test_phased_implementer_rejects_implementer_flag(self, capsys):
        parser = build_parser()
        args = parser.parse_args(["run", "--phased-implementer", "build a thing", "--implementer", "software-engineer"])
        args.plain = True

        assert cmd_run(args) == 1
        captured = capsys.readouterr()
        assert "--phased-implementer cannot be combined with --implementer" in captured.out

    def test_role_only_implementer_does_not_double_apply_to_inline_phase(self, monkeypatch):
        import juvenal.engine

        engine_calls = {}

        class DummyEngine:
            def __init__(self, workflow, **kwargs):
                engine_calls["workflow"] = workflow
                engine_calls["kwargs"] = kwargs

            def run(self):
                return 0

        monkeypatch.setattr(juvenal.engine, "Engine", DummyEngine)

        parser = build_parser()
        args = parser.parse_args(
            [
                "run",
                "--implementer",
                'software-engineer:"Build it."',
                "--implementer",
                "software-engineer",
            ]
        )
        args.plain = True

        assert cmd_run(args) == 0
        prompt = engine_calls["workflow"].phases[0].prompt
        assert prompt.count("expert software engineer") == 1


class TestStatusExitCode:
    def _make_args(self, state_file):
        import argparse

        return argparse.Namespace(state_file=str(state_file))

    def test_status_returns_0_when_all_completed(self, tmp_path):
        """Fully successful pipeline returns exit code 0."""
        state_file = tmp_path / "state.json"
        state = PipelineState(state_file=state_file)
        state.started_at = time.time() - 10
        state.set_attempt("build", 1)
        state.mark_completed("build")
        state.set_attempt("test", 1)
        state.mark_completed("test")
        state.completed_at = time.time()
        state.save()

        assert cmd_status(self._make_args(state_file)) == 0

    def test_status_returns_1_when_failed(self, tmp_path):
        """Pipeline with a failed phase returns exit code 1."""
        state_file = tmp_path / "state.json"
        state = PipelineState(state_file=state_file)
        state.started_at = time.time() - 10
        state.set_attempt("build", 1)
        state.mark_completed("build")
        state.set_attempt("test", 1)
        state.mark_failed("test")
        state.completed_at = time.time()
        state.save()

        assert cmd_status(self._make_args(state_file)) == 1

    def test_status_returns_1_when_incomplete(self, tmp_path):
        """Pipeline still running (no completed_at) returns exit code 1."""
        state_file = tmp_path / "state.json"
        state = PipelineState(state_file=state_file)
        state.started_at = time.time()
        state.set_attempt("build", 1)
        state.mark_completed("build")
        state.save()

        assert cmd_status(self._make_args(state_file)) == 1

    def test_status_returns_1_when_no_state(self, tmp_path):
        """No state file (never run) returns exit code 1."""
        state_file = tmp_path / "state.json"
        assert cmd_status(self._make_args(state_file)) == 1


class TestStatusExitCodeSubprocess:
    """Test that exit codes actually propagate through the real entry point."""

    def test_status_subprocess_exit_0_on_success(self, tmp_path):
        """Successful pipeline exits 0 as a real process."""
        state_file = tmp_path / "state.json"
        state = PipelineState(state_file=state_file)
        state.started_at = time.time() - 10
        state.set_attempt("build", 1)
        state.mark_completed("build")
        state.completed_at = time.time()
        state.save()

        result = subprocess.run(
            [sys.executable, "-m", "juvenal.cli", "status", "--state-file", str(state_file)],
            capture_output=True,
        )
        assert result.returncode == 0

    def test_status_subprocess_exit_1_on_no_state(self, tmp_path):
        """No state file exits 1 as a real process."""
        state_file = tmp_path / "nonexistent.json"
        result = subprocess.run(
            [sys.executable, "-m", "juvenal.cli", "status", "--state-file", str(state_file)],
            capture_output=True,
        )
        assert result.returncode == 1

    def test_plan_interactive_flag(self):
        parser = build_parser()
        args = parser.parse_args(["plan", "build a web app", "--interactive"])
        assert args.interactive is True

    def test_plan_interactive_short_flag(self):
        parser = build_parser()
        args = parser.parse_args(["plan", "build a web app", "-i"])
        assert args.interactive is True

    def test_plan_no_interactive_default(self):
        parser = build_parser()
        args = parser.parse_args(["plan", "build a web app"])
        assert args.interactive is False

    def test_do_interactive_flag(self):
        parser = build_parser()
        args = parser.parse_args(["do", "build a web app", "--interactive"])
        assert args.interactive is True

    def test_do_interactive_short_flag(self):
        parser = build_parser()
        args = parser.parse_args(["do", "build a web app", "-i"])
        assert args.interactive is True

    def test_plan_resume_flag(self):
        parser = build_parser()
        args = parser.parse_args(["plan", "build a web app", "--resume"])
        assert args.resume is True

    def test_plan_no_resume_default(self):
        parser = build_parser()
        args = parser.parse_args(["plan", "build a web app"])
        assert args.resume is False

    def test_plan_interactive_preserves_backend(self, monkeypatch):
        """--interactive does not override the backend — only interactive phases use Claude."""
        import juvenal.engine

        called_with = {}

        def mock_plan_workflow(goal, output, backend, plain=False, interactive=False, resume=False):
            called_with["backend"] = backend
            called_with["interactive"] = interactive

        monkeypatch.setattr(juvenal.engine, "plan_workflow", mock_plan_workflow)

        parser = build_parser()
        args = parser.parse_args(["plan", "build something", "--interactive", "--backend", "codex"])
        args.plain = False
        cmd_plan(args)

        assert called_with["backend"] == "codex"
        assert called_with["interactive"] is True

    def test_plan_rejects_run_checker_before_planning(self, monkeypatch, capsys, tmp_path):
        """Invalid checker specs fail before the planner is invoked."""
        import juvenal.engine

        called = False

        def mock_plan_workflow(goal, output, backend, plain=False, interactive=False, resume=False):
            nonlocal called
            called = True

        monkeypatch.setattr(juvenal.engine, "plan_workflow", mock_plan_workflow)

        parser = build_parser()
        args = parser.parse_args(
            ["plan", "build something", "-o", str(tmp_path / "workflow.yaml"), "--checker", "run:pytest -x"]
        )
        args.plain = True

        with pytest.raises(SystemExit) as excinfo:
            cmd_plan(args)

        captured = capsys.readouterr()
        assert excinfo.value.code == 1
        assert called is False
        assert "Error: Invalid --checker spec" in captured.out

    def test_plan_prompt_checker_writes_checks_and_loads(self, monkeypatch, tmp_path):
        """Prompt checkers injected by plan use the supported checks: schema."""
        import yaml

        import juvenal.engine
        from juvenal.workflow import load_workflow

        def mock_plan_workflow(goal, output, backend, plain=False, interactive=False, resume=False):
            with open(output, "w") as f:
                f.write(
                    """\
name: test
phases:
  - id: build
    prompt: "Build it."
""",
                )

        monkeypatch.setattr(juvenal.engine, "plan_workflow", mock_plan_workflow)

        output_path = tmp_path / "workflow.yaml"
        parser = build_parser()
        args = parser.parse_args(
            ["plan", "build something", "-o", str(output_path), "--checker", "prompt:Verify the build"]
        )
        args.plain = True

        assert cmd_plan(args) == 0

        data = yaml.safe_load(output_path.read_text())
        phase = data["phases"][0]
        assert "checkers" not in phase
        assert phase["checks"] == [{"prompt": "Verify the build"}]

        workflow = load_workflow(output_path)
        assert [p.id for p in workflow.phases] == ["build", "build~check-1"]
        assert workflow.phases[1].type == "check"
        assert workflow.phases[1].prompt == "Verify the build"
        assert workflow.phases[1].bounce_target == "build"

    def test_plan_specialized_role_checker_writes_checks_and_loads(self, monkeypatch, tmp_path):
        """Role-specialized checkers injected by plan preserve both role and prompt."""
        import yaml

        import juvenal.engine
        from juvenal.workflow import load_workflow

        def mock_plan_workflow(goal, output, backend, plain=False, interactive=False, resume=False):
            with open(output, "w") as f:
                f.write(
                    """\
name: test
phases:
  - id: build
    prompt: "Build it."
""",
                )

        monkeypatch.setattr(juvenal.engine, "plan_workflow", mock_plan_workflow)

        output_path = tmp_path / "workflow.yaml"
        parser = build_parser()
        args = parser.parse_args(
            ["plan", "build something", "-o", str(output_path), "--checker", "tester:Focus on API error handling."]
        )
        args.plain = True

        assert cmd_plan(args) == 0

        data = yaml.safe_load(output_path.read_text())
        phase = data["phases"][0]
        assert phase["checks"] == [{"role": "tester", "prompt": "Focus on API error handling."}]

        workflow = load_workflow(output_path)
        assert [p.id for p in workflow.phases] == ["build", "build~check-1"]
        assert workflow.phases[1].type == "check"
        assert workflow.phases[1].role == "tester"
        assert workflow.phases[1].prompt == "Focus on API error handling."
        assert workflow.phases[1].bounce_target == "build"

    def test_plan_quoted_specialized_role_checker_writes_checks_and_loads(self, monkeypatch, tmp_path):
        """Quoted checker specializations are normalized before they reach workflow YAML."""
        import yaml

        import juvenal.engine
        from juvenal.workflow import load_workflow

        def mock_plan_workflow(goal, output, backend, plain=False, interactive=False, resume=False):
            with open(output, "w") as f:
                f.write(
                    """\
name: test
phases:
  - id: build
    prompt: "Build it."
""",
                )

        monkeypatch.setattr(juvenal.engine, "plan_workflow", mock_plan_workflow)

        output_path = tmp_path / "workflow.yaml"
        parser = build_parser()
        args = parser.parse_args(
            ["plan", "build something", "-o", str(output_path), "--checker", 'tester:"Focus on API error handling."']
        )
        args.plain = True

        assert cmd_plan(args) == 0

        data = yaml.safe_load(output_path.read_text())
        phase = data["phases"][0]
        assert phase["checks"] == [{"role": "tester", "prompt": "Focus on API error handling."}]

        workflow = load_workflow(output_path)
        assert [p.id for p in workflow.phases] == ["build", "build~check-1"]
        assert workflow.phases[1].role == "tester"
        assert workflow.phases[1].prompt == "Focus on API error handling."

    def test_status_subprocess_exit_1_on_failure(self, tmp_path):
        """Failed pipeline exits 1 as a real process."""
        state_file = tmp_path / "state.json"
        state = PipelineState(state_file=state_file)
        state.started_at = time.time() - 10
        state.set_attempt("build", 1)
        state.mark_failed("build")
        state.completed_at = time.time()
        state.save()

        result = subprocess.run(
            [sys.executable, "-m", "juvenal.cli", "status", "--state-file", str(state_file)],
            capture_output=True,
        )
        assert result.returncode == 1

    def test_validate_subprocess_rejects_run_checker_cleanly(self, tmp_path):
        """Unsupported run: checker exits cleanly instead of raising a traceback."""
        workflow_path = tmp_path / "workflow.yaml"
        workflow_path.write_text(
            """\
name: test
phases:
  - id: build
    prompt: "Build it."
""",
        )

        result = subprocess.run(
            [sys.executable, "-m", "juvenal.cli", "validate", str(workflow_path), "--checker", "run:pytest -x"],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 1
        assert "Error: Invalid --checker spec" in result.stdout
        assert "Traceback" not in result.stderr

    def test_do_rejects_run_checker_before_planning(self, monkeypatch, capsys):
        """Invalid checker specs fail before planning starts on do."""
        import juvenal.engine

        called = False

        def mock_plan_workflow(goal, output, backend, plain=False, interactive=False, resume=False):
            nonlocal called
            called = True

        monkeypatch.setattr(juvenal.engine, "plan_workflow", mock_plan_workflow)

        parser = build_parser()
        args = parser.parse_args(["do", "build something", "--checker", "run:pytest -x"])
        args.plain = True

        from juvenal.cli import cmd_do

        with pytest.raises(SystemExit) as excinfo:
            cmd_do(args)

        captured = capsys.readouterr()
        assert excinfo.value.code == 1
        assert called is False
        assert "Error: Invalid --checker spec" in captured.out
