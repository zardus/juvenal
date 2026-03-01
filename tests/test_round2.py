"""Tests for Round 2 features: includes, cost tracking, backoff, notifications, enhanced dry-run."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from unittest.mock import patch

import pytest

from juvenal.backends import AgentResult, _extract_claude_tokens, _extract_codex_tokens
from juvenal.engine import Engine
from juvenal.notifications import build_notification_payload, send_webhook
from juvenal.state import PipelineState
from juvenal.workflow import Phase, Workflow, load_workflow, validate_workflow
from tests.conftest import MockBackend

# ─── Feature 1: Workflow Includes ───────────────────────────────────────────


class TestWorkflowIncludes:
    def test_basic_include(self, tmp_path):
        """Include merges phases from the referenced workflow."""
        # Create base workflow
        base_yaml = tmp_path / "base.yaml"
        base_yaml.write_text(
            """\
name: base
phases:
  - id: setup
    prompt: "Set up."
"""
        )
        # Create main workflow that includes base
        main_yaml = tmp_path / "main.yaml"
        main_yaml.write_text(
            """\
name: main
include:
  - base.yaml
phases:
  - id: build
    prompt: "Build it."
"""
        )
        wf = load_workflow(main_yaml)
        assert wf.name == "main"
        assert len(wf.phases) == 2
        assert wf.phases[0].id == "setup"
        assert wf.phases[1].id == "build"

    def test_multiple_includes(self, tmp_path):
        """Multiple includes are merged in order."""
        (tmp_path / "a.yaml").write_text("name: a\nphases:\n  - id: phase-a\n    prompt: A.\n")
        (tmp_path / "b.yaml").write_text("name: b\nphases:\n  - id: phase-b\n    prompt: B.\n")
        (tmp_path / "main.yaml").write_text(
            """\
name: main
include:
  - a.yaml
  - b.yaml
phases:
  - id: phase-c
    prompt: "C."
"""
        )
        wf = load_workflow(tmp_path / "main.yaml")
        assert [p.id for p in wf.phases] == ["phase-a", "phase-b", "phase-c"]

    def test_nested_includes(self, tmp_path):
        """Includes can themselves include other files."""
        (tmp_path / "leaf.yaml").write_text("name: leaf\nphases:\n  - id: leaf-phase\n    prompt: Leaf.\n")
        (tmp_path / "mid.yaml").write_text(
            "name: mid\ninclude:\n  - leaf.yaml\nphases:\n  - id: mid-phase\n    prompt: Mid.\n"
        )
        (tmp_path / "top.yaml").write_text(
            "name: top\ninclude:\n  - mid.yaml\nphases:\n  - id: top-phase\n    prompt: Top.\n"
        )
        wf = load_workflow(tmp_path / "top.yaml")
        assert [p.id for p in wf.phases] == ["leaf-phase", "mid-phase", "top-phase"]

    def test_circular_include_detected(self, tmp_path):
        """Circular includes raise ValueError."""
        (tmp_path / "a.yaml").write_text("name: a\ninclude:\n  - b.yaml\nphases:\n  - id: pa\n    prompt: A.\n")
        (tmp_path / "b.yaml").write_text("name: b\ninclude:\n  - a.yaml\nphases:\n  - id: pb\n    prompt: B.\n")
        with pytest.raises(ValueError, match="Circular include"):
            load_workflow(tmp_path / "a.yaml")

    def test_include_missing_file(self, tmp_path):
        """Including a non-existent file raises FileNotFoundError."""
        (tmp_path / "main.yaml").write_text(
            "name: main\ninclude:\n  - nonexistent.yaml\nphases:\n  - id: a\n    prompt: A.\n"
        )
        with pytest.raises(FileNotFoundError, match="nonexistent.yaml"):
            load_workflow(tmp_path / "main.yaml")

    def test_include_inherits_parallel_groups(self, tmp_path):
        """Parallel groups from included workflows are merged."""
        (tmp_path / "base.yaml").write_text(
            """\
name: base
phases:
  - id: a
    prompt: "A."
  - id: b
    prompt: "B."
parallel_groups:
  - phases: [a, b]
"""
        )
        (tmp_path / "main.yaml").write_text("name: main\ninclude:\n  - base.yaml\nphases:\n  - id: c\n    prompt: C.\n")
        wf = load_workflow(tmp_path / "main.yaml")
        assert ["a", "b"] in wf.parallel_groups

    def test_no_includes(self, tmp_path):
        """Workflow without include key works normally."""
        (tmp_path / "simple.yaml").write_text("name: simple\nphases:\n  - id: a\n    prompt: A.\n")
        wf = load_workflow(tmp_path / "simple.yaml")
        assert len(wf.phases) == 1


# ─── Feature 2: Cost Tracking ───────────────────────────────────────────────


class TestCostTracking:
    def test_agent_result_has_token_fields(self):
        """AgentResult tracks input and output tokens."""
        result = AgentResult(
            exit_code=0,
            output="done",
            transcript="",
            duration=1.0,
            input_tokens=100,
            output_tokens=50,
        )
        assert result.input_tokens == 100
        assert result.output_tokens == 50

    def test_agent_result_defaults_to_zero(self):
        """Token fields default to 0."""
        result = AgentResult(exit_code=0, output="done", transcript="", duration=1.0)
        assert result.input_tokens == 0
        assert result.output_tokens == 0

    def test_extract_claude_tokens(self):
        """Claude result event extracts token usage."""
        event = {"type": "result", "usage": {"input_tokens": 500, "output_tokens": 200}}
        assert _extract_claude_tokens(event) == (500, 200)

    def test_extract_claude_tokens_no_usage(self):
        """Non-result events return zero tokens."""
        assert _extract_claude_tokens({"type": "assistant"}) == (0, 0)

    def test_extract_codex_tokens(self):
        """Codex turn.completed event extracts token usage."""
        event = {"type": "turn.completed", "usage": {"input_tokens": 300, "output_tokens": 100}}
        assert _extract_codex_tokens(event) == (300, 100)

    def test_extract_codex_tokens_no_usage(self):
        """Non turn.completed events return zero tokens."""
        assert _extract_codex_tokens({"type": "item.completed"}) == (0, 0)

    def test_state_tracks_tokens(self, tmp_path):
        """PipelineState accumulates token counts per phase."""
        state = PipelineState(state_file=tmp_path / "state.json")
        state.add_tokens("phase1", 100, 50)
        state.add_tokens("phase1", 200, 75)
        assert state.phases["phase1"].input_tokens == 300
        assert state.phases["phase1"].output_tokens == 125

    def test_state_total_tokens(self, tmp_path):
        """total_tokens() sums across all phases."""
        state = PipelineState(state_file=tmp_path / "state.json")
        state.add_tokens("phase1", 100, 50)
        state.add_tokens("phase2", 200, 75)
        assert state.total_tokens() == (300, 125)

    def test_state_tokens_persisted(self, tmp_path):
        """Token counts survive save/load."""
        state_file = tmp_path / "state.json"
        state = PipelineState(state_file=state_file)
        state.add_tokens("phase1", 500, 200)
        state.save()

        loaded = PipelineState.load(state_file)
        assert loaded.phases["phase1"].input_tokens == 500
        assert loaded.phases["phase1"].output_tokens == 200

    def test_engine_tracks_tokens_for_implement(self, tmp_path):
        """Engine records token counts from implement phases."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done", input_tokens=100, output_tokens=50)
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-check", type="script", run="true"),
            ],
            max_bounces=3,
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"))
        engine.backend = backend
        engine.run()
        assert engine.state.phases["setup"].input_tokens == 100
        assert engine.state.phases["setup"].output_tokens == 50

    def test_engine_tracks_tokens_for_check(self, tmp_path):
        """Engine records token counts from check phases."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="implemented", input_tokens=100, output_tokens=50)
        backend.add_response(exit_code=0, output="VERDICT: PASS", input_tokens=80, output_tokens=30)
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="review", type="check", role="tester"),
            ],
            max_bounces=3,
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"))
        engine.backend = backend
        engine.run()
        assert engine.state.phases["review"].input_tokens == 80
        assert engine.state.phases["review"].output_tokens == 30

    def test_run_summary_shows_tokens(self, tmp_path, capsys):
        """Run summary includes token info when available."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done", input_tokens=500, output_tokens=200)
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-check", type="script", run="true"),
            ],
            max_bounces=3,
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend
        engine.run()
        captured = capsys.readouterr()
        assert "tokens: 500 in, 200 out" in captured.out
        assert "Total tokens: 500 in, 200 out" in captured.out


# ─── Feature 3: Exponential Backoff ─────────────────────────────────────────


class TestExponentialBackoff:
    def test_workflow_backoff_fields(self):
        """Workflow has backoff and max_backoff fields."""
        wf = Workflow(name="test", phases=[], backoff=2.0, max_backoff=30.0)
        assert wf.backoff == 2.0
        assert wf.max_backoff == 30.0

    def test_workflow_backoff_defaults(self):
        """Backoff defaults to 0 (disabled)."""
        wf = Workflow(name="test", phases=[])
        assert wf.backoff == 0.0
        assert wf.max_backoff == 60.0

    def test_backoff_from_yaml(self, tmp_path):
        """Backoff is loaded from YAML."""
        (tmp_path / "wf.yaml").write_text(
            "name: test\nbackoff: 1.5\nmax_backoff: 20\nphases:\n  - id: a\n    prompt: A.\n"
        )
        wf = load_workflow(tmp_path / "wf.yaml")
        assert wf.backoff == 1.5
        assert wf.max_backoff == 20.0

    def test_backoff_applied_on_bounce(self, tmp_path):
        """Engine sleeps with exponential backoff between bounces."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")  # implement
        # Script fails -> bounce
        backend.add_response(exit_code=0, output="done")  # implement retry
        # Script fails again -> exhausted
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-check", type="script", run="false"),
            ],
            max_bounces=2,
            backoff=0.01,  # Very small for testing
            max_backoff=0.1,
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend

        with patch("juvenal.engine.time.sleep") as mock_sleep:
            engine.run()
            # Should have called sleep at least once for the bounce
            assert mock_sleep.call_count >= 1
            # First bounce: delay = 0.01 * 2^0 = 0.01
            assert mock_sleep.call_args_list[0][0][0] == pytest.approx(0.01, abs=0.001)

    def test_backoff_exponential_growth(self, tmp_path):
        """Backoff delay grows exponentially: base * 2^(n-1)."""
        backend = MockBackend()
        # 4 bounces: backoff on 1, 2, 3 then exhausted on 4
        backend.add_response(exit_code=0, output="done")  # implement
        # fail -> bounce 1
        backend.add_response(exit_code=0, output="done")  # implement
        # fail -> bounce 2
        backend.add_response(exit_code=0, output="done")  # implement
        # fail -> bounce 3
        backend.add_response(exit_code=0, output="done")  # implement
        # fail -> bounce 4 -> exhausted
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-check", type="script", run="false"),
            ],
            max_bounces=4,
            backoff=1.0,
            max_backoff=100.0,
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend

        with patch("juvenal.engine.time.sleep") as mock_sleep:
            engine.run()
            delays = [call[0][0] for call in mock_sleep.call_args_list]
            # bounce 1: 1.0 * 2^0 = 1.0
            # bounce 2: 1.0 * 2^1 = 2.0
            # bounce 3: 1.0 * 2^2 = 4.0
            # bounce 4: exhausted (no backoff)
            assert len(delays) == 3
            assert delays[0] == pytest.approx(1.0)
            assert delays[1] == pytest.approx(2.0)
            assert delays[2] == pytest.approx(4.0)

    def test_backoff_capped_at_max(self, tmp_path):
        """Backoff delay is capped at max_backoff."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")
        backend.add_response(exit_code=0, output="done")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-check", type="script", run="false"),
            ],
            max_bounces=2,
            backoff=10.0,
            max_backoff=5.0,  # Cap is lower than base * 2^n
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend

        with patch("juvenal.engine.time.sleep") as mock_sleep:
            engine.run()
            for call in mock_sleep.call_args_list:
                assert call[0][0] <= 5.0

    def test_no_backoff_when_disabled(self, tmp_path):
        """No sleep when backoff is 0 (default)."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-check", type="script", run="false"),
            ],
            max_bounces=1,
            backoff=0.0,
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend

        with patch("juvenal.engine.time.sleep") as mock_sleep:
            engine.run()
            mock_sleep.assert_not_called()

    def test_validation_rejects_negative_backoff(self):
        """Validation catches negative backoff."""
        wf = Workflow(name="test", phases=[Phase(id="a", type="implement", prompt="A.")], backoff=-1.0)
        errors = validate_workflow(wf)
        assert any("backoff" in e for e in errors)

    def test_validation_rejects_negative_max_backoff(self):
        """Validation catches negative max_backoff."""
        wf = Workflow(name="test", phases=[Phase(id="a", type="implement", prompt="A.")], max_backoff=-5.0)
        errors = validate_workflow(wf)
        assert any("max_backoff" in e for e in errors)

    def test_cli_backoff_flag(self):
        """--backoff flag is parsed."""
        from juvenal.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["run", "wf.yaml", "--backoff", "2.5"])
        assert args.backoff == 2.5

    def test_cli_backoff_default(self):
        """--backoff defaults to None."""
        from juvenal.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(["run", "wf.yaml"])
        assert args.backoff is None


# ─── Feature 4: Notifications / Webhooks ─────────────────────────────────────


class TestNotifications:
    def test_workflow_notify_field(self):
        """Workflow has a notify field."""
        wf = Workflow(name="test", phases=[], notify=["https://example.com/hook"])
        assert wf.notify == ["https://example.com/hook"]

    def test_notify_default_empty(self):
        """Notify defaults to empty list."""
        wf = Workflow(name="test", phases=[])
        assert wf.notify == []

    def test_notify_from_yaml(self, tmp_path):
        """Notify URLs are loaded from YAML."""
        (tmp_path / "wf.yaml").write_text(
            """\
name: test
notify:
  - https://example.com/hook1
  - https://example.com/hook2
phases:
  - id: a
    prompt: "A."
"""
        )
        wf = load_workflow(tmp_path / "wf.yaml")
        assert len(wf.notify) == 2

    def test_build_notification_payload(self):
        """build_notification_payload creates correct structure."""
        payload = build_notification_payload(
            workflow_name="test-wf",
            success=True,
            total_bounces=2,
            duration=45.3,
            total_input_tokens=1000,
            total_output_tokens=500,
            phase_summaries=[
                {"id": "setup", "status": "completed", "attempts": 1, "input_tokens": 500, "output_tokens": 250},
                {"id": "build", "status": "completed", "attempts": 2, "input_tokens": 500, "output_tokens": 250},
            ],
        )
        assert payload["workflow"] == "test-wf"
        assert payload["status"] == "success"
        assert payload["total_bounces"] == 2
        assert payload["duration_seconds"] == 45.3
        assert payload["total_input_tokens"] == 1000
        assert payload["total_output_tokens"] == 500
        assert len(payload["phases"]) == 2

    def test_build_notification_payload_failure(self):
        """Failure status is correctly set."""
        payload = build_notification_payload(
            workflow_name="test",
            success=False,
            total_bounces=5,
            duration=None,
            total_input_tokens=0,
            total_output_tokens=0,
            phase_summaries=[],
        )
        assert payload["status"] == "failure"
        assert payload["duration_seconds"] is None

    def test_send_webhook_to_real_server(self):
        """send_webhook() POSTs JSON to a real HTTP server."""
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                received.append(json.loads(body))
                self.send_response(200)
                self.end_headers()

            def log_message(self, format, *args):
                pass  # Suppress output

        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        thread = Thread(target=server.handle_request, daemon=True)
        thread.start()

        try:
            ok = send_webhook(f"http://127.0.0.1:{port}/hook", {"test": True})
            thread.join(timeout=5)
            assert ok
            assert len(received) == 1
            assert received[0]["test"] is True
        finally:
            server.server_close()

    def test_send_webhook_failure_returns_false(self):
        """send_webhook() returns False on network error."""
        ok = send_webhook("http://127.0.0.1:1/nonexistent", {"test": True}, timeout=1)
        assert not ok

    def test_engine_sends_notifications_on_success(self, tmp_path):
        """Engine sends notifications on successful completion."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-check", type="script", run="true"),
            ],
            max_bounces=3,
            notify=["https://example.com/hook"],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend

        with patch("juvenal.engine.send_webhook") as mock_send:
            mock_send.return_value = True
            engine.run()
            mock_send.assert_called_once()
            call_args = mock_send.call_args
            assert call_args[0][0] == "https://example.com/hook"
            payload = call_args[0][1]
            assert payload["status"] == "success"

    def test_engine_sends_notifications_on_failure(self, tmp_path):
        """Engine sends notifications on pipeline failure."""
        backend = MockBackend()
        backend.add_response(exit_code=0, output="done")
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="setup-check", type="script", run="false"),
            ],
            max_bounces=1,
            notify=["https://example.com/hook"],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend

        with patch("juvenal.engine.send_webhook") as mock_send:
            mock_send.return_value = True
            engine.run()
            mock_send.assert_called_once()
            payload = mock_send.call_args[0][1]
            assert payload["status"] == "failure"

    def test_engine_no_notifications_when_empty(self, tmp_path):
        """Engine doesn't attempt notifications when notify is empty."""
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
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), plain=True)
        engine.backend = backend

        with patch("juvenal.engine.send_webhook") as mock_send:
            engine.run()
            mock_send.assert_not_called()

    def test_validation_rejects_bad_notify_url(self):
        """Validation catches non-HTTP notify URLs."""
        wf = Workflow(
            name="test",
            phases=[Phase(id="a", type="implement", prompt="A.")],
            notify=["ftp://bad.com/hook"],
        )
        errors = validate_workflow(wf)
        assert any("notify" in e for e in errors)

    def test_validation_accepts_good_notify_url(self):
        """Valid HTTP/HTTPS URLs pass validation."""
        wf = Workflow(
            name="test",
            phases=[Phase(id="a", type="implement", prompt="A.")],
            notify=["https://hooks.example.com/juvenal"],
        )
        errors = validate_workflow(wf)
        assert errors == []

    def test_cli_notify_flag(self):
        """--notify flag is parsed (can be repeated)."""
        from juvenal.cli import build_parser

        parser = build_parser()
        args = parser.parse_args(
            [
                "run",
                "wf.yaml",
                "--notify",
                "https://a.com/hook",
                "--notify",
                "https://b.com/hook",
            ]
        )
        assert args.notify == ["https://a.com/hook", "https://b.com/hook"]


# ─── Feature 5: Enhanced Dry-Run ────────────────────────────────────────────


class TestEnhancedDryRun:
    def test_dry_run_shows_validation(self, tmp_path, capsys):
        """Dry-run shows validation results."""
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Do it."),
                Phase(id="check", type="script", run="true"),
            ],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), dry_run=True, plain=True)
        engine.run()
        captured = capsys.readouterr()
        assert "Validation: OK" in captured.out

    def test_dry_run_shows_validation_errors(self, tmp_path, capsys):
        """Dry-run shows validation errors when workflow is invalid."""
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="invalid"),
            ],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), dry_run=True, plain=True)
        engine.run()
        captured = capsys.readouterr()
        assert "error" in captured.out

    def test_dry_run_shows_phase_summary(self, tmp_path, capsys):
        """Dry-run shows phase type counts."""
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
                Phase(id="b", type="implement", prompt="B."),
                Phase(id="c", type="script", run="true"),
                Phase(id="d", type="check", role="tester"),
            ],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), dry_run=True, plain=True)
        engine.run()
        captured = capsys.readouterr()
        assert "Phase summary:" in captured.out
        assert "implement: 2" in captured.out
        assert "script: 1" in captured.out
        assert "check: 1" in captured.out

    def test_dry_run_shows_execution_plan(self, tmp_path, capsys):
        """Dry-run shows detailed execution plan."""
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="setup", type="implement", prompt="Set up the project.", timeout=120),
                Phase(id="build", type="script", run="make build", env={"CC": "gcc"}),
                Phase(id="review", type="check", role="tester", bounce_target="setup"),
            ],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), dry_run=True, plain=True)
        engine.run()
        captured = capsys.readouterr()
        assert "Execution plan:" in captured.out
        assert "[implement] setup" in captured.out
        assert "timeout=120s" in captured.out
        assert "[script] build: make build" in captured.out
        assert "[check] review: tester" in captured.out
        assert "bounce->setup" in captured.out

    def test_dry_run_shows_backoff(self, tmp_path, capsys):
        """Dry-run shows backoff configuration when set."""
        workflow = Workflow(
            name="test",
            phases=[Phase(id="a", type="implement", prompt="A.")],
            backoff=2.0,
            max_backoff=30.0,
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), dry_run=True, plain=True)
        engine.run()
        captured = capsys.readouterr()
        assert "Backoff: 2.0s base" in captured.out
        assert "30.0s max" in captured.out

    def test_dry_run_shows_notifications(self, tmp_path, capsys):
        """Dry-run shows notification count when configured."""
        workflow = Workflow(
            name="test",
            phases=[Phase(id="a", type="implement", prompt="A.")],
            notify=["https://example.com/hook"],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), dry_run=True, plain=True)
        engine.run()
        captured = capsys.readouterr()
        assert "1 webhook" in captured.out

    def test_dry_run_shows_parallel_groups(self, tmp_path, capsys):
        """Dry-run shows parallel groups."""
        workflow = Workflow(
            name="test",
            phases=[
                Phase(id="a", type="implement", prompt="A."),
                Phase(id="b", type="implement", prompt="B."),
            ],
            parallel_groups=[["a", "b"]],
        )
        engine = Engine(workflow, state_file=str(tmp_path / "state.json"), dry_run=True, plain=True)
        engine.run()
        captured = capsys.readouterr()
        assert "Parallel groups:" in captured.out
        assert "['a', 'b']" in captured.out
