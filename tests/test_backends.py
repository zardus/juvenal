"""Unit tests for backend helper functions and factory."""

import pytest

from juvenal.backends import (
    ClaudeBackend,
    CodexBackend,
    _extract_claude_tokens,
    _extract_codex_tokens,
    _parse_json_event,
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
        display, assistant = _process_claude_event({"type": "result", "result": "final output"})
        assert display == "final output"
        assert assistant == "final output"

    def test_result_success_subtype(self):
        display, assistant = _process_claude_event({"type": "result", "subtype": "success"})
        assert display == ""
        assert assistant == ""

    def test_tool_use(self):
        display, assistant = _process_claude_event({"type": "tool_use", "name": "Write"})
        assert "Write" in display
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


class TestKillActive:
    def test_kill_active_empty(self):
        backend = ClaudeBackend()
        backend.kill_active()  # should not raise
        assert backend._active_procs == []
