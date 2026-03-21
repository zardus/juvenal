"""Tests for the sample embedded-API script."""

from __future__ import annotations

import re
import subprocess
from contextlib import contextmanager

import pytest

import mockup


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
