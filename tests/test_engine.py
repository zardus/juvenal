"""Unit tests for the execution engine with mocked backend."""

from pathlib import Path
from unittest.mock import patch

import pytest

from juvenal.checkers import parse_verdict
from juvenal.engine import BounceCounter, Engine, _extract_yaml
from juvenal.workflow import ParallelGroup, Phase, Workflow, inject_checkers, inject_implementer
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
        """With preserve_context_on_bounce, bouncing back resumes the session."""
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
        engine = self._make_engine(workflow, backend, tmp_path, preserve_context_on_bounce=True)
        assert engine.run() == 0

        # First call is run_agent (fresh), second implement call should be resume_agent
        assert len(backend.calls) == 3  # implement + check + check (2nd)
        assert len(backend.resume_calls) == 1
        session_id, prompt = backend.resume_calls[0]
        assert session_id == "sess-1"
        assert "failed verification" in prompt

    def test_bounce_without_flag_uses_run_agent(self, tmp_path):
        """Without preserve_context_on_bounce, bouncing back starts fresh."""
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
        engine = self._make_engine(workflow, backend, tmp_path)
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
        engine = self._make_engine(workflow, backend, tmp_path, preserve_context_on_bounce=True)
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
        engine = self._make_engine(workflow, backend, tmp_path, preserve_context_on_bounce=True)
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
        engine = self._make_engine(workflow, backend, tmp_path, preserve_context_on_bounce=True)
        # Will exhaust bounces since "false" always fails, but we check resume was used
        engine.run()

        assert len(backend.resume_calls) == 1
        assert backend.resume_calls[0][0] == "sess-1"

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
        engine = self._make_engine(workflow, backend, tmp_path, preserve_context_on_bounce=True)
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
    def test_plan_creates_temp_structure(self, tmp_path):
        """plan_workflow creates temp dir with .plan/goal.md and runs Engine."""
        from unittest.mock import MagicMock, patch

        from juvenal.engine import plan_workflow

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

    def test_plan_copies_output_on_success(self, tmp_path):
        """plan_workflow copies workflow.yaml to output path and cleans up."""
        from unittest.mock import MagicMock, patch

        from juvenal.engine import plan_workflow

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

    def test_plan_fails_on_engine_failure(self, tmp_path):
        """plan_workflow raises SystemExit if engine returns non-zero."""
        from unittest.mock import MagicMock, patch

        from juvenal.engine import plan_workflow

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
            preserve_context_on_bounce=True,
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

    def test_vars_unrecognized_passthrough(self, mock_backend, tmp_path):
        """Unrecognized {{VAR}} placeholders pass through unchanged."""
        mock_backend.add_response(exit_code=0, output="done")
        workflow = Workflow(
            name="test",
            phases=[Phase(id="build", type="implement", prompt="Use {{KNOWN}} and {{UNKNOWN}}.")],
            vars={"KNOWN": "value"},
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = mock_backend
        engine.run()
        assert "Use value and {{UNKNOWN}}." in mock_backend.calls[0]

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
