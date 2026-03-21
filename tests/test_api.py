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


@pytest.mark.parametrize(
    "session_name",
    ["", "Example", "has_underscore", "two--parts", "-leading", "trailing-", "with space", " spaced", "a/b", ".", ".."],
)
def test_goal_rejects_invalid_session_names(tmp_path, session_name):
    with pytest.raises(JuvenalUsageError, match="session_name must match"):
        with goal("Goal", working_dir=tmp_path, backend=MockBackend(), session_name=session_name):
            pass


def test_goal_rejects_reserved_named_session_namespace(tmp_path):
    with pytest.raises(JuvenalUsageError, match="reserved for anonymous sessions"):
        with goal("Goal", working_dir=tmp_path, backend=MockBackend(), session_name="session-001"):
            pass


@pytest.mark.parametrize("manifest_text", [None, "{not json"])
def test_goal_named_session_requires_valid_manifest_when_directory_exists(tmp_path, manifest_text):
    session_dir = tmp_path / ".juvenal-api" / "example"
    session_dir.mkdir(parents=True)
    if manifest_text is not None:
        (session_dir / "session.json").write_text(manifest_text)

    with pytest.raises(JuvenalUsageError, match="Invalid session manifest"):
        with goal("Goal", working_dir=tmp_path, backend=MockBackend(), session_name="example"):
            pass


def test_goal_named_session_rejects_manifest_missing_session_name(tmp_path):
    with goal("Goal", working_dir=tmp_path, backend=MockBackend(), session_name="example") as session:
        manifest = json.loads(session.manifest_path.read_text())

    del manifest["session_name"]
    session.manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(JuvenalUsageError, match="session_name is required for named sessions"):
        with goal("Goal", working_dir=tmp_path, backend=MockBackend(), session_name="example"):
            pass


def test_goal_named_session_rejects_identity_mismatches(tmp_path):
    working_dir = tmp_path / "work"
    working_dir.mkdir()
    other_working_dir = tmp_path / "other"
    other_working_dir.mkdir()
    artifact_dir = tmp_path / "artifacts"

    class AlternateBackend(MockBackend):
        def name(self) -> str:
            return "alternate-mock"

    with goal(
        "Goal",
        working_dir=working_dir,
        artifact_dir=artifact_dir,
        backend=MockBackend(),
        session_name="example",
    ):
        pass

    mismatch_cases = [
        ({"description": "Different goal"}, "goal_text"),
        ({"working_dir": other_working_dir}, "working_dir"),
        ({"backend": AlternateBackend()}, "backend_name"),
        ({"max_bounces": 1}, "max_bounces"),
        ({"serialize": True}, "serialize"),
        ({"clear_context_on_bounce": True}, "clear_context_on_bounce"),
    ]

    for overrides, field_name in mismatch_cases:
        kwargs = {
            "description": "Goal",
            "working_dir": working_dir,
            "artifact_dir": artifact_dir,
            "backend": MockBackend(),
            "session_name": "example",
        }
        kwargs.update(overrides)
        with pytest.raises(JuvenalUsageError, match=field_name):
            with goal(**kwargs):
                pass


def test_goal_named_session_reuses_manifest_and_restores_state(tmp_path):
    first_backend = MockBackend()
    first_backend.add_response(exit_code=0, output="repo prepared")

    with goal("Ship the API", working_dir=tmp_path, backend=first_backend, session_name="example") as session:
        assert session.session_id == "example"
        assert session.session_key == "example"
        assert session.session_name == "example"
        assert session.session_artifact_dir == (tmp_path / ".juvenal-api" / "example").resolve()
        assert session.manifest_path == (tmp_path / ".juvenal-api" / "example" / "session.json").resolve()
        assert session.manifest_path.exists()

        do("Prepare the repository")

        manifest = json.loads(session.manifest_path.read_text())
        assert manifest["run_counter"] == 1
        assert [entry["instruction"] for entry in manifest["history"]] == ["Prepare the repository"]
        assert manifest["stages"]["do-001"]["status"] == "completed"

    second_backend = MockBackend()
    second_backend.add_response(exit_code=0, output="api implemented")

    with goal(
        "Ship the API",
        working_dir=tmp_path,
        backend=second_backend,
        plain=True,
        session_name="example",
    ) as session:
        assert session.plain is True
        assert session.run_counter == 1
        assert [entry["instruction"] for entry in session.history] == ["Prepare the repository"]
        assert session.stages["do-001"]["status"] == "completed"

        do("Implement the API")

        assert [entry["instruction"] for entry in session.history] == ["Prepare the repository", "Implement the API"]
        assert (session.session_artifact_dir / "run-002-do.json").exists()

        manifest = json.loads(session.manifest_path.read_text())
        assert manifest["run_counter"] == 2
        assert [entry["instruction"] for entry in manifest["history"]] == [
            "Prepare the repository",
            "Implement the API",
        ]
        assert manifest["stages"]["do-002"]["status"] == "completed"


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


def test_do_persists_run_id_checkpoint_when_workflow_validation_fails(tmp_path):
    with goal("Ship the API", working_dir=tmp_path, backend=MockBackend(), session_name="example") as session:
        with pytest.raises(JuvenalUsageError, match="template variable"):
            do("Implement {{VAR}} support")

        manifest = json.loads(session.manifest_path.read_text())
        assert manifest["run_counter"] == 1
        assert manifest["history"] == []
        assert manifest["stages"]["do-001"]["status"] == "failed"

    with goal("Ship the API", working_dir=tmp_path, backend=MockBackend(), session_name="example") as resumed:
        assert resumed.run_counter == 1
        assert resumed.history == []
        assert resumed.stages["do-001"]["status"] == "failed"


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


def test_staged_do_resumes_with_stored_prompt_snapshots_and_same_run_id(tmp_path):
    first_backend = MockBackend()
    first_backend.add_response(exit_code=0, output="step 1 done")
    first_backend.add_response(exit_code=0, output="VERDICT: PASS")
    first_backend.add_response(exit_code=1, output="step 2 crashed")

    with goal(
        "Ship the API",
        working_dir=tmp_path,
        backend=first_backend,
        max_bounces=1,
        session_name="example",
    ) as session:
        with pytest.raises(JuvenalExecutionError):
            do(["Step one", "Step two"], checker="tester", stage_id="build-stage")

        assert session.stages["build-stage"]["status"] == "running"
        assert session.stages["build-stage"]["run_id"] == "001"
        assert [entry["instruction"] for entry in session.history] == ["Step one"]

    second_backend = MockBackend()
    second_backend.add_response(exit_code=0, output="later done")
    second_backend.add_response(exit_code=0, output="step 2 done")
    second_backend.add_response(exit_code=0, output="VERDICT: PASS")

    with goal(
        "Ship the API",
        working_dir=tmp_path,
        backend=second_backend,
        max_bounces=1,
        session_name="example",
    ) as session:
        do("Later history")
        later_summary = session.history[-1]["summary"]

        do(["Step one", "Step two"], checker="tester", stage_id="build-stage")

        assert session.stages["build-stage"]["status"] == "completed"
        assert session.stages["build-stage"]["run_id"] == "001"
        assert later_summary not in second_backend.calls[1]
        assert "Step one" in second_backend.calls[1]
        assert "Step two" in second_backend.calls[1]


def test_staged_do_rejects_reusing_stage_id_with_different_inputs(tmp_path):
    backend = MockBackend()
    backend.add_response(exit_code=0, output="prepared")

    with goal("Ship the API", working_dir=tmp_path, backend=backend, session_name="example"):
        do("Prepare the repository", stage_id="build-stage")

        with pytest.raises(JuvenalUsageError, match="different do\\(\\) inputs"):
            do("Implement the API", stage_id="build-stage")


def test_one_shot_do_rejects_collision_with_existing_user_stage_id(tmp_path):
    backend = MockBackend()
    backend.add_response(exit_code=0, output="prepared")

    with goal("Ship the API", working_dir=tmp_path, backend=backend, session_name="example") as session:
        do("Prepare the repository", stage_id="do-002")

        with pytest.raises(JuvenalUsageError, match="Stage 'do-002' already exists"):
            do("Implement the API")

        assert len(backend.calls) == 1
        assert session.stages["do-002"]["tasks"] == ["Prepare the repository"]
        assert session.stages["do-002"]["status"] == "completed"


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
    wrong_yaml = "name: wrong\nphases:\n  - id: wrong\n    prompt: 'Execute the wrong workflow.'\n"

    with goal("Ship the API", working_dir=tmp_path, backend=backend) as session:
        do("Prepare the repository")
        prior_summary = session.history[0]["summary"]

        def fake_plan(**kwargs):
            captured.update(kwargs)
            workflow_path = Path(kwargs["project_dir"]) / "workflow.yaml"
            workflow_path.write_text(planned_yaml)
            returned_path = Path(kwargs["project_dir"]) / ".plan" / "planner-returned.yaml"
            returned_path.write_text(wrong_yaml)
            return PlanResult(
                success=True,
                workflow_yaml_path=str(returned_path),
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
        assert Path(captured["project_dir"]) / ".plan" / "planner-returned.yaml" != (tmp_path / "workflow.yaml")
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
        assert "Execute the wrong workflow." not in backend.calls[-1]
        manifest = json.loads(session.manifest_path.read_text())
        assert manifest["run_counter"] == 2
        assert manifest["history"][-1]["kind"] == "plan_and_do"
        assert manifest["stages"]["do-001"]["status"] == "completed"
        assert manifest["stages"]["plan-and-do-002"]["status"] == "completed"


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


def test_staged_plan_and_do_requires_named_session(tmp_path):
    with goal("Ship the API", working_dir=tmp_path, backend=MockBackend()):
        with pytest.raises(JuvenalUsageError, match="named session"):
            plan_and_do("Break the work into phases.", stage_id="plan-stage")


def test_staged_plan_and_do_rejects_second_owner_from_persisted_manifest(tmp_path):
    with goal("Ship the API", working_dir=tmp_path, backend=MockBackend(), session_name="example") as session:
        with patch(
            "juvenal.api._plan_workflow_internal",
            return_value=PlanResult(success=False, error="planner failed", temp_dir=None),
        ):
            with pytest.raises(JuvenalExecutionError):
                plan_and_do("Break the work into phases.", stage_id="plan-stage")

        owner_path = Path(session.stages["plan-stage"]["planner_owner_path"])
        planner_state_path = Path(session.stages["plan-stage"]["planner_state_path"])
        owner_path.unlink()
        planner_state_path.unlink()

    with goal("Ship the API", working_dir=tmp_path, backend=MockBackend(), session_name="example") as session:
        with patch("juvenal.api._plan_workflow_internal", side_effect=AssertionError("planner should not rerun")):
            with pytest.raises(JuvenalUsageError, match="owner in the manifest"):
                plan_and_do("Break the work into phases.", stage_id="other-plan-stage")

        assert set(session.stages) == {"plan-stage"}


def test_staged_plan_and_do_resumes_after_planner_complete_without_replanning(tmp_path):
    first_backend = MockBackend()
    first_backend.add_response(exit_code=1, output="planned run crashed")
    planned_yaml = "name: planned\nphases:\n  - id: execute\n    prompt: 'Execute the planned work.'\n"
    wrong_yaml = "name: wrong\nphases:\n  - id: wrong\n    prompt: 'Execute the wrong workflow.'\n"
    planning_calls: list[dict[str, object]] = []

    def fake_plan(**kwargs):
        planning_calls.append(kwargs)
        workflow_path = Path(kwargs["project_dir"]) / "workflow.yaml"
        workflow_path.write_text(planned_yaml)
        return PlanResult(success=True, workflow_yaml_path=str(workflow_path), temp_dir=None)

    with goal(
        "Ship the API",
        working_dir=tmp_path,
        backend=first_backend,
        max_bounces=1,
        session_name="example",
    ) as session:
        with patch("juvenal.api._plan_workflow_internal", side_effect=fake_plan):
            with pytest.raises(JuvenalExecutionError):
                plan_and_do("Break the work into phases.", stage_id="plan-stage")

        assert len(planning_calls) == 1
        assert session.stages["plan-stage"]["status"] == "planner_complete"
        owner_path = (tmp_path / ".plan" / "staged-plan-owner.json").resolve()
        assert owner_path.exists()
        owner_path.unlink()
        (tmp_path / "workflow.yaml").write_text(wrong_yaml)

    second_backend = MockBackend()
    second_backend.add_response(exit_code=0, output="planned work complete")

    with goal(
        "Ship the API",
        working_dir=tmp_path,
        backend=second_backend,
        max_bounces=1,
        session_name="example",
    ) as session:
        with patch("juvenal.api._plan_workflow_internal", side_effect=AssertionError("planner should not rerun")):
            plan_and_do("Break the work into phases.", stage_id="plan-stage")

        assert (tmp_path / ".plan" / "staged-plan-owner.json").exists()
        assert session.stages["plan-stage"]["run_id"] == "001"
        assert session.stages["plan-stage"]["status"] == "completed"
        assert session.history[-1]["kind"] == "plan_and_do"
        assert "Execute the planned work." in second_backend.calls[0]
        assert "Execute the wrong workflow." not in second_backend.calls[0]


def test_staged_plan_and_do_rejects_planner_asset_drift_before_resume(tmp_path):
    backend = MockBackend()

    with goal("Ship the API", working_dir=tmp_path, backend=backend, session_name="example") as session:
        with patch(
            "juvenal.api._plan_workflow_internal",
            return_value=PlanResult(success=False, error="planner failed", temp_dir=None),
        ):
            with pytest.raises(JuvenalExecutionError):
                plan_and_do("Break the work into phases.", stage_id="plan-stage")

        planner_assets_path = Path(session.stages["plan-stage"]["planner_assets_path"])
        assert session.stages["plan-stage"]["status"] == "running"
        assert planner_assets_path.exists()

    with goal("Ship the API", working_dir=tmp_path, backend=MockBackend(), session_name="example"):
        with patch(
            "juvenal.api._build_planner_assets_manifest",
            return_value={"digest": "changed", "files": [], "root": "planner"},
        ):
            with patch("juvenal.api._plan_workflow_internal", side_effect=AssertionError("planner should not rerun")):
                with pytest.raises(JuvenalExecutionError) as exc_info:
                    plan_and_do("Break the work into phases.", stage_id="plan-stage")

        assert exc_info.value.inspection_path == planner_assets_path.resolve()


def test_staged_plan_and_do_rejects_file_relative_yaml_and_rewinds_planner_state(tmp_path):
    backend = MockBackend()
    bad_yaml = (
        "name: planned\n"
        "include:\n"
        "  - shared.yaml\n"
        "phases:\n"
        "  - id: execute\n"
        "    prompt: 'Execute the planned work.'\n"
    )

    with goal("Ship the API", working_dir=tmp_path, backend=backend, session_name="example") as session:
        def fake_plan(**kwargs):
            workflow_path = Path(kwargs["project_dir"]) / "workflow.yaml"
            workflow_path.write_text(bad_yaml)
            return PlanResult(success=True, workflow_yaml_path=str(workflow_path), temp_dir=None)

        with patch("juvenal.api._plan_workflow_internal", side_effect=fake_plan):
            with pytest.raises(JuvenalExecutionError) as exc_info:
                plan_and_do("Break the work into phases.", stage_id="plan-stage")

        error = exc_info.value
        assert error.inspection_path == (tmp_path / "workflow.yaml").resolve()
        assert session.stages["plan-stage"]["status"] == "running"
        planner_state = json.loads(Path(session.stages["plan-stage"]["planner_state_path"]).read_text())
        assert planner_state["phases"]["write-workflow"]["status"] == "pending"
        assert "top-level include is not allowed" in planner_state["phases"]["write-workflow"][
            "failure_contexts"
        ][-1]["context"]
