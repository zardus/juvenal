"""Unit tests for the execution engine with mocked backend."""

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from juvenal.checkers import parse_verdict
from juvenal.engine import BounceCounter, Engine, _extract_yaml, _plan_workflow_internal
from juvenal.workflow import ParallelGroup, Phase, Workflow, expand_multi_vars, inject_checkers, inject_implementer
from tests.conftest import MockBackend


class TestVerdictParsing:
    def test_pass(self):
        passed, reason, target = parse_verdict("some output\nVERDICT: PASS")
        assert passed
        assert reason == ""
        assert target is None

    def test_fail_with_reason(self):
        passed, reason, target = parse_verdict("output\nVERDICT: FAIL: tests broken")
        assert not passed
        assert reason == "tests broken"
        assert target is None

    def test_fail_without_reason(self):
        passed, reason, target = parse_verdict("VERDICT: FAIL")
        assert not passed
        assert reason == "unspecified"
        assert target is None

    def test_no_verdict(self):
        passed, reason, target = parse_verdict("no verdict here")
        assert not passed
        assert "did not emit a VERDICT" in reason
        assert target is None

    def test_verdict_scan_backwards(self):
        """Should find the last VERDICT line."""
        output = "VERDICT: FAIL: old\nmore stuff\nVERDICT: PASS"
        passed, reason, target = parse_verdict(output)
        assert passed
        assert reason == ""
        assert target is None

    def test_fail_with_bounce_target(self):
        """VERDICT: FAIL(target-id): reason extracts both target and reason."""
        passed, reason, target = parse_verdict("review\nVERDICT: FAIL(design-experiments): needs more data")
        assert not passed
        assert reason == "needs more data"
        assert target == "design-experiments"

    def test_fail_with_bounce_target_no_reason(self):
        """VERDICT: FAIL(target-id): with empty reason."""
        passed, reason, target = parse_verdict("VERDICT: FAIL(write-paper):")
        assert not passed
        assert reason == "unspecified"
        assert target == "write-paper"

    def test_fail_with_bounce_target_scan_backwards(self):
        """Should find the last VERDICT line even with targeted fail."""
        output = "VERDICT: FAIL: old reason\nmore output\nVERDICT: FAIL(phase-b): new reason"
        passed, reason, target = parse_verdict(output)
        assert not passed
        assert reason == "new reason"
        assert target == "phase-b"


class TestEngineWithMockedBackend:
    def _make_engine(self, workflow, backend, tmp_path, **kwargs):
        """Create an engine with injected mock backend."""
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), **kwargs)
        engine.backend = backend
        return engine

    def test_single_phase_pass(self, tmp_path):
        """Implement phase followed by a script phase, both pass."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")  # implement
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-check", type="script", run="true"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

    def test_implementation_crash_bounces(self, tmp_path):
        """Implement crash bounces back to self, retried on next visit."""
        backend = MockBackend()
        backend.add_response(exit_code=1, output="crash")  # attempt 1 crashes -> bounce
        backend.add_response(exit_code=0, output="done")  # attempt 2 succeeds
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-check", type="script", run="true"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

    def test_script_checker_failure_bounces(self, tmp_path):
        """Script failure bounces back; global bounce counter exhausts."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")  # implement attempt 1
        # Script fails -> bounce 1 -> back to implement
        backend.add_response(exit_code=0, output="done")  # implement attempt 2
        # Script fails -> bounce 2 -> exhausted (max_bounces=2)
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-check", type="script", run="false"),  # always fails
            ],
            max_bounces=2,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 1  # exhausted

    def test_agent_checker_pass(self, tmp_path):
        """Implement + check phase, check passes."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="implemented")  # implement
        backend.add_response(exit_code=0, output="looks good\nVERDICT: PASS")  # check
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-review", type="check", role="tester"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

    def test_agent_checker_fail_bounces(self, tmp_path):
        """Check failure bounces back to most recent implement phase."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="implemented")  # implement attempt 1
        backend.add_response(exit_code=0, output="VERDICT: FAIL: bad code")  # check fails -> bounce 1
        backend.add_response(exit_code=0, output="fixed")  # implement attempt 2
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # check passes
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-review", type="check", role="tester"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

    def test_multi_phase(self, tmp_path):
        backend = MockBackend()
        # Phase 1: implement passes
        backend.add_response(exit_code=0, output="phase1 done")
        # Phase 2: implement passes
        backend.add_response(exit_code=0, output="phase2 done")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="phase1", type="implement", prompt="Do phase 1."),
                Phase(id="phase1-check", type="script", run="true"),
                Phase(id="phase2", type="implement", prompt="Do phase 2."),
                Phase(id="phase2-check", type="script", run="true"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

    def test_global_bounce_exhaustion_across_phases(self, tmp_path):
        """Global bounce counter accumulates across different phases."""
        backend = MockBackend()
        # Phase 1 implement: pass
        backend.add_response(exit_code=0, output="phase1 done")
        # Phase 1 check: fail -> bounce 1
        backend.add_response(exit_code=0, output="VERDICT: FAIL: not good")
        # Phase 1 implement again: pass
        backend.add_response(exit_code=0, output="phase1 fixed")
        # Phase 1 check: pass
        backend.add_response(exit_code=0, output="VERDICT: PASS")
        # Phase 2 implement: crash -> bounce 2 -> exhausted (max_bounces=2)
        backend.add_response(exit_code=1, output="crash")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="phase1", type="implement", prompt="Do phase 1."),
                Phase(id="phase1-review", type="check", role="tester"),
                Phase(id="phase2", type="implement", prompt="Do phase 2."),
            ],
            max_bounces=2,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 1  # exhausted

    def test_per_phase_bounce_target(self, tmp_path):
        """Phase-level bounce_target directs bounce to a specific phase."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="setup done")  # setup implement
        backend.add_response(exit_code=0, output="feature done")  # feature implement
        backend.add_response(exit_code=0, output="VERDICT: FAIL: bad")  # feature check -> bounce to setup
        backend.add_response(exit_code=0, output="setup redone")  # setup implement again
        backend.add_response(exit_code=0, output="feature redone")  # feature implement again
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # feature check passes
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Set up."),
                Phase(id="feature", type="implement", prompt="Build feature."),
                Phase(id="feature-review", type="check", role="tester", bounce_target="setup"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

    def test_agent_guided_bounce_valid_target(self, tmp_path):
        """Check with bounce_targets: agent picks a valid target via VERDICT: FAIL(target)."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="setup done")  # setup implement
        backend.add_response(exit_code=0, output="experiments designed")  # design-experiments implement
        backend.add_response(exit_code=0, output="paper written")  # write-paper implement
        # Review picks design-experiments as bounce target
        backend.add_response(exit_code=0, output="VERDICT: FAIL(design-experiments): needs more data")
        # Re-run from design-experiments
        backend.add_response(exit_code=0, output="experiments redesigned")  # design-experiments again
        backend.add_response(exit_code=0, output="paper rewritten")  # write-paper again
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # review passes
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Set up."),
                Phase(id="design-experiments", type="implement", prompt="Design experiments."),
                Phase(id="write-paper", type="implement", prompt="Write paper."),
                Phase(
                    id="review",
                    type="check",
                    role="tester",
                    bounce_targets=["design-experiments", "write-paper"],
                ),
            ],
            max_bounces=5,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

    def test_agent_guided_bounce_invalid_target_falls_back(self, tmp_path):
        """Agent picks a target not in bounce_targets — falls back to first in list."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="phase-a done")  # phase-a
        backend.add_response(exit_code=0, output="phase-b done")  # phase-b
        # Review specifies invalid target -> falls back to phase-a (first in list)
        backend.add_response(exit_code=0, output="VERDICT: FAIL(nonexistent): bad")
        backend.add_response(exit_code=0, output="phase-a redone")  # phase-a again
        backend.add_response(exit_code=0, output="phase-b redone")  # phase-b again
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # review passes
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="phase-a", type="implement", prompt="Do A."),
                Phase(id="phase-b", type="implement", prompt="Do B."),
                Phase(
                    id="review",
                    type="check",
                    role="tester",
                    bounce_targets=["phase-a", "phase-b"],
                ),
            ],
            max_bounces=5,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

    def test_agent_guided_bounce_no_target_in_verdict(self, tmp_path):
        """Agent emits VERDICT: FAIL without a target — falls back to first in bounce_targets."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="phase-a done")  # phase-a
        backend.add_response(exit_code=0, output="phase-b done")  # phase-b
        # Review fails without specifying target -> falls back to phase-a (first in list)
        backend.add_response(exit_code=0, output="VERDICT: FAIL: something wrong")
        backend.add_response(exit_code=0, output="phase-a redone")  # phase-a again
        backend.add_response(exit_code=0, output="phase-b redone")  # phase-b again
        backend.add_response(exit_code=0, output="VERDICT: PASS")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="phase-a", type="implement", prompt="Do A."),
                Phase(id="phase-b", type="implement", prompt="Do B."),
                Phase(
                    id="review",
                    type="check",
                    role="tester",
                    bounce_targets=["phase-a", "phase-b"],
                ),
            ],
            max_bounces=5,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

    def test_dry_run(self, tmp_path, capsys):
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do the thing."),
                Phase(id="setup-check", type="script", run="true"),
            ],
        )
        engine = self._make_engine(workflow, MockBackend(), tmp_path, dry_run=True)
        assert engine.run() == 0
        captured = capsys.readouterr()
        assert "test" in captured.out
        assert "setup" in captured.out
        assert "implement" in captured.out
        assert "script" in captured.out

    def test_run_summary_on_success(self, tmp_path, capsys):
        """Successful run prints a summary with phase info and bounce count."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-check", type="script", run="true"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path, plain=True)
        assert engine.run() == 0
        captured = capsys.readouterr()
        assert "Run Summary" in captured.out
        assert "setup" in captured.out
        assert "Total bounces: 0" in captured.out

    def test_run_summary_on_failure(self, tmp_path, capsys):
        """Failed run also prints a summary with bounce count."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")
        # Script always fails
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-check", type="script", run="false"),
            ],
            max_bounces=1,
        )
        engine = self._make_engine(workflow, backend, tmp_path, plain=True)
        assert engine.run() == 1
        captured = capsys.readouterr()
        assert "Run Summary" in captured.out
        assert "Total bounces: 1" in captured.out

    def test_plain_mode_shows_live_output(self, tmp_path, capsys):
        """Plain mode prints agent output lines inline."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-check", type="script", run="true"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path, plain=True)
        # Simulate live output
        engine.display.live_update("building module X")
        engine.display.live_update("running tests")
        captured = capsys.readouterr()
        assert "building module X" in captured.out
        assert "running tests" in captured.out

    def test_rewind_n_phases(self, tmp_path):
        """--rewind N goes back N phases from the resume point and invalidates."""
        backend = MockBackend()
        # First run: complete phase1 and phase2, fail phase3
        backend.add_response(exit_code=0, output="phase1 done")
        backend.add_response(exit_code=0, output="phase2 done")
        backend.add_response(exit_code=1, output="crash")  # phase3 fails -> exhausted
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="phase1", type="implement", prompt="Do phase 1."),
                Phase(id="phase2", type="implement", prompt="Do phase 2."),
                Phase(id="phase3", type="implement", prompt="Do phase 3."),
            ],
            max_bounces=1,
        )
        state_file = str(tmp_path / "state.json")
        engine = self._make_engine(workflow, backend, tmp_path)
        engine.run()
        # phase1 and phase2 completed, phase3 failed
        assert engine.state.phases["phase1"].status == "completed"
        assert engine.state.phases["phase2"].status == "completed"

        # Now rewind 2 from resume point (phase3, idx=2) -> starts at phase1 (idx=0)
        backend2 = MockBackend()
        backend2.add_response(exit_code=0, output="phase1 redone")
        backend2.add_response(exit_code=0, output="phase2 redone")
        backend2.add_response(exit_code=0, output="phase3 done")
        engine2 = Engine(workflow, rewind=2, state_file=state_file)
        engine2.backend = backend2
        assert engine2._start_idx == 0
        assert engine2.run() == 0

    def test_rewind_to_phase(self, tmp_path):
        """--rewind-to PHASE_ID starts from that phase and invalidates it onward."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="phase1 done")
        backend.add_response(exit_code=0, output="phase2 done")
        backend.add_response(exit_code=1, output="crash")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="phase1", type="implement", prompt="Do phase 1."),
                Phase(id="phase2", type="implement", prompt="Do phase 2."),
                Phase(id="phase3", type="implement", prompt="Do phase 3."),
            ],
            max_bounces=1,
        )
        state_file = str(tmp_path / "state.json")
        engine = self._make_engine(workflow, backend, tmp_path)
        engine.run()

        # Rewind to phase2
        backend2 = MockBackend()
        backend2.add_response(exit_code=0, output="phase2 redone")
        backend2.add_response(exit_code=0, output="phase3 done")
        engine2 = Engine(workflow, rewind_to="phase2", state_file=state_file)
        engine2.backend = backend2
        assert engine2._start_idx == 1
        # phase1 should still be completed, phase2 invalidated
        assert engine2.state.phases["phase1"].status == "completed"
        assert engine2.state.phases["phase2"].status == "pending"
        assert engine2.run() == 0

    def test_rewind_clamps_to_zero(self, tmp_path):
        """--rewind N larger than current position clamps to phase 0."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="phase1 done")
        backend.add_response(exit_code=1, output="crash")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="phase1", type="implement", prompt="Do phase 1."),
                Phase(id="phase2", type="implement", prompt="Do phase 2."),
            ],
            max_bounces=1,
        )
        state_file = str(tmp_path / "state.json")
        engine = self._make_engine(workflow, backend, tmp_path)
        engine.run()

        # Rewind 100 from resume point (phase2, idx=1) -> clamps to 0
        backend2 = MockBackend()
        backend2.add_response(exit_code=0, output="phase1 redone")
        backend2.add_response(exit_code=0, output="phase2 done")
        engine2 = Engine(workflow, rewind=100, state_file=state_file)
        engine2.backend = backend2
        assert engine2._start_idx == 0
        assert engine2.run() == 0

    def test_timeout_on_phase(self, tmp_path):
        """Phase timeout field is stored correctly."""
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it.", timeout=60),
            ],
        )
        assert workflow.phases[0].timeout == 60


class TestPreserveContextOnBounce:
    def _make_engine(self, workflow, backend, tmp_path, **kwargs):
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), **kwargs)
        engine.backend = backend
        return engine

    def test_bounce_uses_resume_agent(self, tmp_path):
        """By default, bouncing back resumes the session."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="built it", session_id="sess-1")  # implement attempt 1
        backend.add_response(exit_code=0, output="VERDICT: FAIL: bad code")  # check fails -> bounce
        backend.add_response(exit_code=0, output="fixed it", session_id="sess-1")  # implement attempt 2 (resumed)
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # check passes
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="Build it."),
                Phase(id="review", type="check", role="tester"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

        # First call is run_agent (fresh), second implement call should be resume_agent
        assert len(backend.calls) == 3  # implement + check + check (2nd)
        assert len(backend.resume_calls) == 1
        session_id, prompt = backend.resume_calls[0]
        assert session_id == "sess-1"
        assert "failed verification" in prompt

    def test_bounce_with_clear_context_uses_run_agent(self, tmp_path):
        """With clear_context_on_bounce, bouncing back starts fresh."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="built it", session_id="sess-1")  # implement attempt 1
        backend.add_response(exit_code=0, output="VERDICT: FAIL: bad code")  # check fails -> bounce
        backend.add_response(exit_code=0, output="fixed it")  # implement attempt 2 (fresh)
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # check passes
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="Build it."),
                Phase(id="review", type="check", role="tester"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path, clear_context_on_bounce=True)
        assert engine.run() == 0

        # All calls should be run_agent, no resume
        assert len(backend.calls) == 4
        assert len(backend.resume_calls) == 0

    def test_forward_after_bounce_starts_fresh(self, tmp_path):
        """a->b->c(bounces to a): (a) resumes, (b) and (c) re-run with fresh context.

        Flow: a(run) -> b(run) -> c(run, FAIL) -> a(resume) -> b(run) -> c(run, PASS)
        Verifies:
        1. (a) resumes with previous context after bounce
        2. (b) restarts with fresh context on forward re-run
        3. (c) restarts with fresh context on forward re-run
        """
        backend = MockBackend()
        # Round 1
        backend.add_response(exit_code=0, output="a done", session_id="sess-a")  # a: run_agent
        backend.add_response(exit_code=0, output="b done", session_id="sess-b")  # b: run_agent
        backend.add_response(exit_code=0, output="VERDICT: FAIL: nope", session_id="sess-c")  # c: run_agent, fails
        # Round 2 (after bounce to a)
        backend.add_response(exit_code=0, output="a fixed", session_id="sess-a2")  # a: resume_agent
        backend.add_response(exit_code=0, output="b redone", session_id="sess-b2")  # b: run_agent (fresh)
        backend.add_response(exit_code=0, output="VERDICT: PASS", session_id="sess-c2")  # c: run_agent (fresh)
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="Do a."),
                Phase(id="b", type="implement", prompt="Do b."),
                Phase(id="c", type="check", role="tester", bounce_target="a"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

        # Point 1: (a) was resumed exactly once with the right session
        assert len(backend.resume_calls) == 1
        assert backend.resume_calls[0][0] == "sess-a"
        assert "failed verification" in backend.resume_calls[0][1]

        # Points 2 & 3: (b) and (c) were always run_agent (fresh), never resumed
        # run_agent calls: a(1) + b(1) + c(1) + b(2) + c(2) = 5
        assert len(backend.calls) == 5

    def test_bounce_without_session_id_falls_back_to_fresh(self, tmp_path):
        """If no session_id was captured, fall back to run_agent even with the flag."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="built it")  # no session_id
        backend.add_response(exit_code=0, output="VERDICT: FAIL: bad")  # check fails -> bounce
        backend.add_response(exit_code=0, output="fixed it")  # implement attempt 2 (fresh, no session to resume)
        backend.add_response(exit_code=0, output="VERDICT: PASS")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="Build it."),
                Phase(id="review", type="check", role="tester"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

        # No session_id means no resume, all run_agent
        assert len(backend.calls) == 4
        assert len(backend.resume_calls) == 0

    def test_script_bounce_uses_resume(self, tmp_path):
        """Script failure bounces back and resumes the implement session."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="built it", session_id="sess-1")  # implement attempt 1
        # script "false" fails -> bounce
        backend.add_response(exit_code=0, output="fixed it", session_id="sess-2")  # implement attempt 2 (resumed)
        # script "true" would pass — use a workflow where it passes on second try
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="Build it."),
                Phase(id="build-check", type="script", run="false", bounce_target="build"),
            ],
            max_bounces=2,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        # Will exhaust bounces since "false" always fails, but we check resume was used
        engine.run()

        assert len(backend.resume_calls) == 1
        assert backend.resume_calls[0][0] == "sess-1"

    def test_multi_var_script_bounce_uses_rendered_command_in_failure_context(self, tmp_path):
        """Expanded script phases should report the rendered command on bounce."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="built it", session_id="sess-1")  # implement attempt 1
        backend.add_response(exit_code=0, output="fixed it", session_id="sess-2")  # implement attempt 2 (resumed)
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="Build {{TARGET}}."),
                Phase(id="build~script-1", type="script", run="pytest {{TARGET}} -x", bounce_target="build"),
            ],
            max_bounces=2,
        )
        workflow = expand_multi_vars(workflow, {"TARGET": ["linux"]})
        engine = self._make_engine(workflow, backend, tmp_path)
        with patch("juvenal.engine.run_script") as mock_run:
            mock_run.return_value = type("R", (), {"exit_code": 1, "output": "boom"})()
            assert engine.run() == 1

        assert len(backend.resume_calls) == 2
        assert backend.resume_calls[0][0] == "sess-1"
        assert all("pytest linux -x" in prompt for _, prompt in backend.resume_calls)
        assert all("{{TARGET}}" not in prompt for _, prompt in backend.resume_calls)

    def test_crash_bounce_to_self_uses_resume(self, tmp_path):
        """Implement crash bouncing to self resumes the session."""
        backend = MockBackend()
        backend.add_response(exit_code=1, output="crash", session_id="sess-1")  # attempt 1 crashes
        backend.add_response(exit_code=0, output="done")  # attempt 2 (resumed)
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="check", type="script", run="true"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

        assert len(backend.resume_calls) == 1
        assert backend.resume_calls[0][0] == "sess-1"


class TestWorkflowPhase:
    def _make_engine(self, workflow, backend, tmp_path, **kwargs):
        """Create an engine with injected mock backend."""
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), **kwargs)
        engine.backend = backend
        return engine

    def test_workflow_phase_success(self, tmp_path):
        """Workflow phase succeeds when planning and execution both succeed."""
        from unittest.mock import patch

        from juvenal.engine import PlanResult

        backend = MockBackend()
        workflow = Workflow(
            name="test",
            phases=[Phase(id="dynamic", type="workflow", prompt="Build a thing.")],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)

        # Create a sub-workflow YAML for the sub-engine to load
        sub_yaml = tmp_path / "sub" / "workflow.yaml"
        sub_yaml.parent.mkdir(parents=True)
        sub_yaml.write_text("name: sub\nphases:\n  - id: step1\n    prompt: do it\n")

        plan_result = PlanResult(
            success=True,
            workflow_yaml_path=str(sub_yaml),
            temp_dir=str(sub_yaml.parent),
            input_tokens=100,
            output_tokens=200,
        )

        # Mock _plan_workflow_internal and the sub-engine run
        with patch("juvenal.engine._plan_workflow_internal", return_value=plan_result):
            # The sub-engine will use MockBackend's default response (VERDICT: PASS)
            result = engine.run()

        assert result == 0

    def test_workflow_phase_planning_failure_bounces(self, tmp_path):
        """When sub-workflow planning fails, the phase bounces."""
        from unittest.mock import patch

        from juvenal.engine import PlanResult

        backend = MockBackend()
        # First attempt: planning fails -> bounce back to self
        # Second attempt: also planning fails -> bounce 2 -> exhausted
        plan_fail = PlanResult(
            success=False,
            temp_dir=str(tmp_path / "plan-tmp"),
            error="Planning engine returned non-zero",
            input_tokens=50,
            output_tokens=60,
        )
        workflow = Workflow(
            name="test",
            phases=[Phase(id="dynamic", type="workflow", prompt="Build a thing.")],
            max_bounces=2,
        )
        engine = self._make_engine(workflow, backend, tmp_path)

        with patch("juvenal.engine._plan_workflow_internal", return_value=plan_fail):
            result = engine.run()

        assert result == 1  # exhausted bounces

    def test_workflow_phase_execution_failure_bounces(self, tmp_path):
        """When sub-workflow execution fails, the phase bounces."""
        from unittest.mock import patch

        from juvenal.engine import PlanResult

        backend = MockBackend()
        # Sub-engine will run implement phase that crashes
        backend.add_response(exit_code=1, output="crash")  # sub-engine implement fails

        sub_yaml = tmp_path / "sub" / "workflow.yaml"
        sub_yaml.parent.mkdir(parents=True)
        # max_bounces=1 so the crash exhausts the sub-workflow
        sub_yaml.write_text("name: sub\nmax_bounces: 1\nphases:\n  - id: step1\n    prompt: do it\n")

        plan_result = PlanResult(
            success=True,
            workflow_yaml_path=str(sub_yaml),
            temp_dir=str(sub_yaml.parent),
            input_tokens=10,
            output_tokens=20,
        )

        workflow = Workflow(
            name="test",
            phases=[Phase(id="dynamic", type="workflow", prompt="Build a thing.")],
            max_bounces=1,
        )
        engine = self._make_engine(workflow, backend, tmp_path)

        with patch("juvenal.engine._plan_workflow_internal", return_value=plan_result):
            result = engine.run()

        assert result == 1  # sub-workflow failed, bounce exhausted

    def test_workflow_phase_recursion_depth_exceeded(self, tmp_path):
        """Workflow phase fails immediately when depth >= max_depth."""
        backend = MockBackend()
        workflow = Workflow(
            name="test",
            phases=[Phase(id="dynamic", type="workflow", prompt="Build a thing.")],
            max_bounces=3,
        )
        # Set depth already at max
        engine = self._make_engine(workflow, backend, tmp_path, _depth=3, _max_depth=3)
        result = engine.run()
        assert result == 1  # immediate failure due to depth

    def test_workflow_phase_per_phase_max_depth(self, tmp_path):
        """Per-phase max_depth overrides engine-level max_depth."""
        backend = MockBackend()
        workflow = Workflow(
            name="test",
            phases=[Phase(id="dynamic", type="workflow", prompt="Build.", max_depth=1)],
            max_bounces=3,
        )
        # Engine depth=1, engine max=3, but phase max_depth=1 -> 1 >= 1 -> fail
        engine = self._make_engine(workflow, backend, tmp_path, _depth=1, _max_depth=3)
        result = engine.run()
        assert result == 1

    def test_workflow_phase_token_aggregation(self, tmp_path):
        """Tokens from planning and execution are aggregated into parent phase."""
        from unittest.mock import patch

        from juvenal.engine import PlanResult

        backend = MockBackend()
        # Sub-engine will use one implement call
        backend.add_response(exit_code=0, output="done", input_tokens=300, output_tokens=400)

        sub_yaml = tmp_path / "sub" / "workflow.yaml"
        sub_yaml.parent.mkdir(parents=True)
        sub_yaml.write_text("name: sub\nphases:\n  - id: step1\n    prompt: do it\n")

        plan_result = PlanResult(
            success=True,
            workflow_yaml_path=str(sub_yaml),
            temp_dir=str(sub_yaml.parent),
            input_tokens=100,
            output_tokens=200,
        )

        workflow = Workflow(
            name="test",
            phases=[Phase(id="dynamic", type="workflow", prompt="Build.")],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)

        with patch("juvenal.engine._plan_workflow_internal", return_value=plan_result):
            result = engine.run()

        assert result == 0
        # Check aggregated tokens: planning (100+200) + execution (300+400)
        ps = engine.state.phases["dynamic"]
        assert ps.input_tokens == 100 + 300
        assert ps.output_tokens == 200 + 400

    def test_workflow_phase_dry_run(self, tmp_path, capsys):
        """Dry run displays workflow phase type correctly."""
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Set up."),
                Phase(id="dynamic", type="workflow", prompt="Build a REST API.", max_depth=2),
            ],
        )
        engine = self._make_engine(workflow, MockBackend(), tmp_path, dry_run=True)
        assert engine.run() == 0
        captured = capsys.readouterr()
        assert "workflow" in captured.out
        assert "dynamic" in captured.out
        assert "max_depth=2" in captured.out
        assert "Build a REST API" in captured.out

    def test_dynamic_workflow_inherits_backend_and_execution_flags(self, tmp_path):
        from juvenal.engine import PlanResult

        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")
        created_engines = []
        original_init = Engine.__init__

        sub_yaml = tmp_path / "sub" / "workflow.yaml"
        sub_yaml.parent.mkdir(parents=True)
        sub_yaml.write_text("name: sub\nphases:\n  - id: inner\n    prompt: do it\n")
        plan_result = PlanResult(
            success=True,
            workflow_yaml_path=str(sub_yaml),
            temp_dir=str(sub_yaml.parent),
            input_tokens=0,
            output_tokens=0,
        )

        def tracking_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            created_engines.append(self)

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        workflow = Workflow(
            name="test",
            phases=[Phase(id="dynamic", type="workflow", prompt="Build a thing.")],
            max_bounces=3,
            working_dir=str(project_dir),
        )

        with patch.object(Engine, "__init__", tracking_init):
            with patch("juvenal.engine.create_backend", side_effect=AssertionError("factory should not be called")):
                engine = Engine(
                    workflow,
                    state_file=str(tmp_path / "state.json"),
                    plain=True,
                    serialize=True,
                    interactive=True,
                    clear_context_on_bounce=True,
                    backend_instance=backend,
                )
                with patch("juvenal.engine._plan_workflow_internal", return_value=plan_result) as plan_mock:
                    assert engine.run() == 0

        assert plan_mock.call_args.kwargs["interactive"] is True
        assert plan_mock.call_args.kwargs["serialize"] is True
        assert plan_mock.call_args.kwargs["backend_instance"] is backend
        child = next(created for created in created_engines if created._depth == 1)
        assert child.backend is backend
        assert child.workflow.working_dir == str(project_dir)
        assert child.serialize is True
        assert child.interactive is True
        assert child.preserve_context_on_bounce is False


class TestCheckersShorthandEngine:
    def _make_engine(self, workflow, backend, tmp_path, **kwargs):
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), **kwargs)
        engine.backend = backend
        return engine

    def test_checkers_bounce_on_fail(self, tmp_path):
        """Implement passes, inline checker fails, bounces back to implement."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="built it")  # implement
        backend.add_response(exit_code=0, output="VERDICT: FAIL: bad code")  # check bounces
        backend.add_response(exit_code=0, output="fixed it")  # implement again
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # check passes
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="Build it."),
                Phase(id="build~check-1", type="check", role="tester", bounce_target="build"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

    def test_checkers_script_bounce_on_fail(self, tmp_path):
        """Implement passes, inline script checker fails, bounces back to implement."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="built it")  # implement attempt 1
        # script "false" fails -> bounce
        backend.add_response(exit_code=0, output="fixed it")  # implement attempt 2
        # script "true" would pass but we use "false" always -> bounce 2 -> exhausted
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="Build it."),
                Phase(id="build~script-1", type="script", run="false", bounce_target="build"),
            ],
            max_bounces=2,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 1  # exhausted

    def test_injected_checker_pass(self, tmp_path):
        """Engine run with inject_checkers: checker passes."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="built it")  # implement
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # injected check
        workflow = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build it.")],
            max_bounces=3,
        )
        workflow = inject_checkers(workflow, ["tester"])
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

    def test_injected_checker_bounce(self, tmp_path):
        """Engine run with inject_checkers: checker fails, bounces, then passes."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="built it")  # implement
        backend.add_response(exit_code=0, output="VERDICT: FAIL: bad")  # injected check fails
        backend.add_response(exit_code=0, output="fixed it")  # implement again
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # injected check passes
        workflow = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build it.")],
            max_bounces=3,
        )
        workflow = inject_checkers(workflow, ["tester"])
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0

    def test_injected_checker_failure_context_delivered(self, tmp_path):
        """When an injected checker fails, the implement phase receives the failure context on retry."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="built it")  # implement attempt 1
        backend.add_response(exit_code=0, output="VERDICT: FAIL: missing error handling")  # check fails
        backend.add_response(exit_code=0, output="fixed it")  # implement attempt 2 (should get failure context)
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # check passes
        workflow = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build it.")],
            max_bounces=3,
        )
        workflow = inject_checkers(workflow, ["tester"])
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0
        # calls: [0]=implement, [1]=check, [2]=implement retry, [3]=check retry
        # The implement retry (calls[2]) should contain the failure feedback
        assert len(backend.calls) >= 3
        retry_prompt = backend.calls[2]
        assert "missing error handling" in retry_prompt

    def test_injected_checker_attempt_increments_on_bounce(self, tmp_path):
        """Attempt counter should increment when a checker bounces back to the implement phase."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="built it")  # implement attempt 1
        backend.add_response(exit_code=0, output="VERDICT: FAIL: bad")  # check fails -> bounce
        backend.add_response(exit_code=0, output="fixed it")  # implement attempt 2
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # check passes
        workflow = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build it.")],
            max_bounces=3,
        )
        workflow = inject_checkers(workflow, ["tester"])
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0
        assert engine.state.phases["build"].attempt == 2

    def test_checker_failure_context_delivered_multi_phase(self, tmp_path):
        """With --checkers on multiple phases, bounced phase gets failure context."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="phase-a done")  # phase-a implement
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # phase-a check passes
        backend.add_response(exit_code=0, output="phase-b done")  # phase-b implement
        backend.add_response(exit_code=0, output="VERDICT: FAIL: tests not passing")  # phase-b check fails
        backend.add_response(exit_code=0, output="phase-b fixed")  # phase-b implement retry
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # phase-b check passes
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="phase-a", type="implement", prompt="Do A."),
                Phase(id="phase-b", type="implement", prompt="Do B."),
            ],
            max_bounces=3,
        )
        workflow = inject_checkers(workflow, ["tester"])
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0
        # backend.calls: [phase-a prompt, phase-a check, phase-b prompt, phase-b check, phase-b retry, phase-b check]
        # The phase-b retry (calls[3] since checks also go through run_agent) should have the failure context
        # calls[0] = phase-a implement, calls[1] = phase-a check, calls[2] = phase-b implement, calls[3] = phase-b check
        # calls[4] = phase-b retry implement — should contain failure context
        retry_prompt = backend.calls[4]
        assert "tests not passing" in retry_prompt


class TestImplementerEngine:
    def _make_engine(self, workflow, backend, tmp_path, **kwargs):
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), **kwargs)
        engine.backend = backend
        return engine

    def test_implementer_prompt_reaches_backend(self, tmp_path):
        """inject_implementer preamble is included in the prompt sent to the backend."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")
        workflow = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build a REST API.")],
        )
        workflow = inject_implementer(workflow, "software-engineer")
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0
        assert len(backend.calls) == 1
        prompt = backend.calls[0]
        assert "expert software engineer" in prompt
        assert "Build a REST API." in prompt

    def test_implementer_with_checker(self, tmp_path):
        """--implementer and --checker work together."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="built it")  # implement
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # check
        workflow = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build it.")],
        )
        workflow = inject_implementer(workflow, "software-engineer")
        workflow = inject_checkers(workflow, ["tester"])
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0
        # Implement prompt has the preamble
        assert "expert software engineer" in backend.calls[0]
        assert "Build it." in backend.calls[0]


class TestNoVerdictResume:
    def _make_engine(self, workflow, backend, tmp_path, **kwargs):
        """Create an engine with injected mock backend."""
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), **kwargs)
        engine.backend = backend
        return engine

    def test_no_verdict_resume_succeeds(self, tmp_path):
        """No verdict on check -> resume gets PASS, no bounce consumed."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="implemented")  # implement
        backend.add_response(exit_code=0, output="looks good but forgot verdict", session_id="sess-1")  # check
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # resume -> PASS
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-review", type="check", role="tester"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0
        # Verify resume was called
        assert len(backend.resume_calls) == 1
        assert backend.resume_calls[0][0] == "sess-1"

    def test_no_verdict_resume_gets_fail(self, tmp_path):
        """No verdict on check -> resume gets explicit FAIL -> bounces normally."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="implemented")  # implement
        backend.add_response(exit_code=0, output="no verdict here", session_id="sess-2")  # check
        backend.add_response(exit_code=0, output="VERDICT: FAIL: bad code")  # resume -> FAIL
        backend.add_response(exit_code=0, output="fixed")  # implement again
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # check passes
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-review", type="check", role="tester"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0
        assert len(backend.resume_calls) == 1

    def test_no_verdict_resume_exhausted(self, tmp_path):
        """2 resumes still no verdict -> falls through to bounce."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="implemented")  # implement
        backend.add_response(exit_code=0, output="no verdict", session_id="sess-3")  # check
        backend.add_response(exit_code=0, output="still no verdict")  # resume 1
        backend.add_response(exit_code=0, output="still nothing")  # resume 2
        # After exhausting resumes, it bounces. Re-implement + re-check:
        backend.add_response(exit_code=0, output="fixed")  # implement again
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # check passes
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-review", type="check", role="tester"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0
        assert len(backend.resume_calls) == 2

    def test_explicit_fail_does_not_resume(self, tmp_path):
        """VERDICT: FAIL skips resume entirely."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="implemented")  # implement
        backend.add_response(exit_code=0, output="VERDICT: FAIL: bad", session_id="sess-4")  # check -> explicit FAIL
        backend.add_response(exit_code=0, output="fixed")  # implement again
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # check passes
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-review", type="check", role="tester"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0
        # No resume calls — the explicit FAIL should skip resume
        assert len(backend.resume_calls) == 0

    def test_no_session_id_skips_resume(self, tmp_path):
        """Backend returns no session_id -> falls through to bounce without resume."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="implemented")  # implement
        backend.add_response(exit_code=0, output="no verdict, no session")  # check (no session_id)
        # Should bounce without attempting resume
        backend.add_response(exit_code=0, output="fixed")  # implement again
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # check passes
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-review", type="check", role="tester"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)
        assert engine.run() == 0
        assert len(backend.resume_calls) == 0


class TestExtractYaml:
    def test_yaml_code_fence(self):
        text = "Here's the workflow:\n```yaml\nname: test\nphases: []\n```\nDone."
        assert "name: test" in _extract_yaml(text)
        assert "```" not in _extract_yaml(text)

    def test_generic_code_fence(self):
        text = "Here:\n```\nname: test\nphases: []\n```\n"
        assert "name: test" in _extract_yaml(text)

    def test_no_fence_with_prose(self):
        text = "Sure, here is your workflow.\n\nname: test\nphases:\n  - id: a\n    prompt: do it\n"
        result = _extract_yaml(text)
        assert "name: test" in result
        assert "Sure, here" not in result

    def test_raw_yaml(self):
        text = "name: test\nphases:\n  - id: a\n    prompt: do it\n"
        assert _extract_yaml(text) == text


class TestPlanWorkflow:
    def test_plan_creates_temp_structure(self, tmp_path, monkeypatch):
        """plan_workflow creates temp dir with .plan/goal.md and runs Engine."""
        from unittest.mock import MagicMock, patch

        from juvenal.engine import plan_workflow

        monkeypatch.chdir(tmp_path)
        mock_engine_instance = MagicMock()
        mock_engine_instance.run.return_value = 0

        def fake_engine_init(self_engine, workflow, **kwargs):
            # Write a valid workflow.yaml in the working dir to simulate engine output
            wd = Path(workflow.working_dir)
            (wd / "workflow.yaml").write_text("name: test\nphases:\n  - id: a\n    prompt: do it\n")
            self_engine.workflow = workflow
            self_engine.backend = MagicMock()
            self_engine.display = MagicMock()
            self_engine.dry_run = False
            self_engine.state = MagicMock(**{"total_tokens.return_value": (0, 0)})
            self_engine._start_idx = 0
            # Verify .plan/goal.md was created
            assert (wd / ".plan" / "goal.md").exists()
            assert (wd / ".plan" / "goal.md").read_text() == "build something"

        with (
            patch.object(Engine, "__init__", fake_engine_init),
            patch.object(Engine, "run", return_value=0),
        ):
            out_path = str(tmp_path / "workflow.yaml")
            plan_workflow("build something", out_path)

        assert Path(out_path).exists()
        content = Path(out_path).read_text()
        assert "phases" in content

    def test_plan_copies_output_on_success(self, tmp_path, monkeypatch):
        """plan_workflow copies workflow.yaml to output path and cleans up."""
        from unittest.mock import MagicMock, patch

        from juvenal.engine import plan_workflow

        monkeypatch.chdir(tmp_path)
        yaml_content = "name: result\nphases:\n  - id: x\n    prompt: hi\n"

        def fake_engine_init(self_engine, workflow, **kwargs):
            wd = Path(workflow.working_dir)
            (wd / "workflow.yaml").write_text(yaml_content)
            self_engine.workflow = workflow
            self_engine.backend = MagicMock()
            self_engine.display = MagicMock()
            self_engine.dry_run = False
            self_engine.state = MagicMock(**{"total_tokens.return_value": (0, 0)})
            self_engine._start_idx = 0

        with (
            patch.object(Engine, "__init__", fake_engine_init),
            patch.object(Engine, "run", return_value=0),
        ):
            out_path = str(tmp_path / "out.yaml")
            plan_workflow("goal", out_path)

        import yaml

        parsed = yaml.safe_load(Path(out_path).read_text())
        assert parsed["name"] == "result"
        assert "phases" in parsed

    def test_plan_fails_on_engine_failure(self, tmp_path, monkeypatch):
        """plan_workflow raises SystemExit if engine returns non-zero."""
        from unittest.mock import MagicMock, patch

        from juvenal.engine import plan_workflow

        monkeypatch.chdir(tmp_path)

        def fake_engine_init(self_engine, workflow, **kwargs):
            self_engine.workflow = workflow
            self_engine.backend = MagicMock()
            self_engine.display = MagicMock()
            self_engine.dry_run = False
            self_engine.state = MagicMock(**{"total_tokens.return_value": (0, 0)})
            self_engine._start_idx = 0

        with (
            patch.object(Engine, "__init__", fake_engine_init),
            patch.object(Engine, "run", return_value=1),
        ):
            with pytest.raises(SystemExit):
                plan_workflow("goal", str(tmp_path / "out.yaml"))


class TestBaselineSha:
    def _make_engine(self, workflow, backend, tmp_path, **kwargs):
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), **kwargs)
        engine.backend = backend
        return engine

    def test_baseline_sha_captured_on_first_implement(self, tmp_path):
        """Engine captures git HEAD as baseline_sha before the first implement run."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="check", type="script", run="true"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)

        with patch.object(engine, "_get_git_head", return_value="abc123"):
            engine.run()

        assert engine.state.phases["setup"].baseline_sha == "abc123"

    def test_baseline_sha_not_overwritten_on_bounce(self, tmp_path):
        """baseline_sha is set once and not overwritten when the phase re-runs after bounce."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")  # implement attempt 1
        backend.add_response(exit_code=0, output="VERDICT: FAIL: bad")  # check fails
        backend.add_response(exit_code=0, output="fixed")  # implement attempt 2
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # check passes
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="Build it."),
                Phase(id="review", type="check", role="tester"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)

        call_count = 0

        def mock_git_head():
            nonlocal call_count
            call_count += 1
            return f"sha-{call_count}"

        with patch.object(engine, "_get_git_head", side_effect=mock_git_head):
            engine.run()

        # Should keep the first SHA, not overwrite on the second implement run
        assert engine.state.phases["build"].baseline_sha == "sha-1"

    def test_baseline_sha_injected_into_check_prompt(self, tmp_path):
        """Check phase receives the baseline SHA in its prompt."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="implemented")  # implement
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # check
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="review", type="check", prompt="Check the work."),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)

        with patch.object(engine, "_get_git_head", return_value="deadbeef"):
            engine.run()

        # The check prompt should contain the baseline SHA
        check_prompt = backend.calls[1]  # second call is the check
        assert "deadbeef" in check_prompt
        assert "git diff deadbeef..HEAD" in check_prompt

    def test_baseline_sha_injected_on_bounce_recheck(self, tmp_path):
        """After a bounce, the re-run checker still gets the original baseline SHA."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")  # implement attempt 1
        backend.add_response(exit_code=0, output="VERDICT: FAIL: bad")  # check fails
        backend.add_response(exit_code=0, output="fixed")  # implement attempt 2
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # check passes
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="Build it."),
                Phase(id="review", type="check", prompt="Review."),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)

        with patch.object(engine, "_get_git_head", return_value="baseline-sha"):
            engine.run()

        # Both check calls should have the same baseline SHA
        first_check = backend.calls[1]  # implement, check, implement, check
        second_check = backend.calls[3]
        assert "baseline-sha" in first_check
        assert "baseline-sha" in second_check

    def test_no_baseline_when_not_git_repo(self, tmp_path):
        """When not in a git repo, baseline_sha is None and no SHA is injected."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")  # implement
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # check
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="review", type="check", prompt="Check."),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)

        with patch.object(engine, "_get_git_head", return_value=None):
            engine.run()

        assert engine.state.phases["setup"].baseline_sha is None
        # Check prompt should not have git diff instruction
        check_prompt = backend.calls[1]
        assert "git diff" not in check_prompt

    def test_baseline_sha_with_explicit_bounce_target(self, tmp_path):
        """Check phase with explicit bounce_target uses the target's baseline SHA."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="setup done")  # setup implement
        backend.add_response(exit_code=0, output="feature done")  # feature implement
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # review passes
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Set up."),
                Phase(id="feature", type="implement", prompt="Build feature."),
                Phase(id="review", type="check", prompt="Review.", bounce_target="setup"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path)

        call_count = 0

        def mock_git_head():
            nonlocal call_count
            call_count += 1
            # setup gets sha-1, feature gets sha-2
            return f"sha-{call_count}"

        with patch.object(engine, "_get_git_head", side_effect=mock_git_head):
            engine.run()

        # Review's bounce_target is "setup", so it should use setup's baseline SHA
        check_prompt = backend.calls[2]
        assert "sha-1" in check_prompt

    def test_baseline_sha_persists_through_state_save_load(self, tmp_path):
        """baseline_sha survives state save and load (for --resume)."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")
        backend.add_response(exit_code=1, output="crash")  # next phase crashes -> exhausted
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="phase1", type="implement", prompt="Do phase 1."),
                Phase(id="phase2", type="implement", prompt="Do phase 2."),
            ],
            max_bounces=1,
        )
        state_file = str(tmp_path / "state.json")
        engine = Engine(workflow, state_file=state_file)
        engine.backend = backend

        with patch.object(engine, "_get_git_head", return_value="persist-sha"):
            engine.run()

        # Load state from disk and verify baseline_sha survived
        from juvenal.state import PipelineState

        loaded = PipelineState.load(state_file)
        assert loaded.phases["phase1"].baseline_sha == "persist-sha"


class TestLaneGroups:
    def test_lane_group_both_pass(self, tmp_path):
        """Two lanes, both pass on first try."""
        backend = MockBackend()
        # Lane A: implement a -> check_a passes
        backend.add_response(exit_code=0, output="done a")
        backend.add_response(exit_code=0, output="VERDICT: PASS")
        # Lane B: implement b -> check_b passes
        backend.add_response(exit_code=0, output="done b")
        backend.add_response(exit_code=0, output="VERDICT: PASS")

        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="Build A."),
                Phase(id="check_a", type="check", role="tester", bounce_target="a"),
                Phase(id="b", type="implement", prompt="Build B."),
                Phase(id="check_b", type="check", role="tester", bounce_target="b"),
            ],
            parallel_groups=[ParallelGroup(lanes=[["a", "check_a"], ["b", "check_b"]])],
            max_bounces=5,
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend
        with patch.object(engine, "_get_git_head", return_value=None):
            exit_code = engine.run()
        assert exit_code == 0

    def test_lane_group_one_bounces_then_passes(self, tmp_path):
        """Lane A passes immediately, lane B bounces once then passes."""
        backend = MockBackend()
        # Lane A: implement a -> check_a passes (calls 0, 1)
        backend.add_response(exit_code=0, output="done a")
        backend.add_response(exit_code=0, output="VERDICT: PASS")
        # Lane B attempt 1: implement b -> check_b fails (calls 2, 3)
        backend.add_response(exit_code=0, output="done b")
        backend.add_response(exit_code=0, output="VERDICT: FAIL: tests broken")
        # Lane B attempt 2: implement b retry -> check_b passes (calls 4, 5)
        backend.add_response(exit_code=0, output="done b fixed")
        backend.add_response(exit_code=0, output="VERDICT: PASS")

        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="Build A."),
                Phase(id="check_a", type="check", role="tester", bounce_target="a"),
                Phase(id="b", type="implement", prompt="Build B."),
                Phase(id="check_b", type="check", role="tester", bounce_target="b"),
            ],
            parallel_groups=[ParallelGroup(lanes=[["a", "check_a"], ["b", "check_b"]])],
            max_bounces=5,
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend
        with patch.object(engine, "_get_git_head", return_value=None):
            exit_code = engine.run()
        assert exit_code == 0

    def test_lane_group_exhausts_budget(self, tmp_path):
        """Lanes exhaust global bounce budget, pipeline fails."""
        backend = MockBackend()
        # Each lane bounces: implement -> check fails -> implement -> check fails ...
        for _ in range(10):
            backend.add_response(exit_code=0, output="done")
            backend.add_response(exit_code=0, output="VERDICT: FAIL: bad")

        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="Build A."),
                Phase(id="check_a", type="check", role="tester", bounce_target="a"),
                Phase(id="b", type="implement", prompt="Build B."),
                Phase(id="check_b", type="check", role="tester", bounce_target="b"),
            ],
            parallel_groups=[ParallelGroup(lanes=[["a", "check_a"], ["b", "check_b"]])],
            max_bounces=2,  # Only 2 bounces total across all lanes
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend
        with patch.object(engine, "_get_git_head", return_value=None):
            exit_code = engine.run()
        assert exit_code == 1

    def test_lane_group_preserve_context_on_bounce(self, tmp_path):
        """Session resumption works within lanes."""
        backend = MockBackend()
        # Lane: implement -> check fails -> implement (resumed) -> check passes
        backend.add_response(exit_code=0, output="first attempt", session_id="sess-1")
        backend.add_response(exit_code=0, output="VERDICT: FAIL: tests fail")
        backend.add_response(exit_code=0, output="fixed", session_id="sess-1")
        backend.add_response(exit_code=0, output="VERDICT: PASS")

        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="impl", type="implement", prompt="Build it."),
                Phase(id="chk", type="check", role="tester", bounce_target="impl"),
            ],
            parallel_groups=[ParallelGroup(lanes=[["impl", "chk"]])],
            max_bounces=5,
        )
        engine = Engine(
            workflow,
            state_file=str(tmp_path / "state.json"),
            plain=True,
        )
        engine.backend = backend
        with patch.object(engine, "_get_git_head", return_value=None):
            exit_code = engine.run()
        assert exit_code == 0
        # The second implement call should have been a resume
        assert len(backend.resume_calls) == 1
        assert backend.resume_calls[0][0] == "sess-1"

    def test_lane_group_shared_bounce_budget(self, tmp_path):
        """Two lanes each bounce once, total = 2, within budget of 3."""
        backend = MockBackend()
        # Lane A: implement a -> check fails -> implement a -> check passes
        backend.add_response(exit_code=0, output="a1")
        backend.add_response(exit_code=0, output="VERDICT: FAIL: a bad")
        backend.add_response(exit_code=0, output="a2")
        backend.add_response(exit_code=0, output="VERDICT: PASS")
        # Lane B: implement b -> check fails -> implement b -> check passes
        backend.add_response(exit_code=0, output="b1")
        backend.add_response(exit_code=0, output="VERDICT: FAIL: b bad")
        backend.add_response(exit_code=0, output="b2")
        backend.add_response(exit_code=0, output="VERDICT: PASS")

        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="Build A."),
                Phase(id="check_a", type="check", role="tester", bounce_target="a"),
                Phase(id="b", type="implement", prompt="Build B."),
                Phase(id="check_b", type="check", role="tester", bounce_target="b"),
            ],
            parallel_groups=[ParallelGroup(lanes=[["a", "check_a"], ["b", "check_b"]])],
            max_bounces=3,  # Budget of 3 > 2 bounces consumed
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend
        with patch.object(engine, "_get_git_head", return_value=None):
            exit_code = engine.run()
        assert exit_code == 0


class TestResumeWithParallelPhases:
    """Tests for --resume interaction with parallel groups and lanes."""

    def test_resume_snaps_to_flat_parallel_group_start(self, tmp_path):
        """When resume index lands in middle of a flat parallel group, snap to first phase."""
        backend = MockBackend()
        # After snap, the whole group re-runs: a, b, c all succeed
        backend.add_response(exit_code=0, output="done a")
        backend.add_response(exit_code=0, output="done b")
        backend.add_response(exit_code=0, output="done c")
        # Then the post-group script passes
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
                Phase(id="b", type="implement", prompt="B."),
                Phase(id="c", type="implement", prompt="C."),
                Phase(id="final", type="script", run="true"),
            ],
            parallel_groups=[ParallelGroup(phases=["a", "b", "c"])],
            max_bounces=3,
        )
        # Pre-populate state: "a" completed, "b" pending — resume would land on "b"
        from juvenal.state import PipelineState

        state_file = str(tmp_path / "state.json")
        state = PipelineState(state_file=Path(state_file))
        state.mark_completed("a")
        state.save()

        engine = Engine(workflow, resume=True, state_file=state_file, plain=True)
        engine.backend = backend
        # Should have snapped start_idx to "a" (index 0), not "b" (index 1)
        assert engine._start_idx == 0

    def test_resume_flat_parallel_skips_completed_phases(self, tmp_path):
        """On resume, completed phases within a flat parallel group are not re-run."""
        backend = MockBackend()
        # Only "b" and "c" should run (a is already completed)
        backend.add_response(exit_code=0, output="done b")
        backend.add_response(exit_code=0, output="done c")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
                Phase(id="b", type="implement", prompt="B."),
                Phase(id="c", type="implement", prompt="C."),
            ],
            parallel_groups=[ParallelGroup(phases=["a", "b", "c"])],
            max_bounces=3,
        )
        from juvenal.state import PipelineState

        state_file = str(tmp_path / "state.json")
        state = PipelineState(state_file=Path(state_file))
        state.mark_completed("a")
        state.save()

        engine = Engine(workflow, resume=True, state_file=state_file, plain=True)
        engine.backend = backend
        with patch.object(engine, "_get_git_head", return_value=None):
            exit_code = engine.run()
        assert exit_code == 0
        # Only 2 calls (b and c), not 3
        assert len(backend.calls) == 2

    def test_resume_all_parallel_completed_skips_group(self, tmp_path):
        """If all phases in a flat parallel group are completed, skip the entire group."""
        backend = MockBackend()
        # No backend calls expected for the parallel group — only the post-group script
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
                Phase(id="b", type="implement", prompt="B."),
                Phase(id="post", type="script", run="true"),
            ],
            parallel_groups=[ParallelGroup(phases=["a", "b"])],
            max_bounces=3,
        )
        from juvenal.state import PipelineState

        state_file = str(tmp_path / "state.json")
        state = PipelineState(state_file=Path(state_file))
        state.mark_completed("a")
        state.mark_completed("b")
        state.save()

        engine = Engine(workflow, resume=True, state_file=state_file, plain=True)
        engine.backend = backend
        exit_code = engine.run()
        assert exit_code == 0
        assert len(backend.calls) == 0

    def test_resume_snaps_to_lane_group_start(self, tmp_path):
        """When resume lands inside a lane group, snap to first phase of first lane."""
        backend = MockBackend()
        # Lane A (a, check_a) and Lane B (b, check_b) — all succeed
        backend.add_response(exit_code=0, output="done a")
        backend.add_response(exit_code=0, output="VERDICT: PASS")
        backend.add_response(exit_code=0, output="done b")
        backend.add_response(exit_code=0, output="VERDICT: PASS")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
                Phase(id="check_a", type="check", role="tester", bounce_target="a"),
                Phase(id="b", type="implement", prompt="B."),
                Phase(id="check_b", type="check", role="tester", bounce_target="b"),
            ],
            parallel_groups=[ParallelGroup(lanes=[["a", "check_a"], ["b", "check_b"]])],
            max_bounces=5,
        )
        from juvenal.state import PipelineState

        state_file = str(tmp_path / "state.json")
        state = PipelineState(state_file=Path(state_file))
        state.mark_completed("a")
        state.mark_completed("check_a")
        # Lane B incomplete — resume would land on "b" (index 2)
        state.save()

        engine = Engine(workflow, resume=True, state_file=state_file, plain=True)
        engine.backend = backend
        # Should snap to "a" (index 0)
        assert engine._start_idx == 0

    def test_resume_lane_group_skips_completed_lane(self, tmp_path):
        """On resume, fully-completed lanes are not re-run."""
        backend = MockBackend()
        # Only lane B should run
        backend.add_response(exit_code=0, output="done b")
        backend.add_response(exit_code=0, output="VERDICT: PASS")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
                Phase(id="check_a", type="check", role="tester", bounce_target="a"),
                Phase(id="b", type="implement", prompt="B."),
                Phase(id="check_b", type="check", role="tester", bounce_target="b"),
            ],
            parallel_groups=[ParallelGroup(lanes=[["a", "check_a"], ["b", "check_b"]])],
            max_bounces=5,
        )
        from juvenal.state import PipelineState

        state_file = str(tmp_path / "state.json")
        state = PipelineState(state_file=Path(state_file))
        state.mark_completed("a")
        state.mark_completed("check_a")
        state.save()

        engine = Engine(workflow, resume=True, state_file=state_file, plain=True)
        engine.backend = backend
        with patch.object(engine, "_get_git_head", return_value=None):
            exit_code = engine.run()
        assert exit_code == 0
        # Only 2 calls (b implement + check_b), not 4
        assert len(backend.calls) == 2

    def test_resume_lane_skips_completed_phases_within_lane(self, tmp_path):
        """Within a lane, already-completed phases are skipped on resume."""
        backend = MockBackend()
        # Lane A: check_a should run (a already completed)
        backend.add_response(exit_code=0, output="VERDICT: PASS")
        # Lane B: both b and check_b should run
        backend.add_response(exit_code=0, output="done b")
        backend.add_response(exit_code=0, output="VERDICT: PASS")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
                Phase(id="check_a", type="check", role="tester", bounce_target="a"),
                Phase(id="b", type="implement", prompt="B."),
                Phase(id="check_b", type="check", role="tester", bounce_target="b"),
            ],
            parallel_groups=[ParallelGroup(lanes=[["a", "check_a"], ["b", "check_b"]])],
            max_bounces=5,
        )
        from juvenal.state import PipelineState

        state_file = str(tmp_path / "state.json")
        state = PipelineState(state_file=Path(state_file))
        state.mark_completed("a")
        # check_a and lane B not completed
        state.save()

        engine = Engine(workflow, resume=True, state_file=state_file, plain=True)
        engine.backend = backend
        with patch.object(engine, "_get_git_head", return_value=None):
            exit_code = engine.run()
        assert exit_code == 0
        # 3 calls: check_a + b + check_b (not a)
        assert len(backend.calls) == 3

    def test_align_state_phases_fixes_dict_order(self, tmp_path):
        """State phases dict is reordered to match workflow phase order."""
        from juvenal.state import PipelineState

        state_file = str(tmp_path / "state.json")
        # Create state with phases in wrong order (simulating parallel thread race)
        state = PipelineState(state_file=Path(state_file))
        state._ensure_phase("c")
        state._ensure_phase("a")
        state._ensure_phase("b")
        state.save()

        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
                Phase(id="b", type="implement", prompt="B."),
                Phase(id="c", type="implement", prompt="C."),
            ],
            max_bounces=3,
        )
        engine = Engine(workflow, resume=True, state_file=state_file, plain=True)
        # After _align_state_phases, order should be a, b, c
        assert list(engine.state.phases.keys()) == ["a", "b", "c"]

    def test_invalidate_from_respects_workflow_order_after_align(self, tmp_path):
        """invalidate_from works correctly after _align_state_phases fixes ordering."""
        from juvenal.state import PipelineState

        state_file = str(tmp_path / "state.json")
        # Create state with phases in wrong order
        state = PipelineState(state_file=Path(state_file))
        state._ensure_phase("c")
        state.mark_completed("c")
        state._ensure_phase("a")
        state.mark_completed("a")
        state._ensure_phase("b")
        state.mark_completed("b")
        state.save()

        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
                Phase(id="b", type="implement", prompt="B."),
                Phase(id="c", type="implement", prompt="C."),
            ],
            parallel_groups=[ParallelGroup(phases=["a", "b", "c"])],
            max_bounces=3,
        )
        engine = Engine(workflow, resume=True, state_file=state_file, plain=True)
        # Now invalidate from "b" — should invalidate b and c, but not a
        engine.state.invalidate_from("b")
        assert engine.state.phases["a"].status == "completed"
        assert engine.state.phases["b"].status == "pending"
        assert engine.state.phases["c"].status == "pending"


class TestBounceCounter:
    def test_try_increment_within_budget(self):
        bc = BounceCounter(max_bounces=3)
        assert bc.try_increment()
        assert bc.try_increment()
        assert bc.try_increment()
        assert not bc.try_increment()  # exhausted
        assert bc.count == 3

    def test_try_increment_zero_budget(self):
        bc = BounceCounter(max_bounces=0)
        assert not bc.try_increment()
        assert bc.count == 0


class TestTemplateVarsEngine:
    def test_vars_substituted_in_implement_prompt(self, mock_backend, tmp_path):
        """Vars are substituted in implement phase prompts sent to the backend."""
        mock_backend.add_response(exit_code=0, output="done")
        workflow = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Deploy to {{ENV}} in {{REGION}}.")],
            vars={"ENV": "prod", "REGION": "us-west-2"},
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = mock_backend
        engine.run()
        assert "Deploy to prod in us-west-2." in mock_backend.calls[0]

    def test_vars_substituted_in_check_prompt(self, mock_backend, tmp_path):
        """Vars are substituted in check phase prompts."""
        mock_backend.add_response(exit_code=0, output="done")
        mock_backend.add_response(exit_code=0, output="VERDICT: PASS")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="Build {{APP}}."),
                Phase(id="review", type="check", prompt="Verify {{APP}} works.", bounce_target="build"),
            ],
            vars={"APP": "myservice"},
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = mock_backend
        engine.run()
        # Check phase prompt should contain the substituted var
        assert "Verify myservice works." in mock_backend.calls[1]

    def test_vars_substituted_in_script_run(self, mock_backend, tmp_path):
        """Vars are substituted in script phase run commands."""
        mock_backend.add_response(exit_code=0, output="done")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="Build it."),
                Phase(id="test", type="script", run="pytest {{TEST_DIR}} -x", bounce_target="build"),
            ],
            vars={"TEST_DIR": "tests/unit"},
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = mock_backend
        with patch("juvenal.engine.run_script") as mock_run:
            mock_run.return_value = type("R", (), {"exit_code": 0, "output": "ok"})()
            engine.run()
            mock_run.assert_called_once()
            assert mock_run.call_args[0][0] == "pytest tests/unit -x"

    def test_default_filter_allows_undefined_var_at_runtime(self, mock_backend, tmp_path):
        """Valid Jinja2 undefined handling should not be blocked by validation."""
        mock_backend.add_response(exit_code=0, output="done")
        workflow = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt='Use {{ missing|default("fallback") }}.')],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = mock_backend
        assert engine.run() == 0
        assert mock_backend.calls == ["Use fallback."]

    def test_vars_unrecognized_passthrough_fails_validation(self, mock_backend, tmp_path, capsys):
        """Undefined template vars fail validation before execution."""
        workflow = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Use {{KNOWN}} and {{UNKNOWN}}.")],
            vars={"KNOWN": "value"},
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = mock_backend
        assert engine.run() == 1
        captured = capsys.readouterr()
        assert "{{UNKNOWN}}" in captured.out
        assert "no value defined" in captured.out
        assert mock_backend.calls == []

    def test_vars_in_dry_run(self, mock_backend, tmp_path, capsys):
        """Dry run shows variables."""
        workflow = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build {{APP}}.")],
            vars={"APP": "myservice"},
        )
        engine = Engine(workflow, dry_run=True, state_file=str(tmp_path / "state.json"), plain=True)
        engine.run()
        captured = capsys.readouterr()
        assert "APP" in captured.out
        assert "myservice" in captured.out


class TestInvalidJinjaRuntime:
    def test_invalid_jinja_in_implement_phase_fails_cleanly(self, tmp_path, capsys):
        workflow = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="{{ PROJECT")],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = MockBackend()
        assert engine.run() == 1
        captured = capsys.readouterr()
        assert "invalid Jinja2 prompt" in captured.out
        assert "Traceback" not in captured.out

    def test_invalid_jinja_in_script_phase_fails_cleanly(self, tmp_path, capsys):
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="Build it."),
                Phase(id="test", type="script", run="echo {{ PROJECT", bounce_target="build"),
            ],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend
        assert engine.run() == 1
        captured = capsys.readouterr()
        assert "invalid Jinja2 run" in captured.out
        assert "Traceback" not in captured.out

    def test_invalid_jinja_in_dynamic_workflow_phase_fails_cleanly(self, tmp_path, capsys):
        workflow = Workflow(
            name="test",
            phases=[Phase(id="sub", type="workflow", prompt="{{ PROJECT")],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = MockBackend()
        assert engine.run() == 1
        captured = capsys.readouterr()
        assert "invalid Jinja2 prompt" in captured.out
        assert "Traceback" not in captured.out

    def test_render_error_in_implement_phase_fails_cleanly(self, tmp_path, capsys):
        workflow = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="{{ 1 / 0 }}")],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = MockBackend()
        assert engine.run() == 1
        captured = capsys.readouterr()
        assert "Jinja2 render error in prompt for phase 'build'" in captured.out
        assert "Traceback" not in captured.out

    def test_render_error_in_script_phase_fails_cleanly(self, tmp_path, capsys):
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="build", type="implement", prompt="Build it."),
                Phase(id="test", type="script", run="echo {{ 1 / 0 }}", bounce_target="build"),
            ],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend
        assert engine.run() == 1
        captured = capsys.readouterr()
        assert "Jinja2 render error in script command for phase 'test'" in captured.out
        assert "Traceback" not in captured.out

    def test_render_error_in_dynamic_workflow_phase_fails_cleanly(self, tmp_path, capsys):
        workflow = Workflow(
            name="test",
            phases=[Phase(id="sub", type="workflow", prompt="{{ 1 / 0 }}")],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = MockBackend()
        assert engine.run() == 1
        captured = capsys.readouterr()
        assert "Jinja2 render error in prompt for phase 'sub'" in captured.out
        assert "Traceback" not in captured.out

    def test_nested_lookup_missing_in_implement_phase_fails_cleanly(self, tmp_path, capsys):
        workflow = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="{{ config.env }}")],
            vars={"config": {}},
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = MockBackend()
        assert engine.run() == 1
        captured = capsys.readouterr()
        assert "Jinja2 render error in prompt for phase 'build'" in captured.out
        assert "env" in captured.out
        assert "Traceback" not in captured.out

    def test_builtin_jinja_globals_are_not_available_at_runtime(self, tmp_path, capsys):
        workflow = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="{{ cycler.__init__.__globals__ }}")],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = MockBackend()
        assert engine.run() == 1
        captured = capsys.readouterr()
        assert "{{cycler}}" in captured.out
        assert "no value defined" in captured.out
        assert "Traceback" not in captured.out

    def test_invalid_filtered_undefined_var_fails_before_execution(self, tmp_path, capsys):
        backend = MockBackend()
        workflow = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Deploy {{ app|title }}.")],
            vars={"App": "svc"},
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend
        assert engine.run() == 1
        captured = capsys.readouterr()
        assert "{{app}}" in captured.out
        assert "no value defined" in captured.out
        assert backend.calls == []


class TestSerialize:
    def test_serialize_runs_flat_parallel_sequentially(self, tmp_path):
        """With serialize=True, flat parallel groups run sequentially."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done a")
        backend.add_response(exit_code=0, output="done b")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
                Phase(id="b", type="implement", prompt="B."),
            ],
            parallel_groups=[ParallelGroup(phases=["a", "b"])],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True, serialize=True)
        engine.backend = backend
        with patch.object(engine, "_get_git_head", return_value=None):
            assert engine.run() == 0
        assert len(backend.calls) == 2

    def test_serialize_runs_lane_group_sequentially(self, tmp_path):
        """With serialize=True, lane groups run sequentially."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done a")
        backend.add_response(exit_code=0, output="VERDICT: PASS")
        backend.add_response(exit_code=0, output="done b")
        backend.add_response(exit_code=0, output="VERDICT: PASS")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
                Phase(id="check-a", type="check", prompt="Check A.", bounce_target="a"),
                Phase(id="b", type="implement", prompt="B."),
                Phase(id="check-b", type="check", prompt="Check B.", bounce_target="b"),
            ],
            parallel_groups=[ParallelGroup(lanes=[["a", "check-a"], ["b", "check-b"]])],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True, serialize=True)
        engine.backend = backend
        with patch.object(engine, "_get_git_head", return_value=None):
            assert engine.run() == 0
        assert len(backend.calls) == 4


class TestMultiVarExpansionEngine:
    def test_expanded_phases_run_in_parallel(self, tmp_path):
        """Multi-value var expansion creates parallel lanes that execute."""
        from juvenal.workflow import expand_multi_vars

        backend = MockBackend()
        backend.add_response(exit_code=0, output="built linux")
        backend.add_response(exit_code=0, output="built windows")
        wf = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build for {{TARGET}}.")],
        )
        wf = expand_multi_vars(wf, {"TARGET": ["linux", "windows"]})
        engine = Engine(workflow=wf, state_file=str(tmp_path / "state.json"), plain=True, serialize=True)
        engine.backend = backend
        with patch.object(engine, "_get_git_head", return_value=None):
            assert engine.run() == 0
        assert len(backend.calls) == 2
        # Verify both prompts were sent with substituted values
        prompts = set(backend.calls)
        assert any("linux" in p for p in prompts)
        assert any("windows" in p for p in prompts)

    def test_expanded_group_with_checker(self, tmp_path):
        """Multi-value expansion with checker creates lane groups that bounce correctly."""
        from juvenal.workflow import expand_multi_vars

        backend = MockBackend()
        # Lane 1 (staging): implement pass, check pass
        backend.add_response(exit_code=0, output="deployed to staging")
        backend.add_response(exit_code=0, output="VERDICT: PASS")
        # Lane 2 (prod): implement pass, check fail, implement retry, check pass
        backend.add_response(exit_code=0, output="deployed to prod", session_id="s1")
        backend.add_response(exit_code=0, output="VERDICT: FAIL: broken")
        backend.add_response(exit_code=0, output="fixed prod")
        backend.add_response(exit_code=0, output="VERDICT: PASS")
        wf = Workflow(
            name="test",
            phases=[
                Phase(id="deploy", type="implement", prompt="Deploy to {{ENV}}."),
                Phase(id="deploy~check-1", type="check", prompt="Verify {{ENV}}.", bounce_target="deploy"),
            ],
            max_bounces=5,
        )
        wf = expand_multi_vars(wf, {"ENV": ["staging", "prod"]})
        engine = Engine(workflow=wf, state_file=str(tmp_path / "state.json"), plain=True, serialize=True)
        engine.backend = backend
        with patch.object(engine, "_get_git_head", return_value=None):
            assert engine.run() == 0


class TestKeyboardInterrupt:
    def test_interrupt_saves_state_and_returns_130(self, tmp_path):
        """KeyboardInterrupt saves state and returns exit code 130."""
        backend = MockBackend()

        # Make run_agent raise KeyboardInterrupt
        def interrupted_run(*args, **kwargs):
            raise KeyboardInterrupt

        backend.run_agent = interrupted_run

        workflow = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build.")],
        )
        state_file = str(tmp_path / "state.json")
        engine = Engine(workflow, state_file=state_file, plain=True)
        engine.backend = backend
        assert engine.run() == 130

        # State file should exist (was saved)
        from juvenal.state import PipelineState

        state = PipelineState.load(state_file)
        assert "build" in state.phases

    def test_interrupt_kills_active_processes(self, tmp_path):
        """KeyboardInterrupt calls kill_active on the backend."""
        backend = MockBackend()
        kill_called = []

        original_kill = backend.kill_active

        def tracking_kill():
            kill_called.append(True)
            original_kill()

        backend.kill_active = tracking_kill

        def interrupted_run(*args, **kwargs):
            raise KeyboardInterrupt

        backend.run_agent = interrupted_run

        workflow = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Build.")],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend
        engine.run()
        assert len(kill_called) == 1

    def test_interrupt_mid_pipeline_preserves_completed(self, tmp_path):
        """Interrupt after first phase completes preserves that phase's completed status."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="phase 1 done")

        call_count = [0]
        original_run = backend.run_agent

        def interrupt_on_second(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise KeyboardInterrupt
            return original_run(*args, **kwargs)

        backend.run_agent = interrupt_on_second

        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Setup."),
                Phase(id="build", type="implement", prompt="Build."),
            ],
        )
        state_file = str(tmp_path / "state.json")
        engine = Engine(workflow, state_file=state_file, plain=True)
        engine.backend = backend
        assert engine.run() == 130

        from juvenal.state import PipelineState

        state = PipelineState.load(state_file)
        assert state.phases["setup"].status == "completed"


class TestBackendInjection:
    def test_engine_uses_backend_instance_without_factory(self, tmp_path):
        backend = MockBackend()
        workflow = Workflow(name="test", phases=[Phase(id="build", type="implement", prompt="Build.")])

        with patch("juvenal.engine.create_backend", side_effect=AssertionError("factory should not be called")):
            engine = Engine(
                workflow,
                state_file=str(tmp_path / "state.json"),
                plain=True,
                backend_instance=backend,
            )

        assert engine.backend is backend

    def test_backend_instance_overrides_workflow_backend_during_run(self, tmp_path):
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")
        workflow = Workflow(
            name="test",
            backend="definitely-not-real",
            phases=[Phase(id="build", type="implement", prompt="Build.")],
        )

        with patch("juvenal.engine.create_backend", side_effect=AssertionError("factory should not be called")):
            engine = Engine(
                workflow,
                state_file=str(tmp_path / "state.json"),
                plain=True,
                backend_instance=backend,
            )
            assert engine.run() == 0

        assert backend.calls == ["Build."]

    def test_engine_preserves_existing_positional_resume_argument(self, tmp_path):
        from juvenal.state import PipelineState

        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="phase-1", type="implement", prompt="One."),
                Phase(id="phase-2", type="implement", prompt="Two."),
            ],
        )
        state_file = tmp_path / "state.json"
        state = PipelineState(state_file=state_file)
        state.mark_completed("phase-1")
        state.save()

        with patch("juvenal.engine.create_backend", return_value=MockBackend()):
            engine = Engine(workflow, True, state_file=str(state_file), plain=True)

        assert engine._start_idx == 1


class TestStaticWorkflowPhase:
    def test_workflow_file_success(self, tmp_path):
        """Static workflow_file phase loads and executes the sub-workflow."""
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text("name: sub\nphases:\n  - id: inner\n    prompt: 'Inner task.'\n")

        backend = MockBackend()
        backend.add_response(exit_code=0, output="inner done")

        workflow = Workflow(
            name="test",
            phases=[Phase(id="sub-wf", type="workflow", workflow_file=str(sub_yaml))],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend
        assert engine.run() == 0
        assert len(backend.calls) == 1
        assert "Inner task." in backend.calls[0]

    def test_workflow_dir_success(self, tmp_path):
        """Static workflow_dir phase loads and executes the sub-workflow."""
        sub_dir = tmp_path / "sub"
        sub_dir.mkdir()
        phases_dir = sub_dir / "phases"
        phases_dir.mkdir()
        p = phases_dir / "01-build"
        p.mkdir()
        (p / "prompt.md").write_text("Build inner.")

        backend = MockBackend()
        backend.add_response(exit_code=0, output="built")

        workflow = Workflow(
            name="test",
            phases=[Phase(id="sub-wf", type="workflow", workflow_dir=str(sub_dir))],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend
        assert engine.run() == 0
        assert "Build inner." in backend.calls[0]

    def test_static_workflow_failure_bounces(self, tmp_path):
        """Static sub-workflow failure bounces back."""
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text("name: sub\nphases:\n  - id: inner\n    prompt: 'Inner.'\n")

        backend = MockBackend()
        backend.add_response(exit_code=1, output="crash")  # sub-workflow inner fails
        backend.add_response(exit_code=0, output="done")  # retry setup after bounce
        backend.add_response(exit_code=0, output="inner ok")  # sub-workflow inner succeeds

        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Setup."),
                Phase(id="sub-wf", type="workflow", workflow_file=str(sub_yaml), bounce_target="setup"),
            ],
            max_bounces=3,
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend
        assert engine.run() == 0

    def test_static_workflow_respects_max_depth(self, tmp_path):
        """Static sub-workflows respect recursion depth limits."""
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text("name: sub\nphases:\n  - id: inner\n    prompt: 'Inner.'\n")

        backend = MockBackend()

        workflow = Workflow(
            name="test",
            phases=[Phase(id="sub-wf", type="workflow", workflow_file=str(sub_yaml), max_depth=1)],
            max_bounces=1,
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True, _depth=1, _max_depth=1)
        engine.backend = backend
        assert engine.run() == 1
        assert len(backend.calls) == 0

    def test_static_workflow_inherits_vars(self, tmp_path):
        """Parent workflow vars are propagated to static sub-workflows."""
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text("name: sub\nphases:\n  - id: inner\n    prompt: 'Deploy {{ENV}}.'\n")

        backend = MockBackend()
        backend.add_response(exit_code=0, output="deployed")

        workflow = Workflow(
            name="test",
            phases=[Phase(id="sub-wf", type="workflow", workflow_file=str(sub_yaml))],
            vars={"ENV": "prod"},
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend
        assert engine.run() == 0
        assert "Deploy prod." in backend.calls[0]

    def test_static_workflow_dry_run(self, tmp_path, capsys):
        """Dry run shows workflow_file path."""
        workflow = Workflow(
            name="test",
            phases=[Phase(id="sub-wf", type="workflow", workflow_file="/path/to/sub.yaml")],
        )
        engine = Engine(workflow, dry_run=True, state_file=str(tmp_path / "state.json"), plain=True)
        engine.run()
        captured = capsys.readouterr()
        assert "/path/to/sub.yaml" in captured.out

    def test_static_workflow_inherits_backend_and_execution_flags(self, tmp_path):
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text("name: sub\nphases:\n  - id: inner\n    prompt: 'Inner.'\n")
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")
        created_engines = []
        original_init = Engine.__init__

        def tracking_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            created_engines.append(self)

        workflow = Workflow(
            name="test",
            phases=[Phase(id="sub-wf", type="workflow", workflow_file=str(sub_yaml))],
            working_dir=str(project_dir),
        )

        with patch.object(Engine, "__init__", tracking_init):
            with patch("juvenal.engine.create_backend", side_effect=AssertionError("factory should not be called")):
                engine = Engine(
                    workflow,
                    state_file=str(tmp_path / "state.json"),
                    plain=True,
                    serialize=True,
                    interactive=True,
                    clear_context_on_bounce=True,
                    backend_instance=backend,
                )
                assert engine.run() == 0

        child = next(created for created in created_engines if created._depth == 1)
        assert child.backend is backend
        assert child.workflow.working_dir == str(project_dir)
        assert child.serialize is True
        assert child.interactive is True
        assert child.preserve_context_on_bounce is False


class TestInteractiveMode:
    def _make_engine(self, workflow, backend, tmp_path, **kwargs):
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), **kwargs)
        engine.backend = backend
        return engine

    def test_interactive_completes_immediately_on_sentinel(self, tmp_path):
        """Agent emits PLAN_COMPLETE on first run — no Q&A needed."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="All clear.\nPLAN_COMPLETE", session_id="s1")
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # checker

        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="refine", type="implement", prompt="Refine the plan.", interactive=True),
                Phase(id="review", type="check", prompt="Review.\nVERDICT: PASS or FAIL", bounce_target="refine"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path, interactive=True, plain=True)
        assert engine.run() == 0
        # First call is the interactive run_agent, second is the checker
        assert len(backend.calls) == 2
        assert "interactive mode" in backend.calls[0].lower()

    def test_interactive_qa_loop(self, tmp_path, monkeypatch):
        """Agent asks a question, user answers, agent emits PLAN_COMPLETE."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="What database should we use?", session_id="s1")
        backend.add_response(exit_code=0, output="Got it, using postgres.\nPLAN_COMPLETE", session_id="s1")
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # checker

        monkeypatch.setattr("builtins.input", lambda prompt="": "postgres")

        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="refine", type="implement", prompt="Refine.", interactive=True),
                Phase(id="review", type="check", prompt="Review.\nVERDICT: PASS or FAIL", bounce_target="refine"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path, interactive=True, plain=True)
        assert engine.run() == 0
        # run_agent for interactive, resume_agent for user answer, run_agent for checker
        assert len(backend.calls) == 2  # interactive + checker
        assert len(backend.resume_calls) == 1
        assert backend.resume_calls[0] == ("s1", "postgres")

    def test_interactive_multiple_questions(self, tmp_path, monkeypatch):
        """Agent asks multiple questions one at a time."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="What language?", session_id="s1")
        backend.add_response(exit_code=0, output="What framework?", session_id="s1")
        backend.add_response(exit_code=0, output="Done.\nPLAN_COMPLETE", session_id="s1")

        answers = iter(["python", "flask"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))

        workflow = Workflow(
            name="test",
            phases=[Phase(id="refine", type="implement", prompt="Refine.", interactive=True)],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path, interactive=True, plain=True)
        assert engine.run() == 0
        assert len(backend.resume_calls) == 2

    def test_interactive_skipped_when_engine_not_interactive(self, tmp_path):
        """Phase with interactive=True uses normal run_agent when engine interactive=False."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")  # normal implement
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # checker

        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="refine", type="implement", prompt="Refine.", interactive=True),
                Phase(id="review", type="check", prompt="Review.\nVERDICT: PASS or FAIL", bounce_target="refine"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path, interactive=False, plain=True)
        assert engine.run() == 0
        assert len(backend.calls) == 2  # normal run_agent for both

    def test_interactive_crash_bounces(self, tmp_path):
        """Interactive agent crash bounces back."""
        backend = MockBackend()
        backend.add_response(exit_code=1, output="crash", session_id="s1")  # crash
        backend.add_response(exit_code=0, output="Fixed.\nPLAN_COMPLETE", session_id="s2")  # retry
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # checker

        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="refine", type="implement", prompt="Refine.", interactive=True),
                Phase(id="review", type="check", prompt="Review.\nVERDICT: PASS or FAIL", bounce_target="refine"),
            ],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path, interactive=True, plain=True)
        assert engine.run() == 0

    def test_interactive_tracks_session_id(self, tmp_path):
        """Interactive session stores session_id for future reference."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="PLAN_COMPLETE", session_id="tracked-sess")

        workflow = Workflow(
            name="test",
            phases=[Phase(id="refine", type="implement", prompt="Refine.", interactive=True)],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path, interactive=True, plain=True)
        assert engine.run() == 0
        assert engine._session_ids["refine"] == "tracked-sess"

    def test_interactive_bounce_from_checker_relaunches(self, tmp_path):
        """When checker FAILs, interactive phase re-runs the Q&A loop."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="PLAN_COMPLETE", session_id="s1")  # first interactive
        backend.add_response(exit_code=0, output="VERDICT: FAIL: needs work")  # checker fails
        backend.add_response(exit_code=0, output="PLAN_COMPLETE", session_id="s2")  # re-run interactive
        backend.add_response(exit_code=0, output="VERDICT: PASS")  # checker passes

        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="refine", type="implement", prompt="Refine.", interactive=True),
                Phase(id="review", type="check", prompt="Review.\nVERDICT: PASS or FAIL", bounce_target="refine"),
            ],
            max_bounces=5,
        )
        engine = self._make_engine(workflow, backend, tmp_path, interactive=True, plain=True)
        assert engine.run() == 0
        # Two interactive run_agent calls + two checker run_agent calls
        assert len(backend.calls) == 4

    def test_interactive_logs_step(self, tmp_path):
        """Interactive session logs a step with type 'interactive'."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="PLAN_COMPLETE", session_id="s1")

        workflow = Workflow(
            name="test",
            phases=[Phase(id="refine", type="implement", prompt="Refine.", interactive=True)],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path, interactive=True, plain=True)
        assert engine.run() == 0
        ps = engine.state.phases["refine"]
        assert any(entry.get("step") == "interactive" for entry in ps.logs)

    def test_interactive_user_eof_exits_gracefully(self, tmp_path, monkeypatch):
        """Ctrl+D (EOF) during input exits the interactive loop gracefully."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="What database?", session_id="s1")

        monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError))

        workflow = Workflow(
            name="test",
            phases=[Phase(id="refine", type="implement", prompt="Refine.", interactive=True)],
            max_bounces=3,
        )
        engine = self._make_engine(workflow, backend, tmp_path, interactive=True, plain=True)
        assert engine.run() == 0


class TestPlanWorkflowInternal:
    def test_plan_workflow_internal_resume_keeps_existing_goal_file(self, tmp_path):
        backend = MockBackend()
        plan_dir = tmp_path / ".plan"
        plan_dir.mkdir()
        goal_path = plan_dir / "goal.md"
        goal_path.write_text("existing goal")

        with patch.object(Engine, "run", return_value=1):
            _plan_workflow_internal(
                goal="new goal",
                backend_instance=backend,
                plain=True,
                project_dir=str(tmp_path),
                resume=True,
            )

        assert goal_path.read_text() == "existing goal"

    def test_project_dir_creates_plan_directory(self, tmp_path):
        """When project_dir is set, .plan/ is created there with goal.md."""
        backend = MockBackend()
        # Plan pipeline will fail immediately since mock doesn't produce artifacts,
        # but we can verify .plan/ directory creation
        with patch.object(Engine, "run", return_value=1):
            _plan_workflow_internal(
                goal="test goal",
                backend_instance=backend,
                plain=True,
                project_dir=str(tmp_path),
            )
        plan_dir = tmp_path / ".plan"
        assert plan_dir.exists()
        assert (plan_dir / "goal.md").exists()
        assert (plan_dir / "goal.md").read_text() == "test goal"

    def test_temp_dir_used_without_project_dir(self):
        """When project_dir is None, a temp dir is used."""
        backend = MockBackend()
        with patch.object(Engine, "run", return_value=1):
            result = _plan_workflow_internal(
                goal="test goal",
                backend_instance=backend,
                plain=True,
            )
        # temp_dir should be set when no project_dir
        assert result.temp_dir is not None
        # Clean up
        import shutil

        if result.temp_dir:
            shutil.rmtree(result.temp_dir, ignore_errors=True)

    def test_clear_context_on_bounce_is_set(self, tmp_path):
        """Plan engine uses clear_context_on_bounce=True."""
        backend = MockBackend()
        engines_created = []
        original_init = Engine.__init__

        def tracking_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            engines_created.append(self)

        with patch.object(Engine, "__init__", tracking_init):
            with patch.object(Engine, "run", return_value=1):
                _plan_workflow_internal(
                    goal="test goal",
                    backend_instance=backend,
                    plain=True,
                    project_dir=str(tmp_path),
                )

        assert len(engines_created) >= 1
        # The plan engine should not preserve context on bounce
        assert engines_created[0].preserve_context_on_bounce is False

    def test_interactive_flag_passed_to_engine(self, tmp_path):
        """Interactive flag is passed through to the engine."""
        backend = MockBackend()
        engines_created = []
        original_init = Engine.__init__

        def tracking_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            engines_created.append(self)

        with patch.object(Engine, "__init__", tracking_init):
            with patch.object(Engine, "run", return_value=1):
                _plan_workflow_internal(
                    goal="test goal",
                    backend_instance=backend,
                    plain=True,
                    project_dir=str(tmp_path),
                    interactive=True,
                )

        assert len(engines_created) >= 1
        assert engines_created[0].interactive is True

    def test_serialize_flag_passed_to_engine(self, tmp_path):
        backend = MockBackend()
        engines_created = []
        original_init = Engine.__init__

        def tracking_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            engines_created.append(self)

        with patch.object(Engine, "__init__", tracking_init):
            with patch.object(Engine, "run", return_value=1):
                _plan_workflow_internal(
                    goal="test goal",
                    backend_instance=backend,
                    plain=True,
                    project_dir=str(tmp_path),
                    serialize=True,
                )

        assert len(engines_created) >= 1
        assert engines_created[0].serialize is True

    def test_backend_instance_is_passed_into_plan_engine(self, tmp_path):
        backend = MockBackend()
        seen_backends = []
        backend_factory = Mock(side_effect=AssertionError("factory should not be called"))

        def fake_run(self):
            seen_backends.append(self.backend)
            return 1

        with patch("juvenal.engine.create_backend", backend_factory):
            with patch.object(Engine, "run", autospec=True, side_effect=fake_run):
                result = _plan_workflow_internal(
                    goal="test goal",
                    backend_instance=backend,
                    plain=True,
                    project_dir=str(tmp_path),
                )

        assert result.success is False
        assert backend_factory.call_count == 0
        assert seen_backends == [backend]
