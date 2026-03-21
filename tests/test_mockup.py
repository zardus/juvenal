"""Tests for the sample embedded-API script."""

from __future__ import annotations

import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

import mockup
from juvenal.engine import PlanResult
from tests.conftest import MockBackend


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_mockup_main_initializes_repo_before_goal_and_uses_expected_prompts(tmp_path, monkeypatch):
    tmpdir = tmp_path / "sample-repo"
    tmpdir.mkdir()
    goal_calls: list[dict[str, object]] = []
    do_calls: list[dict[str, object]] = []
    plan_calls: list[dict[str, object]] = []

    @contextmanager
    def fake_goal(description, *, working_dir, **kwargs):
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=working_dir,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert len(head) == 40
        goal_calls.append({"description": description, "working_dir": working_dir, "kwargs": kwargs, "head": head})
        yield object()

    def fake_do(task_or_tasks, **kwargs):
        do_calls.append({"task_or_tasks": task_or_tasks, "kwargs": kwargs})

    def fake_plan_and_do(prompt, **kwargs):
        plan_calls.append({"prompt": prompt, "kwargs": kwargs})

    monkeypatch.setattr(mockup.tempfile, "mkdtemp", lambda: str(tmpdir))
    monkeypatch.setattr(mockup.juvenal, "goal", fake_goal)
    monkeypatch.setattr(mockup.juvenal, "do", fake_do)
    monkeypatch.setattr(mockup.juvenal, "plan_and_do", fake_plan_and_do)

    returned = mockup.main(session_name="stable-session", backend="claude", plain=True, max_bounces=17)

    assert returned == str(tmpdir)
    assert len(goal_calls) == 1
    assert mockup.LIBNAME in goal_calls[0]["description"]
    assert "Rust" in goal_calls[0]["description"]
    assert goal_calls[0]["working_dir"] == str(tmpdir)
    assert goal_calls[0]["kwargs"] == {
        "session_name": "stable-session",
        "backend": "claude",
        "plain": True,
        "max_bounces": 17,
    }

    assert len(do_calls) == 3
    assert do_calls[0]["kwargs"] == {"checker": "pm", "stage_id": mockup.PREPARE_ORIGINAL_STAGE_ID}
    assert do_calls[1]["kwargs"] == {
        "checkers": ["security-engineer"],
        "stage_id": mockup.RESEARCH_CVES_STAGE_ID,
    }
    assert do_calls[2]["kwargs"] == {
        "checkers": ["tester", "senior-tester"],
        "stage_id": mockup.PREPARE_TESTS_STAGE_ID,
    }

    cve_tasks = do_calls[1]["task_or_tasks"]
    assert isinstance(cve_tasks, list)
    assert len(cve_tasks) == 2
    assert f"{tmpdir}/all_cves.json" in cve_tasks[0]
    assert f"{tmpdir}/all_cves.json" in cve_tasks[1]
    assert f"{tmpdir}/relevant_cves.json" in cve_tasks[1]

    test_prep_tasks = do_calls[2]["task_or_tasks"]
    assert isinstance(test_prep_tasks, list)
    assert len(test_prep_tasks) == 4
    assert "repositories" in test_prep_tasks[2]
    assert "appropriate" in test_prep_tasks[3]

    assert len(plan_calls) == 1
    assert plan_calls[0]["kwargs"] == {"stage_id": mockup.PORT_LIBRARY_STAGE_ID}
    final_prompt = plan_calls[0]["prompt"]
    assert "relevant_cves.json" in final_prompt
    assert "commit to git" in final_prompt
    assert "previous implementor" in final_prompt
    assert "interoperability" in final_prompt
    assert "already exist in" in final_prompt
    assert "must be consumed as inputs" in final_prompt
    assert "updated in place" in final_prompt
    assert "generated workflow must be linear" in final_prompt
    assert "Every verifier must immediately follow the implement phase it verifies" in final_prompt

    all_prompts: list[str] = []
    for entry in do_calls:
        if isinstance(entry["task_or_tasks"], list):
            all_prompts.extend(entry["task_or_tasks"])
        else:
            all_prompts.append(entry["task_or_tasks"])
    combined_text = "\n".join(all_prompts + [final_prompt])
    assert set(re.findall(r"all_cves\.\w+", combined_text)) == {"all_cves.json"}
    assert set(re.findall(r"relevant_cves\.\w+", combined_text)) == {"relevant_cves.json"}
    assert "test cases" in combined_text
    assert "exported)" not in combined_text
    assert "No generated phase may redo source retrieval" in final_prompt
    assert "earlier test preparation" in final_prompt


def test_mockup_arg_parser_accepts_rerunnable_cli_options():
    args = mockup._build_arg_parser().parse_args(
        [
            "--working-dir",
            "/tmp/mockup-workspace",
            "--session-name",
            "stable-session",
            "--backend",
            "claude",
            "--plain",
            "--max-bounces",
            "17",
        ]
    )

    assert args.working_dir == "/tmp/mockup-workspace"
    assert args.session_name == "stable-session"
    assert args.backend == "claude"
    assert args.plain is True
    assert args.max_bounces == 17


def test_ensure_repo_initialized_creates_baseline_commit_and_default_identity_when_missing(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home / ".config"))

    mockup.ensure_repo_initialized(
        workspace,
        goal_text=mockup.GOAL_TEXT,
        session_name="stable-session",
        backend="codex",
        max_bounces=7,
    )

    head = _git(workspace, "rev-parse", "HEAD")
    assert len(head) == 40
    assert _git(workspace, "config", "--get", "user.email") == mockup.DEFAULT_GIT_EMAIL
    assert _git(workspace, "config", "--get", "user.name") == mockup.DEFAULT_GIT_NAME


def test_ensure_repo_initialized_preserves_existing_git_identity(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "config", "user.email", "custom@example.com")
    _git(workspace, "config", "user.name", "Custom Name")

    mockup.ensure_repo_initialized(
        workspace,
        goal_text=mockup.GOAL_TEXT,
        session_name="stable-session",
        backend="codex",
        max_bounces=7,
    )

    assert _git(workspace, "config", "--get", "user.email") == "custom@example.com"
    assert _git(workspace, "config", "--get", "user.name") == "Custom Name"


def test_ensure_repo_initialized_rejects_dirty_tree_without_head(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init")
    (workspace / "scratch.txt").write_text("dirty\n")

    with pytest.raises(RuntimeError, match="cannot create the baseline commit with a dirty tree"):
        mockup.ensure_repo_initialized(
            workspace,
            goal_text=mockup.GOAL_TEXT,
            session_name="stable-session",
            backend="codex",
            max_bounces=7,
        )


def test_ensure_repo_initialized_rejects_subdirectory_of_existing_repo(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(repo_root, "init")
    child = repo_root / "child"
    child.mkdir()

    with pytest.raises(RuntimeError, match="must be the git repo root"):
        mockup.ensure_repo_initialized(
            child,
            goal_text=mockup.GOAL_TEXT,
            session_name="stable-session",
            backend="codex",
            max_bounces=7,
        )


def test_ensure_repo_initialized_rejects_dirty_tree_before_named_session_exists(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    mockup.ensure_repo_initialized(
        workspace,
        goal_text=mockup.GOAL_TEXT,
        session_name="stable-session",
        backend="codex",
        max_bounces=7,
    )

    (workspace / "scratch.txt").write_text("dirty\n")

    with pytest.raises(RuntimeError, match="must be clean before creating a new named session manifest"):
        mockup.ensure_repo_initialized(
            workspace,
            goal_text=mockup.GOAL_TEXT,
            session_name="stable-session",
            backend="codex",
            max_bounces=7,
        )


def test_main_allows_dirty_rerun_when_manifest_identity_matches_even_if_plain_changes(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    mockup.ensure_repo_initialized(
        workspace,
        goal_text=mockup.GOAL_TEXT,
        session_name="stable-session",
        backend="codex",
        max_bounces=7,
    )
    with mockup.juvenal.goal(
        mockup.GOAL_TEXT,
        working_dir=workspace,
        backend="codex",
        session_name="stable-session",
        max_bounces=7,
    ):
        pass

    (workspace / "scratch.txt").write_text("dirty\n")
    goal_calls: list[dict[str, object]] = []

    @contextmanager
    def fake_goal(description, *, working_dir, **kwargs):
        goal_calls.append({"description": description, "working_dir": working_dir, "kwargs": kwargs})
        yield object()

    monkeypatch.setattr(mockup.juvenal, "goal", fake_goal)
    monkeypatch.setattr(mockup.juvenal, "do", lambda *args, **kwargs: None)
    monkeypatch.setattr(mockup.juvenal, "plan_and_do", lambda *args, **kwargs: None)

    returned = mockup.main(
        working_dir=workspace,
        session_name="stable-session",
        backend="codex",
        plain=True,
        max_bounces=7,
    )

    assert returned == str(workspace.resolve())
    assert goal_calls == [
        {
            "description": mockup.GOAL_TEXT,
            "working_dir": str(workspace.resolve()),
            "kwargs": {
                "session_name": "stable-session",
                "backend": "codex",
                "plain": True,
                "max_bounces": 7,
            },
        }
    ]


def test_ensure_repo_initialized_rejects_dirty_rerun_when_manifest_identity_mismatches(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    mockup.ensure_repo_initialized(
        workspace,
        goal_text=mockup.GOAL_TEXT,
        session_name="stable-session",
        backend="codex",
        max_bounces=7,
    )
    with mockup.juvenal.goal(
        mockup.GOAL_TEXT,
        working_dir=workspace,
        backend="codex",
        session_name="stable-session",
        max_bounces=7,
    ):
        pass

    (workspace / "scratch.txt").write_text("dirty\n")

    with pytest.raises(RuntimeError, match="identity mismatch in goal_text"):
        mockup.ensure_repo_initialized(
            workspace,
            goal_text="port libzstd from C to Zig",
            session_name="stable-session",
            backend="codex",
            max_bounces=7,
        )


def test_mockup_main_two_run_smoke_reuses_repo_and_skips_completed_top_level_stages(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    planning_calls: list[dict[str, object]] = []

    def fake_plan(**kwargs):
        planning_calls.append(kwargs)
        plan_dir = Path(kwargs["project_dir"]) / ".plan"
        plan_dir.mkdir(exist_ok=True)
        (plan_dir / "workflow-structure.yaml").write_text(
            """\
linear: true
yaml_source_mode: inline-only
verifier_encoding: explicit-phases
phases: []
""",
            encoding="utf-8",
        )
        workflow_path = Path(kwargs["project_dir"]) / "workflow.yaml"
        workflow_path.write_text("name: planned\nphases: []\n", encoding="utf-8")
        return PlanResult(success=True, workflow_yaml_path=str(workflow_path), temp_dir=None)

    first_backend = MockBackend()
    second_backend = MockBackend()

    with patch.object(mockup.juvenal, "_plan_workflow_internal", side_effect=fake_plan):
        first_returned = mockup.main(
            working_dir=workspace,
            session_name="stable-session",
            backend=first_backend,
            max_bounces=7,
        )

    manifest_path = workspace / ".juvenal-api" / "stable-session" / "session.json"
    assert manifest_path.exists()
    first_head = _git(workspace, "rev-parse", "HEAD")
    assert len(first_backend.calls) > 0
    assert len(planning_calls) == 1

    with patch.object(mockup.juvenal, "_plan_workflow_internal", side_effect=fake_plan):
        second_returned = mockup.main(
            working_dir=workspace,
            session_name="stable-session",
            backend=second_backend,
            max_bounces=7,
        )

    assert first_returned == str(workspace.resolve())
    assert second_returned == str(workspace.resolve())
    assert _git(workspace, "rev-parse", "HEAD") == first_head
    assert second_backend.calls == []
    assert len(planning_calls) == 1
