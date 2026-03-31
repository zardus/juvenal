"""Shared fixtures for Juvenal tests."""

from __future__ import annotations

import shutil

import pytest

from juvenal.backends import AgentResult, Backend, InteractiveResult
from juvenal.workflow import Phase, Workflow


@pytest.fixture
def tmp_workflow(tmp_path):
    """Create a temporary workflow directory with phases.

    New convention:
    - Subdirectory with prompt.md and NO check- prefix -> implement
    - Subdirectory with prompt.md and check- prefix -> check
    """
    phases_dir = tmp_path / "phases"
    phases_dir.mkdir()

    # Phase 1: implement (setup)
    p1 = phases_dir / "01-setup"
    p1.mkdir()
    (p1 / "prompt.md").write_text("Set up the project.")

    # Phase 2: standalone check
    p2 = phases_dir / "02-check-build"
    p2.mkdir()
    (p2 / "prompt.md").write_text("Run the project's test/build checks.\nVERDICT: PASS or FAIL")

    # Phase 3: implement (feature)
    p3 = phases_dir / "03-implement"
    p3.mkdir()
    (p3 / "prompt.md").write_text("Implement the feature.")

    # Phase 4: check (review)
    p4 = phases_dir / "04-check-review"
    p4.mkdir()
    (p4 / "prompt.md").write_text("Review the implementation.\nVERDICT: PASS or FAIL")

    return tmp_path


@pytest.fixture
def sample_yaml(tmp_path):
    """Create a sample workflow YAML file with flat phases."""
    yaml_content = """\
name: test-workflow
backend: claude
working_dir: "."
max_bounces: 3

phases:
  - id: setup
    prompt: "Set up the project scaffolding."
  - id: setup-check
    type: check
    role: tester
  - id: implement
    prompt: "Implement the feature."
    bounce_target: setup
  - id: implement-script
    type: check
    prompt: "Run the project's required checks.\nVERDICT: PASS or FAIL"
  - id: implement-review
    type: check
    role: tester
"""
    yaml_path = tmp_path / "workflow.yaml"
    yaml_path.write_text(yaml_content)
    return yaml_path


@pytest.fixture
def bare_md(tmp_path):
    """Create a bare .md workflow file."""
    md_path = tmp_path / "task.md"
    md_path.write_text("Implement a hello world program.")
    return md_path


class MockBackend(Backend):
    """Mock backend for testing."""

    def __init__(self, responses: list[AgentResult] | None = None):
        super().__init__()
        self._responses = list(responses or [])
        self._interactive_responses: list[InteractiveResult] = []
        self._call_count = 0
        self.calls: list[str] = []
        self.resume_calls: list[tuple[str, str]] = []
        self.interactive_calls: list[str] = []

    def name(self) -> str:
        return "mock"

    def add_response(
        self,
        exit_code: int = 0,
        output: str = "",
        transcript: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        session_id: str | None = None,
    ):
        self._responses.append(
            AgentResult(
                exit_code=exit_code,
                output=output,
                transcript=transcript,
                duration=0.1,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                session_id=session_id,
            )
        )

    def run_agent(self, prompt, working_dir, display_callback=None, timeout=None, env=None):
        self.calls.append(prompt)
        if self._call_count < len(self._responses):
            result = self._responses[self._call_count]
        else:
            result = AgentResult(exit_code=0, output="VERDICT: PASS", transcript="", duration=0.1)
        self._call_count += 1
        return result

    def resume_agent(self, session_id, prompt, working_dir, display_callback=None, timeout=None, env=None):
        self.resume_calls.append((session_id, prompt))
        if self._call_count < len(self._responses):
            result = self._responses[self._call_count]
        else:
            result = AgentResult(exit_code=0, output="VERDICT: PASS", transcript="", duration=0.1)
        self._call_count += 1
        return result

    def add_interactive_response(self, exit_code: int = 0, session_id: str = "mock-session"):
        self._interactive_responses.append(InteractiveResult(session_id=session_id, exit_code=exit_code))

    def run_interactive(self, prompt, working_dir, env=None):
        self.interactive_calls.append(prompt)
        if self._interactive_responses:
            return self._interactive_responses.pop(0)
        return InteractiveResult(session_id="mock-session", exit_code=0)


@pytest.fixture
def mock_backend():
    return MockBackend()


@pytest.fixture
def simple_workflow():
    """A simple workflow with an implement phase and a check phase."""
    return Workflow(
        name="test",
        phases=[
            Phase(id="setup", type="implement", prompt="Do the thing."),
            Phase(id="setup-check", type="check", role="tester"),
        ],
        backend="claude",
        max_bounces=3,
    )


def claude_available():
    """Check if Claude CLI is available."""
    return shutil.which("claude") is not None


def codex_available():
    """Check if Codex CLI is available."""
    return shutil.which("npx") is not None
