"""E2E test: trivial workflow with Codex backend."""

import os
import subprocess

import pytest

from juvenal.engine import Engine
from juvenal.workflow import Phase, Workflow
from tests.conftest import codex_available


@pytest.mark.skipif(not codex_available(), reason="Codex CLI not available")
@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set")
def test_trivial_workflow_codex(tmp_path):
    """Run a trivial workflow that creates hello.txt."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    # Codex requires a git repo with user config
    subprocess.run(["git", "init"], cwd=work_dir, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=work_dir, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=work_dir, capture_output=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=work_dir, capture_output=True)

    workflow = Workflow(
        name="hello-test",
        phases=[
            Phase(
                id="create-hello",
                type="implement",
                prompt="Create a file called hello.txt containing exactly 'hello world' (no quotes). Do nothing else.",
            ),
            Phase(
                id="check-hello",
                type="script",
                run="test -f hello.txt && grep -q 'hello world' hello.txt",
            ),
        ],
        backend="codex",
        working_dir=str(work_dir),
        max_retries=3,
    )

    engine = Engine(workflow, state_file=str(tmp_path / "state.json"))
    result = engine.run()
    assert result == 0
    assert (work_dir / "hello.txt").exists()
