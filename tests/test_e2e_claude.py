"""E2E test: trivial workflow with Claude backend."""

import pytest

from juvenal.engine import Engine
from juvenal.workflow import Phase, Workflow, make_command_check_prompt
from tests.conftest import claude_available


@pytest.mark.skipif(not claude_available(), reason="Claude CLI not available")
def test_trivial_workflow_claude(tmp_path):
    """Run a trivial workflow that creates hello.txt."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()

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
                type="check",
                prompt=make_command_check_prompt("test -f hello.txt && grep -q 'hello world' hello.txt"),
            ),
        ],
        backend="claude",
        working_dir=str(work_dir),
        max_bounces=3,
    )

    engine = Engine(workflow, state_file=str(tmp_path / "state.json"))
    result = engine.run()
    assert result == 0
    assert (work_dir / "hello.txt").exists()
