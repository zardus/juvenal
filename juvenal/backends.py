"""AI backend subprocess management — Claude and Codex."""

from __future__ import annotations

import json
import subprocess
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class AgentResult:
    """Result from running an agent subprocess."""

    exit_code: int
    output: str  # final assistant messages
    transcript: str  # full transcript including tool calls
    duration: float  # seconds


class Backend(ABC):
    """Abstract base for AI agent backends."""

    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def run_agent(
        self,
        prompt: str,
        working_dir: str,
        display_callback: Callable[[str], None] | None = None,
    ) -> AgentResult:
        """Run an agent with the given prompt. Returns AgentResult."""
        ...


class ClaudeBackend(Backend):
    """Claude CLI backend using stream-json output."""

    def name(self) -> str:
        return "claude"

    def run_agent(
        self,
        prompt: str,
        working_dir: str,
        display_callback: Callable[[str], None] | None = None,
    ) -> AgentResult:
        cmd = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--allowedTools",
            "Bash,Edit,Read,Write,Glob,Grep,WebFetch,WebSearch",
            "--",
            prompt,
        ]

        start = time.time()
        proc = subprocess.Popen(
            cmd,
            cwd=working_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        transcript_lines: list[str] = []
        assistant_messages: list[str] = []

        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            event = _parse_json_event(line)
            if event:
                display_text, assistant_text = _process_claude_event(event)
                if display_text:
                    transcript_lines.append(display_text)
                    if display_callback:
                        display_callback(display_text)
                if assistant_text:
                    assistant_messages.append(assistant_text)
            else:
                transcript_lines.append(line)
                if display_callback:
                    display_callback(line)

        stderr_output = proc.stderr.read()
        returncode = proc.wait()
        duration = time.time() - start

        if stderr_output:
            transcript_lines.append(f"[stderr] {stderr_output}")

        output = "\n".join(assistant_messages)
        if returncode != 0 and not output:
            output = stderr_output or "\n".join(transcript_lines)

        return AgentResult(
            exit_code=returncode,
            output=output,
            transcript="\n".join(transcript_lines),
            duration=duration,
        )


class CodexBackend(Backend):
    """Codex CLI backend using NDJSON streaming."""

    def name(self) -> str:
        return "codex"

    def run_agent(
        self,
        prompt: str,
        working_dir: str,
        display_callback: Callable[[str], None] | None = None,
    ) -> AgentResult:
        cmd = [
            "npx",
            "@openai/codex@latest",
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--ephemeral",
            "-C",
            working_dir,
            prompt,
        ]

        start = time.time()
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        transcript_lines: list[str] = []
        assistant_messages: list[str] = []

        for raw_line in proc.stdout:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            event = _parse_json_event(line)
            if event:
                display_text, assistant_text = _process_codex_event(event)
                if display_text:
                    transcript_lines.append(display_text)
                    if display_callback:
                        display_callback(display_text)
                if assistant_text:
                    assistant_messages.append(assistant_text)
            else:
                transcript_lines.append(line)
                if display_callback:
                    display_callback(line)

        returncode = proc.wait()
        duration = time.time() - start

        output = "\n".join(assistant_messages)
        if returncode != 0 and not output:
            output = "\n".join(transcript_lines)

        return AgentResult(
            exit_code=returncode,
            output=output,
            transcript="\n".join(transcript_lines),
            duration=duration,
        )


def create_backend(name: str) -> Backend:
    """Factory to create a backend by name."""
    if name == "claude":
        return ClaudeBackend()
    elif name == "codex":
        return CodexBackend()
    else:
        raise ValueError(f"Unknown backend: {name!r}. Must be 'claude' or 'codex'.")


def _parse_json_event(line: str) -> dict | None:
    """Try to parse a line as a JSON event."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _process_claude_event(event: dict) -> tuple[str, str]:
    """Process a Claude stream-json event.

    Returns (display_text, assistant_text).
    """
    event_type = event.get("type", "")

    # Claude stream-json types
    if event_type == "assistant":
        text = event.get("message", "")
        if isinstance(text, dict):
            text = text.get("content", "")
        if isinstance(text, list):
            parts = []
            for block in text:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            text = "\n".join(parts)
        if text:
            return text, text
        return "", ""

    if event_type == "content_block_delta":
        delta = event.get("delta", {})
        text = delta.get("text", "")
        return text, ""

    if event_type == "result":
        # Final result message
        text = event.get("result", "")
        if text:
            return text, text
        # Handle subtype
        subtype = event.get("subtype", "")
        if subtype == "success":
            return "", ""
        return "", ""

    if event_type == "tool_use":
        tool_name = event.get("name", event.get("tool", "unknown"))
        return f"[tool: {tool_name}]", ""

    if event_type == "system":
        msg = event.get("message", "")
        return f"[system] {msg}" if msg else "", ""

    return "", ""


def _process_codex_event(event: dict) -> tuple[str, str]:
    """Process a Codex NDJSON event.

    Returns (display_text, assistant_text).
    """
    event_type = event.get("type", "")

    if event_type == "item.completed":
        item = event.get("item", {})
        item_type = item.get("type", "")
        text = item.get("text", "")

        if item_type == "reasoning":
            return f"[thinking] {text[:200]}", ""
        elif item_type == "agent_message":
            return text, text
        elif item_type == "tool_call":
            tool_name = item.get("name", "unknown")
            return f"[tool: {tool_name}]", ""
        elif text:
            return text, text
        return "", ""

    if event_type == "turn.completed":
        usage = event.get("usage", {})
        if usage:
            inp = usage.get("input_tokens", 0)
            out = usage.get("output_tokens", 0)
            return f"[tokens: {inp} in, {out} out]", ""
        return "", ""

    return "", ""
