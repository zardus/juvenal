"""Tests for the toy embedded-API example module."""

from __future__ import annotations

from contextlib import contextmanager

from tests import mockup


def test_mockup_main_initializes_repo_before_goal_and_uses_expected_prompts(tmp_path, monkeypatch):
    tmpdir = tmp_path / "toy-example-repo"
    tmpdir.mkdir()
    goal_calls: list[dict[str, object]] = []
    do_calls: list[dict[str, object]] = []
    plan_calls: list[dict[str, object]] = []

    @contextmanager
    def fake_goal(description, *, working_dir, **kwargs):
        goal_calls.append({"description": description, "working_dir": working_dir, "kwargs": kwargs})
        yield object()

    def fake_do(task_or_tasks, **kwargs):
        do_calls.append({"task_or_tasks": task_or_tasks, "kwargs": kwargs})

    def fake_plan_and_do(prompt, **kwargs):
        plan_calls.append({"prompt": prompt, "kwargs": kwargs})

    monkeypatch.setattr(mockup.tempfile, "mkdtemp", lambda: str(tmpdir))
    monkeypatch.setattr(mockup.juvenal, "goal", fake_goal)
    monkeypatch.setattr(mockup.juvenal, "do", fake_do)
    monkeypatch.setattr(mockup.juvenal, "plan_and_do", fake_plan_and_do)

    returned = mockup.main()

    assert returned == str(tmpdir)
    assert len(goal_calls) == 1
    assert "toy todo CLI" in goal_calls[0]["description"]
    assert goal_calls[0]["working_dir"] == str(tmpdir)

    assert len(do_calls) == 3
    assert do_calls[0]["kwargs"] == {"checker": "pm"}
    assert do_calls[1]["kwargs"] == {"checkers": ["security-engineer"]}
    assert do_calls[2]["kwargs"] == {"checkers": ["tester", "senior-tester"]}
    assert [len(entry["task_or_tasks"]) if isinstance(entry["task_or_tasks"], list) else 1 for entry in do_calls] == [
        1,
        2,
        4,
    ]

    # first do: example brief
    example_brief_task = do_calls[0]["task_or_tasks"]
    assert isinstance(example_brief_task, str)
    assert f"{tmpdir}/example-brief.md" in example_brief_task
    assert "toy-todo" in example_brief_task
    assert "add" in example_brief_task
    assert "list" in example_brief_task
    assert "done" in example_brief_task
    assert "remove" in example_brief_task

    # second do: sample interactions
    sample_interaction_tasks = do_calls[1]["task_or_tasks"]
    assert isinstance(sample_interaction_tasks, list)
    assert len(sample_interaction_tasks) == 2
    assert f"{tmpdir}/sample-interactions.md" in sample_interaction_tasks[0]
    assert "edge-case" in sample_interaction_tasks[1]

    # third do: acceptance assets
    acceptance_asset_tasks = do_calls[2]["task_or_tasks"]
    assert isinstance(acceptance_asset_tasks, list)
    assert len(acceptance_asset_tasks) == 4
    assert f"{tmpdir}/acceptance-checklist.md" in acceptance_asset_tasks[0]
    assert f"{tmpdir}/smoke-test.sh" in acceptance_asset_tasks[3]

    # plan_and_do
    assert len(plan_calls) == 1
    final_prompt = plan_calls[0]["prompt"]
    assert "toy-todo" in final_prompt
    assert f"{tmpdir}/toy_app" in final_prompt
    assert "commit to git" in final_prompt
    assert "previous implementor" in final_prompt
    assert "already exist in" in final_prompt
    normalized = " ".join(final_prompt.split())
    assert "No generated phase may recreate those prep artifacts" in normalized
    assert "generated workflow must be linear" in final_prompt

    # no leftover libzstd references
    all_prompts: list[str] = []
    for entry in do_calls:
        if isinstance(entry["task_or_tasks"], list):
            all_prompts.extend(entry["task_or_tasks"])
        else:
            all_prompts.append(entry["task_or_tasks"])
    combined_text = "\n".join(all_prompts + [final_prompt])
    assert "libzstd" not in combined_text
    assert "Rust" not in combined_text
