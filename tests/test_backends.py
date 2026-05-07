"""Unit tests for backend helper functions and factory."""

import os
import sys

import pytest

from juvenal.backends import (
    ClaudeBackend,
    CodexBackend,
    InteractiveResult,
    _extract_claude_tokens,
    _extract_codex_tokens,
    _parse_json_event,
    _prepend_juvenal_bin_to_path,
    _process_claude_event,
    _process_codex_event,
    create_backend,
)


class TestCreateBackend:
    def test_claude(self):
        backend = create_backend("claude")
        assert isinstance(backend, ClaudeBackend)
        assert backend.name() == "claude"

    def test_codex(self):
        backend = create_backend("codex")
        assert isinstance(backend, CodexBackend)
        assert backend.name() == "codex"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown backend"):
            create_backend("gpt")


class TestParseJsonEvent:
    def test_valid_json_object(self):
        assert _parse_json_event('{"type": "assistant"}') == {"type": "assistant"}

    def test_non_json_line(self):
        assert _parse_json_event("plain text output") is None

    def test_invalid_json(self):
        assert _parse_json_event("{broken json") is None

    def test_json_array_returns_none(self):
        assert _parse_json_event("[1, 2, 3]") is None

    def test_json_string_returns_none(self):
        assert _parse_json_event('"just a string"') is None

    def test_empty_line(self):
        assert _parse_json_event("") is None

    def test_whitespace_before_json(self):
        assert _parse_json_event('  {"type": "x"}') == {"type": "x"}


class TestProcessClaudeEvent:
    def test_assistant_text(self):
        display, assistant = _process_claude_event({"type": "assistant", "message": "hello"})
        assert display == "hello"
        assert assistant == "hello"

    def test_assistant_dict_message(self):
        display, assistant = _process_claude_event({"type": "assistant", "message": {"content": "hi"}})
        assert display == "hi"
        assert assistant == "hi"

    def test_assistant_list_message(self):
        event = {
            "type": "assistant",
            "message": [{"type": "text", "text": "part1"}, {"type": "text", "text": "part2"}],
        }
        display, assistant = _process_claude_event(event)
        assert "part1" in display
        assert "part2" in display

    def test_content_block_delta(self):
        display, assistant = _process_claude_event({"type": "content_block_delta", "delta": {"text": "chunk"}})
        assert display == "chunk"
        assert assistant == ""

    def test_result_event(self):
        # The `result` event echoes the final assistant text; prior `assistant`
        # events already streamed it to display, so we must NOT re-emit it as
        # display_text or it shows twice in the live buffer.
        display, assistant = _process_claude_event({"type": "result", "result": "final output"})
        assert display == ""
        assert assistant == "final output"

    def test_result_success_subtype(self):
        display, assistant = _process_claude_event({"type": "result", "subtype": "success"})
        assert display == ""
        assert assistant == ""

    def test_assistant_tool_use_block_skipped(self):
        # tool_use blocks are intentionally not rendered — too noisy.
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Write", "input": {"file_path": "/tmp/x.txt"}},
                ]
            },
        }
        display, assistant = _process_claude_event(event)
        assert display == ""
        assert assistant == ""

    def test_assistant_thinking_block(self):
        event = {
            "type": "assistant",
            "message": {"content": [{"type": "thinking", "thinking": "let me consider this"}]},
        }
        display, assistant = _process_claude_event(event)
        assert "[thinking]" in display
        assert "let me consider this" in display
        assert assistant == ""

    def test_assistant_mixed_text_and_tool(self):
        event = {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "I'll read it"},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "a.py"}},
                ]
            },
        }
        display, assistant = _process_claude_event(event)
        assert display == "I'll read it"
        assert assistant == "I'll read it"

    def test_user_tool_result_skipped(self):
        # tool_result events from `user` messages are intentionally not rendered.
        event = {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "content": [{"type": "text", "text": "hello world"}]},
                ]
            },
        }
        display, assistant = _process_claude_event(event)
        assert display == ""
        assert assistant == ""

    def test_system_event(self):
        display, assistant = _process_claude_event({"type": "system", "message": "init"})
        assert "init" in display
        assert assistant == ""

    def test_unknown_event(self):
        display, assistant = _process_claude_event({"type": "unknown_type"})
        assert display == ""
        assert assistant == ""


class TestProcessCodexEvent:
    def test_agent_message(self):
        event = {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}
        display, assistant = _process_codex_event(event)
        assert display == "done"
        assert assistant == "done"

    def test_reasoning(self):
        event = {"type": "item.completed", "item": {"type": "reasoning", "text": "thinking..."}}
        display, assistant = _process_codex_event(event)
        assert "thinking" in display
        assert assistant == ""

    def test_tool_call(self):
        event = {"type": "item.completed", "item": {"type": "tool_call", "name": "shell"}}
        display, assistant = _process_codex_event(event)
        assert "shell" in display
        assert assistant == ""

    def test_turn_completed_with_usage(self):
        event = {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 50}}
        display, assistant = _process_codex_event(event)
        assert "100" in display
        assert "50" in display
        assert assistant == ""

    def test_turn_completed_no_usage(self):
        display, assistant = _process_codex_event({"type": "turn.completed"})
        assert display == ""

    def test_unknown_event(self):
        display, assistant = _process_codex_event({"type": "something.else"})
        assert display == ""
        assert assistant == ""


class TestExtractClaudeTokens:
    def test_result_with_usage(self):
        event = {"type": "result", "usage": {"input_tokens": 500, "output_tokens": 200}}
        assert _extract_claude_tokens(event) == (500, 200)

    def test_result_no_usage(self):
        assert _extract_claude_tokens({"type": "result"}) == (0, 0)

    def test_non_result_event(self):
        assert _extract_claude_tokens({"type": "assistant", "usage": {"input_tokens": 100}}) == (0, 0)


class TestExtractCodexTokens:
    def test_turn_completed_with_usage(self):
        event = {"type": "turn.completed", "usage": {"input_tokens": 300, "output_tokens": 100}}
        assert _extract_codex_tokens(event) == (300, 100)

    def test_turn_completed_no_usage(self):
        assert _extract_codex_tokens({"type": "turn.completed"}) == (0, 0)

    def test_non_turn_event(self):
        assert _extract_codex_tokens({"type": "item.completed"}) == (0, 0)


class TestInteractiveResult:
    def test_dataclass_fields(self):
        result = InteractiveResult(session_id="abc-123", exit_code=0)
        assert result.session_id == "abc-123"
        assert result.exit_code == 0

    def test_nonzero_exit(self):
        result = InteractiveResult(session_id="def-456", exit_code=1)
        assert result.exit_code == 1


class TestRunInteractive:
    def test_codex_raises_not_implemented(self):
        backend = CodexBackend()
        with pytest.raises(NotImplementedError, match="codex.*does not support interactive"):
            backend.run_interactive("prompt", "/tmp")


class TestKillActive:
    def test_kill_active_empty(self):
        backend = ClaudeBackend()
        backend.kill_active()  # should not raise
        assert backend._active_procs == []


class TestPrependJuvenalBinToPath:
    """Regression: agent subprocesses must see the running juvenal's venv bin first.

    Without this, `pipx run juvenal …` launches an agent whose `python` resolves to
    the system interpreter and imports a stale ~/.local/lib juvenal — exactly the
    failure that produced 67 retries on `--linear` in the wild.
    """

    @pytest.fixture
    def fake_bin(self, tmp_path, monkeypatch):
        fake_python = tmp_path / "venv" / "bin" / "python"
        fake_python.parent.mkdir(parents=True)
        fake_python.touch()
        monkeypatch.setattr(sys, "executable", str(fake_python))
        return str(fake_python.parent)

    def test_prepends_when_path_missing(self, fake_bin):
        env: dict[str, str] = {}
        _prepend_juvenal_bin_to_path(env)
        assert env["PATH"] == fake_bin

    def test_prepends_when_path_present(self, fake_bin):
        env = {"PATH": os.pathsep.join(["/opt/a", "/opt/b"])}
        _prepend_juvenal_bin_to_path(env)
        parts = env["PATH"].split(os.pathsep)
        assert parts == [fake_bin, "/opt/a", "/opt/b"]

    def test_idempotent_when_already_first(self, fake_bin):
        original = os.pathsep.join([fake_bin, "/opt/a"])
        env = {"PATH": original}
        _prepend_juvenal_bin_to_path(env)
        assert env["PATH"] == original

    def test_dedupes_when_present_later(self, fake_bin):
        env = {"PATH": os.pathsep.join(["/opt/a", fake_bin, "/opt/b"])}
        _prepend_juvenal_bin_to_path(env)
        assert env["PATH"].split(os.pathsep) == [fake_bin, "/opt/a", "/opt/b"]

    def test_resolves_symlink(self, tmp_path, monkeypatch):
        real_bin = tmp_path / "real" / "bin"
        real_bin.mkdir(parents=True)
        (real_bin / "python").touch()
        link_bin = tmp_path / "link"
        link_bin.symlink_to(real_bin)
        monkeypatch.setattr(sys, "executable", str(link_bin / "python"))

        env: dict[str, str] = {}
        _prepend_juvenal_bin_to_path(env)
        assert env["PATH"] == str(real_bin)
