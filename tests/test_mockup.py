"""Tests for the sample embedded-API script."""

from __future__ import annotations

import re
import subprocess
from contextlib import contextmanager

import mockup


def test_mockup_main_initializes_repo_before_goal_and_uses_expected_prompts(tmp_path, monkeypatch):
    tmpdir = tmp_path / "sample-repo"
    tmpdir.mkdir()
    goal_calls: list[dict[str, object]] = []
    do_calls: list[dict[str, object]] = []
    plan_prompts: list[str] = []

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

    def fake_plan_and_do(prompt):
        plan_prompts.append(prompt)

    monkeypatch.setattr(mockup.tempfile, "mkdtemp", lambda: str(tmpdir))
    monkeypatch.setattr(mockup.juvenal, "goal", fake_goal)
    monkeypatch.setattr(mockup.juvenal, "do", fake_do)
    monkeypatch.setattr(mockup.juvenal, "plan_and_do", fake_plan_and_do)

    returned = mockup.main()

    assert returned == str(tmpdir)
    assert len(goal_calls) == 1
    assert mockup.LIBNAME in goal_calls[0]["description"]
    assert "Rust" in goal_calls[0]["description"]
    assert goal_calls[0]["working_dir"] == str(tmpdir)

    assert len(do_calls) == 3
    assert do_calls[0]["kwargs"] == {"checker": "pm"}
    assert do_calls[1]["kwargs"] == {"checkers": ["security-engineer"]}
    assert do_calls[2]["kwargs"] == {"checkers": ["tester", "senior-tester"]}

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

    assert len(plan_prompts) == 1
    final_prompt = plan_prompts[0]
    assert "relevant_cves.json" in final_prompt
    assert "commit to git" in final_prompt
    assert "previous implementor" in final_prompt
    assert "interoperability" in final_prompt

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
