"""AI backend subprocess management — Claude and Codex."""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
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
    input_tokens: int = 0
    output_tokens: int = 0
    session_id: str | None = None


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
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        """Run an agent with the given prompt. Returns AgentResult."""
        ...

    def resume_agent(
        self,
        session_id: str,
        prompt: str,
        working_dir: str,
        display_callback: Callable[[str], None] | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        """Resume an existing agent session. Default falls back to run_agent."""
        return self.run_agent(prompt, working_dir, display_callback, timeout, env)


class ClaudeBackend(Backend):
    """Claude CLI backend using stream-json output."""

    def name(self) -> str:
        return "claude"

    def run_agent(
        self,
        prompt: str,
        working_dir: str,
        display_callback: Callable[[str], None] | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        session_id = uuid.uuid4().hex
        cmd = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
            "--verbose",
            "--session-id",
            session_id,
            prompt,
        ]
        result = self._run_claude_process(cmd, working_dir, display_callback, timeout, env)
        result.session_id = session_id
        return result

    def resume_agent(
        self,
        session_id: str,
        prompt: str,
        working_dir: str,
        display_callback: Callable[[str], None] | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        cmd = [
            "claude",
            "-p",
            "--output-format",
            "stream-json",
            "--dangerously-skip-permissions",
            "--verbose",
            "--resume",
            session_id,
            prompt,
        ]
        result = self._run_claude_process(cmd, working_dir, display_callback, timeout, env)
        result.session_id = session_id
        return result

    def _run_claude_process(
        self,
        cmd: list[str],
        working_dir: str,
        display_callback: Callable[[str], None] | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        # Strip CLAUDECODE env var so juvenal can be invoked from inside Claude Code
        proc_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        if env:
            proc_env.update(env)

        start = time.time()
        proc = subprocess.Popen(
            cmd,
            cwd=working_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=proc_env,
        )

        transcript_lines: list[str] = []
        assistant_messages: list[str] = []
        total_input_tokens = 0
        total_output_tokens = 0

        try:
            for raw_line in proc.stdout:
                if timeout and (time.time() - start) > timeout:
                    proc.kill()
                    proc.wait()
                    return AgentResult(
                        exit_code=1,
                        output=f"Agent timed out after {timeout}s",
                        transcript="\n".join(transcript_lines),
                        duration=time.time() - start,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                    )
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                event = _parse_json_event(line)
                if event:
                    display_text, assistant_text = _process_claude_event(event)
                    inp, out = _extract_claude_tokens(event)
                    total_input_tokens += inp
                    total_output_tokens += out
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
        except Exception:
            proc.kill()
            proc.wait()
            raise

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
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
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
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        cmd = [
            "npx",
            "@openai/codex@latest",
            "exec",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            prompt,
        ]
        return self._run_codex_process(cmd, working_dir, display_callback, timeout, env)

    def resume_agent(
        self,
        session_id: str,
        prompt: str,
        working_dir: str,
        display_callback: Callable[[str], None] | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        cmd = [
            "npx",
            "@openai/codex@latest",
            "exec",
            "resume",
            session_id,
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            prompt,
        ]
        result = self._run_codex_process(cmd, working_dir, display_callback, timeout, env)
        result.session_id = session_id
        return result

    def _run_codex_process(
        self,
        cmd: list[str],
        working_dir: str,
        display_callback: Callable[[str], None] | None = None,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        proc_env = dict(os.environ)
        if env:
            proc_env.update(env)

        start = time.time()
        proc = subprocess.Popen(
            cmd,
            cwd=working_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=proc_env,
        )

        transcript_lines: list[str] = []
        assistant_messages: list[str] = []
        total_input_tokens = 0
        total_output_tokens = 0
        thread_id: str | None = None

        try:
            for raw_line in proc.stdout:
                if timeout and (time.time() - start) > timeout:
                    proc.kill()
                    proc.wait()
                    return AgentResult(
                        exit_code=1,
                        output=f"Agent timed out after {timeout}s",
                        transcript="\n".join(transcript_lines),
                        duration=time.time() - start,
                        input_tokens=total_input_tokens,
                        output_tokens=total_output_tokens,
                    )
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                event = _parse_json_event(line)
                if event:
                    # Capture thread_id from thread.started event
                    if event.get("type") == "thread.started" and "thread_id" in event:
                        thread_id = event["thread_id"]
                    display_text, assistant_text = _process_codex_event(event)
                    inp, out = _extract_codex_tokens(event)
                    total_input_tokens += inp
                    total_output_tokens += out
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
        except Exception:
            proc.kill()
            proc.wait()
            raise

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
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            session_id=thread_id,
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


def _extract_claude_tokens(event: dict) -> tuple[int, int]:
    """Extract token usage from a Claude event. Returns (input_tokens, output_tokens)."""
    if event.get("type") == "result":
        usage = event.get("usage", {})
        if usage:
            return usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    return 0, 0


def _extract_codex_tokens(event: dict) -> tuple[int, int]:
    """Extract token usage from a Codex event. Returns (input_tokens, output_tokens)."""
    if event.get("type") == "turn.completed":
        usage = event.get("usage", {})
        if usage:
            return usage.get("input_tokens", 0), usage.get("output_tokens", 0)
    return 0, 0
