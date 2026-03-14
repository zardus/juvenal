"""Unit tests for state persistence."""

import json
from pathlib import Path

from juvenal.state import PipelineState
from juvenal.workflow import Phase


class TestAtomicPersistence:
    def test_save_and_load(self, tmp_path):
        state_file = tmp_path / "state.json"
        state = PipelineState(state_file=state_file)
        state.set_attempt("setup", 1)
        state.mark_completed("setup")

        loaded = PipelineState.load(state_file)
        assert "setup" in loaded.phases
        assert loaded.phases["setup"].status == "completed"
        assert loaded.phases["setup"].attempt == 1

    def test_atomic_write_creates_no_tmp(self, tmp_path):
        state_file = tmp_path / "state.json"
        state = PipelineState(state_file=state_file)
        state.set_attempt("phase1", 1)

        # After save, there should be no .tmp file
        tmp_file = state_file.with_name(f"{state_file.name}.tmp")
        assert not tmp_file.exists()
        assert state_file.exists()

    def test_save_produces_valid_json(self, tmp_path):
        state_file = tmp_path / "state.json"
        state = PipelineState(state_file=state_file)
        state.set_attempt("setup", 1)
        state.log_step("setup", 1, "implement", "some output")

        data = json.loads(state_file.read_text())
        assert "phases" in data
        assert "setup" in data["phases"]
        assert data["phases"]["setup"]["attempt"] == 1


class TestResumeLogic:
    def test_resume_from_beginning(self, tmp_path):
        state = PipelineState(state_file=tmp_path / "state.json")
        phases = [Phase(id="a", prompt=""), Phase(id="b", prompt="")]
        assert state.get_resume_phase_index(phases) == 0

    def test_resume_after_first_completed(self, tmp_path):
        state = PipelineState(state_file=tmp_path / "state.json")
        state.set_attempt("a", 1)
        state.mark_completed("a")
        phases = [Phase(id="a", prompt=""), Phase(id="b", prompt="")]
        assert state.get_resume_phase_index(phases) == 1

    def test_resume_all_completed(self, tmp_path):
        state = PipelineState(state_file=tmp_path / "state.json")
        state.mark_completed("a")
        state.mark_completed("b")
        phases = [Phase(id="a", prompt=""), Phase(id="b", prompt="")]
        assert state.get_resume_phase_index(phases) == 2


class TestFailureContext:
    def test_set_and_get_failure_context(self, tmp_path):
        state = PipelineState(state_file=tmp_path / "state.json")
        state.set_failure_context("phase1", "tests failed", attempt=1)
        assert state.get_failure_context("phase1") == "tests failed"

    def test_failure_contexts_are_per_attempt(self, tmp_path):
        state = PipelineState(state_file=tmp_path / "state.json")
        state.set_failure_context("phase1", "first failure", attempt=1)
        state.set_failure_context("phase1", "second failure", attempt=2)
        ps = state.phases["phase1"]
        assert len(ps.failure_contexts) == 2
        assert ps.failure_contexts[0]["context"] == "first failure"
        assert ps.failure_contexts[0]["attempt"] == 1
        assert ps.failure_contexts[1]["context"] == "second failure"
        assert ps.failure_contexts[1]["attempt"] == 2
        # get_failure_context returns latest
        assert state.get_failure_context("phase1") == "second failure"

    def test_failure_contexts_persist_across_save_load(self, tmp_path):
        state = PipelineState(state_file=tmp_path / "state.json")
        state.set_failure_context("phase1", "fail one", attempt=1)
        state.set_failure_context("phase1", "fail two", attempt=2)
        loaded = PipelineState.load(tmp_path / "state.json")
        ps = loaded.phases["phase1"]
        assert len(ps.failure_contexts) == 2
        assert ps.failure_contexts[0]["context"] == "fail one"
        assert ps.failure_contexts[1]["context"] == "fail two"

    def test_get_nonexistent_phase(self, tmp_path):
        state = PipelineState(state_file=tmp_path / "state.json")
        assert state.get_failure_context("nonexistent") == ""

    def test_backwards_compat_scalar_failure_context(self, tmp_path):
        """Old state files with scalar failure_context are migrated on load."""
        import json

        state_file = tmp_path / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "started_at": None,
                    "completed_at": None,
                    "phases": {
                        "build": {
                            "status": "pending",
                            "attempt": 1,
                            "failure_context": "old failure",
                            "logs": [],
                        }
                    },
                }
            )
        )
        loaded = PipelineState.load(state_file)
        assert loaded.get_failure_context("build") == "old failure"
        assert len(loaded.phases["build"].failure_contexts) == 1


class TestInvalidation:
    def test_invalidate_from(self, tmp_path):
        state = PipelineState(state_file=tmp_path / "state.json")
        state.mark_completed("a")
        state.mark_completed("b")
        state.mark_completed("c")
        state.invalidate_from("b")

        assert state.phases["a"].status == "completed"
        assert state.phases["b"].status == "pending"
        assert state.phases["c"].status == "pending"

    def test_invalidate_preserves_attempt_count(self, tmp_path):
        """invalidate_from should not reset the attempt counter."""
        state = PipelineState(state_file=tmp_path / "state.json")
        state.set_attempt("a", 1)
        state.mark_completed("a")
        state.set_attempt("b", 2)
        state.mark_completed("b")
        state.invalidate_from("b")

        assert state.phases["a"].attempt == 1  # untouched
        assert state.phases["b"].attempt == 2  # preserved through invalidation


class TestBaselineSha:
    def test_baseline_sha_default_none(self, tmp_path):
        state = PipelineState(state_file=tmp_path / "state.json")
        ps = state._ensure_phase("setup")
        assert ps.baseline_sha is None

    def test_baseline_sha_persisted(self, tmp_path):
        state_file = tmp_path / "state.json"
        state = PipelineState(state_file=state_file)
        ps = state._ensure_phase("setup")
        ps.baseline_sha = "abc123"
        state.save()

        loaded = PipelineState.load(state_file)
        assert loaded.phases["setup"].baseline_sha == "abc123"

    def test_invalidate_preserves_baseline_sha(self, tmp_path):
        """invalidate_from should not reset baseline_sha."""
        state = PipelineState(state_file=tmp_path / "state.json")
        ps_a = state._ensure_phase("a")
        ps_a.baseline_sha = "sha-a"
        state.mark_completed("a")
        ps_b = state._ensure_phase("b")
        ps_b.baseline_sha = "sha-b"
        state.mark_completed("b")
        state.invalidate_from("b")

        assert state.phases["a"].baseline_sha == "sha-a"  # untouched
        assert state.phases["b"].baseline_sha == "sha-b"  # preserved through invalidation
        assert state.phases["b"].status == "pending"


class TestScopedInvalidation:
    def test_invalidate_from_with_scope(self, tmp_path):
        """Scoped invalidation only affects target phases."""
        state = PipelineState(state_file=tmp_path / "state.json")
        state.mark_completed("a")
        state.mark_completed("b")
        state.mark_completed("c")
        state.mark_completed("d")

        # Invalidate from "b" but only in scope {b, c}
        state.invalidate_from("b", scope={"b", "c"})

        assert state.phases["a"].status == "completed"
        assert state.phases["b"].status == "pending"
        assert state.phases["c"].status == "pending"
        assert state.phases["d"].status == "completed"  # outside scope, preserved

    def test_invalidate_from_none_scope_affects_all(self, tmp_path):
        """Without scope, invalidation works as before (affects all from target)."""
        state = PipelineState(state_file=tmp_path / "state.json")
        state.mark_completed("a")
        state.mark_completed("b")
        state.mark_completed("c")

        state.invalidate_from("b", scope=None)

        assert state.phases["a"].status == "completed"
        assert state.phases["b"].status == "pending"
        assert state.phases["c"].status == "pending"


class TestConcurrentStateWrites:
    def test_concurrent_state_writes(self, tmp_path):
        """Multiple threads writing state concurrently without corruption."""
        import threading

        state = PipelineState(state_file=tmp_path / "state.json")
        errors = []

        def writer(phase_prefix, count):
            try:
                for i in range(count):
                    pid = f"{phase_prefix}-{i}"
                    state.set_attempt(pid, 1)
                    state.mark_completed(pid)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=("lane-a", 10)),
            threading.Thread(target=writer, args=("lane-b", 10)),
            threading.Thread(target=writer, args=("lane-c", 10)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # All 30 phases should be completed
        assert len(state.phases) == 30
        for ps in state.phases.values():
            assert ps.status == "completed"

        # File should be valid JSON
        loaded = PipelineState.load(tmp_path / "state.json")
        assert len(loaded.phases) == 30


class TestLoadEmpty:
    def test_load_nonexistent(self, tmp_path):
        state = PipelineState.load(tmp_path / "nonexistent.json")
        assert len(state.phases) == 0

    def test_load_none(self):
        state = PipelineState.load(None)
        assert state.state_file == Path(".juvenal-state.json")


class TestLogStep:
    def test_log_step_appends(self, tmp_path):
        state = PipelineState(state_file=tmp_path / "state.json")
        state.log_step("build", 1, "implement", "output text", input="prompt text")
        ps = state.phases["build"]
        assert len(ps.logs) == 1
        assert ps.logs[0]["step"] == "implement"
        assert ps.logs[0]["output"] == "output text"
        assert ps.logs[0]["input"] == "prompt text"
        assert ps.logs[0]["attempt"] == 1

    def test_log_step_multiple(self, tmp_path):
        state = PipelineState(state_file=tmp_path / "state.json")
        state.log_step("build", 1, "implement", "out1")
        state.log_step("build", 1, "check", "out2")
        assert len(state.phases["build"].logs) == 2

    def test_log_step_persists(self, tmp_path):
        state_file = tmp_path / "state.json"
        state = PipelineState(state_file=state_file)
        state.log_step("build", 1, "implement", "output", transcript="full transcript")
        loaded = PipelineState.load(state_file)
        assert len(loaded.phases["build"].logs) == 1
        assert loaded.phases["build"].logs[0]["transcript"] == "full transcript"

    def test_log_step_omits_empty_input(self, tmp_path):
        state = PipelineState(state_file=tmp_path / "state.json")
        state.log_step("build", 1, "implement", "output")
        assert "input" not in state.phases["build"].logs[0]


class TestTokenAccumulation:
    def test_add_tokens(self, tmp_path):
        state = PipelineState(state_file=tmp_path / "state.json")
        state.add_tokens("build", 100, 50)
        state.add_tokens("build", 200, 100)
        assert state.phases["build"].input_tokens == 300
        assert state.phases["build"].output_tokens == 150

    def test_total_tokens(self, tmp_path):
        state = PipelineState(state_file=tmp_path / "state.json")
        state.add_tokens("a", 100, 50)
        state.add_tokens("b", 200, 100)
        inp, out = state.total_tokens()
        assert inp == 300
        assert out == 150

    def test_total_tokens_empty(self, tmp_path):
        state = PipelineState(state_file=tmp_path / "state.json")
        assert state.total_tokens() == (0, 0)


class TestCorruptedState:
    def test_load_malformed_json(self, tmp_path):
        """Malformed JSON state file raises an error."""
        state_file = tmp_path / "state.json"
        state_file.write_text("{broken")
        import pytest

        with pytest.raises(json.JSONDecodeError):
            PipelineState.load(state_file)
