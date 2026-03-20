"""Regression tests for the embedded Juvenal API."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from juvenal.api import (
    JuvenalExecutionError,
    JuvenalUsageError,
    do,
    goal,
    plan_and_do,
)
from juvenal.engine import PlanResult
from juvenal.workflow import load_workflow as load_workflow_impl
from tests.conftest import MockBackend


def _init_git_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True, text=True)


def test_goal_rejects_missing_working_dir(tmp_path):
    with pytest.raises(JuvenalUsageError, match="Working directory does not exist"):
        with goal("Goal", working_dir=tmp_path / "missing", backend=MockBackend()):
            pass


def test_goal_rejects_non_directory_working_dir(tmp_path):
    file_path = tmp_path / "goal.txt"
    file_path.write_text("not a directory")

    with pytest.raises(JuvenalUsageError, match="Working directory is not a directory"):
        with goal("Goal", working_dir=file_path, backend=MockBackend()):
            pass


def test_goal_rejects_unknown_backend(tmp_path):
    with pytest.raises(JuvenalUsageError, match="Unknown backend"):
        with goal("Goal", working_dir=tmp_path, backend="not-a-backend"):
            pass


def test_goal_restores_nested_session_and_allocates_distinct_default_session_dirs(tmp_path):
    outer_backend = MockBackend()
    inner_backend = MockBackend()
    outer_backend.add_response(exit_code=0, output="outer done")
    inner_backend.add_response(exit_code=0, output="inner done")

    with goal("Outer goal", working_dir=tmp_path, backend=outer_backend) as outer:
        assert outer.artifact_root == (tmp_path / ".juvenal-api").resolve()
        assert outer.session_artifact_dir == (tmp_path / ".juvenal-api" / "session-001").resolve()
        assert outer.session_artifact_dir.is_dir()

        with goal("Inner goal", working_dir=tmp_path, backend=inner_backend) as inner:
            assert inner.artifact_root == outer.artifact_root
            assert inner.session_artifact_dir == (tmp_path / ".juvenal-api" / "session-002").resolve()
            assert inner.session_artifact_dir.is_dir()
            assert inner.session_artifact_dir != outer.session_artifact_dir
            do("Inner task")
            assert [entry["instruction"] for entry in inner.history] == ["Inner task"]

        do("Outer task")
        assert [entry["instruction"] for entry in outer.history] == ["Outer task"]


def test_do_requires_active_goal_session():
    with pytest.raises(JuvenalUsageError, match="requires an active juvenal.goal"):
        do("Build the feature")


@pytest.mark.parametrize(
    ("operation", "match"),
    [
        (lambda: do(""), "empty after stripping whitespace"),
        (lambda: do([]), "requires at least one task"),
        (lambda: do(["ok", 7]), "task 2 must be a string"),
        (lambda: do("Build", checker="tester", checkers=["pm"]), "either checker= or checkers="),
        (lambda: do("Build", checkers="tester"), "must be a sequence of checker specs"),
        (lambda: do("Build", checker="not-a-valid-role"), "Invalid --checker spec"),
    ],
)
def test_do_rejects_misuse_cases(tmp_path, operation, match):
    with goal("Goal", working_dir=tmp_path, backend=MockBackend()):
        with pytest.raises(JuvenalUsageError, match=match):
            operation()


def test_do_single_step_run_records_history_and_state(tmp_path):
    backend = MockBackend()
    backend.add_response(exit_code=0, output="implemented")
    backend.add_response(exit_code=0, output="VERDICT: PASS")

    with goal("Ship the API", working_dir=tmp_path, backend=backend) as session:
        do("Implement the API", checker="tester")

        assert len(backend.calls) == 2
        implement_prompt = backend.calls[0]
        assert "Ship the API" in implement_prompt
        assert str(tmp_path.resolve()) in implement_prompt
        assert "Implement the API" in implement_prompt

        assert [entry["instruction"] for entry in session.history] == ["Implement the API"]
        assert session.history[0]["success"] is True
        assert "Implement the API" in session.history[0]["summary"]
        do_state_files = list(session.session_artifact_dir.glob("*-do.json"))
        assert len(do_state_files) == 1
        assert do_state_files[0].exists()


def test_do_multi_step_run_uses_completed_steps_context_and_records_each_step(tmp_path):
    backend = MockBackend()
    backend.add_response(exit_code=0, output="first done")
    backend.add_response(exit_code=0, output="VERDICT: PASS")
    backend.add_response(exit_code=0, output="second done")
    backend.add_response(exit_code=0, output="VERDICT: PASS")

    with goal("Ship the API", working_dir=tmp_path, backend=backend) as session:
        do(["Scaffold handlers", "Wire auth"], checker="tester")

        assert len(backend.calls) == 4
        first_prompt = backend.calls[0]
        second_prompt = backend.calls[2]
        assert "Scaffold handlers" in first_prompt
        assert "Wire auth" not in first_prompt
        assert "Wire auth" in second_prompt
        assert "Scaffold handlers" in second_prompt
        assert second_prompt.find("Scaffold handlers") < second_prompt.find("Wire auth")

        assert [entry["instruction"] for entry in session.history] == ["Scaffold handlers", "Wire auth"]
        assert len({entry["phase_id"] for entry in session.history}) == 2


def test_do_reuses_successful_history_in_later_prompts(tmp_path):
    backend = MockBackend()
    backend.add_response(exit_code=0, output="repo prepared")
    backend.add_response(exit_code=0, output="api implemented")

    with goal("Ship the API", working_dir=tmp_path, backend=backend) as session:
        do("Prepare the repository")
        first_summary = session.history[0]["summary"]

        do("Implement the API")

        assert len(backend.calls) == 2
        assert first_summary in backend.calls[1]
        assert [entry["instruction"] for entry in session.history] == ["Prepare the repository", "Implement the API"]


def test_do_preserves_partial_success_history_when_later_step_fails(tmp_path):
    backend = MockBackend()
    backend.add_response(exit_code=0, output="step 1 done")
    backend.add_response(exit_code=0, output="VERDICT: PASS")
    backend.add_response(exit_code=0, output="step 2 done")
    backend.add_response(exit_code=0, output="VERDICT: FAIL: tests are still broken")

    with goal("Ship the API", working_dir=tmp_path, backend=backend, max_bounces=1) as session:
        with pytest.raises(JuvenalExecutionError) as exc_info:
            do(["Finish setup", "Finish integration"], checker="tester")

        error = exc_info.value
        assert error.inspection_path.parent == session.session_artifact_dir
        assert error.inspection_path.name.endswith("-do.json")
        assert error.inspection_path.exists()
        assert [entry["instruction"] for entry in session.history] == ["Finish setup"]
        assert session.history[0]["phase_id"]


def test_goal_resolves_exclude_file_via_git_in_linked_worktree(tmp_path):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    subprocess.run(["git", "config", "user.email", "juvenal@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Juvenal Tests"], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True)

    worktree_dir = tmp_path / "linked-worktree"
    subprocess.run(["git", "worktree", "add", str(worktree_dir)], cwd=repo, check=True)
    exclude_file = Path(
        subprocess.run(
            ["git", "-C", str(worktree_dir), "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )

    before = exclude_file.read_text() if exclude_file.exists() else ""
    before_lines = before.splitlines()

    with goal("Goal", working_dir=worktree_dir, backend=MockBackend()):
        pass

    assert (worktree_dir / ".git").is_file()
    assert exclude_file.read_text().splitlines() == before_lines + ["/.juvenal-api/"]


@pytest.mark.parametrize(
    ("working_dir_kind", "artifact_dir", "expected_entry"),
    [
        ("repo-root", None, "/.juvenal-api/"),
        ("nested", None, "/app/.juvenal-api/"),
        ("nested", Path("..") / "artifacts" / "embedded", "/artifacts/embedded/"),
        ("nested", "__outside_repo__", None),
    ],
)
def test_goal_git_exclude_entries_are_repo_relative(tmp_path, working_dir_kind, artifact_dir, expected_entry):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    app_dir = repo / "app"
    app_dir.mkdir()
    exclude_file = repo / ".git" / "info" / "exclude"
    backend = MockBackend()

    working_dir = repo if working_dir_kind == "repo-root" else app_dir
    resolved_artifact_dir = tmp_path / "outside-artifacts" if artifact_dir == "__outside_repo__" else artifact_dir
    before = exclude_file.read_text() if exclude_file.exists() else ""
    before_lines = before.splitlines()

    with goal("Goal", working_dir=working_dir, artifact_dir=resolved_artifact_dir, backend=backend):
        pass

    after = exclude_file.read_text() if exclude_file.exists() else ""
    if expected_entry is None:
        assert after == before
    else:
        assert after.splitlines() == before_lines + [expected_entry]


def test_plan_and_do_uses_session_working_dir_and_preserves_history(tmp_path):
    backend = MockBackend()
    backend.add_response(exit_code=0, output="repo prepared")
    backend.add_response(exit_code=0, output="planned work complete")
    captured: dict[str, object] = {}
    planned_yaml = "name: planned\nphases:\n  - id: execute\n    prompt: 'Execute the planned work.'\n"

    with goal("Ship the API", working_dir=tmp_path, backend=backend) as session:
        do("Prepare the repository")
        prior_summary = session.history[0]["summary"]

        def fake_plan(**kwargs):
            captured.update(kwargs)
            workflow_path = Path(kwargs["project_dir"]) / "workflow.yaml"
            workflow_path.write_text(planned_yaml)
            return PlanResult(
                success=True,
                workflow_yaml_path=str(workflow_path),
                temp_dir=None,
                input_tokens=11,
                output_tokens=22,
            )

        with patch("juvenal.api._plan_workflow_internal", side_effect=fake_plan):
            with patch("juvenal.api.load_workflow", wraps=load_workflow_impl) as load_workflow_mock:
                plan_and_do("Break the work into phases.")

        assert captured["project_dir"] == session.working_dir
        assert captured["backend_instance"] is backend
        assert captured["plain"] is False
        assert "Ship the API" in captured["goal"]
        assert prior_summary in captured["goal"]
        assert "Break the work into phases." in captured["goal"]
        assert load_workflow_mock.call_count == 1
        assert load_workflow_mock.call_args.args[0] == (tmp_path / "workflow.yaml").resolve()
        assert session.history[-1]["kind"] == "plan_and_do"
        assert session.history[-1]["goal_text"] == "Break the work into phases."
        workflow_archives = list(session.session_artifact_dir.glob("*-workflow.yaml"))
        planned_state_files = list(session.session_artifact_dir.glob("*-planned.json"))
        assert len(workflow_archives) == 1
        assert workflow_archives[0].read_text() == planned_yaml
        assert len(planned_state_files) == 1
        assert planned_state_files[0].exists()
        assert "Execute the planned work." in backend.calls[-1]


def test_plan_and_do_load_failure_reports_workflow_yaml_path(tmp_path):
    backend = MockBackend()
    bad_yaml = "name: planned\nphases:\n  - just-a-string\n"

    with goal("Ship the API", working_dir=tmp_path, backend=backend) as session:
        def fake_plan(**kwargs):
            workflow_path = Path(kwargs["project_dir"]) / "workflow.yaml"
            workflow_path.write_text(bad_yaml)
            return PlanResult(
                success=True,
                workflow_yaml_path=str(workflow_path),
                temp_dir=None,
            )

        with patch("juvenal.api._plan_workflow_internal", side_effect=fake_plan):
            with pytest.raises(JuvenalExecutionError) as exc_info:
                plan_and_do("Break the work into phases.")

        error = exc_info.value
        assert error.inspection_path == (tmp_path / "workflow.yaml").resolve()
        workflow_archives = list(session.session_artifact_dir.glob("*-workflow.yaml"))
        assert len(workflow_archives) == 1
        assert workflow_archives[0].read_text() == bad_yaml


def test_plan_and_do_planned_engine_failure_reports_state_file(tmp_path):
    backend = MockBackend()
    backend.add_response(exit_code=1, output="crash")
    planned_yaml = "name: planned\nphases:\n  - id: execute\n    prompt: 'Execute the planned work.'\n"

    with goal("Ship the API", working_dir=tmp_path, backend=backend, max_bounces=1) as session:
        def fake_plan(**kwargs):
            workflow_path = Path(kwargs["project_dir"]) / "workflow.yaml"
            workflow_path.write_text(planned_yaml)
            return PlanResult(
                success=True,
                workflow_yaml_path=str(workflow_path),
                temp_dir=None,
            )

        with patch("juvenal.api._plan_workflow_internal", side_effect=fake_plan):
            with pytest.raises(JuvenalExecutionError) as exc_info:
                plan_and_do("Break the work into phases.")

        error = exc_info.value
        assert error.inspection_path.parent == session.session_artifact_dir
        assert error.inspection_path.name.endswith("-planned.json")
        assert error.inspection_path.exists()
        assert session.history == []


def test_plan_and_do_planning_failure_reports_planner_state_path(tmp_path):
    backend = MockBackend()

    with goal("Ship the API", working_dir=tmp_path, backend=backend) as session:
        def fake_run(self):
            self.state.set_attempt("planner", 1)
            self.state.mark_failed("planner")
            return 1

        with patch("juvenal.engine.Engine.run", autospec=True, side_effect=fake_run):
            with pytest.raises(JuvenalExecutionError) as exc_info:
                plan_and_do("Break the work into phases.")

        error = exc_info.value
        assert error.inspection_path == (tmp_path / ".plan" / ".juvenal-state.json").resolve()
        assert error.inspection_path.exists()
        state = json.loads(error.inspection_path.read_text())
        assert state["phases"]["planner"]["attempt"] == 1
        assert state["phases"]["planner"]["status"] == "failed"
        assert session.history == []
